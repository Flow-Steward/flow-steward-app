import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { objectOrEmpty, stringFrom } from "@/pages/extensions/extension-manifest-utils";

import { InlineConfirm } from "@/components/shared/inline-confirm";
import "@/pages/extensions/extension-manifest.css";

import { type ManifestRenderContext } from "./extension-manifest-render-model";
type ConnectionStatus = {
  label: string;
  guidance: string;
};

function oauthConnectionStatus(
  row: Record<string, unknown>,
  oauth: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t
): ConnectionStatus {
  const lifecycle = stringFrom(oauth.lifecycle_status, "pending").toLowerCase();
  const health = stringFrom(row.health_status, "unknown").toLowerCase();
  const runtime = stringFrom(row.runtime_status, "unknown").toLowerCase();
  const reconnectReason = stringFrom(oauth.reconnect_reason).toLowerCase();
  const lastError = stringFrom(row.last_error);

  if (lifecycle === "pending") {
    return {
      label: t("react.extensions.authorization_not_finished", "Authorization not finished"),
      guidance: t("react.extensions.display_finish_authorization_then_retry", "Finish authorization, then retry."),
    };
  }
  if (lifecycle === "reconnect_required") {
    return {
      label: t("react.extensions.reconnect_required", "Reconnect required"),
      guidance: reconnectReason
        ? t("react.extensions.display_reconnect_declared_access", "Reconnect and approve the access declared by this extension.")
        : t("react.extensions.display_reconnect_external_account", "Reconnect the external account you want to use."),
    };
  }
  if (lifecycle === "connected" && health === "degraded" && runtime === "unavailable" && lastError) {
    return { label: t("react.extensions.provider_unavailable", "Provider unavailable"), guidance: t("react.extensions.display_use_extension_help", "Use the extension help for recovery steps, then retry.") };
  }
  if (lifecycle === "connected" && health === "healthy" && runtime === "ready") {
    return { label: t("react.extensions.connected_and_ready", "Connected and ready"), guidance: "" };
  }
  if (lifecycle === "connected" && health === "degraded") {
    return {
      label: t("react.extensions.temporarily_unavailable", "Temporarily unavailable"),
      guidance: t("react.extensions.display_try_later_administrator", "Try again later. If the problem continues, ask an administrator for help."),
    };
  }
  if (lifecycle === "disconnected") {
    return { label: t("react.extensions.disconnected", "Disconnected"), guidance: t("react.extensions.display_reconnect_to_use_account", "Reconnect to use this account.") };
  }
  return {
    label: t("react.extensions.connection_unavailable", "Connection unavailable"),
    guidance: t("react.extensions.display_retry_later_or_administrator", "Retry later or ask an administrator for help."),
  };
}

function renderConnectionRowStatus({
  connectionStatus,
  environment,
  health,
  lifecycle,
  oauthConnection,
  rowId,
  runtime,
}: Readonly<{
  connectionStatus: ReturnType<typeof oauthConnectionStatus> | null;
  environment: unknown;
  health: string;
  lifecycle: string;
  oauthConnection: boolean;
  rowId: string;
  runtime: string;
}>) {
  if (connectionStatus) {
    return (
      <div aria-live="polite" className="fs-manifest-row-detail">
        <p>{connectionStatus.label}</p>
        {connectionStatus.guidance ? <p>{connectionStatus.guidance}</p> : null}
      </div>
    );
  }
  return (
    <p className="fs-manifest-row-detail">
      {oauthConnection
        ? `${lifecycle} · ${health} · ${runtime}`
        : stringFrom(environment || rowId)}
    </p>
  );
}

function renderOauthConnectionActions({
  accountLabel,
  component,
  context,
  lifecycle,
  oauthConnection,
  oauthIntent,
  oauthRetry,
  row,
}: Readonly<{
  accountLabel: string;
  component: Record<string, unknown>;
  context: ManifestRenderContext;
  lifecycle: string;
  oauthConnection: boolean;
  oauthIntent: string;
  oauthRetry: boolean;
  row: Record<string, unknown>;
}>, t: I18nContextValue["t"] = fallbackContext.t) {
  if (!oauthConnection) return null;
  return (
    <>
      {oauthIntent ? (
        <button
          aria-label={`${oauthRetry ? t("settings.watchdog.retry", "Retry") : t("react.extensions.reconnect", "Reconnect")} ${accountLabel}`}
          className="fs-button sm primary"
          disabled={context.busy}
          onClick={() => context.onConnectionOAuth(component, row, oauthIntent)}
          type="button"
        >
          {oauthRetry ? t("settings.watchdog.retry", "Retry") : t("react.extensions.reconnect", "Reconnect")}
        </button>
      ) : null}
      {lifecycle === "connected" ? (
        <button
          aria-label={t("react.extensions.disconnect_value0", "Disconnect {value0}", { value0: accountLabel })}
          className="fs-button sm"
          disabled={context.busy}
          onClick={() => context.onConnectionOAuthDisconnect(component, row)}
          type="button"
        >{t("react.registry.disconnect", "Disconnect")}</button>
      ) : null}
    </>
  );
}

