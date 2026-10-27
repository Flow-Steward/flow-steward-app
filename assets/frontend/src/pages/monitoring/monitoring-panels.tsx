import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { Link } from "react-router";
import { DsAlert, DsBadge, DsCard, DsMetricCard, DsTable, DsValueChip, type DsBadgeTone, type DsTableColumn } from "@/components/shared/design-system";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { useUserTimezone } from "@/lib/time/user-timezone";
import { type Entity, type HealthRow, type RuntimeComponents, componentObservationGroups, historyRows, isFreshObservation, measuredCount, serviceVerdicts } from "./monitoring-data";
import { StatusBadge, PanelHeading, EmptyState, QueueDeliveryCard, MetadataRow } from "./monitoring-display";

function readinessLabel(summaryUnavailable: boolean, ready: boolean | null, t: I18nContextValue["t"]): string {
  if (summaryUnavailable || ready === null) return t("react.monitoring.unknown_lowercase", "unknown");
  return ready ? t("react.monitoring.yes", "yes") : t("react.monitoring.no", "no");
}

function channelLabel(state: RuntimeComponents["channel"]["state"], t: I18nContextValue["t"]): string {
  const labels = {
    connected: t("react.monitoring.channel_connected", "connected"),
    disconnected: t("react.monitoring.channel_disconnected", "disconnected"),
    stale: t("react.monitoring.channel_stale", "stale"),
    missing: t("react.monitoring.channel_missing", "missing"),
    not_observed: t("react.monitoring.channel_not_observed", "not_observed"),
    unavailable: t("react.monitoring.channel_unavailable", "unavailable"),
    conflicting: t("react.monitoring.channel_conflicting", "conflicting"),
  };
  return labels[state];
}

function readinessTone(summaryUnavailable: boolean, ready: boolean | null): DsBadgeTone {
  if (summaryUnavailable || ready === null) return "neutral";
  return ready ? "green" : "danger";
}


/**
 * Visible on every tab. The "Ready: yes" and "Failed checks: n" strings are read
 * verbatim by tests/ui/test_dashboard_pages.py, so each badge keeps its label
 * and value as one contiguous text.
 */
export function MonitoringStatusStrip({
  components,
  meta,
  ready,
  summaryUnavailable,
  totals,
}: Readonly<{
  components: RuntimeComponents | null;
  meta: Entity;
  ready: boolean | null;
  summaryUnavailable: boolean;
  totals: Entity;
}>) {
  const { t } = useI18n();
  const unknown = t("react.monitoring.unknown_lowercase", "unknown");
  const failedCount = measuredCount(totals.checks_failed);
  const checksOk = measuredCount(totals.checks_ok);
  const checksTotal = measuredCount(totals.checks_total);
  const checksLabel = summaryUnavailable || checksOk === null || checksTotal === null
    ? unknown
    : `${checksOk} / ${checksTotal}`;
  const failedLabel = summaryUnavailable ? unknown : failedCount ?? unknown;
  const counts = components?.available ? components.counts : null;
  const servicesLabel = counts?.roles_total ?? unknown;
  const version = firstStringFromUnknownValue([meta.version, meta.app_version, meta.release, unknown]);
  const runtimeTarget = firstStringFromUnknownValue([meta.extension_runtime_target, unknown]);
  return (
    <div aria-label={t("react.monitoring.system_status", "System status")} className="fs-monitoring-status" role="group">
      <DsBadge tone={readinessTone(summaryUnavailable, ready)}>
        {t("react.monitoring.ready", "Ready:")} {readinessLabel(summaryUnavailable, ready, t)}
      </DsBadge>
      <DsBadge>{t("react.monitoring.checks", "Checks:")} {checksLabel}</DsBadge>
      <DsBadge tone={!summaryUnavailable && failedCount !== null && failedCount > 0 ? "danger" : "neutral"}>
        {t("react.monitoring.failed_checks", "Failed checks:")} {failedLabel}
      </DsBadge>
      <DsBadge>{t("react.monitoring.services", "Services:")} {servicesLabel}</DsBadge>
      <DsBadge>{t("react.monitoring.running_instances", "Running instances:")} {counts?.instances_fresh ?? unknown}</DsBadge>
      <DsBadge tone={counts && counts.instances_stale > 0 ? "orange" : "neutral"}>
        {t("react.monitoring.not_reporting", "Not reporting:")} {counts?.instances_stale ?? unknown}
      </DsBadge>
      <DsBadge>{t("react.monitoring.dependencies", "Dependencies:")} {counts?.dependencies_total ?? unknown}</DsBadge>
      <DsBadge>{t("react.monitoring.monitoring_channel", "Monitoring channel:")} {components?.channel.state ? channelLabel(components.channel.state, t) : unknown}</DsBadge>
      <DsBadge>{t("react.monitoring.replica_completeness", "Replica completeness:")} {components?.available && components.completeness !== null ? (components.completeness ? t("react.monitoring.complete", "complete") : t("react.monitoring.incomplete", "incomplete")) : unknown}</DsBadge>
      <span className="fs-monitoring-reference">
        {t("registry.data_contracts.column.version", "Version")} <DsValueChip>{version}</DsValueChip>
      </span>
      <span className="fs-monitoring-reference">
        {t("react.monitoring.runtime_target", "Runtime target")} <DsValueChip>{runtimeTarget}</DsValueChip>
      </span>
    </div>
  );
}

