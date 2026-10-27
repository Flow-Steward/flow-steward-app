import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { McpCatalogIcon } from "@/pages/registry/mcp/mcp-catalog-presentation";
import { type ReactNode } from "react";

import { type KeyValueRow } from "./key-value-rows";
import { type ScopeRow } from "./mcp-form-rows";
import { mcpStatusLabel } from "./mcp-status-label";
export { rowsFromRecord, scopeRows } from "./mcp-form-rows";
export type { ScopeRow } from "./mcp-form-rows";

import {
  BlankCell,
  DsBadge,
  DsValueChip,
  RowActionMenu,
  type DsBadgeTone,
  type DsTableColumn,
} from "@/components/shared/design-system";

import { type McpConnectorRecord } from "@/pages/registry/mcp/api";

import { fetchProjectsList } from "@/pages/projects/list/api";

export type FormSubmitEvent = {
  currentTarget: HTMLFormElement;
  preventDefault: () => void;
};

export type McpDraft = {
  /** The project the new connector belongs to. */
  project_id: string;
  display_name: string;
  base_url: string;
  transport: string;
  auth_mode: string;
  headers: KeyValueRow[];
  namespace_prefix: string;
  description: string;
  enabled: boolean;
  requested_scopes: ScopeRow[];
  authorization_parameters: KeyValueRow[];
  refresh_token_required: boolean;
};

/** The `message` state reports successes and failures alike, so the outcome
 *  travels with the text and picks the alert tone. A failed sync rendered in the
 *  neutral tone would read as a completed one. */
export type Outcome = "success" | "danger" | "neutral";

export const EMPTY_DRAFT: McpDraft = {
  project_id: "",
  display_name: "",
  base_url: "",
  transport: "streamable_http",
  auth_mode: "none",
  headers: [],
  namespace_prefix: "",
  description: "",
  enabled: true,
  requested_scopes: [],
  authorization_parameters: [],
  refresh_token_required: false,
};

export const TRANSPORT_OPTIONS = [{ value: "streamable_http", label: "streamable_http" }];

export function localizedAUTH_MODE_OPTIONS(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { value: "none", label: t("registry.published_blocks.none", "none") },
  { value: "bearer", label: "bearer" },
  { value: "api_key", label: "api_key" },
  { value: "oauth2", label: "oauth2" },
];
}

export const AUTH_MODE_OPTIONS = localizedAUTH_MODE_OPTIONS();

/* Orange is genuine attention or escalation. "Never synced" and "not configured"
   are ordinary starting states and stay neutral; anything unrecognised falls
   back to neutral rather than to orange. */
export const GREEN_STATES = new Set(["active", "connected", "ok", "success"]);

export const ORANGE_STATES = new Set(["degraded", "auth_required", "discovery_required", "expired"]);

export const DANGER_STATES = new Set(["error", "failed", "refresh_failed", "offline"]);

export function stateTone(value: string): DsBadgeTone {
  const normalized = value.trim().toLowerCase();
  if (GREEN_STATES.has(normalized)) {
    return "green";
  }
  if (ORANGE_STATES.has(normalized)) {
    return "orange";
  }
  if (DANGER_STATES.has(normalized)) {
    return "danger";
  }
  return "neutral";
}

export function formatLastSync(value: string, status: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (!value || status === "never") {
    return t("react.registry.never", "Never");
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return status === "failed" ? t("settings.mcp.oauth.failed", "Failed") : value;
  }
  return `${date.toLocaleString()}${status === "failed" ? ` (${t("react.extensions.failed", "failed")})` : ""}`;
}

/** What the Health column shows: a turned-off connector reads as disabled. */
export function connectorHealth(row: McpConnectorRecord): string {
  return row.enabled ? row.status : "disabled";
}

/** What the Credentials column shows; "" when the connector signs in without OAuth. */
export function connectorCredential(row: McpConnectorRecord): string {
  return row.auth_mode === "oauth2" ? row.oauth_status.status || row.auth_status : "";
}

