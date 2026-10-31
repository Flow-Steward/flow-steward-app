import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";

type Translate = I18nContextValue["t"];

export type OAuthNotice = { title: string; copy: string };

/**
 * Explanations for the inbound OAuth readiness blockers (ADR 0047, decision 2)
 * and for the errors the authorization endpoint shows before it may redirect.
 * One place, so the consent page, Settings → Account and Settings → Security say
 * the same thing about the same code.
 */
export function oauthNotice(code: string, t: Translate = fallbackContext.t): OAuthNotice {
  switch (code) {
    case "public_https_required":
      return {
        title: t("react.oauth.public_https_required_title", "A public HTTPS address is required"),
        copy: t("react.oauth.public_https_required_copy", "Cloud AI apps such as ChatGPT and Claude can connect only to an installation with a public HTTPS address. Set FS_PUBLIC_BASE_URL to that address; localhost, private and local network addresses cannot be used."),
      };
    case "issuer_mismatch":
      return {
        title: t("react.oauth.issuer_mismatch_title", "The MCP address does not match this installation"),
        copy: t("react.oauth.issuer_mismatch_copy", "FS_MCP_ISSUER_URL or FS_MCP_RESOURCE_URL points to a different address than FS_PUBLIC_BASE_URL. Remove the override or make it match, then restart."),
      };
    case "forward_secret_missing":
      return {
        title: t("react.oauth.forward_secret_missing_title", "The MCP server is not linked to the application"),
        copy: t("react.oauth.forward_secret_missing_copy", "The internal secret between the MCP server and the application is missing or too short. Update or restart the installation so it is generated."),
      };
    case "storage_missing":
      return {
        title: t("react.oauth.storage_missing_title", "The database is missing the OAuth tables"),
        copy: t("react.oauth.storage_missing_copy", "This database was created before cloud app connections existed. An administrator has to add the OAuth tables before apps can connect. Personal access tokens keep working."),
      };
    case "disabled_by_admin":
      return {
        title: t("react.oauth.disabled_by_admin_title", "Cloud AI apps are turned off"),
        copy: t("react.oauth.disabled_by_admin_copy", "An administrator has not allowed cloud AI apps to connect to this account. It can be turned on in Settings → Security."),
      };
    case "client_not_allowed":
      return {
        title: t("react.oauth.client_not_allowed_title", "This app is not supported"),
        copy: t("react.oauth.client_not_allowed_copy", "Only ChatGPT and Claude can connect to Flow Steward this way. Local clients use a personal access token instead."),
      };
    case "client_metadata_unavailable":
      return {
        title: t("react.oauth.client_metadata_unavailable_title", "The app could not be identified"),
        copy: t("react.oauth.client_metadata_unavailable_copy", "Flow Steward could not load the app's identity document. Check that this installation can reach chatgpt.com and claude.ai, then start the connection again."),
      };
    case "invalid_client_metadata":
      return {
        title: t("react.oauth.invalid_client_metadata_title", "The app's identity document is not accepted"),
        copy: t("react.oauth.invalid_client_metadata_copy", "The app describes itself in a way Flow Steward does not support, so it cannot connect."),
      };
    case "invalid_redirect_uri":
      return {
        title: t("react.oauth.invalid_redirect_uri_title", "The return address is not accepted"),
        copy: t("react.oauth.invalid_redirect_uri_copy", "The app asked to return to an address it has not registered. For your safety the connection was stopped."),
      };
    case "invalid_request":
      return {
        title: t("react.oauth.invalid_request_title", "The connection request is incomplete"),
        copy: t("react.oauth.invalid_request_copy", "The app sent a request Flow Steward cannot process. Start the connection again from the app."),
      };
    case "rate_limited":
      return {
        title: t("react.oauth.rate_limited_title", "Too many connection attempts"),
        copy: t("react.oauth.rate_limited_copy", "Wait a few minutes, then start the connection again from the app."),
      };
    default:
      return {
        title: t("react.oauth.unknown_error_title", "The app could not connect"),
        copy: t("react.oauth.unknown_error_copy", "Start the connection again from the app. If it keeps failing, ask an administrator."),
      };
  }
}

export function oauthPresetLabel(preset: string, t: Translate = fallbackContext.t): string {
  if (preset === "full_authoring") return t("react.oauth.preset_full_authoring", "Full authoring");
  if (preset === "operational") return t("react.oauth.preset_operational", "Monitor and run workflows");
  return preset;
}

export function oauthPresetDescription(preset: string, t: Translate = fallbackContext.t): string {
  if (preset === "full_authoring") {
    return t("react.oauth.preset_full_authoring_description", "admin:all is full access: everything you can do in Flow Steward, including creating and changing workflows, agents and skills. It is not a narrow permission.");
  }
  return t("react.oauth.preset_operational_description", "Find projects, read statuses, errors, reports and approvals, and start or cancel runs.");
}
