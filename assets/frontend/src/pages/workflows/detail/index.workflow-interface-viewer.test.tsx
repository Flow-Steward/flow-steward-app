import { fireEvent, screen, within } from "@testing-library/react";
import { mockedUseIsAdmin, mockedUseAuth, canvasGraphFixture, fetchSpy, mockedFetchWorkflowCanvasGraph, mockedFetchWorkflowDetail, mockedFetchWorkflowContract, renderPage, workflowDetailFixture } from "./index.test-helpers";

// jsdom has no canvas hit testing; interaction targets are explicit nodes.
const hitTestDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
beforeAll(() => Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [] }));
afterAll(() => {
  if (hitTestDescriptor) Object.defineProperty(document, "elementsFromPoint", hitTestDescriptor);
  else Reflect.deleteProperty(document, "elementsFromPoint");
});

it("lets a viewer inspect public Start and Stop contracts without edit controls or writes", async () => {
  mockedUseIsAdmin.mockReturnValue(false);
  mockedUseAuth.mockReturnValue({ session: { authenticated: true, user: { roles: ["viewer"] }, roles: ["viewer"] } } as ReturnType<typeof mockedUseAuth>);
  const contract = { enabled: true, input_mode: "fields", expected_inputs: [{ name: "login", type: "secret", default_configured: true, default: "must-not-render" }], output_mode: "variables", expected_outputs: [{ name: "result", type: "string" }] };
  mockedFetchWorkflowContract.mockResolvedValue({ workflow_id: "weekly_report", interface: contract, definition_issues: [] } as never);
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({
    revision: 5,
    settings: { callable: contract },
    steps: [{ step_id: "done", step_kind: "stop_success", display_name: "Done" }],
  }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ entry: { kind: "step", id: "done" }, steps: [{ id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] }], edges: [] }));
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
  expect(await screen.findByRole("heading", { name: "Workflow interface" })).toBeInTheDocument();
  let drawer = await screen.findByRole("dialog", { name: "Workflow interface" });
  expect(await within(drawer).findByRole("tab", { name: "Inputs" })).toHaveAttribute("aria-selected", "true");
  expect(within(drawer).getByRole("combobox", { name: "Input mode" })).toBeDisabled();
  expect(within(drawer).getByDisplayValue("login")).toBeDisabled();
  expect(within(drawer).getByPlaceholderText("Enter a new value to replace")).toHaveValue("");
  expect(within(drawer).getByPlaceholderText("Enter a new value to replace")).toBeDisabled();
  expect(within(drawer).queryByDisplayValue("must-not-render")).not.toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Save workflow settings" })).not.toBeInTheDocument();
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("group", { name: "Open step Done" }));
  drawer = await screen.findByRole("dialog", { name: "Workflow interface" });
  expect(await within(drawer).findByRole("tab", { name: "Outputs" })).toHaveAttribute("aria-selected", "true");
  expect(within(drawer).queryByRole("combobox", { name: "Output mode" })).not.toBeInTheDocument();
  expect(within(drawer).getByDisplayValue("result")).toBeDisabled();
  expect(fetchSpy.mock.calls.filter(([, init]) => init?.method && init.method !== "GET")).toHaveLength(0);
});

afterEach(() => vi.restoreAllMocks());

it("shows a viewer the inactive contract without any activation action or write", async () => {
  mockedUseIsAdmin.mockReturnValue(false);
  mockedUseAuth.mockReturnValue({ session: { authenticated: true, user: { roles: ["viewer"] }, roles: ["viewer"] } } as ReturnType<typeof mockedUseAuth>);
  const contract = { enabled: false, input_mode: "fields", expected_inputs: [], expected_outputs: [{ name: "summary", type: "string", required: true }] };
  mockedFetchWorkflowContract.mockResolvedValue({ workflow_id: "weekly_report", interface: contract, definition_issues: [] } as never);
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ settings: { callable: contract } }));
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
  const drawer = await screen.findByRole("dialog", { name: "Workflow interface" });
  expect(await within(drawer).findByText("Workflow interface is off")).toBeVisible();
  expect(within(drawer).queryByRole("button", { name: "Open workflow settings" })).not.toBeInTheDocument();
  expect(within(drawer).queryByRole("switch", { name: "Enable workflow interface" })).not.toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Save workflow settings" })).not.toBeInTheDocument();
  fireEvent.click(within(drawer).getByRole("tab", { name: "Outputs" }));
  expect(within(drawer).getByDisplayValue("summary")).toBeDisabled();
  expect(fetchSpy.mock.calls.filter(([, init]) => init?.method && init.method !== "GET")).toHaveLength(0);
});
