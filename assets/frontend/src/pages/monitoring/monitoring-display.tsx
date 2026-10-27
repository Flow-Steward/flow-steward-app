import { useI18n } from "@/lib/i18n/useI18n";
import type { I18nContextValue } from "@/lib/i18n/context";
import { type PropsWithChildren, type ReactNode } from "react";
import { ExternalLink } from "lucide-react";
import { Link } from "react-router";
import { DsBadge, DsCard, type DsBadgeTone } from "@/components/shared/design-system";
import { type Entity, type HealthTone, listFromUnknown, stringValue, shortEntityId, displayValue, numberValue, primitiveMetadataEntries, labelFromKey, queueDeliveryTone } from "./monitoring-data";

const TONE_BADGE: Record<HealthTone, DsBadgeTone> = {
  healthy: "green",
  warning: "orange",
  danger: "danger",
  neutral: "neutral",
};

export function StatusBadge({ status, tone }: Readonly<{ status: string; tone: HealthTone }>) {
  return <DsBadge tone={TONE_BADGE[tone]}>{status}</DsBadge>;
}

export function DetailRow({
  label,
  value,
  title,
  mono = false,
}: Readonly<{ label: string; value: ReactNode; title?: string; mono?: boolean }>) {
  return (
    <div className="fs-detail-row">
      <span>{label}</span>
      <span className={mono ? "fs-detail-value mono" : "fs-detail-value"} title={title}>
        {value}
      </span>
    </div>
  );
}

export function PanelHeading({
  title,
  description,
  level = 2,
}: Readonly<{ title: string; description?: ReactNode; level?: 2 | 3 }>) {
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <div className="fs-panel-heading">
      <div>
        <Heading className="fs-panel-title">{title}</Heading>
        {description ? <p>{description}</p> : null}
      </div>
    </div>
  );
}

export function EmptyState({ children }: PropsWithChildren<object>) {
  return (
    <div className="fs-table-state">
      <strong>{children}</strong>
    </div>
  );
}

function AttentionJob({ job }: Readonly<{ job: Entity }>) {
  const { t } = useI18n();
  const jobId = stringValue(job.job_id);
  const detailUrl = stringValue(job.detail_url, jobId ? `/jobs/${jobId}` : "");
  const attentionLabelText = stringValue(job.attention_label || job.attention_kind, t("monitoring.health.needs_attention", "Needs attention"));
  const attentionLabelKey = stringValue(job.attention_label_key);
  const attentionLabel = attentionLabelKey ? t(attentionLabelKey, attentionLabelText) : attentionLabelText;
  const workflow = stringValue(job.selected_workflow || job.selected_workflow_key, t("react.monitoring.workflow_not_recorded", "Workflow not recorded"));
  const projectName = stringValue(job.project_name, t("react.monitoring.project_not_found", "Project not found"));
  const projectState = stringValue(job.project_state);
  const failureCode = stringValue(job.failure_code);
  const failureMessage = stringValue(job.failure_message || job.last_error || job.error);
  const resolutionHint = stringValue(job.resolution_hint);
  const deadLetteredAt = stringValue(job.dead_lettered_at || job.finished_at || job.started_at);
  const retryCount = numberValue(job.retry_count);
  const retryMaxAttempts = numberValue(job.retry_max_attempts);

  return (
    <div className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4">
      <div className="fs-panel-heading">
        <div>
          <h4 className="fs-panel-title">{attentionLabel}</h4>
          {jobId ? <p>{t("dashboard.context.job_fallback", "Job")} {shortEntityId(jobId)}</p> : null}
        </div>
        {detailUrl ? (
          <Link aria-label={jobId ? t("react.monitoring.open_job_value1", "Open job {value1}", { value1: String(jobId) }) : t("react.monitoring.open_job", "Open job")} className="fs-button" to={detailUrl}>
            <span>{t("react.monitoring.open_job", "Open job")}</span>
            <ExternalLink aria-hidden="true" />
          </Link>
        ) : null}
      </div>
      <div className="fs-detail-rows" data-columns="2">
        <DetailRow label={t("jobs.columns.workflow", "Workflow")} title={workflow} value={workflow} />
        <DetailRow
          label={t("events.columns.project", "Project")}
          title={projectName}
          value={
            <>
              {projectName}
              {projectState ? <span className="fs-ink-tertiary"> ({projectState})</span> : null}
            </>
          }
        />
        {failureCode ? <DetailRow label={t("react.monitoring.failure_code", "Failure code")} mono value={failureCode} /> : null}
        {deadLetteredAt ? <DetailRow label={t("react.monitoring.recorded", "Recorded")} value={deadLetteredAt} /> : null}
        {retryCount !== null || retryMaxAttempts !== null ? (
          <DetailRow label={t("react.monitoring.delivery_retries", "Delivery retries")} value={`${retryCount ?? 0}/${retryMaxAttempts ?? 0}`} />
        ) : null}
      </div>
      {failureMessage ? <p className="fs-ink-danger">{failureMessage}</p> : null}
      {resolutionHint ? <p className="fs-ink-secondary">{resolutionHint}</p> : null}
    </div>
  );
}

