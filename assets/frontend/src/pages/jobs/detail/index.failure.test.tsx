import { fireEvent, screen, within } from "@testing-library/react";

import { jobPayload, mockedFetchJobExecutionStepsPage, primeApi, renderPage } from "./index.test-helpers";

function failedRun(overrides: Record<string, unknown> = {}) {
  return jobPayload({
    status: "failed",
    finished_at: "2026-04-25T10:05:00Z",
    lifecycle: { status: { code: "failed", label: "Failed", terminal: true } },
    execution_steps: [
      { id: "load", step_id: "load", title: "Load the feed", status: "completed", status_label: "Completed" },
      { id: "query", step_id: "query", title: "Query saved stock levels", status: "failed", status_label: "Failed" },
    ],
    ...overrides,
  });
}

/** The compact detail has no workflow run; its error comes with the steps page. */
function primeRunError(stepId = "query") {
  mockedFetchJobExecutionStepsPage.mockResolvedValue({
    job_id: "42",
    steps: [],
    page: { has_more: false, next_cursor: "" },
    run_error: { code: "projection_unavailable", message: "Saved query projection is unavailable for new runs", step_id: stepId },
  } as never);
}

describe("JobsDetailPage failed-step overview", () => {
  it("names the failing step with its error code and message on the overview", async () => {
    primeApi(failedRun());
    primeRunError();
    renderPage();

    const overview = await screen.findByTestId("job-run-overview");
    const alert = await within(overview).findByTestId("job-step-failure");
    expect(within(alert).getByText("Step “Query saved stock levels” failed")).toBeInTheDocument();
    expect(within(alert).getByText("projection_unavailable")).toBeInTheDocument();
    expect(within(alert).getByText(/Saved query projection is unavailable for new runs/)).toBeInTheDocument();
    expect(within(alert).getByRole("alert")).toBeInTheDocument();

    // The link opens that step in the timeline's inspector.
    fireEvent.click(within(alert).getByRole("button", { name: "Show the step in the timeline" }));
    expect(await screen.findByRole("button", { name: /Query saved stock levels/, pressed: true })).toBeInTheDocument();
  });

  it.each([
    { code: "query_timeout", message: "The stock query timed out" },
    "The stock query timed out",
  ])("uses the failed step's recorded error when the run has no error: %j", async (error) => {
    primeApi(failedRun({
      execution_steps: [
        { id: "query", step_id: "query", title: "Query saved stock levels", status: "failed", error },
      ],
    }));
    renderPage();

    const alert = await screen.findByTestId("job-step-failure");
    expect(within(alert).getByText("Step “Query saved stock levels” failed")).toBeInTheDocument();
    expect(within(alert).getByText(/The stock query timed out/)).toBeInTheDocument();
    if (typeof error !== "string") expect(within(alert).getByText("query_timeout")).toBeInTheDocument();
  });

  it("does not attribute an off-page run error to a different visible failed step", async () => {
    primeApi(failedRun({
      execution_steps: [
        { id: "cleanup", step_id: "cleanup", title: "Cleanup temporary files", status: "failed", error: { code: "cleanup_failed", message: "Cleanup also failed" } },
      ],
    }));
    primeRunError("stock_query");
    renderPage();

    const alert = await screen.findByTestId("job-step-failure");
    expect(within(alert).getByText("Step “Stock Query” failed")).toBeInTheDocument();
    expect(within(alert).getByText("projection_unavailable")).toBeInTheDocument();
    expect(within(alert).queryByText(/Cleanup temporary files|Cleanup also failed/)).not.toBeInTheDocument();
    expect(within(alert).queryByRole("button", { name: "Show the step in the timeline" })).not.toBeInTheDocument();
  });

  it("keeps a run-level error visible when execution history has no recorded steps", async () => {
    primeApi(failedRun({ execution_steps: [] }));
    primeRunError("stock_query");
    renderPage();

    const alert = await screen.findByTestId("job-step-failure");
    expect(within(alert).getByText("Step “Stock Query” failed")).toBeInTheDocument();
    expect(within(alert).getByText(/Saved query projection is unavailable for new runs/)).toBeInTheDocument();
    expect(within(alert).queryByRole("button")).not.toBeInTheDocument();
  });

  it("does not invent a failure reason when no error was recorded", async () => {
    primeApi(failedRun());
    renderPage();

    await screen.findByTestId("job-run-overview");
    expect(screen.queryByTestId("job-step-failure")).not.toBeInTheDocument();
  });

  it("shows nothing of the kind for a run that did not break", async () => {
    primeApi(jobPayload({ status: "completed", lifecycle: { status: { code: "completed", label: "Completed", terminal: true } } }));
    primeRunError();
    renderPage();

    await screen.findByTestId("job-run-overview");
    expect(screen.queryByTestId("job-step-failure")).not.toBeInTheDocument();
  });
});
