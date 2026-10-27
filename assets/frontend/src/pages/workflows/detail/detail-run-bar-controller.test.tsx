import type { PropsWithChildren } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cancelJob, fetchJobDetail, type JobDetail, type JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import { fetchJobsList } from "@/pages/jobs/list/api";
import { useJobExecutionSnapshot } from "@/pages/jobs/detail/job-execution-snapshot";
import { queryKeys } from "@/lib/query/keys";
import { useSelectedWorkflowTestRun, type TestRunOwner } from "./detail-selected-test-run";
import { useWorkflowRunBarController } from "./detail-run-bar-controller";

vi.mock("@/pages/jobs/detail/api", async (original) => ({ ...(await original<Record<string, unknown>>()), cancelJob: vi.fn(), fetchJobDetail: vi.fn() }));
vi.mock("@/pages/jobs/list/api", async (original) => ({ ...(await original<Record<string, unknown>>()), fetchJobsList: vi.fn() }));
vi.mock("@/pages/jobs/detail/job-execution-snapshot", () => ({ useJobExecutionSnapshot: vi.fn() }));
const owner: TestRunOwner = { workflowId: "workflow", projectId: "alpha", editorRevision: 4 };
const job = (id: string, status = "running", projectId = "alpha"): JobDetail => ({ id, status, selected_workflow: "workflow", project_id: projectId,
  job: { id, status, selected_workflow: "workflow", project_id: projectId }, current_step_id: "plan" });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function setup(confirmStop = vi.fn(async () => true), actions: Pick<Parameters<typeof useWorkflowRunBarController>[0], "requestReviewRequest" | "requestOpenFailedStep"> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: Infinity } } });
  const Wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const view = renderHook(({ currentOwner, roles }) => {
    const selected = useSelectedWorkflowTestRun({ owner: currentOwner, resultsOwnsObservation: false, confirmReadOnlyNavigation: async () => true });
    const bar = useWorkflowRunBarController({ selectedRun: selected, selectionKind: "empty", navigationBusy: false, historyDisabled: false,
      navigate: async (action) => action(), roles, timezone: "UTC", confirmStop, ...actions });
    return { selected, bar };
  }, { wrapper: Wrapper, initialProps: { currentOwner: owner, roles: ["operator"] as unknown[] } });
  return { ...view, client, confirmStop };
}
async function selectHistoryForBarSetup(result: ReturnType<typeof setup>["result"], id = "latest") {
  await waitFor(() => expect(result.current.selected.history.some((row) => row.id === id)).toBe(true));
  await act(async () => { await result.current.selected.requestSelectHistory(id); });
}
beforeEach(() => {
  vi.mocked(fetchJobsList).mockReset().mockImplementation(async (filters) => ({ groups: undefined, jobs: [filters.projectId === "beta" ? "beta-job" : "latest", "older"].map((id) => ({ id, selected_workflow: "workflow",
    project_id: filters.projectId ?? undefined, task_source: "workflow_test_run", status: "running", created_at: "2026-10-19T10:00:00Z", duration_ms: 0 })) }));
  vi.mocked(fetchJobDetail).mockReset().mockImplementation(async (id) => job(id, "running", id === "beta-job" ? "beta" : "alpha"));
  vi.mocked(cancelJob).mockReset().mockResolvedValue({ job_id: "latest", status: "cancelled" });
  vi.mocked(useJobExecutionSnapshot).mockReset().mockReturnValue({ snapshot: null, pending: false, error: null });
});

