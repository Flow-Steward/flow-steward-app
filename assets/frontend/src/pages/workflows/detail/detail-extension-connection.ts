import { objectOrEmpty, textOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";

export type ExtensionConnectionEligibility = {
  allowed: boolean;
  applicable: boolean;
  guidance: string;
  optionSuffix: string;
};

function stringSet(value: unknown): Set<string> {
  return new Set(
    (Array.isArray(value) ? value : [])
      .map((item) => textOrEmpty(item).trim())
      .filter(Boolean),
  );
}

/**
 * Every connection type an operation declares.
 *
 * Contract V2 has one canonical plural representation for zero, one, or many
 * accepted types.
 */
export function operationConnectionTypeIds(operation: Record<string, unknown>): string[] {
  const plural = operation.connection_type_ids;
  if (Array.isArray(plural)) {
    const values = plural.map((value) => textOrEmpty(value)).filter(Boolean);
    if (values.length) {
      return values;
    }
  }
  return [];
}

export function extensionConnectionEligibility(
  row: Record<string, unknown> | undefined,
  requirements: { connectionTypeIds: string[]; requiredScopes: string[] },
  t: I18nContextValue["t"] = fallbackContext.t,
): ExtensionConnectionEligibility {
  const acceptedTypes = requirements.connectionTypeIds.filter(Boolean);
  if (!acceptedTypes.length) {
    return { allowed: true, applicable: false, guidance: "", optionSuffix: "" };
  }
  if (!row) {
    return { allowed: false, applicable: true, guidance: t("react.workflows.choose_a_project_connection", "Choose a project connection."), optionSuffix: "" };
  }
  const rowType = textOrEmpty(row.connection_type || row.connection_type_id);
  // Membership, not equality: an operation may accept several types, and a
  // project holding a valid connection of the second one showed nothing.
  if (!acceptedTypes.includes(rowType)) {
    return { allowed: false, applicable: true, guidance: t("react.workflows.choose_a_compatible_project_connection", "Choose a compatible project connection."), optionSuffix: t("react.extensions.incompatible", "Incompatible") };
  }
  if (row.enabled === false) {
    return { allowed: false, applicable: true, guidance: t("react.workflows.enable_this_connection_before_using_it", "Enable this connection before using it."), optionSuffix: t("react.registry.disabled", "Disabled") };
  }
  const oauth = objectOrEmpty(objectOrEmpty(row.connection_config).oauth);
  const lifecycle = textOrEmpty(row.oauth_lifecycle_status || oauth.lifecycle_status).toLowerCase();
  if (lifecycle && !["connected", "verified"].includes(lifecycle)) {
    return { allowed: false, applicable: true, guidance: t("react.workflows.reconnect_this_account_from_the_extension_setup_page", "Reconnect this account from the extension Setup page."), optionSuffix: t("react.workflows.reconnect_required", "Reconnect required") };
  }
  const grantedScopes = stringSet(row.oauth_granted_scopes || oauth.granted_scopes);
  if (requirements.requiredScopes.some((scope) => !grantedScopes.has(scope))) {
    return { allowed: false, applicable: true, guidance: t("react.workflows.reconnect_this_account_and_approve_the_access_required_by_this_action", "Reconnect this account and approve the access required by this action."), optionSuffix: t("react.workflows.additional_access_required", "Additional access required") };
  }
  const health = textOrEmpty(row.health_status).toLowerCase();
  const runtime = textOrEmpty(row.runtime_status).toLowerCase();
  if (
    ["error", "failed", "unavailable"].includes(health) ||
    ["failed", "incompatible", "unavailable"].includes(runtime)
  ) {
    return { allowed: false, applicable: true, guidance: t("react.workflows.resolve_this_connection_in_the_extension_setup_page_before_retrying", "Resolve this connection in the extension Setup page before retrying."), optionSuffix: t("react.registry.needs_attention", "Needs attention") };
  }
  return { allowed: true, applicable: true, guidance: "", optionSuffix: "" };
}
