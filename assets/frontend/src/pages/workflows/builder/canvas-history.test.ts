import type { WorkflowChangeOperation } from "./api";
import {
  HISTORY_LIMIT,
  appendGraphIntent,
  attachCreationPlacement,
  appendSavedAction,
  applyGraphJournal,
  canRedoGraphIntent,
  canUndoGraphIntent,
  discardGraphJournal,
  emptyGraphJournal,
  flattenGraphJournal,
  redoGraphIntent,
  placementUndoneBy,
  placementRedoneBy,
  redoSequence,
  storableGraphJournal,
  undoGraphIntent,
  undoSequence,
  type GraphJournal,
} from "./canvas-history";
import { definitionRestoreOperations, stableDefinitionJson } from "./canvas-history-restore";
import { applyChangeOperations, definitionFromWorkflow, reconcileChangeOperations, type WorkflowDefinitionModel } from "./change-set";

const ref = (id: string) => ({ kind: "step" as const, id });
const base = definitionFromWorkflow({
  workflow_id: "history",
  revision: 3,
  steps: [
    { step_id: "start", step_kind: "agent", display_name: "Start" },
    { step_id: "compose", step_kind: "template", display_name: "Compose", override: { template: { text: "hello" } } },
    { step_id: "inner", step_kind: "set_variable", display_name: "Inner", group_id: "loop" },
    { step_id: "done", step_kind: "stop_success", display_name: "Done" },
  ],
  groups: [{ group_id: "loop", display_name: "Loop", body: { kind: "sequence", entry: ref("inner") } }],
  settings: {
    execution_graph: {
      schema_version: 1,
      entry: ref("start"),
      edges: [
        { source: ref("start"), outcome: { name: "success" }, target: ref("compose") },
        { source: ref("compose"), outcome: { name: "success" }, target: { kind: "group", id: "loop" } },
        { source: { kind: "group", id: "loop" }, outcome: { name: "success" }, target: ref("done") },
        { source: ref("inner"), outcome: { name: "success" }, target: { kind: "group_exit", group_id: "loop", lane_id: null } },
      ],
    },
  },
});

/** Order-free shape of a definition: two equal definitions compare equal. */
function shape(definition: WorkflowDefinitionModel) {
  const sorted = <T>(rows: T[]) => rows.map((row) => stableDefinitionJson(row)).sort();
  // A field the reverse clears comes back as null, which a step treats as unset.
  const step = (row: Record<string, unknown>) => Object.fromEntries(Object.entries(row).filter(([, value]) => value !== null && value !== undefined));
  return {
    steps: sorted(definition.steps.map((row) => step(row as Record<string, unknown>))),
    groups: sorted(definition.groups),
    forEach: sorted(definition.forEach),
    edges: sorted(definition.graph.edges.map((edge) => ({ ...edge, rework: Boolean(edge.rework) }))),
    entry: stableDefinitionJson(definition.graph.entry),
  };
}

const after = (definition: WorkflowDefinitionModel, operations: WorkflowChangeOperation[]) =>
  applyChangeOperations(definition, reconcileChangeOperations(definition, operations)).definition;

