import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockedFetchWorkflowDetail, mockedFindWorkflowLinkedProjectId, mockedTestRunWorkflowStep, renderPage, workflowDetailFixture } from "./index.test-helpers";
import { fetchWorkflowStepContractPreview } from "@/pages/workflows/shared/api";

it.each([true, false])("uses the existing Step Test Run inputs for a mapped Wait: %s", async (mapped) => {
  mockedFindWorkflowLinkedProjectId.mockResolvedValueOnce("project_alpha");
  mockedFetchWorkflowDetail.mockResolvedValueOnce(workflowDetailFixture({
    steps: [{ id: "11111111-1111-1111-1111-111111111099", step_id: "pause", display_name: "Pause", step_kind: "wait", override: { wait: mapped ? { input_mapping: { duration_ms: "state.inputs.duration" } } : { duration_ms: 0 } } }],
  }));
  const preview = vi.mocked(fetchWorkflowStepContractPreview);
  const original = preview.getMockImplementation();
  onTestFinished(() => { if (original) preview.mockImplementation(original); });
  preview.mockResolvedValue({ locked_required: mapped ? [{ name: "duration_ms", source: "state.inputs.duration", resolved: true, required: true, type: "number" }] : [], unresolved_required: [], optional: [], custom: [], outputs: [] } as never);
  mockedTestRunWorkflowStep.mockResolvedValueOnce({ workflow_id: "weekly_report", step_id: "pause", step_kind: "wait", status: "completed", needs_user_approval: false });
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /Pause/i }));
  await user.click(screen.getByRole("tab", { name: "Test Run" }));
  const summary = await screen.findByLabelText("Step Test Run Summary");
  if (mapped) {
    const duration = screen.getByRole("spinbutton", { name: /^duration_ms/ });
    await user.type(duration, "0");
    expect(duration).toHaveValue(0);
  } else {
    expect(screen.queryByRole("spinbutton", { name: /^duration_ms/ })).not.toBeInTheDocument();
  }
  await user.type(summary, "Test the selected Wait only");
  await user.click(screen.getByRole("button", { name: "Run step test run" }));
  await waitFor(() => expect(mockedTestRunWorkflowStep).toHaveBeenCalledWith(expect.objectContaining({
    stepId: "pause",
    inputs: mapped ? { duration_ms: 0 } : {},
    inputBindings: mapped ? [{ name: "duration_ms", source: "state.inputs.duration", type: "number" }] : [],
  })));
}, 15_000);
