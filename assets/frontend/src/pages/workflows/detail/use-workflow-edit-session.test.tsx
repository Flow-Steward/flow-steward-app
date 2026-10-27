import { act, renderHook, waitFor } from "@testing-library/react";
import { useMemo } from "react";
import { definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import { useWorkflowChangeSet } from "./detail-change-set";
import { useWorkflowEditSession } from "./use-workflow-edit-session";
import { createWorkflowEditSessionStore, readSessionReference, sessionReferenceKey } from "./workflow-edit-session-store";
import { fakeIndexedDB, fakeLocks, fakeTabStorage } from "./workflow-edit-session.test-helpers";

/**
 * The edit session across reloads, Apply, tabs and users (Task 5). A reload is
 * a new mount with the same tab storage, database and locks; the page does not
 * end the session on unload.
 */

const TYPED_PASSWORD = "typed-control-password"; // pragma: allowlist secret
const userA = { accountId: "account-a", userId: "user-a", workflowId: "wf" };
const ref = (id: string) => ({ kind: "step" as const, id });
const step = (id: string) => ({ step_id: id, step_kind: "set_variable", display_name: id });
const workflowAt = (revision: number, stepIds: string[], description = "Described") => ({
  workflow_id: "wf",
  revision,
  enabled: false,
  display_name: "Flow",
  description,
  steps: stepIds.map(step),
  groups: [],
  for_each: [],
  settings: { execution_graph: { schema_version: 1, entry: ref(stepIds[0]), edges: [] } },
});
const create = (id: string): WorkflowChangeOperation => ({ op: "create_step", step: step(id) });
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });

function server(initial = 12) {
  const state = { revision: initial, writes: [] as Array<Record<string, unknown>>, requests: [] as string[] };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    state.requests.push(url);
    if (!url.endsWith("/changes")) throw new Error(`Unexpected request: ${url}`);
    const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
    if (body.expected_revision !== state.revision) return response({ detail: { code: "workflow_revision_conflict" } }, 409);
    if (body.dry_run) return response({ workflow_id: "wf", revision: state.revision, applied: false, valid: true, issues: [], blocking_issues: [], workflow: {} });
    state.writes.push(body);
    state.revision += 1;
    return response({ workflow_id: "wf", revision: state.revision, valid: true, issues: [], workflow: {} });
  });
  return state;
}

type Browser = { store: ReturnType<typeof createWorkflowEditSessionStore>; tab: ReturnType<typeof fakeTabStorage>; locks: ReturnType<typeof fakeLocks> };

function browser(options: { failWrites?: boolean } = {}) {
  const database = fakeIndexedDB(options);
  return { database, store: createWorkflowEditSessionStore(database.factory), tab: fakeTabStorage(), locks: fakeLocks() };
}

function openTab(tabBrowser: Browser, { identity = userA, workflow = workflowAt(12, ["a"]) } = {}) {
  const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "wf", revision: workflow.revision }));
  const view = renderHook(({ record, who }) => {
    const serverDefinition = useMemo(() => definitionFromWorkflow(record), [record]);
    const changeSet = useWorkflowChangeSet({
      workflowId: who.workflowId, serverDefinition, serverRevision: record.revision, refetchWorkflowSurfaces,
    });
    const session = useWorkflowEditSession({
      enabled: true, identity: who, workflow: record, serverRevision: record.revision, changeSet,
      store: tabBrowser.store, tabStorage: tabBrowser.tab, locks: tabBrowser.locks,
    });
    return { changeSet, session };
  }, { initialProps: { record: workflow, who: identity } });
  return { get state() { return view.result.current; }, unmount: () => view.unmount(), rerender: view.rerender };
}

type Mounted = ReturnType<typeof openTab>;
const active = (view: Mounted) => waitFor(() => expect(view.state.session.status).toBe("active"));
const saved = (view: Mounted) => waitFor(() => expect(view.state.session.persistence).toBe("saved"));
/** A reload or a switch to this workflow's other page: the session is kept, not ended. */
async function reload(view: Mounted) {
  act(() => view.state.session.noteNavigation("/registry/workflows/wf/builder"));
  view.unmount();
  // The closed page's Web Lock is released before the next page asks for it.
  await new Promise((resolve) => setTimeout(resolve, 0));
}
const writes = (state: { writes: unknown[] }) => state.writes.length;

afterEach(() => vi.restoreAllMocks());

