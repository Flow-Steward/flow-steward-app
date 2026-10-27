import type { PropsWithChildren } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { queryKeys } from "@/lib/query/keys";
import { fetchJobDetail, type JobDetail, type JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import { fetchJobsList, type JobListItem } from "@/pages/jobs/list/api";
import { useJobDetailQuery } from "@/pages/jobs/detail/use-job-detail-query";
import { useJobExecutionSnapshot } from "@/pages/jobs/detail/job-execution-snapshot";
import { useSelectedWorkflowTestRun, type TestRunOwner } from "./detail-selected-test-run";

vi.mock("@/pages/jobs/detail/api", async (original) => ({ ...(await original<Record<string, unknown>>()), fetchJobDetail: vi.fn() }));
vi.mock("@/pages/jobs/list/api", async (original) => ({ ...(await original<Record<string, unknown>>()), fetchJobsList: vi.fn() }));
vi.mock("@/pages/jobs/detail/job-execution-snapshot", () => ({ useJobExecutionSnapshot: vi.fn() }));
type JobsListResult = Awaited<ReturnType<typeof fetchJobsList>>;
const owner: TestRunOwner = { workflowId: "workflow", projectId: "alpha", editorRevision: 4 };
function row(id: string, extra: Partial<JobListItem> = {}): JobListItem {
  return { id, job_id: id, selected_workflow: "workflow", project_id: "alpha", task_source: "workflow_test_run", status: "completed", duration_ms: 0, ...extra };
}
function job(id: string, extra: Partial<JobDetail> = {}): JobDetail {
  return { id, status: "completed", selected_workflow: "workflow", project_id: "alpha", duration_ms: 0,
    job: { id, status: "completed", selected_workflow: "workflow", project_id: "alpha" }, ...extra };
}
function ack(jobId: string) { return { job_id: jobId, workflow_id: "workflow", project_id: "alpha", status: "queued", needs_user_approval: false }; }
function snapshot(jobId: string, revision = 4): JobExecutionSnapshot {
  return { jobId, definitionWorkflowId: "workflow", definitionRevision: revision, definitionSteps: [{ stepId: "plan" }],
    steps: [{ stepId: "plan", stepExecutionId: "plan-1", status: "completed", durationMs: 17 }], childInvocations: [], complete: true, terminal: true };
}
function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((accept) => { resolve = accept; });
  return { promise, resolve };
}
function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: Infinity } } });
  const Wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { client, Wrapper };
}
beforeEach(() => {
  vi.mocked(fetchJobsList).mockReset().mockResolvedValue({ groups: undefined, jobs: [row("latest"), row("older")], page: 1, per_page: 8 });
  vi.mocked(fetchJobDetail).mockReset().mockImplementation(async (id) => job(id));
  vi.mocked(useJobExecutionSnapshot).mockReset().mockReturnValue({ snapshot: null, pending: false, error: null });
});

it.each(["completed", "failed", "cancelled", "legacy_unknown"])("shows the latest %s run after a reload once its own detail verifies, without an ACK or discarding history", async (status) => {
  const { client, Wrapper } = setup();
  vi.mocked(fetchJobsList).mockResolvedValue({ groups: undefined, jobs: [row("latest", { status }), row("older")] });
  const pending = deferred<JobDetail>();
  vi.mocked(fetchJobDetail).mockImplementation((id) => id === "latest" ? pending.promise : Promise.resolve(job(id)));
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await waitFor(() => expect(result.current.history.map((item) => item.id)).toEqual(["latest", "older"]));
    expect(result.current.serverLatest?.id).toBe("latest");
    // The list row alone selects nothing: no proof, marks or snapshot before verification.
    await waitFor(() => expect(fetchJobDetail).toHaveBeenCalledWith("latest"));
    expect(result.current.selected).toBeNull();
    expect(result.current.captureSelection()?.jobId ?? null).toBe("latest");
    expect(result.current.snapshot).toBeNull();
    expect(result.current.markEligible).toBe(false);
    await act(async () => { pending.resolve(job("latest")); await pending.promise; });
    await waitFor(() => expect(result.current.selected?.jobId).toBe("latest"));
    expect(result.current.verifiedDetail?.id).toBe("latest");
    expect(fetchJobDetail).not.toHaveBeenCalledWith("older");
    expect(result.current.acknowledgedJobId).toBe("");
    expect(result.current.history.map((item) => item.id)).toEqual(["latest", "older"]);
  } finally { pending.resolve(job("latest")); unmount(); client.clear(); }
});

