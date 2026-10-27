import * as actions from "./detail-step-actions";
import { applyChangeOperations, canvasGraphWithDefinition, definitionFromWorkflow, localDefinitionIssues } from "@/pages/workflows/builder/change-set";
import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import type { WorkflowCanvasGraph, WorkflowDetail, WorkflowGraphEdge, WorkflowStep } from "@/pages/workflows/shared/api";

type BuildRequest = { preview: ReturnType<typeof definitionFromWorkflow>; choice: { catalogKey: string; stepKind: string; displayName: string; agentId?: string; blockKey?: string; override?: Record<string, unknown> }; context: { groupId?: string; forEachId?: string; laneId?: string; insert?: { source: { kind: "step" | "group" | "for_each"; id: string }; outcome: { name: string; case_id?: string }; label: string; append?: boolean } }; expectedEdge?: WorkflowGraphEdge; createAsEntry?: boolean };
type BuildResult = { step: WorkflowStep; operations: WorkflowChangeOperation[]; placement: { groupId: string; forEachId: string; laneId: string } };
// Existing action module remains importable before extraction: missing behavior
// fails an output assertion, rather than an unresolved future-module import.
const build = (next: BuildRequest) => (actions as unknown as { buildNewStepOperations?: (request: BuildRequest) => BuildResult }).buildNewStepOperations?.(next);
const choice = { catalogKey: "builtin:wait", stepKind: "wait", displayName: "Wait", override: { wait: { duration_ms: 60000 } } };
const definition = (partial: Partial<WorkflowDetail> = {}) => definitionFromWorkflow({ workflow_id: "draft", revision: 4, steps: [{ step_id: "seed", step_kind: "set_variable" }], ...partial });

type ContainerRequest = { preview: ReturnType<typeof definitionFromWorkflow>; choice: { nodeKind: "group" | "for_each"; catalogKey: string; displayName: string; containerDefaults?: { bodyKind?: "sequence" | "lanes"; parentGroupId?: string } }; context: BuildRequest["context"]; expectedEdge?: WorkflowGraphEdge; createAsEntry?: boolean };
type ContainerResult = { nodeKind: "group" | "for_each"; nodeId: string; operations: WorkflowChangeOperation[]; container: Record<string, unknown> };
const buildContainer = (next: ContainerRequest) => (actions as unknown as { buildNewContainerOperations?: (request: ContainerRequest) => ContainerResult }).buildNewContainerOperations?.(next);

it("honors explicit root entry for both genuine container kinds in the same canonical batch", () => {
  const preview = definition({ settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "seed" }, edges: [] } } });
  const kinds = ["group", "for_each"] as const;
  // Execute both branches before the first assertion so fail-fast retains evidence for both.
  const results = kinds.map((nodeKind) => buildContainer({ preview, choice: { nodeKind, catalogKey: `container:${nodeKind}`, displayName: nodeKind === "group" ? "Group" : "For each" }, context: {}, createAsEntry: true }));
  expect(results.map((result) => result?.operations)).toEqual([
    [{ op: "create_group", group: expect.objectContaining({ group_id: "group" }) }, { op: "set_entry", target: { kind: "group", id: "group" } }],
    [{ op: "create_for_each", for_each: expect.objectContaining({ for_each_id: "for_each" }) }, { op: "set_entry", target: { kind: "for_each", id: "for_each" } }],
  ]);
  expect(results.map((result) => result && applyChangeOperations(preview, result.operations).definition.graph.entry)).toEqual([{ kind: "group", id: "group" }, { kind: "for_each", id: "for_each" }]);
  const defaults = kinds.map((nodeKind) => buildContainer({ preview, choice: { nodeKind, catalogKey: `container:${nodeKind}`, displayName: nodeKind }, context: {} }));
  expect(defaults.map((result) => result && applyChangeOperations(preview, result.operations).definition.graph.entry)).toEqual([preview.graph.entry, preview.graph.entry]);
  expect(defaults.map((result) => result?.operations.some((operation) => operation.op === "set_entry"))).toEqual([false, false]);
});