it("restores the graph, form drafts, history cursor and Redo after a reload, without writing", async () => {
  const state = server();
  const tab = browser();
  const first = openTab(tab);
  await active(first);
  act(() => first.state.changeSet.stage(create("b")));
  act(() => first.state.changeSet.stage(create("c")));
  act(() => first.state.changeSet.undoGraphIntent());
  act(() => first.state.session.setFormDraft("step:b", { display_name: "Draft name" }));
  await saved(first);
  await reload(first);

  const second = openTab(tab);
  await active(second);
  await waitFor(() => expect(second.state.changeSet.operations).toEqual([create("b")]));
  expect(second.state.changeSet.canRedo).toBe(true);
  act(() => second.state.changeSet.redoGraphIntent());
  expect(second.state.changeSet.operations).toEqual([create("b"), create("c")]);
  expect(second.state.session.formDrafts["step:b"]?.value).toEqual({ display_name: "Draft name" });
  expect(writes(state)).toBe(0);
  expect(state.requests.filter((url) => url.includes("/restore"))).toEqual([]);
});

it("keeps Undo across Apply and reload: Undo stages the reverse, the next Apply writes it at the checkpoint", async () => {
  const state = server();
  const tab = browser();
  const first = openTab(tab);
  await active(first);
  act(() => first.state.changeSet.stage(create("b")));
  act(() => first.state.changeSet.stage(create("c")));
  await act(async () => { expect(await first.state.changeSet.apply()).toBe(true); });
  first.rerender({ record: workflowAt(13, ["a", "b", "c"]), who: userA });
  await waitFor(() => expect(tab.database.record(first.state.session.sessionId)).toMatchObject({ serverCheckpoint: { revision: 13 } }));
  await reload(first);

  const second = openTab(tab, { workflow: workflowAt(13, ["a", "b", "c"]) });
  await active(second);
  await waitFor(() => expect(second.state.changeSet.canUndo).toBe(true));
  expect(second.state.changeSet.hasChanges).toBe(false);
  act(() => second.state.changeSet.undoGraphIntent());
  expect(second.state.changeSet.operations).toEqual([{ op: "delete_step", step_id: "c" }]);
  expect(writes(state)).toBe(1); // Undo never writes.
  await act(async () => { expect(await second.state.changeSet.apply()).toBe(true); });
  expect(state.writes.at(-1)).toEqual({ expected_revision: 13, operations: [{ op: "delete_step", step_id: "c" }] });
  expect(state.revision).toBe(14);
  expect(second.state.changeSet.canRedo).toBe(true);
});

it("drops Redo when a new edit follows a recovered Undo", async () => {
  server();
  const tab = browser();
  const first = openTab(tab);
  await active(first);
  act(() => first.state.changeSet.stage(create("b")));
  act(() => first.state.changeSet.undoGraphIntent());
  await saved(first);
  act(() => first.state.session.setFormDraft("keep", { open: true }));
  await saved(first);
  await reload(first);
  const second = openTab(tab);
  await active(second);
  await waitFor(() => expect(second.state.changeSet.canRedo).toBe(true));
  act(() => second.state.changeSet.stage(create("d")));
  expect(second.state.changeSet.canRedo).toBe(false);
});

it("gives a duplicated tab its own session record before its first write", async () => {
  server();
  const tab = browser();
  const first = openTab(tab);
  await active(first);
  act(() => first.state.changeSet.stage(create("b")));
  await saved(first);
  const firstId = first.state.session.sessionId;

  const duplicate = { ...tab, tab: tab.tab.duplicate() };
  expect(readSessionReference(userA, duplicate.tab)).toBe(firstId);
  const second = openTab(duplicate);
  await active(second);
  const secondId = second.state.session.sessionId;
  expect(secondId).not.toBe(firstId);
  expect(readSessionReference(userA, duplicate.tab)).toBe(secondId);
  await waitFor(() => expect(second.state.changeSet.operations).toEqual([create("b")]));
  act(() => second.state.changeSet.stage(create("c")));
  await saved(second);

  const original = tab.database.record(firstId) as { history: { pending: unknown[] } };
  const copy = tab.database.record(secondId) as { history: { pending: unknown[] } };
  expect(original.history.pending).toHaveLength(1);
  expect(copy.history.pending).toHaveLength(2);
});

it("isolates sessions by account, user and workflow", async () => {
  server();
  const tab = browser();
  const first = openTab(tab);
  await active(first);
  act(() => first.state.changeSet.stage(create("b")));
  await saved(first);
  const firstId = first.state.session.sessionId;
  await reload(first);

  // Another user in the same tab starts from the server.
  const userB = { ...userA, userId: "user-b" };
  const other = openTab(tab, { identity: userB });
  await active(other);
  expect(other.state.changeSet.operations).toEqual([]);
  other.unmount();

  // A reference that points at someone else's record is never applied.
  tab.tab.setItem(sessionReferenceKey(userB), firstId);
  const pointed = openTab(tab, { identity: userB });
  await waitFor(() => expect(pointed.state.session.status).toBe("recovery_unavailable"));
  expect(pointed.state.session.problem).toBe("identity");
  expect(pointed.state.changeSet.operations).toEqual([]);
  expect(tab.database.record(firstId)).toBeDefined();
});

