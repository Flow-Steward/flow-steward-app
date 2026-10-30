import { useI18n } from "@/lib/i18n/useI18n";
import type { DrawerGeometry } from "@/components/shared/design-system-overlays";
import { Loader2 } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, type ReactNode } from "react";
import { Link } from "react-router";

import { DsAlert, DsPageHeader, PageContainer } from "@/components/shared/design-system";
import {
  humanizeDiagnosticLabel,
  firstReadableText,
  normalizeTimestamp,
  workflowPath,
} from "@/pages/jobs/detail/job-detail-core";
import { JobDetailPrimaryContent } from "@/pages/jobs/detail/job-detail-page-sections";
import { JobPageActions } from "./job-page-actions";
import { useJobReportDownload } from "@/pages/jobs/detail/use-job-report-download";
import { useJobDetailController } from "@/pages/jobs/detail/use-job-detail-controller";
import { verifiesJobDetailOwner } from "./job-detail-owner";
import { jobProjectionText } from "@/pages/jobs/detail/job-detail-view-model";
import { jobStatusGroup } from "@/pages/jobs/status-groups";
import type { JobExecutionSnapshot } from "./api";
import { useJobExecutionSnapshot } from "./job-execution-snapshot";
import { executionStepKey } from "./job-detail-execution";
import type { ReviewCommentDraft } from "./job-detail-review-panels";

function jobWorkflowTitle(
  taskSummary: string,
  selectedWorkflow: string,
  selectedWorkflowLabel: string,
): string {
  if (selectedWorkflowLabel && selectedWorkflowLabel !== selectedWorkflow) {
    return selectedWorkflowLabel;
  }
  if (selectedWorkflow) {
    return humanizeDiagnosticLabel(selectedWorkflow.replaceAll(/[.:]+/g, " "));
  }
  return taskSummary || "Job details";
}

/** Acknowledge is for a run that broke and waits for nobody but an operator. */
const ACKNOWLEDGEABLE_GROUPS = new Set(["failed", "stuck"]);


function JobTriggerBadge({ trigger }: Readonly<{ trigger: unknown }>) {
  const { t } = useI18n();
  if (!trigger || typeof trigger !== "object") {
    return null;
  }
  const { detail, label, link } = trigger as Record<string, unknown>;
  if (typeof label !== "string" || !label) {
    return null;
  }
  const triggerLabel = jobProjectionText(trigger as Record<string, unknown>, "label", t);
  const text = t("react.jobs.started_by", "Started by: {trigger}", {
    trigger: `${triggerLabel}${typeof detail === "string" && detail ? ` (${detail})` : ""}`,
  });
  if (typeof link === "string" && link.startsWith("/")) {
    return <Link className="fs-badge" data-testid="job-trigger" to={link}>{text}</Link>;
  }
  return <span className="fs-badge" data-testid="job-trigger">{text}</span>;
}

function JobRunBadges({
  canonicalDetail,
  selectedWorkflow,
  selectedWorkflowLabel,
  statusLabel,
  stepLabel,
  userTimezone,
}: Readonly<{
  canonicalDetail: { startedAt: unknown; finishedAt: unknown; job: Record<string, unknown> };
  selectedWorkflow: string;
  selectedWorkflowLabel: string;
  statusLabel: string;
  stepLabel: string;
  userTimezone: string;
}>) {
  const { t } = useI18n();
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <span className="fs-badge">{t("react.jobs.status", "Status:")} {statusLabel}</span>
      {canonicalDetail.job.acknowledged_at ? <span className="fs-badge" data-testid="job-acknowledged-badge">{t("react.jobs.acknowledged", "Acknowledged")}</span> : null}
      <span className="fs-badge">{t("react.workflows.step_variant_41f3c275", "Step:")} {stepLabel}</span>
      <span className="fs-badge">{t("react.jobs.started", "Started:")} {normalizeTimestamp(canonicalDetail.startedAt, userTimezone)}</span>
      <span className="fs-badge">{t("react.jobs.finished", "Finished:")} {normalizeTimestamp(canonicalDetail.finishedAt, userTimezone)}</span>
      <JobTriggerBadge trigger={canonicalDetail.job.trigger} />
      {selectedWorkflow ? (
        <Link className="fs-badge green min-w-0 max-w-full" data-testid="job-workflow-link" title={selectedWorkflowLabel !== selectedWorkflow ? t("react.jobs.workflow_id_value1", "Workflow ID: {value1}", { value1: String(selectedWorkflow) }) : undefined} to={workflowPath(selectedWorkflow)}>
          <span className="break-words">{t("project_detail.task.preview.workflow", "Workflow:")} {selectedWorkflowLabel}</span>
        </Link>
      ) : <span className="fs-badge" data-testid="job-workflow-missing">{t("react.jobs.workflow_not_recorded", "Workflow not recorded")}</span>}
    </div>
  );
}

