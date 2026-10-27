import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { type ReactNode } from "react";
import type { DsBadgeTone } from "@/components/shared/design-system";
import { stringFrom, objectOrEmpty } from "@/pages/extensions/extension-manifest-utils";
import { type ExtensionUninstallPlan, type ExtensionRecord } from "@/pages/extensions/api";
import type { ExtensionCatalogListing } from "@/pages/extensions/catalog-api";
import { isHttpError } from "@/lib/http/errors";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type ExtensionHealthStatus = Record<string, string>;
export type ExtensionsViewFilter = "active" | "disabled" | "all";
export type OAuthCallbackNotice = {
  kind: "error" | "success";
  message: string;
  connectionId: string;
  projectId: string;
  pageId: string;
  helpAnchor: string;
  extensionId: string;
  connectionPreserved: boolean;
};
export type ManifestActionAttempt = {
  failedPayload?: unknown;
  identity: string;
  idempotencyKey: string;
};

export function choose<T>(condition: boolean, yes: T, no: T): T {
  return condition ? yes : no;
}

export function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

export function renderChoice(condition: boolean, yes: ReactNode, no: ReactNode): ReactNode {
  return condition ? yes : no;
}

export function anyTrue(values: boolean[]): boolean {
  return values.some(Boolean);
}

export function allTrue(values: boolean[]): boolean {
  return values.every(Boolean);
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function extensionDetailBackLink(
  canAdministerPlatform: boolean,
  selectedProjectId: string,
  t: (key: string, defaultText?: string) => string = (_key, defaultText = "") => defaultText,
): { label: string; target: string } {
  if (canAdministerPlatform) {
    return { label: t("extensions.detail.back_to_extensions", "Back to extensions"), target: "/extensions" };
  }
  if (selectedProjectId) {
    return {
      label: t("extensions.detail.back_to_project", "Back to project"),
      target: `/projects/${encodeURIComponent(selectedProjectId)}`,
    };
  }
  return { label: t("extensions.detail.back_to_dashboard", "Back to dashboard"), target: "/dashboard" };
}

export function oauthCallbackMessageFor(
  notice: OAuthCallbackNotice | null,
  connection: Record<string, unknown> | undefined, t: I18nContextValue["t"] = fallbackContext.t
): string {
  if (notice?.kind === "success" && connection) {
    return t("react.extensions.value0_connected_successfully", "{value0} connected successfully.", { value0: stringFrom(connection.display_name) || t("react.extensions.oauth_final_external_account", "External account") });
  }
  if (notice?.kind === "error" && notice.connectionPreserved && connection) {
    return preservedReconnectMessage(connection, t);
  }
  return notice?.message || "";
}

export function uninstallExtensionForPlan(
  plan: ExtensionUninstallPlan | null,
  extensions: ExtensionRecord[],
  detailExtension: ExtensionRecord | null,
): ExtensionRecord | null {
  if (!plan) return null;
  return extensions.find((extension) => extension.extension_id === plan.extension_id)
    ?? choose(detailExtension?.extension_id === plan.extension_id, detailExtension, null);
}

export const AMBIGUOUS_EXTERNAL_EFFECT_CODES = new Set([
  "external_effect_timeout_unknown",
  "timeout_unknown",
  "external_effect_replay_persist_failed",
  "external_effect_confirmation_lease_lost",
]);
export const DEFINITIVE_PRE_SPAWN_CODES = new Set([
  "external_effect_pre_spawn_setup_failed",
  "invalid_connection",
]);

function oauthErrorGuidance(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  authorization_cancelled:
    t("react.extensions.oauth_final_cancelled", "Authorization was cancelled. You can try connecting the account again."),
  duplicate_connection:
    t("react.extensions.oauth_final_duplicate", "This external account is already connected to this project. Choose another account or reconnect the existing connection."),
  missing_required_scope:
    t("react.extensions.oauth_final_scope", "Required access was not granted. Reconnect and approve the permissions declared by the extension."),
  missing_refresh_token:
    t("react.extensions.oauth_final_refresh", "The provider did not return reusable access. Reconnect and approve access again."),
  provider_configuration_error:
    t("react.extensions.oauth_final_configuration_error", "The extension OAuth configuration needs attention. Open the extension Setup page."),
  authorization_required:
    t("react.extensions.oauth_final_authorization_required", "Authorization must be completed again. Reconnect the account and approve the declared access."),
  restart_required:
    t("react.extensions.oauth_final_restart", "Authorization could not be completed safely. Start the connection again."),
  provider_unavailable:
    t("react.extensions.oauth_final_unavailable", "The authorization provider is temporarily unavailable. Try again."),
  };
}

