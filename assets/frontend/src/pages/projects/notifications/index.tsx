import { NotificationRouteDialog } from "./notification-route-dialog";
import { textOrEmpty, SEVERITY_OPTIONS, type RouteDraft, type RouteModalMode } from "./notification-route-model";
import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useEffect, useMemo, useState } from "react";
import {
  ROUTE_FIELD_ELEMENT_IDS,
  RouteFieldError,
  type RouteFieldId,
} from "@/pages/projects/notifications/validation";

import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Pencil, Plus, Trash2 } from "lucide-react";
import { Link } from "react-router";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  fetchProjectNotificationProviders,
  fetchProjectNotificationRoutes,
  fetchProjectNotificationWorkflows,
  fetchProjectProviderChannels,
  saveProjectNotificationRoutes,
  type NotificationEntity,
  type ProjectNotificationProvider,
  type ProviderChannel,
} from "@/pages/projects/notifications/api";
import { ignoreAsyncResult } from "@/lib/utils/async";

// Common first-party events, not an exhaustive catalog of extension/custom events.


const RECIPIENT_TARGET_KIND = "recipient";
const CHANNEL_TARGET_KIND = "channel";

function storedTargetKind(hasRecipients: boolean, hasChannel: boolean): RouteDraft["stored_target_kind"] {
  if (hasRecipients) return RECIPIENT_TARGET_KIND;
  return hasChannel ? CHANNEL_TARGET_KIND : "";
}

type RouteTargetKind = typeof RECIPIENT_TARGET_KIND | typeof CHANNEL_TARGET_KIND | "unknown";

function routeTargetKind(
  providerKey: string,
  providers: Map<string, ProjectNotificationProvider>,
): RouteTargetKind {
  const provider = providers.get(providerKey.trim());
  if (!provider) {
    return "unknown";
  }
  return provider.notification_target_kind === RECIPIENT_TARGET_KIND
    ? RECIPIENT_TARGET_KIND
    : CHANNEL_TARGET_KIND;
}

function usesEmailRecipient(
  providerKey: string,
  providers: Map<string, ProjectNotificationProvider>,
): boolean {
  return routeTargetKind(providerKey, providers) === RECIPIENT_TARGET_KIND;
}

function objectOrEmpty(value: unknown): NotificationEntity {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as NotificationEntity)
    : {};
}

function routeToDraft(route: NotificationEntity, index: number): RouteDraft {
  const routeConfig = objectOrEmpty(route.route_config);
  const recipients = Array.isArray(routeConfig.to) ? routeConfig.to : [];
  return {
    // What the route is stored as, so a save can preserve it when the provider
    // that would otherwise say is unavailable.
    stored_target_kind: storedTargetKind(Boolean(recipients.length), Boolean(routeConfig.channel_id)),
    channel_id: textOrEmpty(routeConfig.channel_id || recipients[0]),
    clientId: textOrEmpty(route.route_id) || textOrEmpty(route.route_key) || `route-${index + 1}`,
    connection_id: textOrEmpty(routeConfig.connection_id),
    display_name: textOrEmpty(route.display_name),
    enabled: route.enabled === undefined ? true : Boolean(route.enabled),
    event_type: textOrEmpty(route.event_type) || "workflow.automation.notify",
    provider_key: textOrEmpty(route.provider_key),
    route_key: textOrEmpty(route.route_key),
    severity: textOrEmpty(route.severity) || "any",
    workflow_id: textOrEmpty(route.workflow_id),
  };
}

function defaultRouteDraft(existingKeys: Set<string>): RouteDraft {
  let routeKey: string;
  do {
    routeKey = `route_${globalThis.crypto.randomUUID()}`;
  } while (existingKeys.has(routeKey));
  return {
    stored_target_kind: "",
    channel_id: "",
    clientId: routeKey,
    connection_id: "",
    display_name: "",
    enabled: true,
    event_type: "workflow.automation.notify",
    provider_key: "",
    route_key: routeKey,
    severity: "any",
    workflow_id: "",
  };
}

