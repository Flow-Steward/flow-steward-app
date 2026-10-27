import { act, renderHook } from "@testing-library/react";
import { definitionFromWorkflow, type WorkflowDefinitionModel } from "../builder/change-set";
import type { WorkflowChangeOperation } from "../builder/api";
import { emptyGraphJournal, appendGraphIntent, applyGraphJournal } from "../builder/canvas-history";
import { useWorkflowChangeSet } from "./detail-change-set";
import { useWorkflowStepActions } from "./detail-step-actions";

/**
 * Canvas › Undo and the edit session: every author action is one Undo step in
 * the order it was made, the history survives Apply, Undo never writes, and a
 * newer revision someone else saved never replaces the local copy silently.
 */

const ref = (id: string) => ({ kind: "step" as const, id });
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
const workflow = (text: string, extra: Record<string, unknown> = {}) => ({
  workflow_id: "workflow_a",
  revision: 7,
  steps: [
    { step_id: "start", step_kind: "agent", display_name: "Start" },
    { step_id: "compose", step_kind: "template", display_name: "Compose", override: { template: { text } } },
  ],
  groups: [],
  settings: { execution_graph: { schema_version: 1, entry: ref("start"), edges: [{ source: ref("start"), outcome: { name: "success" }, target: ref("compose") }] } },
  ...extra,
});
const base = (text: string) => definitionFromWorkflow(workflow(text));
const draftStep = (text: string) => ({ step_id: "draft", step_kind: "template", display_name: "Draft", override: { template: { text } } });
const draft = (text: string): WorkflowChangeOperation => ({ op: "create_step", step: draftStep(text) });
/** The saved definition with the draft step, at a revision. */
const withDraft = (text: string, revision: number) =>
  definitionFromWorkflow({ ...workflow("before"), revision, steps: [...workflow("before").steps, draftStep(text)] });
const other: WorkflowChangeOperation = { op: "create_step", step: { step_id: "other", step_kind: "stop_success", display_name: "Other" } };
const stepIds = (definition: WorkflowDefinitionModel) => definition.steps.map((step) => step.step_id);
const draftText = (definition: WorkflowDefinitionModel) =>
  (definition.steps.find((step) => step.step_id === "draft")?.override as { template?: { text?: string } } | undefined)?.template?.text;

/** A server that stores whatever Apply sends and counts the writes. */
function server(initial = 7) {
  const state = { revision: initial, writes: [] as Array<Record<string, unknown>> };
  const transport = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (!String(input).endsWith("/changes")) throw new Error(`Unexpected request: ${String(input)}`);
    const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
    if (body.expected_revision !== state.revision) return response({ detail: { code: "workflow_revision_conflict" } }, 409);
    if (body.dry_run) return response({ workflow_id: "workflow_a", revision: state.revision, applied: false, valid: true, issues: [], blocking_issues: [], workflow: {} });
    state.writes.push(body);
    state.revision += 1;
    return response({ workflow_id: "workflow_a", revision: state.revision, valid: true, issues: [], workflow: {} });
  });
  const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "workflow_a", revision: state.revision }));
  return { state, transport, refetchWorkflowSurfaces };
}

function mount(refetchWorkflowSurfaces: () => Promise<unknown>, definition = base("before"), revision = 7) {
  return renderHook(({ serverDefinition, serverRevision }) => useWorkflowChangeSet({
    workflowId: "workflow_a", serverDefinition, serverRevision, refetchWorkflowSurfaces,
  }), { initialProps: { serverDefinition: definition, serverRevision: revision } });
}

afterEach(() => vi.restoreAllMocks());

it("makes a form-staged edit an Undo step and never skips a later form edit", () => {
  const { refetchWorkflowSurfaces } = server();
  const view = mount(refetchWorkflowSurfaces);
  act(() => { view.result.current.stageGraphIntent([draft("first")]); }); // a canvas action
  act(() => { view.result.current.updateStagedStep("draft", { override: { template: { text: "edited" } } }); }); // the drawer's Save
  act(() => view.result.current.stage(other)); // a step added from the drawer or palette
  expect(view.result.current.canUndo).toBe(true);

  act(() => view.result.current.undoGraphIntent());
  expect(stepIds(view.result.current.preview)).toEqual(["start", "compose", "draft"]);
  expect(draftText(view.result.current.preview)).toBe("edited");
  act(() => view.result.current.undoGraphIntent());
  expect(draftText(view.result.current.preview)).toBe("first");
  act(() => view.result.current.undoGraphIntent());
  expect(stepIds(view.result.current.preview)).toEqual(["start", "compose"]);
  expect(view.result.current.status).toBe("clean");
  for (let index = 0; index < 3; index += 1) act(() => view.result.current.redoGraphIntent());
  expect(stepIds(view.result.current.preview)).toEqual(["start", "compose", "draft", "other"]);
  expect(draftText(view.result.current.preview)).toBe("edited");
  expect(view.result.current.canRedo).toBe(false);
});