it("confirms Stop once and waits for authoritative readback rather than inventing a terminal status", async () => {
  const confirmation = deferred<boolean>();
  const pending = deferred<Record<string, unknown>>();
  const { result, client, unmount, confirmStop } = setup(vi.fn(() => confirmation.promise));
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.bar.stopAction).toBeDefined());
    vi.mocked(cancelJob).mockReturnValueOnce(pending.promise);
    let first!: Promise<boolean | void>;
    act(() => { first = Promise.resolve(result.current.bar.stopAction?.onInvoke()); });
    await waitFor(() => expect(result.current.bar.stopAction?.busy).toBe(true));
    await act(async () => { await result.current.bar.stopAction?.onInvoke(); });
    expect(confirmStop).toHaveBeenCalledOnce();
    expect(cancelJob).not.toHaveBeenCalled();
    await act(async () => { confirmation.resolve(true); await confirmation.promise; });
    await waitFor(() => expect(cancelJob).toHaveBeenCalledExactlyOnceWith("latest"));
    expect(result.current.bar.selection.kind === "verified" && result.current.bar.selection.run.title).not.toBe("Test Run stopped");
    await act(async () => { pending.resolve({ job_id: "latest", status: "cancelled" }); await first; });
    expect(result.current.selected.statusCode).toBe("running");
    expect(result.current.bar.clearMarksAction).toBeUndefined();
    expect(result.current.bar.actionNotice?.message).toContain("Refreshing job state");
  } finally { confirmation.resolve(false); pending.resolve({}); unmount(); client.clear(); }
});

it("does not send Stop when the confirmation is cancelled", async () => {
  const { result, client, unmount } = setup(vi.fn(async () => false));
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.bar.stopAction).toBeDefined());
    await act(async () => { await result.current.bar.stopAction?.onInvoke(); });
    expect(cancelJob).not.toHaveBeenCalled();
    expect(result.current.bar.stopAction?.busy).toBe(false);
  } finally { unmount(); client.clear(); }
});

it.each(["viewer", "completed", "unknown"])("does not authorize Stop for %s", async (refusal) => {
  const { result, rerender, client, unmount } = setup();
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail).toBeTruthy());
    if (refusal === "viewer") rerender({ currentOwner: owner, roles: ["viewer"] });
    else act(() => { client.setQueryData(queryKeys.jobDetail("latest"), job("latest", refusal)); });
    await waitFor(() => expect(result.current.bar.stopAction).toBeUndefined());
    expect(cancelJob).not.toHaveBeenCalled();
  } finally { unmount(); client.clear(); }
});

it.each(["selected job", "project ABA", "revision", "role", "terminal"])("rechecks %s after a held Stop confirmation", async (change) => {
  const confirmation = deferred<boolean>();
  const { result, rerender, client, unmount } = setup(vi.fn(() => confirmation.promise));
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.bar.stopAction).toBeDefined());
    let pending!: Promise<boolean | void>;
    act(() => { pending = Promise.resolve(result.current.bar.stopAction?.onInvoke()); });
    if (change === "project ABA") {
      rerender({ currentOwner: { ...owner, projectId: "beta" }, roles: ["operator"] });
      await selectHistoryForBarSetup(result, "beta-job");
      await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("beta-job"));
      rerender({ currentOwner: owner, roles: ["operator"] });
      await selectHistoryForBarSetup(result);
      await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("latest"));
    } else if (change === "selected job") {
      await act(async () => { await result.current.selected.requestSelectHistory("older"); });
      await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("older"));
    } else if (change === "revision") rerender({ currentOwner: { ...owner, editorRevision: 5 }, roles: ["operator"] });
    else if (change === "role") rerender({ currentOwner: owner, roles: ["viewer"] });
    else act(() => { client.setQueryData(queryKeys.jobDetail("latest"), job("latest", "completed")); });
    await act(async () => { confirmation.resolve(true); await pending; });
    expect(cancelJob).not.toHaveBeenCalled();
    expect(result.current.bar.actionNotice).toBeUndefined();
  } finally { confirmation.resolve(false); unmount(); client.clear(); }
});

it("scopes a late cancellation failure to its retired selection", async () => {
  const response = deferred<Record<string, unknown>>();
  const { result, rerender, client, unmount } = setup();
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.bar.stopAction).toBeDefined());
    vi.mocked(cancelJob).mockReturnValueOnce(response.promise);
    let pending!: Promise<boolean | void>;
    act(() => { pending = Promise.resolve(result.current.bar.stopAction?.onInvoke()); });
    await waitFor(() => expect(cancelJob).toHaveBeenCalledWith("latest"));
    rerender({ currentOwner: { ...owner, projectId: "beta" }, roles: ["operator"] });
    await selectHistoryForBarSetup(result, "beta-job");
    await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("beta-job"));
    await act(async () => { response.reject(new Error("Old cancellation failed")); await pending; });
    expect(result.current.bar.actionNotice).toBeUndefined();
    expect(result.current.selected.selected?.jobId).toBe("beta-job");
    expect(result.current.bar.stopAction?.busy).toBe(false);
  } finally { response.resolve({}); unmount(); client.clear(); }
});