it("never reselects the latest finished run by itself after Clear marks", async () => {
  const { client, Wrapper } = setup();
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await waitFor(() => expect(result.current.selected?.jobId).toBe("latest"));
    await act(async () => { expect(await result.current.requestClearMarks()).toBe(true); });
    expect(result.current.selected).toBeNull();
    await act(async () => { await client.invalidateQueries({ queryKey: ["jobs", "workflow-test-run-history"] }); });
    await waitFor(() => expect(result.current.history).toHaveLength(2));
    expect(result.current.selected).toBeNull();
    expect(result.current.captureSelection()).toBeNull();
  } finally { unmount(); client.clear(); }
});

it("verifies a remembered older terminal job without turning that storage pointer into a selected Result", async () => {
  const { client, Wrapper } = setup();
  const openResults = vi.fn(async () => true);
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, restoredJobId: "older", resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true, openResults }), { wrapper: Wrapper });
  try {
    await waitFor(() => expect(fetchJobDetail).toHaveBeenCalledWith("older"));
    await waitFor(() => expect(result.current.history).toHaveLength(2));
    await waitFor(() => expect(result.current.acknowledgedJobId).toBe("older"));
    // The finished pointer is not the server latest: the latest run is shown instead.
    await waitFor(() => expect(result.current.selected?.jobId).toBe("latest"));
    expect(result.current.captureSelection()?.jobId).toBe("latest");
    expect(result.current.serverLatest?.id).toBe("latest");
    expect(openResults).not.toHaveBeenCalled();
  } finally { unmount(); client.clear(); }
});

it.each(["in_progress", "awaiting_timer", "awaiting_approval"])("retains an observed %s execution when it completes, even below a newer terminal row", async (status) => {
  const { client, Wrapper } = setup();
  vi.mocked(fetchJobsList).mockResolvedValue({ groups: undefined, jobs: [row("latest"), row("active", { status })] });
  vi.mocked(fetchJobDetail).mockImplementation(async (id) => job(id, { status, job: { ...job(id).job, status } }));
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("active"));
    expect(result.current.selected?.jobId).toBe("active");
    expect(result.current.serverLatest?.id).toBe("latest");
    const proof = result.current.captureSelection();
    if (!proof) throw new Error("Observed execution proof missing");
    act(() => { client.setQueryData(queryKeys.jobDetail("active"), job("active")); });
    await waitFor(() => expect(result.current.statusCode).toBe("completed"));
    expect(result.current.captureSelection()).toEqual(proof);
    expect(result.current.selected?.jobId).toBe("active");
    expect(result.current.durationMs).toBe(0);
    expect(result.current.acknowledgedJobId).toBe("");
  } finally { unmount(); client.clear(); }
});

it("does not reveal a terminal non-latest Result from a stale active history hint", async () => {
  const { client, Wrapper } = setup();
  vi.mocked(fetchJobsList).mockResolvedValue({ groups: undefined, jobs: [row("latest", { status: "cancelled" }), row("stale", { status: "in_progress" })] });
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await waitFor(() => expect(fetchJobDetail).toHaveBeenCalledWith("stale"));
    await waitFor(() => expect(result.current.history).toHaveLength(2));
    // The stale hint verifies as finished and is not the latest: never shown; the latest is.
    await waitFor(() => expect(result.current.selected?.jobId).toBe("latest"));
    expect(result.current.serverLatest?.id).toBe("latest");
    await act(async () => { await client.invalidateQueries({ queryKey: ["jobs", "workflow-test-run-history"] }); });
    expect(result.current.selected?.jobId).toBe("latest");
  } finally { unmount(); client.clear(); }
});

it("opens a terminal run only after explicit guarded history selection and preserves zero duration", async () => {
  const { client, Wrapper } = setup();
  const confirm = vi.fn(async () => true);
  const openResults = vi.fn(async (isCurrent: () => boolean) => isCurrent());
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false, confirmReadOnlyNavigation: confirm, openResults }), { wrapper: Wrapper });
  try {
    await waitFor(() => expect(result.current.history).toHaveLength(2));
    await waitFor(() => expect(result.current.selected?.jobId).toBe("latest"));
    expect(confirm).not.toHaveBeenCalled();
    await act(async () => { expect(await result.current.requestSelectHistory("older")).toBe(true); });
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("older"));
    expect(result.current.selected?.jobId).toBe("older");
    expect(result.current.durationMs).toBe(0);
    await act(async () => { expect(await result.current.requestOpenResults()).toBe(true); });
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(openResults).toHaveBeenCalledTimes(1);
    expect(result.current.acknowledgedJobId).toBe("");
    expect(result.current.serverLatest?.id).toBe("latest");
  } finally { unmount(); client.clear(); }
});

async function selectHistoryForSetup(result: { current: ReturnType<typeof useSelectedWorkflowTestRun> }, jobId = "latest") {
  await waitFor(() => expect(result.current.history.some((item) => item.id === jobId)).toBe(true));
  await act(async () => { expect(await result.current.requestSelectHistory(jobId)).toBe(true); });
}

