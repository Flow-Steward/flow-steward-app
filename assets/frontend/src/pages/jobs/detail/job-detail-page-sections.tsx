import type { DrawerGeometry } from "@/components/shared/design-system-overlays";
import { StepInspectorPanel } from "./job-step-inspector-panel";
import { ReadableDataFields, ReadableFileList } from "./readable-data-fields";
import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { createElement, useEffect, useState, type ReactNode, type Ref } from "react";
import { Link } from "react-router";

import { DsAlert, DsCard, DsDrawer, DsValueChip } from "@/components/shared/design-system";
import { jobStatusGroup } from "@/pages/jobs/status-groups";
import {
  type ApprovalDecisionPayload,
  type JobDefinitionPayload,
  type JobExecutionStepsPage,
  type JobDetail,
  type JobFailureSummary,
} from "@/pages/jobs/detail/api";
import {
  firstReadableText,
  humanizeDiagnosticLabel,
  normalizeTimestamp,
  normalizeValue,
  objectOrEmpty,
  workflowPath,
  type Entity,
  type JobViewState,
} from "@/pages/jobs/detail/job-detail-core";
import {
  ReviewWorkspacePanel,
  type ReviewCommentDraft,
} from "@/pages/jobs/detail/job-detail-review-panels";
import {
  costRowDisplay,
  jobFinalAnswer,
  jobProjectionText,
  type CanonicalJobDetailView,
} from "@/pages/jobs/detail/job-detail-view-model";
import { JobDefinitionCanvas } from "./job-definition-canvas";
import { executionStepKey, executionStepStatusLabel, isRunningStep } from "@/pages/jobs/detail/job-detail-execution";
import { StepLiveActivity, StepLiveFiles } from "@/pages/jobs/detail/job-detail-step-live";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

type JobViewStateUpdater = (updater: (current: JobViewState) => JobViewState) => void;

/** The display name of a For each in the executed definition, or its id. */
function forEachLabel(definition: JobDefinitionPayload | undefined, forEachId: unknown): string {
  const id = firstReadableText(forEachId);
  const row = (definition?.for_each || []).find((item) => firstReadableText(item.for_each_id) === id);
  return firstReadableText(row?.display_name) || id;
}

function retryableLabel(value: boolean | null | undefined, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (value === null || value === undefined) {
    return t("dashboard.classic.breakdown.unknown_project", "Unknown");
  }
  return value ? t("settings.common.yes", "Yes") : t("settings.common.no", "No");
}

/** What the run executed and where it waits, from its pinned snapshot and runtime. */
export type JobRunDefinitionView = {
  jobId?: string;
  /** Canonical JobDetail project only; navigation/incident fallbacks cannot authorize runtime facts. */
  projectId?: string;
  activeSteps: Entity[];
  definition: JobDefinitionPayload | undefined;
  executionPage?: JobExecutionStepsPage;
  definitionError: boolean;
  workflowWait: Entity;
};

type JobDetailPrimaryContentProps = {
  approvalCommentDraft?: ReviewCommentDraft;
  reviewWorkspaceRef?: Ref<HTMLDivElement>;
  approvalDecisionUnavailableReason?: string;
  approvalGate: Entity;
  approvalQueryIsLoading: boolean;
  approvalSubmitting: boolean;
  canMutate: boolean;
  canonicalDetail: CanonicalJobDetailView;
  costRows: Entity[];
  decisionHistory: Entity[];
  detail: JobDetail | undefined;
  executionSteps: Entity[];
  failureContext: Entity;
  failureDiagnosticIds: Entity;
  failureEvidence: Entity[];
  failureSummary: JobFailureSummary;
  hasFailureSummary: boolean;
  hasInvocation: boolean;
  hasMoreExecutionSteps: boolean;
  hasReviewWorkspace: boolean;
  heroFacts: Entity[];
  heroSummary: Entity;
  invocation: Entity;
  invocationFiles: Entity[];
  invocationMappedInputs: Entity;
  isLoadingMoreExecutionSteps: boolean;
  loadMoreExecutionSteps: () => Promise<void>;
  nextActionBody: string;
  nextActionTitle: string;
  reviewWorkspace: Entity;
  runDefinition?: JobRunDefinitionView;
  showDefinitionCanvas?: boolean;
  /** The workflow run's error from the execution-steps page; empty when none. */
  runError?: Entity;
  selectedStep: Entity | null;
  selectedStepIndex: number;
  setViewState: JobViewStateUpdater;
  statusLabel: string;
  statusText: string;
  stepSheetOpen: boolean;
  stepInspectorHost?: DrawerGeometry | null;
  stepInspectorChrome?: ReactNode;
  submitApprovalFromWorkspace: (threadId: string, payload: ApprovalDecisionPayload) => Promise<void>;
  totalCostDisplay: string;
  totalTokensValue: number;
  unknownCliTokensValue: number;
  userTimezone: string;
};

