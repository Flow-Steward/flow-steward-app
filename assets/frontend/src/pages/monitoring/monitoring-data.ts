
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { z } from "@/lib/validation/zod";
import { formatUserDateTime, browserTimezone } from "@/lib/time/user-timezone";

const ageSchema = z.number().nonnegative();
const countSchema = ageSchema.int();
const timestampSchema = z.iso.datetime({ offset: true });
const dimensionSchema = z.object({
  state: z.enum(["not_observed", "ready", "not_ready", "healthy", "unhealthy"]),
  checked_at: timestampSchema.nullable(),
  evidence: z.string().nullable(),
  stale_seconds: ageSchema.positive().nullable(),
  age_seconds: ageSchema.nullable(),
  freshness: z.enum(["fresh", "stale", "not_observed"]),
}).refine((check) => check.state === "not_observed"
  ? check.checked_at === null && check.evidence === null && check.stale_seconds === null && check.freshness === "not_observed"
  : check.checked_at !== null && Boolean(check.evidence) && check.freshness !== "not_observed");
const componentsSchema = z.object({
  installation_id: z.string().min(1),
  deployment_profile: z.enum(["compact", "scalable"]),
  available: z.boolean(),
  channel: z.object({
    state: z.enum(["connected", "disconnected", "stale", "missing", "not_observed", "unavailable", "conflicting"]),
    observed_at: timestampSchema.nullable().optional(),
    age_seconds: ageSchema.nullable().optional(),
  }),
  rows: z.array(z.object({
    service_type: z.string().min(1), instance_id: z.uuid(), incarnation_id: z.uuid(), source: z.string().min(1),
    observation_kind: z.enum(["process", "dependency", "role_probe", "channel"]),
    observed_at: timestampSchema, received_at: timestampSchema,
    report_age_seconds: ageSchema, receive_age_seconds: ageSchema, effective_age_seconds: ageSchema,
    freshness: z.enum(["fresh", "stale"]),
    source_age_seconds: ageSchema.nullable(), source_stale_seconds: ageSchema.positive().nullable(),
    source_availability: z.enum(["observed", "missing", "expired", "retired"]),
    source_freshness: z.enum(["fresh", "stale", "missing", "retired"]),
    lifecycle: z.string(), readiness: dimensionSchema, health: dimensionSchema,
    consumer_state: z.string().nullable(), conflicting: z.boolean(),
    started_at: timestampSchema.nullable().optional(),
    superseded: z.boolean().optional(),
  })),
  expected_gaps: z.array(z.object({
    service_type: z.string().min(1), desired_count: countSchema.nullable(), observed_count: countSchema,
    observation_kind: z.enum(["process", "dependency"]),
    reason: z.enum(["missing", "stale", "replica_target_unobserved", "not_observed"]),
  })),
  counts: z.object({
    roles_total: countSchema, roles_observed: countSchema, instances_total: countSchema,
    instances_fresh: countSchema, instances_stale: countSchema, instances_ready: countSchema,
    instances_evidenced_healthy: countSchema, dependencies_total: countSchema,
    instances_superseded: countSchema.optional(),
  }).nullable(),
  completeness: z.boolean().nullable(),
}).refine((value) => {
  if (!value.available) return value.counts === null;
  const counts = value.counts;
  return counts !== null && counts.roles_observed <= counts.roles_total &&
    counts.instances_fresh + counts.instances_stale === counts.instances_total &&
    counts.instances_ready <= counts.instances_fresh && counts.instances_evidenced_healthy <= counts.instances_fresh;
});

export type RuntimeComponents = z.infer<typeof componentsSchema>;

export function isFreshObservation(row: RuntimeComponents["rows"][number]): boolean {
  return row.freshness === "fresh" && row.source_freshness === "fresh";
}

type ComponentObservationGroups = Record<"freshProcesses" | "pastProcesses" | "probes" | "channels", HealthRow[]>;

