
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { JobDetail } from "@/pages/jobs/detail/api";
import {
  firstReadableText,
  humanizeDiagnosticLabel,
  listFromUnknown,
  objectOrEmpty,
  safeEntityId,
  type Entity,
} from "@/pages/jobs/detail/job-detail-core";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { jobStatusGroup, jobStatusGroupLabel } from "@/pages/jobs/status-groups";

export function detailLifecycle(detailEntity: Entity): Entity {
  return objectOrEmpty(detailEntity.lifecycle);
}

export function detailJob(detailEntity: Entity): Entity {
  return objectOrEmpty(detailEntity.job);
}

export function detailProjection(detailEntity: Entity): Entity {
  return objectOrEmpty(detailEntity.projection);
}

export function detailSections(detailEntity: Entity): Entity {
  return objectOrEmpty(detailEntity.sections);
}

export function detailLifecycleStatus(detailEntity: Entity): Entity {
  return objectOrEmpty(detailLifecycle(detailEntity).status);
}

export function detailLifecycleStep(detailEntity: Entity): Entity {
  return objectOrEmpty(detailLifecycle(detailEntity).step);
}

export function detailLifecycleApproval(detailEntity: Entity): Entity {
  return objectOrEmpty(detailLifecycle(detailEntity).approval);
}

export function detailLifecycleCosts(detailEntity: Entity): Entity {
  return objectOrEmpty(detailLifecycle(detailEntity).costs);
}

export function detailSelectedStepProjection(detailEntity: Entity): Entity {
  const sectionStep = objectOrEmpty(detailSections(detailEntity).selected_step);
  if (Object.keys(sectionStep).length) {
    return sectionStep;
  }
  const projectionStep = objectOrEmpty(detailProjection(detailEntity).selected_step);
  if (Object.keys(projectionStep).length) {
    return projectionStep;
  }
  return objectOrEmpty(detailEntity.selected_step);
}

export function detailExecutionStepRows(detailEntity: Entity): Entity[] {
  const sectionRows = listFromUnknown(detailSections(detailEntity).execution_steps);
  if (sectionRows.length) {
    return sectionRows;
  }
  const lifecycleRows = listFromUnknown(detailLifecycle(detailEntity).steps);
  if (lifecycleRows.length) {
    return lifecycleRows;
  }
  const projectionRows = listFromUnknown(detailProjection(detailEntity).execution_steps);
  if (projectionRows.length) {
    return projectionRows;
  }
  const topLevelRows = listFromUnknown(detailEntity.execution_steps);
  if (topLevelRows.length) {
    return topLevelRows;
  }
  const selectedStep = detailSelectedStepProjection(detailEntity);
  return Object.keys(selectedStep).length ? [selectedStep] : [];
}

export function detailReviewWorkspaceProjection(detailEntity: Entity): Entity {
  const sectionWorkspace = objectOrEmpty(detailSections(detailEntity).review_workspace);
  if (Object.keys(sectionWorkspace).length) {
    return sectionWorkspace;
  }
  const lifecycleWorkspace = objectOrEmpty(detailLifecycleApproval(detailEntity).review_workspace);
  if (Object.keys(lifecycleWorkspace).length) {
    return lifecycleWorkspace;
  }
  const projectionWorkspace = objectOrEmpty(detailProjection(detailEntity).review_workspace);
  if (Object.keys(projectionWorkspace).length) {
    return projectionWorkspace;
  }
  return objectOrEmpty(detailEntity.review_workspace);
}

export function detailCostBreakdownProjection(detailEntity: Entity): Entity {
  const sectionCosts = objectOrEmpty(detailSections(detailEntity).cost_breakdown);
  if (Object.keys(sectionCosts).length) {
    return sectionCosts;
  }
  const lifecycleCosts = detailLifecycleCosts(detailEntity);
  if (Object.keys(lifecycleCosts).length) {
    return lifecycleCosts;
  }
  const projectionCosts = objectOrEmpty(detailProjection(detailEntity).cost_breakdown);
  if (Object.keys(projectionCosts).length) {
    return projectionCosts;
  }
  return objectOrEmpty(detailEntity.cost_breakdown);
}

export function detailPreviousDecisionRows(detailEntity: Entity): Entity[] {
  const sectionRows = listFromUnknown(detailSections(detailEntity).previous_decisions);
  if (sectionRows.length) {
    return sectionRows;
  }
  const projectionRows = listFromUnknown(detailProjection(detailEntity).previous_decisions);
  if (projectionRows.length) {
    return projectionRows;
  }
  return listFromUnknown(detailEntity.previous_decisions);
}

