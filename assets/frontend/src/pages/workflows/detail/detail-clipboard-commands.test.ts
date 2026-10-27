import { createWorkflowClipboardCommands, type ClipboardAuthority, type ClipboardCommit } from "./detail-clipboard-commands";
import { encodeBuilderSelection } from "@/pages/workflows/builder/canvas-clipboard";
import { deriveDeletionBatch } from "@/pages/workflows/builder/canvas-delete-batch";
import { applyChangeOperations, type WorkflowDefinitionModel } from "@/pages/workflows/builder/change-set";

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((accept) => { resolve = accept; });
  return { promise, resolve };
}
function ordinary(): WorkflowDefinitionModel {
  return { steps: [{ step_id: "plan", step_kind: "set_variable", display_name: "Plan",
    override: { set_variable: { assignments: { "state.name": "kept" } } } }], groups: [], forEach: [],
    graph: { schema_version: 1, entry: { kind: "step", id: "plan" }, edges: [] } };
}
function ordinaryPair(): WorkflowDefinitionModel {
  const definition = ordinary();
  definition.steps.push({ step_id: "other", step_kind: "set_variable", display_name: "Other" });
  return definition;
}
function containers(): WorkflowDefinitionModel {
  return { groups: [{ group_id: "outer", display_name: "Outer", body: { kind: "sequence", entry: { kind: "for_each", id: "loop" } } }],
    forEach: [{ for_each_id: "loop", group_id: "outer", display_name: "Loop", over: "state.items", item_name: "item",
      body: { kind: "sequence", entry: { kind: "step", id: "work" } } }],
    steps: [{ step_id: "work", step_kind: "set_variable", for_each_id: "loop" }, { step_id: "finish", step_kind: "set_variable", group_id: "outer" }],
    graph: { schema_version: 1, entry: { kind: "group", id: "outer" }, edges: [
      { source: { kind: "step", id: "work" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "loop", lane_id: null } },
      { source: { kind: "for_each", id: "loop" }, outcome: { name: "success" }, target: { kind: "step", id: "finish" } },
      { source: { kind: "step", id: "finish" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "outer", lane_id: null } },
    ] },
  };
}
type Dependencies = Parameters<typeof createWorkflowClipboardCommands>[0];
function harness(definition = ordinary(), target = { kind: "step" as "step" | "group" | "for_each", id: "plan" }, withBatch = false) {
  const events: string[] = [];
  const stageOwner = Symbol("test graph controller");
  let stageSerial = 0;
  const stage = vi.fn<ClipboardAuthority["changeSet"]["stageGraphIntent"]>(() => {
    events.push("stage");
    return { controller: stageOwner, serial: ++stageSerial };
  });
  // An explicit unit boundary: this stub models only fields the orchestrator reads.
  // It is not a real mounted controller, backend guard or durable history proof.
  const changeSet = { workflowId: "owner", operations: [], preview: definition, baseRevision: null,
    status: "clean", stageGraphIntent: stage } as unknown as ClipboardAuthority["changeSet"];
  let current: ClipboardAuthority | null = {
    changeSet, owner: { workflowId: "owner", projectId: "project_a", ownerKey: "owner/a/view_a/1", selectionId: target.id, serverRevision: 5, structureLocked: false },
    canvasWorkflowId: "author_canvas", viewKey: "view_a", generation: 1, authoringAllowed: true,
    targets: [{ ref: target, canvasId: target.kind === "step" ? target.id : target.kind + ":" + target.id }],
    worldPositions: new Map([
      ["plan", { x: 64, y: 64 }], ["other", { x: 400, y: 64 }], ["outer", { x: 64, y: 64 }], ["loop", { x: 128, y: 128 }],
      ["work", { x: 192, y: 192 }], ["finish", { x: 512, y: 128 }],
    ]),
    placement: { groupId: "", forEachId: "", laneId: "" }, pasteOrigin: { x: 64, y: 64 }, pasteMode: "pointer",
    duplicateOrigin: { x: 64, y: 400 },
  };
  const writeText = vi.fn<(text: string) => Promise<void>>(async () => { events.push("write"); });
  const readText = vi.fn<() => Promise<string>>(async () => "");
  const requestDelete = vi.fn<Dependencies["guard"]["requestDelete"]>(async (_target, onAccepted) => {
    events.push("guard");
    onAccepted();
    return true;
  });
  const requestDeleteBatch = vi.fn<NonNullable<Dependencies["guard"]["requestDeleteBatch"]>>(async (targets, onAccepted, context) => {
    events.push("guard_batch");
    const batch = deriveDeletionBatch(context.captured.changeSet.preview as WorkflowDefinitionModel, targets);
    if (!batch) return false;
    onAccepted(batch);
    return true;
  });
  const onCommitted = vi.fn<(commit: ClipboardCommit) => undefined>(() => { events.push("visual"); return undefined; });
  const commands = createWorkflowClipboardCommands({
    readCurrent: () => current, clipboard: { writeText, readText },
    guard: withBatch ? { requestDelete, requestDeleteBatch } : { requestDelete },
    openProtectedScope: async () => true, onCommitted,
    copyLabel: (label, ordinal) => label + " copy" + (ordinal > 1 ? " " + ordinal : ""),
  });
  return { commands, stage, writeText, readText, requestDelete, requestDeleteBatch, onCommitted, events,
    get snapshot() { if (!current) throw new Error("No fixture authority"); return current; },
    setCurrent: (value: ClipboardAuthority | null) => { current = value; } };
}
function selectMany(h: ReturnType<typeof harness>, refs: ClipboardAuthority["targets"]) {
  const targets = [...refs].sort((a, b) => a.canvasId.localeCompare(b.canvasId));
  h.setCurrent({ ...h.snapshot, targets,
    owner: { ...h.snapshot.owner, selectionId: JSON.stringify(targets.map((target) => target.canvasId)) } });
}
function encoded(h: ReturnType<typeof harness>) {
  const result = encodeBuilderSelection({ definition: h.snapshot.changeSet.preview as WorkflowDefinitionModel,
    targets: h.snapshot.targets.map((target) => target.ref), worldPositions: h.snapshot.worldPositions });
  if (!result.ok) throw new Error(result.reason);
  return result.value;
}