export function componentObservationGroups(components: RuntimeComponents | null, timezone: string, t: I18nContextValue["t"] = fallbackContext.t): ComponentObservationGroups {
  const groups: Record<"freshProcesses" | "pastProcesses" | "probes" | "channels", RuntimeComponents["rows"]> = {
    freshProcesses: [], pastProcesses: [], probes: [], channels: [],
  };
  if (components?.available) {
    for (const row of components.rows) {
      if (row.observation_kind === "channel") groups.channels.push(row);
      else if (row.observation_kind !== "process") groups.probes.push(row);
      else if (isFreshObservation(row)) groups.freshProcesses.push(row);
      else groups.pastProcesses.push(row);
    }
  }
  const project = (rows: RuntimeComponents["rows"]) => componentRows(components ? { ...components, rows } : null, timezone, t);
  return {
    freshProcesses: project(groups.freshProcesses), pastProcesses: project(groups.pastProcesses),
    probes: project(groups.probes), channels: project(groups.channels),
  };
}

export function normalizeComponents(value: unknown): RuntimeComponents | null {
  const parsed = componentsSchema.safeParse(value);
  return parsed.success ? parsed.data : null;
}

function observationAge(seconds: number | null, t: I18nContextValue["t"]): string {
  return seconds === null ? t("react.monitoring.not_observed", "Not observed") : `${Math.round(seconds)}s`;
}

function checkDescription(check: RuntimeComponents["rows"][number]["health"], timezone: string, t: I18nContextValue["t"]): string {
  if (check.state === "not_observed") return t("react.monitoring.check_not_observed_help", "Not checked — no health/readiness evidence reported");
  return t("react.monitoring.check_evidence_detail", "{state} ({freshness}; checked {checked}; age {age}; evidence {evidence})", {
    state: labelFromKey(check.state, t), freshness: labelFromKey(check.freshness, t).toLowerCase(),
    checked: formatUserDateTime(check.checked_at, timezone), age: observationAge(check.age_seconds, t),
    evidence: check.evidence ?? t("react.monitoring.none_lowercase", "none"),
  });
}

function measurementSummary(check: RuntimeComponents["rows"][number]["health"], t: I18nContextValue["t"] = fallbackContext.t): string {
  if (check.state === "not_observed") return t("react.monitoring.not_measured", "Not measured");
  return `${labelFromKey(check.state, t)} (${labelFromKey(check.freshness, t)})`;
}

export function componentRows(components: RuntimeComponents | null, timezone = browserTimezone(), t: I18nContextValue["t"] = fallbackContext.t): HealthRow[] {
  if (!components?.available) return [];
  return components.rows.map((row) => {
    const fresh = isFreshObservation(row);
    const failed = fresh && ((row.health.freshness === "fresh" && row.health.state === "unhealthy") ||
      (row.readiness.freshness === "fresh" && row.readiness.state === "not_ready"));
    const healthy = fresh && row.health.freshness === "fresh" && row.health.state === "healthy";
    const staleCheck = row.readiness.freshness === "stale" || row.health.freshness === "stale";
    const status = row.conflicting ? t("react.monitoring.conflicting", "Conflicting") : !fresh ? labelFromKey(row.source_freshness === "fresh" ? row.freshness : row.source_freshness, t) : failed ? t("react.monitoring.failed", "Failed") : staleCheck ? t("react.monitoring.cached_check_stale", "Cached check stale") : healthy ? t("react.monitoring.healthy", "Healthy") : t("react.monitoring.health_not_measured", "Health not measured");
    return {
      key: JSON.stringify([row.service_type, row.instance_id, row.incarnation_id, row.source]),
      title: serviceLabel(row.service_type, t),
      subtitle: `${labelFromKey(row.observation_kind, t)} · ${row.source} · ${shortEntityId(row.instance_id)}${row.started_at ? t("react.monitoring.started_value1", " · started {value1}", { value1: String(formatUserDateTime(row.started_at, timezone)) }) : ""}`,
      status,
      tone: row.conflicting || !fresh ? "warning" : failed ? "danger" : staleCheck ? "warning" : healthy ? "healthy" : "neutral",
      detail: t("react.monitoring.last_heartbeat_value1_value2_report_age_value3_lifecycle_value4_readiness_value5_health_value6_value7", "Last heartbeat: {value1} ({value2}) · Report age: {value3} · Lifecycle: {value4} · Readiness: {value5} · Health: {value6}{value7}", { value1: String(formatUserDateTime(row.observed_at, timezone)), value2: String(timezone), value3: String(observationAge(row.report_age_seconds, t)), value4: String(labelFromKey(row.lifecycle, t)), value5: String(measurementSummary(row.readiness, t)), value6: String(measurementSummary(row.health, t)), value7: String(row.consumer_state ? t("react.monitoring.consumer_detail", " · Consumer: {state}", { state: labelFromKey(row.consumer_state, t) }) : "") }),
      meta: t("react.monitoring.observation_diagnostics_detail", "Readiness: {readiness} · Health: {health} · Receive age: {receiveAge} · Source age: {sourceAge} · Instance: {instance} · Incarnation: {incarnation} · Source: {source}", { readiness: checkDescription(row.readiness, timezone, t), health: checkDescription(row.health, timezone, t), receiveAge: observationAge(row.receive_age_seconds, t), sourceAge: observationAge(row.source_age_seconds, t), instance: row.instance_id, incarnation: row.incarnation_id, source: row.source }),
    };
  });
}

