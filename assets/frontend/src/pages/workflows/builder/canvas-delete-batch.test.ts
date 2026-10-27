import { describe, expect, it } from "vitest";
import { definitionFromWorkflow } from "./change-set";
import { deriveDeletionBatch, sameDeleteBatchSelection } from "./canvas-delete-batch";

const definition = definitionFromWorkflow({
  workflow_id: "wf", revision: 7,
  groups: [{ group_id: "checks", body: { kind: "sequence" } }],
  for_each: [{ for_each_id: "each", group_id: "checks", over: "state.items", item_name: "item", body: { kind: "sequence" } }],
  steps: [
    { step_id: "inside_each", step_kind: "set_variable", for_each_id: "each" },
    { step_id: "inside_group", step_kind: "set_variable", group_id: "checks" },
    { step_id: "outside", step_kind: "stop_success" },
  ],
});

describe("combined Delete authority", () => {
  it("uses one parent operation for selected descendants yet checks every affected rule scope", () => {
    const batch = deriveDeletionBatch(definition, [
      { kind: "step", id: "inside_each" }, { kind: "for_each", id: "each" },
      { kind: "group", id: "checks" }, { kind: "step", id: "outside" },
    ]);
    expect(batch?.operations).toEqual([
      { op: "delete_group", group_id: "checks", with_contents: true },
      { op: "delete_step", step_id: "outside" },
    ]);
    expect(batch?.closure.scopes.map((scope) => `${scope.kind}:${scope.id}`)).toEqual([
      "group:checks", "step:inside_each", "step:inside_group", "step:outside",
    ]);
  });

  it("refuses duplicate participating rows or a Step with conflicting owners without banning typed ID reuse", () => {
    const duplicate = structuredClone(definition);
    duplicate.steps.push({ step_id: "inside_each", step_kind: "set_variable", for_each_id: "each" });
    expect(deriveDeletionBatch(duplicate, [{ kind: "group", id: "checks" }])).toBeNull();
    const conflicting = structuredClone(definition);
    conflicting.steps[0].group_id = "checks";
    expect(deriveDeletionBatch(conflicting, [{ kind: "step", id: "inside_each" }])).toBeNull();
    const typedReuse = structuredClone(definition);
    typedReuse.steps.push({ step_id: "checks", step_kind: "set_variable", group_id: "checks" });
    expect(deriveDeletionBatch(typedReuse, [{ kind: "group", id: "checks" }])?.roots).toEqual([{ kind: "group", id: "checks" }]);
  });

  it("rejects an old selection token after an A→B→A view cycle", () => {
    const oldA = { canvasIds: ["hook:before:check", "hook:before:done"], generation: 4, ownerScopeKey: "wf:hook-A" };
    expect(sameDeleteBatchSelection(oldA, { ...oldA, generation: 6 })).toBe(false);
    expect(sameDeleteBatchSelection(oldA, { ...oldA, ownerScopeKey: "wf:hook-B" })).toBe(false);
    expect(sameDeleteBatchSelection(oldA, { ...oldA, canvasIds: ["hook:before:check", "hook:before:other"] })).toBe(false);
  });
});
