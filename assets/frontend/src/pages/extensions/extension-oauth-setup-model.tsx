import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { type ReactNode } from "react";


import { isHttpError } from "@/lib/http/errors";
import "@/pages/extensions/extension-manifest.css";
import {
  reconcileExtensionOAuthConfigurationRelease,
  type ExtensionOAuthScope,
  type OAuthConfigurationImpact,
  type OAuthConfigurationView,
  type OAuthPostConnectRetryResult,
} from "@/pages/extensions/extension-oauth-api";
import type {
  OAuthConnectionTypeDeclaration,
  OAuthProviderDeclaration,
} from "@/pages/extensions/extension-oauth-types";

export type ExtensionOAuthSetupMode = "setup" | "connections" | "setup_and_connections";

export type ExtensionOAuthSetupProps = {
  canManage: boolean;
  connectionTypes: readonly OAuthConnectionTypeDeclaration[];
  connections: readonly Record<string, unknown>[];
  connectionsQuery?: {
    isError: boolean;
    isLoading: boolean;
    refetch: () => void | Promise<unknown>;
  };
  installId: string;
  allowedConnectionTypeIds?: readonly string[];
  allowedIntentIds?: readonly string[];
  connectionOwners?: readonly {
    intentId: string;
    pageId: string;
  }[];
  /**
   * "setup" edits the provider's app configuration, "connections" connects
   * accounts, and "setup_and_connections" does both on a setup page that also
   * declares its provider's connection form.
   */
  mode?: ExtensionOAuthSetupMode;
  onAuthorization?: (authorizationUrl: string) => void;
  onConnectionsChanged?: () => void | Promise<void>;
  onNavigatePage: (pageId: string, anchor?: string) => void;
  onTestConnection?: (connection: Record<string, unknown>) => void | Promise<void>;
  projectId: string;
  provider: OAuthProviderDeclaration;
  providerSetupReady?: boolean;
  returnPageId: string;
  focusConnectionId?: string;
};

export function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

const INVALID_OAUTH_CONNECTION_DEFINITION_CODES = new Set([
  "invalid_oauth_connection_type",
  "invalid_oauth_provider_contract",
  "invalid_oauth_return_page",
  "unknown_oauth_connection_type",
  "unknown_oauth_intent",
]);

const OAUTH_RECONNECT_IDENTITY_ERRORS = new Set([
  "oauth_reconnect_subject_mismatch",
  "oauth_reconnect_subject_unavailable",
]);

export function errorCode(error: unknown): string {
  const payload = isHttpError(error) ? record(error.payload) : {};
  return text(payload.error) || text(payload.code);
}

/**
 * Run an OAuth action, first moving its setup to the installed extension
 * release when an update left it behind.
 *
 * Without this, authorization after an extension update failed with a bare
 * "could not be started", and deleting the whole setup was the only way out,
 * which removed the connections workflow steps were bound to. The server moves
 * the setup only when the release signs in exactly as before.
 */
export async function withInstalledRelease<T>(
  scope: ExtensionOAuthScope,
  configurationId: string,
  action: () => Promise<T>,
): Promise<T> {
  try {
    return await action();
  } catch (error_) {
    if (!configurationId || errorCode(error_) !== "oauth_release_transition_required") {
      throw error_;
    }
    await reconcileExtensionOAuthConfigurationRelease(scope, configurationId);
    return action();
  }
}

export function authorizationStartGuidance(
  error: unknown,
  fallback: string | undefined = undefined, t: I18nContextValue["t"] = fallbackContext.t
): string {
  const code = errorCode(error);
  if (code === "oauth_release_reapproval_required" || code === "oauth_release_transition_required") {
    return t("react.extensions.this_extension_update_changed_how_it_signs_in_remove_this_oauth_app_setup_and_add_it_again_then_connect_existi", "This extension update changed how it signs in. Remove this OAuth app setup and add it again, then connect; existing connections will need to be connected again.");
  }
  if (OAUTH_RECONNECT_IDENTITY_ERRORS.has(code)) {
    return t("react.extensions.this_connection_cannot_be_safely_reauthorized_create_a_new_connection_for_this_account_instead", "This connection cannot be safely reauthorized. Create a new connection for this account instead.");
  }
  if (INVALID_OAUTH_CONNECTION_DEFINITION_CODES.has(code)) {
    return t("react.extensions.this_extension_s_oauth_connection_setup_is_invalid_update_or_reinstall_the_extension_then_try_again", "This extension's OAuth connection setup is invalid. Update or reinstall the extension, then try again.");
  }
  return fallback ?? t("react.extensions.oauth_final_start", "Authorization could not be started. Check the configuration and try again.");
}

