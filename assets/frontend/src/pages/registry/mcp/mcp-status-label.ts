import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";

/** Translate only built-in display states; unknown provider values remain untouched. */
export function mcpStatusLabel(value: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const labels: Record<string, () => string> = {
    "active": () => t("react.registry.mcp_final_status_active", "active"),
    "disabled": () => t("react.registry.mcp_final_status_disabled", "disabled"),
    "unchecked": () => t("react.registry.mcp_final_status_unchecked", "unchecked"),
    "healthy": () => t("react.registry.mcp_final_status_healthy", "healthy"),
    "unhealthy": () => t("react.registry.mcp_final_status_unhealthy", "unhealthy"),
    "degraded": () => t("react.registry.mcp_final_status_degraded", "degraded"),
    "unknown": () => t("react.registry.mcp_final_status_unknown", "unknown"),
    "connected": () => t("react.registry.mcp_final_status_connected", "connected"),
    "not_configured": () => t("react.registry.mcp_final_status_not_configured", "not_configured"),
    "auth_required": () => t("react.registry.mcp_final_status_auth_required", "auth_required"),
    "discovery_required": () => t("react.registry.mcp_final_status_discovery_required", "discovery_required"),
    "error": () => t("react.registry.mcp_final_status_error", "error"),
    "failed": () => t("react.registry.mcp_final_status_failed", "failed"),
    "success": () => t("react.registry.mcp_final_status_success", "success"),
    "never": () => t("react.registry.mcp_final_status_never", "never"),
    "pending": () => t("react.registry.mcp_final_status_pending", "pending"),
    "manual": () => t("react.registry.mcp_final_status_manual", "manual"),
    "dynamic": () => t("react.registry.mcp_final_status_dynamic", "dynamic"),
    "true": () => t("react.registry.mcp_final_status_true", "true"),
    "false": () => t("react.registry.mcp_final_status_false", "false"),
  };
  return Object.hasOwn(labels, value) ? labels[value]() : value;
}
