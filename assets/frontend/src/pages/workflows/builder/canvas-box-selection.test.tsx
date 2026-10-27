import { act, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import type * as ReactFlowModule from "@xyflow/react";
import { MemoryRouter } from "react-router";
import { WorkflowCanvasBuilderSurface } from "./canvas";
import { definitionFromWorkflow } from "./change-set";
import { useWorkflowChangeSet } from "../detail/detail-change-set";
import type { WorkflowCanvasGraph } from "../shared/api";

type FlowProps = ComponentProps<typeof ReactFlowModule.ReactFlow>;
const captured = vi.hoisted(() => ({ props: null as FlowProps | null }));
vi.mock("@xyflow/react", async (importOriginal) => {
  const actual = await importOriginal<typeof ReactFlowModule>();
  return { ...actual, ReactFlow: (props: FlowProps) => { captured.props = props; return <div data-testid="flow" />; } };
});

const ids = ["alpha", "beta", "gamma"] as const;
const graph: WorkflowCanvasGraph = {
  workflow: { id: "box_selection", display_name: "Box selection", enabled: false, callable_by_parent: false },
  interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true },
  output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
  entry: { kind: "step", id: "alpha" }, groups: [], for_each: [],
  steps: ids.map((id) => ({ id, step_id: id, kind: "set_variable", display_name: id[0].toUpperCase() + id.slice(1), config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] })),
  edges: ids.slice(1).map((id, index) => ({ source: { kind: "step", id: ids[index] }, outcome: { name: "success" }, target: { kind: "step", id } })),
  automation_rules: [], validation_issues: [],
};
const definition = definitionFromWorkflow({
  workflow_id: "box_selection", revision: 1,
  steps: graph.steps.map((item) => ({ step_id: item.id, step_kind: item.kind, display_name: item.display_name })),
  settings: { execution_graph: { schema_version: 1, entry: graph.entry, edges: graph.edges } },
});
const noop = () => undefined;
function Host() {
  const changeSet = useWorkflowChangeSet({ workflowId: "box_selection", serverDefinition: definition, serverRevision: 1, refetchWorkflowSurfaces: async () => undefined });
  return <MemoryRouter>
    <WorkflowCanvasBuilderSurface advancedMode canvasGraph={graph} changeSet={changeSet} workflowId="box_selection"
      onCloseInspector={noop} onConnectPort={() => false} onDisconnectPort={noop} onInsertStep={noop}
      onOpenGroup={noop} onOpenGroupAutomation={noop} onOpenNewGroup={noop} onOpenNewStep={noop}
      onOpenStep={noop} onOpenStepAutomation={noop} onOpenWorkflowAutomation={noop}
      onOpenWorkflowSettings={noop} onToggleAdvancedMode={noop} onValidationIssue={noop} />
  </MemoryRouter>;
}
const flow = () => captured.props!;
const edgeId = "edge:step:alpha:success";

beforeEach(() => { captured.props = null; vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} }); });
afterEach(() => { vi.unstubAllGlobals(); });

it("keeps Group… on the selection bar when a box selection also catches a connection", () => {
  render(<Host />);
  // React Flow's box selection selects the steps and the connections it crosses, in one gesture.
  act(() => flow().onSelectionStart?.({} as never));
  act(() => flow().onNodesChange?.([{ type: "select", id: "alpha", selected: true }, { type: "select", id: "beta", selected: true }]));
  act(() => flow().onEdgesChange?.([{ type: "select", id: edgeId, selected: true }]));
  act(() => flow().onSelectionEnd?.({} as never));
  const selected = screen.getByRole("toolbar", { name: "Selected items" });
  expect(within(selected).getByText("2 selected")).toBeVisible();
  expect(within(selected).getByRole("button", { name: "Group…" })).toBeVisible();
  expect(screen.queryByRole("region", { name: "Connection selected" })).not.toBeInTheDocument();
});

it("drops a connection chosen before the box selection starts", () => {
  render(<Host />);
  act(() => flow().onEdgesChange?.([{ type: "select", id: edgeId, selected: true }]));
  expect(screen.getByRole("region", { name: "Connection selected" })).toBeVisible();
  act(() => flow().onSelectionStart?.({} as never));
  act(() => flow().onNodesChange?.([{ type: "select", id: "alpha", selected: true }, { type: "select", id: "beta", selected: true }]));
  act(() => flow().onSelectionEnd?.({} as never));
  expect(screen.queryByRole("region", { name: "Connection selected" })).not.toBeInTheDocument();
  expect(within(screen.getByRole("toolbar", { name: "Selected items" })).getByRole("button", { name: "Group…" })).toBeVisible();
});

it("still selects a connection on its own outside a box selection", () => {
  render(<Host />);
  act(() => flow().onEdgesChange?.([{ type: "select", id: edgeId, selected: true }]));
  expect(screen.getByRole("region", { name: "Connection selected" })).toBeVisible();
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
});