it("does not touch the clipboard, graph or visual state when the guard authorization boundary refuses", async () => {
  const h = harness();
  h.requestDelete.mockImplementationOnce(async () => false);
  expect(await h.commands.cut()).toMatchObject({ ok: false, reason: "guard_refused", staged: false, clipboardWritten: false });
  expect(h.requestDelete).toHaveBeenCalledTimes(1);
  expect(h.requestDelete.mock.calls[0][0]).toEqual({ kind: "step", id: "plan" });
  expect(h.requestDelete.mock.calls[0][2]?.captured).toMatchObject({ owner: h.snapshot.owner, changeSet: h.snapshot.changeSet });
  expect(h.requestDelete.mock.calls[0][2]?.captured.changeSet.operations).toBe(h.snapshot.changeSet.operations);
  expect(h.requestDelete.mock.calls[0][2]?.current()).toBe(h.snapshot);
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.readText).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
  expect(h.onCommitted).not.toHaveBeenCalled();
});

it("does not stage or change selection/editor state after writeText rejects", async () => {
  const h = harness();
  h.writeText.mockRejectedValueOnce(new DOMException("Permission denied", "NotAllowedError"));
  expect(await h.commands.cut()).toMatchObject({ ok: false, reason: "clipboard_failed", staged: false, clipboardWritten: false });
  expect(h.requestDelete).toHaveBeenCalledTimes(1);
  expect(h.writeText).toHaveBeenCalledTimes(1);
  expect(h.stage).not.toHaveBeenCalled();
  expect(h.onCommitted).not.toHaveBeenCalled();
});

it("waits for authorization and the successful OS write before staging exactly one Cut intent", async () => {
  const h = harness();
  const proof = deferred<void>();
  const write = deferred<void>();
  h.requestDelete.mockImplementationOnce(async (_target, onAccepted) => { await proof.promise; h.events.push("guard"); onAccepted(); return true; });
  h.writeText.mockImplementationOnce(async () => { h.events.push("write"); await write.promise; });
  const pending = h.commands.cut();
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
  proof.resolve();
  await Promise.resolve();
  await Promise.resolve();
  expect(h.writeText).toHaveBeenCalledTimes(1);
  expect(h.stage).not.toHaveBeenCalled();
  expect(h.onCommitted).not.toHaveBeenCalled();
  write.resolve();
  expect(await pending).toMatchObject({ ok: true, staged: true, clipboardWritten: true });
  expect(h.events).toEqual(["guard", "write", "stage", "visual"]);
  expect(h.stage).toHaveBeenCalledExactlyOnceWith([{ op: "delete_step", step_id: "plan" }]);
  expect(h.onCommitted.mock.calls[0][0].removed?.steps).toEqual(["plan"]);
});

