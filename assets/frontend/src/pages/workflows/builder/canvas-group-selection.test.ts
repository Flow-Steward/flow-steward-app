import { applyChangeOperations, definitionFromWorkflow } from "./change-set";
import { planGroupBodyChange, planSelectionGroup, selectionGroupRefusal, selectionLanesRefusal } from "./canvas-group-selection";

// Decode → Classify → Priority? (True → Fulfil, False → Review → Route); Review also fails to Reject.
const definition = definitionFromWorkflow({
  workflow_id: "orders",
  steps: [
    { step_id: "decode", step_kind: "agent", display_name: "Decode invoice" },
    { step_id: "classify", step_kind: "agent", display_name: "Classify request" },
    { step_id: "priority", step_kind: "if", display_name: "Priority order?" },
    { step_id: "review", step_kind: "agent", display_name: "Review order", error_policy: { on_error: "route_to_step" } },
    { step_id: "route", step_kind: "agent", display_name: "Route by decision" },
    { step_id: "fulfil", step_kind: "agent", display_name: "Fulfilment" },
    { step_id: "reject", step_kind: "stop_fail", display_name: "Reject order" },
    { step_id: "member", step_kind: "agent", display_name: "Member", group_id: "existing" },
  ],
  groups: [{ group_id: "existing", display_name: "Existing", body: { kind: "sequence", entry: { kind: "step", id: "member" } } }],
  settings: {
    execution_graph: {
      schema_version: 1,
      entry: { kind: "step", id: "decode" },
      edges: [
        { source: { kind: "step", id: "decode" }, outcome: { name: "success" }, target: { kind: "step", id: "classify" } },
        { source: { kind: "step", id: "classify" }, outcome: { name: "success" }, target: { kind: "step", id: "priority" } },
        { source: { kind: "step", id: "priority" }, outcome: { name: "true" }, target: { kind: "step", id: "fulfil" } },
        { source: { kind: "step", id: "priority" }, outcome: { name: "false" }, target: { kind: "step", id: "review" } },
        { source: { kind: "step", id: "review" }, outcome: { name: "success" }, target: { kind: "step", id: "route" } },
        { source: { kind: "step", id: "review" }, outcome: { name: "error" }, target: { kind: "step", id: "reject" } },
        { source: { kind: "step", id: "route" }, outcome: { name: "success" }, target: { kind: "group", id: "existing" } },
        { source: { kind: "step", id: "member" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "existing", lane_id: null } },
      ],
    },
  },
});

