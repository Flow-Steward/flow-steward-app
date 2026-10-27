import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { cancelJob, type JobDefinitionPayload, type JobDetail } from "@/pages/jobs/detail/api";
import { dispatchApprovalsRefresh } from "@/pages/jobs/detail/job-detail-core";
import { buildCanonicalJobDetailView } from "@/pages/jobs/detail/job-detail-view-model";
import { verifiesJobDetailOwner } from "@/pages/jobs/detail/job-detail-owner";
import { canMutateJob, isCancellableJobStatus } from "@/pages/jobs/detail/job-action-policy";
import { jobStatusGroup } from "@/pages/jobs/status-groups";
import type { CanvasRunBarAction, CanvasRunBarProps } from "@/pages/workflows/builder/canvas-run-bar";
import { formatRunDuration } from "@/pages/workflows/builder/run-duration";
import { runBarText, type RunBarTone } from "./detail-run-bar-text";
import { runClockTime, runStatusWord } from "./detail-test-run-results-model";
import type { SelectedTestRunProof, useSelectedWorkflowTestRun } from "./detail-selected-test-run";

type SelectedRun = ReturnType<typeof useSelectedWorkflowTestRun>;
type Options = Readonly<{
  selectedRun: SelectedRun;
  selectionKind: "empty" | "loading" | "error";
  navigationBusy: boolean;
  historyDisabled: boolean;
  navigate: (action: () => Promise<boolean>) => Promise<boolean>;
  roles: readonly unknown[];
  timezone: string;
  confirmStop: () => Promise<boolean>;
  requestRunAgain?: (proof: SelectedTestRunProof) => Promise<boolean>;
  runAgainBusy?: boolean;
  runAgainBlockedReason?: string;
  requestReviewRequest?: (proof: SelectedTestRunProof, approvalId: string, isCurrent: () => boolean) => Promise<boolean>;
  requestOpenFailedStep?: (proof: SelectedTestRunProof, target: RunBarFailedStep, isCurrent: () => boolean) => Promise<boolean>;
  /** The waiting Approval step's Test Run tab is open: the bar says "Decide in the panel". */
  decisionPanelOpen?: boolean;
}>;
export type RunBarFailedStep = Readonly<{
  stepId: string;
  stepExecutionId: string;
  definitionWorkflowId: string;
  definitionRevision: number;
  hookId?: string;
}>;
type StopFlight = { proof: SelectedTestRunProof };
type Notice = { proof: SelectedTestRunProof; kind: "error" | "status"; message: string };
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function text_(value: unknown): string { return typeof value === "string" ? value.trim() : ""; }

/** The approval the selected run waits on, when every source names the same one. */
export function selectedRunApprovalId(selectedRun: Pick<SelectedRun, "verifiedDetail">): string {
  const detail = selectedRun.verifiedDetail;
  if (!detail) return "";
  const view = buildCanonicalJobDetailView({ detail, detailEntity: record(detail) });
  if (view.statusCode !== "awaiting_approval" || view.isTerminal) return "";
  const gate = record(record(view.lifecycle.approval).gate);
  const identities = [text_(gate.approval_id), text_(record(detail.approval_gate).approval_id), text_(view.sections.reviewWorkspace.approval_id),
    text_(record(record(detail.workflow_run).approval).approval_id)].filter(Boolean);
  return new Set(identities).size === 1 ? identities[0] : "";
}

