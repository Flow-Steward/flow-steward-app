import { applyCanvasNodeState } from "./canvas-state-helpers";
import { expect, it, vi } from "vitest";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import { buildCanvasLayout } from "./layout";

it("keeps branches neutral and static, reserving danger and dashed routing for errors", () => {
  const canvasGraph: WorkflowCanvasGraph = {
    workflow: { id: "edges", display_name: "Edges", enabled: false, callable_by_parent: false },
    interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true },
    output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
    entry: null, groups: [], steps: [], validation_issues: [], automation_rules: [],
    edges: (["true", "false", "error"] as const).map((name) => ({ source: { kind: "step", id: "source" }, target: { kind: "step", id: "target" }, outcome: { name } })),
  };
  const { edges } = buildCanvasLayout({ canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenNewStep: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  expect(edges).toHaveLength(3);
  for (const edge of edges) {
    expect(edge.animated).toBe(false);
    expect(edge.style?.strokeWidth).toBe("var(--fs-workflow-edge-width)");
    expect(edge.style?.stroke).toBe(edge.sourceHandle === "error" ? "var(--fs-workflow-edge-failure)" : "var(--fs-workflow-edge)");
  }
  expect(edges.find((edge) => edge.sourceHandle === "error")?.style?.strokeDasharray).toBe("var(--fs-workflow-edge-dash)");
});

it("exports logical column jumps only within the same actual scope, independently of manual X", () => {
  const step = (id: string, group_id?: string) => ({ id, step_id: id, group_id, kind: "set_variable", display_name: id, node_type: "action", config_summary: "", ports: [{ outcome: { name: "success" as const }, label: "Next" }] });
  const edge = (source: string, target: string) => ({ source: { kind: "step" as const, id: source }, target: { kind: "step" as const, id: target }, outcome: { name: "success" as const } });
  const canvasGraph: WorkflowCanvasGraph = { workflow: { id: "columns", display_name: "Columns", enabled: false, callable_by_parent: false }, interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true }, output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] }, entry: { kind: "step", id: "a" }, groups: [{ id: "once", display_name: "Once", body: { kind: "sequence", entry: { kind: "step", id: "i" } } }], steps: [step("a"), step("b"), step("c"), step("i", "once"), step("j", "once"), step("k", "once")], edges: [edge("a", "b"), edge("b", "c"), edge("a", "c"), edge("i", "j"), edge("j", "k"), edge("a", "k")], automation_rules: [], validation_issues: [] };
  const view = buildCanvasLayout({ canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenNewStep: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const jump = view.edges.find((candidate) => candidate.source === "a" && candidate.target === "c")!;
  const foreign = view.edges.find((candidate) => candidate.source === "a" && candidate.target === "k")!;
  expect(jump.data?.routing).toMatchObject({ sameScope: true, sourceColumn: 0, targetColumn: 2, longJump: true });
  expect(foreign.data?.routing).toMatchObject({ sameScope: false, longJump: false });
  const moved = applyCanvasNodeState({ nodes: view.nodes, manualPositions: { c: { x: 96, y: 24 } }, highlightedNodeId: null, connectionHelpActive: false });
  expect(moved.find((node) => node.id === "c")?.position.x).toBe(96);
  expect(jump.data?.routing?.longJump).toBe(true);
});
