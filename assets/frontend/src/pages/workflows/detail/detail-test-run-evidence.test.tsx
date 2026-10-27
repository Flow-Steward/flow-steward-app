import { useEffect } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, Link, RouterProvider } from "react-router";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { queryKeys } from "@/lib/query/keys";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import { ApprovalDecision } from "./detail-step-test-run-latest";
import type * as JobApi from "@/pages/jobs/detail/api";
import { jobPayload, mockedFetchJobApproval, mockedFetchJobApprovalHistory, mockedFetchJobDefinition,
  mockedFetchJobDetail, mockedFetchJobExecutionStepsPage, primeApi } from "@/pages/jobs/detail/index.test-helpers";
import { JobsDetailPage } from "@/pages/jobs/detail";
import { submitApprovalDecision } from "@/pages/jobs/detail/api";
import { useSelectedWorkflowTestRun } from "./detail-selected-test-run";
import { useWorkflowTestRunEvidence, type TestRunApproval } from "./detail-test-run-evidence";

const owner = { workflowId: "wf_weekly_report", projectId: "project-alpha", editorRevision: 3 };
const builderKey = ["jobs", "workflow-test-run-approval", "42"];
const detailKey = ["jobs", "detail", "42", "approval"];
const comment = "Reviewed this recorded run";
function gate(approvalId = "gate-first", payload: Record<string, unknown> = {}, jobId = "42") {
  const threadId = `thread-${jobId}`;
  return { job_id: jobId, thread_id: threadId, approval_id: approvalId,
    payload: { approval_id: approvalId, thread_id: threadId, step_id: "review", message: "Review the recorded run",
      allowed_actions: ["approve", "reject"], ...payload } };
}
function detail(status = "awaiting_approval", approvalId = "gate-first", payload: Record<string, unknown> = {}, jobId = "42") {
  const awaiting = status === "awaiting_approval";
  return jobPayload({ id: jobId, project_id: owner.projectId, selected_workflow: owner.workflowId,
    task_source: "workflow_test_run", status, thread_id: `thread-${jobId}`, approval_id: awaiting ? approvalId : null,
    lifecycle: { status: { code: status, label: status, terminal: ["completed", "failed"].includes(status) },
      approval: awaiting ? { state: "awaiting_approval", gate: gate(approvalId, payload, jobId).payload } : { state: "none" } },
    execution_steps: [] }) as unknown as JobApi.JobDetail;
}
function Builder({ graph = null, showDecision = false, captureApproval, captureRenderedApproval, showHistory = false }: { graph?: WorkflowCanvasGraph | null; showDecision?: boolean; captureApproval?: (approval: TestRunApproval | null) => void; captureRenderedApproval?: (approval: TestRunApproval | null) => void; showHistory?: boolean }) {
  const selectedRun = useSelectedWorkflowTestRun({ owner, restoredJobId: "42", resultsOwnsObservation: true,
    confirmReadOnlyNavigation: async () => true });
  const evidence = useWorkflowTestRunEvidence({ selectedRun, workflowId: owner.workflowId, canvasGraph: graph,
    runBar: { history: { kind: "ready", rows: [], latestJobId: "42" }, onSelectHistory: () => undefined },
    openStep: () => undefined, lightStep: () => undefined, changeInputs: () => undefined });
  // Retain the first callback produced for a real selection render, before
  // snapshot/query updates can replace it in a later render. Test bookkeeping only.
  captureRenderedApproval?.(evidence.approval);
  useEffect(() => { captureApproval?.(evidence.approval); }, [captureApproval, evidence.approval]);
  return <>
    {showDecision && evidence.approval ? <ApprovalDecision approval={evidence.approval} /> : null}
    <output data-testid="builder-gate">{evidence.approval?.identity || "none"}</output>
    <output data-testid="builder-message">{evidence.approval?.message || "none"}</output>
    <output data-testid="builder-terminal">{String(evidence.terminal)}</output>
    <button onClick={() => evidence.approval?.decide(true, comment)}>Builder approve</button>
    <button onClick={() => evidence.approval?.decide(false, comment)}>Builder reject</button>
    <button onClick={evidence.refresh}>Refresh builder approval</button>
    {showHistory ? selectedRun.history.map((row) => <button key={String(row.id)} onClick={() => { void selectedRun.requestSelectHistory(String(row.id)); }}>Select run {String(row.id)}</button>) : null}
    <Link to="/jobs/42">Open full details</Link>
    <JobsDetailPage embedded jobId="42" expectedWorkflowId={owner.workflowId} expectedProjectId={owner.projectId}
      executionOwner={{ ...owner, jobId: "42", generation: 1, isCurrent: () => true }} />
  </>;
}
async function mount({ payload = {}, graph, showDecision = false, decisionResponse, captureApproval, cachedRun84 = false, captureRenderedApproval }: { payload?: Record<string, unknown>; graph?: WorkflowCanvasGraph; showDecision?: boolean; decisionResponse?: Promise<void>; captureApproval?: (approval: TestRunApproval | null) => void; cachedRun84?: boolean; captureRenderedApproval?: (approval: TestRunApproval | null) => void } = {}) {
  const actual = await vi.importActual<typeof JobApi>("@/pages/jobs/detail/api");
  primeApi(detail("awaiting_approval", "gate-first", payload) as unknown as Record<string, unknown>);
  mockedFetchJobDefinition.mockResolvedValue({ job_id: "42", available: true, workflow_id: owner.workflowId,
    revision: 3, digest: "recorded-revision-three", steps: [], groups: [] });
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: false, steps: [],
    page: { has_more: false, next_cursor: "" } });
  mockedFetchJobApproval.mockImplementation(actual.fetchJobApproval);
  mockedFetchJobApprovalHistory.mockImplementation(actual.fetchJobApprovalHistory);
  vi.mocked(submitApprovalDecision).mockImplementation(actual.submitApprovalDecision);
  let currentGate: ReturnType<typeof gate> | undefined = gate("gate-first", payload);
  let gate84: ReturnType<typeof gate> | undefined = cachedRun84 ? gate("gate-84", payload, "84") : undefined;
  let decision: JobApi.ApprovalDecisionPayload | undefined;
  let approvalReads = 0;
  const decisions: JobApi.ApprovalDecisionPayload[] = [];
  const requests: Array<{ path: string; method: string }> = [];
  const transport = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = new URL(String(input), "http://localhost").pathname;
    requests.push({ path, method: init?.method || "GET" });
    const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
      status, headers: { "content-type": "application/json" } });
    if (path === "/api/v2/jobs") return json({ jobs: [{ id: "42", project_id: owner.projectId,
      selected_workflow: owner.workflowId, task_source: "workflow_test_run", status: "awaiting_approval" },
      ...(cachedRun84 ? [{ id: "84", project_id: owner.projectId, selected_workflow: owner.workflowId, task_source: "workflow_test_run", status: "awaiting_approval" }] : [])] });
    if (path === `/api/workflows/${owner.workflowId}/kept-test-outputs`) return json({ kept_outputs: [] });
    if (path === "/api/jobs/42/approval") {
      approvalReads += 1;
      return currentGate ? json(currentGate) : json({ code: "approval_not_awaiting" }, 409);
    }
    if (path === "/api/jobs/84/approval" && cachedRun84) return gate84 ? json(gate84) : json({ code: "approval_not_awaiting" }, 409);
    if ((path === "/api/approvals/thread-42" || cachedRun84 && path === "/api/approvals/thread-84") && init?.method === "POST") {
      decision = JSON.parse(String(init.body)) as JobApi.ApprovalDecisionPayload;
      decisions.push(decision);
      await decisionResponse;
      if (path === "/api/approvals/thread-84") gate84 = undefined;
      else currentGate = undefined;
      return json({ status: "in_progress" });
    }
    if (path === "/api/jobs/42/approvals") return json({ job_id: "42", approvals: decision ? [{
      id: "decision-first", ...decision, stage: "Review", decided_at: "2026-10-24T10:00:00Z" }] : [] });
    throw new Error(`Unexpected transport: ${init?.method || "GET"} ${path}`);
  });
  const errors = vi.spyOn(console, "error");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, ...(cachedRun84 ? { staleTime: Infinity } : {}) } } });
  if (cachedRun84) {
    queryClient.setQueryData(queryKeys.jobDetail("42"), detail("awaiting_approval", "gate-first", payload));
    queryClient.setQueryData(queryKeys.jobDetail("84"), detail("awaiting_approval", "gate-84", payload, "84"));
    queryClient.setQueryData(["jobs", "workflow-test-run-approval", "42"], currentGate);
    queryClient.setQueryData(["jobs", "workflow-test-run-approval", "84"], gate84);
    mockedFetchJobDetail.mockImplementation(async (id) => detail("awaiting_approval", id === "84" ? "gate-84" : "gate-first", payload, id));
  }
  const router = createMemoryRouter([{ path: "/builder", element: <Builder graph={graph} showDecision={showDecision} captureApproval={captureApproval} captureRenderedApproval={captureRenderedApproval} showHistory={cachedRun84} /> },
    { path: "/jobs/:jobId", element: <JobsDetailPage /> }], { initialEntries: ["/builder"] });
  const view = render(<I18nContext.Provider value={fallbackContext}><QueryClientProvider client={queryClient}>
    <RouterProvider router={router} /></QueryClientProvider></I18nContext.Provider>);
  return { queryClient, router, errors, decisions, requests, getDecision: () => decision, reads: () => approvalReads,
    setGate: (next: ReturnType<typeof gate>) => { currentGate = next; },
    cleanup: () => { view.unmount(); queryClient.clear(); transport.mockRestore(); errors.mockRestore(); } };
}
describe("builder and full Job Details approval cache ownership (JOB-001)", () => {
  it.each([["reject", false, "failed"], ["approve", true, "completed"]] as const)(
    "keeps a %s decision absent through embedded to full Details navigation", async (action, approved, status) => {
      const test = await mount();
      let releaseDetail: ((value: JobApi.JobDetail) => void) | undefined;
      try {
        await screen.findByTestId("approval-approve-button");
        await waitFor(() => expect(screen.getByTestId("builder-gate")).toHaveTextContent("42:gate-first"));
        await waitFor(() => expect(test.queryClient.getQueryState(detailKey)).toMatchObject({
          status: "success", fetchStatus: "idle" }));
        const pendingDetail = new Promise<JobApi.JobDetail>((resolve) => { releaseDetail = resolve; });
        mockedFetchJobDetail.mockReturnValue(pendingDetail);
        fireEvent.click(screen.getByRole("button", { name: `Builder ${action}` }));
        await waitFor(() => expect(test.getDecision()).toEqual({ approval_id: "gate-first", approved, comment }));
        // Hold terminal detail until the absent-gate refetch settles: hiding the
        // approval controls must not conceal undefined data in an active query.
        await waitFor(() => expect(test.queryClient.getQueryState(builderKey)).toMatchObject({
          status: "success", fetchStatus: "idle", data: null, error: null }));
        await waitFor(() => expect(test.queryClient.getQueryState(detailKey)).toMatchObject({
          status: "success", fetchStatus: "idle", error: null,
          data: { approval: undefined, observedRevision: expect.any(Number) } }));
        expect(screen.getByTestId("builder-gate")).toHaveTextContent("none");
        expect(screen.getByTestId("builder-terminal")).toHaveTextContent("false");
        mockedFetchJobDetail.mockResolvedValue(detail(status));
        mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: true, steps: [],
          page: { has_more: false, next_cursor: "" } });
        await act(async () => { releaseDetail?.(detail(status)); });
        await waitFor(() => expect(screen.getByTestId("builder-terminal")).toHaveTextContent("true"));
        fireEvent.click(screen.getByRole("link", { name: "Open full details" }));
        await waitFor(() => expect(test.router.state.location.pathname).toBe("/jobs/42"));
        await screen.findByTestId("job-primary-container");
        await screen.findByText(comment);
        expect(screen.queryByTestId("builder-gate")).not.toBeInTheDocument();
        expect(screen.queryByTestId("job-approval-error")).not.toBeInTheDocument();
        expect(screen.queryByTestId("approval-approve-button")).not.toBeInTheDocument();
        expect(screen.queryByTestId("approval-reject-button")).not.toBeInTheDocument();
        expect(test.queryClient.getQueryState(builderKey)?.status).toBe("success");
        expect(test.queryClient.getQueryData(detailKey)).toMatchObject({ approval: undefined, observedRevision: expect.any(Number) });
        expect(test.errors.mock.calls.map((args) => args.join(" ")).join("\n")).not.toMatch(/Query data cannot be undefined|data is undefined|\["jobs","detail","42","approval"\]/);
      } finally { releaseDetail?.(detail(status)); test.cleanup(); }
    });
  it("reads a later gate and refreshes that gate without replacing the detail envelope", async () => {
    const test = await mount();
    try {
      await waitFor(() => expect(screen.getByTestId("builder-gate")).toHaveTextContent("42:gate-first"));
      test.setGate(gate("gate-second"));
      mockedFetchJobDetail.mockResolvedValue(detail("awaiting_approval", "gate-second"));
      act(() => { test.queryClient.setQueryData(queryKeys.jobDetail("42"), detail("awaiting_approval", "gate-second")); });
      await waitFor(() => expect(screen.getByTestId("builder-gate")).toHaveTextContent("42:gate-second"));
      const refreshed = gate("gate-second");
      refreshed.payload.message = "Updated request after refresh";
      test.setGate(refreshed);
      const beforeRefresh = test.reads();
      fireEvent.click(screen.getByRole("button", { name: "Refresh builder approval" }));
      await waitFor(() => expect(test.reads()).toBeGreaterThan(beforeRefresh));
      await waitFor(() => expect(test.queryClient.getQueryState(builderKey)?.fetchStatus).toBe("idle"));
      await waitFor(() => expect(screen.getByTestId("builder-message")).toHaveTextContent("Updated request after refresh"));
      expect(test.queryClient.getQueryData(builderKey)).toMatchObject({ approval_id: "gate-second" });
      expect(test.queryClient.getQueryData(detailKey)).toMatchObject({ approval: expect.any(Object), observedRevision: expect.any(Number) });
      expect(screen.getByTestId("builder-gate")).not.toHaveTextContent("gate-first");
    } finally { test.cleanup(); }
  });
});


