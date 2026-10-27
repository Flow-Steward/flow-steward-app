import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { DsCheckbox, DsDialog, DsSelect, RequiredLabel } from "@/components/shared/design-system";
import { AlertTriangle, Loader2, Save } from "lucide-react";
import { Link } from "react-router";
import { ROUTE_FIELD_ELEMENT_IDS, type RouteFieldId } from "./validation";
import type { ProjectNotificationProvider, ProviderChannel } from "./api";
import { textOrEmpty, SEVERITY_OPTIONS, type RouteDraft, type RouteModalMode } from "./notification-route-model";

function getEventOptions(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { value: "*", label: t("react.projects.all_events", "All events") },
  { value: "job.created", label: t("react.projects.job_created", "Job created") },
  { value: "job.completed", label: t("react.projects.job_completed", "Job completed") },
  { value: "job.failed", label: t("react.projects.job_failed", "Job failed") },
  { value: "job.awaiting_approval", label: t("react.projects.job_awaiting_approval", "Job awaiting approval") },
  { value: "workflow.automation.notify", label: t("react.projects.workflow_notification", "Workflow notification") },
];
}
/**
 * Some providers deliver to a recipient (an address you type) rather than to a
 * channel (one you pick from a list the provider offers).
 *
 * Which one is read from `notification_target_kind`, derived by the platform from
 * the extension's own declaration. Matching a provider key here would mean the UI
 * knew which extension sends mail, and a second one would silently get the wrong
 * form.
 */
function routeSaveLabel(saving: boolean, mode: RouteModalMode, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (saving) return t("project_edit.security.saving", "Saving...");
  return mode === "create" ? t("react.projects.create_route", "Create route") : t("react.projects.save_route", "Save route");
}

/**
 * A provider that is not in the inventory — disabled, uninstalled, or not yet
 * loaded — has an *unknown* target kind, which is not the same as "channel".
 *
 * Collapsing the two is how editing a route whose extension was disabled used to
 * rewrite a recipient route into a channel route on save, silently, while the
 * form showed no provider at all.
 */
function RouteTargetEditor({
  draft,
  error,
  usesEmailApRecipient,
  channels,
  channelsQuery,
  provider,
  projectId,
  onChange,
}: Readonly<{
  draft: RouteDraft;
  error: { field: RouteFieldId | null; message: string } | null;
  usesEmailApRecipient: boolean;
  channels: ProviderChannel[];
  channelsQuery: { isLoading?: boolean; error?: unknown } | undefined;
  provider: ProjectNotificationProvider | null;
  projectId: string;
  onChange: (patch: Partial<RouteDraft>) => void;
}>) {
  const { t } = useI18n();
  if (usesEmailApRecipient) {
    return (
      <label className="mt-2 block text-xs uppercase tracking-[0.15em] fs-ink-secondary">
        <RequiredLabel>{t("react.projects.recipient_email", "Recipient email")}</RequiredLabel>
        <input
          aria-invalid={error?.field === "channel_id" || undefined}
          aria-required
          className="fs-input"
          id={ROUTE_FIELD_ELEMENT_IDS.channel_id}
          onChange={(event) => onChange({ channel_id: event.target.value })}
          placeholder={t("react.projects.operator_example_com", "operator@example.com")}
          type="email"
          value={draft.channel_id}
        />
      </label>
    );
  }
  if (!draft.provider_key) {
    return (
      <p className="mt-2 text-sm fs-ink-tertiary">
        {t("react.projects.choose_a_provider_to_load_its_configured_channels", "Choose a provider to load its configured channels.")} </p>
    );
  }
  if (channelsQuery?.isLoading) {
    return (
      <div className="mt-2 flex items-center gap-2 text-sm fs-ink-secondary">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t("react.projects.loading_channels", "Loading channels...")} </div>
    );
  }
  if (channelsQuery?.error) {
    return (
      <div className="mt-2 flex items-start gap-2 rounded-lg border border-selected fs-surface-attention px-3 py-2 text-sm fs-ink-attention">
        <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" />
        <span>
          {channelsQuery.error instanceof Error
            ? channelsQuery.error.message
            : t("react.projects.failed_to_load_provider_channels", "Failed to load provider channels.")}
        </span>
      </div>
    );
  }
  if (!channels.length) {
    return (
      <div className="mt-2 rounded-lg border border-amber-300/30 bg-amber-500/10 px-3 py-3 text-sm text-amber-100">
        <p>{t("react.projects.no_channels_configured_for_this_provider_in_the_selected_project", "No channels configured for this provider in the selected project.")}</p>
        <p className="mt-1 text-xs fs-ink-attention/80">
          {t("react.projects.configure_channels_in", "Configure channels in")}{" "}
          <Link
            className="underline underline-offset-4"
            to={`/extensions/${encodeURIComponent(draft.provider_key)}?project_id=${encodeURIComponent(projectId)}`}
          >
            {t("react.projects.provider_configuration", "{provider} configuration", { provider: provider?.display_name || draft.provider_key })}
          </Link>
          .
        </p>
      </div>
    );
  }
  return (
    <div className="mt-2 block text-xs uppercase tracking-[0.15em] fs-ink-secondary">
      <RequiredLabel>{t("react.projects.registered_channel", "Registered channel")}</RequiredLabel>
      <DsSelect
        ariaLabel={t("react.projects.registered_channel", "Registered channel")}
        id={ROUTE_FIELD_ELEMENT_IDS.channel_id}
        invalid={error?.field === "channel_id"}
        onChange={(value) => onChange({ channel_id: value })}
        options={channels.map((channel) => ({
          value: channel.channel_id,
          label: `${channel.display_name} (${channel.channel_id})`,
        }))}
        placeholder={t("react.projects.select_channel", "Select channel")}
        required
        value={draft.channel_id}
      />
    </div>
  );
}