export function componentGapRows(components: RuntimeComponents | null, t: I18nContextValue["t"] = fallbackContext.t): HealthRow[] {
  if (!components?.available) return [];
  return components.expected_gaps.map((gap) => ({
    key: `${gap.observation_kind}:${gap.service_type}`, title: serviceLabel(gap.service_type, t),
    subtitle: gap.observation_kind === "dependency" ? t("react.monitoring.dependency_observation", "Dependency observation") : t("react.monitoring.expected_role", "Expected role"),
    status: labelFromKey(gap.reason, t), tone: gap.reason === "not_observed" ? "neutral" : "warning",
    detail: t("react.monitoring.desired_value1_observed_value2", "Desired: {value1} · Observed: {value2}", { value1: String(gap.desired_count ?? t("react.monitoring.unknown_lowercase", "unknown")), value2: String(gap.observed_count) }),
  }));
}

export type Entity = Record<string, unknown>;
export type OrphanStorageRow = Entity & {
  backend: string;
  project_name?: string;
  project_id?: string;
  storage_scope?: string;
};
export type MonitoringTabKey = "overview" | "processes" | "events" | "services" | "storage";
export type HealthTone = "healthy" | "warning" | "danger" | "neutral";

const MONITORING_TABS: MonitoringTabKey[] = ["overview", "processes", "events", "services", "storage"];
const MONITORING_TAB_SET = new Set<string>(MONITORING_TABS);

export type HealthRow = {
  key: string;
  title: string;
  subtitle?: string;
  status: string;
  detail?: string;
  meta?: string;
  jobStatusFilter?: "stuck";
  tone: HealthTone;
};

export function objectOrEmpty(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

export function entityEntries(value: unknown): Array<[string, Entity]> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return [];
  }
  return Object.entries(value as Entity).map(([key, candidate]) => [key, objectOrEmpty(candidate)]);
}

export function listFromUnknown(value: unknown): Entity[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is Entity => Boolean(item) && typeof item === "object");
}

export function normalizeMonitoringTab(value: string): MonitoringTabKey {
  const normalized = value.trim().toLowerCase();
  if (MONITORING_TAB_SET.has(normalized)) {
    return normalized as MonitoringTabKey;
  }
  return "overview";
}

export function stringValue(value: unknown, fallback = ""): string {
  const normalized = stringFromUnknownValue(value ?? "").trim();
  return normalized || fallback;
}

export function shortEntityId(value: unknown): string {
  const normalized = stringValue(value);
  if (normalized.length <= 16) {
    return normalized;
  }
  return `${normalized.slice(0, 8)}...${normalized.slice(-4)}`;
}

export function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return stringFromUnknownValue(value);
    }
  }
  return stringFromUnknownValue(value);
}

export function numberValue(value: unknown): number | null {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

export function measuredCount(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

export function booleanValue(value: unknown): boolean | null {
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["1", "true", "yes", "on", "ok", "healthy", "running", "available", "enabled"].includes(normalized)) {
      return true;
    }
    if (["0", "false", "no", "off", "failed", "unhealthy", "unavailable", "disabled"].includes(normalized)) {
      return false;
    }
  }
  return null;
}

