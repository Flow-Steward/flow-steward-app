import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import {
  applyChangeOperations,
  canvasGraphWithDefinition,
  definitionFromWorkflow,
  edgesDroppedByKindChange,
  foldStagedStepEdits,
  localDefinitionIssues,
  moveStepOperations,
  operationTouchesGroup,
  operationTouchesStep,
  reconcileChangeOperations,
  stepPorts,
  uniqueLocalId,
} from "@/pages/workflows/builder/change-set";
import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";

const base = definitionFromWorkflow({
  workflow_id: "wf",
  steps: [
    { step_id: "a", step_kind: "agent", display_name: "A" },
    { step_id: "b", step_kind: "agent", display_name: "B" },
    { step_id: "inner", step_kind: "agent", group_id: "loop", display_name: "Inner" },
  ],
  groups: [
    {
      group_id: "loop",
      display_name: "Loop",
      body: { kind: "sequence", entry: { kind: "step", id: "inner" } },
    },
  ],
  settings: {
    execution_graph: {
      schema_version: 1,
      entry: { kind: "step", id: "a" },
      edges: [
        { source: { kind: "step", id: "a" }, outcome: { name: "success" }, target: { kind: "step", id: "b" } },
        {
          source: { kind: "step", id: "inner" },
          outcome: { name: "success" },
          target: { kind: "group_exit", group_id: "loop", lane_id: null },
        },
      ],
    },
  },
});

describe("builder change set preview", () => {
  it("previews the same operations the server applies, without touching the base", () => {
    const { definition, rejected } = applyChangeOperations(base, [
      { op: "create_step", step: { step_id: "c", step_kind: "stop_success" } },
      {
        op: "connect",
        edge: { source: { kind: "step", id: "b" }, outcome: { name: "success" }, target: { kind: "step", id: "c" } },
      },
    ]);
    expect(rejected).toEqual({});
    expect(definition.steps.map((step) => step.step_id)).toEqual(["a", "b", "inner", "c"]);
    expect(definition.graph.edges).toHaveLength(3);
    // The first root node only becomes the entry when there is none.
    expect(definition.graph.entry).toEqual({ kind: "step", id: "a" });
    expect(base.steps).toHaveLength(3);
  });

  it("refuses to take an outcome that already leads somewhere unless replace is explicit", () => {
    const takenEdge = {
      source: { kind: "step" as const, id: "a" },
      outcome: { name: "success" as const },
      target: { kind: "step" as const, id: "inner" },
    };
    expect(applyChangeOperations(base, [{ op: "connect", edge: takenEdge }]).rejected).toEqual({ 0: "outcome taken" });
    const replaced = applyChangeOperations(base, [{ op: "connect", edge: takenEdge, replace: true }]);
    expect(replaced.rejected).toEqual({});
    expect(replaced.definition.graph.edges.filter((edge) => edge.source.id === "a")).toEqual([takenEdge]);
  });

  it("drops a moved step's edges and entry roles instead of guessing a reconnection", () => {
    const { definition } = applyChangeOperations(base, [
      { op: "move_step", step_id: "inner", group_id: null, lane_id: null },
    ]);
    const moved = definition.steps.find((step) => step.step_id === "inner");
    expect(moved?.group_id).toBeNull();
    expect(definition.graph.edges.some((edge) => edge.source.id === "inner")).toBe(false);
    expect((definition.groups[0].body as { entry?: unknown }).entry).toBeUndefined();
  });

  it("deletes a step without building a bypass around it", () => {
    const { definition } = applyChangeOperations(base, [{ op: "delete_step", step_id: "a" }]);
    expect(definition.graph.entry).toBeNull();
    expect(definition.graph.edges.some((edge) => edge.source.id === "a" || (edge.target as { id?: string }).id === "a")).toBe(false);
    expect(definition.graph.edges.some((edge) => edge.target.kind === "step" && edge.target.id === "b")).toBe(false);
  });

  it("deletes a group only with its contents", () => {
    const { definition } = applyChangeOperations(base, [
      { op: "delete_group", group_id: "loop", with_contents: true },
    ]);
    expect(definition.groups).toEqual([]);
    expect(definition.steps.map((step) => step.step_id)).toEqual(["a", "b"]);
    expect(definition.graph.edges.every((edge) => edge.target.kind !== "group_exit")).toBe(true);
  });

  it("inserts an ordinary step into an edge, keeping the source outcome", () => {
    const { definition } = applyChangeOperations(base, [
      {
        op: "insert_step_between",
        source: { kind: "step", id: "a" },
        outcome: { name: "success" },
        step: { step_id: "mid", step_kind: "transform" },
      },
    ]);
    expect(definition.graph.edges.slice(0, 2)).toEqual([
      { source: { kind: "step", id: "a" }, outcome: { name: "success" }, target: { kind: "step", id: "mid" } },
      { source: { kind: "step", id: "mid" }, outcome: { name: "success" }, target: { kind: "step", id: "b" }, rework: false },
    ]);
  });

  it("sets a body or lane entry explicitly", () => {
    const { definition } = applyChangeOperations(base, [
      { op: "create_step", step: { step_id: "first", step_kind: "agent", group_id: "loop" } },
      { op: "set_entry", target: { kind: "step", id: "first" }, group_id: "loop" },
    ]);
    expect((definition.groups[0].body as { entry?: unknown }).entry).toEqual({ kind: "step", id: "first" });
    // A step created in a group never becomes the root entry.
    expect(definition.graph.entry).toEqual({ kind: "step", id: "a" });
  });
});

