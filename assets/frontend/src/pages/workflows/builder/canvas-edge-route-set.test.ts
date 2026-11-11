import type { WorkflowCanvasGraph, WorkflowCanvasStep, WorkflowGraphEdge, WorkflowOutcome } from "../shared/api";
import { applyCanvasNodeState } from "./canvas-state-helpers";
import { canvasDragScope, CanvasEdgeRouteCache, droppedRowTop, routeCanvasEdgeSet } from "./canvas-edge-route-set";
import { buildCanvasLayout } from "./layout";

const step = (id: string, extra: Partial<WorkflowCanvasStep> = {}): WorkflowCanvasStep => ({ id, kind: "set_variable", display_name: id, config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }], ...extra });
const edge = (source: string, target: string, name: WorkflowOutcome["name"] = "success"): WorkflowGraphEdge => ({ source: { kind: "step", id: source }, outcome: { name }, target: { kind: "step", id: target } });

/** One canvas with every frame kind: a simple group, a parallel group with lanes, a For each, an error path and a repeat. */
function mixedGraph(): WorkflowCanvasGraph {
  return {
    workflow: { id: "mixed", enabled: false, display_name: "Mixed", callable_by_parent: false }, interface_contract: {}, output_contract: {},
    entry: { kind: "step", id: "fetch" },
    groups: [
      { id: "prepare", display_name: "Prepare", body: { kind: "sequence", entry: { kind: "step", id: "clean" } } },
      { id: "fan", display_name: "Fan out", body: { kind: "lanes", concurrency_limit: 2, lanes: [{ lane_id: "left", entry: { kind: "step", id: "left_a" } }, { lane_id: "right", entry: { kind: "step", id: "right_a" } }] } },
    ],
    for_each: [{ id: "each_row", display_name: "Each row", over: "state.rows", item_name: "row", body: { kind: "sequence", entry: { kind: "step", id: "row_check" } } }],
    steps: [
      step("fetch", { kind: "connector", ports: [{ outcome: { name: "success" }, label: "Next" }, { outcome: { name: "error" }, label: "Error" }] }),
      step("clean", { group_id: "prepare" }), step("shape", { group_id: "prepare" }),
      step("left_a", { group_id: "fan", lane_id: "left" }), step("left_b", { group_id: "fan", lane_id: "left" }), step("right_a", { group_id: "fan", lane_id: "right" }),
      step("row_check", { kind: "if", for_each_id: "each_row", ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }] }),
      step("row_write", { kind: "connector", for_each_id: "each_row" }),
      step("report", { kind: "template" }), step("failed", { kind: "stop_fail", ports: [] }), step("done", { kind: "stop_success", ports: [] }),
    ],
    edges: [
      { source: { kind: "step", id: "fetch" }, outcome: { name: "success" }, target: { kind: "group", id: "prepare" } },
      edge("fetch", "failed", "error"),
      edge("clean", "shape"),
      { source: { kind: "step", id: "shape" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "prepare", lane_id: null } },
      { source: { kind: "group", id: "prepare" }, outcome: { name: "success" }, target: { kind: "group", id: "fan" } },
      edge("left_a", "left_b"),
      { source: { kind: "group", id: "fan" }, outcome: { name: "success" }, target: { kind: "for_each", id: "each_row" } },
      edge("row_check", "row_write", "true"),
      { source: { kind: "step", id: "row_write" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "each_row", lane_id: null } },
      { source: { kind: "for_each", id: "each_row" }, outcome: { name: "success" }, target: { kind: "step", id: "report" } },
      { ...edge("report", "fetch"), rework: true },
      edge("report", "done"),
    ],
    validation_issues: [], automation_rules: [],
  };
}