export function primitiveMetadataEntries(value: Entity, limit = 4): Array<[string, string]> {
  return Object.entries(value)
    .filter(([, nestedValue]) => nestedValue === null || typeof nestedValue !== "object")
    .slice(0, limit)
    .map(([key, nestedValue]) => [key, displayValue(nestedValue)]);
}

export function labelFromKey(value: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const labels: Record<string, string> = {
    not_observed: t("react.monitoring.state_not_observed", "Not Observed"),
    ready: t("react.monitoring.state_ready", "Ready"),
    not_ready: t("react.monitoring.state_not_ready", "Not Ready"),
    healthy: t("react.monitoring.state_healthy", "Healthy"),
    unhealthy: t("react.monitoring.state_unhealthy", "Unhealthy"),
    fresh: t("react.monitoring.state_fresh", "Fresh"),
    stale: t("react.monitoring.state_stale", "Stale"),
    missing: t("react.monitoring.state_missing", "Missing"),
    expired: t("react.monitoring.state_expired", "Expired"),
    retired: t("react.monitoring.state_retired", "Retired"),
    process: t("react.monitoring.state_process", "Process"),
    dependency: t("react.monitoring.state_dependency", "Dependency"),
    role_probe: t("react.monitoring.state_role_probe", "Role Probe"),
    channel: t("react.monitoring.state_channel", "Channel"),
    running: t("react.monitoring.state_running", "Running"),
    stopped: t("react.monitoring.state_stopped", "Stopped"),
    replica_target_unobserved: t("react.monitoring.state_replica_target_unobserved", "Replica Target Unobserved"),
    stuck_jobs: t("react.monitoring.state_stuck_jobs", "Stuck Jobs"),
    idle: t("react.monitoring.state_idle", "Idle"),
    active: t("react.monitoring.state_active", "Active"),
    disabled: t("react.monitoring.state_disabled", "Disabled"),
    enabled: t("react.monitoring.state_enabled", "Enabled"),
    connected: t("react.monitoring.state_connected", "Connected"),
    disconnected: t("react.monitoring.state_disconnected", "Disconnected"),
    unavailable: t("react.monitoring.state_unavailable", "Unavailable"),
  };
  if (labels[value]) return labels[value];
  return value
    .replaceAll(/[_-]+/g, " ")
    .replaceAll(/\s+/g, " ")
    .trim()
    .replaceAll(/\b\w/g, (letter) => letter.toUpperCase());
}

export function toneFromValue(value: unknown): HealthTone {
  const normalized = stringFromUnknownValue(value ?? "").trim().toLowerCase();
  if (["green", "healthy", "ok", "passing", "running", "available", "ready", "enabled"].includes(normalized)) {
    return "healthy";
  }
  if (["red", "error", "failed", "failing", "unhealthy", "missing", "down", "not_ready"].includes(normalized)) {
    return "danger";
  }
  if (["amber", "yellow", "warning", "warn", "degraded", "attention"].includes(normalized)) {
    return "warning";
  }
  return "neutral";
}

export function queueDeliveryTone(value: Entity): HealthTone {
  const state = stringValue(value.attention_state || value.state).toLowerCase();
  const requiresIntervention = booleanValue(value.requires_intervention);
  if (requiresIntervention === true || state.includes("dead") || state.includes("failed")) {
    return "danger";
  }
  if (state.includes("retry")) {
    return "warning";
  }
  if (state.includes("queued")) {
    return "healthy";
  }
  return "neutral";
}


export function checkTone(ok: boolean | null, fallbackTone: HealthTone): HealthTone {
  if (ok === true) return "healthy";
  if (ok === false) return "danger";
  return fallbackTone;
}

export function checkStatus(ok: boolean | null, tone: HealthTone, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (ok === true) return t("react.monitoring.passing", "Passing");
  if (ok === false) return t("react.monitoring.failing", "Failing");
  return tone === "neutral" ? t("react.monitoring.not_reported", "Not reported") : t("react.monitoring.needs_review", "Needs review");
}