describe("builder ports and local checks", () => {
  it("gives every kind its own named outcomes and a permanent error port except on terminals", () => {
    const names = (step: Record<string, unknown>) =>
      stepPorts(step).map((port) => port.outcome.case_id ? `case:${port.outcome.case_id}` : port.outcome.name);
    expect(names({ step_id: "x", step_kind: "if" })).toEqual(["true", "false", "error"]);
    expect(
      names({ step_id: "x", step_kind: "switch", override: { switch: { cases: [{ case_id: "vip", when: "vip" }] } } }),
    ).toEqual(["case:vip", "default", "error"]);
    expect(names({ step_id: "x", step_kind: "approval" })).toEqual(["approved", "rejected", "error"]);
    expect(names({ step_id: "x", step_kind: "wait" })).toEqual(["success", "error"]);
    expect(names({ step_id: "x", step_kind: "stop_fail" })).toEqual([]);
    expect(names({ step_id: "x", step_kind: "agent", error_policy: { on_error: "route_to_step" } })).toEqual([
      "success",
      "error",
    ]);
    expect(names({ step_id: "x", step_kind: "agent", error_policy: { on_error: "retry" } })).toEqual(["success", "error"]);
  });

  it("names unrouted outcomes and a missing entry", () => {
    const messages = localDefinitionIssues(base).map((issue) => issue.code);
    // b leads nowhere and the loop group has no next step after it.
    expect(messages).toEqual(["outcome_unrouted", "group_outcome_unrouted"]);
    const { definition } = applyChangeOperations(base, [{ op: "delete_step", step_id: "a" }]);
    expect(localDefinitionIssues(definition).map((issue) => issue.code)).toContain("entry_missing");
  });

  it("knows which steps and groups a structural operation concerns", () => {
    const connect = {
      op: "connect" as const,
      edge: { source: { kind: "step" as const, id: "a" }, outcome: { name: "success" as const }, target: { kind: "group" as const, id: "loop" } },
    };
    expect(operationTouchesStep(connect, "a")).toBe(true);
    expect(operationTouchesStep(connect, "b")).toBe(false);
    expect(operationTouchesGroup(connect, "loop")).toBe(true);
    expect(operationTouchesStep({ op: "update_step_config", step_id: "b", patch: {} }, "b")).toBe(true);
  });

  it("chooses a stable unique id for a new node before it is written", () => {
    expect(uniqueLocalId("Review Draft", ["a"])).toBe("review_draft");
    expect(uniqueLocalId("A", ["a", "a_2"])).toBe("a_3");
    expect(uniqueLocalId("   ", [], "group")).toBe("group");
  });

  describe("changing a saved step's kind", () => {
    const routed = definitionFromWorkflow({
      workflow_id: "wf",
      steps: [
        {
          step_id: "check",
          step_kind: "if",
          display_name: "Check",
          group_id: null,
          error_policy: { on_error: "route_to_step" },
          optional: true,
          override: { if: { condition: { selector: "state.ok", operator: "truthy" } } },
        },
        { step_id: "yes", step_kind: "agent" },
        { step_id: "no", step_kind: "agent" },
        { step_id: "oops", step_kind: "agent" },
      ],
      settings: {
        execution_graph: {
          schema_version: 1,
          entry: { kind: "step", id: "check" },
          edges: [
            { source: { kind: "step", id: "check" }, outcome: { name: "true" }, target: { kind: "step", id: "yes" } },
            { source: { kind: "step", id: "check" }, outcome: { name: "false" }, target: { kind: "step", id: "no" } },
            { source: { kind: "step", id: "check" }, outcome: { name: "error" }, target: { kind: "step", id: "oops" } },
          ],
        },
      },
    });

    it("keeps identity, placement, name, error policy and optional, and replaces the old kind's config", () => {
      const { definition, rejected } = applyChangeOperations(routed, [
        {
          op: "change_step_kind",
          step_id: "check",
          step: { step_kind: "set_variable", override: { set_variable: { target: "state.x" } } },
        },
      ]);
      expect(rejected).toEqual({});
      expect(definition.steps[0]).toEqual({
        step_id: "check",
        step_kind: "set_variable",
        display_name: "Check",
        group_id: null,
        error_policy: { on_error: "route_to_step" },
        optional: true,
        override: { set_variable: { target: "state.x" } },
      });
      // true/false do not exist on the new kind; the error edge stays.
      expect(definition.graph.edges).toEqual([
        { source: { kind: "step", id: "check" }, outcome: { name: "error" }, target: { kind: "step", id: "oops" } },
      ]);
      expect(definition.graph.entry).toEqual({ kind: "step", id: "check" });
    });

    it("drops the error edge too when the step becomes a Stop, and lists what it drops", () => {
      const payload = { step_kind: "stop_success" };
      expect(edgesDroppedByKindChange(routed, "check", payload)).toHaveLength(3);
      const { definition } = applyChangeOperations(routed, [{ op: "change_step_kind", step_id: "check", step: payload }]);
      expect(definition.graph.edges).toEqual([]);
    });

    it("keeps the outcomes the new kind shares", () => {
      expect(
        edgesDroppedByKindChange(routed, "check", {
          step_kind: "approval",
          override: { approval: { message: "Ok?" } },
        }).map((edge) => edge.outcome.name),
      ).toEqual(["true", "false"]);
    });

    it("counts as a structural change of that step", () => {
      expect(
        operationTouchesStep({ op: "change_step_kind", step_id: "check", step: { step_kind: "agent" } }, "check"),
      ).toBe(true);
    });
  });
});

