import { render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import type { HookExecutionMark } from "@/pages/workflows/detail/detail-hook-execution-marks";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import { buildCanvasLayout, type StepNodeData } from "./layout";
import { GenericStepNode, TerminalStepNode } from "./canvas-step-nodes";
import { CanvasRuntimeMark } from "./canvas-runtime-mark";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

const graph: WorkflowCanvasGraph = {
  workflow: { id: "author", display_name: "Author", enabled: true, callable_by_parent: false },
  interface_contract: {}, output_contract: {}, entry: { kind: "step", id: "author_step" },
  groups: [{ id: "hook:before", display_name: "Before" }],
  steps: [
    { id: "author_step", kind: "set_variable", display_name: "Author step", config_summary: "", ports: [] },
    { id: "hook:before:action", kind: "set_variable", display_name: "Child action", group_id: "hook:before", config_summary: "", ports: [] },
  ], edges: [], validation_issues: [], automation_rules: [],
};

function layout(runtimeMarks: Record<string, HookExecutionMark>) {
  const options = { canvasGraph: graph, runtimeMarks, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() };
  return buildCanvasLayout(options);
}

it("passes execution evidence to exactly the projected child node it belongs to", () => {
  const { nodes } = layout({ "hook:before:action": { status: "completed", jobId: "child-job", stepExecutionId: "execution-1", occurrence: 2 } });
  expect(nodes.find((node) => node.id === "hook:before:action")?.data).toMatchObject({ runtimeMark: { status: "completed", jobId: "child-job", stepExecutionId: "execution-1", occurrence: 2 } });
  expect(nodes.find((node) => node.id === "author_step")?.data).not.toHaveProperty("runtimeMark");
});

it.each([
  { status: "in_progress", durationMs: undefined, label: "Running", run: "running" },
  { status: "completed", durationMs: 400, label: "0.4 s", run: "done" },
  { status: "completed", durationMs: 40, label: "< 0.1 s", run: "done" },
  { status: "failed", durationMs: 900, label: "Failed", run: "failed" },
  { status: "awaiting_approval", durationMs: undefined, label: "Waiting", run: "waiting" },
])("marks the card's top edge with the recorded $status result on generic and terminal cards", ({ status, durationMs, label, run }) => {
  const data = {
    step: graph.steps[1], runtimeMark: { status, durationMs, jobId: "child-job", occurrence: 2 },
    automationRules: [], isEntry: false, local: false, unreachable: false, validationIssues: [],
    onAutomation: vi.fn(), onOpenStep: vi.fn(), onEditStep: vi.fn(),
  } as StepNodeData & { runtimeMark: HookExecutionMark };
  const props = { id: data.step.id, data, type: "action", selected: false, dragging: false, isConnectable: false, zIndex: 1, selectable: true, draggable: true, deletable: true, positionAbsoluteX: 0, positionAbsoluteY: 0 };
  render(<ReactFlowProvider>
    <GenericStepNode {...props} />
    <TerminalStepNode {...props} id="hook:before:stop" data={{ ...data, step: { ...data.step, id: "hook:before:stop", kind: "stop_success" } }} />
  </ReactFlowProvider>);
  const marks = screen.getAllByTestId("workflow-runtime-mark");
  expect(marks).toHaveLength(2);
  for (const mark of marks) {
    expect(mark).toHaveTextContent(label);
    expect(mark).toHaveAttribute("data-job-id", "child-job");
    expect(mark).toHaveAttribute("data-run", run);
    expect(mark).toHaveAttribute("title", `Test Run · ${label}`);
  }
  for (const card of screen.getAllByRole("group", { name: "Open step Child action" })) expect(card).toHaveAttribute("data-run", run);
});

it("says how often a step ran inside a loop, and when its kept output stood in", () => {
  render(<>
    <CanvasRuntimeMark mark={{ status: "completed", jobId: "run", passes: 2, durationMs: 300, totalDurationMs: 600 }} />
    <CanvasRuntimeMark mark={{ status: "completed", jobId: "run", passes: 2, kept: true }} />
    <CanvasRuntimeMark mark={{ status: "completed", jobId: "run", kept: true }} />
  </>);
  expect(screen.getByText("2× · 0.6 s")).toBeVisible();
  expect(screen.getByText("2× · Kept")).toBeVisible();
  expect(screen.getByText("Kept")).toBeVisible();
});

it("localizes the run words on the mark", () => {
  const messages: Record<string, string> = { "react.workflows.test_run_failed": "Nepavyko" };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages,
    t: (key, defaultText = "", params) => interpolate(messages[key] || defaultText, params) }}>
    <CanvasRuntimeMark mark={{ status: "failed", jobId: "run" }} />
  </I18nContext.Provider>);
  expect(screen.getByText("Nepavyko")).toBeVisible();
});

