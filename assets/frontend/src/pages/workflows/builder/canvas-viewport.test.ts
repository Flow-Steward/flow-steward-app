import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { placeCanvasNode } from "./canvas-placement";
import { Position, getNodesBounds, type Edge, type Node } from "@xyflow/react";
import { canvasNextStepSlots, canvasPastePane, canvasPasteRectsVisible, canvasPasteTranslationToBounds, canvasPasteViewport, fitCanvasToNodes, positionAtViewportCentre, positionRightOfManualSource, type CanvasNextStepSlot } from "./canvas-viewport";
import { nodeSize, type CanvasPosition } from "@/pages/workflows/builder/canvas-core";
import { applyCanvasNodeState } from "@/pages/workflows/builder/canvas-state-helpers";
import { buildCanvasLayout } from "@/pages/workflows/builder/layout";
import { COLUMN_GAP } from "@/pages/workflows/builder/layout-constants";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";

it("fits the library-calculated bounds of expanded cards and notes without measured metadata", async () => {
  const nodes: Node[] = [
    { id: "visual:start", position: { x: 80, y: 156 }, width: 280, height: 230, data: {} },
    { id: "end", position: { x: 424, y: 422 }, width: 220, height: 360, data: {} },
    { id: "annotation:note", position: { x: 900, y: 850 }, width: 280, height: 220, data: {} },
    { id: "hidden", hidden: true, position: { x: 10000, y: 10000 }, width: 100, height: 100, data: {} },
  ];
  const instance = {
    getNodes: vi.fn(() => nodes),
    getNodesBounds: vi.fn(getNodesBounds),
    fitBounds: vi.fn(async () => true),
  };
  expect(await fitCanvasToNodes(instance, 180)).toBe(true);
  expect(instance.getNodesBounds).toHaveBeenCalledWith(nodes.slice(0, 3));
  expect(instance.fitBounds).toHaveBeenCalledWith({x:80,y:156,width:1100,height:914}, {duration:180,padding:0.12});
  expect(nodes.every(node => node.measured === undefined)).toBe(true);
});

it("leaves an empty canvas viewport unchanged", async () => {
  const instance = {getNodes:vi.fn(() => []),getNodesBounds:vi.fn(),fitBounds:vi.fn(async () => true)};
  expect(await fitCanvasToNodes(instance, 220)).toBe(false);
  expect(instance.getNodesBounds).not.toHaveBeenCalled();
  expect(instance.fitBounds).not.toHaveBeenCalled();
});

it("keeps the auto-layout animation and delegates zoom limits to the existing viewport", async () => {
  const node: Node = {id:"entry",position:{x:30,y:20},width:248,height:200,data:{}};
  const instance = {getNodes:vi.fn(() => [node]),getNodesBounds:vi.fn(() => ({x:30,y:20,width:248,height:200})),fitBounds:vi.fn(async () => true)};
  await fitCanvasToNodes(instance, 220);
  expect(instance.fitBounds).toHaveBeenCalledWith({x:30,y:20,width:248,height:200}, {duration:220,padding:0.12});
});

it("uses measured dimensions when React Flow has newer card geometry", async () => {
  const node: Node = {id:"end",position:{x:10,y:20},width:220,height:94,measured:{width:220,height:360},data:{}};
  const instance = {getNodes:vi.fn(() => [node]),getNodesBounds:vi.fn(getNodesBounds),fitBounds:vi.fn(async () => true)};
  await fitCanvasToNodes(instance, 180);
  expect(instance.fitBounds).toHaveBeenCalledWith({x:10,y:20,width:220,height:360}, {duration:180,padding:0.12});
});

it("places a root node at the centre of the visible canvas", () => {
  const toFlow = vi.fn((point: { x: number; y: number }) => ({ x: point.x * 2 - 100, y: point.y * 2 - 50 }));
  const bounds = { left: 100, top: 50, width: 800, height: 600 };
  expect(positionAtViewportCentre({ width: 240, height: 120 }, bounds, toFlow)).toEqual({ x: 780, y: 590 });
  expect(toFlow).toHaveBeenCalledWith({ x: 500, y: 350 });
});

it("leaves a node in a container, or an unmeasured canvas, to the layout", () => {
  const toFlow = vi.fn(() => ({ x: 0, y: 0 }));
  expect(positionAtViewportCentre({ width: 240, height: 120, parentId: "group:loop" }, { left: 0, top: 0, width: 800, height: 600 }, toFlow)).toBeNull();
  expect(positionAtViewportCentre({ width: 240, height: 120 }, { left: 0, top: 0, width: 0, height: 0 }, toFlow)).toBeNull();
  expect(positionAtViewportCentre({ width: 240, height: 120 }, null, toFlow)).toBeNull();
  expect(toFlow).not.toHaveBeenCalled();
});