it("projects archived active context and human history without totals or invented duration", async () => {
  const snapshot: JobExecutionSnapshot = { jobId: "latest", definitionWorkflowId: "workflow", definitionRevision: 4,
      definitionSteps: [{ stepId: "plan", label: "Plan work" }], steps: [{ stepId: "plan", stepExecutionId: "pass", status: "running", groupId: "orders", laneId: "west", iteration: 0, attempt: 2 }],
      terminal: false, complete: false, childInvocations: [] };
  vi.mocked(useJobExecutionSnapshot).mockReturnValue({ snapshot, pending: false, error: null });
  const { result, client, unmount } = setup();
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("latest"));

    await waitFor(() => expect(result.current.selected.snapshot).toBe(snapshot));
    const selection = result.current.bar.selection;
    expect(selection.kind).toBe("verified");
    if (selection.kind !== "verified") throw new Error("Verified selection missing");
    expect(selection.run.title).toBe("Test Run running");
    expect(selection.run.sub).toBe("Plan work (orders · lane west) · 0 steps done · < 0.1 s");
    expect(selection.run.sub).not.toContain("plan");
    const history = result.current.bar.history;
    if (history.kind !== "ready") throw new Error("History missing");
    expect(history.rows[0]).toEqual(expect.objectContaining({ jobId: "latest", number: 2, status: "Running", meta: "< 0.1 s · 10:00:00" }));
    expect(history.rows[0].label).toBe("Run #2 · Running · < 0.1 s · 10:00:00");
    expect(history.rows[1].number).toBe(1);
  } finally { unmount(); client.clear(); }
});

it("shows the recorded run failure without declaring a successful Stop fail operation failed", async () => {
  const snapshot: JobExecutionSnapshot = { jobId: "latest", definitionWorkflowId: "workflow", definitionRevision: 4,
      definitionSteps: [{ stepId: "stop", label: "Intentional Stop fail" }], steps: [{ stepId: "stop", stepExecutionId: "stop-pass", status: "completed", durationMs: 0 }],
      runError: { message: "Expected local audit failure", stepId: "stop" }, terminal: true, complete: true, childInvocations: [] };
  vi.mocked(useJobExecutionSnapshot).mockReturnValue({ snapshot, pending: false, error: null });
  const { result, client, unmount } = setup();
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("latest"));
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), job("latest", "failed")); });
    await waitFor(() => expect(result.current.selected.statusCode).toBe("failed"));
    await waitFor(() => expect(result.current.selected.snapshot).toBe(snapshot));
    const selection = result.current.bar.selection;
    if (selection.kind !== "verified") throw new Error("Selection missing");
    expect(selection.run.tone).toBe("danger");
    expect(selection.run.title).toBe("Failed at Intentional Stop fail");
    expect(selection.run.sub).toBe("Expected local audit failure");
    expect(result.current.selected.snapshot?.steps[0].status).toBe("completed");
  } finally { unmount(); client.clear(); }
});

it("keeps active context attached to its execution step when detail lags and the archive has no display label", async () => {
  const snapshot: JobExecutionSnapshot = { jobId: "latest", definitionWorkflowId: "workflow", definitionRevision: 4,
      definitionSteps: [{ stepId: "plan" }, { stepId: "ship_order" }],
      steps: [{ stepId: "ship_order", stepExecutionId: "ship-pass", status: "running", groupId: "dispatch", laneId: "west", iteration: 0, attempt: 2 }],
      terminal: false, complete: true, childInvocations: [] };
  vi.mocked(useJobExecutionSnapshot).mockReturnValue({ snapshot, pending: false, error: null });
  const { result, client, unmount } = setup();
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("latest"));
    expect(result.current.selected.verifiedDetail?.current_step_id).toBe("plan");
    await waitFor(() => expect(result.current.selected.snapshot).toBe(snapshot));
    const selection = result.current.bar.selection;
    if (selection.kind !== "verified") throw new Error("Selection missing");
    expect(selection.run.sub).toBe("Ship Order (dispatch · lane west) · 0 steps done · < 0.1 s");
    expect(selection.run.sub).not.toContain("Plan");
  } finally { unmount(); client.clear(); }
});