describe("Group… wraps the selected steps", () => {
  it("refuses groups and steps already in one: groups don't nest", () => {
    const nest = "Groups don't nest: select steps, or a For each that is not in a group.";
    expect(selectionGroupRefusal(definition, ["classify", "group:existing"])).toBe(nest);
    expect(selectionGroupRefusal(definition, ["member"])).toBe(nest);
    expect(planSelectionGroup(definition, ["for_each:x"], "sequence")).toEqual({ ok: false, reason: "A selected step is no longer in the workflow." });
    expect(selectionGroupRefusal(definition, ["classify", "priority"])).toBe("");
  });

  it("Group wraps the steps into a plain Group: its inner connections stay, outer ones lead to the group", () => {
    const plan = planSelectionGroup(definition, ["classify", "priority", "review"], "sequence");
    if (!plan.ok) throw new Error(plan.reason);
    expect(plan.nodeKind).toBe("group");
    expect(plan.summary.movesIn).toEqual(["Classify request", "Priority order?", "Review order"]);
    expect(plan.summary.leadsIn).toEqual(["Decode invoice · Next"]);
    expect(plan.summary.ends.map((end) => end.label)).toEqual(["Priority order? · True", "Review order · Next"]);
    expect(plan.summary.dropped).toEqual(["Review order · On error → Reject order"]);
    expect(plan.summary.lanes).toEqual([]);

    const { definition: next, rejected } = applyChangeOperations(definition, plan.operations);
    expect(rejected).toEqual({});
    expect(next.forEach).toEqual([]);
    const group = next.groups.find((item) => item.group_id === plan.containerId)!;
    expect(group).toMatchObject({ display_name: "Group", body: { kind: "sequence", entry: { kind: "step", id: "classify" } } });
    for (const id of ["classify", "priority", "review"]) expect(next.steps.find((step) => step.step_id === id)).toMatchObject({ group_id: plan.containerId, for_each_id: null, lane_id: null });
    const edges = next.graph.edges;
    // The connections between the selected steps are kept as they were.
    expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "classify" }, outcome: { name: "success" }, target: { kind: "step", id: "priority" } }));
    expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "priority" }, outcome: { name: "false" }, target: { kind: "step", id: "review" } }));
    expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "decode" }, target: { kind: "group", id: plan.containerId } }));
    expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "priority" }, outcome: { name: "true" }, target: { kind: "group_exit", group_id: plan.containerId, lane_id: null } }));
    // The error route cannot cross out of the body: the step falls back to failing.
    expect(edges.some((edge) => edge.source.id === "review" && edge.outcome.name === "error")).toBe(false);
    expect(next.steps.find((step) => step.step_id === "review")?.error_policy).toMatchObject({ on_error: "fail" });
    // The group goes on to the most used target; a tie goes to the furthest step.
    expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "group", id: plan.containerId }, outcome: { name: "success" }, target: { kind: "step", id: "route" } }));
    expect(plan.summary.thenGoesTo).toBe("Route by decision");
    expect(plan.summary.ends.find((end) => end.label === "Priority order? · True")?.formerTarget).toBe("Fulfilment");
    expect(next.graph.entry).toEqual({ kind: "step", id: "decode" });
  });

  it("makes each connected chain a parallel lane and moves the workflow entry to the group", () => {
    const plan = planSelectionGroup(definition, ["decode", "fulfil"], "lanes");
    if (!plan.ok) throw new Error(plan.reason);
    expect(plan.summary.lanes).toEqual([{ laneId: "lane_a", steps: ["Decode invoice"] }, { laneId: "lane_b", steps: ["Fulfilment"] }]);
    const { definition: next, rejected } = applyChangeOperations(definition, plan.operations);
    expect(rejected).toEqual({});
    const group = next.groups.find((item) => item.group_id === plan.containerId)!;
    expect(group.body).toMatchObject({ kind: "lanes", lanes: [{ lane_id: "lane_a", entry: { kind: "step", id: "decode" } }, { lane_id: "lane_b", entry: { kind: "step", id: "fulfil" } }] });
    expect(next.steps.find((step) => step.step_id === "fulfil")).toMatchObject({ group_id: plan.containerId, lane_id: "lane_b" });
    expect(next.graph.entry).toEqual({ kind: "group", id: plan.containerId });
    expect(next.graph.edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "decode" }, target: { kind: "group_exit", group_id: plan.containerId, lane_id: "lane_a" } }));
  });

  it("switches a Group to Parallel lanes and back, keeping its steps and their connections", () => {
    const grouped = planSelectionGroup(definition, ["decode", "classify", "fulfil"], "sequence");
    if (!grouped.ok) throw new Error(grouped.reason);
    const sequence = applyChangeOperations(definition, grouped.operations).definition;
    const toLanes = planGroupBodyChange(sequence, grouped.containerId, { kind: "lanes" });
    if (!toLanes.ok) throw new Error(toLanes.reason);
    expect(toLanes.lanes).toEqual(["lane_a", "lane_b"]);
    const lanes = applyChangeOperations(sequence, toLanes.operations);
    expect(lanes.rejected).toEqual({});
    const laneGroup = lanes.definition.groups.find((item) => item.group_id === grouped.containerId)!;
    expect(laneGroup.body).toMatchObject({ kind: "lanes", lanes: [{ lane_id: "lane_a", entry: { kind: "step", id: "decode" } }, { lane_id: "lane_b", entry: { kind: "step", id: "fulfil" } }] });
    expect(lanes.definition.steps.find((step) => step.step_id === "classify")).toMatchObject({ lane_id: "lane_a" });
    expect(lanes.definition.graph.edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "decode" }, target: { kind: "step", id: "classify" } }));

    const back = planGroupBodyChange(lanes.definition, grouped.containerId, { kind: "sequence" });
    if (!back.ok) throw new Error(back.reason);
    const again = applyChangeOperations(lanes.definition, back.operations);
    expect(again.rejected).toEqual({});
    const sequenceGroup = again.definition.groups.find((item) => item.group_id === grouped.containerId)!;
    expect(sequenceGroup.body).toMatchObject({ kind: "sequence", entry: { kind: "step", id: "decode" } });
    expect(again.definition.steps.filter((step) => step.group_id === grouped.containerId).every((step) => !step.lane_id)).toBe(true);
    // One after another: lane A's end leads into lane B, whose end closes the group.
    expect(again.definition.graph.edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "decode" }, target: { kind: "step", id: "classify" } }));
    expect(again.definition.graph.edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "classify" }, target: { kind: "step", id: "fulfil" } }));
    // Lane A's end was Classify request · Next; it now leads into lane B instead of the exit.
    expect(again.definition.graph.edges.some((edge) => edge.target.kind === "group_exit" && edge.target.group_id === grouped.containerId)).toBe(false);
  });

  it("keeps a For each in a Group whose steps run one after another", () => {
    const withLoop = definitionFromWorkflow({
      workflow_id: "loop",
      steps: [{ step_id: "each", step_kind: "agent", for_each_id: "items" }],
      groups: [{ group_id: "prep", display_name: "Prep", body: { kind: "sequence", entry: { kind: "for_each", id: "items" } } }],
      for_each: [{ for_each_id: "items", group_id: "prep", over: "state.items", item_name: "item", body: { kind: "sequence", entry: { kind: "step", id: "each" } } }],
      settings: { execution_graph: { schema_version: 1, entry: { kind: "group", id: "prep" }, edges: [] } },
    });
    expect(planGroupBodyChange(withLoop, "prep", { kind: "lanes" })).toEqual({ ok: false, reason: "A For each stays in a group whose steps run one after another. Move it out first." });
  });
});