const stale = [
  ["project", (a: ClipboardAuthority) => ({ ...a, owner: { ...a.owner, projectId: "project_b" } })],
  ["revision", (a: ClipboardAuthority) => ({ ...a, owner: { ...a.owner, serverRevision: 6 } })],
  ["operations identity", (a: ClipboardAuthority) => ({ ...a, changeSet: { ...a.changeSet, operations: [] } })],
  ["preview identity", (a: ClipboardAuthority) => ({ ...a, changeSet: { ...a.changeSet, preview: ordinary() } })],
  ["view ABA generation", (a: ClipboardAuthority) => ({ ...a, generation: a.generation + 2 })],
] as const;
it.each(stale)("refuses a stale Cut completion after the clipboard succeeds (%s)", async (_label, advance) => {
  const h = harness();
  const write = deferred<void>();
  h.writeText.mockImplementationOnce(() => write.promise);
  const pending = h.commands.cut();
  await Promise.resolve();
  expect(h.writeText).toHaveBeenCalledTimes(1);
  h.setCurrent(advance(h.snapshot));
  write.resolve();
  expect(await pending).toMatchObject({ ok: false, reason: "stale", clipboardWritten: true, staged: false,
    authority: { workflowId: "owner", projectId: "project_a", generation: 1 } });
  expect(h.stage).not.toHaveBeenCalled();
  expect(h.onCommitted).not.toHaveBeenCalled();
});

it("retries the complete Cut rather than using the guard's old flag-only callback as continuation", async () => {
  const h = harness();
  let lateAcceptance: (() => void) | undefined;
  h.requestDelete.mockImplementationOnce(async (_target, onAccepted) => { lateAcceptance = onAccepted; return false; });
  expect(await h.commands.cut()).toMatchObject({ reason: "guard_refused", staged: false });
  lateAcceptance?.(); // Equivalent to an unwired guard.retry callback: must not write or stage.
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
  expect(await h.commands.retryCut()).toMatchObject({ ok: true, staged: true, clipboardWritten: true });
  expect(h.requestDelete).toHaveBeenCalledTimes(2);
  expect(h.writeText).toHaveBeenCalledTimes(1);
  expect(h.stage).toHaveBeenCalledTimes(1);
});

it("rejects a retry after its original selection generation changed", async () => {
  const h = harness();
  h.requestDelete.mockImplementationOnce(async () => false);
  await h.commands.cut();
  h.setCurrent({ ...h.snapshot, generation: 2 });
  expect(await h.commands.retryCut()).toMatchObject({ ok: false, reason: "stale" });
  expect(h.requestDelete).toHaveBeenCalledTimes(1);
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
});

it("passes a full Group→ForEach→Step closure through one existing guard target and one delete batch", async () => {
  const h = harness(containers(), { kind: "group", id: "outer" });
  expect(await h.commands.cut()).toMatchObject({ ok: true, staged: true });
  expect(h.requestDelete.mock.calls[0][0]).toEqual({ kind: "group", id: "outer" });
  expect(h.stage).toHaveBeenCalledExactlyOnceWith([{ op: "delete_group", group_id: "outer", with_contents: true }]);
  expect(h.onCommitted.mock.calls[0][0].removed).toMatchObject({ groups: ["outer"], forEach: ["loop"], steps: ["work", "finish"] });
});

it("keeps multi-root Cut unavailable for a caller that supplies only the frozen single guard", async () => {
  const h = harness(ordinaryPair());
  selectMany(h, [...h.snapshot.targets, { ref: { kind: "step", id: "other" }, canvasId: "other" }]);
  expect(await h.commands.cut()).toMatchObject({ ok: false, reason: "bulk_cut_unavailable", staged: false });
  expect(h.requestDelete).not.toHaveBeenCalled();
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
});

it("cuts two independent roots after one combined guard and one OS write as one Undo intent", async () => {
  const h = harness(ordinaryPair(), undefined, true);
  selectMany(h, [...h.snapshot.targets, { ref: { kind: "step", id: "other" }, canvasId: "other" }]);
  expect(await h.commands.cut()).toMatchObject({ ok: true, staged: true, clipboardWritten: true });
  expect(h.events).toEqual(["guard_batch", "write", "stage", "visual"]);
  expect(h.requestDelete).not.toHaveBeenCalled();
  expect(h.requestDeleteBatch).toHaveBeenCalledTimes(1);
  expect(h.requestDeleteBatch.mock.calls[0][0]).toEqual([{ kind: "step", id: "other" }, { kind: "step", id: "plan" }]);
  expect(h.requestDeleteBatch.mock.calls[0][2].selection.canvasIds).toEqual(["other", "plan"]);
  expect(h.stage).toHaveBeenCalledExactlyOnceWith([
    { op: "delete_step", step_id: "other" }, { op: "delete_step", step_id: "plan" },
  ]);
  expect(h.onCommitted.mock.calls[0][0].removed?.steps).toEqual(["other", "plan"]);
});

