import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { type ReactNode } from "react";

import { keyValueObject as serializeKeyValueRows, type KeyValueRow } from "../key-value-rows";

export { keyValueRowProblem, RESERVED_OAUTH_PARAMETER_IDENTITIES, type KeyValueRow } from "../key-value-rows";

import { isHttpError } from "@/lib/http/errors";

import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

/** The `message` state reports successes and failures alike, so the outcome
 *  travels with the text and picks the alert tone. */
export type Outcome = "success" | "danger" | "neutral";

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

export function mcpListRoute(projectId: string): string {
  return projectId
    ? `/registry/mcp?project_id=${encodeURIComponent(projectId)}`
    : "/registry/mcp";
}

export function isNotFoundHttpError(error: unknown): boolean {
  return isHttpError(error) && error.status === 404;
}

export function projectedToolLabel(
  isInTrash: boolean,
  projectedTool: { enabled?: boolean } | undefined, t: I18nContextValue["t"] = fallbackContext.t
): string {
  if (isInTrash) return t("react.registry.in_trash", "In Trash");
  if (!projectedTool) return t("react.registry.not_published", "not published");
  return projectedTool.enabled ? t("react.registry.published", "published") : t("react.registry.published_disabled", "published disabled");
}

export function projectedToolTone(
  isInTrash: boolean,
  projectedTool: { enabled?: boolean } | undefined,
): "disabled" | "linked" | "available" {
  if (isInTrash) return "disabled";
  if (!projectedTool) return "available";
  return projectedTool.enabled ? "linked" : "disabled";
}

export function storedHeadersMessage(mode: McpSettingsDraft["headers_mode"], t: I18nContextValue["t"] = fallbackContext.t): string {
  if (mode === "clear") return t("react.registry.saved_headers_will_be_removed", "Saved headers will be removed.");
  if (mode === "replace") return t("react.registry.saved_headers_will_be_replaced_by_the_rows_above", "Saved headers will be replaced by the rows above.");
  return t("react.registry.saved_headers_stay_masked_and_unchanged", "Saved headers stay masked and unchanged.");
}

export function oauthStartLabel(pending: boolean, mustRestart: boolean, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (pending) return t("react.extensions.starting", "Starting…");
  return mustRestart ? t("react.registry.reconnect_authorization", "Reconnect Authorization") : t("react.registry.start_authorization", "Start Authorization");
}

export function formatDateTime(value: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (!value) {
    return t("react.registry.mcp_final_never_date", "Never");
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function apiErrorCode(error: unknown): string {
  if (!isHttpError(error) || !error.payload || typeof error.payload !== "object" || Array.isArray(error.payload)) {
    return "";
  }
  return firstStringFromUnknownValue([(error.payload as Record<string, unknown>).code, ""]).trim();
}

export function propertyNamesFromSchema(schema: Record<string, unknown>): string[] {
  const properties =
    schema.properties && typeof schema.properties === "object" && !Array.isArray(schema.properties)
      ? (schema.properties as Record<string, unknown>)
      : {};
  return Object.keys(properties);
}

/* Ported with the merge: upstream added the connector-settings editor, whose
   state and markup survived while its declarations sat in a conflict. */
export type McpSettingsDraft = {
  display_name: string;
  base_url: string;
  transport: string;
  auth_mode: string;
  namespace_prefix: string;
  description: string;
  enabled: boolean;
  headers_mode: "keep" | "replace" | "clear";
  replacement_headers: KeyValueRow[];
  requested_scopes: string[];
  authorization_parameters: KeyValueRow[];
  refresh_token_required: boolean;
};

export function patchSettingsDraft(
  current: McpSettingsDraft | null,
  patch: Partial<McpSettingsDraft>,
): McpSettingsDraft | null {
  return current ? { ...current, ...patch } : current;
}

export function updateRequestedScope(
  current: McpSettingsDraft | null,
  index: number,
  value: string,
): McpSettingsDraft | null {
  if (!current) return current;
  return {
    ...current,
    requested_scopes: current.requested_scopes.map((item, itemIndex) => (
      itemIndex === index ? value : item
    )),
  };
}

export function removeRequestedScope(
  current: McpSettingsDraft | null,
  index: number,
): McpSettingsDraft | null {
  if (!current) return current;
  return {
    ...current,
    requested_scopes: current.requested_scopes.filter((_, itemIndex) => itemIndex !== index),
  };
}

export function updateSettingsAuthMode(
  current: McpSettingsDraft | null,
  authMode: string,
): McpSettingsDraft | null {
  if (!current) return current;
  const clearsCredentialHeaders = ["bearer", "api_key"].includes(current.auth_mode)
    && ["none", "oauth2"].includes(authMode);
  return {
    ...current,
    auth_mode: authMode,
    ...(clearsCredentialHeaders
      ? { headers_mode: "clear" as const, replacement_headers: [] }
      : {}),
  };
}

export function updateReplacementHeaders(
  current: McpSettingsDraft | null,
  replacementHeaders: KeyValueRow[],
): McpSettingsDraft | null {
  if (!current) return current;
  return {
    ...current,
    replacement_headers: replacementHeaders,
    ...(replacementHeaders.length ? { headers_mode: "replace" as const } : {}),
  };
}

export function updateStoredHeadersMode(
  current: McpSettingsDraft | null,
  mode: McpSettingsDraft["headers_mode"],
): McpSettingsDraft | null {
  if (!current) return current;
  return {
    ...current,
    headers_mode: mode,
    ...(mode === "clear" ? { replacement_headers: [] } : {}),
  };
}

export function keyValueObject(
  rows: KeyValueRow[],
  label: string | undefined = undefined,
  reservedKeys?: ReadonlySet<string>,
  t: I18nContextValue["t"] = fallbackContext.t,
): Record<string, string> | undefined {
  const result = serializeKeyValueRows(rows, label ?? t("react.registry.mcp_final_replacement_header", "Replacement header"), reservedKeys, t);
  return Object.keys(result).length ? result : undefined;
}

export function settingsDraftFromConnector(connector: {
  display_name: string;
  base_url: string;
  transport: string;
  auth_mode: string;
  namespace_prefix: string;
  description: string;
  enabled: boolean;
  oauth_requested_scopes: string[];
  oauth_authorization_parameters: Record<string, string>;
  oauth_refresh_token_required: boolean;
}): McpSettingsDraft {
  return {
    display_name: connector.display_name,
    base_url: connector.base_url,
    transport: "streamable_http",
    auth_mode: connector.auth_mode,
    namespace_prefix: connector.namespace_prefix,
    description: connector.description,
    enabled: connector.enabled,
    headers_mode: "keep",
    // Connector headers can contain credentials. Replacement is explicit and is
    // intentionally never prefilled from a detail response.
    replacement_headers: [],
    requested_scopes: connector.oauth_requested_scopes,
    authorization_parameters: Object.entries(connector.oauth_authorization_parameters).map(([key, value], index) => ({ id: index + 1, key, value })),
    refresh_token_required: connector.oauth_refresh_token_required,
  };
}

export function isValidHttpEndpoint(value: string): boolean {
  try {
    const endpoint = new URL(value);
    return endpoint.protocol === "http:" || endpoint.protocol === "https:";
  } catch {
    return false;
  }
}
