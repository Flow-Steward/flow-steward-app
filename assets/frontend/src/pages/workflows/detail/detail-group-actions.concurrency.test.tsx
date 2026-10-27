import { act, renderHook } from "@testing-library/react";
import { useState, type SetStateAction } from "react";
import { definitionFromWorkflow } from "../builder/change-set";
import type { GroupDraft } from "./detail-drawer-controller";
import { useWorkflowChangeSet } from "./detail-change-set";
import { useWorkflowGroupActions } from "./detail-group-actions";

type Mode = "create" | "local" | "saved";
const draft: GroupDraft = {
  containerKind: "group", bodyKind: "lanes", concurrencyLimit: "0", description: "Description",
  displayName: "Workers", groupId: "workers", lanes: "first, second", over: "state.items",
  itemName: "item", indexName: "index", parentGroupId: "",
};
const lanes = [{ lane_id: "first", entry: { kind: "step", id: "one" }, metadata: { kept: true } }, { lane_id: "second" }];

function setup(value: string, kind: GroupDraft["containerKind"] = "group", mode: Mode = "create", overrides: Partial<GroupDraft> = {}, existingLimit: number | null = 3, missingSelection = false) {
  const shared = { display_name: "Workers", body: { kind: "lanes", lanes, ...(existingLimit === null ? {} : { concurrency_limit: existingLimit }) } };
  const group = { group_id: "workers", ...shared };
  const forEach = { for_each_id: "workers", over: "state.items", item_name: "item", index_name: "index", ...shared };
  const raw = kind === "group" ? group : forEach;
  const definition = definitionFromWorkflow({ workflow_id: "workflow_a", revision: 7, steps: [], groups: mode === "saved" && kind === "group" ? [group] : [], for_each: mode === "saved" && kind === "for_each" ? [forEach] : [] });
  const transport = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ workflow_id: "workflow_a", revision: 8 }), { status: 200, headers: { "content-type": "application/json" } }));
  const refetchWorkflowSurfaces = vi.fn(async () => ({}));
  const setSavingWorkflowSurface = vi.fn();
  const view = renderHook(() => {
    const [groupDraft, setGroupDraft] = useState({ ...draft, ...overrides, containerKind: kind, concurrencyLimit: value });
    // Match index.tsx's shared notice text and both wrapper flags.
    const [notice, setNoticeText] = useState("");
    const [noticeIsError, setNoticeIsError] = useState(false);
    const [groupFormSaveFailed, setGroupFormSaveFailed] = useState(false);
    const setSaveError = (message: SetStateAction<string>) => {
      setGroupFormSaveFailed(true);
      setNoticeText(message);
      setNoticeIsError(true);
    };
    const setSaveNotice = (message: string) => {
      setGroupFormSaveFailed(false);
      setNoticeText(message);
      setNoticeIsError(false);
    };
    const [creating, setGroupCreateMode] = useState(mode === "create");
    const changeSet = useWorkflowChangeSet({ workflowId: "workflow_a", serverDefinition: definition, serverRevision: 7, refetchWorkflowSurfaces });
    const actions = useWorkflowGroupActions({
      changeSet, groupCreateMode: creating, groupDraft, refetchWorkflowSurfaces,
      requestConfirmation: vi.fn(), selectedGroup: mode === "create" || missingSelection ? null : { local: mode === "local", groupId: "workers", raw }, serverRevision: 7,
      setGroupCreateMode, setGroupDraft, setSaveError, setSaveNotice,
      setSavingWorkflowSurface, setSelectedGroupId: vi.fn(), workflowId: "workflow_a",
    });
    return { actions, changeSet, error: noticeIsError ? notice : "", setSaveError, setSaveNotice, notice, noticeIsError, groupFormSaveFailed, groupDraft, setGroupDraft, creating };
  });
  if (mode === "local") act(() => view.result.current.changeSet.stage(kind === "group" ? { op: "create_group", group } : { op: "create_for_each", for_each: forEach }));
  return { ...view, transport, refetchWorkflowSurfaces, setSavingWorkflowSurface };
}

afterEach(() => vi.restoreAllMocks());