it("deduplicates a selected Group and child in one combined Cut closure", async () => {
  const h = harness(containers(), { kind: "group", id: "outer" }, true);
  selectMany(h, [...h.snapshot.targets, { ref: { kind: "step", id: "work" }, canvasId: "work" }]);
  expect(await h.commands.cut()).toMatchObject({ ok: true, staged: true });
  expect(h.requestDeleteBatch).toHaveBeenCalledTimes(1);
  expect(h.stage).toHaveBeenCalledExactlyOnceWith([{ op: "delete_group", group_id: "outer", with_contents: true }]);
  expect(h.onCommitted.mock.calls[0][0].removed).toMatchObject({ groups: ["outer"], forEach: ["loop"], steps: ["finish", "work"] });
});

it("does not write or stage after a combined guard refusal, then retries the entire batch", async () => {
  const h = harness(ordinaryPair(), undefined, true);
  selectMany(h, [...h.snapshot.targets, { ref: { kind: "step", id: "other" }, canvasId: "other" }]);
  let lateAcceptance: ((batch: NonNullable<ReturnType<typeof deriveDeletionBatch>>) => void) | undefined;
  h.requestDeleteBatch.mockImplementationOnce(async (_targets, onAccepted) => { lateAcceptance = onAccepted; return false; });
  expect(await h.commands.cut()).toMatchObject({ reason: "guard_refused", staged: false, clipboardWritten: false });
  const batch = deriveDeletionBatch(h.snapshot.changeSet.preview as WorkflowDefinitionModel, h.snapshot.targets.map((target) => target.ref));
  if (!batch) throw new Error("Invalid fixture batch");
  lateAcceptance?.(batch);
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
  expect(await h.commands.retryCut()).toMatchObject({ ok: true, staged: true, clipboardWritten: true });
  expect(h.requestDeleteBatch).toHaveBeenCalledTimes(2);
  expect(h.writeText).toHaveBeenCalledTimes(1);
  expect(h.stage).toHaveBeenCalledTimes(1);
});

it("leaves a guarded multi-root draft untouched when the OS write rejects", async () => {
  const h = harness(ordinaryPair(), undefined, true);
  selectMany(h, [...h.snapshot.targets, { ref: { kind: "step", id: "other" }, canvasId: "other" }]);
  h.writeText.mockRejectedValueOnce(new DOMException("Permission denied", "NotAllowedError"));
  expect(await h.commands.cut()).toMatchObject({ reason: "clipboard_failed", staged: false, clipboardWritten: false });
  expect(h.requestDeleteBatch).toHaveBeenCalledTimes(1);
  expect(h.writeText).toHaveBeenCalledTimes(1);
  expect(h.stage).not.toHaveBeenCalled();
});

it("refuses a multi-root Cut after the selection generation changes during the OS write", async () => {
  const h = harness(ordinaryPair(), undefined, true);
  selectMany(h, [...h.snapshot.targets, { ref: { kind: "step", id: "other" }, canvasId: "other" }]);
  const write = deferred<void>();
  h.writeText.mockImplementationOnce(() => write.promise);
  const pending = h.commands.cut();
  await Promise.resolve();
  expect(h.writeText).toHaveBeenCalledTimes(1);
  h.setCurrent({ ...h.snapshot, generation: 2 });
  write.resolve();
  expect(await pending).toMatchObject({ reason: "stale", clipboardWritten: true, staged: false });
  expect(h.stage).not.toHaveBeenCalled();
});

it("cuts ordinary members in one active Hook-child owner without a Development gate", async () => {
  const h = harness(ordinaryPair(), undefined, true);
  const targets = [
    { ref: { kind: "step" as const, id: "plan" }, canvasId: "hook:client_hook:plan" },
    { ref: { kind: "step" as const, id: "other" }, canvasId: "hook:client_hook:other" },
  ];
  selectMany(h, targets);
  h.setCurrent({ ...h.snapshot, viewKey: "client_hook",
    owner: { ...h.snapshot.owner, workflowId: "child", ownerKey: "author/client_hook/child/1" },
    changeSet: { ...h.snapshot.changeSet, workflowId: "child" } });
  expect(await h.commands.cut()).toMatchObject({ ok: true, staged: true });
  expect(h.requestDeleteBatch.mock.calls[0][2].captured.owner.workflowId).toBe("child");
  expect(h.requestDeleteBatch.mock.calls[0][2].selection.ownerScopeKey).toBe(
    JSON.stringify(["author_canvas", "project_a", "child", "client_hook"]));
  expect(h.stage).toHaveBeenCalledTimes(1);
});

