import { decodeBuilderClipboard, encodeBuilderSelection, remapBuilderClipboard, type BuilderClipboardV1, type CodecResult } from "./canvas-clipboard";
import type { WorkflowDefinitionModel } from "./change-set";
import type { CanvasPosition } from "./canvas-core";

function value<T>(result: CodecResult<T>): T {
  if (!result.ok) throw new Error(`Unexpected codec refusal: ${result.reason}`);
  return result.value;
}
function deepFreeze<T>(row: T): T {
  if (row && typeof row === "object") {
    Object.freeze(row);
    Object.values(row).forEach(deepFreeze);
  }
  return row;
}
function fixture(): WorkflowDefinitionModel {
  return {
    groups: [{ group_id: "g", display_name: "Group", body: { kind: "sequence", entry: { kind: "for_each", id: "f" } } }],
    forEach: [{ for_each_id: "f", group_id: "g", over: "state.items", item_name: "item", index_name: "idx",
      body: { kind: "lanes", concurrency_limit: 2, lanes: [
        { lane_id: "constructor", entry: { kind: "step", id: "__proto__" } },
        { lane_id: "__proto__", entry: { kind: "step", id: "constructor" } },
      ] },
      boundary_contract: { entry: { inputs: [{ name: "source", type: "json", required: false }] } },
    }],
    steps: [
      { step_id: "__proto__", step_kind: "set_variable", for_each_id: "f", group_id: null, lane_id: "constructor",
        override: { set_variable: { assignments: { "state.raw_id": "__proto__", "state.path": "state.steps.constructor.output" } } },
        schema_data: { type: "object", $defs: JSON.parse('{"__proto__":{"type":"string"},"constructor":{"type":"number"}}'), $ref: "#/$defs/constructor" },
      },
      { step_id: "constructor", step_kind: "set_variable", for_each_id: "f", lane_id: "__proto__" },
      { step_id: "after", step_kind: "set_variable", group_id: "g", lane_id: null },
      { step_id: "external", step_kind: "stop_success" },
    ],
    graph: { schema_version: 1, entry: { kind: "group", id: "g" }, edges: [
      { source: { kind: "step", id: "__proto__" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "f", lane_id: "constructor" } },
      { source: { kind: "step", id: "constructor" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "f", lane_id: "__proto__" } },
      { source: { kind: "for_each", id: "f" }, outcome: { name: "success" }, target: { kind: "step", id: "after" } },
      { source: { kind: "step", id: "after" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "g", lane_id: null } },
      { source: { kind: "group", id: "g" }, outcome: { name: "success" }, target: { kind: "step", id: "external" } },
    ] },
  };
}
function worldPositions(): ReadonlyMap<string, CanvasPosition> {
  return new Map([
    ["g", { x: 64, y: 64 }], ["f", { x: 128, y: 160 }], ["__proto__", { x: 192, y: 224 }],
    ["constructor", { x: 192, y: 384 }], ["after", { x: 480, y: 160 }], ["external", { x: 800, y: 64 }],
  ]);
}
function encoded(definition = fixture()) {
  return value(encodeBuilderSelection({ definition, targets: [{ kind: "group", id: "g" }], worldPositions: worldPositions() }));
}
function payload() { return value(decodeBuilderClipboard(encoded())); }

