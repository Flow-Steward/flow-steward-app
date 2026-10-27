import type { WorkflowCanvasGraph, WorkflowCanvasStep, WorkflowGraphEdge, WorkflowNodeRef, WorkflowOutcome } from "../shared/api";
import { buildCanvasLayout, stepNodeWidth } from "./layout";

type Layout = ReturnType<typeof buildCanvasLayout>;
type RootRows = {
  rows: readonly { rootIds: readonly string[]; top: number; bottom: number; gapAfter: number }[];
  rowIndexByRootId: ReadonlyMap<string, number>;
};

const step = (id: string, extra: Partial<WorkflowCanvasStep> = {}): WorkflowCanvasStep => ({
  id, kind: "set_variable", display_name: id, config_summary: "",
  ports: [{ outcome: { name: "success" }, label: "Next" }], ...extra,
});
const edge = (source: string, target: string, outcome: WorkflowOutcome = { name: "success" }): WorkflowGraphEdge => ({
  source: { kind: "step", id: source }, outcome, target: { kind: "step", id: target },
});
function chain(count: number): WorkflowCanvasGraph {
  const ids = Array.from({ length: count }, (_, index) => `node_${String(index).padStart(3, "0")}`);
  return {
    workflow: { id: "wrap_contract", enabled: false, display_name: "Wrap contract", callable_by_parent: false },
    interface_contract: {}, output_contract: {}, groups: [], for_each: [],
    steps: ids.map((id) => step(id)), edges: ids.slice(1).map((id, index) => edge(ids[index], id)),
    entry: { kind: "step", id: ids[0] }, validation_issues: [], automation_rules: [],
  };
}
function layout(canvasGraph: WorkflowCanvasGraph, measuredStepHeights: Record<string, number> = {}) {
  return buildCanvasLayout({
    canvasGraph, measuredStepHeights,
    onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn(),
  });
}
function rowsOf(result: Layout): RootRows {
  // The assertion occurs after the real public layout call. This local shape
  // adds no import of the absent-P Wrap helper or future production type.
  const rows = (result as Layout & { rootRows?: RootRows }).rootRows;
  expect(rows).toBeDefined();
  return rows!;
}
function assertRootRows(result: Layout) {
  const meta = rowsOf(result);
  const roots = result.nodes.filter((node) => !node.parentId && node.id !== "visual:start");
  const byId = new Map(roots.map((node) => [node.id, node]));
  const ids = meta.rows.flatMap((row) => [...row.rootIds]);
  expect(ids).toHaveLength(roots.length);
  expect(new Set(ids).size).toBe(roots.length);
  expect([...ids].sort()).toEqual(roots.map((node) => node.id).sort());
  expect([...meta.rowIndexByRootId.keys()].sort()).toEqual([...ids].sort());
  expect(meta.rows.length).toBeGreaterThan(1);
  const start = result.nodes.find((node) => node.id === "visual:start")!;
  expect(start.position).toEqual({ x: 64, y: 64 });
  for (const [index, row] of meta.rows.entries()) {
    expect(row.rootIds.length).toBeGreaterThan(0);
    const members = row.rootIds.map((id) => byId.get(id)!);
    expect(row.top).toBe(Math.min(...members.map((node) => node.position.y)));
    expect(row.bottom).toBe(Math.max(...members.map((node) => node.position.y + Number(node.height)), index === 0 ? start.position.y + Number(start.height) : -Infinity));
    expect(Math.min(...members.map((node) => node.position.x))).toBe(index === 0 ? 448 : 64);
    expect(row.top % 16).toBe(0);
    expect(row.bottom % 16).toBe(0);
    for (const node of members) {
      expect(meta.rowIndexByRootId.get(node.id)).toBe(index);
      expect(node.position.x % 16).toBe(0);
      expect(node.position.y % 16).toBe(0);
      expect(node.position.y + Number(node.height)).toBeLessThanOrEqual(row.bottom);
    }
    if (index < meta.rows.length - 1) {
      expect(row.gapAfter).toBeGreaterThanOrEqual(128);
      expect(row.gapAfter % 16).toBe(0);
      expect(meta.rows[index + 1].top - row.bottom).toBe(row.gapAfter);
    } else expect(row.gapAfter).toBe(0);
  }
  expect(result.totalWidth).toBeGreaterThanOrEqual(Math.max(...roots.map((node) => node.position.x + Number(node.width))));
  return meta;
}

