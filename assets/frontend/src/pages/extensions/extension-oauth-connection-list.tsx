import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useRef, useState } from "react";


import { InlineConfirm } from "@/components/shared/inline-confirm";
import { focusAfterRequestLoading } from "@/components/shared/request-loading-focus";
import { isHttpError } from "@/lib/http/errors";
import "@/pages/extensions/extension-manifest.css";
import {
  disconnectExtensionOAuthConnection,
  retryExtensionOAuthPostConnect,
  startExtensionOAuthAuthorization,
  type ExtensionOAuthScope,
  type OAuthConfigurationView,
} from "@/pages/extensions/extension-oauth-api";
import type { OAuthProviderDeclaration } from "@/pages/extensions/extension-oauth-types";

import {
  authorizationStartGuidance,
  connectionDomId,
  isConfigurationReady,
  oauthStatusLabel,
  postConnectRetryGuidance,
  record,
  text,
  withInstalledRelease,
} from "./extension-oauth-setup-model";
type ConnectionListProps = {
  canManage: boolean;
  configurations: readonly OAuthConfigurationView[];
  connections: readonly Record<string, unknown>[];
  ownedConnectionTypeId: string;
  ownedIntentId: string;
  legacyOwnershipIsExact: boolean;
  onAuthorization?: (authorizationUrl: string) => void;
  onConnectionsChanged?: () => void | Promise<void>;
  onNavigatePage: (pageId: string, anchor?: string) => void;
  onTestConnection?: (connection: Record<string, unknown>) => void | Promise<void>;
  provider: OAuthProviderDeclaration;
  scope: ExtensionOAuthScope;
  returnPageId: string;
  focusConnectionId?: string;
};

type OAuthConnectionRowView = {
  configuration: OAuthConfigurationView | undefined;
  connectionId: string;
  disconnectRecoveryAvailable: boolean;
  displayName: string;
  helpAnchor: string | undefined;
  lifecycle: string;
  reconnectable: boolean;
  retryablePostConnect: boolean;
};

function oauthConnectionRowView(
  row: Record<string, unknown>,
  configurations: readonly OAuthConfigurationView[],
  provider: OAuthProviderDeclaration,
  currentTime: number,
): OAuthConnectionRowView {
  const connectionId = text(row.connection_id);
  const oauth = record(record(row.connection_config).oauth);
  const lifecycle = text(oauth.lifecycle_status) || "pending";
  const disconnectLeaseExpiresAt = Date.parse(
    text(oauth.disconnect_lease_expires_at),
  );
  const disconnectRecoveryAvailable =
    lifecycle === "disconnecting" &&
    (!Number.isFinite(disconnectLeaseExpiresAt) ||
      disconnectLeaseExpiresAt <= currentTime);
  const errorCode = text(oauth.last_error_code);
  const configuration = configurations.find(
    (candidate) =>
      candidate.configuration_id === text(row.oauth_configuration_id),
  );
  const retryableLifecycle =
    lifecycle === "post_connect_pending" ||
    (lifecycle === "needs_attention" &&
      ["internal_failure", "malformed_response", "provider_unavailable"].includes(
        errorCode,
      ));
  return {
    configuration,
    connectionId,
    disconnectRecoveryAvailable,
    displayName: text(row.display_name) || connectionId,
    helpAnchor: errorCode
      ? provider.error_help_anchors?.[errorCode]
      : undefined,
    lifecycle,
    reconnectable: [
      "authorization_required",
      "disconnected",
      "reconnect_required",
    ].includes(lifecycle),
    retryablePostConnect:
      retryableLifecycle && isConfigurationReady(configuration),
  };
}

function connectionNotices(
  view: OAuthConnectionRowView,
  canManage: boolean, t: I18nContextValue["t"] = fallbackContext.t
): string[] {
  return [
    connectionVerificationNotice(view, canManage, t),
    connectionAttentionNotice(view, canManage, t),
    connectionAuthorizationNotice(view, canManage, t),
    canManage ? "" : connectionViewerReconnectNotice(view, t),
    connectionDisconnectingNotice(view, canManage, t),
  ].filter((notice): notice is string => Boolean(notice));
}