it("uses only current whole-workflow history in server order and keeps ACK separate from historical selection", async () => {
  const { client, Wrapper } = setup();
  vi.mocked(fetchJobsList).mockResolvedValue({ groups: undefined, jobs: [
    row("step", { task_source: "step_test_run" }), row("foreign-project", { project_id: "beta" }),
    row("foreign-workflow", { selected_workflow: "other" }), row("latest"), row("older"),
  ] });
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    expect(fetchJobsList).toHaveBeenCalledWith({ page: 1, perPage: 8, status: "", workflowId: "workflow", projectId: "alpha", taskSource: "workflow_test_run" });
    expect(result.current.history.map((entry) => entry.id)).toEqual(["latest", "older"]);
    expect(result.current.serverLatest?.id).toBe("latest");
    const submission = result.current.captureSubmissionOwner();
    if (!submission) throw new Error("Submission proof missing");
    act(() => { expect(result.current.acceptAcknowledgement(ack("new-ack"), submission)).toBe(true); });
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("new-ack"));
    await waitFor(() => expect(result.current.history).toHaveLength(2));
    expect(result.current.history.some((entry) => entry.id === "new-ack")).toBe(false);
    await act(async () => { expect(await result.current.requestSelectHistory("older")).toBe(true); });
    expect(result.current.selected?.proof.jobId).toBe("older");
    expect(result.current.acknowledgedJobId).toBe("new-ack");
    await act(async () => { expect(await result.current.requestBackToLatest()).toBe(true); });
    expect(result.current.selected?.proof.jobId).toBe("latest");
    expect(result.current.acknowledgedJobId).toBe("new-ack");
    expect(fetchJobDetail).not.toHaveBeenCalledWith("step");
    expect(fetchJobDetail).not.toHaveBeenCalledWith("foreign-project");
  } finally { unmount(); client.clear(); }
});

it("does not let a pre-ACK held history result replace the new ACK or authoritative refreshed latest", async () => {
  const { client, Wrapper } = setup();
  const old = deferred<JobsListResult>();
  vi.mocked(fetchJobsList).mockImplementationOnce(() => old.promise).mockResolvedValue({ groups: undefined, jobs: [row("new-ack"), row("latest")] });
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await waitFor(() => expect(fetchJobsList).toHaveBeenCalledTimes(1));
    const submission = result.current.captureSubmissionOwner();
    if (!submission) throw new Error("Submission proof missing");
    act(() => { expect(result.current.acceptAcknowledgement(ack("new-ack"), submission)).toBe(true); });
    await waitFor(() => expect(result.current.selected?.proof.jobId).toBe("new-ack"));
    await waitFor(() => expect(result.current.serverLatest?.id).toBe("new-ack"));
    await act(async () => { old.resolve({ groups: undefined, jobs: [row("old-latest")] }); await old.promise; });
    expect(result.current.selected?.proof.jobId).toBe("new-ack");
    expect(result.current.serverLatest?.id).toBe("new-ack");
    expect(result.current.history.some((entry) => entry.id === "old-latest")).toBe(false);
  } finally { old.resolve({ groups: undefined, jobs: [] }); unmount(); client.clear(); }
});