function runtimeEdgeSvg(selector: string) {
  const svg = screen.getByTestId("runtime-edge-svg");
  // Exact line/arrow paint has no accessible role; stay inside the Testing Library located SVG.

  return svg.querySelector(selector);
}

it.each([false, true])("paints a taken error edge line and arrow with success, selected=%s", async (selected) => {
  const { CanvasEditableEdge } = await import("./canvas-step-nodes");
  const { Position } = await import("@xyflow/react");
  const { edgeKey } = await import("./api");
  const { runtimeEdgeKey } = await import("@/pages/jobs/detail/job-execution-trace");
  const edge = { source: { kind: "step" as const, id: "author_step" }, outcome: { name: "error" as const }, target: { kind: "step" as const, id: "hook:before:action" } };
  const result = buildCanvasLayout({ canvasGraph: { ...graph, edges: [edge] }, runtimeEdges: new Set([runtimeEdgeKey(edge)]),
    onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const renderedEdge = result.edges.find((candidate) => candidate.data?.edgeKey === edgeKey(edge))!;
  expect(renderedEdge.animated).toBe(false);
  render(<ReactFlowProvider><svg data-testid="runtime-edge-svg"><CanvasEditableEdge id={renderedEdge.id} source="author_step" target="hook:before:action"
    sourceX={0} sourceY={0} targetX={300} targetY={100} sourcePosition={Position.Right} targetPosition={Position.Left}
    data={{ ...renderedEdge.data!, selected }} style={renderedEdge.style} /></svg></ReactFlowProvider>);
  const line = runtimeEdgeSvg(".react-flow__edge-path");
  expect(line).toHaveStyle({ stroke: "var(--fs-workflow-edge-ran)" });
  expect(line).toHaveStyle({ strokeWidth: selected ? "var(--fs-workflow-edge-width-selected)" : "var(--fs-workflow-edge-width)" });
  // No arrowhead: the line ends in the input's circle (Connections › Kinds).
  expect(runtimeEdgeSvg(".workflow-edge-arrow")).toBeNull();
  expect(runtimeEdgeSvg(".animated")).toBeNull();
});

it("says how often the run took a connection when it took it more than once", async () => {
  const { edgeKey } = await import("./api");
  const { runtimeEdgeKey } = await import("@/pages/jobs/detail/job-execution-trace");
  const edge = { source: { kind: "step" as const, id: "author_step" }, outcome: { name: "success" as const }, target: { kind: "step" as const, id: "hook:before:action" } };
  const twice = buildCanvasLayout({ canvasGraph: { ...graph, edges: [edge] }, runtimeEdges: new Map([[runtimeEdgeKey(edge), 2]]),
    onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const once = buildCanvasLayout({ canvasGraph: { ...graph, edges: [edge] }, runtimeEdges: new Map([[runtimeEdgeKey(edge), 1]]),
    onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const twiceEdge = twice.edges.find((candidate) => candidate.data?.edgeKey === edgeKey(edge));
  expect(twiceEdge?.data).toMatchObject({ taken: true, passes: 2 });
  expect(once.edges.find((candidate) => candidate.data?.edgeKey === edgeKey(edge))?.data).not.toHaveProperty("passes");
});