const NEST_REFUSAL = "Groups don't nest: select steps, or a For each that is not in a group.";

// prepare → Each record (For each over state.items: fetch → store) → publish; Each record · On error → alert.
const loopWorkflow = definitionFromWorkflow({
  workflow_id: "loops",
  steps: [
    { step_id: "prepare", step_kind: "agent", display_name: "Prepare" },
    { step_id: "fetch", step_kind: "agent", display_name: "Fetch record", for_each_id: "records" },
    { step_id: "store", step_kind: "agent", display_name: "Store record", for_each_id: "records" },
    { step_id: "publish", step_kind: "agent", display_name: "Publish" },
    { step_id: "alert", step_kind: "agent", display_name: "Alert" },
    { step_id: "inner", step_kind: "agent", display_name: "Inner", for_each_id: "nested" },
  ],
  groups: [{ group_id: "held", display_name: "Held", body: { kind: "sequence", entry: { kind: "for_each", id: "nested" } } }],
  for_each: [
    { for_each_id: "records", display_name: "Each record", over: "state.items", item_name: "record", error_policy: { on_error: "route_to_step" }, body: { kind: "sequence", entry: { kind: "step", id: "fetch" } } },
    { for_each_id: "nested", display_name: "Nested", group_id: "held", over: "state.rows", item_name: "row", body: { kind: "sequence", entry: { kind: "step", id: "inner" } } },
  ],
  settings: {
    execution_graph: {
      schema_version: 1,
      entry: { kind: "step", id: "prepare" },
      edges: [
        { source: { kind: "step", id: "prepare" }, outcome: { name: "success" }, target: { kind: "for_each", id: "records" } },
        { source: { kind: "step", id: "fetch" }, outcome: { name: "success" }, target: { kind: "step", id: "store" } },
        { source: { kind: "step", id: "store" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "records", lane_id: null } },
        { source: { kind: "for_each", id: "records" }, outcome: { name: "success" }, target: { kind: "step", id: "publish" } },
        { source: { kind: "for_each", id: "records" }, outcome: { name: "error" }, target: { kind: "step", id: "alert" } },
        { source: { kind: "for_each", id: "nested" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "held", lane_id: null } },
        { source: { kind: "step", id: "inner" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "nested", lane_id: null } },
      ],
    },
  },
});