export function normalizeCheckRows(checksPayload: unknown, t: I18nContextValue["t"] = fallbackContext.t): HealthRow[] {
  return entityEntries(checksPayload).map(([checkKey, check]) => {
    const ok = booleanValue(check.ok);
    const fallbackTone = toneFromValue(check.status || check.state);
    const tone = checkTone(ok, fallbackTone);
    const status = checkStatus(ok, tone, t);
    const latency = numberValue(check.latency_ms);
    const detail =
      stringValue(check.error) ||
      stringValue(check.message) ||
      stringValue(check.detail) ||
      (ok === true ? t("react.monitoring.check_passing_detail", "Check is passing.") : t("react.monitoring.check_no_detail", "Check has no detail reported."));

    return {
      key: checkKey,
      title: labelFromKey(checkKey, t),
      jobStatusFilter: checkKey === "stuck_jobs" ? "stuck" : undefined,
      status,
      detail,
      meta: latency === null ? undefined : `${latency} ms`,
      tone,
    };
  });
}

export function effectiveReadiness(reportedReady: boolean, checks: HealthRow[]): boolean {
  return reportedReady && !checks.some((check) => check.tone === "danger");
}


/** One label-left, value-right row of the published Details anatomy. */
export function unknownRows(value: unknown): Entity[] {
  return Array.isArray(value) ? value.filter((item): item is Entity => Boolean(item) && typeof item === "object") : [];
}

export function orphanStorageScope(row: OrphanStorageRow): string {
  return firstStringFromUnknownValue([row.storage_scope, row.project_id, row.project_name, ""]);
}

export function orphanStorageTitle(row: OrphanStorageRow, t: I18nContextValue["t"] = fallbackContext.t): string {
  return firstStringFromUnknownValue([row.project_name, row.storage_scope, row.project_id, t("react.monitoring.unknown_storage_scope", "Unknown storage scope")]);
}

export function orphanCleanupSupported(row: OrphanStorageRow): boolean {
  const explicit = booleanValue(row.cleanup_supported);
  if (explicit !== null) {
    return explicit;
  }
  return stringValue(row.project_state).toLowerCase() === "missing";
}

export function orphanStorageDetailEntries(row: OrphanStorageRow): Array<[string, string]> {
  const hiddenKeys = new Set([
    "backend",
    "project_name",
    "cleanup_confirm",
    "cleanup_supported",
    "resolution_hint",
  ]);
  return Object.entries(row)
    .filter(([key]) => !hiddenKeys.has(key))
    .map(([key, value]) => [key, displayValue(value)]);
}

export const MONITORING_TAB_ITEMS: Array<{ key: MonitoringTabKey; label: string }> = getMonitoringTabItems();

export function getMonitoringTabItems(t: I18nContextValue["t"] = fallbackContext.t): Array<{ key: MonitoringTabKey; label: string }> {
  return [
  { key: "overview", label: t("react.projects.overview", "Overview") },
  { key: "processes", label: t("settings.watchdog.processes_link", "Processes") },
  { key: "events", label: t("monitoring.card.events.title", "Events") },
  { key: "services", label: t("monitoring.card.services.title", "Services") },
  { key: "storage", label: t("react.monitoring.storage", "Storage") },
];
}

/** What an operator calls each role and dependency; the raw key stays in diagnostics. */


function getServiceLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  app: t("react.monitoring.web_app", "Web app"),
  control: t("react.monitoring.control_plane", "Control plane"),
  worker: t("monitoring.check.label.worker_manager", "Workers"),
  installer: t("react.monitoring.extension_installer", "Extension installer"),
  mcp: t("react.monitoring.mcp_server", "MCP server"),
  router: t("react.monitoring.router", "Router"),
  postgres: t("registry.index.backend.postgresql", "PostgreSQL"),
  rabbitmq: t("monitoring.check.label.rabbitmq", "RabbitMQ"),
};
}

export function serviceLabel(serviceType: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  return getServiceLabels(t)[serviceType] ?? labelFromKey(serviceType, t);
}



function getEvidenceLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  self_report: t("react.monitoring.the_process_s_own_report", "the process's own report"),
  consumer_state: t("react.monitoring.its_queue_consumer", "its queue consumer"),
  dependency_probe: t("react.monitoring.a_connection_probe", "a connection probe"),
  role_probe: t("react.monitoring.an_endpoint_probe", "an endpoint probe"),
  broker_connection: t("react.monitoring.the_broker_connection", "the broker connection"),
};
}

function evidenceLabel(evidence: string | null, t: I18nContextValue["t"] = fallbackContext.t): string {
  return evidence ? getEvidenceLabels(t)[evidence] ?? labelFromKey(evidence, t) : t("react.monitoring.no_check", "no check");
}

type ComponentRow = RuntimeComponents["rows"][number];

function isCurrent(row: ComponentRow): boolean {
  return !row.superseded;
}

function healthOf(rows: ComponentRow[]): { state: "healthy" | "unhealthy" | "unmeasured"; evidence: string | null; age: number | null } {
  const fresh = rows.filter((row) => isFreshObservation(row) && row.health.freshness === "fresh" && row.health.state !== "not_observed");
  const failing = fresh.find((row) => row.health.state === "unhealthy");
  if (failing) return { state: "unhealthy", evidence: failing.health.evidence, age: failing.health.age_seconds };
  const healthy = fresh.find((row) => row.health.state === "healthy");
  if (healthy) return { state: "healthy", evidence: healthy.health.evidence, age: healthy.health.age_seconds };
  return { state: "unmeasured", evidence: null, age: null };
}

function ageText(seconds: number | null, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (seconds === null) return "";
  if (seconds < 90) return t("react.monitoring.value1_s_ago", "{value1}s ago", { value1: String(Math.round(seconds)) });
  if (seconds < 5400) return t("react.monitoring.value1_min_ago", "{value1} min ago", { value1: String(Math.round(seconds / 60)) });
  return t("react.monitoring.value1_h_ago", "{value1} h ago", { value1: String(Math.round(seconds / 3600)) });
}

const TONE_ORDER: Record<HealthTone, number> = { danger: 0, warning: 1, neutral: 2, healthy: 3 };

function verdictRow(
  key: string,
  title: string,
  rows: ComponentRow[],
  gap: RuntimeComponents["expected_gaps"][number] | undefined,
  kind: "role" | "dependency",
  timezone: string, t: I18nContextValue["t"] = fallbackContext.t,
): HealthRow {
  const processes = rows.filter((row) => row.observation_kind === "process");
  const instances = new Set(processes.map((row) => row.instance_id));
  const running = new Set(processes.filter(isFreshObservation).map((row) => row.instance_id));
  const silent = [...instances].filter((id) => !running.has(id)).length;
  const health = healthOf(rows);
  const notReady = rows.some((row) => isFreshObservation(row) && row.readiness.freshness === "fresh" && row.readiness.state === "not_ready");
  const anyFresh = rows.some(isFreshObservation);
  let status = t("react.monitoring.running_health_unmeasured", "Running · health not measured");
  let tone: HealthTone = "neutral";
  if (health.state === "unhealthy" || notReady) {
    status = kind === "dependency" ? t("react.monitoring.unreachable", "Unreachable") : t("react.monitoring.unhealthy", "Unhealthy");
    tone = "danger";
  } else if (gap?.reason === "replica_target_unobserved") {
    status = t("react.monitoring.running_target_count", "{running} of {target} running", { running: running.size, target: gap.desired_count ?? "?" });
    tone = "warning";
  } else if (gap?.reason === "missing" || (!rows.length && gap)) {
    status = t("react.monitoring.not_reporting_status", "Not reporting");
    tone = "warning";
  } else if (!anyFresh || gap?.reason === "stale" || silent > 0) {
    status = silent > 0 && anyFresh ? t("react.monitoring.not_reporting_count", "{count} not reporting", { count: silent }) : t("react.monitoring.not_reporting_status", "Not reporting");
    tone = "warning";
  } else if (health.state === "healthy") {
    status = t("react.monitoring.healthy", "Healthy");
    tone = "healthy";
  }
  const parts: string[] = [];
  if (kind === "role" && processes.length) parts.push(t("react.monitoring.running_count", "{count} running", { count: running.size }));
  if (health.state !== "unmeasured") {
    parts.push(t("react.monitoring.health_evidence_summary", "{state} by {evidence}{checked}", { state: labelFromKey(health.state, t), evidence: evidenceLabel(health.evidence, t), checked: health.age !== null ? t("react.monitoring.checked_age_suffix", ", checked {age}", { age: ageText(health.age, t) }) : "" }));
  } else if (anyFresh) {
    parts.push(t("react.monitoring.no_health_check_reported", "No health check reported"));
  }
  const latest = rows.reduce<ComponentRow | null>((best, row) => (!best || row.observed_at > best.observed_at ? row : best), null);
  if (latest) parts.push(t("react.monitoring.last_report_detail", "Last report: {date} ({age})", { date: formatUserDateTime(latest.observed_at, timezone), age: ageText(latest.effective_age_seconds, t) }));
  if (gap && !rows.length) parts.push(gap.observation_kind === "dependency" ? t("react.monitoring.dependency_not_reported", "No check has reported this dependency yet") : t("react.monitoring.process_not_reported", "Expected, but no process has reported"));
  const meta = rows.map((row) => t("react.monitoring.process_diagnostics", "{kind} {source} · {instance}{started} · readiness {readiness} · health {health}{evidence}", { kind: labelFromKey(row.observation_kind, t), source: row.source, instance: shortEntityId(row.instance_id), started: row.started_at ? t("react.monitoring.started_value1", " · started {value1}", { value1: formatUserDateTime(row.started_at, timezone) }) : "", readiness: labelFromKey(row.readiness.state, t), health: labelFromKey(row.health.state, t), evidence: row.health.evidence ? ` (${row.health.evidence})` : "" })).join("\n");
  return { key, title, subtitle: kind === "dependency" ? t("react.monitoring.dependency", "Dependency") : undefined, status, tone, detail: parts.join(" · "), meta: meta || undefined };
}

