import { fireEvent, screen, within } from "@testing-library/react";
import {
  canvasGraphFixture,
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowContract,
  mockedFetchWorkflowDetail,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

// jsdom has no canvas hit testing; these are public clicks on rendered Stop sections.
const hitTestDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
beforeAll(() => Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [] }));
afterAll(() => {
  if (hitTestDescriptor) Object.defineProperty(document, "elementsFromPoint", hitTestDescriptor);
  else Reflect.deleteProperty(document, "elementsFromPoint");
});

it("opens Interface › Outputs from a Stop card that lists a returned file with the outputs", async () => {
  const returnedFile = {
    name: "audit_report", type: "file", required: false,
    source: { step_id: "render", step_name: "Render report", result: "file", target: "state.audit_report" },
  };
  const callable = {
    enabled: true,
    trigger_channels: ["ui"],
    input_mode: "fields",
    expected_inputs: [],
    output_mode: "variables_and_artifacts",
    expected_outputs: [{ name: "receipt", type: "string" }],
    returned_files: [returnedFile],
  };
  mockedFetchWorkflowContract.mockResolvedValue({ workflow_id: "weekly_report", interface: callable, definition_issues: [] } as never);
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({
    revision: 5,
    settings: { callable },
    steps: [{ step_id: "done", step_kind: "stop_success", display_name: "Done" }],
  }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "step", id: "done" },
    steps: [{ id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] }],
    output_contract: {
      output_mode: "variables_and_artifacts",
      expected_outputs: [{ name: "receipt", type: "string" }],
      returned_files: [returnedFile],
    },
  }));

  renderPage({ surface: "builder" });
  const stop = await screen.findByRole("group", { name: "Open step Done" });
  expect(within(stop).getByText("receipt")).toBeVisible();
  expect(within(stop).getByText("audit_report")).toBeVisible();
  expect(within(stop).queryByText("Artifacts")).not.toBeInTheDocument();
  expect(within(stop).queryByText("External changes")).not.toBeInTheDocument();

  fireEvent.click(within(stop).getByRole("button", { name: /Outputs/i }));
  const drawer = await screen.findByRole("dialog", { name: "Workflow interface" });
  expect(await within(drawer).findByRole("tab", { name: "Outputs" })).toHaveAttribute("aria-selected", "true");
  expect(within(drawer).getByTestId("workflow-contract-returned-files")).toHaveTextContent("Render report → file");
  expect(fetchSpy.mock.calls.filter(([url, init]) => String(url) === "/api/workflows/weekly_report" && init?.method === "PATCH")).toHaveLength(0);
  expect(fetchSpy.mock.calls.filter(([url, init]) => String(url).endsWith("/changes") && init?.method === "POST")).toHaveLength(0);
}, 10_000);