function JobRunDefinitionFacts({
  runDefinition,
  showDefinitionCanvas = true,
  userTimezone,
}: Readonly<{ runDefinition: JobRunDefinitionView; showDefinitionCanvas?: boolean; userTimezone: string }>) {
  const { t } = useI18n();
  const { activeSteps, definition, definitionError, workflowWait } = runDefinition;
  const workflowId = firstReadableText(definition?.workflow_id);
  const waitKind = firstReadableText(workflowWait.kind);
  const dueAt = firstReadableText(workflowWait.due_at);
  const childJobId = firstReadableText(workflowWait.child_job_id);
  const lanes = activeSteps.filter((step) => firstReadableText(step.lane_id));
  // The innermost For each of the running step: its item position and bounded counts.
  const forEachProgress = activeSteps
    .map((step) => step.for_each_progress)
    .find((progress): progress is Record<string, unknown> => Boolean(progress) && typeof progress === "object");
  const currentWorkflowLink = workflowId ? (
    <Link className="fs-link text-sm" data-testid="job-current-workflow-link" to={workflowPath(workflowId)}>
      {t("react.jobs.open_the_current_workflow", "Open the current workflow")}
    </Link>
  ) : null;
  return (
    <div className="mt-3 space-y-2" data-testid="job-run-definition">
      {definition?.available ? (
        <p className="text-sm fs-ink-secondary" data-testid="job-run-definition-revision">
          {t("react.jobs.executed_definition_revision_value0_digest_value1", "Executed definition: revision {value0} · {value1}", {
            value0: String(definition.revision ?? ""),
            value1: firstReadableText(definition.digest).slice(0, 12),
          })}{" "}
          {currentWorkflowLink}
        </p>
      ) : null}
      {showDefinitionCanvas && definition?.available && !definitionError ? <JobDefinitionCanvas key={definition.snapshot_id || definition.digest} definition={definition} jobId={runDefinition.jobId} projectId={runDefinition.projectId} executionPage={runDefinition.executionPage} /> : null}
      {definition && !definition.available ? (
        <div data-testid="job-run-definition-unavailable">
          <DsAlert tone="warning" title={t("react.jobs.definition_snapshot_unavailable", "Definition snapshot unavailable")}>
            <p>
              {t("react.jobs.this_run_has_no_pinned_definition_only_its_surviving_logs_are_shown", "This run has no pinned definition. Only its surviving logs are shown; the current workflow may differ from what ran.")}
            </p>
            {currentWorkflowLink}
          </DsAlert>
        </div>
      ) : null}
      {definitionError ? (
        <DsAlert tone="danger" title={t("react.jobs.the_executed_definition_could_not_be_loaded", "The executed definition could not be loaded.")} />
      ) : null}
      {waitKind === "timer" && dueAt ? (
        <p className="text-sm fs-ink-primary" data-testid="job-wait-due-at">
          {t("react.jobs.waiting_until_value0", "Waiting until {value0}", { value0: normalizeTimestamp(dueAt, userTimezone) })}
        </p>
      ) : null}
      {waitKind === "child" && childJobId ? (
        <p className="text-sm fs-ink-primary" data-testid="job-wait-child">
          {t("react.jobs.waiting_for_child_workflow_run", "Waiting for the child workflow run")}{" "}
          <Link className="fs-link" data-testid="job-wait-child-link" to={`/jobs/${encodeURIComponent(childJobId)}`}>
            {childJobId}
          </Link>
        </p>
      ) : null}
      {forEachProgress ? (
        <p className="text-sm fs-ink-primary" data-testid="job-for-each-progress">
          {t("react.jobs.for_each_item_value0_of_value1", "For each {value2}: item {value0} of {value1}", {
            value0: String(Number(forEachProgress.item_index ?? 0) + 1),
            value1: stringFromUnknownValue(forEachProgress.selected_count),
            // The executed definition names the For each; its id is the fallback.
            value2: forEachLabel(definition, forEachProgress.for_each_id),
          })}
          {forEachProgress.truncated ? (
            <span className="ml-2 text-xs fs-ink-tertiary">
              {t("react.jobs.for_each_truncated_value0_value1", "Only the first {value0} of {value1} items are processed.", {
                value0: stringFromUnknownValue(forEachProgress.applied_limit),
                value1: stringFromUnknownValue(forEachProgress.source_count),
              })}
            </span>
          ) : null}
        </p>
      ) : null}
      {lanes.length ? (
        <ul className="space-y-1 text-sm fs-ink-secondary" data-testid="job-active-lanes">
          {lanes.map((step) => (
            <li key={`${firstReadableText(step.lane_id)}-${firstReadableText(step.step_execution_id, step.step_id)}`}>
              {t("react.jobs.lane_value0_step_value1_value2", "Lane {value0}: {value1} ({value2})", {
                value0: firstReadableText(step.lane_id),
                value1: firstReadableText(step.step_id),
                value2: humanizeDiagnosticLabel(firstReadableText(step.status)),
              })}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** Groups of a run that stopped on something an operator has to read. */
const BROKEN_RUN_GROUPS = new Set(["failed", "stuck", "interrupted"]);

function stepIdOf(step: Entity): string {
  return firstReadableText(step.workflow_step_id, step.step_id, step.id);
}

/**
 * Which step broke and why, at the top of the overview: the workflow run's own
 * error (code, message, step) first, then the failed step's recorded error and
 * the failure summary. The overview headline alone only says the run stopped.
 */
function JobStepFailureAlert({
  canonicalDetail,
  detail,
  executionSteps,
  failureSummary,
  runError: pagedRunError,
  setViewState,
}: Readonly<JobDetailPrimaryContentProps>) {
  const { t } = useI18n();
  const group = jobStatusGroup(canonicalDetail.statusCode);
  if (!group || !BROKEN_RUN_GROUPS.has(group)) {
    return null;
  }
  const detailRunError = objectOrEmpty(objectOrEmpty(detail?.workflow_run).error);
  const pagedError = objectOrEmpty(pagedRunError);
  const runError = Object.keys(pagedError).length ? pagedError : detailRunError;
  const authoritativeStopError = [pagedError, detailRunError].find(
    (error) => firstReadableText(error.code).toLowerCase() === "workflow_stop_fail",
  );
  const errorStepId = firstReadableText(authoritativeStopError?.step_id, runError.step_id);
  let stepIndex = errorStepId ? executionSteps.findIndex((step) => stepIdOf(step) === errorStepId) : -1;
  if (!errorStepId) {
    stepIndex = executionSteps.findLastIndex((step) => ["failed", "error"].includes(firstReadableText(step.status).toLowerCase()));
  }
  const step = stepIndex >= 0 ? executionSteps[stepIndex] : undefined;
  const isSemanticDesignedStop = failureSummary.kind === "workflow_stopped";
  const isAuthoritativeDesignedStop = Boolean(authoritativeStopError);
  const isDesignedStop = isSemanticDesignedStop || isAuthoritativeDesignedStop;
  const designedStopReason = firstReadableText(
    isSemanticDesignedStop ? failureSummary.detail : "",
    isAuthoritativeDesignedStop ? authoritativeStopError?.message : "",
  );
  const code = isDesignedStop ? "" : firstReadableText(runError.code, objectOrEmpty(step?.error).code);
  const message = isDesignedStop
    ? designedStopReason
    : firstReadableText(
      runError.message,
      typeof step?.error === "string" ? step.error : objectOrEmpty(step?.error).message,
      failureSummary.present ? failureSummary.detail : "",
    );
  if (!isDesignedStop && !code && !message) {
    return null;
  }
  const stepName = step
    ? firstReadableText(step.title, step.skill_label, stepIdOf(step))
    : firstReadableText(errorStepId ? humanizeDiagnosticLabel(errorStepId) : "");
  const title = isDesignedStop
    ? firstReadableText(
      isSemanticDesignedStop ? failureSummary.title : "",
      t("react.workflows.test_run_stopped", "Stopped"),
    )
    : stepName
      ? t("react.jobs.step_value1_failed", "Step “{value1}” failed", { value1: stepName })
      : t("react.jobs.the_run_stopped_on_an_error", "The run stopped on an error");
  return (
    <div data-testid="job-step-failure">
      <DsAlert title={title} tone={isDesignedStop ? "warning" : "danger"}>
        <p>
          {code ? <DsValueChip>{code}</DsValueChip> : null}
          {code && message ? " " : null}
          {message}
        </p>
        {stepIndex >= 0 ? (
          <button
            className="fs-link"
            onClick={() => setViewState((current) => ({ ...current, selectedStepIndex: stepIndex, selectedStepKey: executionStepKey(executionSteps[stepIndex], stepIndex), stepSheetOpen: true }))}
            type="button"
          >
            {t("react.jobs.show_the_step_in_the_timeline", "Show the step in the timeline")}
          </button>
        ) : null}
      </DsAlert>
    </div>
  );
}

function JobRunOverviewCard(props: Readonly<JobDetailPrimaryContentProps>) {
  const {
    canonicalDetail,
    failureContext,
    heroFacts,
    heroSummary,
    runDefinition,
    showDefinitionCanvas,
    statusLabel,
    userTimezone,
  } = props;
  const { t } = useI18n();
  const overviewFacts = [
    { label: t("account_settings.card.status", "Status"), value: statusLabel },
    ...heroFacts.filter(
      (fact) =>
        firstStringFromUnknownValue([fact.label]).trim().toLowerCase() !== "status",
    ),
  ].slice(0, 5);

  return (
    <div data-testid="job-run-overview">
      <DsCard
        description={firstStringFromUnknownValue([
          heroSummary.headline,
          heroSummary.summary,
          canonicalDetail.taskSummary,
          failureContext.job_error,
          t("shell.summary.none", "(no summary)"),
        ])}
        testId="job-hero-summary"
        title={t("react.jobs.run_overview", "Run overview")}
      >
        <JobStepFailureAlert {...props} />
        {overviewFacts.length ? (
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-5">
            {overviewFacts.map((fact, index) => (
              <div
                className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2"
                key={firstStringFromUnknownValue([fact.label, index]) + "-" + index}
              >
                <p className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">
                  {firstStringFromUnknownValue([fact.label, t("react.jobs.fact", "Fact")])}
                </p>
                <p className="mt-1 break-words text-sm fs-ink-primary">
                  {normalizeValue(fact.value)}
                </p>
              </div>
            ))}
          </div>
        ) : null}
        {runDefinition ? <JobRunDefinitionFacts runDefinition={runDefinition} showDefinitionCanvas={showDefinitionCanvas} userTimezone={userTimezone} /> : null}
      </DsCard>
    </div>
  );
}

function JobResultCard({ detail, executionSteps }: Readonly<JobDetailPrimaryContentProps>) {
  const { t } = useI18n();
  const answer = jobFinalAnswer((detail ?? {}) as Entity, executionSteps);
  const artifacts = objectOrEmpty(detail?.workflow_result?.artifacts);
  const hasArtifacts = Object.keys(artifacts).length > 0;
  if (!answer && !hasArtifacts) {
    return null;
  }
  return (
    <DsCard description={answer?.source} testId="job-result-panel" title={t("jobs.detail.inspector.result", "Result")}>
      {answer ? <ReadableDataFields value={answer.outputs} /> : null}
      {hasArtifacts ? (
        <section aria-label={t("react.workflows.final_artifacts", "Final artifacts")} className="mt-3 space-y-2">
          <h3 className="text-sm font-semibold fs-ink-primary">{t("react.workflows.final_artifacts", "Final artifacts")}</h3>
          <ReadableDataFields value={artifacts} />
        </section>
      ) : null}
    </DsCard>
  );
}

function JobInputsCard({
  hasInvocation,
  invocation,
  invocationFiles,
  invocationMappedInputs,
}: Readonly<JobDetailPrimaryContentProps>) {
  const { t } = useI18n();
  if (!hasInvocation) {
    return null;
  }
  return (
    <DsCard
      description={t("react.jobs.values_and_files_supplied_when_this_workflow_run_started", "Values and files supplied when this workflow run started.")}
      testId="job-inputs-panel"
      title={t("registry.agent_detail.io.inputs", "Inputs")}
    >
      <div className="space-y-4 text-sm">
        {invocation.truncated ? (
          <div className="fs-surface-radius border border-attention fs-surface-attention px-3 py-2 fs-ink-attention">
            {t("react.jobs.some_input_details_were_clipped_to_fit_the_job_metadata_size_limit", "Some input details were clipped to fit the job metadata size limit.")} </div>
        ) : null}
        <ReadableDataFields
          emptyText={t("react.jobs.no_mapped_values", "No mapped values were recorded for this run.")}
          value={invocationMappedInputs}
        />
        {invocationFiles.length ? (
          <section className="space-y-2" aria-labelledby="job-input-files-heading">
            <h3 className="text-sm font-semibold fs-ink-primary" id="job-input-files-heading">
              {t("react.jobs.files", "Files")} </h3>
            <ReadableFileList files={invocationFiles} />
          </section>
        ) : null}
      </div>
    </DsCard>
  );
}

function JobFailureCard({
  failureDiagnosticIds,
  failureEvidence,
  failureSummary,
  hasFailureSummary,
  statusLabel,
  statusText,
}: Readonly<JobDetailPrimaryContentProps>) {
  const { t } = useI18n();
  if (!hasFailureSummary) {
    return null;
  }
  const nextAction = firstReadableText(
    failureSummary.next_action,
    failureSummary.remediation?.summary,
  );
  const facts = [
    [
      "Type",
      humanizeDiagnosticLabel(
        firstStringFromUnknownValue([failureSummary.kind, "unknown"]),
      ),
    ],
    [
      "Status",
      firstReadableText(
        failureSummary.status,
        statusLabel,
        humanizeDiagnosticLabel(
          firstStringFromUnknownValue([statusText, "unknown"]),
        ),
      ),
    ],
    ["Retryable", retryableLabel(failureSummary.retryable, t)],
  ];

  return (
    <DsCard
      className="fs-card-danger"
      description={firstStringFromUnknownValue([
        failureSummary.detail,
        failureSummary.summary,
        t("react.jobs.a_terminal_failure_was_recorded", "A terminal failure was recorded."),
      ])}
      testId="job-failure-summary"
      title={firstStringFromUnknownValue([failureSummary.title, t("react.jobs.failure_summary", "Failure summary")])}
    >
      <div className="space-y-3 text-sm">
        <div className="grid gap-2 md:grid-cols-3">
          {facts.map(([label, value]) => (
            <div
              className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2"
              key={label}
            >
              <p className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">
                {label}
              </p>
              <p className="mt-1 fs-ink-primary">{value}</p>
            </div>
          ))}
        </div>

        {failureEvidence.length ? (
          <div className="grid gap-2 md:grid-cols-2">
            {failureEvidence.slice(0, 6).map((row, index) => (
              <div
                className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2"
                key={firstStringFromUnknownValue([row.label, index]) + "-" + index}
              >
                <p className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">
                  {firstStringFromUnknownValue([row.label, t("jobs.detail.diagnostics.evidence", "Evidence")])}
                </p>
                <p className="mt-1 fs-ink-primary">{normalizeValue(row.value)}</p>
              </div>
            ))}
          </div>
        ) : null}

        {nextAction ? (
          <div className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2">
            <p className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">
              {t("jobs.detail.next_action.title", "Next action")} </p>
            <p className="mt-1 fs-ink-primary">{nextAction}</p>
          </div>
        ) : null}

        {Object.keys(failureDiagnosticIds).length ? (
          <div className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2">
            <p className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">
              {t("react.jobs.diagnostic_ids", "Diagnostic IDs")} </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {Object.entries(failureDiagnosticIds).map(([key, value]) => (
                <span className="fs-badge ink" key={key}>
                  {humanizeDiagnosticLabel(key)}: {stringFromUnknownValue(value)}
                </span>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </DsCard>
  );
}

function JobReviewCard({
  approvalCommentDraft,
  hasReviewWorkspace,
  reviewWorkspaceRef,
  approvalGate,
  canMutate,
  approvalDecisionUnavailableReason,
  approvalQueryIsLoading,
  submitApprovalFromWorkspace,
  approvalSubmitting,
  reviewWorkspace,
}: Readonly<JobDetailPrimaryContentProps>) {
  const { t } = useI18n();
  if (!hasReviewWorkspace) {
    return null;
  }
  return (
    <div ref={reviewWorkspaceRef} role="region" aria-label={t("jobs.detail.review.workspace_title", "Review workspace")} tabIndex={-1}>
    <DsCard
      description={t("react.jobs.human_gate_is_active_for_this_run_continue_with_review_controls_before_resuming_execution", "Human gate is active for this run. Continue with review controls before resuming execution.")}
      testId="job-review-workspace"
      title={t("jobs.detail.review.workspace_title", "Review workspace")}
    >
      <ReviewWorkspacePanel
        commentDraft={approvalCommentDraft}
        approvalGate={approvalGate}
        canMutate={canMutate}
        decisionUnavailableReason={approvalDecisionUnavailableReason}
        isLoading={approvalQueryIsLoading}
        onSubmit={submitApprovalFromWorkspace}
        submitting={approvalSubmitting}
        workspace={reviewWorkspace}
      />
    </DsCard>
    </div>
  );
}

function JobExecutionWaterfall({
  executionSteps,
  hasMoreExecutionSteps,
  isLoadingMoreExecutionSteps,
  loadMoreExecutionSteps,
  selectedStepIndex,
  setViewState,
}: Readonly<JobDetailPrimaryContentProps>) {
  const { t } = useI18n();
  const hasRunningStep = executionSteps.some(isRunningStep);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!hasRunningStep) {
      return undefined;
    }
    const timer = globalThis.setInterval(() => setNow(Date.now()), 1000);
    return () => globalThis.clearInterval(timer);
  }, [hasRunningStep]);
  return (
    <DsCard
      className="min-w-0"
      description={t("react.jobs.select_a_step_to_inspect_runtime_details_and_context", "Select a step to inspect runtime details and context.")}
      testId="job-execution-waterfall"
      title={t("jobs.detail.execution_waterfall", "Execution waterfall")}
    >
      {createElement(
        "platform-execution-waterfall",
        {},
        <div className="space-y-2">
          {hasMoreExecutionSteps ? (
            <button
              className="fs-button secondary w-full"
              disabled={isLoadingMoreExecutionSteps}
              onClick={() => void loadMoreExecutionSteps()}
              type="button"
            >
              {isLoadingMoreExecutionSteps ? t("react.jobs.loading_earlier_steps", "Loading earlier steps…") : t("react.jobs.load_earlier_steps", "Load earlier steps")}
            </button>
          ) : null}
          {executionSteps.map((step, index) => {
            const isActive = index === selectedStepIndex;
            const summary = firstReadableText(
              jobProjectionText(step, "summary", t),
              step.reason,
              step.description,
            );
            const liveFiles = isRunningStep(step) && Array.isArray(step.live_files) ? (step.live_files as Entity[]) : [];
            return (
              <div
                key={executionStepKey(step, index)}
              >
              <button
                aria-pressed={isActive}
                className={[
                  "w-full min-w-0 fs-surface-radius border px-3 py-2 text-left text-sm transition",
                  isActive
                    ? "border-selected fs-surface-selected fs-ink-brand"
                    : "border-subtle fs-surface-inset fs-ink-primary hover:border-selected",
                ]
                  .map((value) => stringFromUnknownValue(value))
                  .join(" ")}
                onClick={() =>
                  setViewState((current) => ({
                    ...current,
                    selectedStepIndex: index,
                    selectedStepKey: executionStepKey(step, index),
                    stepSheetOpen: true,
                  }))
                }
                type="button"
              >
                <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                  <span className="min-w-0 break-words font-medium">
                    {firstStringFromUnknownValue([
                      step.title,
                      step.display_name,
                      step.step_name,
                      t("react.jobs.step", "Step ") + (index + 1),
                    ])}
                  </span>
                  <span className="shrink-0 text-xs fs-ink-tertiary">
                    {executionStepStatusLabel(step, t)}
                  </span>
                </div>
                {isRunningStep(step) ? (
                  <StepLiveActivity now={now} step={step} />
                ) : summary ? (
                  <p className="mt-1 break-words text-xs fs-ink-tertiary">
                    {summary}
                  </p>
                ) : null}
              </button>
              <StepLiveFiles files={liveFiles} />
              </div>
            );
          })}
          {!executionSteps.length ? (
            <div className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-secondary">
              {isLoadingMoreExecutionSteps
                ? t("react.jobs.loading_execution_history", "Loading execution history…")
                : t("react.jobs.no_execution_steps_are_available_from_the_runtime_feed_yet", "No execution steps are available from the runtime feed yet.")}
            </div>
          ) : null}
        </div>,
      )}
    </DsCard>
  );
}

function JobStepInspectorDrawer({
  canonicalDetail,
  selectedStep,
  setViewState,
  stepSheetOpen,
  stepInspectorHost,
  stepInspectorChrome,
}: Readonly<JobDetailPrimaryContentProps>) {
  const { t } = useI18n();
  return (
    <DsDrawer
      closeLabel={t("react.jobs.close_step_details", "Close step details")}
      onClose={() =>
        setViewState((current) => ({
          ...current,
          stepSheetOpen: false,
        }))
      }
      open={stepSheetOpen && stepInspectorHost !== null}
      style={stepInspectorHost ? { top: stepInspectorHost.top, right: stepInspectorHost.right, bottom: "auto", width: stepInspectorHost.width, height: stepInspectorHost.height, minWidth: 0, zIndex: "calc(var(--fs-z-inline-dialog) + var(--fs-z-local-raised))" } : undefined}
      testId="job-step-inspector-drawer"
      title={t("react.jobs.step_details", "Step details")}
    >
      {stepInspectorChrome}
      <StepInspectorPanel
        jobId={canonicalDetail.jobId}
        selectedStep={selectedStep}
        testId="job-step-inspector"
      />
    </DsDrawer>
  );
}

function JobDecisionHistoryCard({
  decisionHistory,
  userTimezone,
}: Readonly<JobDetailPrimaryContentProps>) {
  const { t } = useI18n();
  if (!decisionHistory.length) {
    return null;
  }
  return (
    <DsCard title={t("react.jobs.decision_history", "Decision history")}>
      <div className="space-y-2">
        {decisionHistory.map((decision, index) => (
          <div
            className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-primary"
            key={
              firstStringFromUnknownValue([
                decision.entry_id,
                decision.id,
                index,
              ]) +
              "-" +
              index
            }
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">
                {firstStringFromUnknownValue([
                  decision.title,
                  decision.override_key,
                  t("jobs.detail.previous_decisions.decision", "Decision"),
                ])}
              </span>
              <span className="text-xs fs-ink-tertiary">
                {normalizeTimestamp(
                  decision.timestamp || decision.created_at,
                  userTimezone,
                )}
              </span>
            </div>
            <p className="mt-1 text-xs fs-ink-tertiary">
              {firstStringFromUnknownValue([
                decision.summary,
                decision.override_value,
                decision.reason,
                t("react.jobs.no_summary_recorded", "No summary recorded."),
              ])}
            </p>
          </div>
        ))}
      </div>
    </DsCard>
  );
}

function JobCostBreakdownCard({
  costRows,
  totalCostDisplay,
  totalTokensValue,
  unknownCliTokensValue,
}: Readonly<JobDetailPrimaryContentProps>) {
  const { t } = useI18n();
  return (
    <DsCard testId="job-cost-breakdown" title={t("jobs.detail.cost.title", "Cost and resource usage")}>
      <div className="space-y-2">
        {createElement(
          "platform-cost-breakdown",
          {},
          <>
            <div className="flex flex-wrap items-center gap-2 fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-primary">
              <span>{t("react.jobs.total", "Total:")} {totalCostDisplay}</span>
              <span>
                {t("react.jobs.tokens", "Tokens:")} {firstStringFromUnknownValue([totalTokensValue, 0])}
              </span>
              {unknownCliTokensValue > 0 ? (
                <span>
                  {t("react.jobs.unknown_cost_tokens", "Unknown-cost tokens:")} {unknownCliTokensValue.toLocaleString()}
                </span>
              ) : null}
            </div>
            <div className="mt-2 space-y-2">
              {costRows.map((row, index) => (
                <div
                  className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-xs fs-ink-primary"
                  key={
                    firstStringFromUnknownValue([
                      row.agent_id,
                      row.agent_label,
                      row.agent,
                      index,
                    ]) +
                    "-" +
                    index
                  }
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span>
                      {firstStringFromUnknownValue([
                        row.agent_label,
                        row.agent,
                        "agent",
                      ])}
                    </span>
                    <span>{costRowDisplay(row, t)}</span>
                  </div>
                  <p className="mt-1 fs-ink-tertiary">
                    {firstStringFromUnknownValue([
                      row.model,
                      row.model_used,
                      "model",
                    ])}{" "}
                    · {firstStringFromUnknownValue([row.tokens, 0])} {t("react.jobs.tokens_2", "tokens ·")}{" "}
                    {firstStringFromUnknownValue([row.calls, 0])} calls
                  </p>
                </div>
              ))}
              {!costRows.length ? (
                <div className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-secondary">
                  {t("react.jobs.no_billable_model_usage_recorded_for_this_job", "No billable model usage recorded for this job.")} </div>
              ) : null}
            </div>
          </>,
        )}
      </div>
    </DsCard>
  );
}

function LoadedJobDetailPrimaryContent(
  props: Readonly<JobDetailPrimaryContentProps>,
) {
  return (
    <>
      <JobRunOverviewCard {...props} />
      <JobResultCard {...props} />
      <DsCard
        description={props.nextActionBody}
        testId="job-next-action"
        title={props.nextActionTitle}
      />
      <JobInputsCard {...props} />
      <JobFailureCard {...props} />
      <JobReviewCard {...props} />
      <JobExecutionWaterfall {...props} />
      <JobStepInspectorDrawer {...props} />
      <JobDecisionHistoryCard {...props} />
      <JobCostBreakdownCard {...props} />
    </>
  );
}

function EmptyJobDetailPrimaryContent() {
  const { t } = useI18n();
  return (
    <>
      <DsCard
        description={t("react.jobs.job_detail_payload_is_not_available_yet_the_runtime_surface_will_populate_after_the_next_refresh_cycle", "Job detail payload is not available yet. The runtime surface will populate after the next refresh cycle.")}
        testId="job-hero-summary"
        title={t("react.jobs.hero_summary", "Hero summary")}
      />
      <DsCard
        description={t("react.jobs.refresh_this_page_after_runtime_metadata_is_available", "Refresh this page after runtime metadata is available.")}
        testId="job-next-action"
        title={t("jobs.detail.next_action.title", "Next action")}
      />
      <DsCard testId="job-execution-waterfall" title={t("jobs.detail.execution_waterfall", "Execution waterfall")}>
        <p className="fs-empty-copy">{t("react.jobs.no_execution_steps_are_available", "No execution steps are available.")}</p>
      </DsCard>
      <DsCard testId="job-step-inspector" title={t("react.jobs.step_inspector", "Step inspector")}>
        <p className="fs-empty-copy">
          {t("react.jobs.select_a_job_with_runtime_steps_to_inspect_details", "Select a job with runtime steps to inspect details.")} </p>
      </DsCard>
      <DsCard testId="job-cost-breakdown" title={t("jobs.detail.cost.title", "Cost and resource usage")}>
        <p className="fs-empty-copy">
          {t("react.jobs.cost_telemetry_will_appear_once_billing_data_is_attached_to_this_run", "Cost telemetry will appear once billing data is attached to this run.")} </p>
      </DsCard>
    </>
  );
}

export function JobDetailPrimaryContent(
  props: Readonly<JobDetailPrimaryContentProps>,
) {
  return (
    <div className="space-y-4" data-testid="job-primary-container">
      {props.detail ? (
        <LoadedJobDetailPrimaryContent {...props} />
      ) : (
        <EmptyJobDetailPrimaryContent />
      )}
    </div>
  );
}