function draftToRoute(
  draft: RouteDraft,
  providers: Map<string, ProjectNotificationProvider>,
  t: I18nContextValue["t"],
): NotificationEntity {
  const routeKey = draft.route_key.trim();
  const providerKey = draft.provider_key.trim();
  const eventType = draft.event_type.trim();
  const channelId = draft.channel_id.trim();
  if (!routeKey) {
    throw new RouteFieldError("route_key", t("react.projects.route_key_required", "Route key is required for every notification route."));
  }
  if (!providerKey) {
    throw new RouteFieldError("provider_key", t("react.projects.route_provider_required", "Provider is required for route {route}.", { route: routeKey }));
  }
  if (!eventType) {
    throw new RouteFieldError("event_type", t("react.projects.route_event_required", "Event type is required for route {route}.", { route: routeKey }));
  }
  const kind = routeTargetKind(providerKey, providers);
  // An unknown provider cannot decide the shape. A route that already has one
  // keeps it; a new one has nothing to fall back on and must be refused rather
  // than written in whichever shape happens to be the default.
  const resolved = kind === "unknown" ? draft.stored_target_kind : kind;
  if (!resolved) {
    throw new RouteFieldError(
      "provider_key",
      t("react.projects.route_provider_unavailable", "Provider {provider} is not available in this project, so route {route} cannot be saved. Enable the provider's extension, or choose an available provider.", { provider: providerKey, route: routeKey }),
    );
  }
  const outgoingEmail = resolved === RECIPIENT_TARGET_KIND;
  if (!channelId) {
    throw new RouteFieldError(
      "channel_id",
      outgoingEmail ? t("react.projects.route_recipient_required", "Recipient email is required for route {route}.", { route: routeKey }) : t("react.projects.route_channel_required", "Channel is required for route {route}.", { route: routeKey }),
    );
  }
  return {
    display_name: draft.display_name.trim(),
    enabled: draft.enabled,
    event_type: eventType,
    provider_key: providerKey,
    route_config: outgoingEmail
      ? { connection_id: draft.connection_id.trim(), to: [channelId] }
      : { channel_id: channelId },
    route_key: routeKey,
    severity: SEVERITY_OPTIONS.includes(draft.severity) ? draft.severity : "any",
    workflow_id: draft.workflow_id.trim() || null,
  };
}

function channelCapabilitySummary(channel: ProviderChannel | null, t: I18nContextValue["t"] = fallbackContext.t) {
  if (!channel) {
    return "";
  }
  const flags: string[] = [];
  if (channel.capabilities.notify) {
    flags.push("notify");
  }
  if (channel.capabilities.human_decision) {
    flags.push(t("react.projects.human_decision", "human decision"));
  }
  return flags.join(" • ");
}

function routeAvailabilityMessage(
  draft: RouteDraft,
  channels: ProviderChannel[] | undefined,
  providers: Map<string, ProjectNotificationProvider>, t: I18nContextValue["t"] = fallbackContext.t,
): string {
  if (!draft.provider_key) {
    return t("react.projects.provider_missing", "Provider missing");
  }
  if (!draft.channel_id) {
    return usesEmailRecipient(draft.provider_key, providers)
      ? t("react.projects.recipient_missing", "Recipient missing")
      : t("react.projects.channel_missing", "Channel missing");
  }
  if (usesEmailRecipient(draft.provider_key, providers)) {
    return "";
  }
  if (!channels) {
    return "";
  }
  if (!channels.length) {
    return t("react.projects.no_channels_configured", "No channels configured");
  }
  const channel = channels.find((item) => item.channel_id === draft.channel_id) || null;
  if (!channel) {
    return t("react.projects.referenced_channel_was_deleted", "Referenced channel was deleted");
  }
  if (!channel.enabled) {
    return t("react.projects.referenced_channel_is_disabled", "Referenced channel is disabled");
  }
  return "";
}