export function detailDiagnosticsProjection(detailEntity: Entity): Entity {
  const sectionDiagnostics = objectOrEmpty(detailSections(detailEntity).diagnostics);
  if (Object.keys(sectionDiagnostics).length) {
    return sectionDiagnostics;
  }
  const projectionDiagnostics = objectOrEmpty(detailProjection(detailEntity).diagnostics);
  if (Object.keys(projectionDiagnostics).length) {
    return projectionDiagnostics;
  }
  return objectOrEmpty(detailEntity.diagnostics);
}

export function detailHeroSummaryProjection(detailEntity: Entity): Entity {
  const nestedProjection = objectOrEmpty(detailProjection(detailEntity).hero_summary);
  if (Object.keys(nestedProjection).length) {
    return nestedProjection;
  }
  return objectOrEmpty(detailEntity.hero_summary);
}

export function detailNextActionProjection(detailEntity: Entity): Entity {
  const nestedProjection = objectOrEmpty(detailProjection(detailEntity).next_action);
  if (Object.keys(nestedProjection).length) {
    return nestedProjection;
  }
  return objectOrEmpty(detailEntity.next_action);
}

export function detailJobActionsProjection(detailEntity: Entity): Entity {
  const nestedProjection = objectOrEmpty(detailProjection(detailEntity).job_actions);
  if (Object.keys(nestedProjection).length) {
    return nestedProjection;
  }
  return objectOrEmpty(detailEntity.job_actions);
}

export function detailInvocationPayload(detailEntity: Entity): Entity {
  const nestedJob = objectOrEmpty(detailJob(detailEntity).invocation);
  if (Object.keys(nestedJob).length) {
    return nestedJob;
  }
  return objectOrEmpty(detailEntity.invocation);
}

export function detailFailureSummaryPayload(detailEntity: Entity): unknown {
  const nestedJobFailureSummary = detailJob(detailEntity).failure_summary;
  if (nestedJobFailureSummary !== undefined && nestedJobFailureSummary !== null) {
    return nestedJobFailureSummary;
  }
  return detailEntity.failure_summary;
}

export function detailJobId(detailEntity: Entity, detail?: JobDetail): string {
  return firstReadableText(detailJob(detailEntity).id, detailEntity.id, detail?.id);
}

export function detailTaskSummary(detailEntity: Entity, detail?: JobDetail): string {
  const jobPayload = detailJob(detailEntity);
  return firstReadableText(
    jobPayload.task_summary,
    jobPayload.display_summary,
    detailEntity.task_summary,
    detailEntity.display_summary,
    detail?.task_summary,
    detail?.display_summary,
  );
}

export function detailStartedAt(detailEntity: Entity, detail?: JobDetail): string {
  return firstReadableText(detailJob(detailEntity).started_at, detailEntity.started_at, detail?.started_at);
}

export function detailFinishedAt(detailEntity: Entity, detail?: JobDetail): string {
  return firstReadableText(detailJob(detailEntity).finished_at, detailEntity.finished_at, detail?.finished_at);
}

export function detailSelectedWorkflowId(detailEntity: Entity, runtimeEntity?: Entity): string {
  const jobPayload = detailJob(detailEntity);
  return stringFromUnknownValue(firstReadableText(
      jobPayload.selected_workflow,
      detailEntity.selected_workflow,
      runtimeEntity?.selected_workflow,
    )).trim();
}

export function detailSelectedWorkflowLabel(detailEntity: Entity, runtimeEntity?: Entity): string {
  const selectedWorkflow = detailSelectedWorkflowId(detailEntity, runtimeEntity);
  const jobPayload = detailJob(detailEntity);
  return firstReadableText(
    jobPayload.selected_workflow_display_name,
    detailEntity.selected_workflow_display_name,
    detailEntity.workflow_display_name,
    runtimeEntity?.workflow_display_name,
    runtimeEntity?.selected_workflow_display_name,
    selectedWorkflow,
  );
}

export function detailStatusCode(detailEntity: Entity, detail?: JobDetail): string {
  const jobPayload = detailJob(detailEntity);
  return firstStringFromUnknownValue([detailLifecycleStatus(detailEntity).code, jobPayload.status, detailEntity.status, detail?.status, "unknown"])
    .trim()
    .toLowerCase();
}

