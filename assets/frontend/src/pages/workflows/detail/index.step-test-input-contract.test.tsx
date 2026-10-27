import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockedFetchWorkflowDetail, mockedFindWorkflowLinkedProjectId, mockedTestRunWorkflowStep, mockedTestRunWorkflow, renderPage, workflowDetailFixture } from "./index.test-helpers";
import { fetchWorkflowStepContractPreview } from "@/pages/workflows/shared/api";

it("runs a saved literal HTTP request without offering method/url as editable workflow inputs", async () => {
  mockedFindWorkflowLinkedProjectId.mockResolvedValueOnce("project_alpha");
  mockedFetchWorkflowDetail.mockResolvedValueOnce({ workflow_id: "weekly_report", display_name: "Weekly Report", enabled: true, steps: [{ id: "11111111-1111-1111-1111-111111111099", step_id: "fetch", display_name: "Fetch order", step_kind: "http_request", override: { http_request: { method: "GET", url: "https://example.test/orders" } } }], groups: [] } as never);
  const preview = vi.mocked(fetchWorkflowStepContractPreview);
  const original = preview.getMockImplementation();
  onTestFinished(() => { if (original) preview.mockImplementation(original); });
  preview.mockResolvedValue({ locked_required: [{ name: "method", source: "literal", resolved: true, required: true, type: "text" }, { name: "url", source: "literal", resolved: true, required: true, type: "text" }], unresolved_required: [], optional: [], custom: [], outputs: [] } as never);
  mockedTestRunWorkflowStep.mockResolvedValueOnce({ workflow_id: "weekly_report", step_id: "fetch", step_kind: "http_request", status: "completed", needs_user_approval: false });
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /Fetch order/i }));
  await user.click(screen.getByRole("tab", { name: "Test Run" }));
  const summary = await screen.findByLabelText("Step Test Run Summary");
  expect(screen.queryByLabelText(/^method/i)).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/^url/i)).not.toBeInTheDocument();
  expect(screen.getByText("GET")).toBeInTheDocument();
  expect(screen.getByText("https://example.test/orders")).toBeInTheDocument();
  await user.type(summary, "Test the saved request");
  await user.click(screen.getByRole("button", { name: "Run step test run" }));
  await waitFor(() => expect(mockedTestRunWorkflowStep).toHaveBeenCalledWith(expect.objectContaining({ inputs: {}, inputBindings: [] })));
}, 15_000);

const isolatedStepInputHelp = "Test values are optional. Only this step runs; earlier steps and loop context are not loaded. A saved mapping alone does not preload a value. Leaving a value blank may make the test fail.";

function prepareMappedBatch() {
  mockedFindWorkflowLinkedProjectId.mockResolvedValueOnce("project_alpha");
  mockedFetchWorkflowDetail.mockResolvedValueOnce(workflowDetailFixture({
    steps: [{ id: "11111111-1111-1111-1111-111111111099", step_id: "batch_collected_items", display_name: "Batch collected items", step_kind: "batch", override: { batch: { source_path: "state.collected", result_target: "state.batches", batch_size: 1 } } }],
  }));
  const preview = vi.mocked(fetchWorkflowStepContractPreview);
  const original = preview.getMockImplementation();
  onTestFinished(() => { if (original) preview.mockImplementation(original); });
  preview.mockResolvedValue({ workflow_id: "weekly_report", step_id: "batch_collected_items", step_kind: "batch", locked_required: [{ name: "source_path", source: "state.collected", resolved: true, required: true, read_only: true, type: "array" }], unresolved_required: [], optional: [], custom: [], outputs: [{ name: "batches", type: "array" }] });
}

it("shows a mapped Batch contract requirement while allowing an intentional missing-value step test", async () => {
  prepareMappedBatch();
  mockedTestRunWorkflowStep.mockResolvedValueOnce({ workflow_id: "weekly_report", step_id: "batch_collected_items", step_kind: "batch", status: "failed", final_status: "failed", error: "batch source_path must resolve to a bounded array or dataset result", inputs: {}, result: { error: "batch_source_missing" } });
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /Batch collected items/i }));
  await user.click(screen.getByRole("tab", { name: "Test Run" }));
  const summary = await screen.findByLabelText("Step Test Run Summary");
  const source = screen.getByRole("textbox", { name: /^source_path/ });
  expect(source).toHaveAccessibleName(/source_path\s*Required by step · array · state.collected/);
  expect(screen.getByText(isolatedStepInputHelp)).toBeInTheDocument();
  expect(source).not.toBeRequired();
  await user.type(summary, "Check missing Batch input");
  await user.click(screen.getByRole("button", { name: "Run step test run" }));
  await waitFor(() => expect(mockedTestRunWorkflowStep).toHaveBeenCalledTimes(1));
  expect(mockedTestRunWorkflowStep).toHaveBeenCalledWith(expect.objectContaining({ stepId: "batch_collected_items", expectedRevision: 7, inputs: {}, inputBindings: [] }));
  expect(await screen.findByText("batch source_path must resolve to a bounded array or dataset result")).toBeInTheDocument();
  expect(screen.getByText("This step has no inputs.")).toBeInTheDocument();
  expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
}, 15_000);

it("keeps a mapped Batch sample typed and runs only the selected step", async () => {
  prepareMappedBatch();
  const rows = [{ name: "Alpha" }, { name: "Beta" }];
  mockedTestRunWorkflowStep.mockResolvedValueOnce({ workflow_id: "weekly_report", step_id: "batch_collected_items", step_kind: "batch", status: "completed", final_status: "completed", inputs: { source_path: rows }, result: { item_count: 2, batch_size: 1, batch_count: 2, result_target: "state.batches" }, state_patch: { batches: [[rows[0]], [rows[1]]] } });
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /Batch collected items/i }));
  await user.click(screen.getByRole("tab", { name: "Test Run" }));
  const summary = await screen.findByLabelText("Step Test Run Summary");
  const source = screen.getByRole("textbox", { name: /^source_path/ });
  expect(source).toHaveAccessibleName(/source_path\s*Required by step · array · state.collected/);
  fireEvent.change(source, { target: { value: JSON.stringify(rows) } });
  await user.type(summary, "Check two Batch items");
  await user.click(screen.getByRole("button", { name: "Run step test run" }));
  await waitFor(() => expect(mockedTestRunWorkflowStep).toHaveBeenCalledTimes(1));
  expect(mockedTestRunWorkflowStep).toHaveBeenCalledWith(expect.objectContaining({ stepId: "batch_collected_items", inputs: { source_path: rows }, inputBindings: [{ name: "source_path", source: "state.collected", type: "array" }] }));
  const result = await screen.findByRole("region", { name: "Step test run result" });
  expect(within(result).getByText("Source item count: 2")).toBeInTheDocument();
  expect(within(result).getByText("Batch size: 1")).toBeInTheDocument();
  expect(within(result).getByText("Batch count: 2")).toBeInTheDocument();
  expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
}, 15_000);
