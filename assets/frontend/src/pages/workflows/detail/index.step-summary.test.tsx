import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { mockedFetchWorkflowDetail, renderPage, workflowDetailFixture } from "./index.test-helpers";

// Install the shared helper's API mocks before importing the preview consumer.
import { fetchWorkflowStepContractPreview, type WorkflowStepContractPreview } from "@/pages/workflows/shared/api";

function savedSteps(...items: Array<{ id: string; name: string }>) {
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({
    steps: items.map(({ id, name }, index) => ({
      id: `11111111-1111-1111-1111-${String(index + 1).padStart(12, "0")}`,
      step_id: id,
      display_name: name,
      step_kind: "set_variable",
      override: { set_variable: { assignments: { [`state.${id}`]: true } } },
    })),
    edges: items.slice(1).map((item, index) => ({
      source: { kind: "step", id: items[index].id },
      outcome: { name: "success" },
      target: { kind: "step", id: item.id },
    })),
  }));
}

function previewMock() {
  const preview = vi.mocked(fetchWorkflowStepContractPreview);
  const original = preview.getMockImplementation();
  preview.mockReset();
  onTestFinished(() => {
    preview.mockReset();
    if (original) preview.mockImplementation(original);
  });
  return preview;
}

describe("saved Step editor", () => {
  it("shows no Step summary: the contract is already in the fields below", async () => {
    savedSteps({ id: "inspect", name: "Inspect order" });
    const preview = previewMock();
    preview.mockResolvedValue({
      workflow_id: "weekly_report",
      step_id: "inspect",
      locked_required: [{ name: "method", type: "string", source: "state.method" }],
      outputs: [{ name: "result" }],
    } as WorkflowStepContractPreview);
    const user = userEvent.setup();
    renderPage({ surface: "detail" });
    await user.click(await screen.findByRole("button", { name: /Inspect order/ }));
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    // The preview still loads for Test Run; the editor no longer repeats it.
    await waitFor(() => expect(preview).toHaveBeenCalledWith("weekly_report", expect.objectContaining({ stepId: "inspect" })));
    expect(within(drawer).queryByText("Step summary")).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("group", { name: "Input method" })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("group", { name: "Output result" })).not.toBeInTheDocument();
  });
});