function componentHealthMetric(components: RuntimeComponents | null, summaryUnavailable: boolean, t: I18nContextValue["t"] = fallbackContext.t): {
  figure: string; accessibleValue: string; description: string; unavailable: boolean;
} {
  const count = components?.available ? components.counts?.instances_evidenced_healthy : null;
  if (summaryUnavailable || count === null || count === undefined) {
    return { figure: t("dashboard.classic.breakdown.unknown_project", "Unknown"), accessibleValue: t("react.monitoring.unknown_lowercase", "unknown"), description: t("react.monitoring.component_health_observations_unavailable", "Component health observations unavailable"), unavailable: true };
  }
  const measured = components?.rows.some((row) => row.observation_kind === "process" &&
    isFreshObservation(row) && row.health.freshness === "fresh" &&
    (row.health.state === "healthy" || row.health.state === "unhealthy"));
  if (!measured) {
    return { figure: t("react.monitoring.not_measured", "Not measured"), accessibleValue: t("react.monitoring.not_measured_2", "not measured"),
      description: t("react.monitoring.no_running_process_reported_a_health_check", "No running process reported a health check"), unavailable: true };
  }
  const running = components?.counts?.instances_fresh ?? count;
  return { figure: t("react.monitoring.value1_of_value2", "{value1} of {value2}", { value1: String(count), value2: String(running) }), accessibleValue: t("react.monitoring.value1_of_value2", "{value1} of {value2}", { value1: String(count), value2: String(running) }),
    description: t("react.monitoring.running_instances_whose_latest_health_check_passed", "Running instances whose latest health check passed"), unavailable: false };
}

