import { Link } from "react-router";

import { useI18n } from "@/lib/i18n/useI18n";
import { isActionRun, normalizeTimestamp, workflowPath } from "@/pages/jobs/detail/job-detail-core";
import { jobProjectionText } from "@/pages/jobs/detail/job-detail-view-model";

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

export function JobRunBadges({
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
      {selectedWorkflow && isActionRun(canonicalDetail.job) ? (
        <span className="fs-badge green min-w-0 max-w-full" data-testid="job-action-name">
          <span className="break-words">{t("react.workflows.action", "Action:")} {selectedWorkflowLabel}</span>
        </span>
      ) : null}
      {selectedWorkflow && !isActionRun(canonicalDetail.job) ? (
        <Link className="fs-badge green min-w-0 max-w-full" data-testid="job-workflow-link" title={selectedWorkflowLabel !== selectedWorkflow ? t("react.jobs.workflow_id_value1", "Workflow ID: {value1}", { value1: String(selectedWorkflow) }) : undefined} to={workflowPath(selectedWorkflow)}>
          <span className="break-words">{t("project_detail.task.preview.workflow", "Workflow:")} {selectedWorkflowLabel}</span>
        </Link>
      ) : null}
      {selectedWorkflow ? null : <span className="fs-badge" data-testid="job-workflow-missing">{t("react.jobs.workflow_not_recorded", "Workflow not recorded")}</span>}
    </div>
  );
}