it("ends Redo with a new edit, and an empty history changes nothing", () => {
  const { refetchWorkflowSurfaces } = server();
  const view = mount(refetchWorkflowSurfaces);
  act(() => view.result.current.undoGraphIntent());
  act(() => view.result.current.redoGraphIntent());
  expect(view.result.current.operations).toEqual([]);
  act(() => view.result.current.stage(draft("first")));
  act(() => view.result.current.undoGraphIntent());
  expect(view.result.current.canRedo).toBe(true);
  act(() => view.result.current.stage(other));
  expect(view.result.current.canRedo).toBe(false);
  act(() => view.result.current.redoGraphIntent());
  expect(view.result.current.operations).toEqual([other]);
});

it("keeps the history through Apply: Undo stages the reverse without writing, Redo returns to the checkpoint", async () => {
  const { state, refetchWorkflowSurfaces } = server();
  const view = mount(refetchWorkflowSurfaces);
  act(() => view.result.current.stage(draft("first")));
  await act(async () => { expect(await view.result.current.apply()).toBe(true); });
  expect(state.writes).toHaveLength(1);
  // The page reloads the saved definition (revision 8, own).
  view.rerender({ serverDefinition: withDraft("first", 8), serverRevision: 8 });
  expect(view.result.current.status).toBe("clean");
  // dirty_is_content_difference_not_nonempty_history
  expect(view.result.current.hasChanges).toBe(false);
  expect(view.result.current.canUndo).toBe(true);

  act(() => view.result.current.undoGraphIntent());
  expect(view.result.current.operations).toEqual([{ op: "delete_step", step_id: "draft" }]);
  expect(view.result.current.hasChanges).toBe(true);
  expect(stepIds(view.result.current.preview)).toEqual(["start", "compose"]);
  expect(state.writes).toHaveLength(1); // Undo never writes.

  act(() => view.result.current.redoGraphIntent());
  expect(view.result.current.hasChanges).toBe(false);
  expect(stepIds(view.result.current.preview)).toEqual(["start", "compose", "draft"]);

  // Undo past Apply, then the next explicit Apply writes the reverse at the checkpoint revision.
  act(() => view.result.current.undoGraphIntent());
  await act(async () => { expect(await view.result.current.apply()).toBe(true); });
  expect(state.writes.at(-1)).toEqual({ expected_revision: 8, operations: [{ op: "delete_step", step_id: "draft" }] });
});

it("records an own step parameter save as one Undo step that the next Apply takes back", async () => {
  const { state, refetchWorkflowSurfaces } = server();
  const view = renderHook(({ revision, serverDefinition }) => {
    const changeSet = useWorkflowChangeSet({ workflowId: "workflow_a", serverDefinition, serverRevision: revision, refetchWorkflowSurfaces });
    const step = useWorkflowStepActions({
      changeSet, workflowId: "workflow_a", serverRevision: revision, refetchWorkflowSurfaces,
      setSaveError: vi.fn(), setSaveNotice: vi.fn(), setSavingWorkflowSurface: vi.fn(),
      selectedStep: { local: false, runtimeId: "compose", stepKind: "template", raw: { step_id: "compose", step_kind: "template", override: { template: { text: "before" } } } },
      stepKindDraft: "template", stepDisplayName: "Compose", stepOverrideDraft: JSON.stringify({ template: { text: "after" } }),
      stepAgentId: "", stepBlockKey: "", stepOptional: false,
    });
    return { changeSet, step };
  }, { initialProps: { revision: 7, serverDefinition: base("before") } });
  await act(async () => { await view.result.current.step.handleSaveStep(); });
  expect(state.writes).toHaveLength(1);
  view.rerender({ revision: 8, serverDefinition: base("after") });
  expect(view.result.current.changeSet.status).toBe("clean");
  expect(view.result.current.changeSet.canUndo).toBe(true);
  act(() => view.result.current.changeSet.undoGraphIntent());
  const compose = view.result.current.changeSet.preview.steps.find((step) => step.step_id === "compose");
  expect(compose?.override).toEqual({ template: { text: "before" } });
  expect(view.result.current.changeSet.operations).toEqual([
    expect.objectContaining({ op: "update_step_config", step_id: "compose" }),
  ]);
  expect(state.writes).toHaveLength(1);
});