export type ConnectorColumnsOptions = {
  busyConnectorId: string;
  canManageProject: boolean;
  /** The project's name for a connector's project_id. */
  projectName: (projectId: string) => string;
  onDelete: (connectorId: string, displayName: string) => void;
  onHealth: (connectorId: string) => void;
  onRowMenuChange: (connectorId: string | null) => void;
  onSync: (connectorId: string) => void;
  onView: (connectorId: string) => void;
  rowMenu: string | null;
};

export function buildConnectorColumns({
  busyConnectorId,
  canManageProject,
  projectName,
  onDelete,
  onHealth,
  onRowMenuChange,
  onSync,
  onView,
  rowMenu,
}: ConnectorColumnsOptions, t: I18nContextValue["t"] = fallbackContext.t): ReadonlyArray<DsTableColumn<McpConnectorRecord>> {
  return [
    {
      key: "display_name",
      header: t("settings.mcp.fields.name", "Name"),
      width: 260,
      sortable: true,
      cell: (row) => row.catalog_item_id ? (
        <span className="fs-mcp-name-cell">
          <McpCatalogIcon hasIcon itemId={row.catalog_item_id} size="inline" title={row.display_name} />
          <strong title={row.description || undefined}>{row.display_name}</strong>
        </span>
      ) : <strong title={row.description || undefined}>{row.display_name}</strong>,
    },
    {
      key: "status",
      header: t("settings.mcp.health", "Health"),
      width: 116,
      sortable: true,
      cell: (row) => {
        const status = connectorHealth(row);
        return <DsBadge tone={stateTone(status)}>{mcpStatusLabel(status, t)}</DsBadge>;
      },
    },
    {
      key: "last_synced_at",
      header: t("react.registry.last_sync", "Last sync"),
      width: 208,
      sortable: true,
      cell: (row) => (
        <span className={row.last_sync_status === "failed" ? "fs-mcp-sync-failed" : undefined}>
          {formatLastSync(row.last_synced_at, row.last_sync_status, t)}
        </span>
      ),
    },
    {
      key: "auth_mode",
      header: t("react.registry.auth", "Auth"),
      width: 116,
      cell: (row) => <DsValueChip>{row.auth_mode}</DsValueChip>,
    },
    {
      key: "credentials",
      header: t("react.registry.credentials", "Credentials"),
      width: 132,
      cell: (row) => {
        const credential = connectorCredential(row);
        return credential
          ? <DsBadge tone={stateTone(credential)}>{mcpStatusLabel(credential, t)}</DsBadge>
          : <BlankCell />;
      },
    },
    {
      key: "transport",
      header: t("settings.mcp.fields.transport", "Transport"),
      width: 156,
      cell: (row) => <DsValueChip>{row.transport}</DsValueChip>,
    },
    {
      key: "project_id",
      header: t("events.columns.project", "Project"),
      truncate: "16rem",
      sortable: true,
      cell: (row) => (row.project_id ? projectName(row.project_id) : <BlankCell />),
    },
    {
      key: "allowlisted_tools",
      header: t("react.registry.allowed_tools_column", "Allowed Tools"),
      width: 130,
      align: "right",
      sortable: true,
      cell: (row) => String(row.allowlisted_tools.length),
    },
    {
      key: "actions",
      header: "",
      ariaLabel: t("extensions.table.actions", "Actions"),
      cell: (row) => {
        const connectorId = row.connector_id;
        const busy = busyConnectorId === connectorId;
        return (
          <RowActionMenu
            label={t("react.registry.actions_for_value0", "Actions for {value0}", { value0: row.display_name })}
            onOpenChange={(open) => onRowMenuChange(open ? connectorId : null)}
            open={rowMenu === connectorId}
          >
            <div className="fs-row-menu-group">
              <button onClick={() => onView(connectorId)} role="menuitem" type="button">{t("react.registry.view", "View")}</button>
              {canManageProject ? null : <span className="fs-row-menu-label">{t("settings.access.service_accounts.form.level.read", "Read only")}</span>}
              {canManageProject ? (
                <>
                  <button
                    disabled={busy}
                    onClick={() => {
                      onSync(connectorId);
                      onRowMenuChange(null);
                    }}
                    role="menuitem"
                    type="button"
                  >{t("react.registry.sync", "Sync")}</button>
                  <button
                    disabled={busy}
                    onClick={() => {
                      onHealth(connectorId);
                      onRowMenuChange(null);
                    }}
                    role="menuitem"
                    type="button"
                  >{t("settings.mcp.health", "Health")}</button>
                </>
              ) : null}
            </div>
            {canManageProject ? (
              <>
                <div className="fs-row-menu-separator" />
                <div className="fs-row-menu-group">
                  <button
                    className="danger"
                    disabled={busy}
                    onClick={() => {
                      onDelete(connectorId, row.display_name);
                      onRowMenuChange(null);
                    }}
                    role="menuitem"
                    type="button"
                  >{t("settings.model_providers.delete", "Delete")}</button>
                </div>
              </>
            ) : null}
          </RowActionMenu>
        );
      },
    },
  ];
}