it("fences owner ABA ACK/snapshot callbacks while refusing a foreign restored project", async () => {
  const { client, Wrapper } = setup();
  vi.mocked(fetchJobsList).mockImplementation(async (filters) => ({ groups: undefined, jobs: [row(filters.projectId === "beta" ? "beta-run" : "latest", { project_id: filters.projectId || "" })] }));
  vi.mocked(fetchJobDetail).mockImplementation(async (id) => job(id, { project_id: id === "beta-run" ? "beta" : "alpha",
    job: { id, status: "completed", selected_workflow: "workflow", project_id: id === "beta-run" ? "beta" : "alpha" } }));
  const { result, rerender, unmount } = renderHook(({ currentOwner }) => useSelectedWorkflowTestRun({ owner: currentOwner, restoredJobId: "latest",
    resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { initialProps: { currentOwner: owner }, wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    const oldProof = result.current.captureSelection();
    const oldSubmission = result.current.captureSubmissionOwner();
    if (!oldProof || !oldSubmission) throw new Error("Initial owner proof missing");
    rerender({ currentOwner: { ...owner, projectId: "beta" } });
    expect(result.current.selected).toBeNull();
    await selectHistoryForSetup(result, "beta-run");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("beta-run"));
    rerender({ currentOwner: owner });
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    const currentProof = result.current.captureSelection();
    if (!currentProof) throw new Error("Current selection proof missing");
    expect(currentProof.generation).toBeGreaterThan(oldProof.generation);
    const currentSnapshot = snapshot("latest");
    act(() => { result.current.acceptSnapshot(currentProof, currentSnapshot); });
    act(() => {
      expect(result.current.acceptAcknowledgement(ack("old-late-ack"), oldSubmission)).toBe(false);
      result.current.acceptSnapshot(oldProof, null);
      result.current.acceptSnapshot(oldProof, snapshot("latest"));
    });
    expect(result.current.snapshot).toBe(currentSnapshot);
    expect(result.current.selected?.proof.jobId).toBe("latest");
  } finally { unmount(); client.clear(); }
});

it("preserves selected historical job across editor revision changes while retiring marks/callbacks", async () => {
  const { client, Wrapper } = setup();
  const { result, rerender, unmount } = renderHook(({ revision }) => useSelectedWorkflowTestRun({ owner: { ...owner, editorRevision: revision },
    resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { initialProps: { revision: 4 }, wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    await act(async () => { await result.current.requestSelectHistory("older"); });
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("older"));
    const oldProof = result.current.captureSelection();
    if (!oldProof) throw new Error("Historical proof missing");
    act(() => { result.current.acceptSnapshot(oldProof, snapshot("older")); });
    expect(result.current.markEligible).toBe(true);
    rerender({ revision: 5 });
    expect(result.current.selected?.proof.jobId).toBe("older");
    expect(result.current.markEligible).toBe(false);
    const newProof = result.current.captureSelection();
    if (!newProof) throw new Error("Revised selection proof missing");
    const archive = snapshot("older", 4);
    act(() => { result.current.acceptSnapshot(newProof, archive); result.current.acceptSnapshot(oldProof, null); });
    expect(result.current.snapshot).toBe(archive);
    expect(result.current.markEligible).toBe(false);
    expect(result.current.isCurrent(oldProof)).toBe(false);
  } finally { unmount(); client.clear(); }
});

it("preserves real failed status and aggregate zero without substituting per-pass duration", async () => {
  const { client, Wrapper } = setup();
  vi.mocked(fetchJobsList).mockResolvedValue({ groups: undefined, jobs: [row("latest", { status: "failed", duration_ms: null })] });
  vi.mocked(fetchJobDetail).mockResolvedValue(job("latest", { status: "failed", duration_ms: 0,
    job: { id: "latest", status: "failed", selected_workflow: "workflow", project_id: "alpha" } }));
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result);
    await waitFor(() => expect(result.current.statusCode).toBe("failed"));
    expect(result.current.durationMs).toBe(0);
    const proof = result.current.captureSelection();
    if (!proof) throw new Error("Selected proof missing");
    act(() => { result.current.acceptSnapshot(proof, snapshot("latest")); });
    expect(result.current.durationMs).toBe(0);
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), job("latest", { duration_ms: null,
      job: { id: "latest", status: "failed", selected_workflow: "workflow", project_id: "alpha" } })); });
    await waitFor(() => expect(result.current.durationMs).toBeNull());
    expect(result.current.statusCode).toBe("failed");
  } finally { unmount(); client.clear(); }
});

it("hands the existing compact detail query between page and Results observers using one cache entry", async () => {
  const { client, Wrapper } = setup();
  const first = deferred<JobDetail>();
  vi.mocked(fetchJobDetail).mockImplementationOnce(() => first.promise).mockResolvedValue(job("shared"));
  const { result, rerender, unmount } = renderHook(({ resultsOpen }) => ({
    page: useJobDetailQuery("shared", !resultsOpen), results: useJobDetailQuery("shared", resultsOpen),
  }), { initialProps: { resultsOpen: false }, wrapper: Wrapper });
  try {
    await waitFor(() => expect(fetchJobDetail).toHaveBeenCalledTimes(1));
    const detail = job("shared");
    await act(async () => { first.resolve(detail); await first.promise; });
    await waitFor(() => expect(result.current.page.data).toBe(detail));
    expect(result.current.results.data).toBe(detail);
    rerender({ resultsOpen: true });
    expect(result.current.results.data).toBe(detail);
    expect(result.current.page.data).toBe(detail);
    expect(client.getQueryCache().findAll({ queryKey: queryKeys.jobDetail("shared"), exact: true })).toHaveLength(1);
    expect(fetchJobDetail).toHaveBeenCalledTimes(1);
    await act(async () => { await client.invalidateQueries({ queryKey: queryKeys.jobDetail("shared"), exact: true }); });
    expect(fetchJobDetail).toHaveBeenCalledTimes(2);
  } finally { first.resolve(job("shared")); unmount(); client.clear(); }
});