// The browser case: a root step placed by hand at the viewport centre, then an If added after it from its "+".
const appendedIf: WorkflowCanvasGraph = {
  workflow: { id: "router", display_name: "Router", enabled: false, callable_by_parent: false },
  interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true },
  output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
  entry: { kind: "step", id: "seed" },
  groups: [],
  steps: [
    { id: "seed", step_id: "seed", kind: "set_variable", display_name: "Seed", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
    {
      id: "check", step_id: "check", kind: "if", display_name: "Check", config_summary: "",
      ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }],
    },
  ],
  edges: [{ source: { kind: "step", id: "seed" }, outcome: { name: "success" }, target: { kind: "step", id: "check" } }],
  validation_issues: [],
  automation_rules: [],
};

function canvasWith(manualPositions: Record<string, CanvasPosition>) {
  const layout = buildCanvasLayout({
    canvasGraph: appendedIf,
    onOpenGroup: vi.fn(),
    onOpenGroupAutomation: vi.fn(),
    onOpenStep: vi.fn(),
    onOpenStepAutomation: vi.fn(),
  });
  const nodes = applyCanvasNodeState({ connectionHelpActive: false, highlightedNodeId: null, manualPositions, nodes: layout.nodes });
  return { edges: layout.edges, nodes, byId: new Map(nodes.map((item) => [item.id, item])) };
}

function box(item: Node) {
  const size = nodeSize(item);
  return { left: item.position.x, top: item.position.y, right: item.position.x + size.width, bottom: item.position.y + size.height };
}