it("retains the sequence Group owner on an explicit nested For each entry", () => {
  const preview = definition({ groups: [{ group_id: "once", body: { kind: "sequence", entry: { kind: "step", id: "seed" } } }], steps: [{ step_id: "seed", step_kind: "wait", group_id: "once" }] });
  const result = buildContainer({ preview, choice: { nodeKind: "for_each", catalogKey: "container:for_each", displayName: "For each" }, context: { groupId: "once" }, createAsEntry: true });
  expect(result?.operations).toEqual([{ op: "create_for_each", for_each: expect.objectContaining({ group_id: "once" }) }, { op: "set_entry", group_id: "once", target: { kind: "for_each", id: "for_each" } }]);
  expect(result && applyChangeOperations(preview, result.operations).definition.groups[0].body).toMatchObject({ entry: { kind: "for_each", id: "for_each" } });
});

it("creates a genuine once-only Group with a create_group operation rather than casting it as a step", () => {
  const result = buildContainer({ preview: definition(), choice: { nodeKind: "group", catalogKey: "container:group", displayName: "Group" }, context: {} });
  expect(result).toMatchObject({ nodeKind: "group", nodeId: "group", container: { group_id: "group", display_name: "Group", body: { kind: "sequence" } } });
  expect(result?.operations).toEqual([{ op: "create_group", group: result?.container }]);
  expect(result?.container).not.toHaveProperty("loop");
  const applied = result ? applyChangeOperations(definition(), result.operations) : undefined;
  expect(applied?.definition.groups).toHaveLength(1);
  expect(applied?.definition.steps).toHaveLength(1);
});

it("stages a blank-source For each as an editable local container with honest required readiness", () => {
  const preview = definition();
  const result = buildContainer({ preview, choice: { nodeKind: "for_each", catalogKey: "container:for_each", displayName: "For each" }, context: {} });
  expect(result).toMatchObject({ nodeKind: "for_each", container: { for_each_id: "for_each", group_id: null, over: "", item_name: "item", index_name: null, body: { kind: "sequence" } } });
  const applied = result ? applyChangeOperations(preview, result.operations) : undefined;
  expect(applied?.definition.forEach).toHaveLength(1);
  expect(applied && localDefinitionIssues(applied.definition)).toEqual(expect.arrayContaining([expect.objectContaining({ code: "for_each_source_missing", forEachId: "for_each" })]));
  const base: WorkflowCanvasGraph = { workflow: { id: "draft", display_name: "Draft", enabled: false, callable_by_parent: false }, interface_contract: {}, output_contract: {}, entry: null, steps: [], groups: [], edges: [], validation_issues: [], automation_rules: [] };
  expect(applied && canvasGraphWithDefinition(base, applied.definition).for_each).toEqual(expect.arrayContaining([expect.objectContaining({ id: "for_each", over: "", item_name: "item" })]));
});

it("appends a For each in its source's direct sequence Group in one create/connect batch", () => {
  const preview = definition({ groups: [{ group_id: "once", body: { kind: "sequence" } }], steps: [{ step_id: "seed", step_kind: "set_variable", group_id: "once" }] });
  const result = buildContainer({ preview, choice: { nodeKind: "for_each", catalogKey: "container:for_each", displayName: "For each" }, context: { groupId: "once", insert: { source: { kind: "step", id: "seed" }, outcome: { name: "success" }, label: "Next", append: true } } });
  expect(result?.operations).toEqual([{ op: "create_for_each", for_each: expect.objectContaining({ group_id: "once", over: "", body: { kind: "sequence" } }) }, { op: "connect", edge: { source: { kind: "step", id: "seed" }, outcome: { name: "success" }, target: { kind: "for_each", id: "for_each" } } }]);
  expect(result && applyChangeOperations(preview, result.operations).definition.graph.edges).toEqual([{ source: { kind: "step", id: "seed" }, outcome: { name: "success" }, target: { kind: "for_each", id: "for_each" } }]);
});