const detailAliasConflicts: readonly [string, (source: JobDetail) => JobDetail][] = [
  ["top id", (source) => ({ ...source, id: "other-job" })],
  ["nested job id", (source) => ({ ...source, job: { ...source.job, id: "other-job" } })],
  ["current_project_id", (source) => ({ ...source, current_project_id: "beta" })],
  ["top project_id", (source) => ({ ...source, project_id: "beta" })],
  ["top projectId", (source) => ({ ...source, projectId: "beta" })],
  ["top scalar project", (source) => ({ ...source, project: "beta" })],
  ["nested job project_id", (source) => ({ ...source, job: { ...source.job, project_id: "beta" } })],
  ["nested job projectId", (source) => ({ ...source, job: { ...source.job, projectId: "beta" } })],
  ["nested job scalar project", (source) => ({ ...source, job: { ...source.job, project: "beta" } })],
  ["project object id", (source) => ({ ...source, project: { id: "beta" } })],
  ["project object project_id", (source) => ({ ...source, project: { project_id: "beta" } })],
  ["top selected_workflow", (source) => ({ ...source, selected_workflow: "other-workflow" })],
  ["nested job selected_workflow", (source) => ({ ...source, job: { ...source.job, selected_workflow: "other-workflow" } })],
  ["runtime selected_workflow", (source) => ({ ...source, runtime: { selected_workflow: "other-workflow" } })],
];
it.each(detailAliasConflicts)("rejects a settled contradictory canonical detail alias (%s)", async (_label, conflict) => {
  const { client, Wrapper } = setup();
  const response = conflict(job("latest"));
  vi.mocked(fetchJobDetail).mockResolvedValue(response);
  const openResults = vi.fn(async () => true);
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false,
    confirmReadOnlyNavigation: async () => true, openResults }), { wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result);
    await waitFor(() => expect(result.current.detailQuery.isSuccess).toBe(true));
    expect(result.current.detailQuery.data).toBe(response); // real query processing settled
    expect(result.current.verifiedDetail).toBeNull();
    expect(result.current.snapshot).toBeNull();
    expect(result.current.markEligible).toBe(false);
    expect(result.current.statusCode).toBeNull();
    expect(result.current.durationMs).toBeNull();
    expect(await result.current.requestOpenResults()).toBe(false);
    expect(openResults).not.toHaveBeenCalled();
  } finally { unmount(); client.clear(); }
});

it.each(["consistent", "optional-empty"] as const)("accepts correctly shaped canonical aliases (%s)", async (mode) => {
  const { client, Wrapper } = setup();
  const response = mode === "consistent" ? job("latest", {
    current_project_id: "alpha", projectId: "alpha", project: { id: "alpha", project_id: "alpha" },
    job: { id: "latest", project_id: "alpha", projectId: "alpha", project: "alpha", selected_workflow: "workflow", status: "completed" },
    runtime: { selected_workflow: "workflow" },
  }) : job("latest", {
    current_project_id: null, projectId: "", project: { id: null, project_id: "" }, selected_workflow: "",
    job: { id: "latest", project_id: "alpha", projectId: null, project: "", selected_workflow: "workflow", status: "completed" },
    runtime: { selected_workflow: null },
  });
  vi.mocked(fetchJobDetail).mockResolvedValue(response);
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false,
    confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result);
    await waitFor(() => expect(result.current.verifiedDetail).toBe(response));
    expect(result.current.statusCode).toBe("completed");
    expect(result.current.durationMs).toBe(0);
    expect(result.current.selected?.proof.jobId).toBe("latest");
  } finally { unmount(); client.clear(); }
});

it("keeps the selected ACK, snapshot and pointers when read-only history and Back navigation are cancelled", async () => {
  const { client, Wrapper } = setup();
  const confirm = vi.fn(async () => false);
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false,
    confirmReadOnlyNavigation: confirm }), { wrapper: Wrapper });
  try {
    await waitFor(() => expect(result.current.history).toHaveLength(2));
    const submission = result.current.captureSubmissionOwner();
    if (!submission) throw new Error("Submission proof missing");
    act(() => { result.current.acceptAcknowledgement(ack("ack-selected"), submission); });
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("ack-selected"));
    await waitFor(() => expect(result.current.serverLatest?.id).toBe("latest"));
    const selected = result.current.captureSelection();
    if (!selected) throw new Error("Selected ACK proof missing");
    const selectedSnapshot = snapshot("ack-selected");
    act(() => { result.current.acceptSnapshot(selected, selectedSnapshot); });
    await act(async () => {
      expect(await result.current.requestSelectHistory("older")).toBe(false);
      expect(await result.current.requestBackToLatest()).toBe(false);
    });
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(result.current.captureSelection()).toEqual(selected);
    expect(result.current.acknowledgedJobId).toBe("ack-selected");
    expect(result.current.snapshot).toBe(selectedSnapshot);
    expect(result.current.serverLatest?.id).toBe("latest");
    expect(fetchJobDetail).not.toHaveBeenCalledWith("older");
  } finally { unmount(); client.clear(); }
});