function connectionVerificationNotice(view: OAuthConnectionRowView, canManage: boolean, t: I18nContextValue["t"] = fallbackContext.t) {
  if (!view.retryablePostConnect) return "";
  return canManage
    ? t("react.extensions.verification_needs_attention_retry_when_the_provider_is_available", "Verification needs attention. Retry when the provider is available.")
    : t("react.extensions.verification_needs_attention_a_project_manager_must_retry_verification", "Verification needs attention. A project manager must retry verification.");
}

function connectionAttentionNotice(view: OAuthConnectionRowView, canManage: boolean, t: I18nContextValue["t"] = fallbackContext.t) {
  if (view.lifecycle !== "needs_attention" || view.retryablePostConnect) return "";
  return canManage
    ? t("react.extensions.repair_and_save_this_connection_s_configuration_in_setup_before_trying_again", "Repair and save this connection's configuration in Setup before trying again.")
    : t("react.extensions.this_connection_needs_attention_a_project_manager_must_repair_and_save_its_configuration_in_setup", "This connection needs attention. A project manager must repair and save its configuration in Setup.");
}

function connectionAuthorizationNotice(view: OAuthConnectionRowView, canManage: boolean, t: I18nContextValue["t"] = fallbackContext.t) {
  if (view.lifecycle !== "authorization_required") return "";
  return canManage ? t("react.extensions.authorization_is_required_again", "Authorization is required again.") : t("react.extensions.a_project_manager_must_reconnect_this_account", "A project manager must reconnect this account.");
}

/** What a viewer reads while a project manager has to reconnect the account. */
function connectionViewerReconnectNotice(view: OAuthConnectionRowView, t: I18nContextValue["t"] = fallbackContext.t) {
  if (view.lifecycle === "reconnect_required") {
    return t("react.extensions.a_project_manager_must_reconnect_this_account", "A project manager must reconnect this account.");
  }
  if (view.lifecycle === "disconnected") {
    return t("react.extensions.a_project_manager_can_reconnect_this_account", "A project manager can reconnect this account.");
  }
  return "";
}

function connectionDisconnectingNotice(view: OAuthConnectionRowView, canManage: boolean, t: I18nContextValue["t"] = fallbackContext.t) {
  if (!canManage || view.lifecycle !== "disconnecting") return "";
  return view.disconnectRecoveryAvailable
    ? t("react.extensions.disconnect_was_interrupted_resume_it_to_finish_revocation_and_local_cleanup", "Disconnect was interrupted. Resume it to finish revocation and local cleanup.")
    : t("react.extensions.disconnect_in_progress_this_connection_is_temporarily_read_only", "Disconnect in progress. This connection is temporarily read-only.");
}