it.each([
  { batches: 0, realWidth: 5344, totalWidth: 6036 },
  { batches: 8, realWidth: 5600, totalWidth: 6292 },
])("retains the complete nonwrap shape and baseline coordinates at real width $realWidth", ({ batches, realWidth, totalWidth }) => {
  const g = chain(17);
  g.steps = g.steps.map((item, index) => index < batches ? { ...item, kind: "batch" } : item);
  const before = structuredClone(g);
  const result = layout(g);
  const roots = g.steps.map((item) => result.nodes.find((node) => node.id === item.id)!);
  const bound = Math.max(...roots.map((node) => node.position.x + Number(node.width))) - Math.min(...roots.map((node) => node.position.x));
  expect(bound).toBe(realWidth);
  expect(Object.keys(result).sort()).toEqual(["edges", "nodes", "totalWidth"]);
  expect(result.totalWidth).toBe(totalWidth);
  expect(roots.map((node) => node.position)).toEqual(g.steps.map((_, index) => ({ x: 448 + 320 * index + 32 * Math.min(index, batches), y: 64 })));
  expect(result.nodes.find((node) => node.id === "visual:start")?.position).toEqual({ x: 64, y: 64 });
  expect(result.edges.flatMap((link) => link.data ? [link.data.edge] : [])).toEqual(g.edges);
  expect(g).toEqual(before);
});

it.each([123, 201])("publishes consistent world rows for %i measured roots without changing graph or card sizes", (count) => {
  const g = chain(count);
  g.steps[g.steps.length - 1] = step(g.steps[g.steps.length - 1].id, { kind: "stop_success", ports: [] });
  const measured: Record<string, number> = Object.fromEntries(g.steps.filter((_, index) => index % 11 === 0).map((item) => [item.id, 234]));
  const measuredBefore = { ...measured };
  const before = structuredClone(g);
  const result = layout(g, measured);
  const meta = assertRootRows(result);
  expect(meta.rowIndexByRootId.size).toBe(count);
  for (const item of g.steps) {
    const node = result.nodes.find((candidate) => candidate.id === item.id)!;
    expect(node.width).toBe(stepNodeWidth(item));
    expect(Number(node.height) % 16).toBe(0);
    if (measured[item.id] !== undefined) expect(node.height).toBe(240);
  }
  expect(result.edges.flatMap((link) => link.data ? [link.data.edge] : [])).toEqual(g.edges);
  expect(measured).toEqual(measuredBefore);
  expect(g).toEqual(before);
});

function atomicGraph(kind: "group" | "for_each" | "hook") {
  const g = chain(20);
  const replaced = g.steps[10].id;
  const id = kind === "hook" ? "hook:before" : "atomic";
  const ref: WorkflowNodeRef = { kind: kind === "for_each" ? "for_each" : "group", id };
  const nodeId = `${ref.kind}:${id}`;
  const memberIds = kind === "hook" ? ["hook:before:inside_a", "hook:before:inside_b"] : ["inside_a", "inside_b"];
  const body = { kind: "sequence", entry: { kind: "step", id: memberIds[0] } };
  if (kind === "for_each") g.for_each = [{ id, display_name: "Atomic loop", over: "state.items", item_name: "item", body }];
  else g.groups = [{ id, display_name: "Atomic container", body, ...(kind === "hook" ? { hook: { hookId: "before", stepId: "before", displayName: "Hook", connected: true, targetWorkflowId: "child", targetName: "Child", usedIn: 1 } } : {}) }];
  const members = memberIds.map((memberId) => step(memberId, kind === "for_each" ? { for_each_id: id } : { group_id: id }));
  g.steps = [...g.steps.filter((item) => item.id !== replaced), ...members];
  g.edges = g.edges.map((link) => ({ ...link, source: link.source.id === replaced ? ref : link.source, target: link.target.kind === "step" && link.target.id === replaced ? ref : link.target }));
  const internal: WorkflowGraphEdge[] = [edge(memberIds[0], memberIds[1]), {
    source: { kind: "step", id: memberIds[1] }, outcome: { name: "success" },
    target: kind === "for_each" ? { kind: "for_each_exit", for_each_id: id, lane_id: null } : { kind: "group_exit", group_id: id, lane_id: null },
  }];
  g.edges.push(...internal);
  const control: WorkflowCanvasGraph = { ...g, entry: ref, steps: members, edges: internal };
  return { g, control, nodeId, memberIds };
}