type ProjectNotificationRoutesEditorProps = {
  projectId: string;
};

function NotificationRouteRow({
  draft,
  index,
  channels,
  providers,
  saving,
  onEdit,
  onDelete,
}: Readonly<{
  draft: RouteDraft;
  index: number;
  channels: ProviderChannel[] | undefined;
  providers: Map<string, ProjectNotificationProvider>;
  saving: boolean;
  onEdit: (index: number) => void;
  onDelete: (index: number) => void;
}>) {
  const { t } = useI18n();
  const selectedChannel =
    channels?.find((channel) => channel.channel_id === draft.channel_id) || null;
  const unavailableMessage = routeAvailabilityMessage(
    draft,
    channels,
    providers, t,
  );
  const capabilitySummary = channelCapabilitySummary(selectedChannel, t);
  return (
    <div
      className="fs-surface-radius border border-subtle fs-surface-inset p-3"
      data-testid="notification-route-row"
    >
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-full border border-subtle px-2 py-0.5 font-mono text-xs fs-ink-brand">
              {draft.route_key || `route_${index + 1}`}
            </span>
            <span className="text-xs fs-ink-tertiary">
              {draft.enabled ? t("react.projects.enabled", "enabled") : t("react.projects.disabled", "disabled")}
            </span>
            {unavailableMessage ? (
              <span className="rounded-full border border-selected fs-surface-attention px-2 py-0.5 text-xs fs-ink-attention">
                unavailable
              </span>
            ) : null}
          </div>
          <p className="mt-2 text-sm font-medium fs-ink-primary">
            {draft.display_name ||
              selectedChannel?.display_name ||
              draft.provider_key ||
              t("react.projects.notification_route", "Notification route")}
          </p>
          <div className="mt-1 flex flex-wrap gap-2 text-xs fs-ink-tertiary">
            <span>{draft.provider_key || t("react.projects.provider_not_set", "provider not set")}</span>
            <span>{draft.channel_id || t("react.projects.channel_not_set", "channel not set")}</span>
            <span>{draft.event_type || t("react.projects.event_not_set", "event not set")}</span>
            <span>{draft.severity || "any"}</span>
            {draft.workflow_id ? <span>{draft.workflow_id}</span> : null}
          </div>
          {selectedChannel ? (
            <div className="mt-2 text-xs fs-ink-tertiary">
              {selectedChannel.chat_id
                ? t("react.projects.chat_value1", "chat {value1}", { value1: String(selectedChannel.chat_id) })
                : t("react.projects.chat_not_set", "chat not set")}
              {capabilitySummary ? ` • ${capabilitySummary}` : ""}
            </div>
          ) : null}
          {unavailableMessage ? (
            <div className="mt-2 flex items-start gap-2 rounded-lg border border-selected fs-surface-attention px-2 py-2 text-xs fs-ink-attention">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" />
              <span>{unavailableMessage}</span>
            </div>
          ) : null}
        </div>
        <button
          className="fs-button secondary sm"
          onClick={() => onEdit(index)}
          type="button"
        >
          <Pencil className="h-3.5 w-3.5" />
          {t("projects.grid.action.edit", "Edit")} </button>
        <button
          aria-label={t("react.projects.remove_notification_route_value1", "Remove notification route {value1}", { value1: String(draft.route_key || index + 1) })}
          className="fs-button danger sm"
          disabled={saving}
          onClick={() => onDelete(index)}
          type="button"
        >
          <Trash2 className="h-3.5 w-3.5" />
          {t("registry.agent_detail.common.remove", "Remove")} </button>
      </div>
    </div>
  );
}