it("roundtrips a canonical Group/ForEach closure, preserving lane entries/exits and omitting external edges", () => {
  const definition = deepFreeze(fixture());
  const before = JSON.stringify(definition);
  const points = worldPositions();
  const pointEntries = [...points];
  const text = value(encodeBuilderSelection({ definition, targets: [{ kind: "group", id: "g" }, { kind: "step", id: "__proto__" }], worldPositions: points }));
  const copied = value(decodeBuilderClipboard(text));
  expect(copied.format).toBe("flow-steward/builder-selection");
  expect(copied.version).toBe(1);
  expect(copied.roots).toEqual([{ kind: "group", id: "g" }]);
  expect(copied.definition.steps.map((step) => step.step_id)).toEqual(["__proto__", "constructor", "after"]);
  expect(copied.definition.forEach).toEqual(definition.forEach);
  expect(copied.definition.groups).toEqual(definition.groups);
  expect(copied.definition.graph.entry).toEqual({ kind: "group", id: "g" });
  expect(copied.definition.graph.edges).toEqual(definition.graph.edges.slice(0, 4));
  expect(copied.relativeWorldPositions.g).toEqual({ x: 0, y: 0 });
  expect(copied.relativeWorldPositions["__proto__"]).toEqual({ x: 128, y: 160 });
  expect(copied.relativeWorldPositions.constructor).toEqual({ x: 128, y: 320 });
  expect(Object.hasOwn(copied.relativeWorldPositions, "__proto__")).toBe(true);
  expect(Object.getPrototypeOf(copied.relativeWorldPositions)).toBeNull();
  expect(Object.hasOwn(copied.relativeWorldPositions, "external")).toBe(false);
  expect(JSON.stringify(definition)).toBe(before);
  expect([...points]).toEqual(pointEntries);
});

it("allocates globally collision-free IDs, remaps every structural owner/ref and leaves both collections and opaque data unchanged", () => {
  const copied = deepFreeze(payload());
  const sourceBefore = JSON.stringify(copied);
  const destination = deepFreeze<WorkflowDefinitionModel>({
    steps: [{ step_id: "g_2", step_kind: "stop_success" }, { step_id: "proto", step_kind: "set_variable" }],
    groups: [{ group_id: "constructor_2", body: { kind: "sequence", entry: null } }], forEach: [],
    graph: { schema_version: 1, entry: { kind: "step", id: "g_2" }, edges: [] },
  });
  const destinationBefore = JSON.stringify(destination);
  const pasted = value(remapBuilderClipboard(copied, destination));
  expect(pasted.idMap.get("g")).toBe("g_3");
  expect(pasted.idMap.get("__proto__")).toBe("proto_2");
  expect(pasted.idMap.get("constructor")).toBe("constructor_3");
  expect(pasted.roots).toEqual([{ kind: "group", id: "g_3" }]);
  expect(pasted.definition.graph.entry).toEqual({ kind: "group", id: "g_3" });
  expect(pasted.definition.groups[0].body).toEqual({ kind: "sequence", entry: { kind: "for_each", id: "f_2" } });
  expect(pasted.definition.forEach[0]).toEqual({ ...copied.definition.forEach[0], for_each_id: "f_2", group_id: "g_3",
    body: { kind: "lanes", concurrency_limit: 2, lanes: [
      { lane_id: "constructor", entry: { kind: "step", id: "proto_2" } },
      { lane_id: "__proto__", entry: { kind: "step", id: "constructor_3" } },
    ] },
  });
  expect(pasted.definition.steps[0]).toEqual({ ...copied.definition.steps[0], step_id: "proto_2", for_each_id: "f_2" });
  expect(pasted.definition.steps[2]).toEqual({ ...copied.definition.steps[2], step_id: "after_2", group_id: "g_3" });
  expect(pasted.definition.graph.edges).toEqual([
    { source: { kind: "step", id: "proto_2" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "f_2", lane_id: "constructor" } },
    { source: { kind: "step", id: "constructor_3" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "f_2", lane_id: "__proto__" } },
    { source: { kind: "for_each", id: "f_2" }, outcome: { name: "success" }, target: { kind: "step", id: "after_2" } },
    { source: { kind: "step", id: "after_2" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "g_3", lane_id: null } },
  ]);
  expect(pasted.relativeWorldPositions.proto_2).toEqual({ x: 128, y: 160 });
  expect(Object.getPrototypeOf(pasted.relativeWorldPositions)).toBeNull();
  expect(new Set(pasted.idMap.values()).size).toBe(5);
  expect(JSON.stringify(copied)).toBe(sourceBefore);
  expect(JSON.stringify(destination)).toBe(destinationBefore);
  pasted.definition.steps[0].display_name = "Independent edit";
  expect(copied.definition.steps[0].display_name).toBeUndefined();
});