describe("canvas projection of a changed step kind", () => {
  it.each([["if", "ifStep"], ["wait", "wait"], ["stop_success", "end"]])("renders staged %s with its new node type", (kind, nodeType) => {
    const graph: WorkflowCanvasGraph = {
      workflow: { id: "wf", display_name: "Workflow", enabled: false, callable_by_parent: false },
      interface_contract: {}, output_contract: {}, groups: [], edges: [], validation_issues: [], automation_rules: [],
      steps: [{ id: "a", step_id: "a", kind: "agent", node_type: "action", display_name: "A", config_summary: "Old agent configuration" }],
    };
    const changed = applyChangeOperations(base, [{ op: "change_step_kind", step_id: "a", step: { step_kind: kind } }]).definition;
    const projected = canvasGraphWithDefinition(graph, changed).steps.find((step) => step.id === "a");
    expect(projected).toMatchObject({ kind, node_type: nodeType, display_name: "A" });
    expect(projected?.config_summary).not.toBe("Old agent configuration");
    expect(graph.steps[0].kind).toBe("agent");
  });
});

it("preserves a same-kind server If summary and its condition", () => {
  const condition = { selector: "state.line.qty", operator: "lte", value: 0 };
  const definition = definitionFromWorkflow({ workflow_id: "wf", steps: [{ step_id: "route", step_kind: "if", display_name: "Route", override: { if: { condition } } }], settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "route" }, edges: [] } } });
  const graph: WorkflowCanvasGraph = { workflow: { id: "wf", display_name: "Workflow", enabled: false, callable_by_parent: false }, interface_contract: {}, output_contract: {}, groups: [], edges: [], validation_issues: [], automation_rules: [], steps: [{ id: "route", step_id: "route", kind: "if", node_type: "ifStep", display_name: "Route", config_summary: "Server owned wording" }] };
  expect(canvasGraphWithDefinition(graph, definition).steps[0].config_summary).toBe("Server owned wording");
  expect(definition.steps[0].override).toEqual({ if: { condition } });
});