/** A run error can point to a successful Stop fail operation; never rewrite its status. */
function failedStepTarget(selectedRun: SelectedRun): RunBarFailedStep | null {
  const snapshot = selectedRun.snapshot;
  if (!snapshot?.complete || snapshot.jobId !== selectedRun.selected?.jobId || !selectedRun.verifiedDetail ||
    !snapshot.definitionWorkflowId || !Number.isSafeInteger(snapshot.definitionRevision) || snapshot.definitionRevision <= 0) return null;
  const detail = selectedRun.verifiedDetail;
  const view = buildCanonicalJobDetailView({ detail, detailEntity: record(detail) });
  if (jobStatusGroup(view.statusCode) !== "failed") return null;
  const errorStepId = text_(snapshot.runError?.stepId);
  const errorExecutionId = text_(snapshot.runError?.stepExecutionId);
  // A repeated logical step is not enough to identify the causal occurrence.
  // In particular, a prior failed pass must not replace a successful Stop fail.
  const candidates = snapshot.steps.filter((step) => errorExecutionId
    ? step.stepExecutionId === errorExecutionId && (!errorStepId || step.stepId === errorStepId)
    : errorStepId ? step.stepId === errorStepId : ["failed", "error"].includes(step.status.toLowerCase()));
  if (candidates.length !== 1 || !candidates[0].stepExecutionId) return null;
  const step = candidates[0];
  const definitions = snapshot.definitionSteps.filter((row) => row.stepId === step.stepId);
  if (definitions.length !== 1 || snapshot.steps.filter((row) => row.stepExecutionId === step.stepExecutionId).length !== 1) return null;
  return { stepId: step.stepId, stepExecutionId: step.stepExecutionId!, definitionWorkflowId: snapshot.definitionWorkflowId,
    definitionRevision: snapshot.definitionRevision, hookId: definitions[0].hookId };
}

