import { hasPendingRequests } from "@/lib/http/request-activity";
import type * as SimulateApi from "@/pages/workflows/shared/simulate-api";
import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import {
  fetchSpy,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowTestRunState,
  mockedFetchWorkflowValidation,
  mockedSubmitWorkflowTestRunApproval,
  mockedTestRunWorkflow,
  renderPage,
} from "./index.test-helpers";

/**
 * AUDIT-038 symptom (c) — a reload removed the Test Run decision controls.
 *
 * The reviewer was told "Workflow test run approval ID is unavailable. Refresh
 * before deciding." Refreshing then removed the run panel entirely, taking
 * approve/reject with it, while the run stayed blocked on its gate.
 *
 * `workflowTestRunPayload` was plain `useState(null)`, so a reload wiped it and
 * nothing brought it back.
 *
 * The fix remembers only the thread id and re-fetches the run from the server
 * on mount: storage holds a pointer, never a copy of the decision state, so a
 * decision made on another surface is still reflected after the reload.
 */

const THREAD_ID = "thread-awaiting-approval-1";
const STORAGE_KEY = "fs.workflow-test-run.weekly_report";

const AWAITING_RUN = {
  status: "awaiting_approval",
  final_status: "awaiting_approval",
  workflow_id: "weekly_report",
  thread_id: THREAD_ID,
  execution_id: "exec-1",
  needs_user_approval: true,
  approval_id: "int-abc123",
  approval_context: "Step 'analysis' is waiting for human approval.",
  trace: [],
};

const QUEUED_RUN = {
  ...AWAITING_RUN,
  status: "queued",
  final_status: "queued",
  needs_user_approval: false,
  approval_id: null,
  approval_context: null,
};

const FAILED_RUN = {
  ...QUEUED_RUN,
  status: "failed",
  final_status: "failed",
  error: "The workflow step failed.",
};

/** The remembered run is shown in the run bar; its Results open from there (the launcher has no history). */
async function openRememberedResults() {
  const launch = screen.queryByRole("dialog", { name: "Start a test run" });
  if (launch) fireEvent.click(within(launch).getByRole("button", { name: "Cancel" }));
  const runBar = await screen.findByTestId("workflow-run-bar");
  fireEvent.click(await within(runBar).findByRole("button", { name: "Results" }));
  return screen.findByRole("dialog", { name: "Test Run results" });
}
async function runHeading() {
  return within(await screen.findByTestId("workflow-test-run-results")).findByRole("heading", { name: /^Latest test run/ });
}