function NotificationRoutesCard({
  drafts,
  channelsByProvider,
  providers,
  loading,
  providerError,
  routeError,
  message,
  saving,
  projectId,
  onCreate,
  onEdit,
  onDelete,
  onRetry,
}: Readonly<{
  drafts: RouteDraft[];
  channelsByProvider: Map<string, ProviderChannel[]>;
  providers: Map<string, ProjectNotificationProvider>;
  loading: boolean;
  providerError: boolean;
  routeError: boolean;
  message: string;
  saving: boolean;
  projectId: string;
  onCreate: () => void;
  onEdit: (index: number) => void;
  onDelete: (index: number) => void;
  onRetry: () => void;
}>) {
  const { t } = useI18n();
  return (
    <Card className="fs-card">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="text-lg">{t("react.projects.notification_routes", "Notification routes")}</CardTitle>
            <CardDescription>
              {t("react.projects.choose_a_provider_and_a_registered_channel_or_email_recipient_instead_of_authoring_raw_route_json", "Choose a provider and a registered channel or email recipient instead of authoring raw route JSON.")} </CardDescription>
          </div>
          <button
            className="fs-button primary"
            disabled={saving || loading || providerError || routeError || !providers.size}
            id="notification-routes-add"
            onClick={onCreate}
            type="button"
          >
            <Plus className="h-4 w-4" />
            {t("react.projects.create_new", "Create new")} </button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {!loading && !providerError && !providers.size ? (
          <div className="text-sm fs-ink-secondary">
            <p>{t("react.projects.no_enabled_notification_provider_is_linked_to_this_project", "No enabled notification provider is linked to this project.")}</p>
            <p>
              {t("react.projects.enable_a_notification_capable_extension_in", "Enable a notification-capable extension in")} <Link className="underline" to="/extensions">{t("settings.extensions.title", "Extensions")}</Link>{t("react.projects.connect_it_to_this_project_there_and_it_appears_in", " and connect it to this project there; it then appears in")} <Link className="underline" to={`/projects/${encodeURIComponent(projectId)}?tab=connections`}>{t("react.projects.runtime_connections", "Runtime Connections")}</Link>{t("react.projects.provider_bindings_do_not_by_themselves_confirm_delivery_readiness", ". Provider bindings do not by themselves confirm delivery readiness.")} </p>
          </div>
        ) : null}
        {loading ? (
          <div className="flex items-center gap-2 text-sm fs-ink-secondary">
            <Loader2 className="h-4 w-4 animate-spin" />
            {t("react.projects.loading_notification_routes", "Loading notification routes...")} </div>
        ) : null}
        {providerError || routeError ? (
          <div className="flex items-start gap-2 fs-surface-radius border border-selected fs-surface-attention px-3 py-2 text-sm fs-ink-attention">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" />
            <div className="flex min-w-0 flex-1 flex-wrap items-center justify-between gap-2">
              <span>
                {providerError
                  ? t("react.projects.notification_provider_inventory_is_unavailable", "Notification provider inventory is unavailable.")
                  : t("react.projects.notification_route_inventory_is_unavailable", "Notification route inventory is unavailable.")}
              </span>
              <button
                className="rounded-lg border border-rose-200/35 px-2 py-1 text-xs text-rose-50 hover:border-rose-100/60"
                onClick={onRetry}
                type="button"
              >
                {t("react.projects.retry_notification_data", "Retry notification data")} </button>
            </div>
          </div>
        ) : null}
        {drafts.length ? (
          <div className="space-y-3">
            {drafts.map((draft, index) => (
              <NotificationRouteRow
                channels={channelsByProvider.get(draft.provider_key)}
                draft={draft}
                index={index}
                key={draft.clientId}
                onDelete={onDelete}
                onEdit={onEdit}
                providers={providers}
                saving={saving}
              />
            ))}
          </div>
        ) : (
          <p className="fs-empty-copy">
            {t("react.projects.no_notification_routes_configured_for_this_project", "No notification routes configured for this project.")} </p>
        )}
        {message ? <p className="fs-empty-copy">{message}</p> : null}
      </CardContent>
    </Card>
  );
}