function overlaps(left: Node, right: Node) {
  const a = box(left);
  const b = box(right);
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

it("puts a step connected after a hand-placed step into the next column beside it, not on top of it", () => {
  // Deliberately move the source onto the actual automatic target slot to exercise collision avoidance.
  const targetPosition = canvasWith({}).byId.get("check")!.position;
  const manual = { seed: { ...targetPosition } };
  const before = canvasWith(manual);
  expect(overlaps(before.byId.get("seed")!, before.byId.get("check")!)).toBe(true);

  const seed = before.byId.get("seed")!;
  const placed = positionRightOfManualSource(before.byId.get("check")!, before.edges, before.byId, manual);
  expect(placed).toEqual({ x: manual.seed.x + nodeSize(seed).width + COLUMN_GAP, y: manual.seed.y });

  const after = canvasWith({ ...manual, check: placed! });
  const source = after.byId.get("seed")!;
  const added = after.byId.get("check")!;
  expect(overlaps(source, added)).toBe(false);
  expect(added.position.x).toBeGreaterThanOrEqual(box(source).right);
  expect(added.position.y).toBe(source.position.y);
});

it("leaves a step after an auto-laid source, or after a source in another container, to the layout", () => {
  const auto = canvasWith({});
  const check = auto.byId.get("check")!;
  expect(positionRightOfManualSource(check, auto.edges, auto.byId, {})).toBeNull();
  // The layout already puts it one column right of its source.
  expect(overlaps(auto.byId.get("seed")!, check)).toBe(false);
  expect(check.position.x).toBeGreaterThan(box(auto.byId.get("seed")!).right);

  const manual = { seed: { x: 520, y: 359 } };
  const nested = canvasWith(manual);
  expect(positionRightOfManualSource({ id: "check", parentId: "group:loop" }, nested.edges, nested.byId, manual)).toBeNull();
});

it("finds the nearest collision-free grid position beside a manual source", () => {
  const source: Node = { id: "s", position: { x: 64, y: 64 }, width: 224, height: 144, data: {} };
  const added: Node = { id: "n", position: { x: 0, y: 0 }, width: 224, height: 144, data: {} };
  const blocker: Node = { id: "b", position: { x: 384, y: 64 }, width: 224, height: 144, data: {} };
  const result = positionRightOfManualSource(added, [{ source: "s", target: "n" }], new Map([source, added, blocker].map((n) => [n.id, n])), { s: source.position });
  expect(result).toEqual({ x: 384, y: 208 });
});

it("aligns placement to the actual source outcome handle and keeps lane scope", () => {
  const source: Node = { id: "s", position: { x: 64, y: 64 }, width: 240, height: 208, data: { step: { lane_id: "a" } }, handles: [{ id: "false", type: "source", position: Position.Right, x: 235, y: 175, width: 10, height: 10 }] };
  const target: Node = { id: "n", position: { x: 0, y: 0 }, width: 224, height: 144, data: { step: { lane_id: "a" } }, handles: [{ id: "in", type: "target", position: Position.Left, x: -5, y: 19, width: 10, height: 10 }] };
  const edge = { source: "s", target: "n", sourceHandle: "false", targetHandle: "in" };
  expect(positionRightOfManualSource(target, [edge], new Map([source, target].map((n) => [n.id, n])), { s: source.position })).toEqual({ x: 400, y: 224 });
  expect(positionRightOfManualSource(target, [edge], new Map([source, { ...target, data: { step: { lane_id: "b" } } }].map((n) => [n.id, n])), { s: source.position })).toBeNull();
});


it.each([
  { mode: "initial", zoom: 0.5 },
  { mode: "fit", zoom: 0.25 },
])("keeps Start visible when an oversized graph cannot fit at the $mode minimum", async ({ mode, zoom }) => {
  const nodes: Node[] = [
    { id: "visual:start", position: { x: 80, y: 64 }, width: 224, height: 144, data: {} },
    { id: "last", position: { x: 20000, y: 64 }, width: 224, height: 144, data: {} },
  ];
  const instance = { getNodes: () => nodes, getNodesBounds, fitBounds: vi.fn(async () => true), setViewport: vi.fn(async () => true) };
  await fitCanvasToNodes(instance, 0, { width: 1200, height: 700 }, mode as "initial" | "fit");
  expect(instance.setViewport).toHaveBeenCalledWith({ x: 24 - 80 * zoom, y: 24 - 64 * zoom, zoom }, { duration: 0 });
});

it("fits a short graph using both viewport dimensions instead of a width threshold", async () => {
  const nodes: Node[] = [{ id: "visual:start", position: { x: 80, y: 64 }, width: 500, height: 1600, data: {} }];
  const instance = { getNodes: () => nodes, getNodesBounds, fitBounds: vi.fn(async () => true), setViewport: vi.fn(async () => true) };
  await fitCanvasToNodes(instance, 0, { width: 1200, height: 700 }, "initial");
  expect(instance.setViewport).toHaveBeenCalledWith({ x: -16, y: -8, zoom: 0.5 }, { duration: 0 });
});


function actualSlotTemplate(): Node {
  const layout = buildCanvasLayout({ canvasGraph: { ...appendedIf, entry: { kind: "step", id: "slot-preview" }, steps: [{ id: "slot-preview", step_id: "slot-preview", kind: "wait", display_name: "Add next", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] }], edges: [] }, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  return layout.nodes.find((item) => item.id === "slot-preview")!;
}
const nextSlots = (source: Node, nodes: Node[], edges: readonly Edge[] = []) => canvasNextStepSlots(source, nodes, actualSlotTemplate(), edges);

it("offers slots for every free applicable outcome using actual source handle centers", () => {
  const source: Node = { id: "branch", type: "ifStep", position: { x: 320, y: 160 }, width: 240, height: 206, handles: [{ id: "true", type: "source", position: Position.Right, x: 235, y: 79, width: 10, height: 10 }, { id: "false", type: "source", position: Position.Right, x: 235, y: 139, width: 10, height: 10 }, { id: "error", type: "source", position: Position.Bottom, x: 210, y: 201, width: 10, height: 10 }], data: { freeOutcomes: ["true", "error"], step: { id: "branch", step_id: "branch", kind: "if", ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }, { outcome: { name: "error" }, label: "On error" }] } } };
  const slots = nextSlots(source, [source]);
  expect(slots).toEqual([expect.objectContaining({ outcome: { name: "true" }, label: "True", sourcePoint: { x: 560, y: 244 } }), expect.objectContaining({ outcome: { name: "error" }, label: "On error", sourcePoint: { x: 535, y: 366 } })]);
  expect(slots?.some((slot) => slot.outcome.name === "false")).toBe(false);
  // Preferred port-line placement is resolved through the accepted grid helper.
  // The curve previews the actual four-pixel bend, rather than an off-grid card.
  expect(slots?.[0]).toMatchObject({ preferredPosition: { x: 656, y: 220 }, rect: { x: 656, y: 224 }, inputPoint: { x: 656, y: 248 } });
  const created = actualSlotTemplate();
  const resolved = slots?.[0].rect;
  expect(resolved && placeCanvasNode(created, { x: resolved.x, y: resolved.y }, [source, created])).toEqual({ x: 656, y: 224 });
  const target = created.handles?.find((handle) => handle.type === "target" && handle.id === "in");
  expect(target).toBeDefined();
  expect(resolved && target && resolved.y + target.y + (target.height ?? 0) / 2).toBe(248);
  expect(resolved && { width: resolved.width, height: resolved.height }).toEqual(nodeSize(created));
});