export function humanize(value: string): string {
  const label = value.replace(/[-_]+/gu, " ").trim();
  return label ? `${label.charAt(0).toUpperCase()}${label.slice(1)}` : "Value";
}

export function isConfigurationReady(configuration: OAuthConfigurationView | undefined): boolean {
  return configuration?.status === "ready_for_authorization" || configuration?.status === "verified";
}

export function configurationGuidance(status: OAuthConfigurationView["status"], t: I18nContextValue["t"] = fallbackContext.t): string {
  const messages: Record<OAuthConfigurationView["status"], string> = {
    incomplete: t("react.extensions.oauth_final_incomplete_guidance", "Incomplete — complete every required field before connecting."),
    ready_for_authorization: t("react.extensions.oauth_final_ready_guidance", "Ready for authorization."),
    verified: t("react.extensions.oauth_final_verified_guidance", "Verified and ready for authorization."),
    needs_attention: t("react.extensions.oauth_final_attention_guidance", "Needs attention — review the fields or replace the stored secret, then save."),
  };
  return messages[status];
}

export function oauthStatusLabel(status: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const labels: Record<string, string> = {
    pending: t("react.extensions.oauth_final_status_pending", "Pending"),
    connected: t("react.extensions.oauth_final_status_connected", "Connected"),
    disconnected: t("react.extensions.oauth_final_status_disconnected", "Disconnected"),
    disconnecting: t("react.extensions.oauth_final_status_disconnecting", "Disconnecting"),
    post_connect_pending: t("react.extensions.oauth_final_status_post_connect_pending", "Post connect pending"),
    needs_attention: t("react.extensions.oauth_final_status_needs_attention", "Needs attention"),
    authorization_required: t("react.extensions.oauth_final_status_authorization_needed", "Authorization required"),
    reconnect_required: t("react.extensions.oauth_final_status_reconnect_required", "Reconnect required"),
    incomplete: t("react.extensions.oauth_final_status_incomplete", "Incomplete"),
    ready_for_authorization: t("react.extensions.oauth_final_status_ready", "Ready for authorization"),
    verified: t("react.extensions.oauth_final_status_verified", "Verified"),
  };
  return labels[status] || humanize(status);
}

export function redirectRegistrationGuidance(error: unknown, t: I18nContextValue["t"] = fallbackContext.t): ReactNode {
  const payload = isHttpError(error) && error.payload && typeof error.payload === "object"
    ? error.payload as Record<string, unknown>
    : {};
  if (payload.error !== "oauth_public_base_url_unavailable") {
    return t("react.extensions.redirect_uri_could_not_be_registered_try_again", "Redirect URI could not be registered. Try again.");
  }
  if (payload.reason === "unresolvable") {
    return t("react.extensions.the_configured_public_hostname_could_not_be_resolved_from_the_application_container_check_dns_then_retry_regis", "The configured public hostname could not be resolved from the application container. Check DNS, then retry registration.");
  }
  if (payload.reason === "disallowed") {
    return t("react.extensions.the_configured_hostname_must_resolve_to_an_allowed_public_address_ask_the_installation_administrator_to_check_", "The configured hostname must resolve to an allowed public address. Ask the installation administrator to check routing and address policy, then retry registration.");
  }
  if (payload.reason !== "missing" && payload.reason !== "invalid") {
    return t("react.extensions.the_public_callback_address_could_not_be_verified_ask_the_installation_administrator_to_check_the_public_addre", "The public callback address could not be verified. Ask the installation administrator to check the public address and DNS, then retry registration.");
  }
  return (
    <>
      <p>{t("react.extensions.ask_the_installation_administrator_to_configure_public_host_and_public_port", "Ask the installation administrator to configure Public host and Public port:")}</p>
      <pre className="overflow-x-auto whitespace-pre-wrap"><code>{"./flow-steward config set --public-host <hostname> --public-port <port>\n./flow-steward config apply"}</code></pre>
      <p>{t("react.extensions.manual_fs_public_base_url_is_generated_automatically_stack_restart_does_not_apply_configuration_changes_for_https_por", "FS_PUBLIC_BASE_URL is generated automatically; stack restart does not apply configuration changes. For HTTPS port 443, the generated URL has no :443 suffix. Then retry registration.")}</p>
    </>
  );
}