function canvas() {
  const layout = buildCanvasLayout({ canvasGraph: mixedGraph(), onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const place = (manualPositions: Record<string, { x: number; y: number }> = {}) => applyCanvasNodeState({ nodes: layout.nodes, manualPositions, connectionHelpActive: false, highlightedNodeId: null });
  const route = (positioned: ReturnType<typeof place>, draggingIds: ReadonlySet<string> = new Set(), previous: ReturnType<typeof routeCanvasEdgeSet> | null = null) =>
    routeCanvasEdgeSet({ nodes: layout.nodes, positioned, rootRows: layout.rootRows, edges: layout.edges, draggingIds, previous });
  return { layout, place, route };
}
const drawn = (memo: ReturnType<typeof routeCanvasEdgeSet>) => memo.edges.map((link) => ({ id: link.id, route: link.data?.route, labelAt: link.data?.labelAt }));

describe("canvas connections reuse what did not move", () => {
  it("routes every connection of every frame kind, error path and repeat included", () => {
    const { route, place, layout } = canvas();
    const memo = route(place());
    const routed = memo.edges.filter((link) => link.data);
    expect(routed.length).toBe(layout.edges.filter((link) => link.data).length);
    expect(routed.every((link) => link.data?.route?.path)).toBe(true);
    expect(routed.some((link) => link.data?.failurePath)).toBe(true);
    expect(routed.some((link) => link.data?.rework)).toBe(true);
    expect(memo.settled).toBe(true);
  });

  it("keeps the whole pass when a redraw moved nothing (hover, highlight, connection hints)", () => {
    const { route, place } = canvas();
    const first = route(place());
    expect(route(place(), new Set(), first)).toBe(first);
  });

  it("moves only the dragged card's connections, drawn without detours, and leaves the rest as drawn", () => {
    const { route, place } = canvas();
    const first = route(place());
    const dragged = route(place({ report: { x: 2400, y: 900 } }), new Set(["report"]), first);
    expect(dragged.settled).toBe(false);
    dragged.edges.forEach((link, index) => {
      const touches = link.source === "report" || link.target === "report";
      if (!link.data) return;
      if (touches) expect(link).not.toBe(first.edges[index]);
      else expect(link).toBe(first.edges[index]);
    });
  });

  it("routes everything on the drop exactly like a fresh pass, keeping objects whose route stayed", () => {
    const { route, place } = canvas();
    const first = route(place());
    const moved = { report: { x: 2400, y: 900 } };
    const dragged = route(place(moved), new Set(["report"]), first);
    const dropped = route(place(moved), new Set(), dragged);
    expect(dropped.settled).toBe(true);
    expect(drawn(dropped)).toEqual(drawn(route(place(moved))));
    const unchanged = dropped.edges.filter((link, index) => link.data && link.data.route?.path === first.edges[index].data?.route?.path);
    expect(unchanged.length).toBeGreaterThan(0);
    unchanged.forEach((link) => expect(first.edges).toContain(link));
  });

  it("follows a member drag with its whole frame: a frame fits itself round its members", () => {
    const { place } = canvas();
    const positioned = place();
    const members = positioned.filter((node) => node.parentId === "for_each:each_row").map((node) => node.id);
    expect(members.length).toBeGreaterThan(1);
    const scope = canvasDragScope(positioned, new Set([members[0]]));
    expect(scope.has("for_each:each_row")).toBe(true);
    members.forEach((id) => expect(scope.has(id)).toBe(true));
    expect(scope.has("fetch")).toBe(false);
    expect(canvasDragScope(positioned, new Set(["fetch"]))).toEqual(new Set(["fetch"]));
  });

  it("starts over when the layout itself changes", () => {
    const { route, place } = canvas();
    const first = route(place());
    const other = canvas();
    const next = other.route(other.place(), new Set(["report"]), first);
    expect(next.settled).toBe(true);
    expect(next.edges.every((link) => !first.edges.includes(link))).toBe(true);
  });

  it("keeps its last pass across renders through the cache", () => {
    const { layout, place } = canvas();
    const cache = new CanvasEdgeRouteCache();
    const input = { nodes: layout.nodes, rootRows: layout.rootRows, edges: layout.edges, draggingIds: new Set<string>() };
    const first = cache.route({ ...input, positioned: place() });
    expect(cache.route({ ...input, positioned: place() })).toBe(first);
  });
});

describe("the row a connection drops into", () => {
  const source = { x: 1800, y: 400, width: 224, height: 80 };
  it("starts at the tallest card beside the target, not at the source row's frame that hangs lower", () => {
    const target = { x: 0, y: 760, width: 224, height: 80 };
    const sourceRowFrame = { x: 1100, y: 410, width: 600, height: 260 };
    const targetRowFrame = { x: 300, y: 744, width: 300, height: 170 };
    const farBelow = { x: 0, y: 1000, width: 224, height: 80 };
    expect(droppedRowTop(source, target, [sourceRowFrame, targetRowFrame, farBelow, target])).toBe(744);
  });
  it("is the target's own top when nothing stands beside it", () => {
    expect(droppedRowTop(source, { x: 0, y: 760, width: 224, height: 80 }, [])).toBe(760);
  });
});
