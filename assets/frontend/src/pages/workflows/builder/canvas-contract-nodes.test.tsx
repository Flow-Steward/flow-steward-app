import { fireEvent, render, screen, within } from "@testing-library/react";
import { ReactFlow, ReactFlowProvider, type NodeProps, type Node } from "@xyflow/react";
import { GenericStepNode, IfStepNode, TerminalStepNode, WorkflowStartNode } from "./canvas-step-nodes";
import { ContainerCard } from "./canvas-container-card";
import { GroupNode } from "./canvas-group-controls";
import { suppressCanvasNodeClick, consumeCanvasNodeClickSuppression } from "./canvas-core";
import { buildCanvasLayout, type StepNodeData, type StartNodeData, type GroupNodeData } from "./layout";
import { GROUP_HEADER_HEIGHT } from "./layout-constants";
import type { WorkflowCanvasGraph } from "../shared/api";

beforeEach(() => vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} }));
afterEach(() => vi.unstubAllGlobals());

const step: StepNodeData = {
  step: { id: "technical_id", step_id: "technical_id", kind: "agent", display_name: "Classify request", config_summary: "agent: triage_operator", ports: [{ outcome: { name: "success" }, label: "Next" }] },
  automationRules: [], validationIssues: [], isEntry: false, local: false, unreachable: false,
  onAutomation: vi.fn(), onOpenStep: vi.fn(), onEditStep: vi.fn(),
};
function stepProps(data: StepNodeData, selected = false): NodeProps<Node<StepNodeData>> {
  return { id: data.step.id, data, type: "action", selected, dragging: false, isConnectable: false, zIndex: 1, selectable: true, draggable: true, deletable: true, positionAbsoluteX: 0, positionAbsoluteY: 0 };
}
function renderStep(data = step) {
  return render(<ReactFlowProvider><GenericStepNode {...stepProps(data)} /></ReactFlowProvider>);
}
it("shows a useful title and summary without technical identity or empty utility actions", () => {
  renderStep();
  expect(screen.getByText("Classify request")).toBeVisible();
  expect(screen.getByText("agent: triage_operator")).toBeVisible();
  expect(screen.queryByText("technical_id")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Edit step/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "auto" })).not.toBeInTheDocument();
});
it("names configured rules rather than rendering an unexplained count", () => {
  renderStep({ ...step, step: { ...step.step, automation_summary: { rule_count: 2, enabled_count: 2, issue_count: 0, error_count: 0, warning_count: 0 } } });
  expect(screen.getByRole("button", { name: "2 rules" })).toBeVisible();
});
it("keeps If outcomes in human reading order", () => {
  const data: StepNodeData = { ...step, step: { ...step.step, kind: "if", ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }] } };
  render(<ReactFlowProvider><IfStepNode {...stepProps(data)} /></ReactFlowProvider>);
  const rows = screen.getAllByRole("listitem");
  expect(rows.map((row) => row.textContent)).toEqual(["True", "False"]);
});
it("opens Interface Inputs from Start", () => {
  const onOpenInputs = vi.fn();
  const data: StartNodeData = { workflowId: "router", workflowEnabled: true, triggerChannels: [], inputs: ["order_id*"], empty: false, onOpenInputs };
  render(<ReactFlowProvider><WorkflowStartNode {...({ data } as NodeProps<Node<StartNodeData>>)} /></ReactFlowProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Start — workflow inputs" }));
  expect(onOpenInputs).toHaveBeenCalledOnce();
  expect(screen.getByLabelText("Required")).toBeInTheDocument();
  expect(screen.getByText("* Required")).toBeInTheDocument();
});

it("keeps More to Duplicate, Disable and Delete: a card without a canvas offers Delete alone", () => {
  const onEditStep = vi.fn();
  const onDeleteStep = vi.fn();
  const data = { ...step, step: { ...step.step, kind: "stop_success" }, onEditStep, onDeleteStep };
  render(<ReactFlowProvider><TerminalStepNode {...stepProps(data, true)} /></ReactFlowProvider>);
  fireEvent.click(screen.getByRole("button", { name: "More actions for Classify request" }));
  expect(screen.queryByRole("menuitem", { name: /Edit/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
  expect(onDeleteStep).toHaveBeenCalledWith("technical_id");
  expect(onEditStep).not.toHaveBeenCalled();
});

it("opens Workflow interface › Outputs from a Stop · success card's outputs", () => {
  const onOpenInterfaceSection = vi.fn();
  const data: StepNodeData = { ...step, step: { ...step.step, kind: "stop_success" }, contractSummary: { outputs: ["receipt"], artifacts: ["invoice_pdf"] }, onOpenInterfaceSection };
  render(<ReactFlowProvider><TerminalStepNode {...stepProps(data)} /></ReactFlowProvider>);
  fireEvent.click(screen.getByRole("button", { name: /Outputs/ }));
  expect(onOpenInterfaceSection).toHaveBeenCalledWith("outputs");
  // A returned file is listed with the outputs; nothing opens Artifacts or External changes.
  expect(screen.getByText("invoice_pdf")).toBeVisible();
  expect(screen.queryByRole("button", { name: /Artifacts|External changes/ })).not.toBeInTheDocument();
});

it("says Disabled on a disabled step and drops the Selected, optional and lane badges", () => {
  render(<ReactFlowProvider><GenericStepNode {...stepProps({ ...step, step: { ...step.step, disabled: true, optional: true, lane_id: "email" } }, true)} /></ReactFlowProvider>);
  expect(screen.getByText("Disabled")).toBeVisible();
  expect(screen.getByRole("group", { name: "Open step Classify request" })).toHaveAttribute("data-disabled", "true");
  expect(screen.queryByText("Selected")).not.toBeInTheDocument();
  expect(screen.queryByText("optional")).not.toBeInTheDocument();
  expect(screen.queryByText(/lane:/)).not.toBeInTheDocument();
});


it.each(["group", "for_each"] as const)("lets Shift+click on a %s header through to the canvas selection and opens nothing", (containerKind) => {
  // Canvas › Selecting: Shift+click adds a node to the selection, a group included.
  const onOpen = vi.fn();
  const onCanvasClick = vi.fn();
  const Renderer = () => <ContainerCard containerId="work" containerKind={containerKind} displayName="Open once" badges={null} data={{ automationRules: [], isEntry: false, lanes: [], local: false, ports: [], unreachable: false, validationIssues: [] }} onOpen={onOpen} onAddStep={vi.fn()} />;
  consumeCanvasNodeClickSuppression();
  render(<ReactFlowProvider><ReactFlow width={800} height={600} nodes={[{ id: "work", type: "container", position: { x: 0, y: 0 }, width: 352, height: 224, data: {} }]} nodeTypes={{ container: Renderer }} onNodeClick={onCanvasClick} /></ReactFlowProvider>);
  const header = screen.getByRole("button", { name: containerKind === "for_each" ? /^For each item ?Open once$/ : "Open once" });
  fireEvent.click(header, { detail: 1, clientX: 10, clientY: 10, shiftKey: true });
  expect(onOpen).not.toHaveBeenCalled();
  expect(onCanvasClick).toHaveBeenCalledOnce();
});

it.each(["group", "for_each"] as const)("opens a %s header once for one bubbling pointer activation and preserves keyboard/secondary/drag guards", (containerKind) => {
  const onOpen = vi.fn();
  const onCanvasClick = vi.fn();
  const onAdd = vi.fn();
  const Renderer = () => <ContainerCard containerId="work" containerKind={containerKind} displayName="Open once" badges={null} data={{ automationRules: [], isEntry: false, lanes: [], local: false, ports: [], unreachable: false, validationIssues: [] }} onOpen={onOpen} onAddStep={onAdd} />;
  consumeCanvasNodeClickSuppression();
  render(<ReactFlowProvider><ReactFlow width={800} height={600} nodes={[{ id: "work", type: "container", position: { x: 0, y: 0 }, width: 352, height: 224, data: {} }]} nodeTypes={{ container: Renderer }} onNodeClick={onCanvasClick} /></ReactFlowProvider>);
  // A For each names its kind before its title; the header holds no Move chip.
  const header = screen.getByRole("button", { name: containerKind === "for_each" ? /^For each item ?Open once$/ : "Open once" });
  const pointer = (type: string, x: number, y: number) => fireEvent(header, new MouseEvent(type, { bubbles: true, button: 0, clientX: x, clientY: y }));
  pointer("pointerdown", 10, 10);
  pointer("pointerup", 10, 10);
  fireEvent.click(header, { detail: 1, clientX: 10, clientY: 10 });
  expect(onOpen).toHaveBeenCalledOnce();
  expect(onCanvasClick).not.toHaveBeenCalled();
  onOpen.mockClear();
  pointer("pointerdown", 10, 10);
  pointer("pointerup", 50, 50);
  fireEvent.click(header, { detail: 1, clientX: 50, clientY: 50 });
  expect(onOpen).not.toHaveBeenCalled();
  suppressCanvasNodeClick(1200);
  pointer("pointerdown", 10, 10);
  pointer("pointerup", 10, 10);
  fireEvent.click(header, { detail: 1, clientX: 10, clientY: 10 });
  expect(onOpen).not.toHaveBeenCalled();
  // No Move chip: the header itself drags the group.
  expect(screen.queryByText("Move")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add step to Open once" }));
  expect(onAdd).toHaveBeenCalledExactlyOnceWith("");
  expect(onOpen).not.toHaveBeenCalled();
  fireEvent.keyDown(header, { key: "Enter" });
  fireEvent.keyDown(header, { key: " " });
  expect(onOpen).toHaveBeenCalledTimes(2);
});

it("keeps declared Start names authoritative over legacy suffix compatibility", () => {
 const data: StartNodeData = { workflowId: "typed", workflowEnabled: true, triggerChannels: [], inputs: ["legacy*"], inputSignatures: [{ name: "literal*", type: "string", required: false }, { name: "declared", type: "secret", required: true }], empty: false, onOpenInputs: vi.fn() };
 render(<ReactFlowProvider><WorkflowStartNode {...({ data } as NodeProps<Node<StartNodeData>>)} /></ReactFlowProvider>);
 const literal = screen.getByText("literal*", { exact: true });
 expect(literal).toBeVisible();
 expect(within(literal).queryByLabelText("Required")).not.toBeInTheDocument();
 expect(screen.getAllByLabelText("Required")).toHaveLength(1);
 expect(screen.getByText("declared", { exact: true })).toBeVisible();
 expect(screen.queryByText("legacy", { exact: true })).not.toBeInTheDocument();
});


it.each(["group", "for_each"] as const)("renders %s entry and ordinary Next centers at 24px matching static handles while preserving the 96px header", (kind) => {
  const canvasGraph: WorkflowCanvasGraph = { workflow: { id: "ports", display_name: "Ports", enabled: false, callable_by_parent: false }, interface_contract: {}, output_contract: {}, entry: { kind, id: "work" }, steps: [], groups: kind === "group" ? [{ id: "work", display_name: "Work", body: { kind: "sequence" } }] : [], for_each: kind === "for_each" ? [{ id: "work", display_name: "Work", over: "state.items", item_name: "item", body: { kind: "sequence" } }] : [], edges: [], validation_issues: [], automation_rules: [] };
  const result = buildCanvasLayout({ canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
  const node = result.nodes.find((candidate) => candidate.id === `${kind}:work`)!;
  render(<ReactFlowProvider><ContainerCard containerId="work" containerKind={kind} displayName="Work" badges={null} data={{ automationRules: [], isEntry: true, lanes: [], local: false, ports: [], unreachable: false, validationIssues: [] }} onOpen={vi.fn()} /></ReactFlowProvider>);
  const input = screen.getByTitle(kind === "group" ? "Group entry" : "For each entry");
  const next = screen.getByTitle("Next");
  expect(input).toHaveStyle({ top: "24px", left: "0px", width: "10px", height: "10px" });
  expect(next).toHaveStyle({ top: "24px", right: "0px", width: "10px", height: "10px" });
  for (const [handleId, rendered] of [["entry", input], ["success", next]] as const) {
    const handle = node.handles!.find((candidate) => candidate.id === handleId)!;
    expect(handle.y + handle.height! / 2).toBe(Number.parseFloat(rendered.style.top));
  }
  expect(GROUP_HEADER_HEIGHT).toBe(96);
  // The header itself is the drag handle now: no Move chip in its name.
  expect(screen.getByRole("button", { name: /Work Entry$/ })).toHaveClass("workflow-container-header", "group-drag-handle");
});


function renderHookAddMenu(declarationPending = false) {
  const data: GroupNodeData = {
    group: { id: "hook:before", display_name: "Adjust order", body: { kind: "sequence" }, hook: { hookId: "before", stepId: "declare_before", displayName: "Adjust order", connected: true, targetWorkflowId: "client", targetName: "Client adjustments", usedIn: 1, declarationPending } },
    readOnly: true, automationRules: [], isEntry: false, lanes: [], local: false, ports: [], unreachable: false, validationIssues: [],
    onAutomation: vi.fn(), onOpenGroup: vi.fn(), onOpenNewStep: vi.fn(), onOpenNewGroup: vi.fn(),
  };
  const view = render(<ReactFlowProvider><GroupNode data={data} id="group:hook:before" type="groupContainer" selected={false} dragging={false} isConnectable={false} zIndex={0} selectable draggable deletable positionAbsoluteX={0} positionAbsoluteY={0} /></ReactFlowProvider>);
  return { ...view, data };
}

it.each(["Step", "Group"])("portals the Hook %s choice outside the node owner and keeps its exact child command", (label) => {
  const view = renderHookAddMenu();
  const trigger = screen.getByRole("button", { name: "Add step to Adjust order" });
  fireEvent.click(trigger);
  const menu = screen.getByRole("dialog", { name: "Add step to Adjust order" });
  const frame = screen.getByText((_, element) => element?.getAttribute("data-hook-frame") === "before");
  expect(within(frame).queryByRole("dialog", { name: "Add step to Adjust order" })).not.toBeInTheDocument();
  expect(trigger).toHaveAttribute("aria-controls", menu.id);
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  expect(within(menu).getByText("A step in this hook.", { exact: true })).toBeInTheDocument();
  expect(within(menu).queryByText("A step in the workflow root.")).not.toBeInTheDocument();
  // For each is a step (Step → Flow), not a structure choice here.
  expect(within(menu).getAllByRole("button").map((button) => button.firstChild?.textContent)).toEqual(["Step", "Group"]);
  const hint = label === "Step" ? "A step in this hook." : "Steps that belong together. Can become a child workflow.";
  fireEvent.click(within(menu).getByRole("button", { name: `${label}${hint}` }));
  if (label === "Step") {
    expect(view.data.onOpenNewStep).toHaveBeenCalledExactlyOnceWith({ groupId: "hook:before", laneId: "" });
    expect(view.data.onOpenNewGroup).not.toHaveBeenCalled();
  } else {
    expect(view.data.onOpenNewGroup).toHaveBeenCalledExactlyOnceWith({ containerKind: "group", hookId: "before" });
    expect(view.data.onOpenNewStep).not.toHaveBeenCalled();
  }
  expect(view.data.onOpenGroup).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog", { name: "Add step to Adjust order" })).not.toBeInTheDocument();
});

it("focuses the Hook structure choice and dismisses Escape back to the same Add trigger without a command", () => {
  const view = renderHookAddMenu();
  const trigger = screen.getByRole("button", { name: "Add step to Adjust order" });
  fireEvent.click(trigger);
  const menu = screen.getByRole("dialog", { name: "Add step to Adjust order" });
  const step = within(menu).getByRole("button", { name: "StepA step in this hook." });
  expect(step).toHaveFocus();
  fireEvent.keyDown(step, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "Add step to Adjust order" })).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  expect(view.data.onOpenNewStep).not.toHaveBeenCalled();
  expect(view.data.onOpenNewGroup).not.toHaveBeenCalled();
});

it("keeps an unapplied Hook declaration Add disabled even on the protected parent canvas", () => {
  const view = renderHookAddMenu(true);
  const trigger = screen.getByRole("button", { name: "Add step to Adjust order" });
  expect(trigger).toBeDisabled();
  fireEvent.click(trigger);
  expect(screen.queryByRole("dialog", { name: "Add step to Adjust order" })).not.toBeInTheDocument();
  expect(view.data.onOpenNewStep).not.toHaveBeenCalled();
  expect(view.data.onOpenNewGroup).not.toHaveBeenCalled();
});

it("labels lane headers by position instead of exposing persisted lane ids", () => {
  const data: GroupNodeData = {
    automationRules: [], group: { id: "delivery", display_name: "Delivery", body: { kind: "lanes" } },
    isEntry: false, lanes: ["lane_a", "lane_b"], local: false, ports: [], unreachable: false, validationIssues: [],
    onAutomation: vi.fn(), onOpenGroup: vi.fn(),
    laneFrames: [
      { id: "lane_a", x: 0, y: 0, width: 300, height: 180 },
      { id: "lane_b", x: 0, y: 190, width: 300, height: 180 },
    ],
  };
  render(<ReactFlowProvider><ContainerCard containerId="delivery" containerKind="group" displayName="Delivery" badges={null} data={data} onOpen={vi.fn()} /></ReactFlowProvider>);
  expect(screen.getByText("Parallel lane 1")).toBeVisible();
  expect(screen.getByText("Parallel lane 2")).toBeVisible();
  expect(screen.queryByText(/lane_[ab]/)).not.toBeInTheDocument();
});