export function detailStatusLabel(detailEntity: Entity, detail?: JobDetail, t: I18nContextValue["t"] = fallbackContext.t): string {
  const selectedCode = detailStatusCode(detailEntity, detail);
  const localizeLabel = (label: string) => selectedCode === "awaiting_approval"
    ? t("react.jobs.waiting_for_approval", label)
    : label;
  const lifecycleStatus = detailLifecycleStatus(detailEntity);
  const lifecycleCode = firstStringFromUnknownValue([lifecycleStatus.code, ""]).trim().toLowerCase();
  if (lifecycleCode && lifecycleCode === selectedCode) {
    const lifecycleLabel = firstReadableText(lifecycleStatus.label);
    if (lifecycleLabel) {
      return localizeLabel(jobProjectionText(lifecycleStatus, "label", t));
    }
  }
  const explicit = firstReadableText(
    detailEntity.status_label,
  );
  return explicit ? localizeLabel(explicit) : (selectedCode ? localizeLabel(humanizeDiagnosticLabel(selectedCode)) : t("dashboard.classic.breakdown.unknown_project", "Unknown"));
}

/**
 * The status group as the headline and the detailed label as secondary text,
 * e.g. "Failed · Stopped after repeated retries". A status that is its group's
 * namesake (failed, completed, …) has nothing to add. A code outside every
 * group is shown raw: Job details is the one place an unknown status is spelled out.
 */
export function detailGroupedStatusLabel(detailEntity: Entity, detail?: JobDetail, t: I18nContextValue["t"] = fallbackContext.t): string {
  const code = detailStatusCode(detailEntity, detail);
  const group = jobStatusGroup(code);
  if (!group) return code;
  const groupLabel = jobStatusGroupLabel(group, t);
  const detailed = detailStatusLabel(detailEntity, detail, t).trim();
  return code !== group && detailed && detailed.toLowerCase() !== groupLabel.toLowerCase()
    ? `${groupLabel} · ${detailed}`
    : groupLabel;
}

export function jobProjectionText(row: Entity, field: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const value = firstReadableText(row[field]);
  const key = firstReadableText(row[`${field}_key`]);
  const params = Object.fromEntries(Object.entries(objectOrEmpty(row[`${field}_params`]))
    .filter(([, param]) => param === null || ["string", "number", "boolean"].includes(typeof param))) as Record<string, string | number | boolean | null>;
  return key ? t(key, value, params) : value;
}

function localizedHeroSummary(row: Entity, t: I18nContextValue["t"]): Entity {
  return {
    ...row,
    headline: jobProjectionText(row, "headline", t),
    summary: jobProjectionText(row, "summary", t),
    facts: listFromUnknown(row.facts).map((fact) => ({ ...fact, label: jobProjectionText(fact, "label", t) })),
  };
}

export function detailStepCode(detailEntity: Entity, runtimeEntity?: Entity): string {
  const jobPayload = detailJob(detailEntity);
  return firstReadableText(
    detailLifecycleStep(detailEntity).code,
    jobPayload.current_step_id,
    detailEntity.current_step_id,
    runtimeEntity?.current_step_id,
  );
}

export function detailStepLabel(detailEntity: Entity, runtimeEntity?: Entity): string {
  const selectedCode = detailStepCode(detailEntity, runtimeEntity);
  const lifecycleStep = detailLifecycleStep(detailEntity);
  const lifecycleCode = firstStringFromUnknownValue([lifecycleStep.code, ""]).trim();
  if (lifecycleCode && lifecycleCode === selectedCode) {
    const lifecycleLabel = firstReadableText(lifecycleStep.label);
    if (lifecycleLabel) {
      return lifecycleLabel;
    }
  }
  return selectedCode ? humanizeDiagnosticLabel(selectedCode) : "—";
}

export function detailApprovalState(detailEntity: Entity): string {
  return firstStringFromUnknownValue([detailLifecycleApproval(detailEntity).state, detailEntity.approval_state, ""])
    .trim()
    .toLowerCase();
}

export function finiteNumberOrNull(value: unknown): number | null {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}

export function detailNeedsApproval(detailEntity: Entity, detail?: JobDetail): boolean {
  return detailApprovalState(detailEntity) === "awaiting_approval" || detailStatusCode(detailEntity, detail) === "awaiting_approval";
}