it("rejects a held history-navigation intent after owner replacement without changing the new selection", async () => {
  const { client, Wrapper } = setup();
  const confirmation = deferred<boolean>();
  const confirm = vi.fn<((isCurrent: () => boolean) => Promise<boolean>)>(async () => true);
  vi.mocked(fetchJobsList).mockImplementation(async (filters) => ({ groups: undefined, jobs: filters.projectId === "beta"
    ? [row("beta-run", { project_id: "beta" })] : [row("latest"), row("older")] }));
  vi.mocked(fetchJobDetail).mockImplementation(async (id) => job(id, { project_id: id === "beta-run" ? "beta" : "alpha",
    job: { id, status: "completed", selected_workflow: "workflow", project_id: id === "beta-run" ? "beta" : "alpha" } }));
  const { result, rerender, unmount } = renderHook(({ currentOwner }) => useSelectedWorkflowTestRun({ owner: currentOwner,
    resultsOwnsObservation: false, confirmReadOnlyNavigation: confirm }), { initialProps: { currentOwner: owner }, wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    confirm.mockClear();
    confirm.mockImplementation(() => confirmation.promise);
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.requestSelectHistory("older"); });
    expect(confirm).toHaveBeenCalledTimes(1);
    const oldIntentCurrent = confirm.mock.calls[0][0];
    expect(oldIntentCurrent()).toBe(true);
    rerender({ currentOwner: { ...owner, projectId: "beta" } });
    confirm.mockResolvedValueOnce(true);
    await selectHistoryForSetup(result, "beta-run");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("beta-run"));
    const replacement = result.current.captureSelection();
    expect(oldIntentCurrent()).toBe(false);
    await act(async () => { confirmation.resolve(true); expect(await pending).toBe(false); });
    expect(result.current.captureSelection()).toEqual(replacement);
    expect(result.current.selected?.proof.projectId).toBe("beta");
    expect(result.current.acknowledgedJobId).toBe("");
    expect(fetchJobDetail).not.toHaveBeenCalledWith("older");
  } finally { confirmation.resolve(false); unmount(); client.clear(); }
});

it("does not commit an old Back-to-latest target after authoritative history changes during confirmation", async () => {
  const { client, Wrapper } = setup();
  const confirmation = deferred<boolean>();
  const confirm = vi.fn<((isCurrent: () => boolean) => Promise<boolean>)>(async () => true);
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false,
    confirmReadOnlyNavigation: confirm }), { wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    confirm.mockClear();
    await act(async () => { expect(await result.current.requestSelectHistory("older")).toBe(true); });
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("older"));
    const selected = result.current.captureSelection();
    confirm.mockImplementationOnce(() => confirmation.promise);
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.requestBackToLatest(); });
    expect(confirm).toHaveBeenCalledTimes(2);
    const oldLatestCurrent = confirm.mock.calls[1][0];
    expect(oldLatestCurrent()).toBe(true);
    vi.mocked(fetchJobsList).mockResolvedValueOnce({ groups: undefined, jobs: [row("new-server-latest"), row("latest"), row("older")] });
    await act(async () => { await client.invalidateQueries({ queryKey: ["jobs", "workflow-test-run-history"] }); });
    await waitFor(() => expect(result.current.serverLatest?.id).toBe("new-server-latest"));
    expect(oldLatestCurrent()).toBe(false);
    await act(async () => { confirmation.resolve(true); expect(await pending).toBe(false); });
    expect(result.current.captureSelection()).toEqual(selected);
    expect(result.current.selected?.proof.jobId).toBe("older");
    expect(result.current.serverLatest?.id).toBe("new-server-latest");
    expect(result.current.acknowledgedJobId).toBe("");
    expect(fetchJobDetail).not.toHaveBeenCalledWith("new-server-latest");
  } finally { confirmation.resolve(false); unmount(); client.clear(); }
});

