import { useI18n } from "@/lib/i18n/useI18n";

import { useState } from "react";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useParams } from "react-router";
import { useAuth } from "@/lib/auth/useAuth";
import { useRememberProject } from "@/lib/utils/use-remember-project";
import { queryKeys } from "@/lib/query/keys";
import { useKeyedState } from "@/lib/react/useKeyedState";
import {
  fetchJobExecutionStepsPage,
} from "@/pages/jobs/detail/api";
import { buildExecutionSteps, executionStepKey, withLiveStepActivity } from "@/pages/jobs/detail/job-detail-execution";
import {
  DEFAULT_JOB_VIEW_STATE,
  failureSummaryFromDetail,
  firstReadableText,
  listFromUnknown,
  objectOrEmpty,
  safeEntityId,
  type Entity,
  type JobViewState,
} from "@/pages/jobs/detail/job-detail-core";
import {
  approvalGateWithThreadFallback,
  resolveReviewWorkspace,
} from "@/pages/jobs/detail/job-detail-review-panels";
import {
  approvalForApprovalGate,
  detailForApprovalGate,
  useJobLiveState,
} from "@/pages/jobs/detail/job-detail-live-state";
import {
  buildCanonicalJobDetailView,
  detailLifecycleApproval,
  jobCostTotals,
  projectIdFromPayload,
} from "@/pages/jobs/detail/job-detail-view-model";
import {
  firstStringFromUnknownValue,
} from "@/lib/utils/unknown-values";
import { useJobDetailActions } from "./use-job-detail-actions";
import { useJobDetailQueries } from "./use-job-detail-queries";
import { useJobDetailQuery } from "./use-job-detail-query";
import { useJobDetailRealtime } from "./use-job-detail-realtime";
import { canMutateJob, isCancellableJobStatus } from "./job-action-policy";

const EXECUTION_STEPS_LIVE_REFRESH_MS = 5_000;