/** One verdict per role and per dependency, from current observations only. */
export function serviceVerdicts(components: RuntimeComponents | null, timezone: string, t: I18nContextValue["t"] = fallbackContext.t): { services: HealthRow[]; dependencies: HealthRow[] } {
  if (!components?.available) return { services: [], dependencies: [] };
  const current = components.rows.filter((row) => isCurrent(row) && row.observation_kind !== "channel");
  const roleRows = new Map<string, ComponentRow[]>();
  const dependencyRows = new Map<string, ComponentRow[]>();
  for (const row of current) {
    const target = row.observation_kind === "dependency" ? dependencyRows : roleRows;
    target.set(row.service_type, [...(target.get(row.service_type) ?? []), row]);
  }
  const gaps = new Map(components.expected_gaps.map((gap) => [`${gap.observation_kind}:${gap.service_type}`, gap]));
  for (const gap of components.expected_gaps) {
    const target = gap.observation_kind === "dependency" ? dependencyRows : roleRows;
    if (!target.has(gap.service_type)) target.set(gap.service_type, []);
  }
  const project = (rows: Map<string, ComponentRow[]>, kind: "role" | "dependency") =>
    [...rows.entries()]
      .map(([name, list]) => verdictRow(`${kind}:${name}`, serviceLabel(name, t), list,
        gaps.get(`${kind === "dependency" ? "dependency" : "process"}:${name}`), kind, timezone, t))
      .sort((a, b) => TONE_ORDER[a.tone] - TONE_ORDER[b.tone] || a.title.localeCompare(b.title));
  return { services: project(roleRows, "role"), dependencies: project(dependencyRows, "dependency") };
}

/** Observations replaced by a newer start, or that said they stopped. */
export function historyRows(components: RuntimeComponents | null, timezone: string, t: I18nContextValue["t"] = fallbackContext.t): HealthRow[] {
  if (!components?.available) return [];
  return componentRows({ ...components, rows: components.rows.filter((row) => row.superseded) }, timezone, t)
    .map((row) => ({ ...row, tone: "neutral" as const, status: t("react.monitoring.replaced", "Replaced") }));
}