function JobQueryNotices({
  approvalError,
  approvalIsError,
  detailError,
  detailIsError,
  detailIsLoading,
  jobActionError,
  jobActionMessage,
}: Readonly<{
  approvalError: unknown;
  approvalIsError: boolean;
  detailError: unknown;
  detailIsError: boolean;
  detailIsLoading: boolean;
  jobActionError: string;
  jobActionMessage: string;
}>) {
  const { t } = useI18n();
  return (
    <>
      {jobActionMessage ? <DsAlert title={jobActionMessage} tone="success" /> : null}
      {jobActionError ? <DsAlert title={jobActionError} tone="danger" /> : null}
      {detailIsLoading ? <p className="fs-empty-copy inline-flex items-center gap-2"><Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />{t("react.jobs.loading_job_detail", "Loading job detail…")}</p> : null}
      {detailIsError ? <DsAlert title={detailError instanceof Error ? detailError.message : t("react.jobs.failed_to_load_job_detail", "Failed to load job detail.")} tone="danger" /> : null}
      {approvalIsError ? <div data-testid="job-approval-error"><DsAlert title={approvalError instanceof Error ? approvalError.message : t("react.jobs.failed_to_load_the_current_approval_state", "Failed to load the current approval state.")} tone="danger" /></div> : null}
    </>
  );
}

/** A parent-captured selected-run proof, independent of the Jobs query cache.
 * The parent must also fence its callback setters (including snapshot null).
 */
export type JobDetailExecutionOwner = Readonly<{
  jobId: string;
  workflowId: string;
  projectId: string;
  generation: number;
  isCurrent: () => boolean;
}>;

/** Read-only intent; the existing owned surface must verify it again before use. */
export type JobDetailInspectionRequest = Readonly<{
  serial: number;
  jobId: string;
  isCurrent: () => boolean;
} & ({ kind: "review"; approvalId: string } | {
  kind: "step"; stepId: string; stepExecutionId: string; definitionWorkflowId: string; definitionRevision: number;
})>;