it("keeps a code-only run failure diagnostic out of the readable primary reason", async () => {
  const snapshot: JobExecutionSnapshot = { jobId: "latest", definitionWorkflowId: "workflow", definitionRevision: 4,
      definitionSteps: [{ stepId: "stop" }], steps: [{ stepId: "stop", stepExecutionId: "stop-pass", status: "completed", durationMs: 0 }],
      runError: { code: "workflow.stopped" }, terminal: true, complete: true, childInvocations: [] };
  vi.mocked(useJobExecutionSnapshot).mockReturnValue({ snapshot, pending: false, error: null });
  const { result, client, unmount } = setup();
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("latest"));
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), job("latest", "failed")); });
    await waitFor(() => expect(result.current.selected.statusCode).toBe("failed"));
    await waitFor(() => expect(result.current.selected.snapshot).toBe(snapshot));
    const selection = result.current.bar.selection;
    if (selection.kind !== "verified") throw new Error("Selection missing");
    expect(selection.run.title).toBe("Test Run failed");
    expect(selection.run.sub).toBe("");
    expect(result.current.selected.snapshot?.runError?.code).toBe("workflow.stopped");
  } finally { unmount(); client.clear(); }
});


it.each(["missing", "foreign project", "foreign workflow", "conflicting job alias"])("does not admit a held Stop from %s cache data", async (change) => {
  const confirmation = deferred<boolean>();
  const { result, client, unmount } = setup(vi.fn(() => confirmation.promise));
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.bar.stopAction).toBeDefined());
    let pending!: Promise<boolean | void>;
    act(() => { pending = Promise.resolve(result.current.bar.stopAction?.onInvoke()); });
    if (change === "missing") client.removeQueries({ queryKey: queryKeys.jobDetail("latest"), exact: true });
    else {
      const detail = change === "foreign project" ? job("latest", "running", "beta") : job("latest");
      if (change === "foreign workflow") detail.selected_workflow = "other-workflow";
      if (change === "conflicting job alias") detail.job = { ...detail.job, id: "other-job" };
      client.setQueryData(queryKeys.jobDetail("latest"), detail);
    }
    // Deliberately release before observer delivery: cache state is already known.
    await act(async () => { confirmation.resolve(true); await pending; });
    expect(cancelJob).not.toHaveBeenCalled();
    expect(result.current.bar.actionNotice).toBeUndefined();
  } finally { confirmation.resolve(false); unmount(); client.clear(); }
});

it("captures the current approval identity and retires a held contextual request when its gate changes", async () => {
  const requestReviewRequest = vi.fn<NonNullable<Parameters<typeof useWorkflowRunBarController>[0]["requestReviewRequest"]>>(async () => true);
  const { result, client, unmount } = setup(undefined, { requestReviewRequest });
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail).toBeTruthy());
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), { ...job("latest", "awaiting_approval"),
      lifecycle: { status: { code: "awaiting_approval", terminal: false }, approval: { state: "awaiting_approval", gate: { approval_id: "gate-a" } } } }); });
    await waitFor(() => expect(result.current.bar.reviewRequestAction).toBeDefined());
    await act(async () => { await result.current.bar.reviewRequestAction?.onInvoke(); });
    expect(requestReviewRequest).toHaveBeenCalledOnce();
    const [proof, approvalId, isCurrent] = requestReviewRequest.mock.calls[0] as unknown as [unknown, string, () => boolean];
    expect(proof).toEqual(expect.objectContaining({ jobId: "latest", projectId: "alpha" }));
    expect(approvalId).toBe("gate-a");
    expect(isCurrent()).toBe(true);
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), { ...job("latest", "awaiting_approval"),
      lifecycle: { status: { code: "awaiting_approval", terminal: false }, approval: { state: "awaiting_approval", gate: { approval_id: "gate-b" } } } }); });
    await waitFor(() => expect(isCurrent()).toBe(false));
    expect(cancelJob).not.toHaveBeenCalled();
  } finally { unmount(); client.clear(); }
});