function OAuthConnectionActions({
  busy,
  canManage,
  onDisconnect,
  onReconnect,
  onRetry,
  onTest,
  row,
  view,
}: Readonly<{
  busy: boolean;
  canManage: boolean;
  onDisconnect: (row: Record<string, unknown>, recovery?: boolean) => void;
  onReconnect: (row: Record<string, unknown>) => void;
  onRetry: (row: Record<string, unknown>) => void;
  onTest?: (row: Record<string, unknown>) => void;
  row: Record<string, unknown>;
  view: OAuthConnectionRowView;
}>) {
  const { t } = useI18n();
  if (!canManage) return null;
  if (view.disconnectRecoveryAvailable) {
    return (
      <button
        aria-busy={busy}
        aria-label={t("react.extensions.resume_disconnect_value0", "Resume disconnect {value0}", { value0: view.displayName })}
        className="fs-button sm"
        disabled={busy}
        onClick={() => onDisconnect(row, true)}
        type="button"
      >
        {busy ? t("react.extensions.resuming", "Resuming…") : t("react.extensions.resume_disconnect", "Resume disconnect")}
      </button>
    );
  }
  if (view.lifecycle === "disconnecting") return null;
  return (
    <div className="fs-manifest-actions">
      {onTest && view.lifecycle === "connected" ? (
        <button
          aria-label={t("react.extensions.test_value0", "Test {value0}", { value0: view.displayName })}
          className="fs-button sm"
          disabled={busy}
          onClick={() => onTest(row)}
          type="button"
        >
          {t("react.extensions.test", "Test")}</button>
      ) : null}
      {view.retryablePostConnect ? (
        <button
          aria-busy={busy}
          className="fs-button sm primary"
          disabled={busy}
          onClick={() => onRetry(row)}
          type="button"
        >
          {busy ? t("react.extensions.retrying", "Retrying…") : t("settings.watchdog.retry", "Retry")}
        </button>
      ) : null}
      {view.reconnectable ? (
        <button
          aria-label={t("react.extensions.reconnect_value0", "Reconnect {value0}", { value0: view.displayName })}
          className="fs-button sm primary"
          disabled={busy}
          onClick={() => onReconnect(row)}
          type="button"
        >
          {t("react.extensions.reconnect", "Reconnect")}</button>
      ) : null}
      {view.lifecycle !== "disconnected" ? (
        <button
          className="fs-button sm ghost-danger"
          disabled={busy}
          onClick={() => onDisconnect(row)}
          type="button"
        >
          {t("react.extensions.disconnect", "Disconnect")}</button>
      ) : null}
    </div>
  );
}

function OAuthConnectionRow({
  busyId,
  canManage,
  configurations,
  currentTime,
  disconnectConfirm,
  focusConnectionId,
  onCancelDisconnect,
  onConfirmDisconnect,
  onDisconnect,
  onNavigatePage,
  onReconnect,
  onRetry,
  onTest,
  provider,
  row,
}: Readonly<{
  busyId: string;
  canManage: boolean;
  configurations: readonly OAuthConfigurationView[];
  currentTime: number;
  disconnectConfirm: { connectionId: string; error: string } | null;
  focusConnectionId?: string;
  onCancelDisconnect: () => void;
  onConfirmDisconnect: (row: Record<string, unknown>) => void;
  onDisconnect: (row: Record<string, unknown>, recovery?: boolean) => void;
  onNavigatePage: (pageId: string, anchor?: string) => void;
  onReconnect: (row: Record<string, unknown>) => void;
  onRetry: (row: Record<string, unknown>) => void;
  onTest?: (row: Record<string, unknown>) => void;
  provider: OAuthProviderDeclaration;
  row: Record<string, unknown>;
}>) {
  const { t } = useI18n();
  const view = oauthConnectionRowView(row, configurations, provider, currentTime);
  const busy = busyId === view.connectionId;
  const rowClass = focusConnectionId === view.connectionId
    ? "fs-manifest-row is-focused"
    : "fs-manifest-row";
  if (disconnectConfirm?.connectionId === view.connectionId) {
    // Reversible, but reconnecting means authorizing again: the row is
    // replaced in place by its confirmation, and comes back on No.
    return (
      <div
        aria-label={t("react.extensions.connection_value0", "Connection {value0}", { value0: view.displayName })}
        className={rowClass}
        id={connectionDomId(view.connectionId)}
        tabIndex={-1}
      >
        <InlineConfirm
          confirmLabel={t("react.extensions.disconnect_account", "Disconnect account")}
          error={disconnectConfirm.error}
          onCancel={onCancelDisconnect}
          onConfirm={() => onConfirmDisconnect(row)}
          pending={busy}
          pendingLabel={t("react.extensions.disconnecting", "Disconnecting…")}
          question={t("react.extensions.manual_disconnect_value0_reconnecting_needs_authorization_again", "Disconnect {value0}? Reconnecting needs authorization again.", { value0: view.displayName })}
          tone="danger"
        />
      </div>
    );
  }
  return (
    <div
      aria-label={t("react.extensions.connection_value0", "Connection {value0}", { value0: view.displayName })}
      className={rowClass}
      id={connectionDomId(view.connectionId)}
      tabIndex={-1}
    >
      <div className="fs-manifest-row-copy">
        <p className="fs-manifest-row-name">{view.displayName}</p>
        <p className="fs-manifest-row-detail">
          {view.configuration
            ? t("react.extensions.configuration_value0", "Configuration: {value0}", { value0: text(view.configuration.display_name) || t("react.extensions.oauth_final_unnamed", "Unnamed configuration") })
            : t("react.extensions.configuration_unavailable", "Configuration unavailable")}
        </p>
        <p className="fs-manifest-row-detail">{oauthStatusLabel(view.lifecycle, t)}</p>
        {connectionNotices(view, canManage, t).map((notice) => (
          <p
            aria-live={
              view.lifecycle === "disconnecting" ? "polite" : undefined
            }
            className="fs-manifest-notice"
            key={notice}
          >
            {notice}
          </p>
        ))}
        {view.helpAnchor && provider.help_page_id ? (
          <button
            className="fs-manifest-link"
            onClick={() =>
              onNavigatePage(provider.help_page_id || "", view.helpAnchor)
            }
            type="button"
          >
            {t("react.extensions.open_troubleshooting_help", "Open troubleshooting help")}</button>
        ) : null}
      </div>
      <OAuthConnectionActions
        busy={busy}
        canManage={canManage}
        onDisconnect={onDisconnect}
        onReconnect={onReconnect}
        onRetry={onRetry}
        onTest={onTest}
        row={row}
        view={view}
      />
    </div>
  );
}

