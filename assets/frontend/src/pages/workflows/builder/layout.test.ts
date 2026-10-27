import type { WorkflowCanvasGraph, WorkflowCanvasStep, WorkflowGraphEdge, WorkflowOutcome } from "../shared/api";
import { buildCanvasLayout, stepNodeHeight, stepNodeWidth } from "./layout";
import { GROUP_HEADER_HEIGHT } from "./layout-constants";
import { applyCanvasNodeState } from "./canvas-state-helpers";

const step = (id: string, extra: Partial<WorkflowCanvasStep> = {}): WorkflowCanvasStep => ({ id, kind: "set_variable", display_name: id, config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }], ...extra });
const edge = (source: string, target: string, name: WorkflowOutcome["name"] = "success"): WorkflowGraphEdge => ({ source: { kind: "step", id: source }, outcome: { name }, target: { kind: "step", id: target } });
function graph(ids: string[], links: WorkflowGraphEdge[], entry: string | null = ids[0]): WorkflowCanvasGraph {
  return { workflow: { id: "layout", enabled: false, display_name: "Layout", callable_by_parent: false }, interface_contract: {}, output_contract: {}, groups: [], for_each: [], steps: ids.map((id) => step(id)), edges: links, entry: entry ? { kind: "step", id: entry } : null, validation_issues: [], automation_rules: [] };
}
function layout(canvasGraph: WorkflowCanvasGraph) {
  return buildCanvasLayout({ canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
}
const positions = (g: WorkflowCanvasGraph) => Object.fromEntries(layout(g).nodes.filter((n) => n.id !== "visual:start").map((n) => [n.id, n.position]));
it("preserves the DAG columns and row order while rounding card heights to the grid", () => {
  expect(positions(graph(["a", "b", "c", "d"], [edge("a", "b"), edge("a", "c"), edge("b", "d"), edge("c", "d")]))).toEqual({ a: { x: 448, y: 64 }, b: { x: 768, y: 64 }, c: { x: 768, y: 240 }, d: { x: 1088, y: 64 } });
});
it("starts every disconnected piece at a common base and follows its whole chain", () => {
  const g = graph(["a", "done", "f1", "f2", "f3", "i1", "i2"], [edge("a", "done"), edge("f1", "f2"), edge("f2", "f3"), edge("i1", "i2")]);
  const result = layout(g); const p = positions(g);
  expect(p.f1.x).toBe(p.i1.x); expect(p.f2.x).toBe(p.i2.x);
  expect(p.f1.x).toBeGreaterThan(p.done.x); expect(p.f2.x).toBeGreaterThan(p.f1.x); expect(p.f3.x).toBeGreaterThan(p.f2.x); expect(p.f1.y).not.toBe(p.i1.y);
  expect(positions(g)).toEqual(p);
  expect((result.nodes.find((n) => n.id === "f3")?.data as { unreachable: boolean }).unreachable).toBe(true);
});
it.each([null, "missing"])("ranks a whole chain with entry %s without unreachable badges", (entry) => {
  const g = graph(["a", "b", "c"], [edge("a", "b"), edge("b", "c")], entry);
  const result = layout(g); const p = positions(g);
  expect(p.a.x).toBeLessThan(p.b.x); expect(p.b.x).toBeLessThan(p.c.x);
  if (!entry) expect(result.nodes.every((n) => !("unreachable" in n.data && n.data.unreachable))).toBe(true);
});
it("drops only active DFS backedges for ranking while drawing and reaching the entire cycle", () => {
  const g = graph(["a", "b", "c"], [edge("a", "b"), edge("b", "c"), edge("c", "a")]);
  const result = layout(g); const p = positions(g);
  expect(p.a.x).toBe(448); expect(p.b.x).toBe(768); expect(p.c.x).toBe(1088);
  expect(result.edges.filter((e) => e.data)).toHaveLength(3);
  expect(result.nodes.every((n) => !("unreachable" in n.data && n.data.unreachable))).toBe(true); expect(positions(g)).toEqual(p);
  expect(positions({ ...g, entry: null })).toEqual(p);
});
it("orders keyed targets by actual incoming port Y while unkeyed nodes keep their slots", () => {
  const g = graph(["source", "lower", "u", "upper", "tie"], [edge("source", "lower", "false"), edge("source", "u", "default"), edge("source", "upper", "true"), edge("source", "tie", "true")]);
  g.steps[0] = step("source", { kind: "if", ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }] });
  // A persisted unknown outcome has no rendered source handle and supplies no ordering key.
  const result = layout(g); const p = positions(g);
  expect(p.upper.y).toBeLessThan(p.lower.y); expect(p.upper.y).toBeLessThan(p.tie.y); expect(p.u.y).toBe(240);
  const upper = result.edges.find((e) => e.source === "source" && e.target === "upper")!;
  expect(upper.data?.routing).toMatchObject({ sameScope: true, sourceColumn: 0, targetColumn: 1, longJump: false });
});
it.each(["group", "for_each"] as const)("ranks sequence and lane scopes independently for %s", (kind) => {
  for (const lanes of [false, true]) {
    const g = graph(["a", "b", "c", "d"], [edge("a", "b"), edge("c", "d")], null);
    const body = lanes ? { kind: "lanes", concurrency_limit: 2, lanes: [{ lane_id: "left" }, { lane_id: "right" }] } : { kind: "sequence" };
    if (kind === "group") g.groups = [{ id: "box", display_name: "Box", body }];
    else g.for_each = [{ id: "box", display_name: "Box", over: "state.items", item_name: "item", body }];
    g.steps = g.steps.map((s, i) => ({ ...s, [kind === "group" ? "group_id" : "for_each_id"]: "box", lane_id: lanes ? i < 2 ? "left" : "right" : undefined }));
    const r = layout(g); const p = positions(g);
    expect(p.a.x).toBeLessThan(p.b.x); expect(p.c.x).toBeLessThan(p.d.x); expect(p.a.x).toBe(p.c.x); expect(p.a.y).not.toBe(p.c.y);
    expect(r.edges.every((e) => e.data?.routing?.sameScope)).toBe(true);
  }
});
it("rounds actual node and measured heights up to the 16px grid without changing accepted widths", () => {
  for (const kind of ["set_variable", "if", "switch", "batch", "stop_success", "stop_fail"]) {
    const s = step(kind, { kind }); expect(stepNodeHeight(s, 231) % 16).toBe(0); expect(stepNodeHeight(s, 231)).toBeGreaterThanOrEqual(237);
  }
  expect(["if", "switch", "batch", "stop_success"].map((kind) => stepNodeWidth(step(kind, { kind })))).toEqual([240, 272, 256, 224]);
});