it("supplies a post-await Results predicate before a guard-adapter setter, without claiming drawer integration", async () => {
  const { client, Wrapper } = setup();
  const oldConfirmation = deferred<void>();
  const currentConfirmation = deferred<void>();
  // Explicit adapter boundary double: the actual drawer is outside this hook test.
  const simulatedDrawerSetter = vi.fn();
  const openResults = vi.fn<((isCurrent: () => boolean) => Promise<boolean>)>(async (isCurrent) => {
    await oldConfirmation.promise;
    if (!isCurrent()) return false;
    simulatedDrawerSetter();
    return true;
  });
  vi.mocked(fetchJobsList).mockImplementation(async (filters) => ({ groups: undefined, jobs: [row(filters.projectId === "beta" ? "beta-run" : "latest", { project_id: filters.projectId || "" })] }));
  vi.mocked(fetchJobDetail).mockImplementation(async (id) => job(id, { project_id: id === "beta-run" ? "beta" : "alpha",
    job: { id, status: "completed", selected_workflow: "workflow", project_id: id === "beta-run" ? "beta" : "alpha" } }));
  const { result, rerender, unmount } = renderHook(({ currentOwner }) => useSelectedWorkflowTestRun({ owner: currentOwner,
    resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true, openResults }), { initialProps: { currentOwner: owner }, wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    let oldOpen!: Promise<boolean>;
    act(() => { oldOpen = result.current.requestOpenResults(); });
    expect(simulatedDrawerSetter).not.toHaveBeenCalled();
    rerender({ currentOwner: { ...owner, projectId: "beta" } });
    await selectHistoryForSetup(result, "beta-run");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("beta-run"));
    const replacement = result.current.captureSelection();
    expect(openResults.mock.calls[0][0]()).toBe(false);
    await act(async () => { oldConfirmation.resolve(); expect(await oldOpen).toBe(false); });
    expect(simulatedDrawerSetter).not.toHaveBeenCalled();
    expect(result.current.captureSelection()).toEqual(replacement);

    openResults.mockImplementationOnce(async (isCurrent) => {
      await currentConfirmation.promise;
      if (!isCurrent()) return false;
      simulatedDrawerSetter();
      return true;
    });
    let currentOpen!: Promise<boolean>;
    act(() => { currentOpen = result.current.requestOpenResults(); });
    expect(simulatedDrawerSetter).not.toHaveBeenCalled();
    await act(async () => { currentConfirmation.resolve(); expect(await currentOpen).toBe(true); });
    expect(simulatedDrawerSetter).toHaveBeenCalledTimes(1);
    expect(result.current.captureSelection()).toEqual(replacement);
  } finally { oldConfirmation.resolve(); currentConfirmation.resolve(); unmount(); client.clear(); }
});

it("dismisses terminal marks locally without deleting history or ACK and fences late snapshots", async () => {
  const { client, Wrapper } = setup();
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    const submission = result.current.captureSubmissionOwner();
    if (!submission) throw new Error("Submission owner missing");
    act(() => { result.current.acceptAcknowledgement(ack("new-ack"), submission); });
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("new-ack"));
    const selected = result.current.captureSelection();
    if (!selected) throw new Error("Selection missing");
    act(() => { result.current.acceptSnapshot(selected, snapshot("new-ack")); });
    expect(result.current.snapshot?.jobId).toBe("new-ack");
    await act(async () => { expect(await result.current.requestClearMarks()).toBe(true); });
    expect(result.current.selected).toBeNull();
    expect(result.current.acknowledgedJobId).toBe("new-ack");
    expect(result.current.history.map((item) => item.id)).toEqual(["latest", "older"]);
    act(() => { result.current.acceptSnapshot(selected, snapshot("new-ack")); });
    await act(async () => { await client.invalidateQueries({ queryKey: ["jobs", "workflow-test-run-history"] }); });
    expect(result.current.selected).toBeNull();
    expect(result.current.snapshot).toBeNull();
    await act(async () => { expect(await result.current.requestSelectHistory("older")).toBe(true); });
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("older"));
  } finally { unmount(); client.clear(); }
});

it("retains same-scope dismissal across a revision change and revives for explicit new-latest selection or a fresh ACK", async () => {
  const { client, Wrapper } = setup();
  const { result, rerender, unmount } = renderHook(({ currentOwner }) => useSelectedWorkflowTestRun({ owner: currentOwner, resultsOwnsObservation: false,
    confirmReadOnlyNavigation: async () => true }), { initialProps: { currentOwner: owner }, wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    await act(async () => { expect(await result.current.requestClearMarks()).toBe(true); });
    rerender({ currentOwner: { ...owner, editorRevision: 5 } });
    await act(async () => { await client.invalidateQueries({ queryKey: ["jobs", "workflow-test-run-history"] }); });
    expect(result.current.selected).toBeNull();
    vi.mocked(fetchJobsList).mockResolvedValue({ groups: undefined, jobs: [row("new-latest"), row("latest"), row("older")] });
    await act(async () => { await client.invalidateQueries({ queryKey: ["jobs", "workflow-test-run-history"] }); });
    await selectHistoryForSetup(result, "new-latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("new-latest"));
    await act(async () => { expect(await result.current.requestClearMarks()).toBe(true); });
    const submission = result.current.captureSubmissionOwner();
    if (!submission) throw new Error("Submission owner missing");
    act(() => { expect(result.current.acceptAcknowledgement(ack("fresh-ack"), submission)).toBe(true); });
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("fresh-ack"));
  } finally { unmount(); client.clear(); }
});

it("refuses to clear an active or unverified selection even when its snapshot claims terminal", async () => {
  const { client, Wrapper } = setup();
  vi.mocked(fetchJobDetail).mockImplementation(async (id) => job(id, { status: "running", job: { id, status: "running", selected_workflow: "workflow", project_id: "alpha" } }));
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    const selected = result.current.captureSelection();
    if (!selected) throw new Error("Selection missing");
    act(() => { result.current.acceptSnapshot(selected, snapshot("latest")); });
    await act(async () => { expect(await result.current.requestClearMarks()).toBe(false); });
    expect(result.current.selected?.jobId).toBe("latest");
  } finally { unmount(); client.clear(); }
});