it("refuses mixed author and Hook members or a visual Hook frame before guard or OS write", async () => {
  const h = harness(ordinaryPair(), undefined, true);
  selectMany(h, [
    { ref: { kind: "step", id: "plan" }, canvasId: "hook:client_hook:plan" },
    { ref: { kind: "step", id: "other" }, canvasId: "other" },
  ]);
  expect(await h.commands.cut()).toMatchObject({ reason: "unavailable", staged: false });
  h.setCurrent({ ...h.snapshot, targets: [{ ref: { kind: "step", id: "plan" }, canvasId: "hook:client_hook" }] });
  expect(await h.commands.cut()).toMatchObject({ reason: "unavailable", staged: false });
  expect(h.requestDeleteBatch).not.toHaveBeenCalled();
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
});

it("keeps a malformed or nonexistent paste destination unchanged", async () => {
  const h = harness();
  h.readText.mockResolvedValueOnce(encoded(h));
  h.setCurrent({ ...h.snapshot, placement: { groupId: "missing", forEachId: "", laneId: "" } });
  expect(await h.commands.paste()).toMatchObject({ ok: false, reason: "invalid_target", staged: false });
  expect(h.stage).not.toHaveBeenCalled();
  expect(h.onCommitted).not.toHaveBeenCalled();
  expect(h.writeText).not.toHaveBeenCalled();
});

it("pastes one detached batch without overwriting entry, and advances only successful no-pointer offsets", async () => {
  const h = harness();
  h.readText.mockResolvedValue(encoded(h));
  h.setCurrent({ ...h.snapshot, pasteMode: "offset" });
  const originalEntry = (h.snapshot.changeSet.preview as WorkflowDefinitionModel).graph.entry;
  expect(await h.commands.paste()).toMatchObject({ ok: true, staged: true, clipboardWritten: false });
  expect(h.stage.mock.calls[0][0]).toEqual([expect.objectContaining({ op: "create_step", step: expect.objectContaining({ step_id: "plan_2" }) })]);
  expect(h.onCommitted.mock.calls[0][0].worldPositions.get("plan_2")).toEqual({ x: 80, y: 80 });
  const committed = applyChangeOperations(h.snapshot.changeSet.preview as WorkflowDefinitionModel, h.stage.mock.calls[0][0]).definition;
  h.setCurrent({ ...h.snapshot, changeSet: { ...h.snapshot.changeSet, operations: h.stage.mock.calls[0][0], preview: committed, baseRevision: 5, status: "pending" } });
  expect(await h.commands.paste()).toMatchObject({ ok: true, staged: true });
  expect(h.onCommitted.mock.calls[1][0].worldPositions.get("plan_3")).toEqual({ x: 96, y: 96 });
  expect((h.snapshot.changeSet.preview as WorkflowDefinitionModel).graph.entry).toEqual(originalEntry);
  expect(h.stage.mock.calls.flatMap(([operations]) => operations).some((operation) => operation.op === "set_entry")).toBe(false);
  expect(h.requestDelete).not.toHaveBeenCalled();
  expect(h.writeText).not.toHaveBeenCalled();
});

it("duplicates with unique root copy labels and below-source geometry, without any OS clipboard call", async () => {
  const h = harness();
  const preview = ordinary();
  preview.steps.push({ step_id: "old_copy", step_kind: "set_variable", display_name: "Plan copy" });
  h.setCurrent({ ...h.snapshot, changeSet: { ...h.snapshot.changeSet, preview } });
  const before = JSON.stringify(preview);
  expect(await h.commands.duplicate()).toMatchObject({ ok: true, staged: true, clipboardWritten: false });
  expect(h.stage).toHaveBeenCalledExactlyOnceWith([expect.objectContaining({ op: "create_step",
    step: expect.objectContaining({ step_id: "plan_2", display_name: "Plan copy 2", override: preview.steps[0].override }) })]);
  expect(h.onCommitted.mock.calls[0][0].worldPositions.get("plan_2")).toEqual({ x: 64, y: 400 });
  expect(h.readText).not.toHaveBeenCalled();
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.requestDelete).not.toHaveBeenCalled();
  expect(JSON.stringify(preview)).toBe(before);
});