it.each(["group", "for_each", "hook"] as const)("keeps the complete %s atomic and child-local geometry identical when surrounding roots wrap", (kind) => {
  const { g, control, nodeId, memberIds } = atomicGraph(kind);
  const before = structuredClone(g);
  const isolated = layout(control);
  const result = layout(g);
  const meta = assertRootRows(result);
  const frame = result.nodes.find((node) => node.id === nodeId)!;
  const reference = isolated.nodes.find((node) => node.id === nodeId)!;
  expect(frame.parentId).toBeUndefined();
  expect(frame.width).toBe(reference.width);
  expect(frame.height).toBe(reference.height);
  expect(meta.rows.flatMap((row) => [...row.rootIds]).filter((id) => id === nodeId)).toHaveLength(1);
  for (const id of memberIds) {
    const child = result.nodes.find((node) => node.id === id)!;
    const original = isolated.nodes.find((node) => node.id === id)!;
    expect(child.parentId).toBe(nodeId);
    expect(child.position).toEqual(original.position);
    expect(child.width).toBe(original.width);
    expect(child.height).toBe(original.height);
    expect(child.handles).toEqual(original.handles);
    expect(child.extent).toBe("parent");
    expect(child.position.x + Number(child.width)).toBeLessThanOrEqual(Number(frame.width));
    expect(child.position.y + Number(child.height)).toBeLessThanOrEqual(Number(frame.height));
    expect(meta.rowIndexByRootId.has(id)).toBe(false);
  }
  expect(result.nodes.indexOf(frame)).toBeLessThan(result.nodes.indexOf(result.nodes.find((node) => node.id === memberIds[0])!));
  expect(result.edges.flatMap((link) => link.data ? [link.data.edge] : [])).toEqual(g.edges);
  expect(g).toEqual(before);
});

it("uses the connected Repeat cut cost while reserving only one lane per crossing edge", () => {
  const g = chain(20);
  const ids = g.steps.map((item) => item.id);
  const cases = ["a", "b", "c", "d", "e"];
  g.steps[4] = step(ids[4], { kind: "switch", ports: cases.map((case_id) => ({ outcome: { name: "case" as const, case_id }, label: case_id })) });
  g.edges = g.edges.map((link) => link.source.id === ids[4] ? { ...link, outcome: { name: "case" as const, case_id: "a" } } : link);
  g.edges.push(...cases.slice(1).map((case_id) => edge(ids[4], ids[5], { name: "case", case_id })));
  const shortRepeat: WorkflowGraphEdge = { ...edge(ids[4], ids[3], { name: "error" }), rework: true, rework_limit: 2 };
  const longRepeat: WorkflowGraphEdge = { ...edge(ids[19], ids[0], { name: "error" }), rework: true, rework_limit: 2 };
  g.edges.push(shortRepeat, longRepeat);
  const before = structuredClone(g);
  const control = { ...g, edges: g.edges.filter((link) => link !== shortRepeat) };
  const result = layout(g);
  const meta = assertRootRows(result);
  const withoutShort = rowsOf(layout(control));
  // Both public results choose their best nominal candidate. At the proper
  // first cut the short Repeat makes four ordinary crossings equivalent; an
  // unweighted Repeat would instead prefer the earlier cut after four roots.
  expect(meta.rows[0].rootIds).toEqual(ids.slice(0, 5));
  expect(withoutShort.rows[0].rootIds).toEqual(ids.slice(0, 4));
  // Five case edges plus ONE long Repeat cross the first chosen gap. Treating
  // the long Repeat's cut weight as four lanes would incorrectly produce256.
  expect(meta.rows[0].gapAfter).toBe(224);
  expect(meta.rows.slice(1, -1).map((row) => row.gapAfter)).toEqual([160, 160]);
  expect(meta.rows[meta.rows.length - 1].gapAfter).toBe(0);
  expect(result.edges.flatMap((link) => link.data ? [link.data.edge] : [])).toEqual(g.edges);
  expect(g).toEqual(before);
});