export function useJobDetailController(jobIdOverride = "", detailQueryEnabled = true) {
  const { t } = useI18n();
  const { jobId: routeJobId = "" } = useParams();
  const jobId = safeEntityId(jobIdOverride) || routeJobId;
  const auth = useAuth();
  const sessionRecord = (auth.session || {}) as Record<string, unknown>;
  const userRecord = (sessionRecord.user || {}) as Record<string, unknown>;
  const userTimezone = firstStringFromUnknownValue([
    userRecord.timezone,
    sessionRecord.timezone,
    "",
  ]).trim();
  const currentJobId = safeEntityId(jobId) || "";
  const {
    state: streamState,
    setState: setStreamState,
    captureApprovalRevision,
    reconcileRestApproval,
  } = useJobLiveState(`${jobId}:live-stream`);
  const streamEvents = streamState.streamEvents;
  const liveStepRows = streamState.liveStepRows;
  const [jobActionMessage, setJobActionMessage] = useState("");
  const [jobActionError, setJobActionError] = useState("");

  const canMutate = canMutateJob(auth.session?.user?.roles || auth.session?.roles || []);

  const detailQuery = useJobDetailQuery(jobId, detailQueryEnabled);
  const detail = detailForApprovalGate(
    detailQuery.data,
    streamState.approvalGate,
    currentJobId,
  );
  const detailEntity = objectOrEmpty(detail);
  const runtimeEntity = objectOrEmpty(detailEntity.runtime);
  const canonicalDetail = buildCanonicalJobDetailView({
    detail,
    detailEntity,
    runtimeEntity,
  }, t);
  const statusText = canonicalDetail.statusCode;
  const statusLabel = canonicalDetail.statusLabel;
  const stepLabel = canonicalDetail.stepLabel;
  const needsApproval = canonicalDetail.needsApproval;
  const invocation = canonicalDetail.invocation;
  const selectedWorkflow = canonicalDetail.selectedWorkflowId;
  const selectedWorkflowLabel = canonicalDetail.selectedWorkflowLabel;
  const viewStateKey = `${currentJobId}:view`;
  const [viewState, setViewState] = useKeyedState<JobViewState>(
    viewStateKey,
    DEFAULT_JOB_VIEW_STATE,
  );
  const stepSheetOpen = viewState.stepSheetOpen;
  const isTerminal = canonicalDetail.isTerminal;
  const failureSummary = failureSummaryFromDetail(
    canonicalDetail.failureSummaryValue,
  );
  const failureEvidence = listFromUnknown(failureSummary.evidence);
  const failureDiagnosticIds = objectOrEmpty(failureSummary.diagnostic_ids);
  const hasFailureSummary = failureSummary.present === true && !needsApproval;

  const detailReviewWorkspace = canonicalDetail.sections.reviewWorkspace;
  const {
    approvalHistoryQuery,
    approvalQuery,
    costBreakdownQuery,
    decisionOverridesQuery,
    definitionQuery,
    incidentQuery,
  } = useJobDetailQueries({
    approvalActive: streamState.approvalGate.kind === "active",
    captureApprovalRevision,
    hasReviewWorkspace: Object.keys(detailReviewWorkspace).length > 0,
    hasFailureSummary,
    isTerminal,
    jobId,
    needsApproval,
    reconcileRestApproval,
  });

  useJobDetailRealtime({
    approval: approvalQuery.data?.approval,
    currentJobId,
    jobId,
    setError: setJobActionError,
    setStreamState,
    streamState,
  });

  const {
    acknowledgeMutation,
    approvalMutation,
    cancelMutation,
    requestAcknowledgeJob,
    requestCancelJob,
    requestRestartJob,
    restartMutation,
    submitApprovalFromWorkspace,
  } = useJobDetailActions(jobId, setJobActionMessage, setJobActionError);

  const incident = incidentQuery.data;
  const detailCost = objectOrEmpty(detailEntity.cost);
  const heroSummary = canonicalDetail.projection.heroSummary;
  const heroFacts = listFromUnknown(heroSummary.facts);
  const projectedNextAction = canonicalDetail.projection.nextAction;
  const approvalPayload = approvalForApprovalGate(
    approvalQuery.data?.approval,
    streamState.approvalGate,
    currentJobId,
  );
  const approvalGate = approvalGateWithThreadFallback(
    approvalPayload,
    firstReadableText(canonicalDetail.job.thread_id, detailEntity.thread_id),
    [
      detailEntity.approval_gate,
      detailLifecycleApproval(detailEntity).gate,
      objectOrEmpty(detailEntity.workflow_run).approval,
    ],
  );
  // A submitted decision is refreshing only while its own gate is still active.
  // The embedded controller survives new runs, including late mutation responses.
  const submittedApproval = approvalMutation.variables;
  const decisionNotice = approvalMutation.isSuccess && needsApproval &&
    submittedApproval?.jobId === jobId &&
    submittedApproval.threadId === firstReadableText(approvalGate.thread_id) &&
    submittedApproval.payload.approval_id === firstReadableText(approvalGate.approval_id)
    ? t("react.jobs.decision_submitted_refreshing_job_state", "Decision submitted. Refreshing job state…")
    : "";
  const reviewWorkspace = resolveReviewWorkspace({
    approvalPayload,
    approvalGate,
    canMutate,
    canonicalDetail,
    currentJobId: jobId,
    runDefinition: definitionQuery.data,
  }, t);
  const executionStepHistoryQuery = useInfiniteQuery({
    queryKey: queryKeys.jobExecutionSteps(jobId, ""),
    queryFn: ({ pageParam }) => fetchJobExecutionStepsPage(jobId, pageParam),
    initialPageParam: "",
    getNextPageParam: (lastPage) =>
      lastPage.page?.has_more && lastPage.page?.next_cursor
        ? lastPage.page.next_cursor
        : undefined,
    enabled: Boolean(jobId),
    // The step list is the run's progress: re-read it while the job runs, or a
    // long step looks exactly like one that never started.
    refetchInterval: isTerminal ? false : EXECUTION_STEPS_LIVE_REFRESH_MS,
  });
  const historicalExecutionSteps =
    executionStepHistoryQuery.data?.pages.flatMap((page) => page.steps || []) || [];
  const lastExecutionStepPage = executionStepHistoryQuery.data?.pages.at(-1)?.page;
  const runError = objectOrEmpty(executionStepHistoryQuery.data?.pages[0]?.run_error);
  const hasMoreExecutionSteps = Boolean(
    lastExecutionStepPage?.has_more && lastExecutionStepPage.next_cursor,
  );

  const executionSteps = withLiveStepActivity(
    buildExecutionSteps({
      detailEntity,
      historyRows: historicalExecutionSteps,
      incident,
      streamRows: liveStepRows,
    }),
    { isTerminal, liveFiles: streamState.liveFiles, runtime: runtimeEntity, streamEvents },
  );
  const selectedStepProjection = canonicalDetail.sections.selectedStep;
  const selectedStepIndex = viewState.selectedStepKey
    ? executionSteps.findIndex((step, index) => executionStepKey(step, index) === viewState.selectedStepKey)
    : Math.min(Math.max(viewState.selectedStepIndex, 0), Math.max(executionSteps.length - 1, 0));
  // A temporarily absent occurrence must not turn into another row's details.
  const selectedStep = executionSteps[selectedStepIndex] ||
    (!viewState.selectedStepKey && Object.keys(selectedStepProjection).length ? selectedStepProjection : null);

  const decisionHistory: Entity[] = (() => {
    const fromDetail = canonicalDetail.sections.previousDecisions;
    if (fromDetail.length) {
      return fromDetail;
    }
    const fromApprovalHistory = listFromUnknown(
      approvalHistoryQuery.data?.approvals,
    ).map((row, index) => {
      const action = firstReadableText(row.action).trim().toLowerCase();
      const approved = row.approved === true;
      const outcome = action
        ? action.charAt(0).toUpperCase() + action.slice(1)
        : approved
          ? "Approved"
          : "Rejected";
      const stage = firstReadableText(row.stage, "Review");
      const question = firstReadableText(row.question);
      const comment = firstReadableText(row.comment);
      return {
        ...row,
        id: row.id || row.entry_id || `approval-${index}`,
        title: row.title || t("react.jobs.value1_decision", "{value1} decision", { value1: String(stage) }),
        status: row.status || outcome,
        summary:
          row.summary ||
          [question, comment].filter(Boolean).join(" ") ||
          `${stage} was ${outcome.toLowerCase()}.`,
        timestamp:
          row.timestamp || row.decided_at || row.created_at || row.updated_at || "",
      };
    });
    if (fromApprovalHistory.length) {
      return fromApprovalHistory;
    }
    return listFromUnknown(decisionOverridesQuery.data?.overrides).map(
      (row) => ({
        ...row,
        title: row.title || row.override_key || t("jobs.detail.previous_decisions.decision", "Decision"),
        status: row.status || (row.active === false ? "Recorded" : "Active"),
        summary:
          row.summary ||
          row.override_value ||
          row.reason ||
          "Override captured.",
        timestamp: row.created_at || row.updated_at || "",
      }),
    );
  })();

  const costRows: Entity[] = (() => {
    const rowsFromDetail = listFromUnknown(
      canonicalDetail.sections.costBreakdown.rows,
    );
    if (rowsFromDetail.length) {
      return rowsFromDetail;
    }
    return listFromUnknown(costBreakdownQuery.data?.breakdown).map((row) => ({
      agent_label: row.agent || "agent",
      model: row.model_used || "model",
      tokens:
        Number(row.tokens_prompt || 0) + Number(row.tokens_completion || 0),
      calls: row.calls || 0,
      cost_usd:
        row.cost_usd === null || row.cost_knownness === "unknown"
          ? null
          : Number(row.known_cost_usd || row.cost_usd || 0),
      known_cost_usd: Number(row.known_cost_usd || row.cost_usd || 0),
      unknown_cli_tokens: Number(row.unknown_cli_tokens || 0),
      cost_knownness: row.cost_knownness || "known",
      cost_display:
        firstReadableText(row.cost_display) ||
        (row.cost_knownness === "unknown"
          ? "Unknown"
          : `$${Number(row.known_cost_usd || row.cost_usd || 0).toFixed(4)}`),
    }));
  })();

  const incidentEntity = objectOrEmpty(incident);
  const projectedRootCause = objectOrEmpty(
    objectOrEmpty(canonicalDetail.sections.diagnostics).root_cause,
  );
  const incidentRootCause = Object.keys(projectedRootCause).length
    ? projectedRootCause
    : objectOrEmpty(incidentEntity.root_cause);
  const failureContext = objectOrEmpty(incidentEntity.failure_context);

  const isCancellable = isCancellableJobStatus(statusText);
  const hasReviewWorkspace =
    needsApproval || Object.keys(reviewWorkspace).length > 0;

  let fallbackNextActionTitle = t("jobs.detail.next_action.title", "Next action");
  if (needsApproval) {
    fallbackNextActionTitle = t("react.jobs.review_workspace", "Review workspace");
  } else if (isTerminal) {
    fallbackNextActionTitle = t("react.jobs.resolution_path", "Resolution path");
  }
  const nextActionTitle =
    firstReadableText(projectedNextAction.title, fallbackNextActionTitle) || t("jobs.detail.next_action.title", "Next action");
  let fallbackNextActionBody =
    t("react.jobs.continue_monitoring_run", "Continue monitoring this run from the execution waterfall and step inspector.");
  if (needsApproval) {
    fallbackNextActionBody =
      t("react.jobs.human_decision_active", "A human decision is active for this run. Continue from the review workspace below.");
  } else if (isTerminal) {
    fallbackNextActionBody = firstReadableText(
      incidentRootCause.message,
      failureContext.job_error,
      t("react.jobs.review_failed_step", "Review the failed step and re-run if needed."),
    );
  }
  const nextActionBody =
    firstReadableText(projectedNextAction.body, fallbackNextActionBody) ||
    t("react.jobs.continue_monitoring_run", "Continue monitoring this run from the execution waterfall and step inspector.");

  const { totalCostDisplay, totalTokensValue, unknownCliTokensValue } = jobCostTotals({
    projected: canonicalDetail.sections.costBreakdown,
    breakdown: costBreakdownQuery.data as Entity | undefined,
    detailCost,
    rows: costRows,
  }, t);
  const resolvedProjectId =
    projectIdFromPayload(detail) || safeEntityId(incident?.project_id);
  const invocationMappedInputs = objectOrEmpty(invocation.mapped_inputs);
  const invocationFiles = listFromUnknown(invocation.files);
  const hasInvocation = Object.keys(invocation).length > 0;

  useRememberProject(resolvedProjectId);

  return {
    identity: {
      canonicalDetail,
      detail,
      detailQuery,
      isTerminal,
      jobId,
      resolvedProjectId,
      selectedWorkflow,
      selectedWorkflowLabel,
      statusLabel,
      stepLabel,
      statusText,
      userTimezone,
    },
    summary: {
      costRows,
      failureDiagnosticIds,
      failureEvidence,
      failureSummary,
      hasFailureSummary,
      heroFacts,
      heroSummary,
      nextActionBody,
      nextActionTitle,
      totalCostDisplay,
      totalTokensValue,
      unknownCliTokensValue,
    },
    execution: {
      executionSteps,
      hasMoreExecutionSteps,
      runError,
      isLoadingMoreExecutionSteps:
        executionStepHistoryQuery.isPending || executionStepHistoryQuery.isFetchingNextPage,
      loadMoreExecutionSteps: async () => {
        if (hasMoreExecutionSteps && !executionStepHistoryQuery.isFetchingNextPage) {
          await executionStepHistoryQuery.fetchNextPage();
        }
      },
      selectedStep,
      selectedStepIndex,
      setViewState,
      stepSheetOpen,
    },
    review: {
      approvalGate,
      approvalQuery,
      canMutate,
      hasReviewWorkspace,
      reviewWorkspace,
    },
    diagnostics: {
      decisionHistory,
      failureContext,
    },
    runDefinition: {
      jobId,
      projectId: projectIdFromPayload(detail) || undefined,
      activeSteps: listFromUnknown(runtimeEntity.active_steps),
      definition: definitionQuery.data,
      executionPage: executionStepHistoryQuery.data?.pages[0],
      definitionError: definitionQuery.isError,
      workflowWait: objectOrEmpty(runtimeEntity.workflow_wait),
    },
    invocationView: {
      hasInvocation,
      invocation,
      invocationFiles,
      invocationMappedInputs,
    },
    actions: {
      acknowledgeMutation,
      approvalMutation,
      cancelMutation,
      requestAcknowledgeJob,
      isCancellable,
      jobActionError,
      jobActionMessage: jobActionMessage || decisionNotice,
      requestCancelJob,
      requestRestartJob,
      restartMutation,
      submitApprovalFromWorkspace,
    },
  };
}