it("detaches a selected ordinary child from its external container without copying an external exit or inventing an entry", () => {
  const definition = fixture();
  const copied = value(decodeBuilderClipboard(value(encodeBuilderSelection({ definition,
    targets: [{ kind: "step", id: "__proto__" }], worldPositions: worldPositions() }))));
  expect(copied.roots).toEqual([{ kind: "step", id: "__proto__" }]);
  expect(copied.definition.groups).toEqual([]);
  expect(copied.definition.forEach).toEqual([]);
  expect(copied.definition.steps).toEqual([{ ...definition.steps[0], for_each_id: null, lane_id: null }]);
  expect(copied.definition.graph).toEqual({ schema_version: 1, entry: null, edges: [] });
  expect(copied.relativeWorldPositions["__proto__"]).toEqual({ x: 0, y: 0 });
});

const malformed: Array<[string, (row: BuilderClipboardV1) => void]> = [
  ["global ID collision", (row) => { row.definition.steps[0].step_id = "g"; }],
  ["missing geometry own key", (row) => { delete row.relativeWorldPositions["__proto__"]; }],
  ["dangling body entry", (row) => { row.definition.groups[0].body = { kind: "sequence", entry: { kind: "step", id: "external" } }; }],
  ["foreign container kind", (row) => { row.definition.steps[0].for_each_id = "g"; }],
  ["two immediate owners", (row) => { row.definition.steps[0].group_id = "g"; }],
  ["missing explicit exit lane", (row) => { Reflect.deleteProperty(row.definition.graph.edges[0].target, "lane_id"); }],
  ["unknown edge target", (row) => { row.definition.graph.edges[0].target = { kind: "step", id: "external" }; }],
  ["incomplete node reference", (row) => { Reflect.deleteProperty(row.definition.graph.edges[2].source, "kind"); }],
];
it.each(malformed)("refuses strict decode for %s rather than repairing or dropping a malformed fragment", (_label, damage) => {
  const row = payload();
  damage(row);
  expect(decodeBuilderClipboard(JSON.stringify(row))).toEqual({ ok: false, reason: "invalid_fragment" });
});

it.each(["not JSON", JSON.stringify({ format: "other", version: 1 }), JSON.stringify({ format: "flow-steward/builder-selection", version: 2 })])("refuses foreign or unsupported clipboard text %s", (text) => {
  expect(decodeBuilderClipboard(text)).toEqual({ ok: false, reason: "unsupported_format" });
});

it.each(["hook:before_finish", "hook:before_finish:member", "group:hook:before_finish"])("explicitly refuses projected Hook id %s", (nodeId) => {
  const definition: WorkflowDefinitionModel = { steps: [{ step_id: nodeId, step_kind: "set_variable" }], groups: [], forEach: [], graph: { schema_version: 1, entry: null, edges: [] } };
  expect(encodeBuilderSelection({ definition, targets: [{ kind: "step", id: nodeId }], worldPositions: new Map([[nodeId, { x: 0, y: 0 }]]) }))
    .toEqual({ ok: false, reason: "unrepresentable_selection" });
});

it("explicitly refuses a canonical Hook declaration without claiming a copied hook binding", () => {
  const definition: WorkflowDefinitionModel = { steps: [{ step_id: "decl", step_kind: "invoke_workflow", override: { invoke_workflow: {
    hook: { hook_id: "before_finish", input_schema: { type: "object", properties: {} } },
  } } }], groups: [], forEach: [], graph: { schema_version: 1, entry: null, edges: [] } };
  expect(encodeBuilderSelection({ definition, targets: [{ kind: "step", id: "decl" }], worldPositions: new Map([["decl", { x: 0, y: 0 }]]) }))
    .toEqual({ ok: false, reason: "unrepresentable_selection" });
  const row = payload();
  row.definition.steps[0].step_kind = "invoke_workflow";
  row.definition.steps[0].override = definition.steps[0].override;
  expect(decodeBuilderClipboard(JSON.stringify(row))).toEqual({ ok: false, reason: "unrepresentable_selection" });
});

