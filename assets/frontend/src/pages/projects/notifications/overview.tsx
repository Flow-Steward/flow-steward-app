import { useI18n } from "@/lib/i18n/useI18n";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import type { I18nContextValue } from "@/lib/i18n/context";
import type { ReactNode } from "react";

import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Loader2 } from "lucide-react";
import { Link } from "react-router";

import { DsAlert, DsBadge, DsValueChip, type DsBadgeTone } from "@/components/shared/design-system";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  fetchProjectNotificationOverview,
  notificationConnectionPath,
  notificationSettingsPath,
  type NotificationOverviewEntry,
} from "@/pages/projects/notifications/overview-api";

type Translate = I18nContextValue["t"];

const STATE_TONE: Record<NotificationOverviewEntry["state"], DsBadgeTone> = {
  enabled: "green",
  disabled: "neutral",
  setup_problem: "danger",
};

function stateLabel(state: NotificationOverviewEntry["state"], t: Translate): string {
  if (state === "enabled") return t("react.projects.notification_overview_state_enabled", "On");
  if (state === "disabled") return t("react.projects.notification_overview_state_disabled", "Off");
  return t("react.projects.notification_overview_state_setup_problem", "Setup problem");
}

function sourceText(entry: NotificationOverviewEntry, t: Translate): string {
  const { source } = entry;
  const workflow = source.workflow_name || source.workflow_id;
  const scope = source.scope_name || source.scope_id;
  if (entry.kind === "subscription") {
    return t("react.projects.notification_overview_source_project", "Project event subscription");
  }
  if (source.scope_kind === "group") {
    return t("react.projects.notification_overview_source_group", "Automation of group {scope} in workflow {workflow}", { scope, workflow });
  }
  if (source.scope_kind === "step") {
    return t("react.projects.notification_overview_source_step", "Automation of step {scope} in workflow {workflow}", { scope, workflow });
  }
  return t("react.projects.notification_overview_source_workflow", "Automation of workflow {workflow}", { workflow });
}

function conditionTexts(entry: NotificationOverviewEntry, t: Translate): string[] {
  const { conditions } = entry;
  const parts: string[] = [];
  if (conditions.severity) {
    parts.push(t("react.projects.notification_overview_condition_severity", "severity {severity}", { severity: conditions.severity }));
  }
  if (conditions.workflow_id) {
    parts.push(t("react.projects.notification_overview_condition_workflow", "only workflow {workflow}", { workflow: conditions.workflow_name || conditions.workflow_id }));
  }
  for (const item of conditions.fields) {
    parts.push(`${item.field} = ${item.value}`);
  }
  if (conditions.debounce_seconds > 0) {
    parts.push(t("react.projects.notification_overview_condition_debounce", "at most once per {seconds} s", { seconds: String(conditions.debounce_seconds) }));
  }
  return parts;
}

function subscriptionNames(entry: NotificationOverviewEntry, t: Translate): string {
  return entry.delivery.subscriptions
    .map((item) => (item.found
      ? item.name || item.route_key
      : t("react.projects.notification_overview_missing_subscription", "{name} (missing)", { name: item.name || item.route_key })))
    .join(", ");
}

function deliveryText(entry: NotificationOverviewEntry, t: Translate): string {
  const { delivery } = entry;
  const provider = delivery.provider_name || delivery.provider_key;
  if (delivery.method === "matching_subscriptions") {
    const names = subscriptionNames(entry, t);
    return names
      ? t("react.projects.notification_overview_delivery_matching_with", "Matching project event subscriptions, now: {names}", { names })
      : t("react.projects.notification_overview_delivery_matching", "Matching project event subscriptions");
  }
  if (delivery.method === "subscriptions") {
    return t("react.projects.notification_overview_delivery_subscriptions", "Event subscriptions {names}", { names: subscriptionNames(entry, t) });
  }
  if (delivery.method === "recipients") {
    return t("react.projects.notification_overview_delivery_recipients", "{provider} to {recipients}", { provider, recipients: delivery.recipients.join(", ") });
  }
  return delivery.channel_id
    ? t("react.projects.notification_overview_delivery_channel", "{provider} channel {channel}", { provider, channel: delivery.channel_id })
    : provider;
}

function problemText(code: string, extension: string, t: Translate): string {
  switch (code) {
    case "extension_missing":
      return t("react.projects.notification_overview_problem_extension_missing", "The extension {extension} is not installed.", { extension });
    case "extension_disabled":
      return t("react.projects.notification_overview_problem_extension_disabled", "The extension {extension} is disabled.", { extension });
    case "connection_disabled":
      return t("react.projects.notification_overview_problem_connection_disabled", "The project connection to {extension} is switched off.", { extension });
    case "connection_missing":
      return t("react.projects.notification_overview_problem_connection_missing", "The selected project connection no longer exists.");
    case "destination_missing":
      return t("react.projects.notification_overview_problem_destination_missing", "No channel or recipient is set.");
    case "subscription_missing":
      return t("react.projects.notification_overview_problem_subscription_missing", "An event subscription this rule names no longer exists.");
    case "no_matching_subscription":
      return t("react.projects.notification_overview_problem_no_matching_subscription", "No enabled event subscription matches this event, so nothing is sent.");
    case "scope_retired":
      return t("react.projects.notification_overview_problem_scope_retired", "This rule belongs to a retired scope and never runs.");
    default:
      return t("react.projects.notification_overview_problem_other", "This notification cannot be delivered as configured.");
  }
}