it.each(["group", "for_each"] as const)("duplicates a canonical %s and its members in exactly one existing-operation batch", async (kind) => {
  const h = harness(containers(), { kind, id: kind === "group" ? "outer" : "loop" });
  expect(await h.commands.duplicate()).toMatchObject({ ok: true, staged: true });
  const operations = h.stage.mock.calls[0][0];
  expect(h.stage).toHaveBeenCalledTimes(1);
  expect(operations).toHaveLength(kind === "group" ? 7 : 3);
  expect(operations).toContainEqual(kind === "group"
    ? { op: "create_group", group: expect.objectContaining({ group_id: "outer_2", display_name: "Outer copy" }) }
    : { op: "create_for_each", for_each: expect.objectContaining({ for_each_id: "loop_2", display_name: "Loop copy", group_id: null }) });
  expect(operations.some((operation) => operation.op === "set_entry")).toBe(false);
  expect(h.readText).not.toHaveBeenCalled();
  expect(h.writeText).not.toHaveBeenCalled();
});

it("does not let an old owner completion release a newer held command or stage in the new owner", async () => {
  const h = harness();
  const oldWrite = deferred<void>();
  const newWrite = deferred<void>();
  h.writeText.mockImplementationOnce(() => oldWrite.promise).mockImplementationOnce(() => newWrite.promise);
  const oldCut = h.commands.cut();
  await Promise.resolve();
  const newerStage = vi.fn<ClipboardAuthority["changeSet"]["stageGraphIntent"]>(() => ({ controller: Symbol("new owner"), serial: 1 }));
  h.setCurrent({ ...h.snapshot, generation: 2, owner: { ...h.snapshot.owner, workflowId: "new_owner", ownerKey: "new_owner/2" },
    changeSet: { ...h.snapshot.changeSet, workflowId: "new_owner", stageGraphIntent: newerStage } });
  const newCut = h.commands.cut();
  await Promise.resolve();
  expect(h.writeText).toHaveBeenCalledTimes(2);
  oldWrite.resolve();
  expect(await oldCut).toMatchObject({ reason: "stale", staged: false, clipboardWritten: true });
  expect(await h.commands.copy()).toMatchObject({ reason: "busy" });
  expect(h.stage).not.toHaveBeenCalled();
  expect(newerStage).not.toHaveBeenCalled();
  newWrite.resolve();
  expect(await newCut).toMatchObject({ ok: true, staged: true });
  expect(newerStage).toHaveBeenCalledExactlyOnceWith([{ op: "delete_step", step_id: "plan" }]);
  expect(h.onCommitted).toHaveBeenCalledTimes(1);
});

it.each(["authoring", "structureLocked"] as const)("refuses clipboard commands for a readonly %s context", async (mode) => {
  const h = harness();
  h.setCurrent(mode === "authoring" ? { ...h.snapshot, authoringAllowed: false } : { ...h.snapshot, owner: { ...h.snapshot.owner, structureLocked: true } });
  expect(await h.commands.copy()).toMatchObject({ reason: "unavailable", staged: false });
  expect(await h.commands.cut()).toMatchObject({ reason: "unavailable", staged: false });
  expect(h.requestDelete).not.toHaveBeenCalled();
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
});

it("reports a visual callback failure honestly after the one local batch already staged", async () => {
  const h = harness();
  h.onCommitted.mockImplementationOnce(() => { throw new Error("visual setter failed"); });
  expect(await h.commands.duplicate()).toMatchObject({ ok: false, reason: "visual_commit_failed", staged: true, clipboardWritten: false });
  expect(h.stage).toHaveBeenCalledTimes(1);
});

it("stages ordinary canonical members only in the active child controller, and refuses another Hook projection", async () => {
  const h = harness();
  h.setCurrent({ ...h.snapshot, viewKey: "client_hook", owner: { ...h.snapshot.owner, workflowId: "child", ownerKey: "author/client_hook/child/1" },
    changeSet: { ...h.snapshot.changeSet, workflowId: "child" },
    targets: [{ ref: { kind: "step", id: "plan" }, canvasId: "hook:client_hook:plan" }] });
  expect(await h.commands.cut()).toMatchObject({ ok: true, staged: true });
  expect(h.requestDelete.mock.calls[0][2]?.captured.owner.workflowId).toBe("child");
  expect(h.stage).toHaveBeenCalledExactlyOnceWith([{ op: "delete_step", step_id: "plan" }]);
  h.setCurrent({ ...h.snapshot, generation: 2, targets: [{ ref: { kind: "step", id: "plan" }, canvasId: "hook:other_hook:plan" }] });
  expect(await h.commands.copy()).toMatchObject({ reason: "unavailable" });
  expect(h.writeText).toHaveBeenCalledTimes(1);
});