it("keeps terminal ACK dismissal when a held history refresh returns previously witnessed runs", async () => {
  const { client, Wrapper } = setup();
  const refresh = deferred<JobsListResult>();
  const { result, unmount } = renderHook(() => useSelectedWorkflowTestRun({ owner, resultsOwnsObservation: false,
    confirmReadOnlyNavigation: async () => true }), { wrapper: Wrapper });
  try {
    await selectHistoryForSetup(result, "latest");
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("latest"));
    expect(result.current.history.map((item) => item.id)).toEqual(["latest", "older"]);
    vi.mocked(fetchJobsList).mockImplementation(() => refresh.promise);
    const submission = result.current.captureSubmissionOwner();
    if (!submission) throw new Error("Submission owner missing");
    act(() => { expect(result.current.acceptAcknowledgement(ack("fast-terminal-ack"), submission)).toBe(true); });
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("fast-terminal-ack"));
    await waitFor(() => expect(result.current.historyPending).toBe(true));
    expect(result.current.history).toEqual([]);
    const proof = result.current.captureSelection();
    if (!proof) throw new Error("Selection missing");
    act(() => { result.current.acceptSnapshot(proof, snapshot("fast-terminal-ack")); });
    await act(async () => { expect(await result.current.requestClearMarks()).toBe(true); });
    expect(result.current.selected).toBeNull();
    await act(async () => { refresh.resolve({ groups: undefined, jobs: [row("latest"), row("older")] }); await refresh.promise; });
    await waitFor(() => expect(result.current.serverLatest?.id).toBe("latest"));
    expect(result.current.selected).toBeNull();
    expect(result.current.snapshot).toBeNull();
    expect(result.current.acknowledgedJobId).toBe("fast-terminal-ack");
    await act(async () => { expect(await result.current.requestSelectHistory("older")).toBe(true); });
    await waitFor(() => expect(result.current.verifiedDetail?.id).toBe("older"));
  } finally { refresh.resolve({ groups: undefined, jobs: [] }); unmount(); client.clear(); }
});


it.each(["project", "workflow"])("keeps history idle without a %s owner and recovers to a real empty history", async (missing) => {
  const { client, Wrapper } = setup();
  const history = deferred<JobsListResult>();
  vi.mocked(fetchJobsList).mockReturnValue(history.promise);
  const unreadyOwner = { ...owner, ...(missing === "project" ? { projectId: "" } : { workflowId: "" }) };
  const { result, rerender, unmount } = renderHook(({ currentOwner }) => useSelectedWorkflowTestRun({ owner: currentOwner,
    resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true }), { initialProps: { currentOwner: unreadyOwner }, wrapper: Wrapper });
  try {
    expect(result.current.historyPending).toBe(false);
    expect(result.current.historyError).toBeNull();
    expect(result.current.history).toEqual([]);
    expect(result.current.selected).toBeNull();
    expect(fetchJobsList).not.toHaveBeenCalled();
    expect(fetchJobDetail).not.toHaveBeenCalled();
    rerender({ currentOwner: owner });
    await waitFor(() => expect(fetchJobsList).toHaveBeenCalledExactlyOnceWith({ page: 1, perPage: 8, status: "", workflowId: "workflow", projectId: "alpha", taskSource: "workflow_test_run" }));
    expect(result.current.historyPending).toBe(true);
    await act(async () => { history.resolve({ groups: undefined, jobs: [] }); await history.promise; });
    await waitFor(() => expect(result.current.historyPending).toBe(false));
    expect(result.current.historyError).toBeNull();
    expect(result.current.history).toEqual([]);
    expect(result.current.selected).toBeNull();
    rerender({ currentOwner: unreadyOwner });
    expect(result.current.historyPending).toBe(false);
    expect(result.current.historyError).toBeNull();
  } finally { history.resolve({ groups: undefined, jobs: [] }); unmount(); client.clear(); }
});