export function NotificationRouteDialog({
  mode,
  draft,
  saving,
  error,
  providerOptions,
  providerChoices,
  provider,
  channels,
  channelsQuery,
  usesEmailRecipientTarget,
  workflowChoices,
  workflowsLoading,
  workflowsError,
  workflowsReady,
  onRetryWorkflows,
  createInventoryReady,
  onRetryNotificationData,
  projectId,
  onChange,
  onClose,
  onSave,
}: Readonly<{
  mode: RouteModalMode;
  draft: RouteDraft;
  saving: boolean;
  error: { field: RouteFieldId | null; message: string } | null;
  providerOptions: ProjectNotificationProvider[];
  providerChoices: Array<{ value: string; label: string }>;
  provider: ProjectNotificationProvider | null;
  channels: ProviderChannel[];
  channelsQuery: { isLoading?: boolean; error?: unknown } | undefined;
  usesEmailRecipientTarget: boolean;
  workflowChoices: Array<{ value: string; label: string }>;
  workflowsLoading: boolean;
  workflowsError: boolean;
  workflowsReady: boolean;
  onRetryWorkflows: () => void;
  createInventoryReady: boolean;
  onRetryNotificationData: () => void;
  projectId: string;
  onChange: (patch: Partial<RouteDraft>) => void;
  onClose: () => void;
  onSave: () => void;
}>) {
  const { t } = useI18n();
  return (
    <DsDialog
      busy={saving}
      closeLabel={t("react.projects.close_notification_route_editor", "Close notification route editor")}
      description={t("react.projects.a_route_is_a_provider_plus_the_channel_or_recipient_it_delivers_to", "A route is a provider plus the channel or recipient it delivers to.")}
      eyebrow={t("settings.tab.notifications", "Notifications")}
      footer={
        <>
          <button className="fs-button" onClick={onClose} type="button">
            {t("common.cancel", "Cancel")} </button>
          <button
            className="fs-button primary"
            disabled={saving || (mode === "create" && (!workflowsReady || !createInventoryReady))}
            id="notification-route-save"
            onClick={onSave}
            type="button"
          >
            <Save className="h-4 w-4" />
            {routeSaveLabel(saving, mode, t)}
          </button>
        </>
      }
      onClose={onClose}
      open
      title={
        mode === "create"
          ? t("react.projects.create_notification_route", "Create notification route")
          : t("react.projects.edit_notification_route", "Edit notification route")
      }
    >
      {mode === "create" && !createInventoryReady ? (
        <div className="mb-3 text-sm fs-ink-attention" role="alert">
          <p>{t("react.projects.notification_provider_or_route_inventory_is_loading_or_unavailable_reload_notification_data_before_creating_a_", "Notification provider or route inventory is loading or unavailable. Reload notification data before creating a route; your draft is preserved.")}</p>
          <button className="fs-button sm" onClick={onRetryNotificationData} type="button">{t("react.projects.retry_notification_data", "Retry notification data")}</button>
        </div>
      ) : null}
      {error ? (
        <div
          className="mb-3 flex items-start gap-2 rounded-lg border border-selected fs-surface-attention px-3 py-2 text-sm fs-ink-attention"
          role="alert"
        >
          <AlertTriangle
            aria-hidden="true"
            className="mt-0.5 h-4 w-4 flex-none"
          />
          <span>{error.message}</span>
        </div>
      ) : null}
      <div className="grid gap-3 lg:grid-cols-3">
        <label className="block text-xs uppercase tracking-[0.15em] fs-ink-secondary">
          <span>{t("react.projects.route_key_automatic", "Route key (automatic)")}</span>
          <input
            className="fs-input fs-code-value"
            id={ROUTE_FIELD_ELEMENT_IDS.route_key}
            readOnly
            value={draft.route_key}
          />
        </label>
        <label className="block text-xs uppercase tracking-[0.15em] fs-ink-secondary">
          <span>{t("react.projects.display_name", "Display name")}</span>
          <input
            className="fs-input"
            onChange={(event) => onChange({ display_name: event.target.value })}
            placeholder={t("react.projects.ops_alerts", "Ops alerts")}
            value={draft.display_name}
          />
        </label>
        <div className="block text-xs uppercase tracking-[0.15em] fs-ink-secondary">
          <RequiredLabel>{t("project_edit.ai_models.provider", "Provider")}</RequiredLabel>
          <DsSelect
            ariaLabel={t("project_edit.ai_models.provider", "Provider")}
            id={ROUTE_FIELD_ELEMENT_IDS.provider_key}
            invalid={error?.field === "provider_key"}
            onChange={(value) =>
              onChange({
                channel_id: "",
                connection_id: textOrEmpty(
                  providerOptions.find(
                    (candidate) => candidate.provider_key === value,
                  )?.connection_id,
                ),
                provider_key: value,
              })
            }
            options={providerChoices}
            placeholder={t("react.projects.select_provider", "Select provider")}
            required
            value={draft.provider_key}
          />
        </div>
        <div className="block text-xs uppercase tracking-[0.15em] fs-ink-secondary">
          <RequiredLabel>{t("react.projects.event_type", "Event type")}</RequiredLabel>
          <DsSelect
            ariaLabel={t("react.projects.event_type", "Event type")}
            invalid={error?.field === "event_type"}
            id={ROUTE_FIELD_ELEMENT_IDS.event_type}
            onChange={(value) => onChange({ event_type: value })}
            options={getEventOptions(t).some((option) => option.value === draft.event_type)
              ? getEventOptions(t)
              : [...getEventOptions(t), { value: draft.event_type, label: t("react.projects.value1_existing", "{value1} (existing)", { value1: String(draft.event_type) }) }]}
            required
            value={draft.event_type}
          />
        </div>
        <div className="block text-xs uppercase tracking-[0.15em] fs-ink-secondary">
          <span>{t("events.columns.severity", "Severity")}</span>
          <DsSelect
            ariaLabel={t("events.columns.severity", "Severity")}
            onChange={(value) => onChange({ severity: value })}
            options={SEVERITY_OPTIONS.map((severity) => ({
              value: severity,
              label: severity,
            }))}
            value={draft.severity}
          />
        </div>
        <div className="block text-xs uppercase tracking-[0.15em] fs-ink-secondary">
          <span>{t("jobs.columns.workflow", "Workflow")}</span>
          <DsSelect
            ariaLabel={t("jobs.columns.workflow", "Workflow")}
            disabled={!workflowsReady}
            onChange={(value) => onChange({ workflow_id: value })}
            options={workflowChoices}
            value={draft.workflow_id}
          />
          {workflowsLoading ? <p>{t("react.projects.loading_project_workflows", "Loading project workflows...")}</p> : null}
          {workflowsError ? <div role="alert">
            <p>{t("react.projects.project_workflow_inventory_is_unavailable_existing_selection_is_preserved_new_routes_require_a_complete_invent", "Project workflow inventory is unavailable. Existing selection is preserved; new routes require a complete inventory.")}</p>
            <button className="fs-button sm" onClick={onRetryWorkflows} type="button">{t("react.projects.retry_project_workflows", "Retry project workflows")}</button>
          </div> : null}
        </div>
      </div>
      <div className="mt-3 fs-surface-radius border border-subtle fs-surface-inset p-3">
        <p className="text-xs uppercase tracking-[0.15em] fs-ink-secondary">
          {usesEmailRecipientTarget ? t("react.projects.recipient", "Recipient") : t("settings.notifications.columns.channel", "Channel")}
        </p>
        <RouteTargetEditor
          channels={channels}
          channelsQuery={channelsQuery}
          draft={draft}
          error={error}
          onChange={onChange}
          projectId={projectId}
          provider={provider}
          usesEmailApRecipient={usesEmailRecipientTarget}
        />
      </div>
      <DsCheckbox
        checked={draft.enabled}
        onChange={(checked) => onChange({ enabled: checked })}
      >
        {t("monitoring.flags.enabled", "Enabled")} </DsCheckbox>
    </DsDialog>
  );
}