export function detailIsTerminal(detailEntity: Entity, detail?: JobDetail): boolean {
  const lifecycleStatus = detailLifecycleStatus(detailEntity);
  if (typeof lifecycleStatus.terminal === "boolean") {
    return lifecycleStatus.terminal;
  }
  return ["failed", "completed", "cancelled", "max_iterations"].includes(
    detailStatusCode(detailEntity, detail),
  );
}

export function jobDetailPollingInterval(
  detail: JobDetail | undefined,
  interval: number,
): number | false {
  return detail && detailIsTerminal(objectOrEmpty(detail), detail) ? false : interval;
}

export type CanonicalJobDetailView = {
  job: Entity;
  lifecycle: Entity;
  statusCode: string;
  statusLabel: string;
  stepCode: string;
  stepLabel: string;
  approvalState: string;
  needsApproval: boolean;
  isTerminal: boolean;
  jobId: string;
  taskSummary: string;
  startedAt: string;
  finishedAt: string;
  selectedWorkflowId: string;
  selectedWorkflowLabel: string;
  invocation: Entity;
  failureSummaryValue: unknown;
  sections: {
    selectedStep: Entity;
    executionSteps: Entity[];
    executionStepsPage: Entity;
    reviewWorkspace: Entity;
    previousDecisions: Entity[];
    costBreakdown: Entity;
    diagnostics: Entity;
  };
  projection: {
    heroSummary: Entity;
    nextAction: Entity;
    jobActions: Entity;
  };
};

export function buildCanonicalJobDetailView({
  detail,
  detailEntity,
  runtimeEntity,
}: {
  detail?: JobDetail;
  detailEntity: Entity;
  runtimeEntity?: Entity;
}, t: I18nContextValue["t"] = fallbackContext.t): CanonicalJobDetailView {
  return {
    job: detailJob(detailEntity),
    lifecycle: detailLifecycle(detailEntity),
    statusCode: detailStatusCode(detailEntity, detail),
    statusLabel: detailGroupedStatusLabel(detailEntity, detail, t),
    stepCode: detailStepCode(detailEntity, runtimeEntity),
    stepLabel: detailStepLabel(detailEntity, runtimeEntity),
    approvalState: detailApprovalState(detailEntity),
    needsApproval: detailNeedsApproval(detailEntity, detail),
    isTerminal: detailIsTerminal(detailEntity, detail),
    jobId: detailJobId(detailEntity, detail),
    taskSummary: detailTaskSummary(detailEntity, detail),
    startedAt: detailStartedAt(detailEntity, detail),
    finishedAt: detailFinishedAt(detailEntity, detail),
    selectedWorkflowId: detailSelectedWorkflowId(detailEntity, runtimeEntity),
    selectedWorkflowLabel: detailSelectedWorkflowLabel(detailEntity, runtimeEntity),
    invocation: detailInvocationPayload(detailEntity),
    failureSummaryValue: detailFailureSummaryPayload(detailEntity),
    sections: {
      selectedStep: detailSelectedStepProjection(detailEntity),
      executionSteps: detailExecutionStepRows(detailEntity),
      executionStepsPage: objectOrEmpty(detailSections(detailEntity).execution_steps_page),
      reviewWorkspace: detailReviewWorkspaceProjection(detailEntity),
      previousDecisions: detailPreviousDecisionRows(detailEntity),
      costBreakdown: detailCostBreakdownProjection(detailEntity),
      diagnostics: detailDiagnosticsProjection(detailEntity),
    },
    projection: {
      heroSummary: localizedHeroSummary(detailHeroSummaryProjection(detailEntity), t),
      nextAction: {
        ...detailNextActionProjection(detailEntity),
        title: jobProjectionText(detailNextActionProjection(detailEntity), "title", t),
        body: jobProjectionText(detailNextActionProjection(detailEntity), "body", t),
      },
      jobActions: detailJobActionsProjection(detailEntity),
    },
  };
}

export function mergeLifecycleRecord(current: JobDetail | undefined, patch: Entity): Entity {
  const lifecycle = objectOrEmpty(current?.lifecycle);
  const status = {
    ...objectOrEmpty(lifecycle.status),
    ...objectOrEmpty(patch.status),
  };
  const step = {
    ...objectOrEmpty(lifecycle.step),
    ...objectOrEmpty(patch.step),
  };
  const approval = {
    ...objectOrEmpty(lifecycle.approval),
    ...objectOrEmpty(patch.approval),
  };
  return {
    ...lifecycle,
    ...patch,
    status,
    step,
    approval,
  };
}