it("does not expose Review request for missing or conflicting published approval identities", async () => {
  const requestReviewRequest = vi.fn<NonNullable<Parameters<typeof useWorkflowRunBarController>[0]["requestReviewRequest"]>>(async () => true);
  const { result, client, unmount } = setup(undefined, { requestReviewRequest });
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail).toBeTruthy());
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), job("latest", "awaiting_approval")); });
    await waitFor(() => expect(result.current.selected.statusCode).toBe("awaiting_approval"));
    expect(result.current.bar.reviewRequestAction).toBeUndefined();
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), { ...job("latest", "awaiting_approval"), approval_gate: { approval_id: "raw-b" },
      lifecycle: { status: { code: "awaiting_approval", terminal: false }, approval: { gate: { approval_id: "canonical-a" } } } }); });
    await waitFor(() => expect(result.current.selected.verifiedDetail?.approval_gate?.approval_id).toBe("raw-b"));
    expect(result.current.bar.reviewRequestAction).toBeUndefined();
    expect(requestReviewRequest).not.toHaveBeenCalled();
  } finally { unmount(); client.clear(); }
});

it("opens the exact failed occurrence, refuses ambiguous failures and keeps Stop fail statuses truthful", async () => {
  const requestOpenFailedStep = vi.fn<NonNullable<Parameters<typeof useWorkflowRunBarController>[0]["requestOpenFailedStep"]>>(async () => true);
  let snapshot: JobExecutionSnapshot = { jobId: "latest", definitionWorkflowId: "workflow", definitionRevision: 4,
    definitionSteps: [{ stepId: "plan" }], runError: { stepId: "plan", stepExecutionId: "pass-2", message: "Recorded failure" },
    steps: [{ stepId: "plan", stepExecutionId: "pass-1", status: "completed" }, { stepId: "plan", stepExecutionId: "pass-2", status: "failed" }],
    childInvocations: [], terminal: true, complete: true };
  vi.mocked(useJobExecutionSnapshot).mockImplementation(() => ({ snapshot, pending: false, error: null }));
  const { result, client, rerender, unmount } = setup(undefined, { requestOpenFailedStep });
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail).toBeTruthy());
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), job("latest", "failed")); });
    await waitFor(() => expect(result.current.bar.openFailedStepAction).toBeDefined());
    await act(async () => { await result.current.bar.openFailedStepAction?.onInvoke(); });
    const [, target, isCurrent] = requestOpenFailedStep.mock.calls[0] as unknown as [unknown, unknown, () => boolean];
    expect(target).toEqual(expect.objectContaining({ stepId: "plan", stepExecutionId: "pass-2", definitionWorkflowId: "workflow", definitionRevision: 4 }));
    expect(isCurrent()).toBe(true);
    snapshot = { ...snapshot, runError: { stepId: "plan", message: "Recorded failure" }, steps: snapshot.steps.map((step) => ({ ...step, status: "failed" })) };
    rerender({ currentOwner: owner, roles: ["operator"] });
    await waitFor(() => expect(result.current.bar.openFailedStepAction).toBeUndefined());
    expect(isCurrent()).toBe(false);
    snapshot = { ...snapshot, steps: [{ stepId: "plan", stepExecutionId: "stop-successful", status: "completed", durationMs: 0 }] };
    rerender({ currentOwner: owner, roles: ["operator"] });
    await waitFor(() => expect(result.current.bar.openFailedStepAction).toBeDefined());
    await act(async () => { await result.current.bar.openFailedStepAction?.onInvoke(); });
    expect(requestOpenFailedStep.mock.calls.at(-1)?.[1]).toEqual(expect.objectContaining({ stepExecutionId: "stop-successful" }));
    expect(result.current.selected.snapshot?.steps[0].status).toBe("completed");
    snapshot = { ...snapshot, complete: false };
    rerender({ currentOwner: owner, roles: ["operator"] });
    await waitFor(() => expect(result.current.bar.openFailedStepAction).toBeUndefined());
  } finally { unmount(); client.clear(); }
});