it("keeps complete DAG coordinates identical at already-grid-aligned measured heights", () => {
  const g = graph(["a", "b", "c", "d"], [edge("a", "b"), edge("a", "c"), edge("b", "d"), edge("c", "d")]);
  const result = buildCanvasLayout({ canvasGraph: g, measuredStepHeights: { a: 138, b: 138, c: 138, d: 138 }, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  expect(Object.fromEntries(result.nodes.filter((n) => n.id !== "visual:start").map((n) => [n.id, n.position]))).toEqual({ a: { x: 448, y: 64 }, b: { x: 768, y: 64 }, c: { x: 768, y: 240 }, d: { x: 1088, y: 64 } });
});
it("uses mean previous-column source port Y and excludes Repeat and long-jump sources", () => {
  const g = graph(["entry", "top", "bottom", "one", "two"], [edge("entry", "top"), edge("entry", "bottom"), edge("bottom", "one"), edge("top", "two"), edge("bottom", "two"), { ...edge("top", "one"), rework: true }, edge("entry", "one")]);
  const p = positions(g); expect(p.two.y).toBeLessThan(p.one.y);
});
it("publishes logical long-jump columns independently of manual coordinates and excludes exits", () => {
  const g = graph(["a", "b", "c"], [edge("a", "b"), edge("b", "c"), edge("a", "c", "error")]);
  expect(layout(g).edges.find((e) => e.source === "a" && e.target === "c")?.data?.routing).toEqual({ sameScope: true, sourceColumn: 0, targetColumn: 2, longJump: true });
});

it("ranks missing-entry For each bodies inside a sequence Group without changing parent or exit refs", () => {
  const g = graph(["a", "b", "c"], [edge("a", "b"), edge("b", "c")], null);
  g.entry = { kind: "group", id: "outer" }; g.groups = [{ id: "outer", display_name: "Once", body: { kind: "sequence", entry: { kind: "for_each", id: "walk" } } }];
  g.for_each = [{ id: "walk", group_id: "outer", display_name: "Walk", over: "state.items", item_name: "item", body: { kind: "sequence", concurrency_limit: 2 } }];
  g.steps = g.steps.map((s) => ({ ...s, for_each_id: "walk" }));
  g.edges.push({ source: { kind: "step", id: "c" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "walk", lane_id: null } });
  const result = layout(g); const byId = new Map(result.nodes.map((n) => [n.id, n]));
  expect(byId.get("for_each:walk")?.parentId).toBe("group:outer"); expect(byId.get("a")?.parentId).toBe("for_each:walk");
  expect(byId.get("a")!.position.x).toBeLessThan(byId.get("b")!.position.x); expect(byId.get("b")!.position.x).toBeLessThan(byId.get("c")!.position.x);
  expect(result.nodes.indexOf(byId.get("group:outer")!)).toBeLessThan(result.nodes.indexOf(byId.get("for_each:walk")!));
  expect(result.edges.find((e) => e.source === "c")?.data?.edge.target).toEqual({ kind: "for_each_exit", for_each_id: "walk", lane_id: null });
});


it.each(["step", "group", "for_each"] as const)("aligns automatic Start with the root %s entry on the 16px grid", (kind) => {
  const g = graph(["work"], []);
  g.entry = { kind, id: "work" };
  if (kind === "group") {
    g.groups = [{ id: "work", display_name: "Once", body: { kind: "sequence", entry: { kind: "step", id: "member" } } }];
    g.steps = [step("member", { group_id: "work" })];
  } else if (kind === "for_each") {
    g.for_each = [{ id: "work", display_name: "Each item", over: "state.items", item_name: "item", body: { kind: "sequence", entry: { kind: "step", id: "member" } } }];
    g.steps = [step("member", { for_each_id: "work" })];
  }
  const result = layout(g);
  const id = kind === "step" ? "work" : `${kind}:work`;
  const entry = result.nodes.find((node) => node.id === id)!;
  const start = result.nodes.find((node) => node.id === "visual:start")!;
  expect(start.position.y).toBe(entry.position.y);
  expect(start.draggable).toBe(false);
  expect(start.position.x % 16).toBe(0);
  expect(start.position.y % 16).toBe(0);
  const source = start.handles!.find((handle) => handle.id === "entry")!;
  const target = entry.handles!.find((handle) => handle.id === (kind === "step" ? "in" : "entry"))!;
  expect(start.position.y + source.y + source.height! / 2).toBe(entry.position.y + target.y + target.height! / 2);
  expect(result.edges.find((link) => link.id === "visual:entry")).toMatchObject({ source: "visual:start", target: id, sourceHandle: "entry", targetHandle: kind === "step" ? "in" : "entry", selectable: false, deletable: false });
  if (kind !== "step") {
    expect(entry.handles).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "entry", x: -5, y: 19, width: 10, height: 10 }),
      expect.objectContaining({ id: "success", x: entry.width! - 5, y: 19, width: 10, height: 10 }),
      expect.objectContaining({ id: "exit", x: entry.width! / 2 - 5, y: entry.height! - 5, width: 10, height: 10 }),
    ]));
    expect(GROUP_HEADER_HEIGHT).toBe(96);
    expect(result.nodes.find((node) => node.id === "member")?.position.y).toBe(128);
  }
  const moved = applyCanvasNodeState({ nodes: result.nodes, manualPositions: { [id]: { x: 896, y: 496 } }, connectionHelpActive: false, highlightedNodeId: null });
  expect(moved.find((node) => node.id === "visual:start")?.position).toEqual({ x: 512, y: 496 });
});