it("preserves backend-valid non-workflow-regex node IDs and ordinary direct Invoke business configuration", () => {
  const definition: WorkflowDefinitionModel = { steps: [{ step_id: "123 / local", step_kind: "invoke_workflow", override: { invoke_workflow: {
    workflow_id: "child_workflow", input_mapping: { city: "state.inputs.city" }, output_mapping: { summary: "state.steps.original.output" },
  } } }], groups: [], forEach: [], graph: { schema_version: 1, entry: null, edges: [] } };
  const row = value(decodeBuilderClipboard(value(encodeBuilderSelection({ definition, targets: [{ kind: "step", id: "123 / local" }],
    worldPositions: new Map([["123 / local", { x: 0, y: 0 }]]) }))));
  expect(row.definition.steps[0].step_id).toBe("123 / local");
  const pasted = value(remapBuilderClipboard(row, { steps: [], groups: [], forEach: [], graph: { schema_version: 1, entry: null, edges: [] } }));
  expect(pasted.definition.steps[0].override).toEqual(definition.steps[0].override);
});

it.each([undefined, { x: Infinity, y: 0 }])("refuses missing or non-finite geometry without inventing positions (%s)", (badPoint) => {
  const points = new Map(worldPositions());
  if (badPoint) points.set("__proto__", badPoint);
  else points.delete("__proto__");
  expect(encodeBuilderSelection({ definition: fixture(), targets: [{ kind: "group", id: "g" }], worldPositions: points }))
    .toEqual({ ok: false, reason: "invalid_fragment" });
});

it("retains explicitly incomplete body-entry presence instead of manufacturing a runnable body", () => {
  const definition = fixture();
  definition.groups[0].body = { kind: "sequence", entry: null };
  const row = value(decodeBuilderClipboard(encoded(definition)));
  expect(row.definition.groups[0].body).toEqual({ kind: "sequence", entry: null });
  const mapped = value(remapBuilderClipboard(row, { steps: [], groups: [], forEach: [], graph: { schema_version: 1, entry: null, edges: [] } }));
  expect(mapped.definition.groups[0].body).toEqual({ kind: "sequence", entry: null });
});