// The same sequences as test_a_stale_builder_set_is_refused_and_its_restated_form_applies
// (tests/unit/test_workflow_definition_service.py), which runs the stale set and the
// restated one through the real change engine.
describe("builder change sets that an in-place edit left stale", () => {
  const ref = (id: string) => ({ kind: "step" as const, id });
  const inGroup = (stepId: string, kind: string) => ({ step_id: stepId, step_kind: kind, group_id: "test_group", for_each_id: null, lane_id: null });
  const connect = (source: string, outcome: string, target: string): WorkflowChangeOperation => ({
    op: "connect",
    edge: { source: ref(source), outcome: { name: outcome } as never, target: ref(target) },
  });
  const empty = definitionFromWorkflow({ workflow_id: "wf", steps: [], groups: [] });
  const groupWithIfAndHttp: WorkflowChangeOperation[] = [
    { op: "create_group", group: { group_id: "test_group", display_name: "Test Group", body: { kind: "sequence" } } },
    { op: "create_step", step: inGroup("if", "if") },
    { op: "set_entry", target: ref("if"), group_id: "test_group" },
    { op: "create_step", step: inGroup("http_request", "http_request") },
    connect("if", "true", "http_request"),
  ];
  const groupBuild: WorkflowChangeOperation[] = [
    ...groupWithIfAndHttp,
    { op: "create_step", step: inGroup("approval", "approval") },
    connect("http_request", "success", "approval"),
  ];
  const withoutStep = (operations: WorkflowChangeOperation[], stepId: string) =>
    operations.filter((operation) => !operationTouchesStep(operation, stepId));

  it("drops the disconnect of an edge whose new target step was deleted (the stand repro)", () => {
    // Add after Approval · Approved → Tmp, set Approved to Not connected, delete Tmp.
    const staged: WorkflowChangeOperation[] = [
      ...groupBuild,
      { op: "create_step", step: inGroup("tmp", "agent") },
      connect("approval", "approved", "tmp"),
      { op: "disconnect", source: ref("approval"), outcome: { name: "approved" } },
    ];
    const afterDelete = withoutStep(staged, "tmp");
    expect(afterDelete.at(-1)).toEqual({ op: "disconnect", source: ref("approval"), outcome: { name: "approved" } });
    expect(applyChangeOperations(empty, afterDelete).rejected).toEqual({ 7: "edge missing" });
    expect(reconcileChangeOperations(empty, afterDelete)).toEqual(groupBuild);
  });

  it("turns an insert on an edge whose new target step was deleted into create and connect", () => {
    const staged: WorkflowChangeOperation[] = [
      ...groupBuild,
      { op: "insert_step_between", source: ref("http_request"), outcome: { name: "success" }, step: { step_id: "note", step_kind: "set_variable" } },
    ];
    const restated = reconcileChangeOperations(empty, withoutStep(staged, "approval"));
    expect(restated).toEqual([
      ...groupWithIfAndHttp,
      { op: "create_step", step: inGroup("note", "set_variable") },
      connect("http_request", "success", "note"),
    ]);
    expect(applyChangeOperations(empty, restated).rejected).toEqual({});
  });

  it("creates and connects an inserted step whose kind became branching, replacing the edge", () => {
    const staged: WorkflowChangeOperation[] = [
      ...groupWithIfAndHttp,
      { op: "insert_step_between", source: ref("if"), outcome: { name: "true" }, step: { step_id: "gate", step_kind: "approval" } },
    ];
    expect(reconcileChangeOperations(empty, staged)).toEqual([
      ...groupWithIfAndHttp,
      { op: "create_step", step: inGroup("gate", "approval") },
      { ...connect("if", "true", "gate"), replace: true },
    ]);
  });

  it("drops the disconnect of an edge a re-edited kind change now drops", () => {
    const saved = definitionFromWorkflow({
      workflow_id: "wf",
      steps: [{ step_id: "a", step_kind: "http_request" }, { step_id: "end", step_kind: "stop_success" }],
      settings: { execution_graph: { schema_version: 1, entry: ref("a"), edges: [{ source: ref("a"), outcome: { name: "success" }, target: ref("end") }] } },
    });
    const kindChange: WorkflowChangeOperation = { op: "change_step_kind", step_id: "a", step: { step_id: "a", step_kind: "if" } };
    expect(reconcileChangeOperations(saved, [kindChange, { op: "disconnect", source: ref("a"), outcome: { name: "success" } }])).toEqual([kindChange]);
  });

  it("keeps a set whose operations all apply exactly as it is", () => {
    const staged: WorkflowChangeOperation[] = [
      ...groupBuild,
      { op: "insert_step_between", source: ref("http_request"), outcome: { name: "success" }, step: { step_id: "note", step_kind: "set_variable" } },
      { op: "disconnect", source: ref("if"), outcome: { name: "true" } },
    ];
    expect(reconcileChangeOperations(empty, staged)).toEqual(staged);
  });
});