it("rechecks authority after a held clipboard read and never stages into a replaced paste destination", async () => {
  const h = harness();
  const text = encoded(h);
  const read = deferred<string>();
  h.readText.mockImplementationOnce(() => read.promise);
  const pending = h.commands.paste();
  expect(h.readText).toHaveBeenCalledTimes(1);
  h.setCurrent({ ...h.snapshot, changeSet: { ...h.snapshot.changeSet, preview: ordinary() } });
  read.resolve(text);
  expect(await pending).toMatchObject({ ok: false, reason: "stale", staged: false });
  expect(h.stage).not.toHaveBeenCalled();
  expect(h.onCommitted).not.toHaveBeenCalled();
});

it("copies real codec text without a delete guard, graph batch or visual mutation", async () => {
  const h = harness();
  expect(await h.commands.copy()).toMatchObject({ ok: true, clipboardWritten: true, staged: false });
  expect(h.writeText).toHaveBeenCalledExactlyOnceWith(encoded(h));
  expect(h.readText).not.toHaveBeenCalled();
  expect(h.requestDelete).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
  expect(h.onCommitted).not.toHaveBeenCalled();
});

it("refuses a repeated command until the staged journal has actually published a new committed proof", async () => {
  const h = harness();
  expect(await h.commands.duplicate()).toMatchObject({ ok: true, staged: true });
  expect(await h.commands.duplicate()).toMatchObject({ ok: false, reason: "busy", staged: false });
  expect(h.stage).toHaveBeenCalledTimes(1);
  expect(h.onCommitted).toHaveBeenCalledTimes(1);
});


it("keeps a refused guard's live command proof and protected-scope action after Cut returns", async () => {
  const h = harness();
  h.requestDelete.mockImplementationOnce(async () => false);
  expect(await h.commands.cut()).toMatchObject({ reason: "guard_refused", staged: false });
  const context = h.requestDelete.mock.calls[0][2];
  if (!context) throw new Error("Guard command context missing");
  // The real guard retains this callback for passive notice validation on a
  // later render, and retains openProtectedScope for its public action.
  h.setCurrent({ ...h.snapshot, owner: { ...h.snapshot.owner }, changeSet: { ...h.snapshot.changeSet } });
  expect(context.current()).toBe(h.snapshot);
  expect(await context.openProtectedScope?.({ kind: "step", id: "plan", label: "Plan" })).toBe(true);
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
  expect(h.onCommitted).not.toHaveBeenCalled();
  h.setCurrent({ ...h.snapshot, generation: h.snapshot.generation + 1 });
  expect(context.current()).toBeNull();
  expect(await context.openProtectedScope?.({ kind: "step", id: "plan", label: "Plan" })).toBe(false);
});

it("releases Cut busy after refusal while a new command retires the retained old guard proof", async () => {
  const h = harness();
  h.requestDelete.mockImplementationOnce(async () => false);
  await h.commands.cut();
  const oldContext = h.requestDelete.mock.calls[0][2];
  if (!oldContext) throw new Error("First guard command context missing");
  const nextGuard = deferred<boolean>();
  h.requestDelete.mockImplementationOnce(() => nextGuard.promise);
  const pending = h.commands.cut();
  expect(h.requestDelete).toHaveBeenCalledTimes(2); // refusal did not leave busy stuck
  const nextContext = h.requestDelete.mock.calls[1][2];
  if (!nextContext) throw new Error("Second guard command context missing");
  expect(oldContext.current()).toBeNull();
  expect(await oldContext.openProtectedScope?.({ kind: "step", id: "plan", label: "Plan" })).toBe(false);
  expect(nextContext.current()).toBe(h.snapshot);
  expect(await h.commands.cut()).toMatchObject({ reason: "busy", staged: false });
  expect(nextContext.current()).toBe(h.snapshot);
  nextGuard.resolve(false);
  expect(await pending).toMatchObject({ reason: "guard_refused", staged: false });
  expect(nextContext.current()).toBe(h.snapshot);
  h.commands.invalidate();
  expect(nextContext.current()).toBeNull();
  expect(await nextContext.openProtectedScope?.({ kind: "step", id: "plan", label: "Plan" })).toBe(false);
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.stage).not.toHaveBeenCalled();
  expect(h.onCommitted).not.toHaveBeenCalled();
});


