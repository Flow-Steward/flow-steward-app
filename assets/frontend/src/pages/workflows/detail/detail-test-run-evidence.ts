import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { fetchJobApproval, submitApprovalDecision } from "@/pages/jobs/detail/api";
import { approvalQueryKey, dispatchApprovalsRefresh } from "@/pages/jobs/detail/job-detail-core";
import { buildCanonicalJobDetailView } from "@/pages/jobs/detail/job-detail-view-model";
import { jobStatusGroup } from "@/pages/jobs/status-groups";
import type { CanvasRunBarAction, CanvasRunBarHistoryRow, CanvasRunBarProps } from "@/pages/workflows/builder/canvas-run-bar";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import {
  deleteKeptTestOutput,
  fetchKeptTestOutputs,
  keptTestOutputsQueryKey,
  saveKeptTestOutput,
  type KeptTestOutput,
} from "@/pages/workflows/shared/kept-test-output-api";
import type { useSelectedWorkflowTestRun } from "./detail-selected-test-run";
import { selectedRunApprovalId } from "./detail-run-bar-controller";
import {
  approvalShownValues,
  buildRunPasses,
  countPasses,
  definitionNames,
  formatRunDuration,
  isHiddenValue,
  runStatusWord,
  useTestRunPassData,
  type RunCounts,
  type RunPass,
} from "./detail-test-run-results-model";

type SelectedRun = ReturnType<typeof useSelectedWorkflowTestRun>;
type Entity = Record<string, unknown>;

// The builder stores a raw gate; Job Details owns a revision-aware envelope.
function testRunApprovalQueryKey(jobId: string) {
  return ["jobs", "workflow-test-run-approval", jobId] as const;
}