it("keeps a next-column slot beside a manually placed nested source and avoids same-lane cards", () => {
  const parent: Node = { id: "group:workers", type: "group", position: { x: 400, y: 200 }, width: 1200, height: 700, data: {} };
  const source: Node = { id: "branch", parentId: parent.id, type: "ifStep", position: { x: 64, y: 96 }, width: 240, height: 206, handles: [{ id: "true", type: "source", position: Position.Right, x: 235, y: 79, width: 10, height: 10 }], data: { freeOutcomes: ["true"], step: { id: "branch", step_id: "branch", kind: "if", lane_id: "a", ports: [{ outcome: { name: "true" }, label: "True" }] } } };
  const obstacle: Node = { id: "occupied", parentId: parent.id, type: "action", position: { x: 400, y: 144 }, width: 224, height: 124, data: { step: { lane_id: "a" } } };
  const slots = nextSlots(source, [parent, source, obstacle]);
  expect(slots).toHaveLength(1);
  expect(slots?.[0].sourcePoint).toEqual({ x: 704, y: 380 });
  const rect = slots?.[0].rect;
  expect(rect).toEqual(expect.objectContaining({ x: expect.any(Number), y: expect.any(Number), width: expect.any(Number), height: expect.any(Number) }));
  expect(rect && rect.x >= 704).toBe(true);
  expect(rect && rect.x < 1024 && rect.x + rect.width > 800 && rect.y < 468 && rect.y + rect.height > 344).toBe(false);
});


const switchPorts = [
  { outcome: { name: "case" as const, case_id: "high" }, label: "High" },
  { outcome: { name: "case" as const, case_id: "low" }, label: "Low" },
  { outcome: { name: "default" as const }, label: "Default" },
  { outcome: { name: "error" as const }, label: "On error" },
];
const switchOutcomes = ["case:high", "case:low", "default", "error"];