describe("Group… takes a For each", () => {
  it("wraps a For each alone: its body goes along, the connections around it follow", () => {
    expect(selectionGroupRefusal(loopWorkflow, ["for_each:records"])).toBe("");
    const plan = planSelectionGroup(loopWorkflow, ["for_each:records"], "sequence");
    if (!plan.ok) throw new Error(plan.reason);
    expect(plan.summary.movesIn).toEqual(["Each record"]);
    expect(plan.summary.leadsIn).toEqual(["Prepare · Next"]);
    expect(plan.summary.thenGoesTo).toBe("Publish");
    expect(plan.summary.dropped).toEqual(["Each record · error → Alert"]);
    const { definition: next, rejected } = applyChangeOperations(loopWorkflow, plan.operations);
    expect(rejected).toEqual({});
    const group = next.groups.find((item) => item.group_id === plan.containerId)!;
    expect(group.body).toMatchObject({ kind: "sequence", entry: { kind: "for_each", id: "records" } });
    expect(next.forEach.find((item) => item.for_each_id === "records")).toMatchObject({ group_id: plan.containerId, error_policy: { on_error: "fail" }, body: { entry: { kind: "step", id: "fetch" } } });
    // The body stays in the For each: no step moved to the group.
    for (const id of ["fetch", "store"]) expect(next.steps.find((step) => step.step_id === id)).toMatchObject({ for_each_id: "records" });
    const edges = next.graph.edges;
    expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "prepare" }, target: { kind: "group", id: plan.containerId } }));
    expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "for_each", id: "records" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: plan.containerId, lane_id: null } }));
    expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "group", id: plan.containerId }, outcome: { name: "success" }, target: { kind: "step", id: "publish" } }));
    expect(edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "store" }, target: { kind: "for_each_exit", for_each_id: "records", lane_id: null } }));
    expect(edges.some((edge) => edge.source.kind === "for_each" && edge.source.id === "records" && edge.outcome.name === "error")).toBe(false);
    expect(next.graph.entry).toEqual({ kind: "step", id: "prepare" });
  });

  it("wraps a For each together with the steps around it, keeping their connection", () => {
    const plan = planSelectionGroup(loopWorkflow, ["prepare", "for_each:records", "publish"], "sequence");
    if (!plan.ok) throw new Error(plan.reason);
    expect(plan.summary.movesIn).toEqual(["Prepare", "Each record", "Publish"]);
    const { definition: next, rejected } = applyChangeOperations(loopWorkflow, plan.operations);
    expect(rejected).toEqual({});
    const group = next.groups.find((item) => item.group_id === plan.containerId)!;
    expect(group.body).toMatchObject({ kind: "sequence", entry: { kind: "step", id: "prepare" } });
    expect(next.steps.filter((step) => step.group_id === plan.containerId).map((step) => step.step_id).sort()).toEqual(["prepare", "publish"]);
    expect(next.forEach.find((item) => item.for_each_id === "records")?.group_id).toBe(plan.containerId);
    expect(next.graph.edges).toContainEqual(expect.objectContaining({ source: { kind: "step", id: "prepare" }, target: { kind: "for_each", id: "records" } }));
    expect(next.graph.edges).toContainEqual(expect.objectContaining({ source: { kind: "for_each", id: "records" }, outcome: { name: "success" }, target: { kind: "step", id: "publish" } }));
    // The workflow starts at the group now.
    expect(next.graph.entry).toEqual({ kind: "group", id: plan.containerId });
  });

  it("makes the For each the group's entry, and the group the workflow's, when it started there", () => {
    const starts = definitionFromWorkflow({
      workflow_id: "starts",
      steps: [{ step_id: "fetch", step_kind: "agent", for_each_id: "records" }],
      for_each: [{ for_each_id: "records", over: "state.items", item_name: "record", body: { kind: "sequence", entry: { kind: "step", id: "fetch" } } }],
      settings: { execution_graph: { schema_version: 1, entry: { kind: "for_each", id: "records" }, edges: [{ source: { kind: "step", id: "fetch" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "records", lane_id: null } }] } },
    });
    const plan = planSelectionGroup(starts, ["for_each:records"], "sequence");
    if (!plan.ok) throw new Error(plan.reason);
    const { definition: next, rejected } = applyChangeOperations(starts, plan.operations);
    expect(rejected).toEqual({});
    expect(next.groups[0].body).toMatchObject({ kind: "sequence", entry: { kind: "for_each", id: "records" } });
    expect(next.graph.entry).toEqual({ kind: "group", id: plan.containerId });
  });

  it("takes the For each's own steps as part of it when both are selected", () => {
    const plan = planSelectionGroup(loopWorkflow, ["for_each:records", "fetch"], "sequence");
    if (!plan.ok) throw new Error(plan.reason);
    expect(plan.summary.movesIn).toEqual(["Each record"]);
    expect(plan.operations.some((operation) => operation.op === "move_step")).toBe(false);
    // A step of a For each alone still stays in it.
    expect(selectionGroupRefusal(loopWorkflow, ["fetch"])).toBe(NEST_REFUSAL);
  });

  it("refuses a For each already in a group, and gives Parallel lanes the reason it cannot hold one", () => {
    expect(selectionGroupRefusal(loopWorkflow, ["for_each:nested"])).toBe(NEST_REFUSAL);
    const lanes = "A For each stays in a group whose steps run one after another. Move it out first.";
    expect(selectionLanesRefusal(["for_each:records"])).toBe(lanes);
    expect(selectionLanesRefusal(["prepare"])).toBe("");
    expect(planSelectionGroup(loopWorkflow, ["prepare", "for_each:records"], "lanes")).toEqual({ ok: false, reason: lanes });
  });
});
