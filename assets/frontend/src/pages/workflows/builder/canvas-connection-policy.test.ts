import { HOOK_END_STEP_ID, hookGroupId, hookMemberId } from "./hook-frames";
import { describe, expect, it } from "vitest";
import { definitionFromWorkflow } from "./change-set";
import { connectionRefusal, scopeExitTargetLabel, scopeTargetOptions } from "./canvas-connection-policy";
import type { WorkflowOutcome } from "../shared/api";
const definition = definitionFromWorkflow({ workflow_id: "test", steps: [
  { step_id: "s", step_kind: "switch", override: { switch: { cases: [{ case_id: "red" }] } } }, { step_id: "t", step_kind: "if" },
  { step_id: "inside", step_kind: "if", for_each_id: "each" }, { step_id: "body", step_kind: "set_variable", for_each_id: "each" },
  { step_id: "lane", step_kind: "if", group_id: "parallel", lane_id: "a" }, { step_id: "other", step_kind: "set_variable", group_id: "parallel", lane_id: "b" },
  { step_id: "approved", step_kind: "approval" }, { step_id: "next", step_kind: "set_variable" }, { step_id: "stop", step_kind: "stop_success" },
], groups: [{ group_id: "parallel", body: { kind: "lanes", lanes: [{ lane_id: "a" }, { lane_id: "b" }] } }], for_each: [{ for_each_id: "each", over: "state.items", item_name: "item", body: { kind: "sequence" } }], settings: {} });
const target = { kind: "step", id: "t" } as const;
describe("one connection policy", () => {
  it("preserves the existing picker helper's trimmed labels and primitive text handling", () => {
    const model = definitionFromWorkflow({ workflow_id: "trim", steps: [{ step_id: "source", step_kind: "set_variable" }, { step_id: "target", step_kind: "stop_success", display_name: "  Target name  " }], settings: {} });
    expect(scopeTargetOptions(model, { groupId: "", forEachId: "", laneId: "" }, { kind: "step", id: "source" }, "Exit").map((row) => row.label)).toEqual(["Target name"]);
  });
  it("preserves the child root scope inside a visual hook frame and offers the end of the hook", () => {
    const frame = hookGroupId("before"); const start = hookMemberId("before", "start"); const group = hookMemberId("before", "once"); const each = hookMemberId("before", "items");
    const projected = definitionFromWorkflow({ workflow_id: "projected", steps: [{ step_id: start, step_kind: "set_variable", group_id: frame }], groups: [{ group_id: frame, body: { kind: "sequence" } }, { group_id: group, parent: { kind: "group", id: frame }, body: { kind: "sequence" } }], for_each: [{ for_each_id: each, parent: { kind: "group", id: frame }, over: "state.items", item_name: "item", body: { kind: "sequence" } }], settings: {} });
    const source = { kind: "step", id: start } as const;
    expect(connectionRefusal(projected, source, { name: "success" }, { kind: "group", id: group })).toBeNull();
    const targets = scopeTargetOptions(projected, { groupId: frame, forEachId: "", laneId: "" }, source, "Exit");
    expect(targets.map((option) => option.target)).toEqual([{ kind: "for_each", id: each }, { kind: "group_exit", group_id: frame, lane_id: null }, { kind: "group", id: group }]);
    // R2-11: the frame's exit is the hook's hidden end, reached like a Group's exit.
    expect(connectionRefusal(projected, source, { name: "success" }, { kind: "group_exit", group_id: frame, lane_id: null })).toBeNull();
    expect(scopeExitTargetLabel({ groupId: frame, forEachId: "", laneId: "" })).toBe("End the hook here");
  });
  it("names a hook's hidden end, in its own workflow's terms, as the end of the hook", () => {
    const child = definitionFromWorkflow({ workflow_id: "storage", steps: [{ step_id: "adjust", step_kind: "set_variable" }, { step_id: HOOK_END_STEP_ID, step_kind: "stop_success" }], settings: {} });
    const targets = scopeTargetOptions(child, { groupId: "", forEachId: "", laneId: "" }, { kind: "step", id: "adjust" }, "Exit");
    expect(targets).toEqual([{ key: expect.any(String), label: "End the hook here", target: { kind: "step", id: HOOK_END_STEP_ID } }]);
  });
  it.each(["true", "false", "case", "default", "rejected", "error"])("allows explicit rework from %s in the same sequential scope", (name) => {
    const sourceId = name === "rejected" ? "approved" : name === "true" || name === "false" ? "t" : "s";
    const next = sourceId === "t" ? { kind: "step", id: "next" } as const : target;
    const outcome = { name, ...(name === "case" ? { case_id: "red" } : {}) } as WorkflowOutcome;
    expect(connectionRefusal(definition, { kind: "step", id: sourceId }, outcome, next, true)).toBeNull();
  });
  it.each([{ id: "next", name: "success" }, { id: "approved", name: "approved" }])("keeps normal $name valid and rejects only its explicit rework intent", ({ id, name }) => {
    const source = { kind: "step", id } as const; const outcome = { name } as WorkflowOutcome;
    expect(connectionRefusal(definition, source, outcome, target)).toBeNull();
    expect(connectionRefusal(definition, source, outcome, target, true)?.code).toBe("rework-outcome");
  });
  it("keeps For each references and lane exits; refuses foreign scopes and terminal outputs", () => {
    const inner = { kind: "step", id: "inside" } as const;
    expect(connectionRefusal(definition, inner, { name: "true" }, { kind: "step", id: "body" }, true)).toBeNull();
    expect(connectionRefusal(definition, inner, { name: "true" }, { kind: "for_each_exit", for_each_id: "each", lane_id: null })).toBeNull();
    expect(connectionRefusal(definition, inner, { name: "true" }, { kind: "for_each_exit", for_each_id: "each", lane_id: null }, true)?.code).toBe("rework-exit");
    expect(connectionRefusal(definition, { kind: "step", id: "s" }, { name: "default" }, { kind: "for_each_exit", for_each_id: "each", lane_id: null })?.code).toBe("foreign-exit");
    expect(connectionRefusal(definition, { kind: "step", id: "lane" }, { name: "true" }, { kind: "step", id: "other" })?.code).toBe("scope");
    expect(connectionRefusal(definition, { kind: "step", id: "stop" }, { name: "error" }, target)?.code).toBe("outcome");
    expect(scopeTargetOptions(definition, { groupId: "", forEachId: "each", laneId: "" }, inner, "Exit").map((row) => row.target)).toEqual([{ kind: "step", id: "body" }, { kind: "for_each_exit", for_each_id: "each", lane_id: null }]);
  });
});