describe("definitionRestoreOperations (Undo of an applied action)", () => {
  it.each<[string, WorkflowChangeOperation[]]>([
    ["add a step and connect it", [
      { op: "create_step", step: { step_id: "extra", step_kind: "agent", display_name: "Agent" } },
      { op: "connect", edge: { source: ref("done"), outcome: { name: "error" }, target: ref("extra") } },
    ]],
    ["delete a connected entry step", [{ op: "delete_step", step_id: "start" }]],
    ["move a step into a group", [{ op: "move_step", step_id: "compose", group_id: "loop", lane_id: null }]],
    ["replace a connection", [{ op: "connect", edge: { source: ref("start"), outcome: { name: "success" }, target: ref("done") }, replace: true }]],
    ["remove a connection", [{ op: "disconnect", source: ref("compose"), outcome: { name: "success" } }]],
    ["change a step's kind", [{ op: "change_step_kind", step_id: "compose", step: { step_kind: "stop_success" } }]],
    ["edit a step's parameters", [{ op: "update_step_config", step_id: "compose", patch: { display_name: "Renamed", description: "new" } }]],
    ["delete a group with its steps", [{ op: "delete_group", group_id: "loop", with_contents: true }]],
    ["rename a group", [{ op: "update_group_config", group_id: "loop", patch: { display_name: "Renamed" } }]],
    ["set another entry", [{ op: "set_entry", target: ref("compose") }]],
  ])("restores the definition before: %s", (_name, operations) => {
    const changed = after(base, operations);
    expect(shape(changed)).not.toEqual(shape(base));
    const inverse = definitionRestoreOperations(changed, base);
    expect(inverse.length).toBeGreaterThan(0);
    const restored = applyChangeOperations(changed, inverse);
    expect(restored.rejected).toEqual({});
    expect(shape(restored.definition)).toEqual(shape(base));
  });

  it("names nothing when nothing changed", () => {
    expect(definitionRestoreOperations(base, base)).toEqual([]);
  });
});

const created: WorkflowChangeOperation = { op: "create_step", step: { step_id: "agent_1", step_kind: "agent", display_name: "Agent" } };
const moved: WorkflowChangeOperation = { op: "move_step", step_id: "compose", group_id: "loop", lane_id: null };
const stage = (journal: GraphJournal, operations: WorkflowChangeOperation[]) => appendGraphIntent(journal, operations);