export function isValidHttpEndpoint(value: string): boolean {
  try {
    const endpoint = new URL(value);
    return endpoint.protocol === "http:" || endpoint.protocol === "https:";
  } catch {
    return false;
  }
}

export async function fetchAllActiveProjects() {
  const projects: Awaited<ReturnType<typeof fetchProjectsList>>["projects"] = [];
  let page = 1;
  let totalPages = 1;
  do {
    const response = await fetchProjectsList({ page, perPage: 200, search: "" });
    projects.push(...response.projects);
    totalPages = Math.max(1, response.total_pages || 1);
    page += 1;
  } while (page <= totalPages);
  return projects.filter((project) => project.status === undefined || project.status === "active");
}

export function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

/** Whether the caller may add or change connectors: the capability, and a project to put them in. */
export function projectCanManage(
  loading: boolean,
  projects: Awaited<ReturnType<typeof fetchAllActiveProjects>>,
  canMutateProjects: boolean,
): boolean {
  return !loading && canMutateProjects && projects.length > 0;
}

export function mcpTableState({
  loading,
  loadError,
  total,
  filtered,
  onRetry,
}: Readonly<{
  loading: boolean;
  loadError: string;
  total: number;
  /** Whether a search or a filter narrowed the list. */
  filtered: boolean;
  onRetry: () => void;
}>, t: I18nContextValue["t"] = fallbackContext.t): ReactNode {
  if (loading) {
    return <div className="fs-table-state">
      <strong>{t("react.registry.loading_mcp_connectors", "Loading MCP connectors")}</strong>
      <span>{t("react.registry.the_catalog_will_remain_in_this_table_layout", "The catalog will remain in this table layout.")}</span>
    </div>;
  }
  if (loadError) {
    return <div className="fs-table-state">
      <strong>{t("react.registry.mcp_connectors_could_not_be_loaded", "MCP connectors could not be loaded")}</strong>
      <span>{loadError}</span>
      <button className="fs-button" onClick={onRetry} type="button">{t("settings.watchdog.retry", "Retry")}</button>
    </div>;
  }
  if (!total) {
    return <div className="fs-table-state">
      <strong>{filtered ? t("react.registry.no_mcp_connectors_match_this_search", "No MCP connectors match this search") : t("react.registry.no_mcp_connectors_found", "No MCP connectors found")}</strong>
      <span>{filtered ? t("react.registry.change_or_clear_the_search", "Change or clear the search.") : t("react.registry.register_a_connector_to_get_started", "Register a connector to get started.")}</span>
    </div>;
  }
  return null;
}
