import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export type McpDiscoveredTool = {
  remote_tool_name: string;
  projected_tool_id: string;
  display_name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  annotations: Record<string, unknown>;
  lifecycle_state: "published" | "available_to_add" | "in_trash" | "id_conflict";
  can_publish: boolean;
  availability_reason_code: string;
  availability_reason_text: string;
  recovery_action: string;
  trash_key: string;
  trash_entity_type: string;
  deleted_at: string;
  deleted_by: string;
};

export type McpProjectedTool = {
  tool_id: string;
  display_name: string;
  enabled: boolean;
  remote_tool_name: string;
};

export type McpConnectorRecord = {
  connector_id: string;
  project_id: string;
  display_name: string;
  description: string;
  transport: string;
  base_url: string;
  auth_mode: string;
  namespace_prefix: string;
  allowlisted_tools: string[];
  discovered_tools_jsonb: McpDiscoveredTool[];
  projected_tools: McpProjectedTool[];
  status: string;
  auth_status: string;
  oauth_status: McpConnectorOAuthStatus;
  oauth_requested_scopes: string[];
  oauth_authorization_parameters: Record<string, string>;
  oauth_refresh_token_required: boolean;
  has_client_id: boolean;
  has_client_secret: boolean;
  last_synced_at: string;
  last_sync_status: "never" | "success" | "failed";
  last_sync_error: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  /** Set when the connector was installed from the MCP catalog. */
  catalog_item_id: string;
  catalog_remote_id: string;
  catalog_item_revision: number;
};

export type McpConnectorPayload = {
  display_name?: string;
  base_url?: string;
  transport?: string;
  auth_mode?: string;
  headers?: Record<string, string>;
  headers_mode?: "keep" | "replace" | "clear";
  oauth_config?: Record<string, unknown>;
  namespace_prefix?: string;
  description?: string;
  enabled?: boolean;
  allowlisted_tools?: string[];
};

export type McpHealthResult = {
  connector_id: string;
  status: string;
  latency_ms: number | null;
  error: string | null;
  checked_at: string | null;
};

export type McpConnectorDeletePlan = {
  connector_id: string;
  connector: {
    display_name: string;
    enabled: boolean;
    status: string;
  };
  remove: {
    tools: Array<{
      tool_id: string;
      display_name: string;
      affected_workflow_ids: string[];
    }>;
  };
  affected: {
    workflows: Array<{
      workflow_id: string;
      display_name: string;
      status: string;
      enabled: boolean;
      tool_ids: string[];
      via_connector: boolean;
    }>;
  };
  warnings: string[];
  revision: string;
};

export type McpConnectorOAuthStatus = {
  status: string;
  redirect_uri: string;
  issuer: string;
  registration_mode: string;
  resource: string;
  expires_at: string;
  granted_scopes: string[];
  connected_identity: string;
  has_access_token: boolean;
  has_refresh_token: boolean;
  last_error: string;
};

export type McpConnectorOAuthStartPayload = {
  return_path?: string;
};

export type McpConnectorOAuthCredentialsPayload = {
  client_id: string;
  client_secret: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => firstStringFromUnknownValue([item, ""]).trim())
    .filter(isTruthy);
}

function normalizeDiscoveredTool(value: Record<string, unknown>): McpDiscoveredTool {
  return {
    remote_tool_name: firstStringFromUnknownValue([value.remote_tool_name, value.name, ""]).trim(),
    projected_tool_id: firstStringFromUnknownValue([value.projected_tool_id, ""]).trim(),
    display_name: firstStringFromUnknownValue([value.display_name, value.title, value.remote_tool_name, value.name, ""]).trim(),
    description: firstStringFromUnknownValue([value.description, ""]).trim(),
    input_schema: isRecord(value.input_schema) ? value.input_schema : {},
    output_schema: isRecord(value.output_schema) ? value.output_schema : {},
    annotations: isRecord(value.annotations) ? value.annotations : {},
    lifecycle_state: firstStringFromUnknownValue([value.lifecycle_state, "available_to_add"]).trim() as McpDiscoveredTool["lifecycle_state"],
    can_publish: value.can_publish !== false,
    availability_reason_code: firstStringFromUnknownValue([value.availability_reason_code, ""]).trim(),
    availability_reason_text: firstStringFromUnknownValue([value.availability_reason_text, ""]).trim(),
    recovery_action: firstStringFromUnknownValue([value.recovery_action, ""]).trim(),
    trash_key: firstStringFromUnknownValue([value.trash_key, ""]).trim(),
    trash_entity_type: firstStringFromUnknownValue([value.trash_entity_type, ""]).trim(),
    deleted_at: firstStringFromUnknownValue([value.deleted_at, ""]).trim(),
    deleted_by: firstStringFromUnknownValue([value.deleted_by, ""]).trim(),
  };
}