it("reuses canonical independent lanes defaults and chooses an identity unique across all node families", () => {
  const preview = definition({ steps: [{ step_id: "workers", step_kind: "wait" }], groups: [{ group_id: "workers_2", body: { kind: "sequence" } }] });
  const result = buildContainer({ preview, choice: { nodeKind: "for_each", catalogKey: "container:for_each:lanes", displayName: "Workers", containerDefaults: { bodyKind: "lanes" } }, context: {} });
  expect(result).toMatchObject({ nodeId: "workers_3", container: { for_each_id: "workers_3", body: { kind: "lanes", lanes: [{ lane_id: "lane_a" }, { lane_id: "lane_b" }] } } });
  expect(result?.container).not.toHaveProperty("body.concurrency_limit");
});

it("refuses nested Group, For each-inside-For each, lane containers, and occupied insertion without partial creation", () => {
  const preview = definition({ groups: [{ group_id: "once", body: { kind: "sequence" } }, { group_id: "lanes", body: { kind: "lanes", lanes: [{ lane_id: "a" }] } }], for_each: [{ for_each_id: "each", over: "state.items", item_name: "item", body: { kind: "sequence" } }] });
  expect(() => buildContainer({ preview, choice: { nodeKind: "group", catalogKey: "container:group", displayName: "Group" }, context: { groupId: "once" } })).toThrow();
  for (const context of [{ forEachId: "each" }, { groupId: "lanes", laneId: "a" }]) expect(() => buildContainer({ preview, choice: { nodeKind: "for_each", catalogKey: "container:for_each", displayName: "For each" }, context })).toThrow();
  const edge: WorkflowGraphEdge = { source: { kind: "step", id: "seed" }, outcome: { name: "success" }, target: { kind: "group", id: "once" } };
  const occupied = { ...preview, graph: { ...preview.graph, edges: [edge] } };
  expect(() => buildContainer({ preview: occupied, choice: { nodeKind: "group", catalogKey: "container:group", displayName: "Group" }, context: { insert: { source: edge.source, outcome: edge.outcome, label: "occupied" } }, expectedEdge: edge })).toThrow();
  expect(preview.groups).toHaveLength(2);
  expect(preview.forEach).toHaveLength(1);
});

it("builds the actual chosen kind with canonical defaults as an incomplete local draft", () => {
  const result = build({ preview: definition(), choice, context: {} });
  expect(result).toMatchObject({ step: { step_id: "wait", step_kind: "wait", display_name: "Wait", override: { wait: { duration_ms: 60000 } } }, placement: { groupId: "", forEachId: "", laneId: "" } });
  expect(result?.operations).toEqual([{ op: "create_step", step: result?.step }]);
});

it("keeps a free keyed outcome and the source's actual lane in one create/connect batch", () => {
  const preview = definition({ groups: [{ group_id: "workers", body: { kind: "lanes", lanes: [{ lane_id: "a" }, { lane_id: "b" }] } }], steps: [{ step_id: "seed", step_kind: "switch", group_id: "workers", lane_id: "a", override: { switch: { selector: "state.mode", cases: [{ case_id: "email", when: "email" }] } } }] });
  const result = build({ preview, choice, context: { insert: { source: { kind: "step", id: "seed" }, outcome: { name: "case", case_id: "email" }, label: "Email", append: true } } });
  expect(result?.placement).toEqual({ groupId: "workers", forEachId: "", laneId: "a" });
  expect(result?.operations).toEqual([{ op: "create_step", step: expect.objectContaining({ group_id: "workers", for_each_id: null, lane_id: "a" }) }, { op: "connect", edge: { source: { kind: "step", id: "seed" }, outcome: { name: "case", case_id: "email" }, target: { kind: "step", id: "wait" } } }]);
});

it("inserts one ordinary Next atomically while retaining the exact old outcome and target", () => {
  const edge: WorkflowGraphEdge = { source: { kind: "step", id: "seed" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } };
  const preview = definition({ steps: [{ step_id: "seed", step_kind: "set_variable" }, { step_id: "done", step_kind: "stop_success" }], settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "seed" }, edges: [edge] } } });
  const result = build({ preview, choice, context: { insert: { source: edge.source, outcome: edge.outcome, label: "seed to done" } }, expectedEdge: edge });
  expect(result?.operations).toEqual([{ op: "insert_step_between", source: edge.source, outcome: edge.outcome, step: expect.objectContaining({ step_kind: "wait" }) }]);
  const applied = result ? applyChangeOperations(preview, result.operations) : undefined;
  expect(applied?.definition.graph.edges).toEqual([{ ...edge, target: { kind: "step", id: "wait" } }, { source: { kind: "step", id: "wait" }, outcome: { name: "success" }, target: { kind: "step", id: "done" }, rework: false }]);
  expect(preview.graph.edges).toEqual([edge]);
});