export function ProjectNotificationRoutesEditor({ projectId }: Readonly<ProjectNotificationRoutesEditorProps>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [modalDraft, setModalDraft] = useState<RouteDraft | null>(null);
  const [modalIndex, setModalIndex] = useState<number | null>(null);
  const [modalMode, setModalMode] = useState<RouteModalMode | null>(null);
  const [message, setMessage] = useState("");
  // A failed submit has to answer inside the dialog: the card underneath is
  // covered by the overlay, so a message written there is invisible.
  const [modalError, setModalError] = useState<{ field: RouteFieldId | null; message: string } | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setModalDraft(null);
    setModalIndex(null);
    setModalMode(null);
    setMessage("");
  }, [projectId]);

  const routesQuery = useQuery({
    queryKey: ["projects", "notification-routes", projectId],
    queryFn: () => fetchProjectNotificationRoutes(projectId),
    enabled: Boolean(projectId),
    refetchInterval: 30_000,
  });

  const providersQuery = useQuery({
    queryKey: ["projects", "notification-providers", projectId],
    queryFn: () => fetchProjectNotificationProviders(projectId),
    enabled: Boolean(projectId),
    refetchInterval: 60_000,
  });

  const workflowsQuery = useQuery({
    queryKey: ["projects", "notification-workflows", projectId],
    queryFn: () => fetchProjectNotificationWorkflows(projectId, t),
    enabled: Boolean(projectId && modalMode),
    retry: false,
  });
  const workflowsReady = workflowsQuery.isSuccess && !workflowsQuery.isFetching;
  const workflowChoices = [
    { value: "", label: t("settings.access.limits.all_workflows", "All workflows") },
    ...(workflowsQuery.data || []).map((workflow) => ({
      value: workflow.workflow_id,
      label: workflow.display_name || workflow.workflow_id,
    })),
  ];
  if (modalDraft?.workflow_id && !workflowChoices.some((option) => option.value === modalDraft.workflow_id)) {
    workflowChoices.push({ value: modalDraft.workflow_id, label: t("react.projects.value1_unavailable", "{value1} (unavailable)", { value1: String(modalDraft.workflow_id) }) });
  }

  const providerOptions = useMemo(
    () => (Array.isArray(providersQuery.data) ? providersQuery.data : []),
    [providersQuery.data],
  );
  const serverDrafts = useMemo(
    () =>
      (routesQuery.data?.notification_routes || []).map((route, index) => routeToDraft(route, index)),
    [routesQuery.data?.notification_routes],
  );
  const providersByKey = useMemo(
    () => new Map(providerOptions.map((provider) => [provider.provider_key, provider])),
    [providerOptions],
  );
  const selectedProviderKey = textOrEmpty(modalDraft?.provider_key);
  const createInventoryReady = providersQuery.isSuccess && !providersQuery.isFetching
    && routesQuery.isSuccess && !routesQuery.isFetching
    && providersByKey.size > 0 && (!selectedProviderKey || providersByKey.has(selectedProviderKey));
  const providerKeysForChannelLookup = useMemo(() => {
    const keys = new Set<string>();
    for (const draft of serverDrafts) {
      const providerKey = textOrEmpty(draft.provider_key);
      if (providerKey && !usesEmailRecipient(providerKey, providersByKey)) {
        keys.add(providerKey);
      }
    }
    const modalProviderKey = textOrEmpty(modalDraft?.provider_key);
    if (modalProviderKey && !usesEmailRecipient(modalProviderKey, providersByKey)) {
      keys.add(modalProviderKey);
    }
    return [...keys];
  }, [modalDraft?.provider_key, providersByKey, serverDrafts]);
  const channelQueries = useQueries({
    queries: providerKeysForChannelLookup.map((providerKey) => ({
      queryKey: ["projects", "notification-provider-channels", projectId, providerKey],
      queryFn: () => fetchProjectProviderChannels(projectId, providerKey),
      enabled: Boolean(projectId && providerKey),
      retry: false,
    })),
  });
  const channelsByProvider = useMemo(() => {
    const next = new Map<string, ProviderChannel[]>();
    providerKeysForChannelLookup.forEach((providerKey, index) => {
      const rows = channelQueries[index]?.data;
      if (Array.isArray(rows)) {
        next.set(providerKey, rows);
      }
    });
    return next;
  }, [channelQueries, providerKeysForChannelLookup]);
  const modalProvider = useMemo(
    () =>
      providerOptions.find((provider) => provider.provider_key === textOrEmpty(modalDraft?.provider_key)) ||
      null,
    [modalDraft?.provider_key, providerOptions],
  );
  // A route can point at a provider the inventory no longer offers. Dropping it
  // from the list makes the select read "Select provider" over a route that does
  // have one, so it is kept and named as unavailable instead.
  const modalProviderChoices = useMemo(() => {
    const options = providerOptions.map((provider) => ({
      value: provider.provider_key,
      label: provider.display_name || provider.provider_key,
    }));
    const current = textOrEmpty(modalDraft?.provider_key);
    if (current && !options.some((option) => option.value === current)) {
      options.unshift({ value: current, label: t("react.projects.value1_unavailable", "{value1} (unavailable)", { value1: String(current) }) });
    }
    return options;
  }, [modalDraft?.provider_key, providerOptions, t]);

  const modalUsesEmailRecipient = usesEmailRecipient(
    textOrEmpty(modalDraft?.provider_key),
    providersByKey,
  );
  const modalChannels = channelsByProvider.get(textOrEmpty(modalDraft?.provider_key)) || [];
  const modalChannelsQuery = channelQueries.find(
    (_, index) => providerKeysForChannelLookup[index] === textOrEmpty(modalDraft?.provider_key),
  );

  function updateModalDraft(patch: Partial<RouteDraft>) {
    setModalDraft((current) => (current ? { ...current, ...patch } : current));
    // Once the named field is edited the complaint is about a value that no
    // longer exists; leaving the marker up reads as "still wrong".
    setModalError((current) =>
      current?.field && Object.hasOwn(patch, current.field) ? null : current,
    );
  }

  function openCreateModal() {
    setMessage("");
    setModalError(null);
    setModalDraft(defaultRouteDraft(new Set(serverDrafts.map((draft) => draft.route_key))));
    setModalIndex(null);
    setModalMode("create");
  }

  function openEditModal(index: number) {
    setMessage("");
    setModalError(null);
    setModalDraft(serverDrafts[index] ? { ...serverDrafts[index] } : null);
    setModalIndex(index);
    setModalMode("edit");
  }

  function closeRouteModal() {
    setModalError(null);
    setModalDraft(null);
    setModalIndex(null);
    setModalMode(null);
  }

  function focusRouteField(field: RouteFieldId) {
    // After the state update paints, so the control exists and carries aria-invalid.
    globalThis.requestAnimationFrame(() => {
      const element = globalThis.document?.getElementById(ROUTE_FIELD_ELEMENT_IDS[field]);
      if (element instanceof HTMLElement) {
        element.focus();
      }
    });
  }

  async function saveModalDraft() {
    if (!modalDraft || !modalMode) {
      return;
    }
    if (modalMode === "create" && !workflowsReady) {
      setModalError({ field: null, message: t("react.projects.load_the_project_workflow_inventory_before_creating_a_route", "Load the project workflow inventory before creating a route.") });
      return;
    }
    if (modalMode === "create" && !createInventoryReady) {
      setModalError({ field: "provider_key", message: t("react.projects.reload_notification_data_before_creating_a_route", "Reload notification data before creating a route.") });
      focusRouteField("provider_key");
      return;
    }
    setMessage("");
    setModalError(null);
    setSaving(true);
    try {
      const nextDrafts =
        modalMode === "edit" && modalIndex !== null
          ? serverDrafts.map((draft, index) => (index === modalIndex ? modalDraft : draft))
          : [...serverDrafts, modalDraft];
      const routes = nextDrafts.map((draft) => draftToRoute(draft, providersByKey, t));
      await saveProjectNotificationRoutes(projectId, routes);
      setMessage(modalMode === "create" ? t("react.projects.notification_route_created", "Notification route created.") : t("react.projects.notification_route_updated", "Notification route updated."));
      closeRouteModal();
      await queryClient.invalidateQueries({ queryKey: ["projects", "notification-routes", projectId] });
    } catch (error) {
      // Both a rejected field and a backend refusal belong in front of the
      // operator, not behind the overlay.
      const text = error instanceof Error ? error.message : t("react.projects.save_notification_routes_failed", "Failed to save notification routes.");
      const field = error instanceof RouteFieldError ? error.field : null;
      setModalError({ field, message: text });
      if (field) {
        focusRouteField(field);
      }
    } finally {
      setSaving(false);
    }
  }

  async function deleteChannel(index: number) {
    const routeKey = serverDrafts[index]?.route_key || `route ${index + 1}`;
    if (!globalThis.confirm(t("react.projects.confirm_delete_notification_route", "Delete notification route {route}?", { route: routeKey }))) {
      return;
    }
    setMessage("");
    setSaving(true);
    try {
      const routes = serverDrafts.filter((_, currentIndex) => currentIndex !== index).map((draft) => draftToRoute(draft, providersByKey, t));
      await saveProjectNotificationRoutes(projectId, routes);
      setMessage(t("react.projects.notification_route_deleted", "Notification route deleted."));
      await queryClient.invalidateQueries({ queryKey: ["projects", "notification-routes", projectId] });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : t("react.projects.failed_to_delete_notification_route", "Failed to delete notification route."));
    } finally {
      setSaving(false);
    }
  }

  function retryNotificationData() {
    ignoreAsyncResult(
      Promise.all([routesQuery.refetch(), providersQuery.refetch()]).then(
        () => undefined,
      ),
    );
  }

  return (
    <div className="space-y-4">
      <NotificationRoutesCard
        channelsByProvider={channelsByProvider}
        drafts={serverDrafts}
        loading={routesQuery.isFetching || providersQuery.isFetching}
        message={message}
        onCreate={openCreateModal}
        onDelete={(index) => ignoreAsyncResult(deleteChannel(index))}
        onEdit={openEditModal}
        onRetry={retryNotificationData}
        providerError={providersQuery.isError}
        projectId={projectId}
        providers={providersByKey}
        routeError={routesQuery.isError}
        saving={saving}
      />
      {modalMode && modalDraft ? (
        <NotificationRouteDialog
          channels={modalChannels}
          channelsQuery={modalChannelsQuery}
          draft={modalDraft}
          error={modalError}
          mode={modalMode}
          onChange={updateModalDraft}
          onClose={closeRouteModal}
          onSave={() => ignoreAsyncResult(saveModalDraft())}
          projectId={projectId}
          provider={modalProvider}
          providerChoices={modalProviderChoices}
          providerOptions={providerOptions}
          saving={saving}
          workflowChoices={workflowChoices}
          workflowsLoading={workflowsQuery.isFetching}
          workflowsError={workflowsQuery.isError}
          workflowsReady={workflowsReady}
          onRetryWorkflows={() => ignoreAsyncResult(workflowsQuery.refetch())}
          createInventoryReady={createInventoryReady}
          onRetryNotificationData={retryNotificationData}
          usesEmailRecipientTarget={modalUsesEmailRecipient}
        />
      ) : null}
    </div>
  );
}
