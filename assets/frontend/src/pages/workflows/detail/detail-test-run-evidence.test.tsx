import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, Link, RouterProvider } from "react-router";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { queryKeys } from "@/lib/query/keys";
import type * as JobApi from "@/pages/jobs/detail/api";
import { jobPayload, mockedFetchJobApproval, mockedFetchJobApprovalHistory, mockedFetchJobDefinition,
  mockedFetchJobDetail, mockedFetchJobExecutionStepsPage, primeApi } from "@/pages/jobs/detail/index.test-helpers";
import { JobsDetailPage } from "@/pages/jobs/detail";
import { submitApprovalDecision } from "@/pages/jobs/detail/api";
import { useSelectedWorkflowTestRun } from "./detail-selected-test-run";
import { useWorkflowTestRunEvidence } from "./detail-test-run-evidence";

const owner = { workflowId: "wf_weekly_report", projectId: "project-alpha", editorRevision: 3 };
const builderKey = ["jobs", "workflow-test-run-approval", "42"];
const detailKey = ["jobs", "detail", "42", "approval"];
const comment = "Reviewed this recorded run";
function gate(approvalId = "gate-first") {
  return { job_id: "42", thread_id: "thread-42", approval_id: approvalId,
    payload: { approval_id: approvalId, thread_id: "thread-42", step_id: "review", message: "Review the recorded run",
      allowed_actions: ["approve", "reject"] } };
}
function detail(status = "awaiting_approval", approvalId = "gate-first") {
  const awaiting = status === "awaiting_approval";
  return jobPayload({ id: "42", project_id: owner.projectId, selected_workflow: owner.workflowId,
    task_source: "workflow_test_run", status, thread_id: "thread-42", approval_id: awaiting ? approvalId : null,
    lifecycle: { status: { code: status, label: status, terminal: ["completed", "failed"].includes(status) },
      approval: awaiting ? { state: "awaiting_approval", gate: gate(approvalId).payload } : { state: "none" } },
    execution_steps: [] }) as unknown as JobApi.JobDetail;
}
function Builder() {
  const selectedRun = useSelectedWorkflowTestRun({ owner, restoredJobId: "42", resultsOwnsObservation: true,
    confirmReadOnlyNavigation: async () => true });
  const evidence = useWorkflowTestRunEvidence({ selectedRun, workflowId: owner.workflowId, canvasGraph: null,
    runBar: { history: { kind: "ready", rows: [], latestJobId: "42" }, onSelectHistory: () => undefined },
    openStep: () => undefined, lightStep: () => undefined, changeInputs: () => undefined });
  return <>
    <output data-testid="builder-gate">{evidence.approval?.identity || "none"}</output>
    <output data-testid="builder-message">{evidence.approval?.message || "none"}</output>
    <output data-testid="builder-terminal">{String(evidence.terminal)}</output>
    <button onClick={() => evidence.approval?.decide(true, comment)}>Builder approve</button>
    <button onClick={() => evidence.approval?.decide(false, comment)}>Builder reject</button>
    <button onClick={evidence.refresh}>Refresh builder approval</button>
    <Link to="/jobs/42">Open full details</Link>
    <JobsDetailPage embedded jobId="42" expectedWorkflowId={owner.workflowId} expectedProjectId={owner.projectId}
      executionOwner={{ ...owner, jobId: "42", generation: 1, isCurrent: () => true }} />
  </>;
}
async function mount() {
  const actual = await vi.importActual<typeof JobApi>("@/pages/jobs/detail/api");
  primeApi(detail() as unknown as Record<string, unknown>);
  mockedFetchJobDefinition.mockResolvedValue({ job_id: "42", available: true, workflow_id: owner.workflowId,
    revision: 3, digest: "recorded-revision-three", steps: [], groups: [] });
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: false, steps: [],
    page: { has_more: false, next_cursor: "" } });
  mockedFetchJobApproval.mockImplementation(actual.fetchJobApproval);
  mockedFetchJobApprovalHistory.mockImplementation(actual.fetchJobApprovalHistory);
  vi.mocked(submitApprovalDecision).mockImplementation(actual.submitApprovalDecision);
  let currentGate: ReturnType<typeof gate> | undefined = gate();
  let decision: JobApi.ApprovalDecisionPayload | undefined;
  let approvalReads = 0;
  const transport = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = new URL(String(input), "http://localhost").pathname;
    const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
      status, headers: { "content-type": "application/json" } });
    if (path === "/api/v2/jobs") return json({ jobs: [{ id: "42", project_id: owner.projectId,
      selected_workflow: owner.workflowId, task_source: "workflow_test_run", status: "awaiting_approval" }] });
    if (path === `/api/workflows/${owner.workflowId}/kept-test-outputs`) return json({ kept_outputs: [] });
    if (path === "/api/jobs/42/approval") {
      approvalReads += 1;
      return currentGate ? json(currentGate) : json({ code: "approval_not_awaiting" }, 409);
    }
    if (path === "/api/approvals/thread-42" && init?.method === "POST") {
      decision = JSON.parse(String(init.body)) as JobApi.ApprovalDecisionPayload;
      currentGate = undefined;
      return json({ status: "in_progress" });
    }
    if (path === "/api/jobs/42/approvals") return json({ job_id: "42", approvals: decision ? [{
      id: "decision-first", ...decision, stage: "Review", decided_at: "2026-10-24T10:00:00Z" }] : [] });
    throw new Error(`Unexpected transport: ${init?.method || "GET"} ${path}`);
  });
  const errors = vi.spyOn(console, "error");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([{ path: "/builder", element: <Builder /> },
    { path: "/jobs/:jobId", element: <JobsDetailPage /> }], { initialEntries: ["/builder"] });
  const view = render(<I18nContext.Provider value={fallbackContext}><QueryClientProvider client={queryClient}>
    <RouterProvider router={router} /></QueryClientProvider></I18nContext.Provider>);
  return { queryClient, router, errors, getDecision: () => decision, reads: () => approvalReads,
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
