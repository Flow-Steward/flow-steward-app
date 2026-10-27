import { isHttpError } from "@/lib/http/errors";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export type McpFormErrorField = "name" | "endpoint" | "headers" | "requested_scopes" | "authorization_parameters" | "refresh_token_required" | "client_id" | "client_secret" | "";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function mcpFormErrorField(error: unknown): McpFormErrorField {
  if (!isHttpError(error)) return "";
  const payload = record(error.payload);
  const nested = record(payload.error);
  const raw = firstStringFromUnknownValue([payload.field, nested.field]).trim().toLowerCase();
  if (["base_url", "endpoint", "transport"].includes(raw)) return "endpoint";
  if (["headers", "header"].includes(raw)) return "headers";
  if (raw === "requested_scopes") return "requested_scopes";
  if (["authorization_parameters", "oauth_config"].includes(raw)) return "authorization_parameters";
  if (raw === "refresh_token_required") return "refresh_token_required";
  if (raw === "client_id" || raw === "client_secret") return raw;
  if (raw === "display_name" || raw === "name") return "name";
  return "";
}