function decisionGraph(): WorkflowCanvasGraph {
  return {
    workflow: { id: owner.workflowId, display_name: "Weekly report", enabled: true, revision: 3, callable_by_parent: false },
    interface_contract: {}, output_contract: {}, groups: [], validation_issues: [], automation_rules: [],
    steps: [
      { id: "review", kind: "agent", display_name: "Draft report", config_summary: "" },
      { id: "send", kind: "stop_success", display_name: "Send report", config_summary: "" },
    ],
    edges: [{ source: { kind: "step", id: "review" }, outcome: { name: "success" }, target: { kind: "step", id: "send" } }],
  };
}

describe("canonical recovery decision previews (SURF-GUARD-002)", () => {
  it("predicts same-step rework when a failed agent execution is rejected", async () => {
    const test = await mount({ graph: decisionGraph(), showDecision: true, payload: {
      gate_kind: "recovery", recovery_kind: "failure", step_id: "review", step_execution_id: "draft-pass-1",
      allowed_actions: ["approve", "reject"],
      message: "This step failed. Approve to continue past the error on the step's success path, or reject to send it back for rework with your comment.",
    } });
    try {
      const decision = await screen.findByTestId("workflow-approval-decision");
      const outcomes = within(decision).getAllByRole("listitem");
      expect(outcomes[0]).toHaveTextContent("Approved");
      expect(outcomes[0]).toHaveTextContent("Send report");
      expect(outcomes[1]).toHaveTextContent("Rejected");
      expect(outcomes[1]).toHaveTextContent(/rework|retry|again/i);
      expect(outcomes[1]).toHaveTextContent("Draft report");
      expect(outcomes[1]).not.toHaveTextContent("the run ends");
      expect(within(decision).getByRole("button", { name: "Reject" })).toBeEnabled();
      expect(within(decision).getByRole("button", { name: "Approve" })).toBeEnabled();
    } finally { test.cleanup(); }
  });

  it.each([false, true])("keeps authored Approval graph routing with a rejected edge: %s", async (hasRejectedEdge) => {
    const graph = decisionGraph();
    graph.steps[0] = { ...graph.steps[0], kind: "approval", display_name: "Review report" };
    graph.edges[0] = { ...graph.edges[0], outcome: { name: "approved" } };
    if (hasRejectedEdge) {
      graph.steps.push({ id: "revise", kind: "agent", display_name: "Revise report", config_summary: "" });
      graph.edges.push({ source: { kind: "step", id: "review" }, outcome: { name: "rejected" }, target: { kind: "step", id: "revise" } });
    }
    const test = await mount({ graph, showDecision: true, payload: {
      gate_kind: "approval", step_id: "review", step_execution_id: "review-pass-1",
      allowed_actions: ["approve", "reject"], message: "Send this report?",
    } });
    try {
      const decision = await screen.findByTestId("workflow-approval-decision");
      const outcomes = within(decision).getAllByRole("listitem");
      expect(outcomes[0]).toHaveTextContent("Approved → Send report");
      expect(outcomes[1]).toHaveTextContent(hasRejectedEdge ? "Rejected → Revise report" : "Rejected → the run ends");
      expect(within(decision).getByRole("button", { name: "Reject" })).toBeEnabled();
    } finally { test.cleanup(); }
  });

  it("offers approve or cancel at the rework budget gate without a rejected outcome", async () => {
    const test = await mount({ graph: decisionGraph(), showDecision: true, payload: {
      gate_kind: "recovery", recovery_kind: "rework_budget", step_id: "review", step_execution_id: "draft-pass-2",
      allowed_actions: ["approve", "abort"], rework_budget_exhausted: true,
      message: "The rework budget of this run is exhausted. Approve to continue with additional budget, or abort the run.",
    } });
    try {
      const decision = await screen.findByTestId("workflow-approval-decision");
      expect(within(decision).queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
      expect(within(decision).queryByText("Rejected")).not.toBeInTheDocument();
      expect(within(decision).getByRole("button", { name: "Approve" })).toBeEnabled();
      expect(within(decision).getAllByRole("listitem")[0]).toHaveTextContent(/additional.*budget/i);
      expect(within(decision).getAllByRole("listitem")[0]).not.toHaveTextContent("Send report");
      expect(within(decision).getByRole("button", { name: "Cancel run" })).toBeEnabled();
    } finally { test.cleanup(); }
  });
});


it("submits one scoped abort decision and disables repeat cancellation until acknowledgement", async () => {
  let release: (() => void) | undefined;
  const response = new Promise<void>((resolve) => { release = resolve; });
  const test = await mount({ graph: decisionGraph(), showDecision: true, decisionResponse: response, payload: {
    gate_kind: "recovery", recovery_kind: "rework_budget", step_id: "review", step_execution_id: "draft-pass-3",
    allowed_actions: ["approve", "abort"], rework_budget_exhausted: true,
    message: "The report needs more rework budget. Continue or cancel the run.",
  } });
  const invalidations = vi.spyOn(test.queryClient, "invalidateQueries");
  try {
    const decision = await screen.findByTestId("workflow-approval-decision");
    fireEvent.change(within(decision).getByRole("textbox", { name: "Comment" }), { target: { value: "  Stop drafting this report  " } });
    const cancel = within(decision).getByRole("button", { name: "Cancel run" });
    invalidations.mockClear();
    fireEvent.click(cancel);
    await waitFor(() => expect(test.decisions).toEqual([{
      approval_id: "gate-first", approved: false, action: "abort", comment: "Stop drafting this report",
    }]));
    expect(test.requests.filter((request) => request.path === "/api/approvals/thread-42" && request.method === "POST")).toHaveLength(1);
    await waitFor(() => expect(cancel).toBeDisabled());
    expect(within(decision).getByRole("button", { name: "Approve" })).toBeDisabled();
    fireEvent.click(cancel);
    expect(test.decisions).toHaveLength(1);
    expect(test.requests.some((request) => request.path.endsWith("/cancel"))).toBe(false);
    await act(async () => { release?.(); });
    await waitFor(() => expect(invalidations).toHaveBeenCalledWith({ queryKey: builderKey }));
    expect(invalidations).toHaveBeenCalledWith({ queryKey: queryKeys.jobDetail("42") });
    expect(invalidations).toHaveBeenCalledWith({ queryKey: detailKey });
    expect(invalidations).toHaveBeenCalledWith({ queryKey: queryKeys.jobExecutionSteps("42", "") });
    expect(test.decisions).toHaveLength(1);
  } finally { release?.(); invalidations.mockRestore(); test.cleanup(); }
});

it("retires a captured abort action when the same job advances to a different approval gate", async () => {
  const captured: { current: TestRunApproval | null } = { current: null };
  const payload = { gate_kind: "recovery", recovery_kind: "rework_budget", step_id: "review", step_execution_id: "draft-pass-4",
    allowed_actions: ["approve", "abort"], rework_budget_exhausted: true, message: "Continue with more budget?" };
  const test = await mount({ graph: decisionGraph(), showDecision: true, payload,
    captureApproval: (approval) => { if (approval && !captured.current) captured.current = approval; } });
  try {
    await screen.findByTestId("workflow-approval-decision");
    await waitFor(() => expect(captured.current?.identity).toBe("42:gate-first"));
    test.setGate(gate("gate-second", { ...payload, step_execution_id: "draft-pass-5" }));
    mockedFetchJobDetail.mockResolvedValue(detail("awaiting_approval", "gate-second", payload));
    act(() => { test.queryClient.setQueryData(queryKeys.jobDetail("42"), detail("awaiting_approval", "gate-second", payload)); });
    await waitFor(() => expect(screen.getByTestId("builder-gate")).toHaveTextContent("42:gate-second"));
    await act(async () => { captured.current?.abort?.("Old gate"); });
    expect(test.decisions).toEqual([]);
  } finally { test.cleanup(); }
});


it("submits the first rendered decision callback for a newly selected cached run", async () => {
  const first = new Map<string, TestRunApproval>();
  const test = await mount({ graph: decisionGraph(), cachedRun84: true, payload: {
    gate_kind: "recovery", recovery_kind: "rework_budget", step_id: "review", step_execution_id: "draft-pass",
    allowed_actions: ["approve", "abort"],
  }, captureRenderedApproval: (approval) => { if (approval && !first.has(approval.identity)) first.set(approval.identity, approval); } });
  try {
    await waitFor(() => expect(screen.getByTestId("builder-gate")).toHaveTextContent("42:gate-first"));
    fireEvent.click(await screen.findByRole("button", { name: "Select run 84" }));
    await waitFor(() => expect(screen.getByTestId("builder-gate")).toHaveTextContent("84:gate-84"));
    // This is the callback from the first real 84 render, not a later callback
    // repaired incidentally by snapshot acceptance or another query render.
    await act(async () => { first.get("84:gate-84")?.abort?.("Stop this report"); });
    await waitFor(() => expect(test.decisions).toEqual([{
      approval_id: "gate-84", approved: false, action: "abort", comment: "Stop this report",
    }]));
    expect(test.requests.filter((request) => request.method === "POST")).toEqual([
      { path: "/api/approvals/thread-84", method: "POST" },
    ]);
  } finally { test.cleanup(); }
});

it("keeps an old cached-run callback retired after selecting another run and returning", async () => {
  const first = new Map<string, TestRunApproval>();
  const test = await mount({ graph: decisionGraph(), cachedRun84: true, payload: {
    gate_kind: "recovery", recovery_kind: "rework_budget", step_id: "review", step_execution_id: "draft-pass",
    allowed_actions: ["approve", "abort"],
  }, captureRenderedApproval: (approval) => { if (approval && !first.has(approval.identity)) first.set(approval.identity, approval); } });
  try {
    await waitFor(() => expect(screen.getByTestId("builder-gate")).toHaveTextContent("42:gate-first"));
    fireEvent.click(await screen.findByRole("button", { name: "Select run 84" }));
    await waitFor(() => expect(screen.getByTestId("builder-gate")).toHaveTextContent("84:gate-84"));
    fireEvent.click(screen.getByRole("button", { name: "Select run 42" }));
    await waitFor(() => expect(screen.getByTestId("builder-gate")).toHaveTextContent("42:gate-first"));
    await act(async () => { first.get("42:gate-first")?.abort?.("Old report selection"); });
    expect(test.decisions).toEqual([]);
    expect(test.requests.filter((request) => request.method === "POST")).toEqual([]);
  } finally { test.cleanup(); }
});
