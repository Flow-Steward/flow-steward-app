import type { I18nContextValue } from "@/lib/i18n/context";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

/** Editor choices; all three execute and persist as the canonical connector kind. */
export type ToolCategory = "connector" | "mcp_tools" | "extension_tools";

export function isToolCategory(value: string): value is ToolCategory {
  return value === "connector" || value === "mcp_tools" || value === "extension_tools";
}

export function connectorToolCategory(row: Record<string, unknown>, config: Record<string, unknown> = {}): ToolCategory {
  const id = stringFromUnknownValue(row.connector_id || config.connector_id);
  if (row.source === "extension" || id.startsWith("extension.") || row.extension_id || config.extension_id) return "extension_tools";
  if (row.source === "mcp_connector" || id.startsWith("connector.mcp.")) return "mcp_tools";
  return "connector";
}

export function localizedToolCategoryOptions(t: I18nContextValue["t"]) {
  return [
    { value: "connector", label: t("react.workflows.system_tools", "System tools") },
    { value: "extension_tools", label: t("react.workflows.extension_tools", "Extension tools") },
    { value: "mcp_tools", label: t("react.workflows.mcp_tools", "MCP tools") },
  ];
}