it.each([null, { kind: "step" as const, id: "missing" }, { kind: "group" as const, id: "missing" }, { kind: "for_each" as const, id: "missing" }, { kind: "step" as const, id: "nested" }])("keeps Start stable for an absent or non-root entry %j", (entry) => {
  const g = graph(["nested"], [], null);
  g.entry = entry;
  g.groups = [{ id: "box", display_name: "Box", body: { kind: "sequence", entry: { kind: "step", id: "nested" } } }];
  g.steps = [step("nested", { group_id: "box" })];
  const result = layout(g);
  const start = result.nodes.find((node) => node.id === "visual:start")!;
  expect(start.data).toMatchObject({ entryNodeId: undefined });
  expect(start.position).toEqual({ x: 64, y: 64 });
  const moved = applyCanvasNodeState({ nodes: result.nodes, manualPositions: { nested: { x: 896, y: 496 }, missing: { x: 896, y: 496 } }, connectionHelpActive: false, highlightedNodeId: null });
  expect(moved.find((node) => node.id === "visual:start")?.position).toEqual(start.position);
});


// Same linear topology as the existing disabled f14 visual fixture: 122
// ordinary SetVariable cards followed by Done. Keep real production widths.
function largeLinearGraphForWrap(): WorkflowCanvasGraph {
  const ids = [...Array.from({ length: 122 }, (_, index) => `node_${String(index + 1).padStart(3, "0")}`), "done"];
  const result = graph(ids, ids.slice(1).map((id, index) => edge(ids[index], id)));
  result.steps[result.steps.length - 1] = step("done", { kind: "stop_success", ports: [] });
  return result;
}