export function renderConnectionRow({
  component,
  connectIntent,
  context,
  oauthConnection,
  reconnectIntent,
  row,
  selectedId,
}: Readonly<{
  component: Record<string, unknown>;
  connectIntent: string;
  context: ManifestRenderContext;
  oauthConnection: boolean;
  reconnectIntent: string;
  row: Record<string, unknown>;
  selectedId: string;
}>, t: I18nContextValue["t"] = fallbackContext.t) {
  const rowId = stringFrom(row.connection_id);
  const selected = rowId === selectedId;
  const oauth = objectOrEmpty(objectOrEmpty(row.connection_config).oauth);
  const connectedEmail = stringFrom(oauth.connected_email);
  const lifecycle = stringFrom(oauth.lifecycle_status, "pending");
  const oauthIntent = lifecycle === "pending" ? connectIntent : reconnectIntent;
  const oauthRetry = lifecycle === "pending";
  const health = stringFrom(row.health_status, t("project_detail.connections.status.unknown", "unknown"));
  const runtime = stringFrom(row.runtime_status, t("project_detail.connections.status.unknown", "unknown"));
  const connectionStatus = oauthConnection
    ? oauthConnectionStatus(row, oauth, t)
    : null;
  const displayName = stringFrom(
    row.display_name || row.connection_type || rowId,
  );
  const accountLabel =
    connectedEmail && connectedEmail !== displayName
      ? `${displayName} (${connectedEmail})`
      : displayName;
  const disconnect = context.pendingDisconnect;
  if (disconnect?.connectionId === rowId) {
    // The row is replaced in place by its confirmation, and comes back on No.
    return (
      <div className="fs-manifest-row" key={rowId}>
        <InlineConfirm
          confirmLabel={t("react.extensions.disconnect_account", "Disconnect account")}
          error={disconnect.error}
          onCancel={() => context.onConnectionOAuthDisconnectCancel?.()}
          onConfirm={() => context.onConnectionOAuthDisconnectConfirm?.(component, row)}
          pending={disconnect.pending}
          pendingLabel={t("react.extensions.disconnecting", "Disconnecting…")}
          question={t("react.extensions.manual_disconnect_value0_reconnecting_needs_authorization_again", "Disconnect {value0}? Reconnecting needs authorization again.", { value0: accountLabel })}
          tone="danger"
        />
      </div>
    );
  }
  return (
    <div
      className="fs-manifest-row"
      key={rowId}
    >
      <div className="fs-manifest-row-copy">
        <p className="fs-manifest-row-name">{displayName}</p>
        {oauthConnection && connectedEmail ? (
          <p className="fs-manifest-row-detail">{connectedEmail}</p>
        ) : null}
        {renderConnectionRowStatus({
          connectionStatus,
          environment: row.environment,
          health,
          lifecycle,
          oauthConnection,
          rowId,
          runtime,
        })}
      </div>
      <fieldset
        aria-label={t("react.extensions.value0_actions", "{value0} actions", { value0: accountLabel })}
        className="fs-manifest-actions"
      >
        <button
          aria-label={oauthConnection ? t("react.extensions.edit_value0", "Edit {value0}", { value0: accountLabel }) : undefined}
          className="fs-button sm"
          disabled={context.busy}
          onClick={() => context.onConnectionSelect(component, row)}
          type="button"
        >
          {selected ? t("react.extensions.selected", "Selected") : t("extensions.repeater.edit", "Edit")}
        </button>
        {renderOauthConnectionActions({
          accountLabel,
          component,
          context,
          lifecycle,
          oauthConnection,
          oauthIntent,
          oauthRetry,
          row,
        }, t)}
        <button
          aria-label={oauthConnection ? t("react.extensions.remove_value0", "Remove {value0}", { value0: accountLabel }) : undefined}
          className="fs-button sm ghost-danger"
          disabled={context.busy}
          onClick={() => context.onConnectionDelete(component, row)}
          type="button"
        >
          {oauthConnection ? t("extensions.repeater.remove", "Remove") : t("settings.model_providers.delete", "Delete")}
        </button>
      </fieldset>
    </div>
  );
}