describe("a disabled step", () => {
  it("carries the flag and the tool source into the canvas graph and keeps it through a kind change", async () => {
    const { stepAfterKindChange, stepCanBeDisabled } = await import("@/pages/workflows/builder/change-set");
    const definition = definitionFromWorkflow({
      workflow_id: "wf",
      steps: [
        { step_id: "a", step_kind: "agent", display_name: "A", disabled: true },
        { step_id: "tool", step_kind: "connector", display_name: "Tool", connector_id: "connector.mcp.jira" },
      ],
      settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "a" }, edges: [] } },
    });
    const canvas = canvasGraphWithDefinition({ workflow: { id: "wf", display_name: "wf", enabled: false, callable_by_parent: false }, interface_contract: {}, output_contract: {}, steps: [], groups: [], edges: [], validation_issues: [], automation_rules: [] } as WorkflowCanvasGraph, definition);
    expect(canvas.steps.find((step) => step.id === "a")).toMatchObject({ disabled: true });
    expect(canvas.steps.find((step) => step.id === "tool")).toMatchObject({ disabled: false, tool_category: "mcp_tools" });
    expect(stepAfterKindChange(definition.steps[0], { step_kind: "transform" })).toMatchObject({ disabled: true, step_kind: "transform" });
    expect(stepCanBeDisabled({ step_id: "a", step_kind: "agent" })).toBe(true);
    for (const kind of ["if", "switch", "approval", "stop_success", "stop_fail", "break", "continue"]) expect(stepCanBeDisabled({ step_id: "x", step_kind: kind })).toBe(false);
  });

  it("is a local issue when its kind has no single Next outcome", () => {
    const definition = definitionFromWorkflow({
      workflow_id: "wf",
      steps: [{ step_id: "gate", step_kind: "if", display_name: "Gate", disabled: true }],
      settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "gate" }, edges: [] } },
    });
    expect(localDefinitionIssues(definition).map((issue) => issue.code)).toContain("step_disabled_needs_next");
  });
});

describe("moving a step", () => {
  it("joins a place that has an entry as a plain move", () => {
    expect(moveStepOperations(base, "b", { groupId: "loop", forEachId: "", laneId: "" })).toEqual([
      { op: "move_step", step_id: "b", group_id: "loop", for_each_id: null, lane_id: null },
    ]);
  });

  it("starts a place with no entry yet, explicitly, and the preview agrees", () => {
    const emptied = applyChangeOperations(base, [{ op: "move_step", step_id: "inner", group_id: null, lane_id: null }]).definition;
    const operations = moveStepOperations(emptied, "b", { groupId: "loop", forEachId: "", laneId: "" });
    expect(operations).toEqual([
      { op: "move_step", step_id: "b", group_id: "loop", for_each_id: null, lane_id: null },
      { op: "set_entry", target: { kind: "step", id: "b" }, group_id: "loop" },
    ]);
    const { definition, rejected } = applyChangeOperations(emptied, operations);
    expect(rejected).toEqual({});
    expect(definition.groups[0].body).toEqual(expect.objectContaining({ entry: { kind: "step", id: "b" } }));
  });

  it("makes the moved step the workflow entry when the root has none", () => {
    const rootless = applyChangeOperations(base, [{ op: "move_step", step_id: "a", group_id: "loop", lane_id: null }]).definition;
    expect(rootless.graph.entry).toBeNull();
    expect(moveStepOperations(rootless, "inner", { groupId: "", forEachId: "", laneId: "" })).toEqual([
      { op: "move_step", step_id: "inner", group_id: null, for_each_id: null, lane_id: null },
      { op: "set_entry", target: { kind: "step", id: "inner" } },
    ]);
  });
});

describe("foldStagedStepEdits (one change per new step on the wire)", () => {
  it("carries a new step's parameter edits inside its creation, and leaves a saved step's edit alone", () => {
    const create: WorkflowChangeOperation = { op: "create_step", step: { step_id: "n", step_kind: "agent", display_name: "New" } };
    const edit: WorkflowChangeOperation = { op: "update_step_config", step_id: "n", patch: { display_name: "Renamed" } };
    const saved: WorkflowChangeOperation = { op: "update_step_config", step_id: "a", patch: { display_name: "A2" } };
    expect(foldStagedStepEdits([create, saved, edit])).toEqual([
      { op: "create_step", step: { step_id: "n", step_kind: "agent", display_name: "Renamed" } },
      saved,
    ]);
    const deleted: WorkflowChangeOperation = { op: "delete_step", step_id: "n" };
    expect(foldStagedStepEdits([create, deleted, edit])).toEqual([create, deleted, edit]);
  });
});
