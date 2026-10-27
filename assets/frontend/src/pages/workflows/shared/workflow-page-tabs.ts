import { useEffect, useRef } from "react";

import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { declaredWorkflowHooks } from "@/pages/workflows/shared/workflow-hooks";

/** A tab of the workflow page after Overview and Projects (which are routes of their own). */
export type WorkflowPageTab = { key: string; label: string };

/** The channel state the Webhook and Schedules tabs follow: the saved one, or the unsaved draft. */
export type WorkflowChannelState = { enabled: boolean; channels: string[] };

/** Tabs that edit the workflow's settings: administrators and authors only. */
export const WORKFLOW_SETTINGS_TAB_KEYS = ["smart-setup", "webhook", "schedules", "automation", "history"] as const;

const SETTINGS_TAB_SET = new Set<string>(WORKFLOW_SETTINGS_TAB_KEYS);

export function isWorkflowSettingsTab(key: string): boolean {
  return SETTINGS_TAB_SET.has(key);
}

/**
 * Workflow settings show a channel's tab only once that channel is on: the
 * Webhook tab with Incoming Webhook, Schedules with Schedule. Nothing is
 * configured for a channel that cannot start the workflow. Shared by the
 * builder's Workflow settings drawer (`triggers`) and the workflow page (`webhook`).
 */
export function workflowChannelTabHidden(tabKey: string, ctx: Record<string, unknown>): boolean {
  const channels = Array.isArray(ctx.workflowInterfaceTriggerChannelsDraft) ? ctx.workflowInterfaceTriggerChannelsDraft as unknown[] : null;
  if (!channels) return false;
  const on = (channel: string) => ctx.workflowInterfaceEnabledDraft !== false && channels.includes(channel);
  if (tabKey === "triggers" || tabKey === "webhook") return !on("webhook");
  if (tabKey === "schedules") return !on("schedule");
  return false;
}

export function workflowChannelTabHiddenFor(tabKey: string, state: WorkflowChannelState | null): boolean {
  return workflowChannelTabHidden(tabKey, state
    ? { workflowInterfaceEnabledDraft: state.enabled, workflowInterfaceTriggerChannelsDraft: state.channels }
    : {});
}

/** The channels the workflow was last saved with, read from its detail record. */
export function savedWorkflowChannelState(detail: unknown): WorkflowChannelState {
  const settings = (detail as { settings?: unknown } | null | undefined)?.settings;
  const callable = (settings as { callable?: unknown } | null | undefined)?.callable as
    | { enabled?: unknown; trigger_channels?: unknown }
    | undefined;
  if (!callable || typeof callable !== "object") return { enabled: false, channels: [] };
  return {
    enabled: Boolean(callable.enabled),
    channels: Array.isArray(callable.trigger_channels) ? callable.trigger_channels.map(String) : [],
  };
}

/**
 * The tabs after Overview and Projects, in order: Hooks (only when the workflow
 * declares hooks), then, for administrators and authors, Smart setup, Webhook
 * (Incoming Webhook is on), Schedules (Schedule is on), Automation and History.
 */
export function workflowPageTabs({
  canAuthor,
  channels,
  hasHooks,
  t = fallbackContext.t,
}: Readonly<{
  canAuthor: boolean;
  channels: WorkflowChannelState | null;
  hasHooks: boolean;
  t?: I18nContextValue["t"];
}>): WorkflowPageTab[] {
  const tabs: WorkflowPageTab[] = [];
  if (hasHooks) tabs.push({ key: "hooks", label: t("react.workflows.hooks", "Hooks") });
  if (!canAuthor) return tabs;
  const settingsTabs: WorkflowPageTab[] = [
    { key: "smart-setup", label: t("react.workflows.smart_setup", "Smart setup") },
    { key: "webhook", label: t("react.workflows.webhook_tab", "Webhook") },
    { key: "schedules", label: t("react.workflows.schedules", "Schedules") },
    { key: "automation", label: t("project_detail.tab.automation", "Automation") },
    { key: "history", label: t("react.workflows.history_tab", "History") },
  ];
  return [...tabs, ...settingsTabs.filter((tab) => !workflowChannelTabHiddenFor(tab.key, channels))];
}

/** The page's tabs for a loaded workflow, from its saved settings or an unsaved draft of the channels. */
export function workflowPageTabsForDetail(
  detail: unknown,
  canAuthor: boolean,
  t: I18nContextValue["t"],
  draftChannels: WorkflowChannelState | null = null,
): WorkflowPageTab[] {
  const steps = (detail as { steps?: unknown } | null | undefined)?.steps;
  return workflowPageTabs({
    canAuthor,
    channels: draftChannels ?? savedWorkflowChannelState(detail),
    hasHooks: declaredWorkflowHooks(steps).length > 0,
    t,
  });
}

/**
 * Turning a channel off while its tab is open goes back to Smart setup. Only a
 * tab that was shown and then hidden moves: a deep link opened before the
 * settings load, or a closed surface, never does.
 */
export function useChannelTabFallback({
  active,
  hidden,
  onFallback,
  tab,
}: Readonly<{ active: boolean; hidden: boolean; onFallback: () => void; tab: string }>): void {
  const shownChannelTab = useRef("");
  useEffect(() => {
    if (!active) { shownChannelTab.current = ""; return; }
    if (!hidden) { shownChannelTab.current = tab; return; }
    if (shownChannelTab.current === tab) { shownChannelTab.current = ""; onFallback(); }
  }, [active, hidden, onFallback, tab]);
}