it.each(["error", "notice"] as const)("does not clear another owner's %s when the existing missing-selection guard makes saving a no-op", async (state) => {
  const view = setup("0", "group", "saved", {}, 2, true);
  await act(async () => { await view.result.current.actions.handleSaveGroup(); });
  expect(view.result.current.noticeIsError).toBe(true);
  const message = state === "error" ? "Another action reported a revision conflict." : "Another action completed successfully.";
  act(() => {
    if (state === "error") view.result.current.setSaveError(message);
    else view.result.current.setSaveNotice(message);
    view.result.current.setGroupDraft((current) => ({ ...current, concurrencyLimit: "2" }));
  });
  await act(async () => { await view.result.current.actions.handleSaveGroup(); });
  expect(view.result.current.notice).toBe(message);
  expect(view.result.current.noticeIsError).toBe(state === "error");
  expect(view.result.current.groupFormSaveFailed).toBe(state === "error");
  expect(view.result.current.changeSet.operations).toEqual([]);
  expect(view.transport).not.toHaveBeenCalled();
  expect(view.refetchWorkflowSurfaces).not.toHaveBeenCalled();
});

it("rejects 0, fractional and partially numeric input before real Group staging", async () => {
  const results = [];
  for (const value of ["0", "1.5", "2x"]) {
    const view = setup(value);
    await act(async () => { await view.result.current.actions.handleSaveGroup(); });
    results.push({ value, operations: view.result.current.changeSet.operations, creating: view.result.current.creating });
    view.unmount();
    vi.restoreAllMocks();
  }
  expect(results).toEqual([
    { value: "0", operations: [], creating: true },
    { value: "1.5", operations: [], creating: true },
    { value: "2x", operations: [], creating: true },
  ]);
});