it.each(["current execution", "missing execution", "different execution", "different step"])("uses the authoritative error execution identity for %s without choosing a prior failed attempt", async (identity) => {
  const requestOpenFailedStep = vi.fn<NonNullable<Parameters<typeof useWorkflowRunBarController>[0]["requestOpenFailedStep"]>>(async () => true);
  const snapshot: JobExecutionSnapshot = { jobId: "latest", definitionWorkflowId: "workflow", definitionRevision: 4,
    definitionSteps: [{ stepId: "plan" }, { stepId: "other" }],
    runError: { stepId: identity === "different step" ? "other" : "plan", message: "Intentional Stop fail",
      ...(identity !== "missing execution" ? { stepExecutionId: identity === "different execution" ? "absent" : "current-pass" } : {}) },
    steps: [{ stepId: "plan", stepExecutionId: "prior-failed", status: "failed" },
      { stepId: "plan", stepExecutionId: "current-pass", status: "completed", durationMs: 0 }],
    childInvocations: [], terminal: true, complete: true };
  vi.mocked(useJobExecutionSnapshot).mockReturnValue({ snapshot, pending: false, error: null });
  const { result, client, unmount } = setup(undefined, { requestOpenFailedStep });
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail).toBeTruthy());
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), job("latest", "failed")); });
    await waitFor(() => expect(result.current.selected.statusCode).toBe("failed"));
    if (identity === "current execution") {
      await waitFor(() => expect(result.current.bar.openFailedStepAction).toBeDefined());
      await act(async () => { await result.current.bar.openFailedStepAction?.onInvoke(); });
      expect(requestOpenFailedStep.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ stepId: "plan", stepExecutionId: "current-pass" }));
      expect(result.current.selected.snapshot?.steps[1]).toEqual(expect.objectContaining({ status: "completed", durationMs: 0 }));
    } else {
      expect(result.current.bar.openFailedStepAction).toBeUndefined();
      expect(requestOpenFailedStep).not.toHaveBeenCalled();
    }
  } finally { unmount(); client.clear(); }
});


it.each([
  { status: "completed", durationMs: 1000, listDurationMs: 0, label: "Finished", title: "Test Run finished in 1.0 s", elapsed: "1.0 s" },
  { status: "failed", durationMs: 0, listDurationMs: 9000, label: "Failed", title: "Test Run failed", elapsed: "< 0.1 s" },
  { status: "completed", durationMs: null, listDurationMs: null, label: "Finished", title: "Test Run finished", elapsed: null },
])("keeps selected history aligned with verified $status readback and duration=$durationMs while its list remains stale", async ({ status, durationMs, listDurationMs, label, title, elapsed }) => {
  vi.mocked(fetchJobsList).mockResolvedValue({ groups: undefined, jobs: ["latest", "older"].map((id) => ({
    id, selected_workflow: "workflow", project_id: "alpha", task_source: "workflow_test_run",
    status: "in_progress", created_at: "2026-10-19T10:00:00Z", duration_ms: id === "latest" ? listDurationMs : 0,
  })) });
  vi.mocked(fetchJobDetail).mockImplementation(async (id) => job(id, "in_progress"));
  const { result, client, unmount } = setup();
  try {
    await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("latest"));
    await waitFor(() => expect(result.current.bar.history.kind).toBe("ready"));
    const before = result.current.bar.history;
    if (before.kind !== "ready") throw new Error("History missing");
    const olderLabel = before.rows.find((row) => row.jobId === "older")?.label;
    const proof = result.current.selected.captureSelection();
    const completed = job("latest", status);
    completed.job = { ...completed.job, duration_ms: durationMs };
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), completed); });
    await waitFor(() => expect(result.current.selected.statusCode).toBe(status));
    expect(result.current.selected.selected?.proof).toEqual(proof);
    expect(result.current.selected.history[0].status).toBe("in_progress");
    const selection = result.current.bar.selection;
    if (selection.kind !== "verified") throw new Error("Verified selection missing");
    expect(selection.run.title).toBe(title);
    const history = result.current.bar.history;
    if (history.kind !== "ready") throw new Error("History missing");
    expect(history.rows.map((row) => row.jobId)).toEqual(["latest", "older"]);
    expect(history.latestJobId).toBe("latest");
    expect(history.rows[0].label).toMatch(new RegExp(`^Run #2 · ${label} ·`));
    if (elapsed === null) expect(history.rows[0].label.split(" · ")).toHaveLength(3);
    else expect(history.rows[0].label).toContain(` · ${elapsed} · 10:00:00`);
    expect(history.rows[1].label).toBe(olderLabel);
    await act(async () => { await result.current.selected.requestSelectHistory("older"); });
    await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("older"));
    const olderHistory = result.current.bar.history;
    if (olderHistory.kind !== "ready") throw new Error("History missing");
    expect(olderHistory.rows[1].label).toBe(olderLabel);
    expect(result.current.selected.selected?.jobId).toBe("older");
  } finally { unmount(); client.clear(); }
});