it("keeps the local copy and its Undo history when someone else saves, until the author chooses", async () => {
  const { refetchWorkflowSurfaces } = server();
  const view = mount(refetchWorkflowSurfaces);
  act(() => view.result.current.stage(draft("first")));
  await act(async () => { expect(await view.result.current.apply()).toBe(true); });
  view.rerender({ serverDefinition: withDraft("first", 8), serverRevision: 8 });
  expect(view.result.current.status).toBe("clean");
  // remote_change_does_not_silently_erase_clean_local_undo
  view.rerender({ serverDefinition: base("someone else"), serverRevision: 9 });
  expect(view.result.current.status).toBe("conflict");
  expect(view.result.current.checkpoint).toMatchObject({ revision: 8, held: true });
  expect(stepIds(view.result.current.preview)).toEqual(["start", "compose", "draft"]);
  expect(view.result.current.journal.items).toHaveLength(1);
  // "Accept the server version": a new base, an empty history, nothing written.
  act(() => view.result.current.acceptServerVersion());
  expect(view.result.current.status).toBe("clean");
  expect(view.result.current.canUndo).toBe(false);
  expect(view.result.current.journal.items).toEqual([]);
  expect(stepIds(view.result.current.preview)).toEqual(["start", "compose"]);
});

it("moves a clean checkpoint over a revision that changed nothing authored, keeping Undo", async () => {
  const { refetchWorkflowSurfaces } = server();
  const view = mount(refetchWorkflowSurfaces);
  act(() => view.result.current.stage(draft("first")));
  await act(async () => { expect(await view.result.current.apply()).toBe(true); });
  view.rerender({ serverDefinition: withDraft("first", 8), serverRevision: 8 });
  // Enable or a setting saved elsewhere: a new revision with the same definition.
  view.rerender({ serverDefinition: withDraft("first", 9), serverRevision: 9 });
  expect(view.result.current.status).toBe("clean");
  expect(view.result.current.canUndo).toBe(true);
});

it("keeps my version as a new revision and continues the same history", async () => {
  const { refetchWorkflowSurfaces } = server();
  const view = mount(refetchWorkflowSurfaces);
  act(() => view.result.current.stage(draft("mine")));
  view.rerender({ serverDefinition: base("theirs"), serverRevision: 8 });
  expect(view.result.current.status).toBe("conflict");
  expect(view.result.current.checkpoint).toMatchObject({ revision: 7, held: true });
  expect(draftText(view.result.current.preview)).toBe("mine");
  expect(view.result.current.preview.steps.find((step) => step.step_id === "compose")?.override).toEqual({ template: { text: "before" } });
  act(() => view.result.current.keepOwnVersionApplied(9));
  expect(view.result.current.status).toBe("clean");
  expect(view.result.current.checkpoint.held).toBe(false);
  expect(view.result.current.canUndo).toBe(true);
  // The written revision is this author's: it does not raise another conflict.
  view.rerender({ serverDefinition: withDraft("mine", 9), serverRevision: 9 });
  expect(view.result.current.status).toBe("clean");
  act(() => view.result.current.undoGraphIntent());
  expect(view.result.current.operations).toEqual([{ op: "delete_step", step_id: "draft" }]);
});

function recoveredJournal() {
  let journal = appendGraphIntent(emptyGraphJournal(), [draft("a")]);
  journal = applyGraphJournal(journal, base("before"));
  return appendGraphIntent(journal, [other]);
}

it("continues a recovered history, cursor and Redo on its own revision", () => {
  const { refetchWorkflowSurfaces } = server();
  const journal = recoveredJournal();
  const view = mount(refetchWorkflowSurfaces, withDraft("a", 12), 12);
  act(() => view.result.current.restoreJournal({ journal, checkpointRevision: 12, checkpointDefinition: withDraft("a", 12) }));
  expect(view.result.current.status).toBe("pending");
  expect(view.result.current.operations).toEqual([other]);
  expect(view.result.current.baseRevision).toBe(12);
  act(() => view.result.current.undoGraphIntent());
  act(() => view.result.current.undoGraphIntent());
  expect(view.result.current.operations).toEqual([{ op: "delete_step", step_id: "draft" }]);
  act(() => view.result.current.redoGraphIntent());
  act(() => view.result.current.redoGraphIntent());
  expect(view.result.current.operations).toEqual([other]);
});

it("holds a recovered copy on its checkpoint when the server moved on, without replaying it", () => {
  const { refetchWorkflowSurfaces, state } = server(13);
  const view = mount(refetchWorkflowSurfaces, base("someone else"), 13);
  act(() => view.result.current.restoreJournal({ journal: recoveredJournal(), checkpointRevision: 12, checkpointDefinition: withDraft("a", 12) }));
  expect(view.result.current.status).toBe("conflict");
  expect(view.result.current.checkpoint).toMatchObject({ revision: 12, held: true });
  expect(stepIds(view.result.current.preview)).toEqual(["start", "compose", "draft", "other"]);
  expect(view.result.current.preview.steps.find((step) => step.step_id === "compose")?.override).toEqual({ template: { text: "before" } });
  expect(state.writes).toEqual([]);
});