export function MonitoringOverview({
  components,
  checks,
  checksFailed,
  eventCount,
  loading,
  meta,
  summaryUnavailable,
}: Readonly<{
  checks: HealthRow[];
  checksFailed: number;
  components: RuntimeComponents | null;
  eventCount: number;
  loading: boolean;
  meta: Entity;
  summaryUnavailable: boolean;
}>) {
  const { t } = useI18n();
  const healthMetric = componentHealthMetric(components, summaryUnavailable, t);
  const queueDelivery = meta.queue_delivery;
  const metadataEntries = Object.entries(meta).filter(([key]) => key !== "queue_delivery");
  return (
    <>
      <div className="fs-metric-grid" data-columns="3">
        <DsMetricCard
          ariaLabel={t("react.monitoring.healthy_instances_value1_open_services", "Healthy instances: {value1}. Open services", { value1: String(healthMetric.accessibleValue) })}
          figure={healthMetric.figure}
          label={t("react.monitoring.healthy_instances", "Healthy instances")}
          meta={healthMetric.description}
          to="/monitoring?tab=services"
          unavailable={healthMetric.unavailable}
        />
        <DsMetricCard
          ariaLabel={t("react.monitoring.readiness_checks_reporting_value1_open_services", "Readiness checks reporting: {value1}. Open services", { value1: String(checks.length) })}
          figure={String(checks.length)}
          label={t("react.monitoring.readiness_checks", "Readiness checks")}
          meta={checksFailed ? `${checksFailed} failing` : undefined}
          to="/monitoring?tab=services"
        />
        <DsMetricCard
          ariaLabel={t("react.monitoring.event_records_available_value1_open_events", "Event records available: {value1}. Open events", { value1: String(eventCount) })}
          figure={String(eventCount)}
          label={t("react.monitoring.event_records", "Event records")}
          to="/monitoring?tab=events"
        />
      </div>
      {queueDelivery && typeof queueDelivery === "object" && !Array.isArray(queueDelivery) ? (
        <QueueDeliveryCard value={queueDelivery as Entity} />
      ) : null}
      <DsCard title={t("events.fields.metadata", "Metadata")}>
        {metadataEntries.length ? (
          <div className="fs-detail-rows" data-columns="2">
            {metadataEntries.map(([key, value]) => (
              <MetadataRow key={key} metaKey={key} value={value} />
            ))}
          </div>
        ) : null}
        {!loading && !metadataEntries.length ? <p className="fs-empty-copy">{t("react.monitoring.no_metadata_returned", "No metadata returned.")}</p> : null}
      </DsCard>
    </>
  );
}

// Operator-critical details wrap; diagnostic provenance is secondary and
// reachable through a native keyboard-accessible disclosure, not hover alone.


function getHealthColumns(t: I18nContextValue["t"] = fallbackContext.t): ReadonlyArray<DsTableColumn<HealthRow>> {
  return [
  {
    key: "service",
    header: t("monitoring.table.service", "Service"),
    width: 220,
    cell: (row) => (
      <span className="fs-user-copy">
        <strong>{row.title}</strong>
        {row.subtitle ? <span>{row.subtitle}</span> : null}
      </span>
    ),
  },
  { key: "status", header: t("account_settings.card.status", "Status"), cell: (row) => <StatusBadge status={row.status} tone={row.tone} /> },
  {
    key: "detail", header: t("events.fields.detail", "Detail"), width: 360,
    cell: (row) => row.jobStatusFilter === "stuck" ? (
      <span className="fs-user-copy">
        {row.detail ? <span>{row.detail}</span> : null}
        <Link to="/jobs?status=stuck">{t("react.monitoring.view_persisted_stuck_jobs", "View persisted stuck jobs")}</Link>
      </span>
    ) : row.detail || null,
  },
  {
    key: "meta",
    header: t("react.monitoring.diagnostics", "Diagnostics"),
    width: 240,
    cell: (row) => row.meta ? (
      <details className="fs-disclosure" aria-label={t("react.monitoring.value1_observation_diagnostics", "{value1} observation diagnostics", { value1: String(row.title) })}>
        <summary>{t("react.monitoring.observation_diagnostics", "Observation diagnostics")}</summary>
        <p>{row.meta}</p>
      </details>
    ) : null,
  },
];
}

function HealthTableGroup({
  title,
  description,
  rows,
  emptyMessage,
  loading = false,
}: Readonly<{
  title: string;
  description: string;
  rows: HealthRow[];
  emptyMessage?: string;
  loading?: boolean;
}>) {
  const { t } = useI18n();
  return (
    <div className="fs-panel-group">
      <PanelHeading description={description} title={title} />
      <article className="fs-card fs-table-card fs-monitoring-health-table">
        <DsTable
          ariaLabel={title}
          busy={loading}
          columns={getHealthColumns(t)}
          rowKey={(row) => `${title}:${row.key}`}
          rows={rows}
          state={!loading && !rows.length && emptyMessage ? <EmptyState>{emptyMessage}</EmptyState> : null}
        />
      </article>
    </div>
  );
}

