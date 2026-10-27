import { fetchJson } from "@/lib/http/client";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type NotificationOverviewScopeKind = "project" | "workflow" | "group" | "step";
export type NotificationOverviewState = "enabled" | "disabled" | "setup_problem";
export type NotificationOverviewMethod = "matching_subscriptions" | "subscriptions" | "channel" | "recipients";

export type NotificationOverviewSubscription = { route_key: string; name: string; found: boolean };

export type NotificationOverviewEntry = {
  entry_id: string;
  kind: "automation" | "subscription";
  name: string;
  source: {
    scope_kind: NotificationOverviewScopeKind;
    workflow_id: string;
    workflow_name: string;
    scope_id: string;
    scope_name: string;
  };
  event_type: string;
  conditions: {
    severity: string;
    workflow_id: string;
    workflow_name: string;
    fields: Array<{ field: string; value: string }>;
    debounce_seconds: number;
  };
  delivery: {
    method: NotificationOverviewMethod;
    provider_key: string;
    provider_name: string;
    channel_id: string;
    recipients: string[];
    subscriptions: NotificationOverviewSubscription[];
    decided_at_run_time: boolean;
  };
  enabled: boolean;
  state: NotificationOverviewState;
  problems: string[];
  settings: {
    kind: "automation" | "subscription";
    workflow_id?: string;
    scope_kind?: string;
    scope_id?: string;
    rule_key?: string;
    route_key?: string;
  };
  connection: { extension_id: string } | null;
};

type Row = Record<string, unknown>;

function rowOf(value: unknown): Row {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Row) : {};
}

function text(value: unknown): string {
  return stringFromUnknownValue(value ?? "").trim();
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.map(text).filter(Boolean) : [];
}

const SCOPE_KINDS = new Set(["project", "workflow", "group", "step"]);
const STATES = new Set(["enabled", "disabled", "setup_problem"]);
const METHODS = new Set(["matching_subscriptions", "subscriptions", "channel", "recipients"]);

function normalizeEntry(value: unknown): NotificationOverviewEntry | null {
  const row = rowOf(value);
  const entryId = text(row.entry_id);
  if (!entryId) return null;
  const source = rowOf(row.source);
  const conditions = rowOf(row.conditions);
  const delivery = rowOf(row.delivery);
  const settings = rowOf(row.settings);
  const connection = rowOf(row.connection);
  const scopeKind = text(source.scope_kind);
  const state = text(row.state);
  const method = text(delivery.method);
  return {
    entry_id: entryId,
    kind: text(row.kind) === "subscription" ? "subscription" : "automation",
    name: text(row.name),
    source: {
      // A scope this screen does not know (a retired one) still names its kind.
      scope_kind: (SCOPE_KINDS.has(scopeKind) ? scopeKind : scopeKind || "workflow") as NotificationOverviewScopeKind,
      workflow_id: text(source.workflow_id),
      workflow_name: text(source.workflow_name),
      scope_id: text(source.scope_id),
      scope_name: text(source.scope_name),
    },
    event_type: text(row.event_type),
    conditions: {
      severity: text(conditions.severity),
      workflow_id: text(conditions.workflow_id),
      workflow_name: text(conditions.workflow_name),
      fields: (Array.isArray(conditions.fields) ? conditions.fields : [])
        .map((item) => ({ field: text(rowOf(item).field), value: text(rowOf(item).value) }))
        .filter((item) => item.field),
      debounce_seconds: Number(conditions.debounce_seconds) || 0,
    },
    delivery: {
      method: (METHODS.has(method) ? method : "matching_subscriptions") as NotificationOverviewMethod,
      provider_key: text(delivery.provider_key),
      provider_name: text(delivery.provider_name),
      channel_id: text(delivery.channel_id),
      recipients: texts(delivery.recipients),
      subscriptions: (Array.isArray(delivery.subscriptions) ? delivery.subscriptions : []).map((item) => {
        const subscription = rowOf(item);
        return { route_key: text(subscription.route_key), name: text(subscription.name), found: subscription.found !== false };
      }),
      decided_at_run_time: delivery.decided_at_run_time === true,
    },
    enabled: row.enabled !== false,
    // An unknown state is never shown as working.
    state: (STATES.has(state) ? state : "setup_problem") as NotificationOverviewState,
    problems: texts(row.problems),
    settings: {
      kind: text(settings.kind) === "subscription" ? "subscription" : "automation",
      workflow_id: text(settings.workflow_id),
      scope_kind: text(settings.scope_kind),
      scope_id: text(settings.scope_id),
      rule_key: text(settings.rule_key),
      route_key: text(settings.route_key),
    },
    connection: text(connection.extension_id) ? { extension_id: text(connection.extension_id) } : null,
  };
}

export async function fetchProjectNotificationOverview(projectId: string): Promise<NotificationOverviewEntry[]> {
  const payload = await fetchJson<unknown>(
    `/api/projects/${encodeURIComponent(projectId)}/notification-overview`,
  );
  const entries = rowOf(payload).entries;
  if (!Array.isArray(entries)) {
    throw new TypeError("The notification overview answered without entries.");
  }
  return entries.map(normalizeEntry).filter((entry): entry is NotificationOverviewEntry => entry !== null);
}

/** Where an entry is configured: its Automation rule on its scope. A project event subscription has no editor, so no path. */
export function notificationSettingsPath(projectId: string, entry: NotificationOverviewEntry): string | null {
  const { settings } = entry;
  if (settings.kind === "subscription") return null;
  const workflowId = settings.workflow_id || "";
  const params = new URLSearchParams({ project_id: projectId });
  if (settings.scope_kind === "group" && settings.scope_id) {
    params.set("drawer", "group");
    params.set("group_id", settings.scope_id);
    params.set("group_tab", "automation");
  } else if (settings.scope_kind === "step" && settings.scope_id) {
    params.set("drawer", "step");
    params.set("step_id", settings.scope_id);
    params.set("step_tab", "automation");
  } else {
    params.set("drawer", "automation");
  }
  if (settings.rule_key) params.set("rule", settings.rule_key);
  return `/registry/workflows/${encodeURIComponent(workflowId)}/builder?${params.toString()}`;
}

/** The extension that owns the connection a setup problem is about. */
export function notificationConnectionPath(projectId: string, extensionId: string): string {
  return `/extensions/${encodeURIComponent(extensionId)}?project_id=${encodeURIComponent(projectId)}`;
}