function switchSlotsFixture(freeOutcomes = switchOutcomes, doneY = 64, occupied: Array<{ id: string; x: number; y: number }> = [], connections: WorkflowCanvasGraph["edges"] = []) {
  const graph: WorkflowCanvasGraph = {
    ...appendedIf,
    entry: { kind: "step", id: "priority" },
    steps: [
      { id: "priority", step_id: "priority", kind: "switch", display_name: "Route priority", config_summary: "state.priority (2 cases)", ports: switchPorts },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
      ...occupied.map(({ id }) => ({ id, step_id: id, kind: "set_variable", display_name: id, config_summary: "", ports: [{ outcome: { name: "success" as const }, label: "Next" }] })),
    ],
    edges: connections,
  };
  const layout = buildCanvasLayout({ canvasGraph: graph, measuredStepHeights: { done: 170 }, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  // Native fixture world coordinates: screen coordinates are translated by (8, 306).
  const nodes = layout.nodes.filter((node) => node.id !== "visual:start").map((node) => {
    if (node.id === "priority") return { ...node, position: { x: 768, y: 64 }, data: { ...node.data, freeOutcomes } };
    if (node.id === "done") return { ...node, position: { x: 1136, y: doneY } };
    const placed = occupied.find(({ id }) => id === node.id)!;
    return { ...node, position: { x: placed.x, y: placed.y } };
  });
  return { source: nodes.find((node) => node.id === "priority")!, nodes, edges: layout.edges };
}

function branchSlots(slots: CanvasNextStepSlot[]) {
  return slots.filter(({ outcome }) => outcome.name === "case" || outcome.name === "default");
}

/** Inspect the actual returned cubic, rather than inferring crossings from endpoints.
 * Identical, monotone X controls and strictly ordered Y controls prove disjoint
 * cubic curves: X(t) is shared and increasing; Y(lower,t) > Y(upper,t).
 */
function expectDisjointBranchCurves(slots: CanvasNextStepSlot[]) {
  const controls = slots.map(({ path, sourcePoint, inputPoint }) => {
    expect(path.match(/[A-Za-z]/g)).toEqual(["M", "L", "C", "L"]);
    const numbers = path.match(/-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi)!.map(Number);
    expect(numbers).toHaveLength(12);
    const points = Array.from({ length: 6 }, (_, i) => ({ x: numbers[i * 2], y: numbers[i * 2 + 1] }));
    expect(points[0]).toEqual(sourcePoint);
    expect(points[5]).toEqual({ x: inputPoint.x - 5, y: inputPoint.y });
    points.slice(1).forEach((point, i) => expect(point.x).toBeGreaterThanOrEqual(points[i].x));
    return points;
  });
  controls.slice(1).forEach((points, i) => {
    expect(points.map(({ x }) => x)).toEqual(controls[i].map(({ x }) => x));
    points.forEach((point, j) => expect(point.y).toBeGreaterThan(controls[i][j].y));
  });
}

it.each([64, 256])("keeps all named Switch ghosts and their actual curves ordered with Done at y=%i", (doneY) => {
  const { source, nodes } = switchSlotsFixture(switchOutcomes, doneY);
  const before = nodes.map(({ id, position }) => ({ id, position: { ...position } }));
  const slots = nextSlots(source, nodes);
  const branches = branchSlots(slots);
  expect(slots.map(({ outcome }) => outcome)).toEqual([
    { name: "case", case_id: "high" }, { name: "case", case_id: "low" }, { name: "default" }, { name: "error" },
  ]);
  expect(branches.map(({ sourcePoint }) => sourcePoint)).toEqual([{ x: 1040, y: 196 }, { x: 1040, y: 228 }, { x: 1040, y: 260 }]);
  expect(branches.map(({ rect }) => rect.x)).toEqual([1136, 1136, 1136]);
  branches.slice(1).forEach((slot, i) => expect(slot.rect.y).toBeGreaterThanOrEqual(branches[i].rect.y + branches[i].rect.height));
  const ghosts: Node[] = slots.map(({ rect }, i) => ({ id: `ghost:${i}`, position: { x: rect.x, y: rect.y }, width: rect.width, height: rect.height, data: {} }));
  ghosts.forEach((ghost, i) => {
    nodes.forEach((node) => expect(overlaps(ghost, node)).toBe(false));
    ghosts.slice(i + 1).forEach((later) => expect(overlaps(ghost, later)).toBe(false));
  });
  expectDisjointBranchCurves(branches);
  expect(nodes.map(({ id, position }) => ({ id, position }))).toEqual(before);
});

it.each([
  { free: ["case:low", "default", "error"], outcomes: [{ name: "case", case_id: "low" }, { name: "default" }], occupied: [{ id: "set_variable", x: 1136, y: 112 }] },
  { free: ["case:high", "case:low", "error"], outcomes: [{ name: "case", case_id: "high" }, { name: "case", case_id: "low" }], occupied: [{ id: "set_variable", x: 1136, y: 112 }, { id: "set_variable_2", x: 1136, y: 432 }, { id: "set_variable_3", x: 1360, y: 240 }] },
  { free: ["default", "error"], outcomes: [{ name: "default" }], occupied: [{ id: "set_variable", x: 1136, y: 112 }, { id: "set_variable_2", x: 1136, y: 432 }] },
  { free: ["case:high", "error"], outcomes: [{ name: "case", case_id: "high" }], occupied: [{ id: "set_variable", x: 1136, y: 112 }, { id: "set_variable_2", x: 1136, y: 432 }, { id: "set_variable_3", x: 1360, y: 240 }] },
])("retains the remaining named branch geometry when free handles are $free", ({ free, outcomes, occupied }) => {
  const { source, nodes } = switchSlotsFixture(free, 256, occupied);
  const before = nodes.map(({ id, position }) => ({ id, position: { ...position } }));
  const slots = nextSlots(source, nodes);
  const branches = branchSlots(slots);
  expect(branches.map(({ outcome }) => outcome)).toEqual(outcomes);
  expect(slots.at(-1)?.outcome).toEqual({ name: "error" });
  expect(slots).toHaveLength(outcomes.length + 1);
  branches.forEach((slot, i) => {
    expect(slot.rect.x).toBe(1136);
    if (i) expect(slot.rect.y).toBeGreaterThanOrEqual(branches[i - 1].rect.y + branches[i - 1].rect.height);
    const ghost: Node = { id: "ghost", position: { x: slot.rect.x, y: slot.rect.y }, width: slot.rect.width, height: slot.rect.height, data: {} };
    nodes.forEach((node) => expect(overlaps(ghost, node)).toBe(false));
  });
  expectDisjointBranchCurves(branches);
  expect(nodes.map(({ id, position }) => ({ id, position }))).toEqual(before);
});

it("chooses one clear shape for sibling Switch ghost curves around a narrow gap obstacle", () => {
  const { source, nodes } = switchSlotsFixture();
  const obstacle: Node = { id: "narrow", position: { x: 1072, y: 206 }, width: 1, height: 1, data: {} };
  expectDisjointBranchCurves(branchSlots(nextSlots(source, [...nodes, obstacle])));
});

it("keeps optional ordered placement bounds inside the current parent and lane", () => {
  const target = { ...actualSlotTemplate(), parentId: "owner", data: { step: { lane_id: "a" } } };
  const blocker: Node = { ...target, id: "blocker", position: { x: 400, y: 224 } };
  expect(placeCanvasNode(target, { x: 400, y: 64 }, [blocker], { minX: 400, maxX: 400, minY: 224 })).toEqual({ x: 400, y: 368 });
  expect(placeCanvasNode(target, { x: 400, y: 64 }, [{ ...blocker, parentId: "elsewhere" }], { minX: 400, maxX: 400, minY: 224 })).toEqual({ x: 400, y: 224 });
  expect(placeCanvasNode(target, { x: 400, y: 64 }, [{ ...blocker, data: { step: { lane_id: "b" } } }], { minX: 400, maxX: 400, minY: 224 })).toEqual({ x: 400, y: 224 });
});


function connectedSwitchSlotsFixture(connected: Array<"high" | "low" | "default">) {
  const anchors = {
    high: { id: "set_variable", x: 1136, y: 112 },
    low: { id: "set_variable_2", x: 1136, y: 432 },
    default: { id: "set_variable_3", x: 1136, y: 576 },
  };
  const edges: WorkflowCanvasGraph["edges"] = connected.map((key) => ({
    source: { kind: "step", id: "priority" },
    outcome: key === "default" ? { name: "default" } : { name: "case", case_id: key },
    target: { kind: "step", id: anchors[key].id },
  }));
  const handles: string[] = connected.map((key) => key === "default" ? key : `case:${key}`);
  return switchSlotsFixture(switchOutcomes.filter((key) => !handles.includes(key)), 256, connected.map((key) => anchors[key]), edges);
}

it("keeps free Default below its real connected High and Low targets and their native solid curves", () => {
  const { source, nodes, edges } = connectedSwitchSlotsFixture(["high", "low"]);
  const before = nodes.map(({ id, position }) => ({ id, position: { ...position } }));
  expect(edges.filter((edge) => edge.source === "priority").map(({ sourceHandle, target }) => [sourceHandle, target])).toEqual([["case:high", "set_variable"], ["case:low", "set_variable_2"]]);
  const slots = nextSlots(source, nodes, edges);
  expect(slots.map(({ outcome }) => outcome)).toEqual([{ name: "default" }, { name: "error" }]);
  expect(slots[0].rect).toEqual({ x: 1136, y: 576, width: 224, height: 144 });
  expect(slots[0].inputPoint).toEqual({ x: 1136, y: 600 });
  // Root's actual native Low solid has these M/L/C/L controls, from y=228 to y=456.
  const nativeLowX = [1040, 1048, 1085.5, 1085.5, 1123, 1131];
  const nativeLowY = [228, 228, 228, 456, 456, 456];
  expect(slots[0].path.match(/[A-Za-z]/g)).toEqual(["M", "L", "C", "L"]);
  const controls = slots[0].path.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
  expect(controls.filter((_, i) => i % 2 === 0)).toEqual(nativeLowX);
  controls.filter((_, i) => i % 2 === 1).forEach((y, i) => expect(y).toBeGreaterThan(nativeLowY[i]));
  expect(nodes.map(({ id, position }) => ({ id, position }))).toEqual(before);
});

it.each([
  { connected: ["low"] as Array<"high" | "low" | "default">, want: [{ outcome: { name: "case", case_id: "high" }, y: 112 }, { outcome: { name: "default" }, y: 576 }] },
  { connected: ["default"] as Array<"high" | "low" | "default">, want: [{ outcome: { name: "case", case_id: "high" }, y: 112 }, { outcome: { name: "case", case_id: "low" }, y: 432 }] },
])("preserves ordinary authored order around connected $connected anchors", ({ connected, want }) => {
  const { source, nodes, edges } = connectedSwitchSlotsFixture(connected);
  expect(branchSlots(nextSlots(source, nodes, edges)).map(({ outcome, rect }) => ({ outcome, y: rect.y }))).toEqual(want);
});

it.each(["missing", "foreign-source", "foreign-parent", "foreign-lane", "error-handle"])("does not borrow an occupied Switch floor from a %s anchor", (kind) => {
  const { source, nodes } = switchSlotsFixture(["default", "error"], 256, [{ id: "set_variable", x: 1136, y: 112 }, { id: "set_variable_2", x: 1136, y: 432 }]);
  const anchor: Node = { id: "foreign", position: { x: 1136, y: 2048 }, width: 224, height: 144, ...(kind === "foreign-parent" ? { parentId: "other" } : {}), data: { step: { lane_id: kind === "foreign-lane" ? "other" : "" } } };
  const edge: Edge = { id: "stale", source: kind === "foreign-source" ? "other" : "priority", sourceHandle: kind === "error-handle" ? "error" : "case:high", target: kind === "missing" ? "missing" : anchor.id };
  expect(nextSlots(source, [...nodes, anchor], [edge])[0].rect.y).toBe(-32);
});


it.each([
  { y: Number.NaN, height: 144 },
  { y: Number.POSITIVE_INFINITY, height: 144 },
  { y: 2048, height: Number.POSITIVE_INFINITY },
])("does not borrow a nonfinite computed anchor bottom from y=$y height=$height", ({ y, height }) => {
  const { source, nodes } = switchSlotsFixture(["default", "error"], 256, [{ id: "set_variable", x: 1136, y: 112 }, { id: "set_variable_2", x: 1136, y: 432 }]);
  const anchor: Node = { id: "malformed", position: { x: 1136, y }, width: 224, height, data: {} };
  const edge: Edge = { id: "malformed", source: "priority", sourceHandle: "case:high", target: anchor.id };
  const slot = nextSlots(source, [...nodes, anchor], [edge])[0];
  expect(slot.rect.y).toBe(-32);
  expect(Number.isFinite(slot.inputPoint.y)).toBe(true);
});


it.each([0.5, 1, 1.5])("keeps the complete pasted batch in the pane with one world delta at zoom %s", (zoom) => {
  const pane = { left: 16 / zoom, top: 96 / zoom, right: 974 / zoom, bottom: 726 / zoom };
  const cards = [{ x: 900 / zoom, y: 850 / zoom, width: 220, height: 200 }, { x: 900 / zoom + 320, y: 850 / zoom + 48, width: 220, height: 200 }];
  const delta = canvasPasteTranslationToBounds(cards, pane);
  expect(delta).not.toBeNull();
  for (const card of cards) {
    expect(card.x + delta!.x).toBeGreaterThanOrEqual(pane.left);
    expect(card.y + delta!.y).toBeGreaterThanOrEqual(pane.top);
    expect(card.x + delta!.x + card.width).toBeLessThanOrEqual(pane.right);
    expect(card.y + delta!.y + card.height).toBeLessThanOrEqual(pane.bottom);
  }
  expect((cards[1].x + delta!.x) - (cards[0].x + delta!.x)).toBeCloseTo(320);
  expect((cards[1].y + delta!.y) - (cards[0].y + delta!.y)).toBeCloseTo(48);
});

it("does not invent finite placement for invalid or oversized pasted geometry", () => {
  const pane = { left: 16, top: 96, right: 974, bottom: 726 };
  expect(canvasPasteTranslationToBounds([{ x: 0, y: 0, width: 2000, height: 200 }], pane)).toBeNull();
  expect(canvasPasteTranslationToBounds([{ x: NaN, y: 0, width: 200, height: 200 }], pane)).toBeNull();
  expect(canvasPasteTranslationToBounds([{ x: 0, y: 0, width: 200, height: 0 }], pane)).toBeNull();
  expect(canvasPasteViewport({ x: 0, y: 0, width: Infinity, height: 200 }, pane, { left: 0, top: 80 })).toBeNull();
});

it("uses actual banner-adjusted stage, drawer width and bottom control rectangles", () => {
  render(createElement("div", { "data-testid": "paste-stage" },
    createElement("div", { className: "workflow-canvas-toolbar", "data-testid": "paste-toolbar" }),
    createElement("div", { className: "workflow-canvas-minimap", "data-testid": "paste-minimap" }),
  ));
  const stage = screen.getByTestId("paste-stage");
  const toolbar = screen.getByTestId("paste-toolbar");
  const minimap = screen.getByTestId("paste-minimap");
  vi.spyOn(stage, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 120, 1440, 780));
  vi.spyOn(toolbar, "getBoundingClientRect").mockReturnValue(new DOMRect(200, 842, 800, 42));
  vi.spyOn(minimap, "getBoundingClientRect").mockReturnValue(new DOMRect(800, 742, 180, 100));
  expect(canvasPastePane(stage, 450)).toEqual({ left: 16, top: 136, right: 974, bottom: 726 });
  expect(canvasPastePane(stage, 1440)).toBeNull();
});

it("fits actual library bounds in a smaller pane using a stage-local transform", () => {
  const bounds = { x: 1728, y: 240, width: 600, height: 220 };
  const pane = { left: 16, top: 136, right: 974, bottom: 726 };
  const viewport = canvasPasteViewport(bounds, pane, { left: 0, top: 120 });
  expect(viewport).not.toBeNull();
  expect(bounds.x * viewport!.zoom + viewport!.x).toBeGreaterThanOrEqual(pane.left);
  expect((bounds.x + bounds.width) * viewport!.zoom + viewport!.x).toBeLessThanOrEqual(pane.right);
  expect(bounds.y * viewport!.zoom + viewport!.y + 120).toBeGreaterThanOrEqual(pane.top);
  expect((bounds.y + bounds.height) * viewport!.zoom + viewport!.y + 120).toBeLessThanOrEqual(pane.bottom);
});


it("keeps a visible manual offset anchor even when another part of the pane contains a minimap", () => {
  const cards = [{ x: 20, y: 700, width: 200, height: 100 }, { x: 340, y: 700, width: 200, height: 100 }];
  const pane = { left: 16, top: 96, right: 974, bottom: 884 };
  expect(canvasPasteRectsVisible(cards, pane, [{ x: 788, y: 781, width: 186, height: 45 }])).toBe(true);
  expect(canvasPasteTranslationToBounds(cards, pane)).toEqual({ x: 0, y: 0 });
});

it("uses a shared 16-unit delta for fractional world pane edges at 150 percent", () => {
  const cards = [{ x: 864, y: 176, width: 224, height: 144 }, { x: 1184, y: 176, width: 224, height: 144 }];
  const pane = { left: 0, top: 0, right: 1408 - 136 / 1.5, bottom: 1000 };
  const delta = canvasPasteTranslationToBounds(cards, pane);
  expect(delta).toEqual({ x: -96, y: 0 });
  expect(delta!.x / 16).toBe(Math.trunc(delta!.x / 16));
  expect(cards[1].x + delta!.x - (cards[0].x + delta!.x)).toBe(320);
});

it("qualifies a physical fit that has no feasible shared grid delta", () => {
  expect(canvasPasteTranslationToBounds([{ x: 0, y: 0, width: 100, height: 100 }], { left: 1, top: 0, right: 101, bottom: 100 })).toBeNull();
});


it("finds a finite common grid translation beside controls for a 704-high batch", () => {
  const cards = [{ x: 1456, y: 0, width: 224, height: 144 }, { x: 1456, y: 560, width: 224, height: 144 }];
  const pane = { left: -8, top: -8, right: 1400, bottom: 780 };
  const controls = [{ x: 301, y: 738, width: 790, height: 42 }, { x: 1176, y: 638, width: 180, height: 100 }];
  const original = cards.map((card) => ({ ...card }));
  expect(canvasPasteTranslationToBounds(cards, { ...pane, bottom: 622 }, controls)).toBeNull();
  const delta = canvasPasteTranslationToBounds(cards, pane, controls);
  expect(delta).toEqual({ x: -512, y: 0 });
  expect(canvasPasteRectsVisible(cards.map((card) => ({ ...card, x: card.x + delta!.x, y: card.y + delta!.y })), pane, controls)).toBe(true);
  expect(cards).toEqual(original);
});

it("checks actual card rectangles so a control inside an empty batch gap does not move a clear anchor", () => {
  const cards = [{ x: 32, y: 32, width: 224, height: 144 }, { x: 32, y: 592, width: 224, height: 144 }];
  const pane = { left: 16, top: 16, right: 1424, bottom: 884 };
  expect(canvasPasteTranslationToBounds(cards, pane, [{ x: 16, y: 200, width: 400, height: 300 }])).toEqual({ x: 0, y: 0 });
});