export function ConnectionList({
  canManage,
  configurations,
  connections,
  ownedConnectionTypeId,
  ownedIntentId,
  legacyOwnershipIsExact,
  onAuthorization,
  onConnectionsChanged,
  onNavigatePage,
  onTestConnection,
  provider,
  returnPageId,
  focusConnectionId,
  scope,
}: Readonly<ConnectionListProps>) {
  const { t } = useI18n();
  const focusedConnectionKeyRef = useRef("");
  const [busyId, setBusyId] = useState("");
  const [disconnectConfirm, setDisconnectConfirm] = useState<{ connectionId: string; error: string } | null>(null);
  const [clockNow, setClockNow] = useState(() => Date.now());
  const [error, setError] = useState("");
  const [retryHelpTarget, setRetryHelpTarget] = useState<{
    pageId: string;
    anchor?: string;
  } | null>(null);
  const owned = connections.filter((row) => {
    const oauth = record(record(row.connection_config).oauth);
    const storedIntentId = text(oauth.authorization_intent_id);
    const storedIntentIsDeclared = provider.authorization_intents.some(
      (intent) => intent.intent_id === storedIntentId,
    );
    return Boolean(ownedConnectionTypeId && ownedIntentId)
      && text(row.extension_install_id) === scope.installId
      && text(row.oauth_provider_id) === scope.providerId
      && text(row.connection_type) === ownedConnectionTypeId
      && (
        storedIntentIsDeclared
          ? storedIntentId === ownedIntentId
          : legacyOwnershipIsExact
      );
  });
  const currentTime = clockNow;
  const nextDisconnectLeaseDeadline = Math.min(
    ...owned
      .filter((row) => text(record(record(row.connection_config).oauth).lifecycle_status) === "disconnecting")
      .map((row) => Date.parse(text(record(record(row.connection_config).oauth).disconnect_lease_expires_at)))
      .filter((deadline) => Number.isFinite(deadline) && deadline > currentTime),
  );
  const ownsFocusedConnection = Boolean(
    focusConnectionId
    && owned.some((row) => text(row.connection_id) === focusConnectionId),
  );

  useEffect(() => {
    if (!focusConnectionId || !ownsFocusedConnection) {
      focusedConnectionKeyRef.current = "";
      return;
    }
    const focusKey = `${scope.projectId}:${scope.installId}:${focusConnectionId}`;
    if (focusedConnectionKeyRef.current === focusKey) return;
    return focusAfterRequestLoading(
      globalThis.document?.getElementById(connectionDomId(focusConnectionId)) ?? null,
      () => { focusedConnectionKeyRef.current = focusKey; },
    );
  }, [focusConnectionId, ownsFocusedConnection, scope.installId, scope.projectId]);

  useEffect(() => {
    if (!Number.isFinite(nextDisconnectLeaseDeadline)) return undefined;
    const delay = Math.min(
      Math.max(nextDisconnectLeaseDeadline - Date.now() + 1, 1),
      60_000,
    );
    const timer = globalThis.setTimeout(() => setClockNow(Date.now()), delay);
    return () => globalThis.clearTimeout(timer);
  }, [currentTime, nextDisconnectLeaseDeadline]);

  async function retry(row: Record<string, unknown>) {
    const connectionId = text(row.connection_id);
    const configurationId = text(row.oauth_configuration_id);
    const connectionType = text(row.connection_type);
    if (!connectionId || !configurationId || !connectionType) {
      return;
    }
    setBusyId(connectionId);
    setError("");
    setRetryHelpTarget(null);
    try {
      const result = await retryExtensionOAuthPostConnect(scope, connectionId, {
        configuration_id: configurationId,
        connection_type: connectionType,
      });
      await onConnectionsChanged?.();
      if (result.outcome !== "connected") {
        setError(postConnectRetryGuidance(result.outcome, t));
        const helpTarget = record(result.help_target);
        const pageId = text(helpTarget.page_id);
        if (pageId) {
          const anchor = text(helpTarget.anchor);
          setRetryHelpTarget({ pageId, ...(anchor ? { anchor } : {}) });
        }
      }
    } catch {
      setError(t("react.extensions.connection_verification_could_not_be_retried_try_again", "Connection verification could not be retried. Try again."));
    } finally {
      setBusyId("");
    }
  }

  async function disconnect(row: Record<string, unknown>, recovery = false) {
    const connectionId = text(row.connection_id);
    const configurationId = text(row.oauth_configuration_id);
    if (!connectionId || !configurationId) {
      return;
    }
    // Resuming an interrupted disconnect finishes a decision already made.
    if (!recovery) {
      setDisconnectConfirm({ connectionId, error: "" });
      return;
    }
    await performDisconnect(row, true);
  }

  async function performDisconnect(row: Record<string, unknown>, recovery = false) {
    const connectionId = text(row.connection_id);
    const configurationId = text(row.oauth_configuration_id);
    if (!connectionId || !configurationId) {
      return;
    }
    setBusyId(connectionId);
    setError("");
    setRetryHelpTarget(null);
    try {
      await disconnectExtensionOAuthConnection(scope, configurationId, connectionId);
      setDisconnectConfirm(null);
      await onConnectionsChanged?.();
    } catch (error_) {
      const recoveryConflict = recovery && isHttpError(error_) && error_.status === 409;
      // Gone already: retrying can only fail again, so refresh the list instead.
      const alreadyGone = isHttpError(error_) && error_.status === 404;
      let message = t("react.extensions.manual_connection_could_not_be_disconnected_try_again", "Connection could not be disconnected. Try again.");
      if (recoveryConflict) {
        message = t("react.extensions.manual_disconnect_is_still_in_progress_try_again_after_the_recovery_lease_expires", "Disconnect is still in progress. Try again after the recovery lease expires.");
      } else if (alreadyGone) {
        message = t("react.extensions.manual_this_connection_no_longer_exists_the_list_was_refreshed", "This connection no longer exists. The list was refreshed.");
      }
      if (alreadyGone) {
        setDisconnectConfirm(null);
        setError(message);
        try {
          await onConnectionsChanged?.();
        } catch {
          // The explanation must survive a best-effort refresh failure.
        }
        return;
      }
      if (recovery) {
        setError(message);
      } else {
        // The reason stays beside the actions it restored.
        setDisconnectConfirm({ connectionId, error: message });
      }
      if (recoveryConflict) {
        try {
          await onConnectionsChanged?.();
        } catch {
          // The stable recovery alert must survive a best-effort refresh failure.
        }
      }
    } finally {
      setBusyId("");
    }
  }

  async function testConnection(row: Record<string, unknown>) {
    const connectionId = text(row.connection_id);
    if (!connectionId || !onTestConnection) {
      return;
    }
    setBusyId(connectionId);
    setError("");
    setRetryHelpTarget(null);
    try {
      await onTestConnection(row);
      await onConnectionsChanged?.();
    } catch {
      setError(t("react.extensions.connection_test_could_not_be_completed_try_again", "Connection test could not be completed. Try again."));
    } finally {
      setBusyId("");
    }
  }

  async function reconnect(row: Record<string, unknown>) {
    const connectionId = text(row.connection_id);
    const configurationId = text(row.oauth_configuration_id);
    const connectionType = text(row.connection_type);
    const displayName = text(row.display_name) || connectionId;
    const ownedIntentIsDeclared = provider.authorization_intents.some(
      (intent) => intent.intent_id === ownedIntentId,
    );
    const intentId = ownedIntentIsDeclared ? ownedIntentId : "";
    const configuration = configurations.find(
      (candidate) => candidate.configuration_id === configurationId,
    );
    if (
      !connectionId
      || !connectionType
      || !displayName
      || !intentId
      || !isConfigurationReady(configuration)
    ) {
      setError(
        intentId
          ? t("react.extensions.this_connection_cannot_be_reauthorized_until_its_configuration_is_ready", "This connection cannot be reauthorized until its configuration is ready.")
          : t("react.extensions.this_page_does_not_identify_an_exact_authorization_intent", "This page does not identify an exact authorization intent."),
      );
      return;
    }
    setBusyId(connectionId);
    setError("");
    setRetryHelpTarget(null);
    try {
      const result = await withInstalledRelease(scope, configurationId, () => startExtensionOAuthAuthorization(scope, {
        configuration_id: configurationId,
        connection_type: connectionType,
        intent_id: intentId,
        display_name: displayName,
        return_page_id: returnPageId,
        intent_inputs: {},
        existing_connection_id: connectionId,
      }));
      if (!result.authorization_url) {
        throw new Error("missing_authorization_url");
      }
      (onAuthorization || ((url: string) => globalThis.window.location.assign(url)))(result.authorization_url);
    } catch (error_) {
      setError(authorizationStartGuidance(
        error_,
        t("react.extensions.oauth_final_restart_authorization", "Reauthorization could not be started. Check the configuration and try again."), t
      ));
    } finally {
      setBusyId("");
    }
  }

  if (!owned.length) {
    return null;
  }

  return (
    <section className="fs-manifest-list" aria-label={t("react.extensions.oauth_connections", "OAuth connections")}>
      <h3 className="fs-manifest-section-title">{t("project_detail.connections.title", "Connections")}</h3>
      {owned.map((row) => (
        <OAuthConnectionRow
          busyId={busyId}
          canManage={canManage}
          configurations={configurations}
          currentTime={currentTime}
          disconnectConfirm={disconnectConfirm}
          focusConnectionId={focusConnectionId}
          key={text(row.connection_id)}
          onCancelDisconnect={() => setDisconnectConfirm(null)}
          onConfirmDisconnect={(target) => void performDisconnect(target)}
          onDisconnect={(target, recovery) => void disconnect(target, recovery)}
          onNavigatePage={onNavigatePage}
          onReconnect={(target) => void reconnect(target)}
          onRetry={(target) => void retry(target)}
          onTest={
            onTestConnection
              ? (target) => void testConnection(target)
              : undefined
          }
          provider={provider}
          row={row}
        />
      ))}
      {error ? (
        <div className="fs-manifest-list">
          <p className="fs-field-helper error" role="alert">{error}</p>
          {retryHelpTarget ? (
            <button
              className="fs-manifest-link"
              onClick={() => onNavigatePage(retryHelpTarget.pageId, retryHelpTarget.anchor)}
              type="button"
            >
              {t("react.extensions.open_retry_help", "Open retry help")}</button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