export function mergeJobRecord(current: JobDetail | undefined, patch: Entity): Entity {
  const job = objectOrEmpty(current?.job);
  return {
    ...job,
    ...patch,
  };
}

export function jobMetadataPatch(frameRecord: Entity): Entity {
  const patch: Entity = {};
  const status = firstReadableText(frameRecord.status);
  const currentStepId = firstReadableText(frameRecord.current_step_id, frameRecord.step_id);
  const currentGroupId = firstReadableText(frameRecord.current_group_id, frameRecord.group_id);
  const currentAgent = firstReadableText(frameRecord.current_agent, frameRecord.active_agent, frameRecord.agent);
  const selectedWorkflow = firstReadableText(frameRecord.selected_workflow);
  const selectedWorkflowDisplayName = firstReadableText(
    frameRecord.selected_workflow_display_name,
    frameRecord.workflow_display_name,
  );
  const selectedWorkflowSource = firstReadableText(frameRecord.selected_workflow_source);
  const workflowSelectionReason = firstReadableText(frameRecord.workflow_selection_reason);
  const startedAt = firstReadableText(frameRecord.started_at);
  const finishedAt = firstReadableText(frameRecord.finished_at, frameRecord.completed_at);
  const taskSummary = firstReadableText(frameRecord.task_summary, frameRecord.display_summary);
  const displaySummary = firstReadableText(frameRecord.display_summary, frameRecord.task_summary);
  const threadId = firstReadableText(frameRecord.thread_id);

  if (status) {
    patch.status = status;
  }
  if (currentStepId) {
    patch.current_step_id = currentStepId;
  }
  if (currentGroupId) {
    patch.current_group_id = currentGroupId;
  }
  if (currentAgent) {
    patch.current_agent = currentAgent;
  }
  if (selectedWorkflow) {
    patch.selected_workflow = selectedWorkflow;
  }
  if (selectedWorkflowDisplayName) {
    patch.selected_workflow_display_name = selectedWorkflowDisplayName;
  }
  if (selectedWorkflowSource) {
    patch.selected_workflow_source = selectedWorkflowSource;
  }
  if (workflowSelectionReason) {
    patch.workflow_selection_reason = workflowSelectionReason;
  }
  if (startedAt) {
    patch.started_at = startedAt;
  }
  if (finishedAt) {
    patch.finished_at = finishedAt;
  }
  if (taskSummary) {
    patch.task_summary = taskSummary;
  }
  if (displaySummary) {
    patch.display_summary = displaySummary;
  }
  if (threadId) {
    patch.thread_id = threadId;
  }
  return patch;
}

export function mergeSectionsRecord(current: JobDetail | undefined, patch: Entity): Entity {
  const sections = objectOrEmpty(current?.sections);
  return {
    ...sections,
    ...patch,
    review_workspace: {
      ...objectOrEmpty(sections.review_workspace),
      ...objectOrEmpty(patch.review_workspace),
    },
    selected_step: {
      ...objectOrEmpty(sections.selected_step),
      ...objectOrEmpty(patch.selected_step),
    },
    cost_breakdown: {
      ...objectOrEmpty(sections.cost_breakdown),
      ...objectOrEmpty(patch.cost_breakdown),
    },
    diagnostics: {
      ...objectOrEmpty(sections.diagnostics),
      ...objectOrEmpty(patch.diagnostics),
    },
    ...(Array.isArray(patch.execution_steps) ? { execution_steps: patch.execution_steps } : {}),
    ...(Array.isArray(patch.previous_decisions) ? { previous_decisions: patch.previous_decisions } : {}),
  };
}

function lifecycleStatusFields(
  frameRecord: Entity,
  nextStatusCode: string,
  currentStatus: Entity,
): Entity {
  if (!nextStatusCode) {
    return {};
  }
  const previousCode = firstStringFromUnknownValue([currentStatus.code, ""])
    .trim()
    .toLowerCase();
  const statusChanged = nextStatusCode.toLowerCase() !== previousCode;
  const attentionGroup = firstReadableText(frameRecord.attention_group);
  return {
    code: nextStatusCode,
    label: firstReadableText(frameRecord.status_label) || (statusChanged ? "" : currentStatus.label),
    tone: firstReadableText(frameRecord.status_tone) || (statusChanged ? "" : currentStatus.tone),
    ...(typeof frameRecord.terminal === "boolean" ? { terminal: frameRecord.terminal } : {}),
    ...(attentionGroup ? { attention_group: attentionGroup } : {}),
  };
}