describe.each(["group", "for_each"] as const)("%s lane concurrency submission", (kind) => {
  describe.each(["create", "local", "saved"] as const)("%s path", (mode) => {
    it.each(["0", "-1", "1.5", "2x", "NaN", "Infinity", "1e2", "9007199254740993"])("retains raw %s and leaves API, revision and pending operations untouched", async (value) => {
      const view = setup(value, kind, mode);
      const before = view.result.current.changeSet.operations;
      const preview = view.result.current.changeSet.preview;
      const revision = view.result.current.changeSet.baseRevision;
      await act(async () => { await view.result.current.actions.handleSaveGroup(); });
      expect(view.result.current.changeSet.operations).toBe(before);
      expect(view.result.current.changeSet.preview).toBe(preview);
      expect(view.result.current.changeSet.baseRevision).toBe(revision);
      expect(view.result.current.error).not.toBe("");
      expect(view.result.current.groupDraft.concurrencyLimit).toBe(value);
      expect(view.result.current.creating).toBe(mode === "create");
      expect(view.transport).not.toHaveBeenCalled();
      expect(view.refetchWorkflowSurfaces).not.toHaveBeenCalled();
      expect(view.setSavingWorkflowSurface).not.toHaveBeenCalled();
    });

    it.each([["", undefined], [" \t ", undefined], ["2", 2], [" 002 ", 2]] as const)("stages the exact optional limit from %j and preserves existing lane data", async (value, limit) => {
      const view = setup(value, kind, mode);
      await act(async () => { await view.result.current.actions.handleSaveGroup(); });
      const operation = view.result.current.changeSet.operations.at(-1)!;
      const container = operation.op === "create_group" ? operation.group : operation.op === "create_for_each" ? operation.for_each : "patch" in operation ? operation.patch : {};
      expect(container).toMatchObject({ display_name: "Workers", description: "Description", body: { kind: "lanes", lanes: mode === "create" ? [{ lane_id: "first" }, { lane_id: "second" }] : lanes } });
      const body = (container as { body: Record<string, unknown> }).body;
      if (limit === undefined) expect(body).not.toHaveProperty("concurrency_limit");
      else expect(body.concurrency_limit).toBe(2);
      if (kind === "for_each") expect(container).toMatchObject({ over: "state.items", item_name: "item", index_name: "index" });
      expect(view.result.current.groupDraft.concurrencyLimit).toBe(value);
      expect(view.transport).not.toHaveBeenCalled();
    });

    it("ignores a hidden invalid lane limit when switching to a sequence", async () => {
      const view = setup("2x", kind, mode, { bodyKind: "sequence" });
      await act(async () => { await view.result.current.actions.handleSaveGroup(); });
      const operation = view.result.current.changeSet.operations.at(-1)!;
      const container = operation.op === "create_group" ? operation.group : operation.op === "create_for_each" ? operation.for_each : "patch" in operation ? operation.patch : {};
      expect(container).toMatchObject({ body: { kind: "sequence" } });
      expect((container as { body: unknown }).body).toEqual({ kind: "sequence" });
      expect(view.result.current.error).toBe("");
      expect(view.transport).not.toHaveBeenCalled();
    });
  });

  it.each([["", undefined], ["2", 2]] as const)("preserves direct config CAS and refetch when limit %j is unchanged", async (value, limit) => {
    const view = setup(value, kind, "saved", {}, limit ?? null);
    await act(async () => { await view.result.current.actions.handleSaveGroup(); });
    expect(view.result.current.changeSet.operations).toEqual([]);
    expect(view.transport).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(view.transport.mock.calls[0][1]?.body))).toEqual({ expected_revision: 7, operations: [{ op: kind === "group" ? "update_group_config" : "update_for_each_config", [kind === "group" ? "group_id" : "for_each_id"]: "workers", patch: { display_name: "Workers", description: "Description", ...(kind === "for_each" ? { over: "state.items", item_name: "item", index_name: "index" } : {}) } }] });
    expect(view.refetchWorkflowSurfaces).toHaveBeenCalledTimes(1);
  });

  it("keeps a revision conflict visible without refetching or staging", async () => {
    const view = setup("2", kind, "saved", {}, 2);
    view.transport.mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: "workflow_revision_conflict" } }), { status: 409, headers: { "content-type": "application/json" } }));
    await act(async () => { await view.result.current.actions.handleSaveGroup(); });
    expect(view.result.current.changeSet.status).toBe("conflict");
    expect(view.result.current.error).toContain("Nothing was saved");
    expect(view.result.current.changeSet.operations).toEqual([]);
    expect(view.refetchWorkflowSurfaces).not.toHaveBeenCalled();
  });

  it("clears its old validation error on a corrected valid submit", async () => {
    const view = setup("0", kind);
    await act(async () => { await view.result.current.actions.handleSaveGroup(); });
    expect(view.result.current.error).not.toBe("");
    act(() => view.result.current.setGroupDraft((current) => ({ ...current, concurrencyLimit: "2" })));
    await act(async () => { await view.result.current.actions.handleSaveGroup(); });
    expect(view.result.current.error).toBe("");
    expect(view.result.current.notice).not.toBe("");
    expect(view.result.current.noticeIsError).toBe(false);
    expect(view.result.current.groupFormSaveFailed).toBe(false);
    expect(view.result.current.changeSet.operations).toHaveLength(1);
  });

  it("restores the real shared notice flags after a corrected direct save", async () => {
    const view = setup("0", kind, "saved", {}, 2);
    await act(async () => { await view.result.current.actions.handleSaveGroup(); });
    expect(view.result.current.noticeIsError).toBe(true);
    expect(view.result.current.groupFormSaveFailed).toBe(true);
    act(() => view.result.current.setGroupDraft((current) => ({ ...current, concurrencyLimit: "2" })));
    await act(async () => { await view.result.current.actions.handleSaveGroup(); });
    expect(view.result.current.notice).not.toBe("");
    expect(view.result.current.noticeIsError).toBe(false);
    expect(view.result.current.groupFormSaveFailed).toBe(false);
    expect(view.transport).toHaveBeenCalledTimes(1);
    expect(view.refetchWorkflowSurfaces).toHaveBeenCalledTimes(1);
    expect(view.result.current.changeSet.operations).toEqual([]);
  });
});

it.each([
  ["for_each", { over: "", lanes: "" }, "Enter the list the For each goes through."],
  ["for_each", { lanes: "" }, "Add at least one lane."],
  ["group", { lanes: "" }, "Add at least one lane."],
] as const)("keeps earlier validation ordering for %s", async (kind, overrides, message) => {
  const view = setup("0", kind, "create", overrides);
  await act(async () => { await view.result.current.actions.handleSaveGroup(); });
  expect(view.result.current.error).toBe(message);
  expect(view.result.current.changeSet.operations).toEqual([]);
  expect(view.transport).not.toHaveBeenCalled();
});

  it("keeps the pending-structure guard on a direct settings save", async () => {
    const view = setup("2", "group", "saved", {}, 2);
    act(() => view.result.current.changeSet.stage({ op: "update_group_config", group_id: "workers", patch: { description: "Pending" } }));
    const before = view.result.current.changeSet.operations;
    await act(async () => { await view.result.current.actions.handleSaveGroup(); });
    expect(view.result.current.error).toContain("unapplied structure changes");
    expect(view.result.current.changeSet.operations).toBe(before);
    expect(view.transport).not.toHaveBeenCalled();
  });
