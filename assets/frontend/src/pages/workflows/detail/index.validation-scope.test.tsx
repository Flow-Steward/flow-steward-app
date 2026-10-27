import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { fetchSpy, mockedFetchWorkflowTestRunState, mockedTestRunWorkflow, renderPage } from "./index.test-helpers";

it("labels saved validation while drafting and lists identical draft issues only once", async () => {
  const original = fetchSpy.getMockImplementation();
  const issue = { code: "step_unreachable", path: "steps.extra_wait", message: "Extra wait is unreachable from the workflow entry." };
  fetchSpy.mockImplementation(async (url, init) => {
    if (String(url).endsWith("/changes") && init?.method === "POST") return new Response(JSON.stringify({ applied: false, issues: [issue, issue], blocking_issues: [] }));
    return original ? original(url, init) : new Response("{}");
  });
  renderPage({ surface: "builder" });
  expect(await screen.findByRole("button", { name: "Diagnostics" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add step" }));
  const palette = await screen.findByRole("listbox", { name: "Add step" });
  fireEvent.click(within(palette).getByRole("option", { name: "Wait" }));

  fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "Extra wait" } });
  expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("wait");
  fireEvent.click(screen.getByRole("button", { name: "Update new step" }));
  expect(await screen.findByRole("button", { name: /Diagnostics.*Saved workflow valid/ })).toBeInTheDocument();
  const kept = await screen.findByTestId("change-check-kept");
  expect(within(kept).getAllByText(issue.message)).toHaveLength(1);
}, 15_000);

it("keeps a past owned result available after explicit history selection when the next start fails", async () => {
  const jobId = "previous-success";
  const threadId = "previous-success-thread";
  const job = { id: jobId, project_id: "project_alpha", selected_workflow: "weekly_report", status: "completed" };
  const detail = { ...job, job, execution_steps: [], workflow_result: { outputs: { report: "Prior successful report" } } };
  const reads: Array<{ path: string; scope: Record<string, string> }> = [];
  const original = fetchSpy.getMockImplementation();
  const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { "content-type": "application/json" } });
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
    const path = url.pathname;
    if (path === "/api/v2/jobs") {
      reads.push({ path, scope: Object.fromEntries(url.searchParams) });
      return json({ jobs: [{ ...job, job_id: jobId, task_source: "workflow_test_run" }], page: 1, per_page: 8, total: 1, total_pages: 1 });
    }
    if (path === `/api/v2/jobs/${jobId}`) {
      reads.push({ path, scope: Object.fromEntries(url.searchParams) });
      return json(detail);
    }
    if (path === `/api/jobs/${jobId}/definition`) return json({ job_id: jobId, available: true, workflow_id: "weekly_report", revision: 7, digest: "previous-success-archive", schema_version: 1, steps: [{ step_id: "plan", display_name: "Plan work", step_kind: "set_variable" }], groups: [], for_each: [], execution_graph: { schema_version: 1, entry: { kind: "step", id: "plan" }, edges: [] } });
    if (path === `/api/v2/jobs/${jobId}/execution-steps`) return json({ job_id: jobId, run_terminal: true, steps: [], child_invocations: [], page: { count: 0, has_more: false, next_cursor: "" } });
    if (path === `/api/jobs/${jobId}/cost-breakdown`) return json({ breakdown: [], total_cost_usd: 0 });
    if (path === `/api/jobs/${jobId}/approval`) return json({ job_id: jobId, approvals: [] });
    if (path === `/api/jobs/${jobId}/decision-overrides`) return json({ job_id: jobId, overrides: [] });
    if (!original) throw new Error(`Missing fixture response for ${path}`);
    return original(input, init);
  });
  localStorage.setItem("fs.workflow-test-run.weekly_report", threadId);
  localStorage.setItem("fs.workflow-run-job.weekly_report", jobId);
  mockedFetchWorkflowTestRunState.mockResolvedValue({ status: "completed", final_status: "completed", workflow_id: "weekly_report", project_id: "project_alpha", job_id: jobId, thread_id: threadId, final_report: "Prior successful report", trace: [] } as never);
  mockedTestRunWorkflow.mockRejectedValue(new Error("Workflow has unreachable steps."));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  await waitFor(() => expect(reads).toEqual(expect.arrayContaining([
    { path: "/api/v2/jobs", scope: { page: "1", per_page: "8", project_id: "project_alpha", workflow_id: "weekly_report", task_source: "workflow_test_run" } },
    { path: `/api/v2/jobs/${jobId}`, scope: { compact: "true", step_limit: "0" } },
  ])));
  // The latest finished run is shown in the run bar after the reload; Results stay closed until asked.
  const runBar = await screen.findByTestId("workflow-run-bar");
  await waitFor(() => expect(runBar).toHaveAttribute("data-job-id", jobId));
  expect(screen.queryByRole("dialog", { name: "Test Run results" })).not.toBeInTheDocument();
  expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
  expect(screen.queryByText("\"Prior successful report\"")).not.toBeInTheDocument();
  const openLauncher = async () => {
    fireEvent.click(within(screen.getByRole("toolbar", { name: "Canvas tools" })).getByRole("link", { name: "Test Run" }));
    return screen.findByRole("dialog", { name: /^Start a test run$/ });
  };
  // Explicit history selection happens in Results › Test runs.
  fireEvent.click(within(runBar).getByRole("button", { name: "Results" }));
  const pastRun = await screen.findByTestId(`workflow-test-run-row:${jobId}`);
  fireEvent.click(pastRun);
  expect(pastRun).toHaveAttribute("aria-current", "true");
  expect(await screen.findByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", jobId);
  expect(await screen.findByText("\"Prior successful report\"")).toBeInTheDocument();
  fireEvent.click(within(screen.getByRole("dialog", { name: "Test Run results" })).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Test Run results" })).not.toBeInTheDocument());
  const retry = await openLauncher();
  expect(within(retry).queryByRole("combobox", { name: "Test Run history" })).not.toBeInTheDocument();
  const run = within(retry).getByRole("button", { name: "Run workflow test run" });
  await waitFor(() => expect(run).toBeEnabled());
  fireEvent.click(run);
  expect(await within(retry).findByText("Workflow has unreachable steps.")).toBeInTheDocument();
  expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("Previous test run — the latest start request failed.")).not.toBeInTheDocument();
  fireEvent.click(within(retry).getByRole("button", { name: "Cancel" }));
  // The past owned result stays selected and available after the failed start.
  const selected = await screen.findByTestId("workflow-run-bar");
  await waitFor(() => expect(selected).toHaveAttribute("data-job-id", jobId));
  fireEvent.click(within(selected).getByRole("button", { name: "Results" }));
  expect(await screen.findByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", jobId);
  expect(await screen.findByText("\"Prior successful report\"")).toBeInTheDocument();
  expect(localStorage.getItem("fs.workflow-run-job.weekly_report")).toBe(jobId);
}, 15_000);