export function MonitoringServices({
  components,
  checks,
  loading,
}: Readonly<{
  checks: HealthRow[];
  components: RuntimeComponents | null;
  loading: boolean;
}>) {
  const { t } = useI18n();
  const timezone = useUserTimezone();
  const verdicts = serviceVerdicts(components, timezone, t);
  const groups = componentObservationGroups(
    components ? { ...components, rows: components.rows.filter((row) => !row.superseded) } : null,
    timezone, t,
  );
  const current = [...groups.freshProcesses, ...groups.pastProcesses, ...groups.probes, ...groups.channels];
  const history = historyRows(components, timezone, t);
  return (
    <>
      {!loading && !components?.available ? <DsAlert title={t("react.monitoring.component_observations_unavailable", "Component observations unavailable")}>{t("react.monitoring.health_and_replica_counts_are_unknown_no_discovery_fallback_is_used", "Health and replica counts are unknown. No discovery fallback is used.")}</DsAlert> : null}
      <HealthTableGroup
        description={t("react.monitoring.one_row_per_role_is_it_running_and_healthy_right_now_problems_come_first", "One row per role: is it running and healthy right now. Problems come first.")}
        rows={verdicts.services}
        title={t("monitoring.card.services.title", "Services")}
        loading={loading}
        emptyMessage={t("react.monitoring.no_service_has_reported_yet", "No service has reported yet.")}
      />
      <HealthTableGroup
        description={t("react.monitoring.shared_dependencies_from_the_checks_that_reach_them", "Shared dependencies, from the checks that reach them.")}
        rows={verdicts.dependencies}
        title={t("react.monitoring.dependencies_2", "Dependencies")}
        loading={loading}
        emptyMessage={t("react.monitoring.no_dependency_checks_reported", "No dependency checks reported.")}
      />
      <HealthTableGroup
        description={t("react.monitoring.checks_behind_the_readiness_endpoint", "Checks behind the readiness endpoint.")}
        emptyMessage={t("react.monitoring.no_readiness_checks_reported", "No readiness checks reported.")}
        loading={loading}
        rows={checks}
        title={t("react.monitoring.readiness_checks", "Readiness checks")}
      />
      <details className="fs-disclosure fs-panel-group">
        <summary>{t("react.monitoring.current_observations", "Current observations (")}{current.length})</summary>
        <HealthTableGroup
          description={t("react.monitoring.every_current_report_behind_the_verdicts_above_for_diagnosis", "Every current report behind the verdicts above, for diagnosis.")}
          rows={current}
          title={t("react.monitoring.current_observations_2", "Current observations")}
          loading={loading}
          emptyMessage={t("react.monitoring.no_current_observations", "No current observations.")}
        />
      </details>
      <details className="fs-disclosure fs-panel-group">
        <summary>{t("react.monitoring.history_past_process_starts", "History: past process starts (")}{history.length})</summary>
        <HealthTableGroup
          description={t("react.monitoring.processes_replaced_by_a_newer_start_or_that_reported_stopping_kept_for_diagnosis_they_do_not_affect_any_status", "Processes replaced by a newer start or that reported stopping. Kept for diagnosis; they do not affect any status above.")}
          rows={history}
          title={t("react.projects.history", "History")}
          loading={loading}
          emptyMessage={t("react.monitoring.no_past_process_starts", "No past process starts.")}
        />
      </details>
    </>
  );
}

export function MonitoringQueryStatus({
  loading,
  error,
}: Readonly<{ loading: boolean; error: unknown }>) {
  const { t } = useI18n();
  if (loading) {
    return <DsAlert title={t("react.monitoring.loading_monitoring_summary", "Loading monitoring summary…")} />;
  }
  if (!error) return null;
  return (
    <DsAlert title={t("react.monitoring.monitoring_summary_could_not_be_loaded", "Monitoring summary could not be loaded")} tone="danger">
      {error instanceof Error ? error.message : t("react.monitoring.failed_to_load_monitoring_summary", "Failed to load monitoring summary.")}
    </DsAlert>
  );
}
