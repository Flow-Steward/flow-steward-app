import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import { buildCanvasLayout, type ForEachNodeData, type StepNodeData } from "./layout";
import { inlineLoopBody, inlineLoopEligible, inlineLoopIds, setInlineLoop } from "./canvas-inline-loop-layout";

const graph: WorkflowCanvasGraph = {
  workflow: { id: "orders", display_name: "Orders", enabled: false, callable_by_parent: false },
  interface_contract: {}, output_contract: {},
  entry: { kind: "for_each", id: "lines" },
  groups: [],
  for_each: [{ id: "lines", display_name: "Each line item", over: "state.order.line_items", item_name: "line", body: { kind: "sequence", entry: { kind: "step", id: "reserve" } } }],
  steps: [
    { id: "normalise", step_id: "normalise", kind: "transform", display_name: "Normalise SKU", config_summary: "line.sku → line.sku_clean", for_each_id: "lines", ports: [{ outcome: { name: "success" }, label: "Next" }] },
    { id: "reserve", step_id: "reserve", kind: "connector", display_name: "Reserve stock", config_summary: "inventory.shopify → reserve_item", for_each_id: "lines", ports: [{ outcome: { name: "success" }, label: "Next" }] },
  ],
  edges: [
    { source: { kind: "step", id: "reserve" }, outcome: { name: "success" }, target: { kind: "step", id: "normalise" } },
    { source: { kind: "step", id: "normalise" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "lines", lane_id: null } },
  ],
  validation_issues: [], automation_rules: [],
};
const handlers = { onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() };

afterEach(() => localStorage.clear());

describe("Inline loop", () => {
  it("keeps the presentation per workflow in this browser", () => {
    expect(inlineLoopIds("orders").has("lines")).toBe(false);
    setInlineLoop("orders", "lines", true);
    expect(inlineLoopIds("orders").has("lines")).toBe(true);
    expect(inlineLoopIds("other").has("lines")).toBe(false);
    setInlineLoop("orders", "lines", false);
    expect(inlineLoopIds("orders").size).toBe(0);
  });

  it("lists the body from its entry along Next and accepts only a few simple steps", () => {
    const loop = graph.for_each![0];
    expect(inlineLoopBody(loop, graph.steps, graph.edges).map((step) => step.name)).toEqual(["Reserve stock", "Normalise SKU"]);
    expect(inlineLoopEligible(loop, graph.steps, graph.for_each!)).toBe(true);
    expect(inlineLoopEligible(loop, [...graph.steps, { id: "branch", kind: "if", display_name: "Branch", config_summary: "", for_each_id: "lines" }], graph.for_each!)).toBe(false);
    expect(inlineLoopEligible({ ...loop, body: { kind: "lanes", lanes: [] } }, graph.steps, graph.for_each!)).toBe(false);
  });

  it("draws an inline For each as one step-sized card and hides its body nodes and lines", () => {
    const { nodes, edges } = buildCanvasLayout({ canvasGraph: graph, ...handlers, inlineLoopIds: new Set(["lines"]) });
    const card = nodes.find((node) => node.id === "for_each:lines")!;
    expect(card.type).toBe("inlineLoop");
    expect(card.width).toBe(256);
    expect((card.data as ForEachNodeData).bodySteps?.map((step) => step.id)).toEqual(["reserve", "normalise"]);
    expect(card.handles?.find((handle) => handle.id === "entry")?.y).toBe(19);
    expect(nodes.filter((node) => node.parentId === "for_each:lines").every((node) => node.hidden)).toBe(true);
    expect(edges.filter((edge) => edge.id !== "visual:entry").every((edge) => edge.hidden)).toBe(true);
  });

  it("keeps the ordinary group drawing, with an end circle where a member ends the item", () => {
    const { nodes, edges } = buildCanvasLayout({ canvasGraph: graph, ...handlers });
    expect(nodes.find((node) => node.id === "for_each:lines")?.type).toBe("forEachContainer");
    const ending = nodes.find((node) => node.id === "normalise")!.data as StepNodeData;
    expect(ending.bodyEnds).toEqual([expect.objectContaining({ handle: "success", kind: "item" })]);
    expect(edges.find((edge) => edge.target === "for_each:lines" && edge.targetHandle === "exit")?.hidden).toBe(true);
    expect(edges.find((edge) => edge.source === "reserve")?.hidden).toBeUndefined();
  });
});