describe("one history that goes through Apply (Canvas › Undo)", () => {
  it("undoes an applied action by staging its reverse, and Redo walks forward the same way", () => {
    let journal = stage(emptyGraphJournal(), [created]);
    journal = applyGraphJournal(journal, base);
    const saved = after(base, [created]);
    expect(flattenGraphJournal(journal)).toEqual([]);
    expect(canUndoGraphIntent(journal)).toBe(true);

    journal = undoGraphIntent(journal);
    expect(flattenGraphJournal(journal)).toEqual([{ op: "delete_step", step_id: "agent_1" }]);
    expect(canRedoGraphIntent(journal)).toBe(true);
    // Redo before Apply takes the staged reverse back out: the copy equals the checkpoint again.
    journal = redoGraphIntent(journal);
    expect(flattenGraphJournal(journal)).toEqual([]);

    // Undo, Apply the reverse, then Redo: the forward action is staged again.
    journal = applyGraphJournal(undoGraphIntent(journal), saved);
    expect(flattenGraphJournal(journal)).toEqual([]);
    journal = redoGraphIntent(journal);
    expect(flattenGraphJournal(journal)).toEqual([created]);
    expect(canRedoGraphIntent(journal)).toBe(false);
  });

  it("walks back over unapplied and applied actions alike, newest first", () => {
    let journal = applyGraphJournal(stage(emptyGraphJournal(), [created]), base);
    const saved = after(base, [created]);
    journal = stage(journal, [moved]);
    journal = undoGraphIntent(journal);
    expect(flattenGraphJournal(journal)).toEqual([]);
    journal = undoGraphIntent(journal);
    expect(flattenGraphJournal(journal)).toEqual([{ op: "delete_step", step_id: "agent_1" }]);
    expect(canUndoGraphIntent(journal)).toBe(false);
    expect(shape(after(saved, flattenGraphJournal(journal)))).toEqual(shape(base));
  });

  it("ends the redo branch on a new action, and an empty history changes nothing", () => {
    let journal = stage(stage(emptyGraphJournal(), [created]), [moved]);
    journal = undoGraphIntent(journal);
    expect(canRedoGraphIntent(journal)).toBe(true);
    journal = stage(journal, [{ op: "set_entry", target: ref("compose") }]);
    expect(canRedoGraphIntent(journal)).toBe(false);
    expect(redoGraphIntent(journal)).toBe(journal);
    const empty = emptyGraphJournal();
    expect(undoGraphIntent(empty)).toBe(empty);
    expect(redoGraphIntent(empty)).toBe(empty);
  });

  it("keeps an own one-step save in the history without adding it to the unapplied set", () => {
    const save: WorkflowChangeOperation = { op: "update_step_config", step_id: "compose", patch: { display_name: "Saved" } };
    const inverse = definitionRestoreOperations(after(base, [save]), base);
    let journal = appendSavedAction(emptyGraphJournal(), [save], inverse);
    expect(flattenGraphJournal(journal)).toEqual([]);
    journal = undoGraphIntent(journal);
    expect(flattenGraphJournal(journal)).toEqual(inverse);
    expect(shape(after(after(base, [save]), flattenGraphJournal(journal)))).toEqual(shape(base));
    journal = redoGraphIntent(journal);
    expect(flattenGraphJournal(journal)).toEqual([]);
  });

  it("orders Undo by the latest action and Redo by the latest Undo", () => {
    let journal = stage(emptyGraphJournal(), [created]);
    const first = undoSequence(journal);
    journal = stage(journal, [moved]);
    expect(undoSequence(journal)).toBeGreaterThan(first ?? 0);
    journal = undoGraphIntent(journal);
    expect(redoSequence(journal)).toBeGreaterThan(undoSequence(journal) ?? 0);
  });

  it("keeps at most the newest entries, never shortening the unapplied set", () => {
    let journal = emptyGraphJournal();
    for (let index = 0; index < HISTORY_LIMIT + 5; index += 1) {
      journal = stage(journal, [{ op: "update_step_config", step_id: "compose", patch: { display_name: `Name ${index}` } }]);
    }
    expect(journal.items).toHaveLength(HISTORY_LIMIT);
    expect(journal.cursor).toBe(HISTORY_LIMIT);
    expect(journal.pending).toHaveLength(HISTORY_LIMIT + 5);
  });

  it("forgets on Discard only what never reached the server", () => {
    let journal = applyGraphJournal(stage(emptyGraphJournal(), [created]), base);
    journal = stage(journal, [moved]);
    journal = discardGraphJournal(journal);
    expect(journal.items.map((item) => item.operations)).toEqual([[created]]);
    expect(journal.cursor).toBe(1);
    expect(flattenGraphJournal(journal)).toEqual([]);
  });

  it("stores no session-only toast token", () => {
    const token = { controller: Symbol("controller"), serial: 1 };
    const journal = appendGraphIntent(emptyGraphJournal(), [created], { intentToken: token });
    const stored = storableGraphJournal(journal);
    expect(stored.items[0]).not.toHaveProperty("intentToken");
    expect(JSON.parse(JSON.stringify(stored))).toEqual(stored);
  });
});