it("uses the verified selected status label fallback without projecting foreign job detail into history", async () => {
  const { result, client, unmount } = setup();
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("latest"));
    await waitFor(() => expect(result.current.bar.history.kind).toBe("ready"));
    const before = result.current.bar.history;
    if (before.kind !== "ready") throw new Error("History missing");
    const originalLabel = before.rows[0].label;
    const unknown = { ...job("latest", "custom_pause"), status_label: "Waiting for local review" };
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), unknown); });
    await waitFor(() => expect(result.current.selected.statusCode).toBe("custom_pause"));
    const history = result.current.bar.history;
    if (history.kind !== "ready") throw new Error("History missing");
    expect(history.rows[0].label).toMatch(/^Run #2 · Running ·/);
    const selection = result.current.bar.selection;
    if (selection.kind !== "verified") throw new Error("Verified selection missing");
    expect(selection.run.title).toBe("Test Run running");
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), job("latest", "completed", "beta")); });
    await waitFor(() => expect(result.current.selected.verifiedDetail).toBeNull());
    const unverified = result.current.bar.history;
    if (unverified.kind !== "ready") throw new Error("History missing");
    expect(unverified.rows[0].label).toBe(originalLabel);
    expect(result.current.bar.selection.kind).toBe("empty");
  } finally { unmount(); client.clear(); }
});

it("overlays only the explicit verified history selection and never follows an unselected detail cache update", async () => {
  const { result, client, unmount } = setup();
  try {
    await selectHistoryForBarSetup(result);
    await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("latest"));
    await waitFor(() => expect(result.current.bar.history.kind).toBe("ready"));
    const before = result.current.bar.history;
    if (before.kind !== "ready") throw new Error("History missing");
    const latestLabel = before.rows[0].label;
    const olderLabel = before.rows[1].label;
    const originalProof = result.current.selected.captureSelection();
    const older = job("older", "completed");
    older.job = { ...older.job, duration_ms: 0 };
    act(() => { client.setQueryData(queryKeys.jobDetail("older"), older); });
    expect(result.current.selected.selected?.jobId).toBe("latest");
    expect(result.current.selected.captureSelection()).toEqual(originalProof);
    const unselected = result.current.bar.history;
    if (unselected.kind !== "ready") throw new Error("History missing");
    expect(unselected.rows.map((row) => row.label)).toEqual([latestLabel, olderLabel]);

    await act(async () => { await result.current.selected.requestSelectHistory("older"); });
    await waitFor(() => expect(result.current.selected.verifiedDetail?.id).toBe("older"));
    const historical = result.current.bar.history;
    if (historical.kind !== "ready") throw new Error("History missing");
    // The verified selected row overlays its readback: Finished, its own zero duration.
    expect(historical.rows[1].label).toBe("Run #1 · Finished · < 0.1 s · 10:00:00");
    expect(historical.rows[1]).toEqual(expect.objectContaining({ jobId: "older", status: "Finished", tone: "success" }));
    expect(historical.rows[0].label).toBe(latestLabel);
    expect(historical.latestJobId).toBe("latest");
    const historicalProof = result.current.selected.captureSelection();
    const newerLatest = job("latest", "failed");
    newerLatest.job = { ...newerLatest.job, duration_ms: 5000 };
    act(() => { client.setQueryData(queryKeys.jobDetail("latest"), newerLatest); });
    expect(result.current.selected.selected?.jobId).toBe("older");
    expect(result.current.selected.captureSelection()).toEqual(historicalProof);
    const after = result.current.bar.history;
    if (after.kind !== "ready") throw new Error("History missing");
    expect(after.rows).toEqual(historical.rows);
    expect(result.current.selected.history.map((row) => row.status)).toEqual(["running", "running"]);
  } finally { unmount(); client.clear(); }
});