type CopiedKind = "step" | "group" | "for_each";
function alphaPayload(kind: CopiedKind) {
  const definition: WorkflowDefinitionModel = { steps: [], groups: [], forEach: [], graph: { schema_version: 1, entry: { kind, id: "alpha" }, edges: [] } };
  if (kind === "step") definition.steps.push({ step_id: "alpha", step_kind: "set_variable" });
  else {
    if (kind === "group") {
      definition.groups.push({ group_id: "alpha", body: { kind: "sequence", entry: { kind: "step", id: "leaf" } } });
      definition.steps.push({ step_id: "leaf", step_kind: "set_variable", group_id: "alpha" });
    } else {
      definition.forEach.push({ for_each_id: "alpha", over: "state.items", item_name: "item", body: { kind: "sequence", entry: { kind: "step", id: "leaf" } } });
      definition.steps.push({ step_id: "leaf", step_kind: "set_variable", for_each_id: "alpha" });
    }
  }
  return value(decodeBuilderClipboard(value(encodeBuilderSelection({ definition, targets: [{ kind, id: "alpha" }],
    worldPositions: new Map([["alpha", { x: 0, y: 0 }], ["leaf", { x: 64, y: 64 }]]) }))));
}
function destinationDraft(): WorkflowDefinitionModel {
  return { steps: [{ step_id: "keeper", step_kind: "set_variable" }], groups: [], forEach: [],
    graph: { schema_version: 1, entry: { kind: "step", id: "keeper" }, edges: [] } };
}
const danglingReferences: Array<{ label: string; copiedKind: CopiedKind; prepare: (draft: WorkflowDefinitionModel) => void }> = [
  { label: "graph entry", copiedKind: "step", prepare: (draft) => { draft.graph.entry = { kind: "step", id: "alpha_2" }; } },
  { label: "edge source", copiedKind: "step", prepare: (draft) => { draft.graph.edges.push({ source: { kind: "step", id: "alpha_2" }, outcome: { name: "success" }, target: { kind: "step", id: "keeper" } }); } },
  { label: "edge target", copiedKind: "step", prepare: (draft) => { draft.graph.edges.push({ source: { kind: "step", id: "keeper" }, outcome: { name: "success" }, target: { kind: "step", id: "alpha_2" } }); } },
  { label: "Group exit owner", copiedKind: "group", prepare: (draft) => { draft.graph.edges.push({ source: { kind: "step", id: "keeper" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "alpha_2", lane_id: null } }); } },
  { label: "ForEach exit owner", copiedKind: "for_each", prepare: (draft) => { draft.graph.edges.push({ source: { kind: "step", id: "keeper" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "alpha_2", lane_id: null } }); } },
  { label: "Group body entry", copiedKind: "step", prepare: (draft) => { draft.groups.push({ group_id: "shell", body: { kind: "sequence", entry: { kind: "step", id: "alpha_2" } } }); } },
  { label: "ForEach lane entry", copiedKind: "step", prepare: (draft) => { draft.forEach.push({ for_each_id: "shell", over: "state.items", item_name: "item", body: { kind: "lanes", lanes: [{ lane_id: "one", entry: { kind: "step", id: "alpha_2" } }] } }); } },
  { label: "Step Group placement", copiedKind: "group", prepare: (draft) => { draft.steps[0].group_id = "alpha_2"; } },
  { label: "Step ForEach placement", copiedKind: "for_each", prepare: (draft) => { draft.steps[0].for_each_id = "alpha_2"; } },
  { label: "ForEach parent Group", copiedKind: "group", prepare: (draft) => { draft.forEach.push({ for_each_id: "shell", group_id: "alpha_2", over: "state.items", item_name: "item", body: { kind: "sequence", entry: null } }); } },
];
it.each(danglingReferences)("does not allocate a pasted node into the destination's dangling $label", ({ copiedKind, prepare }) => {
  const draft = destinationDraft();
  prepare(draft);
  const copied = deepFreeze(alphaPayload(copiedKind));
  const sourceBefore = JSON.stringify(copied);
  const draftBefore = JSON.stringify(draft);
  deepFreeze(draft);
  const pasted = value(remapBuilderClipboard(copied, draft));
  expect(pasted.idMap.get("alpha")).toBe("alpha_3");
  expect([...pasted.idMap.values()]).not.toContain("alpha_2");
  expect(pasted.roots).toEqual([{ kind: copiedKind, id: "alpha_3" }]);
  expect([...pasted.definition.steps.map((row) => row.step_id), ...pasted.definition.groups.map((row) => row.group_id), ...pasted.definition.forEach.map((row) => row.for_each_id)])
    .not.toContain("alpha_2");
  // The old draft still names its missing ID; this operation neither repairs it
  // nor creates a node that can accidentally become its target/owner.
  expect(JSON.stringify(draft)).toBe(draftBefore);
  expect(JSON.stringify(copied)).toBe(sourceBefore);
});

it("does not reserve opaque business strings or lane IDs as though they were structural node refs", () => {
  const draft = destinationDraft();
  draft.steps[0].override = { set_variable: { assignments: { "state.literal": "alpha_2" } } };
  draft.groups.push({ group_id: "shell", body: { kind: "lanes", lanes: [{ lane_id: "alpha_2", entry: null }] } });
  const before = JSON.stringify(draft);
  const pasted = value(remapBuilderClipboard(alphaPayload("step"), draft));
  expect(pasted.idMap.get("alpha")).toBe("alpha_2");
  expect(JSON.stringify(draft)).toBe(before);
});

it("fails closed for an unrepresentable destination structural ref instead of guessing a node ID", () => {
  const draft = destinationDraft();
  Reflect.set(draft.graph.entry!, "kind", "future_unknown_kind");
  const before = JSON.stringify(draft);
  expect(remapBuilderClipboard(alphaPayload("step"), draft)).toEqual({ ok: false, reason: "invalid_fragment" });
  expect(JSON.stringify(draft)).toBe(before);
});
