import {
  authorRef,
  canvasOperation,
  childRef,
  clearHookOperations,
  HOOK_END_STEP_ID,
  hookEndCompletion,
  hookFrame,
  isHookMemberRef,
  canvasPlacement,
  childPlacement,
  parseHookNodeId,
  withHookFrames,
} from "@/pages/workflows/builder/hook-frames";
import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import { definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import type { WorkflowCanvasGraph, WorkflowTargetRef } from "@/pages/workflows/shared/api";

const NEXT = [{ outcome: { name: "success" as const }, label: "Next" }];

function graph(steps: WorkflowCanvasGraph["steps"], edges: WorkflowCanvasGraph["edges"], entry: WorkflowCanvasGraph["entry"]): WorkflowCanvasGraph {
  return {
    workflow: { id: "wf", display_name: "WF", enabled: true, callable_by_parent: false },
    interface_contract: {},
    output_contract: {},
    entry,
    groups: [],
    steps,
    edges,
    validation_issues: [],
    automation_rules: [],
  } as WorkflowCanvasGraph;
}

const author = graph(
  [
    { id: "prepare", step_id: "prepare", kind: "set_variable", display_name: "Prepare", config_summary: "", ports: NEXT },
    { id: "before_finish", step_id: "before_finish", kind: "invoke_workflow", display_name: "Before", config_summary: "", ports: NEXT },
    { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
  ],
  [
    { source: { kind: "step", id: "prepare" }, outcome: { name: "success" }, target: { kind: "step", id: "before_finish" } },
    { source: { kind: "step", id: "before_finish" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
  ],
  { kind: "step", id: "prepare" },
);
const child = graph(
  [
    { id: "fix", step_id: "fix", kind: "set_variable", display_name: "Fix", config_summary: "", ports: NEXT },
    { id: "stop", step_id: "stop", kind: "stop_success", display_name: "Stop", config_summary: "", ports: [] },
  ],
  [{ source: { kind: "step", id: "fix" }, outcome: { name: "success" }, target: { kind: "step", id: "stop" } }],
  { kind: "step", id: "fix" },
);
const state = {
  hookId: "before_finish",
  stepId: "before_finish",
  displayName: "Before the order is finished",
  description: "",
  connected: true,
  targetWorkflowId: "client_fix",
  targetName: "Fix the name",
  bindingRevision: 1,
};

describe("hook frames", () => {
  it("replace a root hook step with a Group holding the client's steps, and rewire the author's edges", () => {
    const frame = hookFrame(state, child, 2);
    const framed = withHookFrames(author, new Map([["before_finish", frame]]));

    expect(framed.groups).toEqual([
      expect.objectContaining({
        id: "hook:before_finish",
        display_name: "Before the order is finished",
        body: { entry: { kind: "step", id: "hook:before_finish:fix" } },
        hook: expect.objectContaining({ hookId: "before_finish", usedIn: 2, targetName: "Fix the name" }),
      }),
    ]);
    expect(framed.steps.map((step) => [step.id, step.group_id || ""])).toEqual([
      ["prepare", ""],
      ["done", ""],
      ["hook:before_finish:fix", "hook:before_finish"],
      ["hook:before_finish:stop", "hook:before_finish"],
    ]);
    expect(framed.edges).toEqual([
      { source: { kind: "step", id: "prepare" }, outcome: { name: "success" }, target: { kind: "group", id: "hook:before_finish" } },
      { source: { kind: "group", id: "hook:before_finish" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
      { source: { kind: "step", id: "hook:before_finish:fix" }, outcome: { name: "success" }, target: { kind: "step", id: "hook:before_finish:stop" } },
    ]);
  });

  it("draw an empty hook as an empty frame", () => {
    const framed = withHookFrames(author, new Map([["before_finish", hookFrame({ ...state, connected: false, targetWorkflowId: "" }, null)]]));

    expect(framed.groups[0]).toMatchObject({ id: "hook:before_finish", body: {} });
    expect(framed.steps.filter((step) => step.group_id)).toEqual([]);
  });

  it.each(["group", "for_each"] as const)("does not project an invalid hook inside an author %s", (kind) => {
    const invalid = graph([{ ...author.steps[1], group_id: kind === "group" ? "outer" : null, for_each_id: kind === "for_each" ? "outer" : null }], [], null);
    invalid.validation_issues = [{ scope: "step", scope_id: "before_finish", severity: "error", code: "workflow.hook.root_only", title: "Hooks belong in the workflow root, before or after a Group or For each." }];
    const projected = withHookFrames(invalid, new Map([["before_finish", hookFrame(state, child)]]));
    expect(projected).toBe(invalid);
    expect(projected.steps[0]).toMatchObject({ id: "before_finish", kind: "invoke_workflow" });
    expect(projected.validation_issues[0].title).toContain("workflow root");
  });

  it("retains child Group lanes, nested For each, container entries and all exit edges", () => {
    const nested = graph([
      { ...child.steps[0], id: "left", step_id: "left", group_id: "checks", lane_id: "left" },
      { ...child.steps[0], id: "right", step_id: "right", group_id: "checks", lane_id: "right" },
      { ...child.steps[0], id: "item", step_id: "item", for_each_id: "items" }, child.steps[1],
    ], [
      { source: { kind: "step", id: "left" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "checks", lane_id: "left" } },
      { source: { kind: "step", id: "right" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "checks", lane_id: "right" } },
      { source: { kind: "group", id: "checks" }, outcome: { name: "success" }, target: { kind: "group", id: "sequence" } },
      { source: { kind: "for_each", id: "items" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "sequence", lane_id: null } },
      { source: { kind: "step", id: "item" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "items", lane_id: null } },
    ], { kind: "group", id: "checks" });
    nested.groups = [
      { id: "checks", group_id: "checks", display_name: "Checks", body: { kind: "lanes", concurrency_limit: 2, lanes: [{ lane_id: "left", entry: { kind: "step", id: "left" } }, { lane_id: "right", entry: { kind: "step", id: "right" } }] } },
      { id: "sequence", group_id: "sequence", display_name: "Sequence", body: { kind: "sequence", entry: { kind: "for_each", id: "items" } } },
    ];
    nested.for_each = [{ id: "items", for_each_id: "items", group_id: "sequence", display_name: "Items", over: "state.items", item_name: "item", body: { kind: "sequence", entry: { kind: "step", id: "item" } } }];
    const original = structuredClone(nested);
    const frame = hookFrame(state, nested);
    const result = withHookFrames(author, new Map([["before_finish", frame]]));
    expect(frame.memberEntry).toEqual({ kind: "group", id: "hook:before_finish:checks" });
    expect(result.groups.find((group) => group.id === "hook:before_finish")?.body).toEqual({ entry: { kind: "group", id: "hook:before_finish:checks" } });
    expect(result.groups.find((group) => group.id === "hook:before_finish:checks")).toMatchObject({ group_id: "hook:before_finish:checks", parent: { kind: "group", id: "hook:before_finish" }, body: { kind: "lanes", concurrency_limit: 2, lanes: [{ lane_id: "left", entry: { kind: "step", id: "hook:before_finish:left" } }, { lane_id: "right", entry: { kind: "step", id: "hook:before_finish:right" } }] } });
    expect(result.groups.find((group) => group.id === "hook:before_finish:sequence")?.body).toEqual({ kind: "sequence", entry: { kind: "for_each", id: "hook:before_finish:items" } });
    expect(result.for_each).toEqual([expect.objectContaining({ id: "hook:before_finish:items", group_id: "hook:before_finish:sequence", parent: { kind: "group", id: "hook:before_finish:sequence" }, body: { kind: "sequence", entry: { kind: "step", id: "hook:before_finish:item" } } })]);
    expect(result.steps.map((step) => [step.id, step.group_id || "", step.for_each_id || "", step.lane_id || ""])).toEqual([
      ["prepare", "", "", ""], ["done", "", "", ""], ["hook:before_finish:left", "hook:before_finish:checks", "", "left"], ["hook:before_finish:right", "hook:before_finish:checks", "", "right"], ["hook:before_finish:item", "", "hook:before_finish:items", ""], ["hook:before_finish:stop", "hook:before_finish", "", ""],
    ]);
    expect(result.edges.slice(2)).toEqual([
      { source: { kind: "step", id: "hook:before_finish:left" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "hook:before_finish:checks", lane_id: "left" } },
      { source: { kind: "step", id: "hook:before_finish:right" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "hook:before_finish:checks", lane_id: "right" } },
      { source: { kind: "group", id: "hook:before_finish:checks" }, outcome: { name: "success" }, target: { kind: "group", id: "hook:before_finish:sequence" } },
      { source: { kind: "for_each", id: "hook:before_finish:items" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "hook:before_finish:sequence", lane_id: null } },
      { source: { kind: "step", id: "hook:before_finish:item" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "hook:before_finish:items", lane_id: null } },
    ]);
    expect(nested).toEqual(original);
  });

  it.each([
    [{ kind: "group", id: "hook:before_finish:checks" }, { kind: "group", id: "checks" }],
    [{ kind: "for_each", id: "hook:before_finish:items" }, { kind: "for_each", id: "items" }],
    [{ kind: "group_exit", group_id: "hook:before_finish:checks", lane_id: "left" }, { kind: "group_exit", group_id: "checks", lane_id: "left" }],
    [{ kind: "for_each_exit", for_each_id: "hook:before_finish:items", lane_id: null }, { kind: "for_each_exit", for_each_id: "items", lane_id: null }],
  ] as Array<[WorkflowTargetRef, WorkflowTargetRef]>)("restores child references: %j", (projected, raw) => {
    expect(childRef(projected)).toEqual(raw);
    expect(isHookMemberRef(projected)).toBe(true);
  });

  it("restores child placements and recognizes For each canvas prefixes", () => {
    expect(parseHookNodeId("for_each:hook:before_finish:items")).toEqual({ hookId: "before_finish", memberStepId: "items" });
    expect(canvasPlacement("before_finish", { groupId: "", forEachId: "", laneId: "" })).toEqual({ groupId: "hook:before_finish", forEachId: "", laneId: "" });
    expect(canvasPlacement("before_finish", { groupId: "", forEachId: "items", laneId: "left" })).toEqual({ groupId: "", forEachId: "hook:before_finish:items", laneId: "left" });
    expect(childPlacement({ groupId: "hook:before_finish", forEachId: "", laneId: "" })).toEqual({ groupId: "", forEachId: "", laneId: "" });
    expect(childPlacement({ groupId: "hook:before_finish:checks", forEachId: "", laneId: "left" })).toEqual({ groupId: "checks", forEachId: "", laneId: "left" });
    expect(childPlacement({ groupId: "", forEachId: "hook:before_finish:items", laneId: "right" })).toEqual({ groupId: "", forEachId: "items", laneId: "right" });
  });

  it.each([undefined, null, 0, -1, 1.5, 7])("pins only positive integer child revisions: %s", (revision) => {
    const graph = { ...child, workflow: { ...child.workflow, revision } };
    expect(hookFrame(state, graph).targetRevision).toBe(revision === 7 ? 7 : null);
  });

  it.each([
    [{ op: "create_group", group: { group_id: "checks", body: { kind: "sequence", entry: { kind: "for_each", id: "items" } } } }, { op: "create_group", group: { group_id: "hook:before_finish:checks", body: { kind: "sequence", entry: { kind: "for_each", id: "hook:before_finish:items" } } } }],
    [{ op: "create_for_each", for_each: { for_each_id: "items", group_id: "checks", over: "state.items", item_name: "item" } }, { op: "create_for_each", for_each: { for_each_id: "hook:before_finish:items", group_id: "hook:before_finish:checks", over: "state.items", item_name: "item" } }],
    [{ op: "create_step", step: { step_id: "fix", step_kind: "set_variable", for_each_id: "items", lane_id: "left" } }, { op: "create_step", step: { step_id: "hook:before_finish:fix", step_kind: "set_variable", for_each_id: "hook:before_finish:items", lane_id: "left" } }],
    [{ op: "connect", edge: { source: { kind: "group", id: "checks" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "items", lane_id: "left" } } }, { op: "connect", edge: { source: { kind: "group", id: "hook:before_finish:checks" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "hook:before_finish:items", lane_id: "left" } } }],
    [{ op: "disconnect", source: { kind: "for_each", id: "items" }, outcome: { name: "success" } }, { op: "disconnect", source: { kind: "for_each", id: "hook:before_finish:items" }, outcome: { name: "success" } }],
    [{ op: "set_entry", target: { kind: "for_each", id: "items" }, group_id: "checks", lane_id: "left" }, { op: "set_entry", target: { kind: "for_each", id: "hook:before_finish:items" }, group_id: "hook:before_finish:checks", lane_id: "left" }],
    [{ op: "move_step", step_id: "fix", group_id: null, for_each_id: "items", lane_id: "left" }, { op: "move_step", step_id: "hook:before_finish:fix", group_id: null, for_each_id: "hook:before_finish:items", lane_id: "left" }],
    [{ op: "move_for_each", for_each_id: "items", group_id: "checks" }, { op: "move_for_each", for_each_id: "hook:before_finish:items", group_id: "hook:before_finish:checks" }],
    [{ op: "delete_group", group_id: "checks", with_contents: true }, { op: "delete_group", group_id: "hook:before_finish:checks", with_contents: true }],
    [{ op: "delete_for_each", for_each_id: "items", with_contents: true }, { op: "delete_for_each", for_each_id: "hook:before_finish:items", with_contents: true }],
    [{ op: "update_step_config", step_id: "fix", patch: { value: "checks" } }, { op: "update_step_config", step_id: "hook:before_finish:fix", patch: { value: "checks" } }],
    [{ op: "change_step_kind", step_id: "fix", step: { step_kind: "stop_success" } }, { op: "change_step_kind", step_id: "hook:before_finish:fix", step: { step_kind: "stop_success" } }],
    [{ op: "update_group_config", group_id: "checks", patch: { body: { kind: "sequence", entry: { kind: "step", id: "fix" } } } }, { op: "update_group_config", group_id: "hook:before_finish:checks", patch: { body: { kind: "sequence", entry: { kind: "step", id: "hook:before_finish:fix" } } } }],
    [{ op: "update_for_each_config", for_each_id: "items", patch: { over: "state.items" } }, { op: "update_for_each_config", for_each_id: "hook:before_finish:items", patch: { over: "state.items" } }],
    [{ op: "delete_step", step_id: "fix" }, { op: "delete_step", step_id: "hook:before_finish:fix" }],
    [{ op: "insert_step_between", source: { kind: "group", id: "checks" }, outcome: { name: "success" }, step: { step_id: "fix", step_kind: "set_variable", group_id: "checks" } }, { op: "insert_step_between", source: { kind: "group", id: "hook:before_finish:checks" }, outcome: { name: "success" }, step: { step_id: "hook:before_finish:fix", step_kind: "set_variable", group_id: "hook:before_finish:checks" } }],
  ] as Array<[WorkflowChangeOperation, WorkflowChangeOperation]>)("projects draft references without changing payload values: %j", (operation, expected) => {
    const original = structuredClone(operation);
    expect(canvasOperation("before_finish", operation)).toEqual(expected);
    expect(operation).toEqual(original);
  });

  it("map references both ways", () => {
    const frames = new Map([["before_finish", hookFrame(state, child)]]);

    expect(parseHookNodeId("group:hook:before_finish")).toEqual({ hookId: "before_finish", memberStepId: "" });
    expect(parseHookNodeId("hook:before_finish:fix")).toEqual({ hookId: "before_finish", memberStepId: "fix" });
    expect(parseHookNodeId("prepare")).toBeNull();
    expect(authorRef({ kind: "group", id: "hook:before_finish" }, frames)).toEqual({ kind: "step", id: "before_finish" });
    expect(authorRef({ kind: "group", id: "g" }, frames)).toEqual({ kind: "group", id: "g" });
    expect(childRef({ kind: "step", id: "hook:before_finish:fix" })).toEqual({ kind: "step", id: "fix" });
    expect(canvasOperation("before_finish", { op: "create_step", step: { step_id: "notify", step_kind: "set_variable" } })).toEqual({
      op: "create_step",
      step: { step_id: "hook:before_finish:notify", step_kind: "set_variable" },
    });
  });
});

describe("a hook's hidden end (R2-11)", () => {
  const end = { id: HOOK_END_STEP_ID, step_id: HOOK_END_STEP_ID, kind: "stop_success", display_name: "", config_summary: "", ports: [] };
  const exit = { kind: "group_exit", group_id: "hook:before_finish", lane_id: null };
  const storage = (steps: WorkflowCanvasGraph["steps"], edges: WorkflowCanvasGraph["edges"], entry: WorkflowCanvasGraph["entry"]) => graph([...steps, end], edges, entry);
  const fix = child.steps[0];

  it("draws a new hook's storage as an empty frame: its end is no card", () => {
    const frame = hookFrame(state, storage([], [], { kind: "step", id: HOOK_END_STEP_ID }));
    const framed = withHookFrames(author, new Map([["before_finish", frame]]));

    expect(frame.stepCount).toBe(0);
    expect(framed.groups[0]).toMatchObject({ id: "hook:before_finish", body: {}, hook: expect.objectContaining({ stepCount: 0 }) });
    expect(framed.steps.filter((step) => step.group_id)).toEqual([]);
  });

  it("draws a step leading to the hidden end as finishing the hook frame, and maps it back", () => {
    const frame = hookFrame(state, storage([fix], [{ source: { kind: "step", id: "fix" }, outcome: { name: "success" }, target: { kind: "step", id: HOOK_END_STEP_ID } }], { kind: "step", id: "fix" }));

    expect(frame.members.map((step) => step.id)).toEqual(["hook:before_finish:fix"]);
    expect(frame.memberEdges).toEqual([{ source: { kind: "step", id: "hook:before_finish:fix" }, outcome: { name: "success" }, target: exit }]);
    expect(frame.memberEntry).toEqual({ kind: "step", id: "hook:before_finish:fix" });
    expect(childRef(exit as WorkflowTargetRef)).toEqual({ kind: "step", id: HOOK_END_STEP_ID });
    expect(canvasOperation("before_finish", { op: "connect", edge: { source: { kind: "step", id: "fix" }, outcome: { name: "success" }, target: { kind: "step", id: HOOK_END_STEP_ID } } })).toEqual({
      op: "connect", edge: { source: { kind: "step", id: "hook:before_finish:fix" }, outcome: { name: "success" }, target: exit },
    });
  });

  it("marks a hook without a project, so its Add step can say why", () => {
    const framed = withHookFrames(author, new Map([["before_finish", hookFrame({ ...state, connected: null, needsProject: true }, null)]]));
    expect(framed.groups[0].hook).toMatchObject({ needsProject: true });
  });

  const storageDefinition = (steps: Array<Record<string, unknown>>, entry: string, edges: Array<Record<string, unknown>> = []) => definitionFromWorkflow({
    workflow_id: "storage", settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: entry }, edges } },
    steps: [...steps, { step_id: HOOK_END_STEP_ID, step_kind: "stop_success" }],
  } as never);

  it("starts an empty hook with its first step and leads that step to the end", () => {
    const base = storageDefinition([], HOOK_END_STEP_ID);
    const create: WorkflowChangeOperation = { op: "create_step", step: { step_id: "adjust", step_kind: "set_variable" } };

    expect(hookEndCompletion(base, [], [create])).toEqual([
      { op: "set_entry", target: { kind: "step", id: "adjust" } },
      { op: "connect", edge: { source: { kind: "step", id: "adjust" }, outcome: { name: "success" }, target: { kind: "step", id: HOOK_END_STEP_ID } } },
    ]);
    // A branching first step starts the hook; its outcomes stay for the author to route.
    expect(hookEndCompletion(base, [], [{ op: "create_step", step: { step_id: "check", step_kind: "if" } }])).toEqual([
      { op: "set_entry", target: { kind: "step", id: "check" } },
    ]);
  });

  it("lets the end start the hook again when its last step goes, and leaves other edits alone", () => {
    const base = storageDefinition([{ step_id: "adjust", step_kind: "set_variable" }], "adjust", [
      { source: { kind: "step", id: "adjust" }, outcome: { name: "success" }, target: { kind: "step", id: HOOK_END_STEP_ID } },
    ]);

    expect(hookEndCompletion(base, [], [{ op: "delete_step", step_id: "adjust" }])).toEqual([
      { op: "set_entry", target: { kind: "step", id: HOOK_END_STEP_ID } },
    ]);
    expect(hookEndCompletion(base, [], [{ op: "create_step", step: { step_id: "more", step_kind: "set_variable" } }])).toEqual([]);
    // An older connected workflow without the hidden end gets nothing.
    const legacy = definitionFromWorkflow({ workflow_id: "legacy", steps: [{ step_id: "fix", step_kind: "set_variable" }], settings: {} } as never);
    expect(hookEndCompletion(legacy, [], [{ op: "delete_step", step_id: "fix" }])).toEqual([]);
  });

  it("clears every node of the hook but its end, which starts it again", () => {
    const base = definitionFromWorkflow({
      workflow_id: "storage",
      settings: { execution_graph: { schema_version: 1, entry: { kind: "group", id: "checks" }, edges: [] } },
      groups: [{ group_id: "checks", body: { kind: "sequence" } }],
      for_each: [{ for_each_id: "items", over: "state.items", item_name: "item", body: { kind: "sequence" } }],
      steps: [{ step_id: "inside", step_kind: "set_variable", group_id: "checks" }, { step_id: "adjust", step_kind: "set_variable" }, { step_id: HOOK_END_STEP_ID, step_kind: "stop_success" }],
    } as never);

    expect(clearHookOperations(base)).toEqual([
      { op: "delete_group", group_id: "checks", with_contents: true },
      { op: "delete_for_each", for_each_id: "items", with_contents: true },
      { op: "delete_step", step_id: "adjust" },
      { op: "set_entry", target: { kind: "step", id: HOOK_END_STEP_ID } },
    ]);
    expect(hookEndCompletion(base, [], clearHookOperations(base))).toEqual([]);
  });

  it("empties a hook made before the hidden end existed by adding the end first", () => {
    // The demo's older hooks hold Build the result / Return the result / done and no hidden end.
    const legacy = definitionFromWorkflow({
      workflow_id: "storage",
      settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "build_result" }, edges: [] } },
      steps: [{ step_id: "build_result", step_kind: "transform" }, { step_id: "return_result", step_kind: "set_variable" }, { step_id: "done", step_kind: "stop_success" }],
    } as never);

    expect(clearHookOperations(legacy)).toEqual([
      { op: "create_step", step: { step_id: HOOK_END_STEP_ID, step_kind: "stop_success" } },
      { op: "delete_step", step_id: "build_result" },
      { op: "delete_step", step_id: "return_result" },
      { op: "delete_step", step_id: "done" },
      { op: "set_entry", target: { kind: "step", id: HOOK_END_STEP_ID } },
    ]);
  });

  it("keeps a hook's chain whole when a step in the middle is removed", () => {
    // Owner feedback, demo 0.7.21: deleting a step in a hook left a gap and Apply was refused.
    const chain = (withEnd: boolean) => definitionFromWorkflow({
      workflow_id: "storage",
      settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "build_result" }, edges: [
        { source: { kind: "step", id: "build_result" }, outcome: { name: "success" }, target: { kind: "step", id: "return_result" } },
        { source: { kind: "step", id: "return_result" }, outcome: { name: "success" }, target: { kind: "step", id: withEnd ? HOOK_END_STEP_ID : "done" } },
      ] } },
      steps: [{ step_id: "build_result", step_kind: "transform" }, { step_id: "return_result", step_kind: "set_variable" }, { step_id: withEnd ? HOOK_END_STEP_ID : "done", step_kind: "stop_success" }],
    } as never);
    expect(hookEndCompletion(chain(false), [], [{ op: "delete_step", step_id: "return_result" }])).toEqual([
      { op: "connect", edge: { source: { kind: "step", id: "build_result" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } } },
    ]);
    expect(hookEndCompletion(chain(true), [], [{ op: "delete_step", step_id: "return_result" }])).toEqual([
      { op: "connect", edge: { source: { kind: "step", id: "build_result" }, outcome: { name: "success" }, target: { kind: "step", id: HOOK_END_STEP_ID } } },
    ]);
    expect(hookEndCompletion(chain(false), [], [{ op: "delete_step", step_id: "build_result" }])).toEqual([
      { op: "set_entry", target: { kind: "step", id: "return_result" } },
    ]);
  });
});