function record(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Entity : {};
}
function text(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

export type TestRunInputValue = { name: string; value: string; hidden: boolean };
export type TestRunApproval = {
  /** The Approval step the gate belongs to; empty for a gate no step names (a recovery approval). */
  stepId: string;
  /** One gate of one run: a retained comment draft belongs to exactly this identity. */
  identity: string;
  stepLabel: string;
  message: string;
  shown: Array<{ name: string; value: unknown }>;
  approvedTo: string;
  rejectedTo: string;
  pending: boolean;
  error: string;
  decide: (approved: boolean, comment?: string) => void;
};

export type WorkflowTestRunEvidence = {
  workflowId: string;
  jobId: string;
  verified: boolean;
  loading: boolean;
  error: string;
  isLatest: boolean;
  runNumber: number | null;
  statusWord: string;
  statusTone: "running" | "success" | "danger" | "attention" | "neutral";
  terminal: boolean;
  runs: readonly CanvasRunBarHistoryRow[];
  runsLoading: boolean;
  runsDisabled: boolean;
  selectRun: (jobId: string) => void;
  inputs: TestRunInputValue[];
  /** What the finished run published (its workflow result), never legacy top-level copies. */
  runOutputs: Record<string, unknown>;
  /** The archived definition the run executed: its revision and digest. */
  executedRevision: number | null;
  executedDigest: string;
  runArtifacts: Record<string, unknown>;
  passes: RunPass[];
  counts: RunCounts;
  duration: string;
  approval: TestRunApproval | null;
  kept: ReadonlyMap<string, KeptTestOutput>;
  keptPending: boolean;
  /** Resolves to "" when kept, else the reason it was not. */
  keepOutput: (stepId: string, outputs: Record<string, unknown>, source: { kind: "run" | "manual"; jobId?: string }) => Promise<string>;
  clearOutput: (stepId: string) => Promise<string>;
  openStep: (stepId: string, tab?: string) => void;
  lightStep: (pass: RunPass | null) => void;
  litPassKey: string;
  changeInputs: () => void;
  runAgain?: CanvasRunBarAction;
  /** Read the selected run's detail and gate again (a panel opening shows the current request). */
  refresh: () => void;
};

type Options = {
  selectedRun: SelectedRun;
  runBar: Pick<CanvasRunBarProps, "history" | "historyDisabled" | "onSelectHistory" | "runAgainAction" | "navigationBusy">;
  workflowId: string;
  canvasGraph: WorkflowCanvasGraph | null | undefined;
  openStep: (stepId: string, tab?: string) => void;
  lightStep: (pass: RunPass | null) => void;
  changeInputs: () => void;
};

/** One owner of what the Results panel and a step's Test Run tab read about the selected run. */
export function useWorkflowTestRunEvidence(options: Options): WorkflowTestRunEvidence {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const { selectedRun, runBar, workflowId, canvasGraph } = options;
  const detail = selectedRun.verifiedDetail;
  const jobId = detail && selectedRun.selected ? selectedRun.selected.jobId : "";
  const canonical = detail ? buildCanonicalJobDetailView({ detail, detailEntity: record(detail), runtimeEntity: record(detail.runtime) }, t) : null;
  const data = useTestRunPassData(jobId, Boolean(jobId));
  const passes = useMemo(() => buildRunPasses(data.rows, definitionNames(data.definition, selectedRun.snapshot), selectedRun.snapshot, t),
    [data.definition, data.rows, selectedRun.snapshot, t]);
  const runs = runBar.history.kind === "ready" ? runBar.history.rows : [];
  const latestJobId = runBar.history.kind === "ready" ? runBar.history.latestJobId : null;
  const group = jobStatusGroup(canonical?.statusCode);
  const statusTone: WorkflowTestRunEvidence["statusTone"] = group === "completed" ? "success" : group === "failed" || group === "stuck" ? "danger"
    : group === "awaiting_approval" || group === "waiting" ? "attention" : group === "cancelled" ? "neutral" : "running";
  const inputs = Object.entries(record(canonical?.invocation.mapped_inputs)).map(([name, value]) => ({
    name,
    value: typeof value === "string" ? value : JSON.stringify(value),
    hidden: isHiddenValue(value),
  }));

  const keptQuery = useQuery({
    queryKey: keptTestOutputsQueryKey(workflowId),
    queryFn: () => fetchKeptTestOutputs(workflowId),
    enabled: Boolean(workflowId),
    retry: false,
  });
  const kept = useMemo(() => {
    const rows = keptQuery.data?.kept_outputs;
    return new Map((Array.isArray(rows) ? rows : []).filter((row) => row && typeof row.step_id === "string").map((row) => [row.step_id, row]));
  }, [keptQuery.data]);
  const keepMutation = useMutation({
    mutationFn: ({ stepId, outputs, kind, sourceJobId }: { stepId: string; outputs: Record<string, unknown>; kind: "run" | "manual"; sourceJobId?: string }) =>
      saveKeptTestOutput(workflowId, stepId, { outputs, source_kind: kind, source_job_id: sourceJobId }),
    onSuccess: () => ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: keptTestOutputsQueryKey(workflowId) })),
  });
  const clearMutation = useMutation({
    mutationFn: (stepId: string) => deleteKeptTestOutput(workflowId, stepId),
    onSuccess: () => ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: keptTestOutputsQueryKey(workflowId) })),
  });

  const awaiting = group === "awaiting_approval" && !canonical?.isTerminal;
  const gateQuery = useQuery({
    queryKey: testRunApprovalQueryKey(jobId),
    queryFn: async () => (await fetchJobApproval(jobId)) ?? null,
    enabled: Boolean(jobId) && awaiting,
    retry: false,
    refetchInterval: awaiting ? 10_000 : false,
  });
  // A new gate on the verified detail (the next approval of the run) re-reads the request.
  const detailApprovalId = selectedRunApprovalId(selectedRun);
  useEffect(() => {
    if (jobId && detailApprovalId) ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: testRunApprovalQueryKey(jobId) }));
  }, [detailApprovalId, jobId, queryClient]);
  const [decisionError, setDecisionError] = useState("");
  const decisionMutation = useMutation({
    mutationFn: ({ threadId, approvalId, approved, comment }: { threadId: string; approvalId: string; approved: boolean; comment: string }) =>
      submitApprovalDecision(threadId, { approval_id: approvalId, approved, ...(comment ? { comment } : {}) }),
    onError: (error) => setDecisionError(error instanceof Error ? error.message : t("react.jobs.approval_request_failed", "Approval request failed.")),
    onSuccess: () => {
      setDecisionError("");
      ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(jobId) }));
      ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: testRunApprovalQueryKey(jobId) }));
      ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: approvalQueryKey(jobId) }));
      ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: queryKeys.jobExecutionSteps(jobId, "") }));
      dispatchApprovalsRefresh();
    },
  });
  // Never show one gate's request under another gate the verified detail names.
  const gate = gateQuery.data && (!detailApprovalId || !text(gateQuery.data.approval_id) || text(gateQuery.data.approval_id) === detailApprovalId) ? gateQuery.data : undefined;
  const gatePayload = record(gate?.payload);
  const approvalStepId = text(gatePayload.step_id) || [...passes].reverse().find((pass) => pass.state === "waiting")?.stepId || "";
  const approval: TestRunApproval | null = awaiting && gate ? (() => {
    const shown = approvalShownValues(record(gatePayload.gate_meta).show_outputs, text(gatePayload.content));
    const target = (outcome: string) => {
      const edge = (canvasGraph?.edges || []).find((row) => row.source.kind === "step" && row.source.id === approvalStepId && row.outcome.name === outcome);
      if (!edge) return "";
      const targetRef = edge.target;
      if (targetRef.kind === "step") return canvasGraph?.steps.find((step) => step.id === targetRef.id)?.display_name || targetRef.id;
      if (targetRef.kind === "group") return canvasGraph?.groups.find((row) => row.id === targetRef.id)?.display_name || targetRef.id;
      if (targetRef.kind === "for_each") return canvasGraph?.for_each?.find((row) => row.id === targetRef.id)?.display_name || targetRef.id;
      return "";
    };
    const threadId = text(gate?.thread_id) || text(record(detail?.job).thread_id) || text(record(detail).thread_id);
    const approvalId = text(gate?.approval_id) || text(gatePayload.approval_id);
    const stepLabel = canvasGraph?.steps.find((step) => step.id === approvalStepId || step.step_id === approvalStepId)?.display_name || approvalStepId;
    return {
      stepId: approvalStepId,
      identity: `${jobId}:${approvalId}`,
      stepLabel,
      message: text(gatePayload.message),
      shown,
      approvedTo: target("approved"),
      rejectedTo: target("rejected"),
      pending: decisionMutation.isPending,
      error: decisionError,
      decide: (approved: boolean, comment = "") => {
        if (!threadId || !approvalId || decisionMutation.isPending) return;
        decisionMutation.mutate({ threadId, approvalId, approved, comment: comment.trim() });
      },
    };
  })() : null;

  const [litPassKey, setLitPassKey] = useState("");
  return {
    workflowId,
    jobId,
    verified: Boolean(jobId),
    // A finished run whose step history predates its end is still loading its steps.
    loading: Boolean(jobId) && (data.pending || (Boolean(canonical?.isTerminal) && !data.terminal)),
    error: data.error instanceof Error ? data.error.message : "",
    isLatest: Boolean(jobId) && (!latestJobId || latestJobId === jobId),
    runNumber: runs.find((row) => row.jobId === jobId)?.number ?? null,
    statusWord: canonical ? runStatusWord(canonical.statusCode, t) : "",
    statusTone,
    terminal: Boolean(canonical?.isTerminal),
    runs,
    runsLoading: runBar.history.kind === "loading",
    runsDisabled: Boolean(runBar.historyDisabled || runBar.navigationBusy),
    selectRun: (target) => {
      if (target === jobId || runBar.historyDisabled || runBar.navigationBusy) return;
      ignoreAsyncResult(Promise.resolve(runBar.onSelectHistory(target)));
    },
    inputs,
    runOutputs: record(record(detail?.workflow_result).outputs),
    executedRevision: data.definition?.available && typeof data.definition.revision === "number" ? data.definition.revision : null,
    executedDigest: data.definition?.available ? text(data.definition.digest) : "",
    runArtifacts: record(record(detail?.workflow_result).artifacts),
    passes,
    counts: countPasses(passes),
    duration: formatRunDuration(selectedRun.durationMs, t),
    approval,
    kept,
    keptPending: keepMutation.isPending || clearMutation.isPending,
    keepOutput: async (stepId, outputs, source) => {
      try {
        await keepMutation.mutateAsync({ stepId, outputs, kind: source.kind, sourceJobId: source.jobId });
        return "";
      } catch (error) {
        return keptOutputErrorMessage(error) || t("react.workflows.kept_output_save_failed", "The test output could not be kept.");
      }
    },
    clearOutput: async (stepId) => {
      try {
        await clearMutation.mutateAsync(stepId);
        return "";
      } catch (error) {
        return keptOutputErrorMessage(error) || t("react.workflows.kept_output_clear_failed", "The test output could not be removed.");
      }
    },
    openStep: options.openStep,
    lightStep: (pass) => {
      setLitPassKey(pass?.key || "");
      options.lightStep(pass);
    },
    litPassKey,
    changeInputs: options.changeInputs,
    runAgain: runBar.runAgainAction,
    refresh: () => {
      if (!jobId) return;
      ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(jobId) }));
      if (awaiting) ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: testRunApprovalQueryKey(jobId) }));
    },
  };
}

/** The error a failed kept-output save reports, for the field under the JSON. */
export function keptOutputErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "";
}