export function JobsDetailPage({
  embedded = false,
  stepInspectorHost,
  stepInspectorChrome,
  approvalCommentDraft,
  expectedWorkflowId = "",
  expectedProjectId = "",
  detailQueryEnabled = true,
  executionOwner,
  inspectionRequest,
  jobId: explicitJobId = "",
  onRuntimeLocationChange,
  onExecutionSnapshotChange,
}: Readonly<{
  embedded?: boolean;
  stepInspectorHost?: DrawerGeometry | null;
  stepInspectorChrome?: ReactNode;
  approvalCommentDraft?: ReviewCommentDraft;
  expectedWorkflowId?: string;
  expectedProjectId?: string;
  detailQueryEnabled?: boolean;
  executionOwner?: JobDetailExecutionOwner;
  inspectionRequest?: JobDetailInspectionRequest | null;
  jobId?: string;
  onRuntimeLocationChange?: (
    location: { stepId: string; groupId?: string; definitionWorkflowId?: string; definitionRevision?: number } | null,
  ) => void;
  onExecutionSnapshotChange?: (snapshot: JobExecutionSnapshot | null) => void;
}> = {}) {
  const { t } = useI18n();
  const {
    identity: {
      canonicalDetail,
      detail,
      detailQuery,
      isTerminal,
      jobId,
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
      isLoadingMoreExecutionSteps,
      loadMoreExecutionSteps,
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
    runDefinition,
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
      isCancellable,
      jobActionError,
      jobActionMessage,
      requestAcknowledgeJob,
      requestCancelJob,
      requestRestartJob,
      restartMutation,
      submitApprovalFromWorkspace,
    },
  } = useJobDetailController(explicitJobId, detailQueryEnabled);
  const workflowTitle = jobWorkflowTitle(
    canonicalDetail.taskSummary,
    selectedWorkflow,
    selectedWorkflowLabel,
  );
  const pageDescription = t("react.jobs.execution_summary_and_step_by_step_debugging_for_this_workflow_run", "Execution summary and step-by-step debugging for this workflow run.");
  const reportDownloadUrl = jobId
    ? `/api/jobs/${encodeURIComponent(jobId)}/report`
    : "";
  const reportDownload = useJobReportDownload(reportDownloadUrl);
  const workflowMismatch = Boolean(
    expectedWorkflowId &&
      selectedWorkflow &&
      selectedWorkflow !== expectedWorkflowId,
  );
  const activeExecutionStep = [...executionSteps]
    .reverse()
    .find((step) =>
      [
        "in_progress",
        "running",
        "pending",
        "awaiting_approval",
        "awaiting_timer",
        "awaiting_child",
      ].includes(
        firstReadableText(step.status, step.status_code).toLowerCase(),
      ),
    );
  const runtimeExecutionStep = isTerminal
    ? null
    : activeExecutionStep || executionSteps.at(-1) || null;
  const runtimeStepId = runtimeExecutionStep
    ? firstReadableText(
        runtimeExecutionStep.workflow_step_id,
        runtimeExecutionStep.step_id,
        runtimeExecutionStep.id,
      )
    : "";
  const runtimeGroupId = isTerminal ? "" : firstReadableText(runtimeExecutionStep?.group_id, detail?.current_group_id);
  const executedDefinition = runDefinition.definition;
  const ownedEmbedded = embedded && Boolean(
    expectedProjectId || executionOwner || onRuntimeLocationChange || onExecutionSnapshotChange,
  );
  const embeddedOwnerVerified = Boolean(
    ownedEmbedded && expectedWorkflowId && expectedProjectId && jobId &&
    executionOwner?.jobId === jobId && executionOwner.workflowId === expectedWorkflowId &&
    executionOwner.projectId === expectedProjectId && Number.isSafeInteger(executionOwner.generation) &&
    executionOwner.generation > 0 && executionOwner.isCurrent() &&
    verifiesJobDetailOwner(detail, jobId, { workflowId: expectedWorkflowId, projectId: expectedProjectId }),
  );
  const genericEmbeddedVerified = canonicalDetail.jobId === jobId && Boolean(jobId) &&
    (!expectedWorkflowId || selectedWorkflow === expectedWorkflowId);
  const archivedOwnerMatches = Boolean(
    executedDefinition?.available && executedDefinition.job_id === jobId &&
    executedDefinition.workflow_id === expectedWorkflowId &&
    Number.isInteger(executedDefinition.revision) && Number(executedDefinition.revision) > 0,
  );
  // A missing archive remains inspectable. A returned foreign archive is never
  // exposed by the owned embedded surface; it can only clear this current proof.
  const foreignArchive = Boolean(ownedEmbedded && executedDefinition && (
    executedDefinition.job_id !== jobId || executedDefinition.available && executedDefinition.workflow_id !== expectedWorkflowId
  ));
  const embeddedContentAllowed = !embedded || (ownedEmbedded ? embeddedOwnerVerified && !foreignArchive : genericEmbeddedVerified);
  const reviewWorkspaceRef = useRef<HTMLDivElement | null>(null);
  const consumedInspection = useRef<JobDetailInspectionRequest | null>(null);
  const publication = useMemo(() => ({
    embedded, ownedEmbedded, verified: embeddedOwnerVerified, executionOwner,
    onExecutionSnapshotChange, onRuntimeLocationChange,
  }), [embedded, ownedEmbedded, embeddedOwnerVerified, executionOwner, onExecutionSnapshotChange, onRuntimeLocationChange]);
  const committedPublication = useRef<typeof publication | null>(null);
  useLayoutEffect(() => {
    committedPublication.current = publication;
    return () => {
      // Retire the captured render without publishing an unconditional null.
      if (committedPublication.current === publication) committedPublication.current = null;
    };
  }, [publication]);
  const { snapshot: executionSnapshot } = useJobExecutionSnapshot(
    jobId, Boolean(onExecutionSnapshotChange) && (!embedded || embeddedOwnerVerified), runDefinition.projectId,
  );
  useEffect(() => {
    if (committedPublication.current !== publication || publication.embedded && (
      !publication.ownedEmbedded || !publication.verified || !publication.executionOwner?.isCurrent()
    )) return;
    if (embedded && executionSnapshot && (
      executionSnapshot.jobId !== jobId || executionSnapshot.definitionWorkflowId !== expectedWorkflowId
    )) {
      onExecutionSnapshotChange?.(null);
      return;
    }
    onExecutionSnapshotChange?.(executionSnapshot);
  }, [embedded, executionSnapshot, expectedWorkflowId, jobId, onExecutionSnapshotChange, publication]);
  useEffect(() => {
    if (committedPublication.current !== publication || publication.embedded && (
      !publication.ownedEmbedded || !publication.verified || !publication.executionOwner?.isCurrent()
    )) return;
    onRuntimeLocationChange?.(
      (runtimeStepId || runtimeGroupId) && (!embedded || archivedOwnerMatches)
        ? {
            stepId: runtimeStepId,
            groupId: runtimeGroupId,
            definitionWorkflowId: executedDefinition?.available ? executedDefinition.workflow_id || "" : "",
            definitionRevision: executedDefinition?.available ? executedDefinition.revision ?? undefined : undefined,
          }
        : null,
    );
  }, [archivedOwnerMatches, embedded, onRuntimeLocationChange, executedDefinition, publication, runtimeGroupId, runtimeStepId]);

  useEffect(() => {
    if (!inspectionRequest || consumedInspection.current === inspectionRequest ||
      committedPublication.current !== publication || !embedded || !embeddedContentAllowed || !embeddedOwnerVerified ||
      !executionOwner?.isCurrent() || !inspectionRequest.isCurrent() || inspectionRequest.jobId !== jobId) return;
    if (inspectionRequest.kind === "review") {
      const gateId = firstReadableText(approvalGate.approval_id);
      const workspaceId = firstReadableText(reviewWorkspace.approval_id);
      if (canonicalDetail.statusCode !== "awaiting_approval" || approvalQuery.isLoading || approvalQuery.isError ||
        !hasReviewWorkspace || gateId !== inspectionRequest.approvalId || workspaceId && workspaceId !== gateId || !reviewWorkspaceRef.current) return;
      consumedInspection.current = inspectionRequest;
      reviewWorkspaceRef.current.focus();
      reviewWorkspaceRef.current.scrollIntoView?.({ block: "nearest" });
      return;
    }
    if (!archivedOwnerMatches || !executedDefinition?.available ||
      executedDefinition.workflow_id !== inspectionRequest.definitionWorkflowId || executedDefinition.revision !== inspectionRequest.definitionRevision) return;
    const matches = executionSteps.map((step, index) => ({ step, index })).filter(({ step }) =>
      firstReadableText(step.step_execution_id) === inspectionRequest.stepExecutionId &&
      firstReadableText(step.workflow_step_id, step.step_id) === inspectionRequest.stepId);
    if (matches.length !== 1) return;
    const { step, index } = matches[0];
    consumedInspection.current = inspectionRequest;
    setViewState((current) => !inspectionRequest.isCurrent() || !executionOwner.isCurrent() ? current : ({ ...current,
      selectedStepIndex: index, selectedStepKey: executionStepKey(step, index), stepSheetOpen: true }));
  }, [inspectionRequest, publication, embedded, embeddedContentAllowed, embeddedOwnerVerified, executionOwner, jobId,
    approvalGate, reviewWorkspace, canonicalDetail.statusCode, approvalQuery.isLoading, approvalQuery.isError, hasReviewWorkspace,
    archivedOwnerMatches, executedDefinition, executionSteps, setViewState]);

  return (
    <PageContainer width="standard">
    <section
      className={
        embedded
          ? "fs-page-sections overflow-x-hidden fs-settings-portal"
          : "fs-page-sections overflow-x-hidden"
      }
      data-job-id={jobId}
      data-testid={embedded ? "workflow-builder-job-details" : "job-detail-page"}
    >
      {!embeddedContentAllowed ? (
        <p className="fs-empty-copy">{detailQuery.isLoading
          ? t("react.jobs.loading_job_detail", "Loading job detail…")
          : t("react.jobs.failed_to_load_job_detail", "Failed to load job detail.")}</p>
      ) : null}

      {embeddedContentAllowed ? <>
      {embedded ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-medium fs-ink-primary">{t("react.jobs.live_job_details", "Live Job Details")}</p>
          <Link className="fs-link text-sm" to={`/jobs/${encodeURIComponent(jobId)}`}>
            {t("react.jobs.open_full_job_details", "Open full Job Details")} </Link>
        </div>
      ) : (
        <DsPageHeader
        action={<JobPageActions acknowledged={Boolean(canonicalDetail.job.acknowledged_at)} acknowledgePending={acknowledgeMutation.isPending} canAcknowledge={ACKNOWLEDGEABLE_GROUPS.has(jobStatusGroup(canonicalDetail.statusCode) || "")} requestAcknowledgeJob={requestAcknowledgeJob} canMutate={canMutate} cancelPending={cancelMutation.isPending} isCancellable={isCancellable} isTerminal={isTerminal} reportDownload={reportDownload} requestCancelJob={requestCancelJob} requestRestartJob={requestRestartJob} restartPending={restartMutation.isPending} />}
        description={pageDescription}
        eyebrow={t("jobs.page_title", "Jobs")}
        title={workflowTitle}
        />
      )}

      <JobRunBadges canonicalDetail={canonicalDetail} selectedWorkflow={selectedWorkflow} selectedWorkflowLabel={selectedWorkflowLabel} statusLabel={statusLabel} stepLabel={stepLabel} userTimezone={userTimezone} />
      <JobQueryNotices approvalError={approvalQuery.error} approvalIsError={approvalQuery.isError} detailError={detailQuery.error} detailIsError={detailQuery.isError} detailIsLoading={detailQuery.isLoading} jobActionError={jobActionError} jobActionMessage={jobActionMessage} />

      {workflowMismatch ? (
        <DsAlert
          title={t("react.jobs.this_job_belongs_to_a_different_workflow_and_cannot_be_shown_in_this_builder", "This job belongs to a different workflow and cannot be shown in this builder.")}
          tone="danger"
        />
      ) : null}

      {!workflowMismatch ? <JobDetailPrimaryContent
        approvalCommentDraft={embeddedOwnerVerified ? approvalCommentDraft : undefined}
        approvalGate={approvalGate}
        approvalDecisionUnavailableReason={
          approvalQuery.isError
            ? "Approval decisions are temporarily unavailable. Refresh the gate before deciding."
            : undefined
        }
        approvalQueryIsLoading={approvalQuery.isLoading}
        approvalSubmitting={approvalMutation.isPending}
        canMutate={canMutate}
        canonicalDetail={canonicalDetail}
        costRows={costRows}
        decisionHistory={decisionHistory}
        detail={detail}
        executionSteps={executionSteps}
        runError={runError}
        hasMoreExecutionSteps={hasMoreExecutionSteps}
        failureContext={failureContext}
        failureDiagnosticIds={failureDiagnosticIds}
        failureEvidence={failureEvidence}
        failureSummary={failureSummary}
        hasFailureSummary={hasFailureSummary}
        hasInvocation={hasInvocation}
        hasReviewWorkspace={hasReviewWorkspace}
        heroFacts={heroFacts}
        heroSummary={heroSummary}
        invocation={invocation}
        invocationFiles={invocationFiles}
        invocationMappedInputs={invocationMappedInputs}
        isLoadingMoreExecutionSteps={isLoadingMoreExecutionSteps}
        loadMoreExecutionSteps={loadMoreExecutionSteps}
        nextActionBody={nextActionBody}
        nextActionTitle={nextActionTitle}
        reviewWorkspace={reviewWorkspace}
        reviewWorkspaceRef={reviewWorkspaceRef}
        runDefinition={runDefinition}
        showDefinitionCanvas={!embedded}
        selectedStep={selectedStep}
        selectedStepIndex={selectedStepIndex}
        setViewState={setViewState}
        statusLabel={statusLabel}
        statusText={statusText}
        stepSheetOpen={stepSheetOpen}
        stepInspectorHost={embedded ? stepInspectorHost : undefined}
        stepInspectorChrome={embedded ? stepInspectorChrome : undefined}
        submitApprovalFromWorkspace={submitApprovalFromWorkspace}
        totalCostDisplay={totalCostDisplay}
        totalTokensValue={totalTokensValue}
        unknownCliTokensValue={unknownCliTokensValue}
        userTimezone={userTimezone}
      /> : null}

      </> : null}

      {!embedded ? <div>
        <Link className="fs-link text-sm" to="/jobs">
          {t("react.jobs.back_to_jobs_list", "← Back to jobs list")} </Link>
      </div> : null}
    </section>
    </PageContainer>
  );
}