function OverviewEntry({ entry, projectId }: Readonly<{ entry: NotificationOverviewEntry; projectId: string }>) {
  const { t } = useI18n();
  const name = entry.name || entry.entry_id;
  const conditions = conditionTexts(entry, t);
  // The settings live in the builder, which only administrators open; a reader gets no dead link.
  const canOpenSettings = useIsAdmin();
  const settingsPath = canOpenSettings ? notificationSettingsPath(projectId, entry) : "";
  const extension = entry.connection?.extension_id || entry.delivery.provider_key;
  return (
    <li
      className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-3 text-sm fs-ink-secondary"
      data-testid="notification-overview-entry"
    >
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="truncate font-medium fs-ink-brand">{name}</p>
            <DsBadge tone={STATE_TONE[entry.state]}>{stateLabel(entry.state, t)}</DsBadge>
          </div>
          <div className="fs-detail-rows mt-2">
            <div className="fs-detail-row">
              <span>{t("react.projects.notification_overview_source", "Source")}</span>
              <span className="fs-detail-value">{sourceText(entry, t)}</span>
            </div>
            <div className="fs-detail-row">
              <span>{t("react.projects.notification_overview_event", "Event")}</span>
              <span className="fs-detail-value"><DsValueChip>{entry.event_type || "*"}</DsValueChip></span>
            </div>
            {conditions.length ? (
              <div className="fs-detail-row">
                <span>{t("react.projects.notification_overview_conditions", "Conditions")}</span>
                <span className="fs-detail-value">{conditions.join("; ")}</span>
              </div>
            ) : null}
            <div className="fs-detail-row">
              <span>{t("react.projects.notification_overview_delivery", "Sends to")}</span>
              <span className="fs-detail-value">
                {deliveryText(entry, t)}
                {entry.delivery.decided_at_run_time ? (
                  <> · {t("react.projects.notification_overview_decided_at_run_time", "decided at run time")}</>
                ) : null}
              </span>
            </div>
          </div>
          {entry.problems.length ? (
            <ul className="mt-2 space-y-1 text-xs fs-ink-attention">
              {entry.problems.map((code) => (
                <li key={code}>{problemText(code, extension, t)}</li>
              ))}
            </ul>
          ) : null}
        </div>
        <div className="flex flex-wrap gap-2">
          {settingsPath ? (
            <Link
              aria-label={t("react.projects.notification_overview_open_settings_of", "Open settings of {name}", { name })}
              className="fs-button secondary sm"
              to={settingsPath}
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t("react.projects.notification_overview_open_settings", "Open settings")}
            </Link>
          ) : null}
          {entry.connection ? (
            <Link
              aria-label={t("react.projects.notification_overview_open_connection_of", "Open connection settings of {name}", { name })}
              className="fs-button secondary sm"
              to={notificationConnectionPath(projectId, entry.connection.extension_id)}
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t("react.projects.notification_overview_open_connection", "Open connection settings")}
            </Link>
          ) : null}
        </div>
      </div>
    </li>
  );
}

/**
 * The project's automatic notifications, read-only. Each entry comes from the
 * live setting that sends it - an Automation rule or an existing project event
 * subscription. Automation rules link to their settings; subscriptions have no
 * editor and are only listed. Nothing here creates, edits, switches or deletes. Steps that send messages are not listed: they are part
 * of the workflow, not automatic notifications.
 */
export function ProjectNotificationOverview({ projectId }: Readonly<{ projectId: string }>) {
  const { t } = useI18n();
  const overviewQuery = useQuery({
    queryKey: ["projects", "notification-overview", projectId],
    queryFn: () => fetchProjectNotificationOverview(projectId),
    enabled: Boolean(projectId),
    refetchInterval: 30_000,
  });
  const entries = overviewQuery.data ?? [];
  const loading = overviewQuery.isPending;
  const failed = overviewQuery.isError && !overviewQuery.data;

  let body: ReactNode;
  if (loading) {
    body = (
      <div className="flex items-center gap-2 text-sm fs-ink-secondary" role="status">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t("react.projects.notification_overview_loading", "Loading automatic notifications…")}
      </div>
    );
  } else if (failed) {
    body = (
      <DsAlert tone="danger" title={t("react.projects.notification_overview_failed", "Automatic notifications could not be loaded.")}>
        <p>{overviewQuery.error instanceof Error ? overviewQuery.error.message : ""}</p>
        <button className="fs-button secondary sm mt-2" onClick={() => { void overviewQuery.refetch(); }} type="button">
          {t("react.projects.notification_overview_retry", "Try again")}
        </button>
      </DsAlert>
    );
  } else if (!entries.length) {
    body = (
      <p className="fs-empty-copy">
        {t("react.projects.notification_overview_empty", "No automatic notifications are set up for this project. Add a Send notification rule in a workflow's Automation.")}
      </p>
    );
  } else {
    body = (
      <ul aria-label={t("react.projects.notification_overview_title", "Automatic notifications")} className="space-y-3">
        {entries.map((entry) => <OverviewEntry entry={entry} key={entry.entry_id} projectId={projectId} />)}
      </ul>
    );
  }

  return (
    <section className="space-y-4">
      <Card className="fs-card">
        <CardHeader>
          <CardTitle className="text-lg">{t("react.projects.notification_overview_title", "Automatic notifications")}</CardTitle>
          <CardDescription>
            {t("react.projects.notification_overview_description", "Automatic notifications about project events and workflow runs. Messages sent by individual steps are configured inside the workflow and are not shown here.")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">{body}</CardContent>
      </Card>
    </section>
  );
}