function normalizeProjectedTool(value: Record<string, unknown>): McpProjectedTool {
  return {
    tool_id: firstStringFromUnknownValue([value.tool_id, ""]).trim(),
    display_name: firstStringFromUnknownValue([value.display_name, value.tool_id, ""]).trim(),
    enabled: value.enabled !== false,
    remote_tool_name: firstStringFromUnknownValue([value.remote_tool_name, ""]).trim(),
  };
}

function normalizeOAuthStatus(value: unknown, fallbackStatus = "not_configured"): McpConnectorOAuthStatus {
  const payload = isRecord(value) ? value : {};
  return {
    status: firstStringFromUnknownValue([payload.status, fallbackStatus]).trim() || fallbackStatus,
    redirect_uri: firstStringFromUnknownValue([payload.redirect_uri, ""]).trim(),
    issuer: firstStringFromUnknownValue([payload.issuer, ""]).trim(),
    registration_mode: firstStringFromUnknownValue([payload.registration_mode, ""]).trim(),
    resource: firstStringFromUnknownValue([payload.resource, ""]).trim(),
    expires_at: firstStringFromUnknownValue([payload.expires_at, ""]).trim(),
    granted_scopes: stringList(payload.granted_scopes),
    connected_identity: firstStringFromUnknownValue([payload.connected_identity, ""]).trim(),
    has_access_token: payload.has_access_token === true,
    has_refresh_token: payload.has_refresh_token === true,
    last_error: firstStringFromUnknownValue([payload.last_error, ""]).trim(),
  };
}

function normalizeConnector(value: Record<string, unknown>): McpConnectorRecord {
  const authStatus = firstStringFromUnknownValue([value.auth_status, "not_configured"]).trim() || "not_configured";
  return {
    connector_id: firstStringFromUnknownValue([value.connector_id, ""]).trim(),
    project_id: firstStringFromUnknownValue([value.project_id, ""]).trim(),
    display_name: firstStringFromUnknownValue([value.display_name, ""]).trim(),
    description: firstStringFromUnknownValue([value.description, ""]).trim(),
    transport: firstStringFromUnknownValue([value.transport, "streamable_http"]).trim(),
    base_url: firstStringFromUnknownValue([value.base_url, ""]).trim(),
    auth_mode: firstStringFromUnknownValue([value.auth_mode, "none"]).trim(),
    namespace_prefix: firstStringFromUnknownValue([value.namespace_prefix, ""]).trim(),
    allowlisted_tools: stringList(value.allowlisted_tools),
    discovered_tools_jsonb: Array.isArray(value.discovered_tools_jsonb)
      ? value.discovered_tools_jsonb.filter(isRecord).map(normalizeDiscoveredTool)
      : [],
    projected_tools: Array.isArray(value.projected_tools)
      ? value.projected_tools.filter(isRecord).map(normalizeProjectedTool)
      : [],
    status: firstStringFromUnknownValue([value.status, "unchecked"]).trim(),
    auth_status: authStatus,
    oauth_status: normalizeOAuthStatus(value.oauth_status, authStatus),
    oauth_requested_scopes: stringList(value.oauth_requested_scopes),
    oauth_authorization_parameters: isRecord(value.oauth_authorization_parameters)
      ? Object.fromEntries(
          Object.entries(value.oauth_authorization_parameters).map(([key, item]) => [
            key,
            firstStringFromUnknownValue([item, ""]),
          ]),
        )
      : {},
    oauth_refresh_token_required: value.oauth_refresh_token_required === true,
    has_client_id: value.has_client_id === true,
    has_client_secret: value.has_client_secret === true,
    last_synced_at: firstStringFromUnknownValue([value.last_synced_at, ""]).trim(),
    last_sync_status: (
      firstStringFromUnknownValue([value.last_sync_status, "never"]).trim().toLowerCase() || "never"
    ) as McpConnectorRecord["last_sync_status"],
    last_sync_error: firstStringFromUnknownValue([value.last_sync_error, ""]).trim(),
    enabled: value.enabled !== false,
    created_at: firstStringFromUnknownValue([value.created_at, ""]).trim(),
    updated_at: firstStringFromUnknownValue([value.updated_at, ""]).trim(),
    catalog_item_id: firstStringFromUnknownValue([value.catalog_item_id, ""]).trim(),
    catalog_remote_id: firstStringFromUnknownValue([value.catalog_remote_id, ""]).trim(),
    catalog_item_revision: typeof value.catalog_item_revision === "number" ? value.catalog_item_revision : 0,
  };
}