it.each(["if", "switch", "approval", "stop_success", "stop_fail", "break", "continue"])("refuses insertion of %s before constructing local operations", (stepKind) => {
  const edge: WorkflowGraphEdge = { source: { kind: "step", id: "seed" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } };
  const preview = definition({ steps: [{ step_id: "seed", step_kind: "set_variable" }, { step_id: "done", step_kind: "stop_success" }], settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "seed" }, edges: [edge] } } });
  expect(() => build({ preview, choice: { ...choice, stepKind, override: {} }, context: { insert: { source: edge.source, outcome: edge.outcome, label: "seed to done" } }, expectedEdge: edge })).toThrow();
  expect(preview.steps).toHaveLength(2);
});

it("refuses a changed occupied edge and a missing source instead of silently replacing topology", () => {
  const preview = definition();
  const expectedEdge: WorkflowGraphEdge = { source: { kind: "step", id: "seed" }, outcome: { name: "success" }, target: { kind: "step", id: "removed" } };
  expect(() => build({ preview, choice, context: { insert: { source: expectedEdge.source, outcome: expectedEdge.outcome, label: "stale" } }, expectedEdge })).toThrow();
  expect(() => build({ preview, choice, context: { insert: { source: { kind: "step", id: "missing" }, outcome: { name: "success" }, label: "missing", append: true } } })).toThrow();
});

it.each(["collect", "break", "continue"])("offers %s only directly inside a sequence For each", (stepKind) => {
  const preview = definition({ for_each: [{ for_each_id: "each", over: "state.items", item_name: "item", index_name: "index", body: { kind: "sequence" } }] });
  const direct = build({ preview, choice: { ...choice, stepKind, override: {} }, context: { forEachId: "each" } });
  expect(direct?.step).toMatchObject({ step_kind: stepKind, group_id: null, for_each_id: "each", lane_id: null });
  expect(() => build({ preview, choice: { ...choice, stepKind, override: {} }, context: {} })).toThrow();
  const lanes = definition({ for_each: [{ for_each_id: "each", over: "state.items", item_name: "item", body: { kind: "lanes", lanes: [{ lane_id: "a" }] } }] });
  expect(() => build({ preview: lanes, choice: { ...choice, stepKind, override: {} }, context: { forEachId: "each", laneId: "a" } })).toThrow();
});

it("keeps ordinary kinds legal in For each lanes and permits the For each's direct sequence Group parent", () => {
  const preview = definition({ groups: [{ group_id: "once", body: { kind: "sequence" } }], for_each: [{ for_each_id: "each", group_id: "once", over: "state.items", item_name: "item", index_name: "index", body: { kind: "lanes", concurrency_limit: 2, lanes: [{ lane_id: "a" }] } }] });
  const result = build({ preview, choice, context: { forEachId: "each", laneId: "a" } });
  expect(result?.step).toMatchObject({ step_kind: "wait", group_id: null, for_each_id: "each", lane_id: "a" });
  expect(preview.forEach[0]).toMatchObject({ group_id: "once", item_name: "item", index_name: "index", body: { concurrency_limit: 2 } });
  expect(() => build({ preview, choice, context: { groupId: "once", forEachId: "each" } })).toThrow();
});

