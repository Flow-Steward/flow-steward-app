import { buildCanvasLayout } from "./layout";
import type { WorkflowCanvasGraph } from "../shared/api";
import type { Node } from "@xyflow/react";
import { applyCanvasNodeState, manualPositionsFromNodes } from "./canvas-state-helpers";
import { absoluteNodeCenter } from "./canvas-core";
const node = (id: string, type: string, x: number, y: number, width: number, height: number, parentId?: string): Node => ({ id, type, position: { x, y }, width, height, style: { width, height }, parentId, data: {} });
function initial(lanes = false, peer = false): Node[] {
  const graph: WorkflowCanvasGraph = {
    workflow: { id: "manual", enabled: false, display_name: "Manual", callable_by_parent: false }, interface_contract: {}, output_contract: {},
    entry: { kind: "group", id: "outer" },
    groups: [{ id: "outer", display_name: "Outer", body: { kind: "sequence", entry: { kind: "for_each", id: "inner" } } }],
    for_each: [{ id: "inner", group_id: "outer", display_name: "Inner", over: "state.items", item_name: "item", body: lanes ? { kind: "lanes", concurrency_limit: 2, lanes: [{ lane_id: "a", entry: { kind: "step", id: "child" } }] } : { kind: "sequence", entry: { kind: "step", id: "child" } } }],
    steps: (peer ? ["child", "peer"] : ["child"]).map((id) => ({ id, display_name: id, kind: "set_variable", config_summary: "", for_each_id: "inner", lane_id: lanes ? "a" : undefined, ports: [{ outcome: { name: "success" }, label: "Next" }] })),
    edges: [], automation_rules: [], validation_issues: [],
  };
  return buildCanvasLayout({ canvasGraph: graph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() }).nodes.filter((n) => n.type !== "workflowStart");
}
function state(nodes: Node[], manualPositions: Record<string, { x: number; y: number }>) { return applyCanvasNodeState({ nodes, manualPositions, connectionHelpActive: false, highlightedNodeId: null }); }
it("focuses the deepest node using every ancestor offset", () => {
  const nodes = initial(); expect(absoluteNodeCenter(nodes[2], new Map(nodes.map((n) => [n.id, n])))).toEqual({ x: 624, y: 392 });
});
it("grows then shrinks all ancestor containers around actual manual positions", () => {
  const nodes = initial(); const grown = state(nodes, { child: { x: 800, y: 700 } }); const inner = grown[1]; const outer = grown[0];
  expect(inner.width).toBeGreaterThanOrEqual(1056); expect(inner.height).toBeGreaterThanOrEqual(908);
  expect(outer.width).toBeGreaterThanOrEqual(inner.position.x + inner.width! + 32); expect(outer.height).toBeGreaterThanOrEqual(inner.position.y + inner.height! + 64);
  const shrunk = state(nodes, { child: { x: 32, y: 128 } }); expect(shrunk[1].width).toBe(352); expect(shrunk[1].height).toBe(336); expect(shrunk[0].width).toBeLessThan(outer.width!);
  expect(shrunk.map((n) => n.parentId)).toEqual(nodes.map((n) => n.parentId));
});
it("keeps every child absolute position stable while a container origin expands left/up", () => {
  const nodes = initial(false, true);
  const beforePeer = absoluteNodeCenter(nodes[3], new Map(nodes.map((n) => [n.id, n])));
  const moved = state(nodes, { child: { x: -96, y: -32 } }); const byId = new Map(moved.map((n) => [n.id, n]));
  expect(absoluteNodeCenter(moved[3], byId)).toEqual(beforePeer);
  expect(absoluteNodeCenter(moved[2], byId)).toEqual({ x: 496, y: 232 });
  expect(moved[2].position.x).toBeGreaterThanOrEqual(32); expect(moved[2].position.y).toBeGreaterThanOrEqual(128);
  expect(nodes[2].position).toEqual({ x: 32, y: 128 });
});

it("round trips resized parent coordinates without child drift on recompute", () => {
  const base = initial(); const first = state(base, { child: { x: -96, y: -32 } });
  const manual = manualPositionsFromNodes(first, base, { child: { x: -96, y: -32 } });
  const again = state(base, manual);
  expect(again.map((n) => ({ position: n.position, width: n.width, height: n.height }))).toEqual(first.map((n) => ({ position: n.position, width: n.width, height: n.height })));
});
it("retains lane IDs, controls, notes and hook callbacks while fitting manual lane children", () => {
  const nodes = initial(true); const callback = nodes[1].data.onOpenForEach;
  nodes.push(node("annotation:note", "canvasNote", 5000, 6000, 240, 200));
  const result: Node[] = state(nodes, { child: { x: 800, y: 700 } });
  const frame = (result[1].data.laneFrames as { id: string; width: number; addControl: { y: number } }[])[0];
  expect(frame.id).toBe("a"); expect(frame.width).toBe(236); expect(frame.addControl.y).toBe(856);
  expect(result[1].data.onOpenForEach).toBe(callback); expect(result[1].data.forEach).toBe(nodes[1].data.forEach); expect(result[2].data.step).toBe(nodes[2].data.step); expect(result[3].position).toEqual(nodes[3].position);
});