function lifecycleStepFields(nextStepCode: string, currentStep: Entity): Entity {
  if (!nextStepCode) {
    return {};
  }
  const previousCode = firstStringFromUnknownValue([currentStep.code, ""]).trim();
  const stepChanged = nextStepCode !== previousCode;
  return {
    code: nextStepCode,
    label: stepChanged ? "" : currentStep.label,
  };
}

export function lifecycleStatusPatch(frameRecord: Entity, current?: JobDetail): Entity {
  const lifecycle = objectOrEmpty(current?.lifecycle);
  const currentStatus = objectOrEmpty(lifecycle.status);
  const currentStep = objectOrEmpty(lifecycle.step);
  const nextStatusCode = firstReadableText(frameRecord.status);
  const nextStepCode = firstReadableText(frameRecord.current_step_id);
  const statusPatch = lifecycleStatusFields(frameRecord, nextStatusCode, currentStatus);
  const stepPatch = lifecycleStepFields(nextStepCode, currentStep);
  return {
    ...(Object.keys(statusPatch).length ? { status: statusPatch } : {}),
    ...(Object.keys(stepPatch).length ? { step: stepPatch } : {}),
  };
}

export function lifecycleRuntimePatch(frameRecord: Entity, current?: JobDetail): Entity {
  return lifecycleStatusPatch(
    { current_step_id: firstReadableText(frameRecord.current_step_id) },
    current,
  );
}

export function lifecycleApprovalPatch(frameRecord: Entity, projectedWorkspace: Entity, current?: JobDetail): Entity {
  const patch = lifecycleStatusPatch(
    {
      status: "awaiting_approval",
      status_label: "Waiting for approval",
      current_step_id: firstReadableText(frameRecord.current_step_id, frameRecord.step_id),
    },
    current,
  );
  return {
    ...patch,
    approval: {
      state: "awaiting_approval",
      label: "Waiting for approval",
      ...(Object.keys(projectedWorkspace).length ? { review_workspace: projectedWorkspace } : {}),
    },
  };
}

export function jobDetailWithoutStaleSummaries(
  current: JobDetail | undefined,
  frame: Entity,
): JobDetail | undefined {
  if (!current) return current;
  const record = objectOrEmpty(current);
  const nextStatus = firstReadableText(frame.status);
  const nextStep = firstReadableText(frame.current_step_id, frame.step_id);
  const nextApprovalId = firstReadableText(frame.approval_id, frame.interrupt_id);
  const currentApprovalId = firstReadableText(
    objectOrEmpty(detailLifecycleApproval(record).gate).approval_id,
    detailReviewWorkspaceProjection(record).approval_id,
    record.approval_id,
  );
  if (
    (!nextStatus || nextStatus === detailStatusCode(record)) &&
    (!nextStep || nextStep === detailStepCode(record)) &&
    (!nextApprovalId || nextApprovalId === currentApprovalId)
  ) return current;

  // These texts belong to the canonical detail response. A lifecycle frame cannot
  // update them, so hide the old projection while its replacement is fetched.
  return {
    ...current,
    hero_summary: {},
    next_action: {},
    projection: { ...detailProjection(record), hero_summary: {}, next_action: {} },
  };
}

export function jobDetailWithApprovalFrame(
  current: JobDetail | undefined,
  frameRecord: Entity,
  currentJobId: string,
): JobDetail {
  current = jobDetailWithoutStaleSummaries(current, { ...frameRecord, status: "awaiting_approval" });
  const projectedWorkspace = objectOrEmpty(frameRecord.review_workspace);
  return {
    ...(current || ({ id: currentJobId, status: "awaiting_approval" } as JobDetail)),
    status: "awaiting_approval",
    job: mergeJobRecord(current, {
      ...jobMetadataPatch(frameRecord),
      status: "awaiting_approval",
    }),
    lifecycle: {
      ...mergeLifecycleRecord(
        current,
        lifecycleApprovalPatch(frameRecord, projectedWorkspace, current),
      ),
      approval: {
        state: "awaiting_approval",
        label: "Waiting for approval",
        gate: frameRecord,
        review_workspace: projectedWorkspace,
      },
    },
    sections: {
      ...objectOrEmpty(current?.sections),
      review_workspace: projectedWorkspace,
    },
    review_workspace: projectedWorkspace,
  };
}