async function reopenLaunchDialog(router: ReturnType<typeof renderPage>["router"]) {
  const results = screen.getByRole("dialog", { name: "Test Run results" });
  fireEvent.click(within(results).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Test Run results" })).not.toBeInTheDocument());
  const toolbar = within(screen.getByRole("toolbar", { name: "Canvas tools" }));
  const running = toolbar.queryByRole("button", { name: "Running" });
  if (running) {
    // Pending execution opens its owned Results; the launcher remains a public URL intent.
    fireEvent.click(running);
    const reopened = await screen.findByRole("dialog", { name: "Test Run results" });
    expect(within(reopened).getByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "job-remembered-approval-1");
    fireEvent.click(within(reopened).getByRole("button", { name: "Close" }));
    await act(async () => { await router.navigate("/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha"); });
  } else {
    fireEvent.click(toolbar.getByRole("link", { name: "Test Run" }));
  }
  return screen.findByRole("dialog", { name: "Start a test run" });
}

function installOwnedRememberedRun(initialRun: SimulateApi.WorkflowTestRunResult = AWAITING_RUN, decisionError = "") {
  const jobId = "job-remembered-approval-1";
  let run = { ...initialRun, project_id: "project_alpha", job_id: jobId };
  const decisions: Array<{ path: string; payload: unknown }> = [];
  mockedFetchWorkflowTestRunState.mockImplementation(async () => run as never);
  const previousFetch = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
    const job = { id: jobId, project_id: run.project_id, selected_workflow: run.workflow_id,
      task_source: "workflow_test_run", thread_id: run.thread_id, status: run.status === "queued" ? "pending" : run.status };
    const awaitingApproval = run.status === "awaiting_approval";
    const terminal = run.status === "failed";
    const gate = { approval_id: run.approval_id, job_id: jobId, thread_id: run.thread_id,
      gate_kind: "approval", message: run.approval_context, content: run.approval_context };
    if (url.pathname === `/api/approvals/${THREAD_ID}` && init?.method === "POST") {
      decisions.push({ path: url.pathname, payload: JSON.parse(String(init.body)) });
      return decisionError ? json({ error: decisionError }, 503) : json({ status: "in_progress" });
    }
    if (url.pathname === "/api/v2/jobs" && (!init?.method || init.method === "GET")) {
      const scoped = url.searchParams.get("workflow_id") === job.selected_workflow && url.searchParams.get("project_id") === job.project_id
        && url.searchParams.get("task_source") === job.task_source && url.searchParams.get("page") === "1" && url.searchParams.get("per_page") === "8";
      return scoped ? json({ ok: true, kind: "list", data: { jobs: [job], page: 1, per_page: 8, total: 1, total_pages: 1 }, error: null })
        : json({ error: "Unexpected history scope" }, 422);
    }
    if (url.pathname === `/api/v2/jobs/${jobId}` && (!init?.method || init.method === "GET")) return json({ ok: true, kind: "detail",
      data: { ...job, job, execution_steps: [], workflow_result: null,
        lifecycle: { status: { code: job.status, label: awaitingApproval ? "Waiting for approval" : terminal ? "Failed" : "Queued", terminal },
          approval: awaitingApproval ? { state: "awaiting_approval", gate } : { state: "none" } } }, error: null });
    if (url.pathname === `/api/jobs/${jobId}/approval`) return awaitingApproval
      ? json({ job_id: jobId, approval_id: gate.approval_id, payload: gate }) : json({ code: "approval_not_awaiting" }, 409);
    if (url.pathname === `/api/jobs/${jobId}/approvals`) return json({ job_id: jobId, approvals: [] });
    if (url.pathname === `/api/jobs/${jobId}/definition`) return json({ job_id: jobId, available: false, workflow_id: job.selected_workflow });
    if (url.pathname === `/api/v2/jobs/${jobId}/execution-steps`) return json({ job_id: jobId, run_terminal: terminal, child_invocations: [], steps: [], page: { count: 0, has_more: false, next_cursor: "" } });
    if (url.pathname === `/api/jobs/${jobId}/cost-breakdown`) return json({ breakdown: [], total_cost_usd: 0 });
    if (url.pathname === `/api/jobs/${jobId}/decision-overrides`) return json({ job_id: jobId, overrides: [] });
    return previousFetch ? previousFetch(input, init) : new Response("{}");
  });
  localStorage.setItem("fs.workflow-run-job.weekly_report", jobId);
  return { jobId, decisions, setRun: (next: typeof initialRun) => { run = { ...next, project_id: "project_alpha", job_id: jobId }; } };
}

