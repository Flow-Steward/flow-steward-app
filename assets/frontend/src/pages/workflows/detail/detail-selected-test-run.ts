import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/query/keys";
import { jobStatusGroup } from "@/pages/jobs/status-groups";
import { fetchJobsList, type JobListItem } from "@/pages/jobs/list/api";
import type { JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import { verifiesJobDetailOwner as verifiesDetail } from "@/pages/jobs/detail/job-detail-owner";
import { useJobDetailQuery } from "@/pages/jobs/detail/use-job-detail-query";
import { useJobExecutionSnapshot } from "@/pages/jobs/detail/job-execution-snapshot";
import { buildCanonicalJobDetailView, detailIsTerminal } from "@/pages/jobs/detail/job-detail-view-model";
import type { WorkflowTestRunResult } from "@/pages/workflows/shared/simulate-api";

export type TestRunOwner = { workflowId: string; projectId: string; editorRevision: number | null };
export type SelectedTestRunProof = TestRunOwner & { jobId: string; generation: number };
export type TestRunSubmissionProof = TestRunOwner & { ownerEpoch: number };
type Selection = { jobId: string; origin: "ack" | "history" | "restored" | "initial" };
type State = { owner: TestRunOwner; scopeEpoch: number; ownerEpoch: number; generation: number; ackEpoch: number; acknowledgedJobId: string; selection: Selection | null;
  /** Runs not to select by themselves again; `cleared` after Clear marks keeps the canvas clear. */
  dismissal: { knownJobIds: readonly string[]; cleared?: boolean } | null };
type Options = {
  owner: TestRunOwner;
  /** Existing workflow-only storage pointer: a candidate, not verified owner authority. */
  restoredJobId?: string;
  resultsOwnsObservation: boolean;
  confirmReadOnlyNavigation: (isCurrent: () => boolean) => Promise<boolean>;
  /** Existing drawer guard must check this predicate after confirmation, before setters. */
  openResults?: (isCurrent: () => boolean) => Promise<boolean>;
};
function text(value: unknown): string { return typeof value === "string" || typeof value === "number" ? String(value).trim() : ""; }
function record(value: unknown): Record<string, unknown> { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function sameScope(a: TestRunOwner, b: TestRunOwner) { return a.workflowId === b.workflowId && a.projectId === b.projectId; }
function sameOwner(a: TestRunOwner, b: TestRunOwner) { return sameScope(a, b) && a.editorRevision === b.editorRevision; }
function duration(value: unknown): number | null { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null; }
function matchesProof(proof: SelectedTestRunProof, state: State, owner: TestRunOwner): boolean {
  return sameOwner(state.owner, owner) && sameOwner(proof, owner) && proof.generation === state.generation && proof.jobId === state.selection?.jobId;
}
function isActiveInitialStatus(status: unknown): boolean {
  const group = jobStatusGroup(status);
  return group === "in_progress" || group === "waiting" || group === "awaiting_approval";
}
function validHistoryRow(row: JobListItem, owner: TestRunOwner): boolean {
  return Boolean(text(row.id) && (!row.job_id || text(row.job_id) === text(row.id)) &&
    row.task_source === "workflow_test_run" && text(row.project_id) === owner.projectId && text(row.selected_workflow) === owner.workflowId);
}


/** One selected run, separate ACK/server-Latest pointers and existing query owners.
 * This hook never writes ACK/thread storage, submits a run, or follows history rows.
 */
export function useSelectedWorkflowTestRun(options: Options) {
  const { owner, restoredJobId = "", resultsOwnsObservation } = options;
  const { workflowId, projectId, editorRevision } = owner;
  const queryClient = useQueryClient();
  const [state, setState] = useState<State>(() => ({ owner: { ...owner }, scopeEpoch: 1, ownerEpoch: 1, generation: 1, ackEpoch: 0,
    acknowledgedJobId: "", selection: text(restoredJobId) ? { jobId: text(restoredJobId), origin: "restored" } : null, dismissal: null }));
  // Owner-derived state is reconciled by a guarded render retry. No ref or
  // external setter is mutated until the committed layout effect below.
  if (!sameOwner(state.owner, owner)) setState((current) => {
    if (sameOwner(current.owner, owner)) return current;
    if (sameScope(current.owner, owner)) return { ...current, owner: { ...owner }, ownerEpoch: current.ownerEpoch + 1, generation: current.generation + 1 };
    return { owner: { ...owner }, scopeEpoch: current.scopeEpoch + 1, ownerEpoch: current.ownerEpoch + 1, generation: current.generation + 1,
      ackEpoch: 0, acknowledgedJobId: "", selection: text(restoredJobId) ? { jobId: text(restoredJobId), origin: "restored" } : null, dismissal: null };
  });
  const ready = sameOwner(state.owner, owner) && Boolean(owner.workflowId && owner.projectId);
  const live = useRef({ state, owner, verifiedJobId: "", terminal: false, history: [] as JobListItem[], latestJobId: "", witnessedJobIds: [] as string[], options });
  const historyKey = useMemo(() => ["jobs", "workflow-test-run-history", owner.workflowId, owner.projectId,
    "workflow_test_run", 1, 8] as const, [owner.workflowId, owner.projectId]);
  const historyQuery = useQuery({
    queryKey: historyKey,
    enabled: ready,
    queryFn: async () => ({ owner: { ...owner }, scopeEpoch: state.scopeEpoch, ackEpoch: state.ackEpoch,
      response: await fetchJobsList({ page: 1, perPage: 8, status: "", workflowId: owner.workflowId,
        projectId: owner.projectId, taskSource: "workflow_test_run" }) }),
    // No additional history polling loop. A launch invalidates this bounded list.
  });
  const history = useMemo(() => {
    const data = historyQuery.data;
    if (!ready || !data || !sameScope(data.owner, owner) || data.scopeEpoch !== state.scopeEpoch || data.ackEpoch !== state.ackEpoch) return [];
    return (data.response.jobs || []).slice(0, 8).filter((row) => validHistoryRow(row, owner));
  }, [historyQuery.data, owner, ready, state.scopeEpoch, state.ackEpoch]);
  const serverLatest = history[0] ?? null;
  const candidate = ready ? state.selection : null;
  const detailQuery = useJobDetailQuery(candidate?.jobId || "", ready && !resultsOwnsObservation);
  const verifiedDetail = candidate && verifiesDetail(detailQuery.data, candidate.jobId, owner) ? detailQuery.data : null;
  const canonical = verifiedDetail ? buildCanonicalJobDetailView({ detail: verifiedDetail, detailEntity: record(verifiedDetail), runtimeEntity: record(verifiedDetail.runtime) }) : null;
  const initialCandidate = candidate?.origin === "restored" || candidate?.origin === "initial";
  // Entry hints become visible only after canonical active execution verification.
  // ACK and explicitly selected Results retain their original lifecycle behavior.
  const initialActive = Boolean(canonical && isActiveInitialStatus(canonical.statusCode));
  // After a reload with no active run, the server's latest finished run is still shown
  // (the run bar, then Results and its history), once its own detail has verified.
  const latestJobId = text(serverLatest?.id);
  const initialLatest = Boolean(canonical && candidate && latestJobId && candidate.jobId === latestJobId);
  const initialAdmitted = initialActive || initialLatest;
  const historySettled = historyQuery.isError || (historyQuery.isSuccess && !historyQuery.isFetching);
  const selected = candidate && (!initialCandidate || initialAdmitted) ? candidate : null;
  const proof = useMemo<SelectedTestRunProof | null>(() => selected && ready
    ? { workflowId, projectId, editorRevision, jobId: selected.jobId, generation: state.generation } : null,
  [workflowId, projectId, editorRevision, ready, selected, state.generation]);
  const [snapshotEntry, setSnapshotEntry] = useState<{ proof: SelectedTestRunProof; snapshot: JobExecutionSnapshot | null } | null>(null);
  const observation = useJobExecutionSnapshot(candidate?.jobId || "", Boolean(verifiedDetail) && !resultsOwnsObservation, verifiedDetail ? owner.projectId : undefined);
  useLayoutEffect(() => {
    // ACK refresh hides the old epoch's rows before its replacement arrives.
    // Retain witnessed identities only within this committed workflow/project scope.
    const previous = live.current;
    const witnessedJobIds = previous.state.scopeEpoch === state.scopeEpoch && sameScope(previous.owner, owner) ? previous.witnessedJobIds : [];
    live.current = { state, owner: { ...owner }, verifiedJobId: verifiedDetail ? candidate?.jobId || "" : "",
      terminal: Boolean(verifiedDetail && detailIsTerminal(record(verifiedDetail), verifiedDetail)), history, latestJobId: text(serverLatest?.id),
      witnessedJobIds: [...new Set([...witnessedJobIds, ...history.map((row) => text(row.id))])], options };
  }, [state, owner, verifiedDetail, candidate?.jobId, history, serverLatest?.id, options]);
  const isCurrent = useCallback((captured: SelectedTestRunProof) => matchesProof(captured, live.current.state, live.current.owner), []);
  const captureSelection = useCallback((): SelectedTestRunProof | null => {
    const current = live.current;
    return sameOwner(current.state.owner, current.owner) && current.state.selection
      ? { ...current.owner, jobId: current.state.selection.jobId, generation: current.state.generation } : null;
  }, []);
  const acceptSnapshot = useCallback((captured: SelectedTestRunProof, snapshot: JobExecutionSnapshot | null) => {
    const current = live.current;
    if (!matchesProof(captured, current.state, current.owner) || current.verifiedJobId !== captured.jobId ||
      snapshot && (snapshot.jobId !== captured.jobId || snapshot.definitionWorkflowId !== captured.workflowId)) return;
    setSnapshotEntry((previous) => {
      // A queued update can run after a newer commit; reading the live proof is
      // deliberate. No ref mutation or other setter occurs in this updater.
      if (!matchesProof(captured, live.current.state, live.current.owner) || live.current.verifiedJobId !== captured.jobId ||
        previous && previous.proof.generation > captured.generation) return previous;
      return previous?.proof.generation === captured.generation && previous.snapshot === snapshot ? previous : { proof: captured, snapshot };
    });
  }, []);
  useEffect(() => {
    if (!resultsOwnsObservation && verifiedDetail && proof) acceptSnapshot(proof, observation.snapshot);
  }, [acceptSnapshot, observation.snapshot, proof, resultsOwnsObservation, verifiedDetail]);
  // The execution history polls faster than the compact detail: when it reports the run
  // finished, read the detail again instead of showing a stale running status.
  const executionTerminal = Boolean(observation.snapshot?.terminal);
  const detailTerminal = Boolean(verifiedDetail && detailIsTerminal(record(verifiedDetail), verifiedDetail));
  const observedJobId = verifiedDetail ? candidate?.jobId || "" : "";
  useEffect(() => {
    if (observedJobId && executionTerminal && !detailTerminal) void queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(observedJobId) });
  }, [detailTerminal, executionTerminal, observedJobId, queryClient]);
  useEffect(() => {
    if (!ready || state.ackEpoch === 0) return;
    let current = true;
    void (async () => {
      // Explicit cancellation also retires a first, still-data-less request;
      // invalidate alone may reuse that old promise. Epoch checks remain the fence.
      await queryClient.cancelQueries({ queryKey: historyKey, exact: true });
      if (current) await queryClient.invalidateQueries({ queryKey: historyKey, exact: true });
    })();
    return () => { current = false; };
  }, [historyKey, queryClient, ready, state.ackEpoch, state.scopeEpoch]);
  useEffect(() => {
    // A cached A result is not current after A→B→A even with a long cache lifetime.
    const data = historyQuery.data;
    if (ready && data && data.scopeEpoch !== state.scopeEpoch && !historyQuery.isFetching)
      void queryClient.invalidateQueries({ queryKey: historyKey, exact: true });
  }, [historyKey, historyQuery.data, historyQuery.isFetching, queryClient, ready, state.scopeEpoch]);
  // A finished entry hint waits for the bounded history to say whether it is the latest.
  if (ready && candidate && initialCandidate && (detailQuery.isError || detailQuery.data) && (initialAdmitted || historySettled)) setState((current) => {
    if (current.generation !== state.generation || !sameOwner(current.owner, owner)) return current;
    const acknowledgedJobId = candidate.origin === "restored" && verifiedDetail ? candidate.jobId : current.acknowledgedJobId;
    if (initialAdmitted) return { ...current, acknowledgedJobId, selection: { jobId: candidate.jobId, origin: "history" } };
    const knownJobIds = [...new Set([...(current.dismissal?.knownJobIds ?? []), candidate.jobId])];
    return { ...current, acknowledgedJobId, generation: current.generation + 1, selection: null, dismissal: { knownJobIds, cleared: current.dismissal?.cleared } };
  });
  const unknownRow = (row: JobListItem) => !state.dismissal?.knownJobIds.includes(text(row.id));
  const initialHistory = history.find((row) => isActiveInitialStatus(row.status) && unknownRow(row))
    || (serverLatest && !state.dismissal?.cleared && unknownRow(serverLatest) ? serverLatest : undefined);
  if (ready && initialHistory && !candidate) setState((current) => current.generation !== state.generation || !sameOwner(current.owner, owner)
    ? current : { ...current, generation: current.generation + 1, selection: { jobId: text(initialHistory.id), origin: "initial" }, dismissal: current.dismissal });

  const captureSubmissionOwner = useCallback((): TestRunSubmissionProof | null => {
    const current = live.current;
    return sameOwner(current.state.owner, current.owner) && current.owner.workflowId && current.owner.projectId
      ? { ...current.owner, ownerEpoch: current.state.ownerEpoch } : null;
  }, []);
  const acceptAcknowledgement = useCallback((payload: WorkflowTestRunResult, capturedOwner: TestRunSubmissionProof) => {
    const current = live.current;
    const jobId = text(payload.job_id);
    if (!jobId || capturedOwner.ownerEpoch !== current.state.ownerEpoch || !sameOwner(capturedOwner, current.owner) || !sameOwner(current.state.owner, current.owner) ||
      text(payload.workflow_id) !== current.owner.workflowId || text(payload.project_id) !== current.owner.projectId) return false;
    const generation = current.state.generation;
    setState((previous) => previous.generation !== generation || previous.ownerEpoch !== capturedOwner.ownerEpoch || !sameOwner(previous.owner, capturedOwner) ? previous
      : { ...previous, generation: previous.generation + 1, ackEpoch: previous.ackEpoch + 1,
        acknowledgedJobId: jobId, selection: { jobId, origin: "ack" }, dismissal: null });
    return true;
  }, []);
  const selectHistory = useCallback(async (jobId: string, latestOnly = false) => {
    const current = live.current;
    if (!sameOwner(current.state.owner, current.owner) || !current.history.some((row) => text(row.id) === jobId)) return false;
    const capturedOwner = { ...current.owner };
    const generation = current.state.generation;
    const stillCurrent = () => {
      const next = live.current;
      return sameOwner(next.owner, capturedOwner) && sameOwner(next.state.owner, capturedOwner) && next.state.generation === generation &&
        next.history.some((row) => text(row.id) === jobId) && (!latestOnly || next.latestJobId === jobId);
    };
    if (!await current.options.confirmReadOnlyNavigation(stillCurrent) || !stillCurrent()) return false;
    setState((previous) => previous.generation !== generation || !sameOwner(previous.owner, capturedOwner) ? previous
      : { ...previous, generation: previous.generation + 1, selection: { jobId, origin: "history" }, dismissal: null });
    return true;
  }, []);
  const requestSelectHistory = useCallback((jobId: string) => selectHistory(jobId), [selectHistory]);
  const requestBackToLatest = useCallback(() => live.current.latestJobId ? selectHistory(live.current.latestJobId, true) : Promise.resolve(false), [selectHistory]);
  const requestOpenResults = useCallback(async () => {
    const captured = captureSelection();
    const current = live.current;
    if (captured?.jobId !== current.verifiedJobId || !current.options.openResults) return false;
    const stillCurrent = () => isCurrent(captured) && live.current.verifiedJobId === captured.jobId;
    return await current.options.openResults(stillCurrent) && stillCurrent();
  }, [captureSelection, isCurrent]);
  const requestClearMarks = useCallback(async () => {
    const captured = captureSelection();
    const current = live.current;
    if (!captured || !matchesProof(captured, current.state, current.owner) ||
      current.verifiedJobId !== captured.jobId || !current.terminal) return false;
    // Local dismissal is not navigation or persistence. Retire the proof and
    // suppress known latest rows without changing ACK/history/storage pointers.
    const knownJobIds = [...new Set([captured.jobId, current.latestJobId, ...current.witnessedJobIds, ...current.history.map((row) => text(row.id))].filter(Boolean))];
    setState((previous) => !matchesProof(captured, previous, live.current.owner) ? previous
      : { ...previous, generation: previous.generation + 1, selection: null, dismissal: { knownJobIds, cleared: true } });
    return true;
  }, [captureSelection]);
  const snapshot = proof && verifiedDetail && snapshotEntry && matchesProof(snapshotEntry.proof, state, owner) ? snapshotEntry.snapshot : null;
  const markEligible = Boolean(snapshot && owner.editorRevision && snapshot.definitionRevision === owner.editorRevision && snapshot.definitionWorkflowId === owner.workflowId);
  const selectedHistoryRow = history.find((row) => text(row.id) === selected?.jobId);
  const durationMs = verifiedDetail ? duration(record(verifiedDetail.job).duration_ms) ?? duration(verifiedDetail.duration_ms) ?? duration(selectedHistoryRow?.duration_ms) : null;
  return {
    selected: selected && proof ? { ...selected, proof } : null,
    acknowledgedJobId: ready ? state.acknowledgedJobId : "", serverLatest, history,
    verifiedDetail, detailQuery, historyPending: ready && (historyQuery.isPending || historyQuery.isFetching),
    historyError: ready ? historyQuery.error : null, snapshot, markEligible,
    statusCode: canonical?.statusCode ?? null, durationMs,
    captureSubmissionOwner, acceptAcknowledgement, requestSelectHistory, requestBackToLatest, requestOpenResults,
    captureSelection, isCurrent, acceptSnapshot, requestClearMarks,
  };
}