export function decisionDiagnosticsPatch(frameData: unknown, current?: JobDetail): Entity {
  if (!Array.isArray(frameData)) {
    return {};
  }
  const currentDiagnostics = detailDiagnosticsProjection(objectOrEmpty(current as unknown));
  const currentTabs = objectOrEmpty(currentDiagnostics.tabs);
  const decisionsTab = {
    ...objectOrEmpty(currentTabs.decisions),
    count: frameData.length,
    rows: frameData,
  };
  const overridesTab = {
    ...objectOrEmpty(currentTabs.overrides),
    count: frameData.length,
    rows: frameData,
  };
  return {
    ...currentDiagnostics,
    tabs: {
      ...currentTabs,
      decisions: decisionsTab,
      overrides: overridesTab,
    },
  };
}

export function costBreakdownStreamPatch(frameRecord: Entity, current?: JobDetail): Entity {
  const currentBreakdown = detailCostBreakdownProjection(objectOrEmpty(current as unknown));
  const totalCostUsd =
    finiteNumberOrNull(frameRecord.total_cost_usd) ??
    finiteNumberOrNull(frameRecord.cost_usd) ??
    finiteNumberOrNull(frameRecord.total_cost);
  const totalTokens =
    finiteNumberOrNull(frameRecord.total_tokens) ??
    finiteNumberOrNull(frameRecord.tokens_total) ??
    finiteNumberOrNull(frameRecord.tokens);

  return {
    ...currentBreakdown,
    ...(totalCostUsd !== null
      ? {
          total_cost_usd: totalCostUsd,
          total_cost_display: firstReadableText(frameRecord.total_cost_display) || `$${totalCostUsd.toFixed(4)}`,
        }
      : {}),
    ...(totalTokens !== null ? { total_tokens: totalTokens } : {}),
    ...(frameRecord.known_cost_usd !== undefined ? { known_cost_usd: frameRecord.known_cost_usd } : {}),
    ...(frameRecord.unknown_cli_tokens !== undefined
      ? { unknown_cli_tokens: frameRecord.unknown_cli_tokens }
      : {}),
    ...(frameRecord.cost_knownness !== undefined ? { cost_knownness: frameRecord.cost_knownness } : {}),
  };
}

export function costKnownnessDisplay(knownCostUsd: number, knownness: unknown, unknownTokens: number, t: I18nContextValue["t"] = fallbackContext.t): string {
  const normalized = firstStringFromUnknownValue([knownness, ""]).trim().toLowerCase();
  const knownDisplay = `$${knownCostUsd.toFixed(4)}`;
  if (normalized === "partial" && unknownTokens > 0) {
    return t("react.jobs.value1_known_unknown_cost_value2_tokens", "{value1} known + unknown cost ({value2} tokens)", { value1: String(knownDisplay), value2: String(unknownTokens.toLocaleString()) });
  }
  if (normalized === "unknown" && unknownTokens > 0) {
    return t("react.jobs.unknown_cost_value1_tokens", "Unknown cost ({value1} tokens)", { value1: String(unknownTokens.toLocaleString()) });
  }
  return knownDisplay;
}

export type JobFinalAnswer = { source: string; outputs: Entity };

const FINISHED_STEP_STATUSES = new Set(["completed", "succeeded", "success", "passed"]);

/**
 * A run's final answer, where a user looks first: the workflow's declared
 * outputs when it returns any, else the outputs of the last finished agent
 * step. Without this the answer was only reachable inside step evidence.
 */
export function jobFinalAnswer(detailEntity: Entity, steps: readonly Entity[]): JobFinalAnswer | null {
  const publicOutputs = objectOrEmpty(objectOrEmpty(detailEntity.workflow_result).outputs);
  const declared = Object.keys(publicOutputs).length
    ? publicOutputs
    : objectOrEmpty(objectOrEmpty(objectOrEmpty(detailEntity.workflow_run).result).outputs);
  if (Object.keys(declared).length) return { source: "Workflow outputs", outputs: declared };
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const step = steps[index];
    const agent = firstReadableText(step.agent);
    const status = firstStringFromUnknownValue([step.status, ""]).trim().toLowerCase();
    // Built-in step runners report as agents too (control_flow_step,
    // connector_step, transform_step...); their outputs are bookkeeping such as
    // a stop step's terminal status, not the run's answer.
    if (!agent || agent.endsWith("_step") || !FINISHED_STEP_STATUSES.has(status)) continue;
    // `workflow_data` is the run's whole shared state, not this step's answer.
    const outputs = Object.fromEntries(
      Object.entries(objectOrEmpty(step.outputs)).filter(([key]) => key !== "workflow_data"),
    );
    if (!Object.keys(outputs).length) continue;
    const label = firstReadableText(step.title, step.label, step.display_name, step.scope_id, step.step_id);
    return { source: label ? `From ${label}` : "From the last agent step", outputs };
  }
  return null;
}