describe("Workflow Test Run survives a reload (AUDIT-038 symptom c)", () => {
  it("keeps the actual pending approval comment focused through state-poll failure and retry", async () => {
    const actual = await vi.importActual<typeof SimulateApi>("@/pages/workflows/shared/simulate-api");
    mockedFetchWorkflowTestRunState.mockImplementation(actual.fetchWorkflowTestRunState);
    const jobId = "job-pending-approval-focus";
    const ownedRun = { ...AWAITING_RUN, project_id: "project_alpha", job_id: jobId };
    const job = { id: jobId, project_id: "project_alpha", selected_workflow: AWAITING_RUN.workflow_id,
      task_source: "workflow_test_run", thread_id: THREAD_ID, status: AWAITING_RUN.status };
    const gate = { approval_id: AWAITING_RUN.approval_id, job_id: jobId, thread_id: THREAD_ID,
      gate_kind: "approval", message: AWAITING_RUN.approval_context, content: AWAITING_RUN.approval_context };
    const previousFetch = fetchSpy.getMockImplementation();
    const pending: Array<(response: Response) => void> = [];
    let calls = 0;
    let holdPolls = false;
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input) === `/api/workflows/test-runs/${THREAD_ID}`) {
        calls += 1;
        if (holdPolls) return new Promise<Response>((resolve) => { pending.push(resolve); });
        return new Response(JSON.stringify(ownedRun), { headers: { "content-type": "application/json" } });
      }
      const url = new URL(String(input), "http://localhost");
      const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
      if (url.pathname === "/api/v2/jobs" && (!init?.method || init.method === "GET")) {
        const scoped = url.searchParams.get("workflow_id") === job.selected_workflow && url.searchParams.get("project_id") === job.project_id
          && url.searchParams.get("task_source") === job.task_source && url.searchParams.get("page") === "1" && url.searchParams.get("per_page") === "8";
        return scoped ? json({ ok: true, kind: "list", data: { jobs: [job], page: 1, per_page: 8, total: 1, total_pages: 1 }, error: null })
          : json({ error: "Unexpected history scope" }, 422);
      }
      if (url.pathname === `/api/v2/jobs/${jobId}` && (!init?.method || init.method === "GET")) return json({ ok: true, kind: "detail",
        data: { ...job, job, execution_steps: [], workflow_result: null,
          lifecycle: { status: { code: job.status, label: "Waiting for approval", terminal: false },
            approval: { state: "awaiting_approval", gate } } }, error: null });
      if (url.pathname === `/api/jobs/${jobId}/approval`) return json({ job_id: jobId, approval_id: gate.approval_id, payload: gate });
      if (url.pathname === `/api/jobs/${jobId}/approvals`) return json({ job_id: jobId, approvals: [] });
      if (url.pathname === `/api/jobs/${jobId}/definition`) return json({ job_id: jobId, available: false, workflow_id: job.selected_workflow });
      if (url.pathname === `/api/v2/jobs/${jobId}/execution-steps`) return json({ job_id: jobId, run_terminal: false, child_invocations: [], steps: [], page: { count: 0, has_more: false, next_cursor: "" } });
      if (url.pathname === `/api/jobs/${jobId}/cost-breakdown`) return json({ breakdown: [], total_cost_usd: 0 });
      if (url.pathname === `/api/jobs/${jobId}/decision-overrides`) return json({ job_id: jobId, overrides: [] });
      return previousFetch ? previousFetch(input, init) : new Response("{}");
    });
    localStorage.setItem(STORAGE_KEY, THREAD_ID);
    localStorage.setItem("fs.workflow-run-job.weekly_report", jobId);
    vi.useFakeTimers();
    const view = renderPage({ initialEntry: "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha", surface: "builder" });
    const { unmount: unmountIndicator } = render(<RequestLoadingIndicator />);
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(calls).toBe(2);
      expect(mockedFetchWorkflowTestRunState.mock.calls).toEqual([[THREAD_ID], [THREAD_ID]]);
      expect(pending).toHaveLength(0);
      expect(hasPendingRequests()).toBe(false);
      let resultsOpened = false;
      const openingResults = openRememberedResults().then(() => { resultsOpened = true; });
      await vi.waitFor(async () => {
        await act(async () => { await vi.advanceTimersByTimeAsync(0); });
        expect(resultsOpened).toBe(true);
      });
      await openingResults;
      expect(screen.getByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", jobId);
      const field = screen.getByRole("textbox", { name: "Comment for rejection" });
      fireEvent.change(field, { target: { value: "Still reviewing real output" } });
      field.focus();
      holdPolls = true;
      await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
      expect(calls).toBe(4); expect(pending).toHaveLength(2);
      expect(mockedFetchWorkflowTestRunState.mock.calls.slice(2)).toEqual([
        [THREAD_ID, { presentation: "background" }],
        [THREAD_ID, { presentation: "background" }],
      ]);
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      expect(screen.getByRole("textbox", { name: "Comment for rejection" })).toBe(field);
      expect(field).toHaveFocus(); expect(field).toHaveValue("Still reviewing real output");
      await act(async () => { pending.splice(0).forEach((resolve) => resolve(new Response(JSON.stringify({ error: "Temporary read failure" }), { status: 503, headers: { "content-type": "application/json" } }))); await vi.advanceTimersByTimeAsync(0); });
      await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
      expect(calls).toBe(6); expect(pending).toHaveLength(2);
      expect(mockedFetchWorkflowTestRunState.mock.calls.slice(4)).toEqual([
        [THREAD_ID, { presentation: "background" }],
        [THREAD_ID, { presentation: "background" }],
      ]);
      expect(field).toHaveFocus(); expect(field).toHaveValue("Still reviewing real output");
      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      await act(async () => { pending.splice(0).forEach((resolve) => resolve(new Response(JSON.stringify(ownedRun), { headers: { "content-type": "application/json" } }))); await vi.advanceTimersByTimeAsync(0); });
      expect(field).toHaveValue("Still reviewing real output"); expect(field).toHaveFocus();
      expect(fetchSpy.mock.calls.filter(([, init]) => init?.method && init.method !== "GET")).toEqual([]);
      expect(mockedFetchWorkflowTestRunState).toHaveBeenLastCalledWith(THREAD_ID, { presentation: "background" });
    } finally {
      view.unmount(); unmountIndicator(); pending.splice(0).forEach((resolve) => resolve(new Response("{}")));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      vi.clearAllTimers(); vi.useRealTimers();
    }
  });

  beforeEach(() => {
    globalThis.localStorage.clear();
    mockedFetchWorkflowTestRunState.mockReset();
    mockedFetchWorkflowTestRunState.mockResolvedValue(AWAITING_RUN as never);
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
  });

  it("re-fetches the remembered pending run on mount", async () => {
    globalThis.localStorage.setItem(STORAGE_KEY, THREAD_ID);

    renderPage();

    await waitFor(() => {
      expect(mockedFetchWorkflowTestRunState).toHaveBeenCalledWith(THREAD_ID);
    });
  });

  it("brings the approval controls back after the reload", async () => {
    globalThis.localStorage.setItem(STORAGE_KEY, THREAD_ID);
    installOwnedRememberedRun();

    renderPage({
      initialEntry:
        "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha",
      surface: "builder",
    });

    await openRememberedResults();
    // The decision control itself — the thing the refresh used to take away —
    // not merely a network call.
    expect(
      await screen.findByRole("textbox", { name: "Comment for rejection" }),
    ).toBeInTheDocument();
    // Rendered both in the banner and in the review-decision panel.
    expect(
      await screen.findAllByText(/Step 'analysis' is waiting for human approval\./),
    ).not.toHaveLength(0);
  });

  it("preserves an approval comment and pending run through pending toolbar Results and the public launcher route", async () => {
    globalThis.localStorage.setItem(STORAGE_KEY, THREAD_ID);
    installOwnedRememberedRun();
    const view = renderPage({ initialEntry: "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha", surface: "builder" });
    await openRememberedResults();
    fireEvent.change(await screen.findByRole("textbox", { name: "Comment for rejection" }), { target: { value: "Checked the report" } });
    await reopenLaunchDialog(view.router);
    expect(screen.getByLabelText("Run workflow test run")).toBeDisabled();
    await openRememberedResults();
    expect(await screen.findByRole("textbox", { name: "Comment for rejection" })).toHaveValue("Checked the report");
  });

  it("keeps a second workflow test run disabled while the durable run is pending", async () => {
    globalThis.localStorage.setItem(STORAGE_KEY, THREAD_ID);
    installOwnedRememberedRun(QUEUED_RUN);

    const view = renderPage({
      initialEntry:
        "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha",
      surface: "builder",
    });

    await openRememberedResults();
    expect(await runHeading()).toHaveTextContent("Running");
    expect(await mockedFetchWorkflowTestRunState.mock.results.at(-1)?.value).toMatchObject({ status: "queued", final_status: "queued" });
    await reopenLaunchDialog(view.router);
    expect(screen.getByLabelText("Run workflow test run")).toBeDisabled();
  });

  it("keeps a second workflow test run disabled while the durable run awaits approval", async () => {
    globalThis.localStorage.setItem(STORAGE_KEY, THREAD_ID);
    installOwnedRememberedRun();

    const view = renderPage({
      initialEntry:
        "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha",
      surface: "builder",
    });

    await openRememberedResults();
    expect(await runHeading()).toHaveTextContent("Waiting for your decision");
    await reopenLaunchDialog(view.router);
    expect(screen.getByLabelText("Run workflow test run")).toBeDisabled();
  });

  it("continues polling a restored pending run until durable terminal state", async () => {
    globalThis.localStorage.setItem(STORAGE_KEY, THREAD_ID);
    const fixture = installOwnedRememberedRun(QUEUED_RUN);

    const view = renderPage({
      initialEntry:
        "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha",
      surface: "builder",
    });

    await openRememberedResults();
    expect(await runHeading()).toHaveTextContent("Running");
    expect(await mockedFetchWorkflowTestRunState.mock.results.at(-1)?.value).toMatchObject({ status: "queued", final_status: "queued" });
    fixture.setRun(FAILED_RUN);
    await reopenLaunchDialog(view.router);
    await waitFor(() => expect(screen.getByLabelText("Run workflow test run")).toBeEnabled(), { timeout: 3000 });
    expect(mockedFetchWorkflowTestRunState).toHaveBeenCalledWith(THREAD_ID, { presentation: "background" });
    await openRememberedResults();
    await waitFor(async () => expect(await runHeading()).toHaveTextContent("Failed"), { timeout: 3000 });
    await reopenLaunchDialog(view.router);
    expect(screen.getByLabelText("Run workflow test run")).toBeEnabled();
  });

  it("does not query anything when no run is remembered", async () => {
    renderPage();

    await waitFor(() => {
      expect(mockedFetchWorkflowDetail).toHaveBeenCalled();
    });
    expect(mockedFetchWorkflowTestRunState).not.toHaveBeenCalled();
  });

  it("keeps the pointer while the restore is still in flight", async () => {
    globalThis.localStorage.setItem(STORAGE_KEY, THREAD_ID);
    let release: ((value: unknown) => void) | undefined;
    mockedFetchWorkflowTestRunState.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }) as never,
    );

    renderPage();

    await waitFor(() => {
      expect(mockedFetchWorkflowTestRunState).toHaveBeenCalled();
    });
    // The empty panel on the first render must not be read as "the run ended".
    expect(globalThis.localStorage.getItem(STORAGE_KEY)).toBe(THREAD_ID);
    release?.(AWAITING_RUN);
  });

  it("drops the pointer when the remembered thread can no longer be read", async () => {
    globalThis.localStorage.setItem(STORAGE_KEY, THREAD_ID);
    mockedFetchWorkflowTestRunState.mockRejectedValue(new Error("404 thread not found"));

    renderPage();

    await waitFor(() => {
      expect(globalThis.localStorage.getItem(STORAGE_KEY)).toBeNull();
    });
  });

  it("stores only the thread id, never the decision state", async () => {
    globalThis.localStorage.setItem(STORAGE_KEY, THREAD_ID);

    renderPage();

    await waitFor(() => {
      expect(mockedFetchWorkflowTestRunState).toHaveBeenCalled();
    });

    const stored = globalThis.localStorage.getItem(STORAGE_KEY) ?? "";
    expect(stored).toBe(THREAD_ID);
    expect(stored).not.toContain("approval_id");
    expect(stored).not.toContain("awaiting_approval");
  });

  it("shows a failed owned approval in active Results while preserving the comment and remembered run", async () => {
    globalThis.localStorage.setItem(STORAGE_KEY, THREAD_ID);
    const fixture = installOwnedRememberedRun(AWAITING_RUN, "The approval service is unavailable. Try again.");
    renderPage({ initialEntry: "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha", surface: "builder" });
    const results = await openRememberedResults();
    const comment = await within(results).findByRole("textbox", { name: "Comment for rejection" });
    fireEvent.change(comment, { target: { value: "Keep this decision comment after failure" } });
    fireEvent.click(within(results).getByRole("button", { name: "Reject" }));

    await waitFor(() => expect(fixture.decisions).toEqual([{ path: `/api/approvals/${THREAD_ID}`, payload: {
      approval_id: "int-abc123", approved: false, comment: "Keep this decision comment after failure",
    } }]));
    expect(mockedSubmitWorkflowTestRunApproval).not.toHaveBeenCalled();
    const decisionError = await within(within(results).getByTestId("workflow-approval-decision")).findByRole("alert");
    expect(decisionError).toHaveTextContent(/^The approval service is unavailable\. Try again\.$/);
    expect(decisionError).toBeVisible();
    expect(comment).toHaveValue("Keep this decision comment after failure");
    expect(within(results).getByRole("heading", { name: /^Latest test run/ })).toHaveTextContent("Waiting for your decision");
    expect(await mockedFetchWorkflowTestRunState.mock.results.at(-1)?.value).toMatchObject({
      workflow_id: "weekly_report", project_id: "project_alpha", thread_id: THREAD_ID, job_id: fixture.jobId, execution_id: "exec-1",
    });
    expect(within(results).getByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", fixture.jobId);
    expect(within(results).getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(globalThis.localStorage.getItem(STORAGE_KEY)).toBe(THREAD_ID);
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument();
  });

  it("retires the retained comment for a different approval and after project scope A to B to A", async () => {
    localStorage.setItem(STORAGE_KEY, THREAD_ID);
    const fixture = installOwnedRememberedRun();
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const view = renderPage({ initialEntry: "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha", surface: "builder" });
    try {
      let results = await openRememberedResults();
      fireEvent.change(await within(results).findByRole("textbox", { name: "Comment for rejection" }), { target: { value: "First gate draft" } });
      const reopenPendingResults = async () => {
        fireEvent.click(within(screen.getByRole("dialog", { name: "Test Run results" })).getByRole("button", { name: "Close" }));
        // Closing is guarded (unsaved automation asks first) and settles before the next open.
        await waitFor(() => expect(screen.queryByRole("dialog", { name: "Test Run results" })).not.toBeInTheDocument());
        fireEvent.click(within(screen.getByRole("toolbar", { name: "Canvas tools" })).getByRole("button", { name: "Running" }));
        const reopened = await screen.findByRole("dialog", { name: "Test Run results" });
        await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
        return reopened;
      };
      fixture.setRun({ ...AWAITING_RUN, approval_id: "int-next", approval_context: "A different approval request." });
      results = await reopenPendingResults();
      // The decision follows the gate the verified detail names, never the previous one.
      const decision = () => within(screen.getByRole("dialog", { name: "Test Run results" })).getByTestId("workflow-approval-decision");
      await waitFor(() => expect(decision()).toHaveTextContent("A different approval request."));
      results = screen.getByRole("dialog", { name: "Test Run results" });
      expect(decision()).not.toHaveTextContent(AWAITING_RUN.approval_context);
      expect(within(results).getByText("A different approval request.")).toBeVisible();
      expect(within(results).getByRole("textbox", { name: "Comment for rejection" })).toHaveValue("");
      fireEvent.change(within(results).getByRole("textbox", { name: "Comment for rejection" }), { target: { value: "Second gate draft" } });
      fixture.setRun(AWAITING_RUN);
      results = await reopenPendingResults();
      await waitFor(() => expect(decision()).toHaveTextContent(AWAITING_RUN.approval_context));
      results = screen.getByRole("dialog", { name: "Test Run results" });
      expect(decision()).not.toHaveTextContent("A different approval request.");
      expect(within(results).getByText(AWAITING_RUN.approval_context)).toBeVisible();
      expect(within(results).getByRole("textbox", { name: "Comment for rejection" })).toHaveValue("");
      fireEvent.change(within(results).getByRole("textbox", { name: "Comment for rejection" }), { target: { value: "Retire with project owner" } });
      fireEvent.click(within(results).getByRole("button", { name: "Close" }));
      await act(async () => { await view.router.navigate("/registry/workflows/weekly_report/builder?project_id=project_beta"); });
      await waitFor(() => expect(fetchSpy.mock.calls.some(([input]) => {
        const url = new URL(String(input), "http://localhost");
        return url.pathname === "/api/v2/jobs" && url.searchParams.get("project_id") === "project_beta";
      })).toBe(true));
      await act(async () => { await view.router.navigate("/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha"); });
      results = await openRememberedResults();
      expect(await within(results).findByRole("textbox", { name: "Comment for rejection" })).toHaveValue("");
      expect(within(results).getByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", fixture.jobId);
      expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
      expect(fetchSpy.mock.calls.filter(([, init]) => init?.method && init.method !== "GET")).toEqual([]);
    } finally {
      view.unmount();
      vi.clearAllTimers();
      vi.useRealTimers();
    }
  }, 15_000);
});