it("preserves catalog source/action/agent/block identity and strips platform-owned connector inputs without inventing a connection", () => {
  const connector = build({ preview: definition(), choice: { catalogKey: "extension:mail:send", stepKind: "connector", displayName: "Send mail", override: { connector: { connector_id: "extension.mail", connector_install_id: "install-mail", operation_id: "send", input_mapping: { subject: "Hello", connection_ref: "must-not-persist" }, output_mapping: { message: "state.message" }, connection_binding: {} } } }, context: {} });
  expect(connector?.step).toMatchObject({ step_kind: "connector", connector_id: "extension.mail", connector_install_id: "install-mail", input_mapping_jsonb: { subject: "Hello" }, connection_binding_jsonb: {}, override: { connector: { operation_id: "send" } } });
  const agent = build({ preview: definition(), choice: { catalogKey: "agent:writer", stepKind: "agent", displayName: "Writer", agentId: "writer", blockKey: "compose" }, context: {} });
  expect(agent?.step).toMatchObject({ step_kind: "agent", agent_id: "writer", block_key: "compose" });
  expect(connector?.step).not.toHaveProperty("connection_binding_jsonb.connection_id");
});


it.each([
  { config: { input_mapping_jsonb: { query: "literal:imported" } }, expected: { query: "literal:imported" } },
  { config: { input_mapping: {}, input_mapping_jsonb: { query: "literal:old" } }, expected: {} },
  { config: { input_mapping: { query: "state.query", filter: null, note: "", connection_ref: "literal:internal" } }, expected: { query: "state.query", filter: null, note: "" } },
])("uses the same canonical Connector input for the column and saved override", ({ config, expected }) => {
  const override = { connector: { connector_id: "extension.fixture.mail", connector_install_id: "install-mail", operation_id: "send_message", output_mapping: { result: "state.result" }, connection_binding: { connection_id: "mail-1" }, ...config } };
  const patch = actions.stepConfigPatch({ stepKindDraft: "connector", stepDisplayName: "Mapping step", stepOptional: false, selectedStep: { raw: {} } }, override);
  expect(patch.input_mapping_jsonb).toEqual(expected);
  expect(patch.override.connector).toEqual({ operation_id: "send_message", output_mapping: { result: "state.result" }, connection_binding: { connection_id: "mail-1" }, input_mapping: expected });
  expect(patch).toMatchObject({ connector_id: "extension.fixture.mail", connector_install_id: "install-mail", output_mapping_jsonb: { result: "state.result" }, connection_binding_jsonb: { connection_id: "mail-1" }, display_name: "Mapping step", optional: false });
  expect(JSON.stringify(patch)).not.toContain("retainedValue");
});


it("migrates an input alias for a new local Connector through the same canonical serializer", () => {
  const result = build({ preview: definition(), choice: { catalogKey: "extension:fixture:send", stepKind: "connector", displayName: "Mapping connector", override: { connector: { connector_id: "extension.fixture.mail", connector_install_id: "install-mail", operation_id: "send_message", input_mapping_jsonb: { query: "literal:imported", filter: null, note: "", connection_ref: "literal:internal" } } } }, context: {} });
  expect(result?.step).toMatchObject({ connector_id: "extension.fixture.mail", connector_install_id: "install-mail", input_mapping_jsonb: { query: "literal:imported", filter: null, note: "" }, override: { connector: { operation_id: "send_message", input_mapping: { query: "literal:imported", filter: null, note: "" } } } });
  expect(result?.step.override).not.toHaveProperty("connector.input_mapping_jsonb");
});

it("starts an empty For each, Group or lane with the step added there, and leaves a filled one alone", () => {
  // Placing, grouping and moving steps: a place with no entry yet starts with what is added there.
  const preview = definition({
    for_each: [{ for_each_id: "each", over: "state.items", item_name: "item", body: { kind: "sequence" } }],
    groups: [{ group_id: "lanes", body: { kind: "lanes", lanes: [{ lane_id: "a" }, { lane_id: "b", entry: { kind: "step", id: "seed" } }] } }],
  } as Partial<WorkflowDetail>);
  const empty = build({ preview, choice, context: { forEachId: "each" } });
  expect(empty?.operations).toContainEqual({ op: "set_entry", target: { kind: "step", id: "wait" }, for_each_id: "each" });
  expect(empty && applyChangeOperations(preview, empty.operations).definition.forEach[0].body).toMatchObject({ entry: { kind: "step", id: "wait" } });
  const lane = build({ preview, choice, context: { groupId: "lanes", laneId: "a" } });
  expect(lane?.operations).toContainEqual({ op: "set_entry", target: { kind: "step", id: "wait" }, group_id: "lanes", lane_id: "a" });
  // The only step of a body also ends it: Next goes on to the next item, or ends the lane.
  expect(empty?.operations).toContainEqual({ op: "connect", edge: { source: { kind: "step", id: "wait" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "each", lane_id: null } } });
  expect(empty && applyChangeOperations(preview, empty.operations).definition.graph.edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "wait" }, target: { kind: "for_each_exit", for_each_id: "each", lane_id: null } }));
  expect(lane?.operations).toContainEqual({ op: "connect", edge: { source: { kind: "step", id: "wait" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "lanes", lane_id: "a" } } });
  const filled = build({ preview, choice, context: { groupId: "lanes", laneId: "b" } });
  expect(filled?.operations.some((operation) => operation.op === "set_entry" || operation.op === "connect")).toBe(false);
});

