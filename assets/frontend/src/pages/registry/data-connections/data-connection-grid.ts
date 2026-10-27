import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import type { DataConnectionRecord } from "@/pages/registry/datasets/api";

import { formatDate } from "./data-connection-helpers";

export type DataConnectionRow = DataConnectionRecord & {
  _workspace_label: string;
  _source_label: string;
  _health_label: string;
  _last_checked_label: string;
  _tags_label: string;
};

export type ConnectionRowContext = {
  workspaceById: Map<string, string>;
  connectorSourceLabelByLocalId: Record<string, string>;
};

/** The health column reads `disabled` for a disabled connection, as it always has. */
export function healthLabel(connection: DataConnectionRecord, t: I18nContextValue["t"] = fallbackContext.t): string {
  return connection.enabled ? connection.health_status || t("project_detail.connections.status.unknown", "unknown") : t("settings.notifications.status.disabled", "disabled");
}

export function sourceLabel(
  connection: DataConnectionRecord,
  connectorSourceLabelByLocalId: Record<string, string>, t: I18nContextValue["t"] = fallbackContext.t
): string {
  const ref = connection.connector_ref_jsonb || {};
  const local = firstStringFromUnknownValue([ref.local_connector_id, ""]).trim();
  return connectorSourceLabelByLocalId[local] || connection.source_kind || t("react.registry.extension", "extension");
}

export function buildConnectionRows(
  connections: readonly DataConnectionRecord[],
  { workspaceById, connectorSourceLabelByLocalId }: ConnectionRowContext, t: I18nContextValue["t"] = fallbackContext.t
): DataConnectionRow[] {
  return connections.map((connection) => ({
    ...connection,
    _workspace_label:
      connection.workspace_display_name ||
      workspaceById.get(connection.workspace_id) ||
      connection.workspace_id ||
      "-",
    _source_label: sourceLabel(connection, connectorSourceLabelByLocalId, t),
    _health_label: healthLabel(connection, t),
    _last_checked_label: formatDate(connection.last_checked_at),
    _tags_label: connection.tags?.length ? connection.tags.join(", ") : "",
  }));
}

/**
 * Column and row-action description, in the same declarative shape the other
 * registry catalogs use. `readOnly` drops the action column entirely rather
 * than rendering an empty one.
 */
export function dataConnectionGridConfig(readOnly = false, t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  return {
    id_field: "connection_id",
    columns: [
      { key: "display_name", label: t("react.workflows.connection", "Connection"), sortable: true },
      { key: "_workspace_label", label: t("project_mode.workspace", "Workspace"), sortable: true },
      { key: "_source_label", label: t("react.registry.source_variant_0e570ca6", "Source"), sortable: true },
      { key: "environment", label: t("react.registry.environment_variant_9e471951", "Environment"), sortable: true },
      {
        key: "_health_label",
        label: t("settings.mcp.health", "Health"),
        type: "badge",
        badge_map: {
          healthy: "green",
          unhealthy: "danger",
          unknown: "neutral",
          disabled: "neutral",
        },
      },
      { key: "_last_checked_label", label: t("react.registry.last_checked", "Last checked"), sortable: true },
    ],
    row_actions: readOnly
      ? []
      : [
          { action_id: "edit", label: t("extensions.repeater.edit", "Edit") },
          { action_id: "test", label: t("react.registry.test", "Test") },
          { action_id: "introspect", label: t("react.registry.introspect", "Introspect") },
          { action_id: "toggle", label: t("react.registry.toggle", "Toggle") },
          { action_id: "delete", label: t("settings.model_providers.delete", "Delete"), variant: "danger" },
        ],
  };
}
