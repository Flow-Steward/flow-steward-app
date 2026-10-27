import { declaredSignatures, schemaSignatures, orderedStepPorts } from "./canvas-card-presentation";
import { applyCanvasNodeState } from "./canvas-state-helpers";
import { stepVisual } from "./canvas-core";
import { buildCanvasLayout, type StartNodeData, type ForEachNodeData } from "./layout";
import type { WorkflowCanvasGraph, WorkflowCanvasPort } from "../shared/api";

it("projects only declared signature facts, preserving optionality and excluding values", () => {
  expect(declaredSignatures([{ name: "api_token", type: "secret", required: true, default: "PRIVATE", value: "PRIVATE" }, { name: "receipt" }, { type: "string" }])).toEqual([{ name: "api_token", type: "secret", required: true }, { name: "receipt" }]);
  expect(schemaSignatures({ properties: { order_id: { type: "string", default: "PRIVATE" }, priority: { type: ["string", "null"] } }, required: ["order_id"] })).toEqual([{ name: "order_id", type: "string", required: true }, { name: "priority", type: "string | null", required: false }]);
});
it("orders named outcomes without changing the original ports", () => {
  const ports: WorkflowCanvasPort[] = [{ outcome: { name: "false" }, label: "False" }, { outcome: { name: "true" }, label: "True" }, { outcome: { name: "error" }, label: "On error" }];
  expect(orderedStepPorts("if", ports).map((port) => port.label)).toEqual(["True", "False"]);
  expect(ports.map((port) => port.label)).toEqual(["False", "True", "On error"]);
});
it.each([
  ["agent", "action"], ["connector", "action"], ["http_request", "action"], ["invoke_workflow", "action"],
  ["dataset_query", "data"], ["set_variable", "data"], ["transform", "data"], ["tabular_feed", "data"], ["file_from_base64", "data"], ["template", "data"], ["collect", "data"],
  ["if", "neutral"], ["switch", "neutral"], ["approval", "neutral"], ["wait", "neutral"], ["batch", "neutral"], ["break", "neutral"], ["continue", "neutral"], ["stop_success", "success"], ["stop_fail", "danger"],
])("keeps %s in its published family with a neutral card", (kind, family) => {
  const visual = stepVisual(kind);
  expect(visual.Icon).toBeDefined();
  if (family === "action" || family === "data") expect(visual.iconClass).toContain(`--fs-workflow-family-${family}-tile`);
  else if (family !== "neutral") expect(visual.iconClass).toContain(`--fs-status-${family}`);
  else expect(visual.iconClass).toContain("--fs-bg-surface-muted");
});
it("keeps the Start edge drawn-only and puts declared inputs beside the actual entry", () => {
  const canvasGraph: WorkflowCanvasGraph = { workflow: { id: "w", display_name: "Workflow", enabled: true, callable_by_parent: false }, interface_contract: { expected_inputs: [{ name: "order_id", type: "string", required: true }] }, output_contract: {}, entry: { kind: "step", id: "work" }, steps: [{ id: "work", kind: "agent", display_name: "Work", config_summary: "agent: operator" }], groups: [], edges: [], validation_issues: [], automation_rules: [] };
  const result = buildCanvasLayout({ canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const start = result.nodes.find((node) => node.id === "visual:start")!;
  const work = result.nodes.find((node) => node.id === "work")!;
  expect(start.position.y).toBe(work.position.y);
  expect(start.position.x + start.width!).toBeLessThan(work.position.x);
  expect((start.data as StartNodeData).inputSignatures).toEqual([{ name: "order_id", type: "string", required: true }]);
  expect(result.edges.find((edge) => edge.id === "visual:entry")).toMatchObject({ selectable: false, deletable: false });
});

it("marks a missing entry once at Start without calling every step unreachable", () => {
  const canvasGraph: WorkflowCanvasGraph = { workflow: { id: "w", display_name: "Workflow", enabled: false, callable_by_parent: false }, interface_contract: {}, output_contract: {}, entry: null, steps: [{ id: "work", kind: "agent", display_name: "Work", config_summary: "agent: operator" }], groups: [], edges: [], validation_issues: [{ scope: "workflow", scope_id: "w", severity: "error", title: "The workflow has no entry" }], automation_rules: [] };
  const result = buildCanvasLayout({ canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  expect(result.nodes.find((node) => node.id === "visual:start")?.data).toMatchObject({ hasEntry: false });
  expect(result.nodes.find((node) => node.id === "work")?.data).toMatchObject({ unreachable: false });
  expect(canvasGraph.validation_issues).toHaveLength(1);
  expect(result.edges.find((edge) => edge.id === "visual:entry")).toBeUndefined();
});

it("frames the actual independent For each lanes without making Group iterative", () => {
  const canvasGraph: WorkflowCanvasGraph = { workflow: { id: "w", display_name: "Workflow", enabled: false, callable_by_parent: false }, interface_contract: {}, output_contract: {}, entry: { kind: "group", id: "once" }, groups: [{ id: "once", display_name: "Runs once", body: { kind: "sequence", entry: { kind: "for_each", id: "items" } } }], for_each: [{ id: "items", group_id: "once", display_name: "Each item", over: "state.items", item_name: "item", body: { kind: "lanes", concurrency_limit: 2, lanes: [{ lane_id: "left", entry: { kind: "step", id: "work" } }] } }], steps: [{ id: "work", kind: "agent", display_name: "Work", config_summary: "agent: operator", for_each_id: "items", lane_id: "left" }], edges: [], validation_issues: [], automation_rules: [] };
  const result = buildCanvasLayout({ canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const group = result.nodes.find((node) => node.id === "group:once")!;
  const each = result.nodes.find((node) => node.id === "for_each:items")!;
  const work = result.nodes.find((node) => node.id === "work")!;
  expect(group.data).toMatchObject({ group: { body: { kind: "sequence" } } });
  expect(group.data).not.toHaveProperty("loop");
  expect(each).toMatchObject({ parentId: "group:once", type: "forEachContainer", data: { forEach: { over: "state.items", item_name: "item", body: { concurrency_limit: 2 } } } });
  expect(work.parentId).toBe("for_each:items");
  const frames = (each.data as ForEachNodeData).laneFrames;
  expect(frames).toEqual([{ id: "left", x: work.position.x - 6, y: work.position.y - 6, width: work.width! + 12, height: work.height! + 56, addControl: { x: work.position.x, y: work.position.y + work.height! + 12, width: work.width!, height: 28 } }]);
});

it.each(["step", "group", "for_each"] as const)("keeps Start beside a manually moved root %s entry", (kind) => {
  const canvasGraph: WorkflowCanvasGraph = { workflow: { id: "w", display_name: "Workflow", enabled: false, callable_by_parent: false }, interface_contract: {}, output_contract: {}, entry: { kind, id: "work" }, steps: kind === "step" ? [{ id: "work", kind: "agent", display_name: "Work", config_summary: "agent: operator" }] : [], groups: kind === "group" ? [{ id: "work", display_name: "Work", body: { kind: "sequence", entry: null } }] : [], for_each: kind === "for_each" ? [{ id: "work", display_name: "Work", over: "state.items", item_name: "item", body: { kind: "sequence", entry: null } }] : [], edges: [], validation_issues: [], automation_rules: [] };
  const result = buildCanvasLayout({ canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const id = kind === "step" ? "work" : `${kind}:work`;
  const moved = applyCanvasNodeState({ nodes: result.nodes, manualPositions: { [id]: { x: 900, y: 500 } }, connectionHelpActive: false, highlightedNodeId: null });
  expect(moved.find((node) => node.id === id)?.position).toEqual({ x: 900, y: 500 });
  expect(moved.find((node) => node.id === "visual:start")?.position).toEqual({ x: 516, y: 500 });
});
it.each([null, { kind: "step" as const, id: "missing" }])("keeps Start stable when its entry is absent from actual nodes: %s", (entry) => {
  const canvasGraph: WorkflowCanvasGraph = { workflow: { id: "w", display_name: "Workflow", enabled: false, callable_by_parent: false }, interface_contract: {}, output_contract: {}, entry, steps: [], groups: [], edges: [], validation_issues: [], automation_rules: [] };
  const result = buildCanvasLayout({ canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const moved = applyCanvasNodeState({ nodes: result.nodes, manualPositions: { missing: { x: 900, y: 500 } }, connectionHelpActive: false, highlightedNodeId: null });
  expect(moved.find((node) => node.id === "visual:start")?.position).toEqual({ x: 64, y: 64 });
});


it("reserves a contained Add step slot below each actual parallel lane's members", () => {
  const canvasGraph: WorkflowCanvasGraph = { workflow: { id: "w", display_name: "Workflow", enabled: false, callable_by_parent: false }, interface_contract: {}, output_contract: {}, entry: { kind: "for_each", id: "items" }, groups: [], for_each: [{ id: "items", display_name: "Each item", over: "state.items", item_name: "item", body: { kind: "lanes", lanes: [{ lane_id: "left", entry: { kind: "step", id: "left" } }, { lane_id: "right", entry: { kind: "step", id: "right" } }] } }], steps: ["left", "right"].map((id) => ({ id, kind: "agent", display_name: id, config_summary: "agent: operator", for_each_id: "items", lane_id: id })), edges: [], validation_issues: [], automation_rules: [] };
  const result = buildCanvasLayout({ canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const each = result.nodes.find((node) => node.id === "for_each:items")!;
  const frames = (each.data as ForEachNodeData).laneFrames!;
  for (const [index, frame] of frames.entries()) {
    const member = result.nodes.find((node) => node.id === frame.id)!;
    expect(frame).toHaveProperty("addControl");
    const control = frame.addControl!;
    expect(control.y).toBeGreaterThanOrEqual(member.position.y + member.height! + 12);
    expect(control.x).toBeGreaterThanOrEqual(frame.x);
    expect(control.x + control.width).toBeLessThanOrEqual(frame.x + frame.width);
    expect(control.y + control.height).toBeLessThanOrEqual(frame.y + frame.height);
    expect(frame.y + frame.height).toBeLessThanOrEqual(each.height!);
    if (frames[index + 1]) expect(frames[index + 1].y).toBeGreaterThan(frame.y + frame.height);
  }
});