it("puts a step added at the bottom of a group or hook last, before the end the body already reaches (R3-6)", () => {
  const exit = { kind: "group_exit", group_id: "once", lane_id: null };
  const group = definitionFromWorkflow({
    workflow_id: "draft", revision: 4,
    settings: { execution_graph: { schema_version: 1, entry: { kind: "group", id: "once" }, edges: [{ source: { kind: "step", id: "seed" }, outcome: { name: "success" }, target: exit }] } },
    groups: [{ group_id: "once", body: { kind: "sequence", entry: { kind: "step", id: "seed" } } }],
    steps: [{ step_id: "seed", step_kind: "set_variable", group_id: "once" }],
  } as never);
  const inGroup = build({ preview: group, choice, context: { groupId: "once" } });
  expect(inGroup?.operations).toEqual([{ op: "insert_step_between", source: { kind: "step", id: "seed" }, outcome: { name: "success" }, step: expect.objectContaining({ step_id: "wait", group_id: "once" }) }]);
  const edges = inGroup ? applyChangeOperations(group, inGroup.operations).definition.graph.edges : [];
  expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "seed" }, target: { kind: "step", id: "wait" } }));
  expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "wait" }, target: exit }));

  // A hook's storage workflow ends in its hidden end step instead of an exit.
  const hook = definitionFromWorkflow({
    workflow_id: "storage", revision: 2,
    settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "adjust" }, edges: [{ source: { kind: "step", id: "adjust" }, outcome: { name: "success" }, target: { kind: "step", id: "hook_end" } }] } },
    steps: [{ step_id: "adjust", step_kind: "set_variable" }, { step_id: "hook_end", step_kind: "stop_success" }],
  } as never);
  expect(build({ preview: hook, choice, context: {} })?.operations).toEqual([{ op: "insert_step_between", source: { kind: "step", id: "adjust" }, outcome: { name: "success" }, step: expect.objectContaining({ step_id: "wait" }) }]);
  // A hook made before the hidden end (Build the result / Return the result / done) ends in its one finishing step.
  const legacy = definitionFromWorkflow({
    workflow_id: "storage", revision: 3,
    settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "build_result" }, edges: [
      { source: { kind: "step", id: "build_result" }, outcome: { name: "success" }, target: { kind: "step", id: "return_result" } },
      { source: { kind: "step", id: "return_result" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
    ] } },
    steps: [{ step_id: "build_result", step_kind: "transform" }, { step_id: "return_result", step_kind: "set_variable" }, { step_id: "done", step_kind: "stop_success" }],
  } as never);
  expect(build({ preview: legacy, choice, context: {}, hookBody: true } as BuildRequest)?.operations).toEqual([{ op: "insert_step_between", source: { kind: "step", id: "return_result" }, outcome: { name: "success" }, step: expect.objectContaining({ step_id: "wait" }) }]);
  // Outside a hook the same finishing step is no end to slot before.
  expect(build({ preview: legacy, choice, context: {} })?.operations).toEqual([{ op: "create_step", step: expect.objectContaining({ step_id: "wait" }) }]);
  // An ordinary workflow's root has no end to reach: the step stays free, as before.
  expect(build({ preview: definition(), choice, context: {} })?.operations).toEqual([{ op: "create_step", step: expect.objectContaining({ step_id: "wait" }) }]);
});