export type JobCostTotals = {
  totalCostValue: number;
  totalTokensValue: number;
  unknownCliTokensValue: number;
  totalCostKnownness: unknown;
  totalCostDisplay: string;
};

/**
 * The cost card's totals, read from the same source as its rows. The compact
 * job detail always carries a cost section, but an empty one (no rows, zero
 * totals) when it skipped the ledger read; the rows then come from the
 * cost-breakdown endpoint, and so must the totals. Mixing the two showed a
 * real usage row next to "Total: $0.00 · Tokens: 0".
 */
export function jobCostTotals({
  projected,
  breakdown,
  detailCost,
  rows,
}: {
  projected: Entity;
  breakdown: Entity | undefined;
  detailCost: Entity;
  rows: Entity[];
}, t: I18nContextValue["t"] = fallbackContext.t): JobCostTotals {
  // The placeholder has no rows and nothing counted; a projection that carries
  // totals (a live cost frame, say) stays authoritative even without rows.
  const projectedEmpty =
    !listFromUnknown(projected.rows).length &&
    !(finiteNumberOrNull(projected.total_tokens) ?? 0) &&
    !(finiteNumberOrNull(projected.known_cost_usd) ?? 0) &&
    !(finiteNumberOrNull(projected.total_cost_usd) ?? 0);
  const source = projectedEmpty ? objectOrEmpty(breakdown) : projected;
  const rowsTokens = rows.reduce((sum, row) => sum + (finiteNumberOrNull(row.tokens) ?? 0), 0);
  const totalCostValue =
    finiteNumberOrNull(source.known_cost_usd) ||
    finiteNumberOrNull(source.total_cost_usd) ||
    finiteNumberOrNull(detailCost.known_cost_usd) ||
    finiteNumberOrNull(detailCost.cost_usd) ||
    0;
  const totalTokensValue =
    finiteNumberOrNull(source.total_tokens) || rowsTokens || finiteNumberOrNull(detailCost.tokens_total) || 0;
  const unknownCliTokensValue =
    finiteNumberOrNull(source.unknown_cli_tokens) || finiteNumberOrNull(detailCost.unknown_cli_tokens) || 0;
  const totalCostKnownness = source.cost_knownness || detailCost.cost_knownness || "known";
  const totalCostDisplay =
    firstReadableText(source.total_cost_display) ||
    costKnownnessDisplay(totalCostValue, totalCostKnownness, unknownCliTokensValue, t);
  return { totalCostValue, totalTokensValue, unknownCliTokensValue, totalCostKnownness, totalCostDisplay };
}

export function costRowDisplay(row: Entity, t: I18nContextValue["t"] = fallbackContext.t): string {
  const projected = firstReadableText(row.cost_display);
  if (projected) {
    return projected;
  }
  const knownness = firstStringFromUnknownValue([row.cost_knownness, ""]).trim().toLowerCase();
  if (knownness === "unknown") {
    return t("dashboard.classic.breakdown.unknown_project", "Unknown");
  }
  const value = finiteNumberOrNull(row.known_cost_usd) ?? finiteNumberOrNull(row.cost_usd) ?? 0;
  return `$${value.toFixed(4)}`;
}

export function projectIdFromPayload(payload: unknown): string | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const record = payload as Record<string, unknown>;
  const nestedProject =
    record.project && typeof record.project === "object" && !Array.isArray(record.project)
      ? (record.project as Record<string, unknown>)
      : {};
  const nestedJob =
    record.job && typeof record.job === "object" && !Array.isArray(record.job)
      ? (record.job as Record<string, unknown>)
      : {};
  return (
    safeEntityId(record.current_project_id) ||
    safeEntityId(record.project_id) ||
    safeEntityId(record.projectId) ||
    safeEntityId(record.project) ||
    safeEntityId(nestedJob.project_id) ||
    safeEntityId(nestedJob.projectId) ||
    safeEntityId(nestedJob.project) ||
    safeEntityId(nestedProject.id) ||
    safeEntityId(nestedProject.project_id) ||
    null
  );
}