it("wraps a wide linear graph into left-to-right rows with Start at the top left before manual placement", () => {
  const g = largeLinearGraphForWrap();
  const original = structuredClone(g);
  const result = layout(g);
  const authored = g.steps.map((item) => result.nodes.find((node) => node.id === item.id)!);
  expect(authored).toHaveLength(123);
  // The cards alone exceed the threshold; no invented custom width or exact
  // off-grid 5601px fixture is needed to establish this precondition.
  expect(authored.reduce((width, node) => width + Number(node.width), 0)).toBeGreaterThan(5600);
  const start = result.nodes.find((node) => node.id === "visual:start")!;
  expect(start.position.x).toBeLessThanOrEqual(Math.min(...authored.map((node) => node.position.x)));
  expect(start.position.y).toBe(Math.min(...authored.map((node) => node.position.y)));
  expect(new Set(authored.map((node) => node.position.y)).size).toBeGreaterThan(1);
  for (let index = 1; index < authored.length; index += 1) {
    const previous = authored[index - 1].position;
    const current = authored[index].position;
    if (current.y === previous.y) {
      expect(current.x).toBeGreaterThan(previous.x);
    } else {
      expect(current.y).toBeGreaterThan(previous.y);
      expect(current.x).toBeLessThanOrEqual(previous.x);
    }
  }
  expect(g).toEqual(original);
  const drawnEdges = result.edges.flatMap((link) => link.data ? [link.data.edge] : []);
  expect(drawnEdges).toHaveLength(original.edges.length);
  expect(drawnEdges).toEqual(expect.arrayContaining(original.edges));

  // Manual coordinates belong to the existing later overlay, never the
  // automatic wrapping pass. A manually placed card may sit outside its row.
  const automaticPositions = result.nodes.map((node) => ({ id: node.id, position: { ...node.position } }));
  const manualPositions = { node_061: { x: 9600, y: 640 } };
  const manualBefore = structuredClone(manualPositions);
  const displayed = applyCanvasNodeState({ nodes: result.nodes, manualPositions, connectionHelpActive: false, highlightedNodeId: null });
  expect(displayed.find((node) => node.id === "node_061")?.position).toEqual({ x: 9600, y: 640 });
  expect(manualPositions).toEqual(manualBefore);
  expect(result.nodes.map((node) => ({ id: node.id, position: node.position }))).toEqual(automaticPositions);
});

it("keeps a sequence Group and its member-relative geometry atomic when the wide root chain wraps", () => {
  const g = largeLinearGraphForWrap();
  const replacedId = "node_061";
  const rootIds = g.steps.map((item) => item.id === replacedId ? "group:wrap_box" : item.id);
  const box = { kind: "group" as const, id: "wrap_box" };
  g.steps = [
    ...g.steps.filter((item) => item.id !== replacedId),
    step("inside_a", { group_id: "wrap_box" }),
    step("inside_b", { group_id: "wrap_box" }),
  ];
  g.groups = [{ id: "wrap_box", display_name: "Keep together", body: { kind: "sequence", entry: { kind: "step", id: "inside_a" } } }];
  g.edges = g.edges.map((link) => ({
    ...link,
    source: link.source.kind === "step" && link.source.id === replacedId ? box : link.source,
    target: link.target.kind === "step" && link.target.id === replacedId ? box : link.target,
  }));
  g.edges.push(edge("inside_a", "inside_b"), { source: { kind: "step", id: "inside_b" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "wrap_box", lane_id: null } });
  const original = structuredClone(g);
  const result = layout(g);
  const frame = result.nodes.find((node) => node.id === "group:wrap_box")!;
  const members = result.nodes.filter((node) => node.parentId === "group:wrap_box");
  expect(frame.parentId).toBeUndefined();
  expect(members.map((node) => node.id).sort()).toEqual(["inside_a", "inside_b"]);
  for (const member of members) {
    expect(member.position.x).toBeGreaterThanOrEqual(0);
    expect(member.position.y).toBeGreaterThanOrEqual(0);
    expect(member.position.x + Number(member.width)).toBeLessThanOrEqual(Number(frame.width));
    expect(member.position.y + Number(member.height)).toBeLessThanOrEqual(Number(frame.height));
  }
  const rootNodes = rootIds.map((id) => result.nodes.find((node) => node.id === id)!);
  expect(rootNodes.reduce((width, node) => width + Number(node.width), 0)).toBeGreaterThan(5600);
  expect(new Set(rootNodes.map((node) => node.position.y)).size).toBeGreaterThan(1);
  expect(g).toEqual(original);
  expect(result.edges.find((link) => link.source === "inside_b")?.data?.edge.target).toEqual({ kind: "group_exit", group_id: "wrap_box", lane_id: null });
});