it.each([
  ["version", { schemaVersion: 99 }],
  ["corrupt", { history: { items: "broken" } }],
])("reports a %s record as unrecoverable, keeps it until deleted, and applies nothing", async (problem, damage) => {
  const state = server();
  const tab = browser();
  const first = openTab(tab);
  await active(first);
  act(() => first.state.changeSet.stage(create("b")));
  await saved(first);
  const id = first.state.session.sessionId;
  await reload(first);
  tab.database.put(id, { ...(tab.database.record(id) ?? {}), ...damage });

  const second = openTab(tab);
  await waitFor(() => expect(second.state.session.status).toBe("recovery_unavailable"));
  expect(second.state.session.problem).toBe(problem);
  expect(second.state.changeSet.operations).toEqual([]);
  expect(tab.database.record(id)).toBeDefined();
  await act(async () => { await second.state.session.deleteRecovery(); });
  expect(tab.database.record(id)).toBeUndefined();
  expect(second.state.session.status).toBe("active");
  expect(writes(state)).toBe(0);
});

it("says when the browser cannot keep the copy, and the keys keep working without any write", async () => {
  const state = server();
  const tab = browser({ failWrites: true });
  const view = openTab(tab);
  await active(view);
  act(() => view.state.changeSet.stage(create("b")));
  await waitFor(() => expect(view.state.session.persistence).toBe("failed"));
  expect(view.state.changeSet.canUndo).toBe(true);
  act(() => view.state.changeSet.undoGraphIntent());
  expect(view.state.changeSet.operations).toEqual([]);
  expect(writes(state)).toBe(0);
});

it("never stores a credential typed into a form", async () => {
  server();
  const tab = browser();
  const view = openTab(tab);
  await active(view);
  act(() => view.state.session.setFormDraft("settings", { description: "Draft", secretDraft: TYPED_PASSWORD }));
  await saved(view);
  const stored = JSON.stringify(tab.database.record(view.state.session.sessionId));
  expect(stored).not.toContain(TYPED_PASSWORD);
  expect(view.state.session.formDrafts.settings.reentry).toEqual(["secretDraft"]);
});

it("ends a clean session on a normal leave, and the next visit starts from the server", async () => {
  server();
  const tab = browser();
  const first = openTab(tab);
  await active(first);
  act(() => first.state.changeSet.stage(create("b")));
  await act(async () => { expect(await first.state.changeSet.apply()).toBe(true); });
  first.rerender({ record: workflowAt(13, ["a", "b"]), who: userA });
  await saved(first);
  const id = first.state.session.sessionId;
  expect(first.state.session.dirty).toBe(false);
  first.unmount();
  await waitFor(() => expect(tab.database.record(id)).toBeUndefined());
  expect(readSessionReference(userA, tab.tab)).toBeNull();

  const next = openTab(tab, { workflow: workflowAt(13, ["a", "b"]) });
  await active(next);
  expect(next.state.changeSet.canUndo).toBe(false);
});

it("keeps a dirty session unless the author chose to leave and discard", async () => {
  server();
  const tab = browser();
  const first = openTab(tab);
  await active(first);
  act(() => first.state.changeSet.stage(create("b")));
  await saved(first);
  const firstId = first.state.session.sessionId;
  first.unmount();
  expect(tab.database.record(firstId)).toBeDefined();
  await new Promise((resolve) => setTimeout(resolve, 0)); // the closed page's lock is released

  const second = openTab(tab);
  await active(second);
  expect(second.state.session.sessionId).toBe(firstId);
  second.unmount();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(tab.database.record(firstId)).toBeDefined(); // still dirty: kept

  const third = openTab(tab);
  await active(third);
  await waitFor(() => expect(third.state.changeSet.operations).toEqual([create("b")]));
  act(() => third.state.session.discardOnLeave());
  third.unmount();
  await waitFor(() => expect(tab.database.record(firstId)).toBeUndefined());
});

it("holds a recovered history when someone else saved meanwhile, instead of erasing or replaying it", async () => {
  const state = server();
  const tab = browser();
  const first = openTab(tab);
  await active(first);
  act(() => first.state.changeSet.stage(create("b")));
  await act(async () => { expect(await first.state.changeSet.apply()).toBe(true); });
  first.rerender({ record: workflowAt(13, ["a", "b"]), who: userA });
  await waitFor(() => expect(tab.database.record(first.state.session.sessionId)).toMatchObject({ serverCheckpoint: { revision: 13 } }));
  await reload(first);

  state.revision = 14;
  const second = openTab(tab, { workflow: workflowAt(14, ["a"], "Someone else") });
  await active(second);
  await waitFor(() => expect(second.state.changeSet.status).toBe("conflict"));
  expect(second.state.changeSet.checkpoint).toMatchObject({ revision: 13, held: true });
  expect(second.state.changeSet.preview.steps.map((row) => row.step_id)).toEqual(["a", "b"]);
  expect(second.state.changeSet.journal.items).toHaveLength(1);
  expect(second.state.session.dirty).toBe(true);
  expect(writes(state)).toBe(1);
});