describe("creation geometry shares its graph Undo item", () => {
  it("attaches only to the active created target and replays old and new XY in graph order", () => {
    const token = { controller: Symbol("owner"), serial: 1 };
    const old = { x: 1136, y: -32 };
    const placed = { x: 1136, y: 576 };
    const deletion: WorkflowChangeOperation = { op: "delete_step", step_id: "set_variable_3" };
    const replacement: WorkflowChangeOperation = { op: "create_step", step: { step_id: "set_variable_3", step_kind: "set_variable" } };
    let journal = appendGraphIntent(appendGraphIntent(emptyGraphJournal(), [deletion]), [replacement], { intentToken: token });
    const delta = { canvasWorkflowId: "author", subjectIds: ["set_variable_3"], canonicalStepSubjectIds: ["set_variable_3"],
      before: { set_variable_3: old }, after: { set_variable_3: placed } };
    expect(attachCreationPlacement(journal, { controller: Symbol("other"), serial: 1 }, { kind: "step", id: "set_variable_3" }, "set_variable_3", delta)).toBe(journal);
    expect(attachCreationPlacement(journal, token, { kind: "step", id: "other" }, "set_variable_3", delta)).toBe(journal);
    const wrongSubject = { ...delta, subjectIds: ["set_variable"] };
    const wrongCanonical = { ...delta, canonicalStepSubjectIds: ["set_variable"] };
    const extraBefore = { ...delta, before: { ...delta.before, set_variable: old } };
    const extraAfter = { ...delta, after: { ...delta.after, set_variable: placed } };
    for (const invalid of [wrongSubject, wrongCanonical, extraBefore, extraAfter])
      expect(attachCreationPlacement(journal, token, { kind: "step", id: "set_variable_3" }, "set_variable_3", invalid)).toBe(journal);
    expect(attachCreationPlacement(journal, token, { kind: "step", id: "set_variable_3" }, "hook:before:set_variable_3", delta)).toBe(journal);
    journal = attachCreationPlacement(journal, token, { kind: "step", id: "set_variable_3" }, "set_variable_3", delta);
    expect(journal.items).toHaveLength(2); // A move must not become a second Undo action.
    expect(attachCreationPlacement(journal, token, { kind: "step", id: "set_variable_3" }, "set_variable_3", delta)).toBe(journal);
    expect(placementUndoneBy(journal)?.before.set_variable_3).toEqual(old);
    journal = undoGraphIntent(journal); // New card disappears; retired XY becomes visible to the old card on Undo deletion.
    expect(flattenGraphJournal(journal)).toEqual([deletion]);
    journal = undoGraphIntent(journal);
    expect(flattenGraphJournal(journal)).toEqual([]);
    journal = redoGraphIntent(journal);
    expect(flattenGraphJournal(journal)).toEqual([deletion]);
    expect(placementRedoneBy(journal)?.after.set_variable_3).toEqual(placed);
    journal = redoGraphIntent(journal);
    expect(flattenGraphJournal(journal)).toEqual([deletion, replacement]);
    const undoneCreation = undoGraphIntent(journal);
    expect(attachCreationPlacement(undoneCreation, token, { kind: "step", id: "set_variable_3" }, "set_variable_3", delta)).toBe(undoneCreation);
  });

  it("keeps a later manual move later in history", () => {
    const token = { controller: Symbol("owner"), serial: 2 };
    const target = { kind: "step" as const, id: "new" };
    const create: WorkflowChangeOperation = { op: "create_step", step: { step_id: "new", step_kind: "set_variable" } };
    const first = { canvasWorkflowId: "author", subjectIds: ["new"], canonicalStepSubjectIds: ["new"],
      before: { new: null }, after: { new: { x: 100, y: 100 } } };
    const dragged = { ...first, before: first.after, after: { new: { x: 300, y: 300 } } };
    let journal = attachCreationPlacement(appendGraphIntent(emptyGraphJournal(), [create], { intentToken: token }), token, target, "new", first);
    journal = appendGraphIntent(journal, [], { placement: dragged });
    // An unplaced creation may not attach after a newer placement, even if it is undone back to P.
    const delayed = appendGraphIntent(emptyGraphJournal(), [create], { intentToken: token });
    const moved = appendGraphIntent(delayed, [], { placement: dragged });
    expect(attachCreationPlacement(moved, token, target, "new", first)).toBe(moved);
    const returned = undoGraphIntent(moved);
    expect(attachCreationPlacement(returned, token, target, "new", first)).toBe(returned);
    expect(placementUndoneBy(journal)?.before.new).toEqual(first.after.new);
    journal = undoGraphIntent(journal);
    expect(placementUndoneBy(journal)?.before.new).toBeNull();
    expect(placementRedoneBy(journal)?.after.new).toEqual(dragged.after.new);
    journal = redoGraphIntent(journal);
    expect(placementUndoneBy(journal)?.before.new).toEqual(first.after.new);
  });
});