export function QueueDeliveryCard({ value }: Readonly<{ value: Entity }>) {
  const { t } = useI18n();
  const tone = queueDeliveryTone(value);
  const statusText = stringValue(value.state_label || value.state, t("react.monitoring.not_reported", "Not reported"));
  const statusKey = stringValue(value.state_label_key);
  const status = statusKey ? t(statusKey, statusText) : statusText;
  const messageText = stringValue(value.state_message || value.attention_message, t("react.monitoring.queue_delivery_has_no_runtime_detail", "Queue delivery has no runtime detail."));
  const messageKey = stringValue(value.state_message ? value.state_message_key : value.attention_message_key);
  const message = messageKey ? t(messageKey, messageText) : messageText;
  const attentionJobs = listFromUnknown(value.attention_jobs);
  const attentionJobCount = numberValue(value.attention_job_count) ?? attentionJobs.length;
  const operatorActionText = stringValue(value.operator_action);
  const operatorActionKey = stringValue(value.operator_action_key);
  const operatorAction = operatorActionKey ? t(operatorActionKey, operatorActionText) : operatorActionText;
  const counts = [
    [t("react.monitoring.queued", "Queued"), value.queued_pending_jobs],
    [t("react.monitoring.retrying", "Retrying"), value.retrying_delivery_jobs],
    [t("react.monitoring.dead_lettered", "Dead-lettered"), value.dead_lettered_jobs],
    [t("react.monitoring.failed_after_delivery", "Failed after delivery"), value.post_claim_execution_failed_jobs],
  ] as const;
  const showing = attentionJobCount > attentionJobs.length
    ? t("react.monitoring.showing_jobs_total", "Showing {shown} of {total} jobs", { shown: attentionJobs.length, total: attentionJobCount })
    : (attentionJobCount === 1 ? t("react.monitoring.showing_one_job", "Showing 1 job") : t("react.monitoring.showing_jobs_count", "Showing {count} jobs", { count: attentionJobs.length }));

  return (
    <DsCard description={message} title={t("react.monitoring.queue_delivery", "Queue delivery")}>
      <div className="fs-detail-rows" data-columns="2">
        <DetailRow label={t("monitoring.table.state", "State")} value={<StatusBadge status={status} tone={tone} />} />
        {counts.map(([label, count]) => (
          <DetailRow key={label} label={label} value={numberValue(count) ?? 0} />
        ))}
      </div>
      {attentionJobs.length ? (
        <div className="fs-panel-group">
          <PanelHeading description={showing} level={3} title={t("react.monitoring.needs_operator_action", "Needs operator action")} />
          {operatorAction ? <p className="fs-ink-secondary">{operatorAction}</p> : null}
          {attentionJobs.map((job, index) => (
            <AttentionJob job={job} key={`${stringValue(job.job_id) || "attention-job"}-${index}`} />
          ))}
        </div>
      ) : null}
    </DsCard>
  );
}

function metadataCountLabel(itemCount: number, t: I18nContextValue["t"]): string {
  if (!itemCount) return "-";
  return itemCount === 1 ? t("react.monitoring.one_metadata_field", "1 field") : t("react.monitoring.metadata_field_count", "Fields: {count}", { count: itemCount });
}

/**
 * A nested value summarizes as its size; its primitive fields stay reachable in
 * the title rather than being dumped recursively into the row.
 */
export function MetadataRow({ metaKey, value }: Readonly<{ metaKey: string; value: unknown }>) {
  const { t } = useI18n();
  if (value && typeof value === "object") {
    const itemCount = Array.isArray(value) ? value.length : Object.keys(value as Entity).length;
    const primitiveEntries = Array.isArray(value) ? [] : primitiveMetadataEntries(value as Entity);
    const title = primitiveEntries.map(([key, nested]) => `${labelFromKey(key, t)}: ${nested}`).join(" · ");
    return <DetailRow label={labelFromKey(metaKey, t)} title={title || undefined} value={metadataCountLabel(itemCount, t)} />;
  }
  const display = displayValue(value);
  return <DetailRow label={labelFromKey(metaKey, t)} title={display} value={display} />;
}