export function postConnectRetryGuidance(outcome: OAuthPostConnectRetryResult["outcome"], t: I18nContextValue["t"] = fallbackContext.t): string {
  if (outcome === "needs_attention" || outcome === "provider_unavailable") {
    return t("react.extensions.connection_verification_still_needs_attention_review_the_provider_troubleshooting_guidance_and_try_again", "Connection verification still needs attention. Review the provider troubleshooting guidance and try again.");
  }
  if (outcome === "authorization_required" || outcome === "authorization_denied") {
    return t("react.extensions.connection_authorization_is_required_again_reconnect_this_account", "Connection authorization is required again. Reconnect this account.");
  }
  return t("react.extensions.connection_verification_could_not_be_completed_restart_the_connection_flow", "Connection verification could not be completed. Restart the connection flow.");
}

export type OAuthConfigurationDeletePreview = {
  scopeKey: string;
  configurationId: string;
  expectedRevision: number;
  impact: OAuthConfigurationImpact;
};

export type OAuthConfigurationSaveNotice = {
  configurationId: string;
  message: string;
  scopeKey: string;
};

export type OAuthConnectionDeclaration = {
  connectionDeclarationIsExact: boolean;
  exactAllowedConnectionTypeIds: string[];
  exactAllowedIntentIds: string[];
  legacyOwnershipIsExact: boolean;
  ownedConnectionTypeId: string;
  providerConnectionTypes: OAuthConnectionTypeDeclaration[];
};

export function uniqueTextValues(values: readonly string[] | undefined): string[] {
  return Array.from(new Set((values || []).map(text).filter(Boolean)));
}

export function selectableOAuthConfigurations(
  configurations: readonly OAuthConfigurationView[],
  mode: ExtensionOAuthSetupMode,
  providerSetupReady: boolean,
): readonly OAuthConfigurationView[] {
  if (mode === "setup") return configurations;
  if (!providerSetupReady) return [];
  return configurations.filter((configuration) => isConfigurationReady(configuration));
}

export function activeOAuthConfiguration(
  configurations: readonly OAuthConfigurationView[],
  selectedId: string,
): OAuthConfigurationView | undefined {
  if (selectedId === "new" || configurations.length === 0) return undefined;
  return configurations.find((row) => row.configuration_id === selectedId) || configurations[0];
}

export function exactOAuthConnectionOwners(
  connectionOwners: readonly { intentId: string; pageId: string }[],
) {
  return Array.from(new Map(
    connectionOwners
      .map((owner) => ({ intentId: text(owner.intentId), pageId: text(owner.pageId) }))
      .filter((owner) => owner.intentId && owner.pageId)
      .map((owner) => [JSON.stringify([owner.pageId, owner.intentId]), owner]),
  ).values());
}

export function oauthConnectionDeclaration({
  allowedConnectionTypeIds,
  allowedIntentIds,
  connectionOwners,
  connectionTypes,
  mode,
  providerId,
  returnPageId,
}: {
  allowedConnectionTypeIds?: readonly string[];
  allowedIntentIds?: readonly string[];
  connectionOwners: readonly { intentId: string; pageId: string }[];
  connectionTypes: readonly OAuthConnectionTypeDeclaration[];
  mode: ExtensionOAuthSetupMode;
  providerId: string;
  returnPageId: string;
}): OAuthConnectionDeclaration {
  const exactAllowedConnectionTypeIds = uniqueTextValues(allowedConnectionTypeIds);
  const exactAllowedIntentIds = uniqueTextValues(allowedIntentIds);
  const connectionDeclarationIsExact = mode === "setup" || (
    exactAllowedConnectionTypeIds.length === 1 && exactAllowedIntentIds.length === 1
  );
  const providerConnectionTypes = connectionTypes.filter((row) =>
    connectionDeclarationIsExact
    && row.auth.type.toLowerCase() === "oauth2"
    && row.auth.provider_id === providerId
    && exactAllowedConnectionTypeIds.includes(row.connection_type_id),
  );
  const exactConnectionOwners = exactOAuthConnectionOwners(connectionOwners);
  return {
    connectionDeclarationIsExact,
    exactAllowedConnectionTypeIds,
    exactAllowedIntentIds,
    legacyOwnershipIsExact: exactConnectionOwners.length === 1
      && text(exactConnectionOwners[0].intentId) === exactAllowedIntentIds[0]
      && text(exactConnectionOwners[0].pageId) === returnPageId,
    ownedConnectionTypeId: providerConnectionTypes.length === 1
      ? providerConnectionTypes[0].connection_type_id
      : "",
    providerConnectionTypes,
  };
}

export function controlSegment(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/gu, "-");
}

export function connectionDomId(value: string): string {
  return `extension-oauth-connection-${encodeURIComponent(value)}`;
}