it.each([false, true])("does not reuse an unpublished graph after visual selection changes (invalidate=%s)", async (invalidate) => {
  const h = harness();
  h.readText.mockResolvedValue(encoded(h));
  h.onCommitted.mockImplementation((commit) => {
    const root = commit.roots[0];
    if (!root) throw new Error("Created root missing");
    h.setCurrent({ ...h.snapshot, generation: h.snapshot.generation + 1,
      viewKey: "view_b", owner: { ...h.snapshot.owner, ownerKey: "owner/a/view_b/2", selectionId: root.id },
      targets: [{ ref: root, canvasId: root.id }] });
    return undefined;
  });
  const before = h.snapshot.changeSet;
  expect(await h.commands.paste()).toMatchObject({ ok: true, staged: true });
  const firstBatch = h.stage.mock.calls[0][0];
  expect(firstBatch).toEqual([expect.objectContaining({ op: "create_step", step: expect.objectContaining({ step_id: "plan_2" }) })]);
  expect(h.snapshot.owner.selectionId).toBe("plan_2");
  expect(h.snapshot.generation).toBe(2);
  expect(h.snapshot.changeSet.operations).toBe(before.operations);
  expect(h.snapshot.changeSet.preview).toBe(before.preview);
  if (invalidate) h.commands.invalidate();
  expect(await h.commands.paste()).toMatchObject({ ok: false, reason: "busy", staged: false });
  expect(h.stage).toHaveBeenCalledTimes(1);
  expect(h.readText).toHaveBeenCalledTimes(1);

  // Publish a real journal/preview result, rather than fabricating an ID counter.
  const published = applyChangeOperations(before.preview as WorkflowDefinitionModel, firstBatch).definition;
  h.setCurrent({ ...h.snapshot, changeSet: { ...before, operations: firstBatch, preview: published, baseRevision: 5, status: "pending" } });
  expect(await h.commands.paste()).toMatchObject({ ok: true, staged: true });
  expect(h.stage).toHaveBeenCalledTimes(2);
  expect(h.stage.mock.calls[1][0]).toEqual([expect.objectContaining({ op: "create_step", step: expect.objectContaining({ step_id: "plan_3" }) })]);
  const finalGraph = applyChangeOperations(published, h.stage.mock.calls[1][0]).definition;
  expect(finalGraph.steps.map((step) => step.step_id)).toEqual(["plan", "plan_2", "plan_3"]);
  expect(new Set(finalGraph.steps.map((step) => step.step_id)).size).toBe(3);
  expect(h.writeText).not.toHaveBeenCalled();
});

it("allows a genuinely different graph owner after an unpublished commit and explicit invalidate", async () => {
  const h = harness();
  expect(await h.commands.duplicate()).toMatchObject({ ok: true, staged: true });
  const previous = h.snapshot;
  const nextStage = vi.fn<ClipboardAuthority["changeSet"]["stageGraphIntent"]>(() => ({ controller: Symbol("other owner"), serial: 1 }));
  h.commands.invalidate();
  h.setCurrent({ ...previous, owner: { ...previous.owner, workflowId: "other", projectId: "project_b", ownerKey: "other/b" },
    changeSet: { ...previous.changeSet, workflowId: "other", operations: [], preview: ordinary(), stageGraphIntent: nextStage } });
  expect(await h.commands.duplicate()).toMatchObject({ ok: true, staged: true });
  expect(h.stage).toHaveBeenCalledTimes(1);
  expect(nextStage).toHaveBeenCalledExactlyOnceWith([expect.objectContaining({ op: "create_step", step: expect.objectContaining({ step_id: "plan_2" }) })]);
  expect(h.readText).not.toHaveBeenCalled();
  expect(h.writeText).not.toHaveBeenCalled();
});


it("does not publish a visual commit when the owning journal refuses Duplicate admission", async () => {
  const h = harness();
  h.stage.mockReturnValueOnce(null);
  expect(await h.commands.duplicate()).toMatchObject({ ok: false, reason: "stale", staged: false, clipboardWritten: false });
  expect(h.stage).toHaveBeenCalledTimes(1);
  expect(h.onCommitted).not.toHaveBeenCalled();
  expect(h.writeText).not.toHaveBeenCalled();
  expect(h.readText).not.toHaveBeenCalled();
});