export function withExtensionActiveState(
  rows: ExtensionRecord[] | undefined,
  extensionId: string,
  active: boolean,
  state: string,
) {
  return rows?.map((row) =>
    row.extension_id === extensionId ? { ...row, active, state } : row,
  );
}

export function oauthCallbackNoticeFromSearch(
  search: string,
  hash: string,
  extensionId: string, t: I18nContextValue["t"] = fallbackContext.t
): OAuthCallbackNotice | null {
  const params = new URLSearchParams(search);
  const outcome = params.get("oauth");
  const rawConnectionId = stringFrom(params.get("oauth_connection_id"));
  const connectionId = rawConnectionId.length <= 200
    && /^[A-Za-z0-9][A-Za-z0-9._:-]*$/u.test(rawConnectionId)
    ? rawConnectionId
    : "";
  const connectionPreserved = outcome === "error"
    && Boolean(connectionId)
    && params.get("oauth_connection_preserved") === "1";
  const pageId = stringFrom(params.get("extension_page"));
  const projectId = stringFrom(params.get("project_id"));
  const rawAnchor = hash.replace(/^#/u, "");
  const helpAnchor = rawAnchor.length <= 64 && /^[a-z0-9][a-z0-9_-]*$/u.test(rawAnchor)
    ? rawAnchor
    : "";
  if (outcome === "success") {
    return {
      kind: "success",
      message: t("react.extensions.external_account_connected_successfully", "External account connected successfully."),
      connectionId,
      projectId,
      pageId,
      helpAnchor,
      extensionId,
      connectionPreserved: false,
    };
  }
  if (outcome !== "error") {
    return null;
  }
  return {
    kind: "error",
    message: oauthErrorGuidance(t)[params.get("reason") || ""]
      || t("react.extensions.authorization_failed_try_connecting_the_account_again", "Authorization failed. Try connecting the account again."),
    connectionId,
    projectId,
    pageId,
    helpAnchor,
    extensionId,
    connectionPreserved,
  };
}

function preservedReconnectMessage(connection: { display_name?: unknown } | undefined, t: I18nContextValue["t"] = fallbackContext.t): string {
  const name = connection ? stringFrom(connection.display_name) : "";
  const subject = name
    ? t("react.extensions.oauth_final_reauthorization_subject", "{name} reauthorization", { name })
    : t("react.extensions.oauth_final_reauthorization", "Reauthorization");
  return t("react.extensions.value0_did_not_complete_the_existing_connection_and_its_previous_authorization_were_not_replaced_and_remain_in", "{value0} did not complete. The existing connection and its previous authorization were not replaced and remain in their prior state.", { value0: subject });
}

export function oauthStartErrorMessage(error: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  const payload = isHttpError(error) ? objectOrEmpty(error.payload) : {};
  const technicalText = [
    error instanceof Error ? error.message : "",
    stringFrom(payload.error),
    stringFrom(payload.error_code),
    stringFrom(payload.detail),
  ].join(" ").toLowerCase();
  if (
    technicalText.includes("not configured")
    || technicalText.includes("client_id")
    || technicalText.includes("client id")
    || technicalText.includes("client secret")
    || technicalText.includes("invalid_client")
  ) {
    return t("react.extensions.the_extension_oauth_configuration_needs_attention_open_the_extension_setup_page", "The extension OAuth configuration needs attention. Open the extension Setup page.");
  }
  return t("react.extensions.authorization_couldn_t_start_try_again_or_open_the_extension_help_page", "Authorization couldn’t start. Try again or open the extension help page.");
}

export function connectionAccountTarget(row: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t): string {
  const displayName = stringFrom(row.display_name);
  const oauth = objectOrEmpty(objectOrEmpty(row.connection_config).oauth);
  const connectedEmail = stringFrom(oauth.connected_email);
  if (displayName && connectedEmail && displayName !== connectedEmail) {
    return `${displayName} (${connectedEmail})`;
  }
  return displayName || connectedEmail || t("react.extensions.oauth_final_this_account", "this account");
}

export const extensionArtifactCatalogQueryKeys = [
  ["workflows"],
  ["registry", "agents"],
  ["registry", "skills"],
  ["registry", "tools"],
  ["registry", "datasets"],
  ["dataset-query-templates"],
] as const;

export function catalogLabel(value: unknown): string {
  return stringFromUnknownValue(value)
    .trim()
    .split(/[-_]+/)
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

export function extensionCategory(value: unknown): string {
  return stringFromUnknownValue(value).trim() || "uncategorized";
}

export function extensionsViewFilterLabel(filter: ExtensionsViewFilter, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (filter === "active") return t("users.status.active", "Active");
  if (filter === "disabled") return t("react.extensions.disabled", "Disabled");
  return t("users.filter.all_states", "All");
}

export function emptyExtensionsTitle(
  hasCatalogFilters: boolean,
  viewFilter: ExtensionsViewFilter,
  disabledCount = 0, t: I18nContextValue["t"] = fallbackContext.t
): string {
  if (hasCatalogFilters) return t("react.extensions.no_matching_extensions", "No matching extensions");
  if (viewFilter === "active") {
    return disabledCount > 0 ? t("react.extensions.nothing_enabled_yet", "Nothing enabled yet") : t("react.extensions.no_active_extensions", "No active extensions");
  }
  return t("react.extensions.no_extensions_in_this_view", "No extensions in this view");
}

export function emptyExtensionsDescription(
  hasCatalogFilters: boolean,
  viewFilter: ExtensionsViewFilter,
  disabledCount = 0, t: I18nContextValue["t"] = fallbackContext.t
): string {
  if (hasCatalogFilters) return t("react.extensions.no_extensions_match_the_current_filters", "No extensions match the current filters.");
  if (viewFilter === "active") {
    // AC17-R66: a fresh install arrives with its extensions already installed and every one
    // of them disabled, and this view opens on Active. The old wording told the operator to
    // "enable an extension" while showing none, so the ones they already had were invisible
    // and the surrounding page offered installing an archive instead -- a command a Compact
    // deployment refuses. Say how many are actually here.
    if (disabledCount > 0) {
      return disabledCount === 1
        ? t("react.extensions.1_extension_is_installed_but_disabled_enable_it_to_use_it_no_download_needed", "1 extension is installed but disabled. Enable it to use it — no download needed.")
        : t("react.extensions.oauth_final_disabled_count", "{count} extensions are installed but disabled. Enable the ones you need — no download needed.", { count: disabledCount });
    }
    return t("react.extensions.enable_an_extension_or_choose_disabled_or_all_to_review_installed_extensions", "Enable an extension or choose Disabled or All to review installed extensions.");
  }
  return t("react.extensions.change_the_filter_or_install_an_extension_from_a_zip_file", "Change the filter or install an extension from a `.zip` file.");
}

export function extensionStateBadgeTone(state: string, active: boolean): DsBadgeTone {
  if (state === "broken") return "danger";
  if (active) return "green";
  return "neutral";
}

export function detailHealthStatusFromStatus(status: string | undefined): string {
  const text = stringFromUnknownValue(status).trim();
  return text === "Checking health..." || text === "Healthy" || text.startsWith("Unhealthy")
    ? text
    : "";
}

export function detailActionStatusFromStatus(status: string | undefined): string {
  const text = stringFromUnknownValue(status).trim();
  if (!text || detailHealthStatusFromStatus(text)) {
    return "";
  }
  const lower = text.toLowerCase();
  return (
    lower.startsWith("running ")
    || lower.startsWith("saving ")
    || lower.includes("successful")
    || lower.includes("completed")
    || lower === "connection saved."
    || lower === "connection created."
    || lower === "extension state saved."
  )
    ? text
    : "";
}

/** Fixed status sentences the platform emits, keyed by their exact English text. */
const EXTENSION_STATUS_LABELS: Readonly<Record<string, (t: I18nContextValue["t"]) => string>> = {
  "Checking health...": (t) => t("react.extensions.oauth_final_health_checking", "Checking health..."),
  "Healthy": (t) => t("react.extensions.oauth_final_health_healthy", "Healthy"),
  "Unhealthy": (t) => t("react.extensions.oauth_final_health_unhealthy", "Unhealthy"),
  "Broken extensions cannot be enabled.": (t) => t("react.extensions.display_broken_cannot_enable", "Broken extensions cannot be enabled."),
  "Save a public HTTPS runtime endpoint on the extension page before enabling.": (t) => t("react.extensions.display_save_public_runtime_endpoint", "Save a public HTTPS runtime endpoint on the extension page before enabling."),
  "Updating active state...": (t) => t("react.extensions.display_updating_active_state", "Updating active state..."),
  "Extension activated.": (t) => t("react.extensions.display_extension_activated", "Extension activated."),
  "Extension disabled.": (t) => t("react.extensions.display_extension_disabled", "Extension disabled."),
  "Awaiting browser authority approval.": (t) => t("react.extensions.display_awaiting_browser_authority_approval", "Awaiting browser authority approval."),
  "Activation cancelled.": (t) => t("react.extensions.display_activation_cancelled", "Activation cancelled."),
  "Installation cancelled.": (t) => t("react.extensions.display_installation_cancelled", "Installation cancelled."),
  "Failed to activate extension.": (t) => t("react.extensions.display_failed_to_activate_extension", "Failed to activate extension."),
  "Failed to disable extension.": (t) => t("react.extensions.display_failed_to_disable_extension", "Failed to disable extension."),
  "Failed to install extension package.": (t) => t("react.extensions.display_failed_to_install_extension_package", "Failed to install extension package."),
  "Install extension from file accepts only .zip archives.": (t) => t("react.extensions.install_extension_from_file_accepts_only_zip_archives", "Install extension from file accepts only .zip archives."),
  "Uninstalling extension...": (t) => t("react.extensions.uninstalling_extension", "Uninstalling extension..."),
  "Uninstall failed.": (t) => t("react.extensions.uninstall_failed", "Uninstall failed."),
  "Extension uninstalled.": (t) => t("react.extensions.extension_uninstalled", "Extension uninstalled."),
  "OAuth account disconnected.": (t) => t("react.extensions.display_oauth_account_disconnected", "OAuth account disconnected."),
  "Connection saved.": (t) => t("react.extensions.display_connection_saved", "Connection saved."),
  "Connection created.": (t) => t("react.extensions.display_connection_created", "Connection created."),
  "Extension state saved.": (t) => t("react.extensions.display_extension_state_saved", "Extension state saved."),
  "Connection test successful.": (t) => t("react.extensions.display_connection_test_successful", "Connection test successful."),
  "Extension action is missing an id.": (t) => t("react.extensions.display_action_missing_id", "Extension action is missing an id."),
  "Save the connection before testing it.": (t) => t("react.extensions.save_the_connection_before_testing_it", "Save the connection before testing it."),
  "Select a project before configuring callbacks.": (t) => t("react.extensions.display_select_project_before_callbacks", "Select a project before configuring callbacks."),
  "Create the callback registration before refreshing webhook status.": (t) => t("react.extensions.display_create_callback_before_refresh", "Create the callback registration before refreshing webhook status."),
  "Refreshing webhook status...": (t) => t("react.extensions.display_refreshing_webhook_status", "Refreshing webhook status..."),
  "Webhook status refreshed.": (t) => t("react.extensions.display_webhook_status_refreshed", "Webhook status refreshed."),
  "Creating callback registration...": (t) => t("react.extensions.display_creating_callback_registration", "Creating callback registration..."),
  "Callback registered and webhook configured.": (t) => t("react.extensions.display_callback_registered_webhook_configured", "Callback registered and webhook configured."),
  "Webhook configuration failed. Polling fallback remains active.": (t) => t("react.extensions.display_webhook_configuration_failed_polling", "Webhook configuration failed. Polling fallback remains active."),
  "Webhook configuration failed.": (t) => t("react.extensions.display_webhook_configuration_failed", "Webhook configuration failed."),
};

/** Display-only projection of the platform's finite legacy status vocabulary. */
export function translatedExtensionStatus(status: string | undefined, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (!status) return "";
  const label = Object.hasOwn(EXTENSION_STATUS_LABELS, status) ? EXTENSION_STATUS_LABELS[status] : undefined;
  if (label) return label(t);
  let match = /^Unhealthy — ([\s\S]*)$/u.exec(status);
  if (match) return t("react.extensions.oauth_final_health_unhealthy_reason", "Unhealthy — {reason}", { reason: match[1] });
  match = /^Couldn't check health: ([\s\S]*)$/u.exec(status);
  if (match) return t("react.extensions.oauth_final_health_failure", "Couldn't check health: {reason}", { reason: match[1] });
  match = /^Couldn't load the uninstall plan: ([\s\S]*)$/u.exec(status);
  if (match) return t("react.extensions.oauth_final_uninstall_plan_failure", "Couldn't load the uninstall plan: {reason}", { reason: match[1] });
  match = /^Running ([\s\S]+)\.\.\.$/u.exec(status);
  if (match) return t("react.extensions.display_running_action", "Running {label}...", { label: match[1] });
  match = /^Saving ([\s\S]+)\.\.\.$/u.exec(status);
  if (match) return t("react.extensions.display_saving_action", "Saving {label}...", { label: match[1] });
  match = /^([\s\S]+) completed\.$/u.exec(status);
  if (match) return t("react.extensions.display_action_completed", "{label} completed.", { label: match[1] });
  match = /^([\s\S]+) is required\.$/u.exec(status);
  if (match) return t("react.extensions.display_field_required", "{field} is required.", { field: match[1] });
  match = /^([\s\S]+) must be a valid ([\s\S]+)\.$/u.exec(status);
  if (match) return t("react.extensions.display_field_valid_type", "{field} must be a valid {type}.", { field: match[1], type: match[2] });
  match = /^Installing ([\s\S]+)\.\.\.$/u.exec(status);
  if (match) return t("react.extensions.oauth_final_installing", "Installing {name}...", { name: match[1] });
  match = /^([\s\S]+\.) Install check, (\d+) to review: ([\s\S]*)$/u.exec(status);
  if (match && /^(?:Installed|Updated) /u.test(match[1])) {
    return t("react.extensions.oauth_final_install_check", "{message} Install check, {count} to review: {warnings}", { message: translatedExtensionStatus(match[1], t), count: match[2], warnings: match[3] });
  }
  match = /^Installed ([\s\S]+)\.$/u.exec(status);
  if (match) return t("react.extensions.oauth_final_installed", "Installed {detail}.", { detail: match[1] });
  match = /^Updated ([\s\S]+) from (\S+) to (\S+)\.$/u.exec(status);
  if (match) return t("react.extensions.oauth_final_updated_versions", "Updated {name} from {old} to {new}.", { name: match[1], old: match[2], new: match[3] });
  match = /^Updated ([\s\S]+) to (\S+)\.$/u.exec(status);
  if (match) return t("react.extensions.oauth_final_updated_version", "Updated {name} to {new}.", { name: match[1], new: match[2] });
  match = /^Updated ([\s\S]+)\.$/u.exec(status);
  if (match) return t("react.extensions.oauth_final_updated", "Updated {detail}.", { detail: match[1] });
  return status;
}

/**
 * Where "How to fill this in" points: the documentation the Marketplace links
 * (the catalog's repository/README), else the vendor website, and only then
 * the support address (for Ollama that is the issue tracker, not the docs).
 */
export function extensionDocumentationUrl(
  extension: ExtensionRecord,
  catalog: ExtensionCatalogListing | undefined,
): string {
  const catalogEntry = (catalog?.items ?? []).find((item) => item.extension_id === extension.extension_id);
  return stringFrom(catalogEntry?.repository_url || extension.website_url || extension.support_url);
}
