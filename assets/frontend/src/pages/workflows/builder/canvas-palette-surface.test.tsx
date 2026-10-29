import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { WorkflowCanvasPaletteSurface } from "./canvas-palette-surface";
import type { BuilderSurfaceProps } from "./canvas-types";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";

vi.mock("./canvas", () => ({
  WorkflowCanvasBuilderSurface: (props: { onOpenNewStep: (context: { groupId: string; laneId: string }) => void }) => (
    <button type="button" onClick={() => props.onOpenNewStep({ groupId: "delivery", laneId: "lane_b" })}>
      Open lane palette
    </button>
  ),
}));

const graph: WorkflowCanvasGraph = {
  workflow: { id: "orders", display_name: "Orders", enabled: false, callable_by_parent: false },
  interface_contract: {},
  output_contract: {},
  entry: null,
  groups: [{
    id: "delivery",
    display_name: "Delivery",
    body: { kind: "lanes", lanes: [{ lane_id: "lane_a" }, { lane_id: "lane_b" }] },
  }],
  for_each: [],
  steps: [],
  edges: [],
  validation_issues: [],
  automation_rules: [],
};

const props = {
  advancedMode: false,
  canvasGraph: graph,
  changeSet: {} as BuilderSurfaceProps["changeSet"],
  onToggleAdvancedMode: vi.fn(),
  onCloseInspector: vi.fn(),
  onConnectPort: vi.fn(),
  onDisconnectPort: vi.fn(),
  onInsertStep: vi.fn(),
  onOpenGroup: vi.fn(),
  onOpenGroupAutomation: vi.fn(),
  onOpenNewGroup: vi.fn(),
  onOpenNewStep: vi.fn(),
  onOpenStep: vi.fn(),
  onOpenStepAutomation: vi.fn(),
  onOpenWorkflowAutomation: vi.fn(),
  onOpenWorkflowSettings: vi.fn(),
  onValidationIssue: vi.fn(),
  workflowId: "orders",
} as unknown as BuilderSurfaceProps;

it("uses the ordinal lane label in the Add step subtitle", () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><WorkflowCanvasPaletteSurface {...props} /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Open lane palette" }));

  expect(screen.getByText("In Delivery · Parallel lane 2")).toBeVisible();
  expect(screen.queryByText(/lane_b/u)).not.toBeInTheDocument();
});