/** The existing selected-run owner supplies all observation; this owner only composes actions. */
export function useWorkflowRunBarController(options: Options): CanvasRunBarProps & { toolbarAction?: CanvasRunBarAction } {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const committed = useRef<Options | null>(null);
  useLayoutEffect(() => {
    committed.current = options;
    return () => { if (committed.current === options) committed.current = null; };
  }, [options]);
  const stopFlight = useRef<StopFlight | null>(null);
  const [pendingStop, setPendingStop] = useState<StopFlight | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const { selectedRun, navigationBusy, navigate } = options;
  const canonical = selectedRun.verifiedDetail ? buildCanonicalJobDetailView({ detail: selectedRun.verifiedDetail,
    detailEntity: record(selectedRun.verifiedDetail), runtimeEntity: record(selectedRun.verifiedDetail.runtime) }, t) : null;
  const verified = Boolean(canonical && canonical.jobId === selectedRun.selected?.jobId);
  const canStop = verified && canMutateJob(options.roles) && !canonical?.isTerminal && isCancellableJobStatus(canonical?.statusCode);
  const stopBusy = Boolean(pendingStop && selectedRun.isCurrent(pendingStop.proof));

  function currentStopOwner(proof: SelectedTestRunProof): Options | null {
    const current = committed.current;
    if (!current?.selectedRun.isCurrent(proof) || current.selectedRun.selected?.jobId !== proof.jobId || !current.selectedRun.verifiedDetail) return null;
    const detail = current.selectedRun.verifiedDetail;
    const view = buildCanonicalJobDetailView({ detail, detailEntity: record(detail), runtimeEntity: record(detail.runtime) });
    if (view.jobId !== proof.jobId || view.isTerminal || !canMutateJob(current.roles) || !isCancellableJobStatus(view.statusCode)) return null;
    // Cache writes precede observer commits. Only restrict the already verified
    // current owner here; a cached row must never grant Stop authority.
    const cached = queryClient.getQueryData<JobDetail>(queryKeys.jobDetail(proof.jobId));
    if (!verifiesJobDetailOwner(cached, proof.jobId, proof)) return null;
    const cachedView = buildCanonicalJobDetailView({ detail: cached, detailEntity: record(cached), runtimeEntity: record(cached.runtime) });
    return !cachedView.isTerminal && isCancellableJobStatus(cachedView.statusCode) ? current : null;
  }
  async function requestStop(): Promise<boolean> {
    const current = committed.current;
    const proof = current?.selectedRun.captureSelection();
    if (!current || !proof || !currentStopOwner(proof) || current.navigationBusy ||
      stopFlight.current && current.selectedRun.isCurrent(stopFlight.current.proof)) return false;
    const flight = { proof };
    stopFlight.current = flight;
    setPendingStop(flight);
    setNotice(null);
    try {
      if (!await current.confirmStop() || !currentStopOwner(proof)) return false;
      await cancelJob(proof.jobId);
      // Invalidate the captured job even if navigation retired the visible owner.
      ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(proof.jobId) }));
      ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: queryKeys.jobExecutionSteps(proof.jobId, "") }));
      ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: ["jobs", "workflow-test-run-history", proof.workflowId, proof.projectId] }));
      dispatchApprovalsRefresh();
      if (!committed.current?.selectedRun.isCurrent(proof)) return false;
      setNotice({ proof, kind: "status", message: t("react.jobs.cancel_request_sent_refreshing_job_state", "Cancel request sent. Refreshing job state…") });
      return true;
    } catch (error) {
      if (committed.current?.selectedRun.isCurrent(proof)) setNotice({ proof, kind: "error", message: error instanceof Error ? error.message : t("react.jobs.cancel_request_failed", "Cancel request failed.") });
      return false;
    } finally {
      if (stopFlight.current === flight) {
        stopFlight.current = null;
        if (committed.current) setPendingStop((previous) => previous === flight ? null : previous);
      }
    }
  }
  async function requestRunAgain(): Promise<boolean> {
    const current = committed.current;
    const proof = current?.selectedRun.captureSelection();
    const detail = current?.selectedRun.verifiedDetail;
    const again = current?.requestRunAgain;
    if (!current || !again || !proof || !detail || !current.selectedRun.isCurrent(proof) || current.navigationBusy ||
      current.runAgainBusy || current.runAgainBlockedReason || stopFlight.current && current.selectedRun.isCurrent(stopFlight.current.proof) ||
      !buildCanonicalJobDetailView({ detail, detailEntity: record(detail) }).isTerminal) return false;
    return current.navigate(() => current.selectedRun.isCurrent(proof) ? again(proof) : Promise.resolve(false));
  }
  async function requestReviewRequest(): Promise<boolean> {
    const current = committed.current;
    const proof = current?.selectedRun.captureSelection();
    const approvalId = current && selectedRunApprovalId(current.selectedRun);
    if (!current || !proof || !approvalId || !current.requestReviewRequest || current.navigationBusy) return false;
    const isCurrent = () => Boolean(committed.current?.selectedRun.isCurrent(proof) && selectedRunApprovalId(committed.current.selectedRun) === approvalId);
    return current.navigate(() => isCurrent() ? current.requestReviewRequest!(proof, approvalId, isCurrent) : Promise.resolve(false));
  }
  async function requestOpenFailedStep(): Promise<boolean> {
    const current = committed.current;
    const proof = current?.selectedRun.captureSelection();
    const target = current && failedStepTarget(current.selectedRun);
    if (!current || !proof || !target || !current.requestOpenFailedStep || current.navigationBusy) return false;
    const isCurrent = () => {
      const latest = committed.current;
      const next = latest && failedStepTarget(latest.selectedRun);
      return Boolean(latest?.selectedRun.isCurrent(proof) && next && next.stepExecutionId === target.stepExecutionId &&
        next.stepId === target.stepId && next.definitionWorkflowId === target.definitionWorkflowId && next.definitionRevision === target.definitionRevision);
    };
    return current.navigate(() => isCurrent() ? current.requestOpenFailedStep!(proof, target, isCurrent) : Promise.resolve(false));
  }

  const snapshot = selectedRun.snapshot;
  const running = Boolean(canonical && !canonical.isTerminal);
  const historyBehind = Boolean(canonical?.isTerminal && snapshot && !snapshot.terminal);
  const snapshotJobId = snapshot?.jobId || "";
  // The step history polls on its own clock; fetch it once more as soon as the run ends.
  useEffect(() => {
    if (historyBehind && snapshotJobId) ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: queryKeys.jobExecutionSteps(snapshotJobId, "") }));
  }, [historyBehind, queryClient, snapshotJobId]);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = globalThis.setInterval(() => setNow(Date.now()), 1000);
    return () => globalThis.clearInterval(timer);
  }, [running]);
  const selectedJobId = selectedRun.selected?.jobId || "";
  // The archived definition the canvas observer already loaded names steps, groups and loops.
  const definition = selectedJobId ? queryClient.getQueryData<JobDefinitionPayload>(["jobs", "detail", selectedJobId, "definition"]) : undefined;
  const barText = canonical ? runBarText({
    statusCode: canonical.statusCode, isTerminal: canonical.isTerminal, snapshot, definition,
    detail: record(selectedRun.verifiedDetail), durationMs: selectedRun.durationMs, now, timezone: options.timezone,
    decisionPanelOpen: options.decisionPanelOpen,
  }, t) : null;
  const historyRows = selectedRun.history.map((row, index) => {
    // The bounded history list can predate completion. Its selected row shares
    // the bar's verified detail; other rows retain their scoped list evidence.
    const isSelected = verified && String(row.id) === selectedRun.selected?.jobId;
    const statusCode = isSelected && canonical ? canonical.statusCode : row.status;
    const group = jobStatusGroup(statusCode);
    const tone: RunBarTone = group === "completed" ? "success" : group === "failed" || group === "stuck" ? "danger"
      : group === "awaiting_approval" || group === "waiting" ? "attention" : group === "cancelled" ? "neutral" : "running";
    const status = runStatusWord(statusCode, t);
    const timestamp = text_(row.started_at) || text_(row.created_at) || text_(row.queued_at) || text_(row.finished_at);
    const durationMs = isSelected ? selectedRun.durationMs : row.duration_ms;
    const meta = [formatRunDuration(durationMs, t), runClockTime(timestamp, options.timezone)].filter(Boolean).join(" · ");
    const number = selectedRun.history.length - index;
    const name = t("react.workflows.test_run_number", "Run #{number}", { number: String(number) });
    return { jobId: String(row.id), number, status, tone, meta, label: [name, status, meta].filter(Boolean).join(" · ") };
  });
  const selectedNumber = historyRows.find((row) => row.jobId === selectedRun.selected?.jobId)?.number ?? null;

  return {
    selection: verified && canonical && selectedRun.selected && barText ? { kind: "verified", run: {
      jobId: selectedRun.selected.jobId, tone: barText.tone, title: barText.title, sub: barText.sub, runNumber: selectedNumber,
    } } : { kind: options.selectionKind },
    history: selectedRun.historyPending ? { kind: "loading" } : selectedRun.historyError ? { kind: "error" }
      : { kind: "ready", rows: historyRows, latestJobId: selectedRun.serverLatest ? String(selectedRun.serverLatest.id) : null },
    navigationBusy,
    viewResultsDisabled: !verified,
    historyDisabled: options.historyDisabled,
    backToLatestDisabled: !verified,
    onViewResults: () => navigate(selectedRun.requestOpenResults),
    onSelectHistory: (jobId) => navigate(() => selectedRun.requestSelectHistory(jobId)),
    onBackToLatest: () => navigate(selectedRun.requestBackToLatest),
    stopAction: canStop ? { onInvoke: requestStop, busy: stopBusy, disabled: navigationBusy } : undefined,
    reviewRequestAction: verified && !options.decisionPanelOpen && selectedRunApprovalId(selectedRun) && options.requestReviewRequest
      ? { onInvoke: requestReviewRequest, disabled: navigationBusy || stopBusy } : undefined,
    openFailedStepAction: verified && failedStepTarget(selectedRun) && options.requestOpenFailedStep
      ? { onInvoke: requestOpenFailedStep, disabled: navigationBusy || stopBusy } : undefined,
    clearMarksAction: verified && canonical?.isTerminal ? { onInvoke: () => navigate(selectedRun.requestClearMarks), disabled: navigationBusy || stopBusy } : undefined,
    runAgainAction: verified && canonical?.isTerminal && options.requestRunAgain ? { onInvoke: requestRunAgain,
      busy: options.runAgainBusy, disabled: navigationBusy || stopBusy || Boolean(options.runAgainBlockedReason), disabledReason: options.runAgainBlockedReason } : undefined,
    toolbarAction: verified && canonical && !canonical.isTerminal && isCancellableJobStatus(canonical.statusCode)
      ? { onInvoke: () => navigate(selectedRun.requestOpenResults), busy: navigationBusy } : undefined,
    actionNotice: notice && selectedRun.isCurrent(notice.proof) ? { kind: notice.kind, message: notice.message } : undefined,
  };
}