/** The path with project_id when one is set; without it the server answers for every project the caller may see. */
function inProjects(path: string, projectId: string): string {
  const normalized = projectId?.trim() || "";
  return normalized ? `${path}${path.includes("?") ? "&" : "?"}project_id=${encodeURIComponent(normalized)}` : path;
}

function withProject(path: string, projectId: string): string {
  const normalized = projectId?.trim() || "";
  if (!normalized) throw new Error("project_id is required for MCP operations");
  return `${path}${path.includes("?") ? "&" : "?"}project_id=${encodeURIComponent(normalized)}`;
}

export async function fetchMcpConnectors(projectId = "") {
  const payload = await fetchJson<unknown>(inProjects("/api/mcp", projectId));
  if (!isRecord(payload) || !Array.isArray(payload.connectors)) {
    return [];
  }
  return payload.connectors.filter(isRecord).map(normalizeConnector);
}

export async function fetchMcpConnector(connectorId: string, projectId: string) {
  return normalizeConnector(
    await fetchJson<Record<string, unknown>>(withProject(`/api/mcp/${encodeURIComponent(connectorId)}`, projectId)),
  );
}

export async function createMcpConnector(payload: McpConnectorPayload, projectId: string) {
  return normalizeConnector(
    await fetchJson<Record<string, unknown>>(withProject("/api/mcp", projectId), {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

export async function updateMcpConnector(
  connectorId: string,
  payload: McpConnectorPayload,
  projectId: string,
) {
  return normalizeConnector(
    await fetchJson<Record<string, unknown>>(withProject(`/api/mcp/${encodeURIComponent(connectorId)}`, projectId), {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

export async function fetchMcpConnectorDeletePlan(connectorId: string, projectId: string) {
  return fetchJson<McpConnectorDeletePlan>(
    withProject(`/api/mcp/${encodeURIComponent(connectorId)}/delete-plan`, projectId),
    { cache: "no-store" },
  );
}

export async function deleteMcpConnector(connectorId: string, revision: string, projectId: string) {
  return fetchJson<Record<string, unknown>>(withProject(`/api/mcp/${encodeURIComponent(connectorId)}/delete`, projectId), {
    method: "POST",
    body: JSON.stringify({ revision }),
  });
}

export async function checkMcpConnectorHealth(connectorId: string, projectId: string) {
  return fetchJson<McpHealthResult>(withProject(`/api/mcp/${encodeURIComponent(connectorId)}/health`, projectId), {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function syncMcpConnector(connectorId: string, projectId: string) {
  return normalizeConnector(
    await fetchJson<Record<string, unknown>>(withProject(`/api/mcp/${encodeURIComponent(connectorId)}/sync`, projectId), {
      method: "POST",
      body: JSON.stringify({}),
    }),
  );
}

export async function startMcpConnectorOAuth(
  connectorId: string,
  projectId: string,
  payload: McpConnectorOAuthStartPayload = {},
) {
  return fetchJson<Record<string, unknown>>(withProject(`/api/mcp/${encodeURIComponent(connectorId)}/oauth/start`, projectId), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function saveMcpConnectorOAuthCredentials(
  connectorId: string,
  payload: McpConnectorOAuthCredentialsPayload,
  projectId: string,
) {
  return fetchJson<Record<string, unknown>>(
    withProject(`/api/mcp/${encodeURIComponent(connectorId)}/oauth/credentials`, projectId),
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export async function fetchMcpConnectorOAuthStatus(connectorId: string, projectId: string) {
  return normalizeOAuthStatus(
    await fetchJson<Record<string, unknown>>(withProject(`/api/mcp/${encodeURIComponent(connectorId)}/oauth/status`, projectId)),
  );
}

export async function disconnectMcpConnectorOAuth(connectorId: string, projectId: string) {
  return fetchJson<Record<string, unknown>>(withProject(`/api/mcp/${encodeURIComponent(connectorId)}/oauth/disconnect`, projectId), {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// ---------------------------------------------------------------------------
// MCP server catalog
// ---------------------------------------------------------------------------

export type McpCatalogAuth = {
  mode: "none" | "headers" | "oauth2";
  client_registration: "" | "dcr" | "client_metadata" | "user_supplied";
  requested_scopes: string[];
  authorization_parameters: Record<string, string>;
  refresh_token_required: boolean;
  setup_url: string;
};

export type McpCatalogVariable = {
  name: string;
  description: string;
  required: boolean;
  choices: string[];
  default: string;
  /** What the form calls the field ("Freshdesk subdomain"); the name is the URL key. */
  label?: string;
};

export type McpCatalogHeader = { name: string; description: string; required: boolean; secret: boolean; label?: string };

export type McpCatalogRemote = {
  remote_id: string;
  label: string;
  url: string;
  allowed_hosts: string[];
  user_defined_host: boolean;
  auth: McpCatalogAuth;
  variables: McpCatalogVariable[];
  headers: McpCatalogHeader[];
};

export type McpCatalogInstalled = {
  connector_id: string;
  display_name: string;
  /** The project the connector belongs to. */
  project_id?: string;
  remote_id: string;
  catalog_item_revision: number;
};

export type McpCatalogItem = {
  item_id: string;
  revision: number;
  status: string;
  server_name: string;
  server_version: string;
  title: string;
  summary: string;
  description: string;
  category: string;
  website_url: string;
  help_url: string;
  preview_note: string;
  official: { vendor: string; evidence_url: string } | null;
  has_icon: boolean;
  remotes: McpCatalogRemote[];
  installed: McpCatalogInstalled[];
  /** Other names people search for: "Jira" for Atlassian, a former product name. */
  keywords?: string[];
  /** When the server first appeared in the catalog; absent from an index that does not date its entries. */
  added_at?: string;
};

/** What a catalog card and the filters need; the full entry is fetched on open. */
export type McpCatalogCard = Pick<
  McpCatalogItem,
  | "item_id"
  | "revision"
  | "status"
  | "server_name"
  | "title"
  | "summary"
  | "category"
  | "preview_note"
  | "official"
  | "has_icon"
  | "installed"
  | "keywords"
> & { sign_in: string[]; updated_at?: string; added_at?: string };

export type McpCatalogListing = {
  catalog_revision: string;
  category_labels: Record<string, string>;
  items: McpCatalogCard[];
};

export type McpCatalogAvailability = {
  configured: boolean;
  available: boolean;
  reason?: string;
  message?: string;
  stale?: boolean;
  item_count?: number;
};

export type McpCatalogInstallPayload = {
  remote_id: string;
  item_revision: number;
  display_name?: string;
  variables?: Record<string, string>;
  headers?: Record<string, string>;
  client_id?: string;
  client_secret?: string;
  /** The OAuth settings the form showed, as the user left them. */
  requested_scopes?: string[];
  authorization_parameters?: Record<string, string>;
  refresh_token_required?: boolean;
};

export type McpCatalogInstallResult = {
  connector: McpConnectorRecord;
  next_step: "sync" | "oauth_connect" | "oauth_client";
  setup_url: string;
  client_error: string;
};

export const MCP_CATALOG_QUERY_KEY = ["registry", "mcp", "catalog"] as const;

export function mcpCatalogIconUrl(itemId: string): string {
  return `/api/mcp/catalog/${encodeURIComponent(itemId)}/icon`;
}

export async function fetchMcpCatalogAvailability(): Promise<McpCatalogAvailability> {
  return fetchJson<McpCatalogAvailability>("/api/mcp/catalog/availability");
}

export async function fetchMcpCatalog(projectId = ""): Promise<McpCatalogListing> {
  const payload = await fetchJson<Partial<McpCatalogListing>>(inProjects("/api/mcp/catalog", projectId));
  return {
    catalog_revision: String(payload.catalog_revision ?? ""),
    category_labels: isRecord(payload.category_labels)
      ? (payload.category_labels as Record<string, string>)
      : {},
    items: Array.isArray(payload.items) ? (payload.items as McpCatalogCard[]) : [],
  };
}

export async function fetchMcpCatalogItem(itemId: string, projectId = ""): Promise<McpCatalogItem> {
  return fetchJson<McpCatalogItem>(inProjects(`/api/mcp/catalog/${encodeURIComponent(itemId)}`, projectId));
}

export async function installMcpCatalogItem(
  itemId: string,
  payload: McpCatalogInstallPayload,
  projectId: string,
): Promise<McpCatalogInstallResult> {
  const result = await fetchJson<Record<string, unknown>>(
    withProject(`/api/mcp/catalog/${encodeURIComponent(itemId)}/install`, projectId),
    { method: "POST", body: JSON.stringify(payload) },
  );
  return {
    connector: normalizeConnector(isRecord(result.connector) ? result.connector : {}),
    next_step: (firstStringFromUnknownValue([result.next_step, "sync"]) || "sync") as McpCatalogInstallResult["next_step"],
    setup_url: firstStringFromUnknownValue([result.setup_url, ""]),
    client_error: firstStringFromUnknownValue([result.client_error, ""]),
  };
}
