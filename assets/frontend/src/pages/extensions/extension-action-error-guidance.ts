import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";

const extensionActionErrorGuidanceByCode: Readonly<Record<string, (t: I18nContextValue["t"]) => string>> = {
  provider_secret_value_required: (t) => t("react.extensions.secret_value_required", "Enter a secret value before saving."),
  provider_secret_storage_unavailable: (t) => t("react.extensions.secret_storage_unavailable", "Secret storage is unavailable. Retry, then contact an administrator if the problem continues."),
  provider_secret_storage_failed: (t) => t("react.extensions.secret_storage_failed", "The secret was not saved. Retry, then contact an administrator if the problem continues."),
  project_connection_storage_unavailable: (t) => t("react.extensions.connection_storage_unavailable", "The connection could not be saved. Retry, then contact an administrator if the problem continues."),
  project_context_required: (t) => t("react.extensions.project_required_for_action", "Select a project before running this action."),
  authorization_required: (t) => t("react.extensions.authorization_required_for_action", "Authorize this connection, then retry the action."),
  invalid_configuration: (t) => t("react.extensions.invalid_connection_configuration", "Review the connection configuration and correct the highlighted values."),
  secret_configuration_unapproved: (t) => t("react.extensions.connection_secrets_not_approved", "This connection uses stored secrets that are not approved for it yet. Approval is needed again after the connection changes. Choose Approve use of secrets on the connection's card, then run the action again."),
};

// A connection whose credentials could not be produced fails as
// "connection_secret_resolution_failed:<connection id>:<code>"; the last part is
// the stable code (the HTTP error carries no separate code field).
const connectionSecretFailurePattern = /^connection_secret_resolution_failed:[^:]*:([a-z_]+)$/;

function guidanceCode(value: string): string {
  const code = value.trim().toLowerCase();
  return connectionSecretFailurePattern.exec(code)?.[1] ?? code;
}

export function extensionActionErrorGuidance(...values: unknown[]): string {
  return translatedExtensionActionErrorGuidance(fallbackContext.t, ...values);
}

export function translatedExtensionActionErrorGuidance(t: I18nContextValue["t"], ...values: unknown[]): string {
  for (const value of values) {
    if (typeof value !== "string") {
      continue;
    }
    const code = guidanceCode(value);
    if (!Object.hasOwn(extensionActionErrorGuidanceByCode, code)) {
      continue;
    }
    const guidance = extensionActionErrorGuidanceByCode[code];
    if (guidance) {
      return guidance(t);
    }
  }
  return "";
}
