import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export const HTTP_FIELDS = ["method", "url", "headers", "query_params", "body_template", "response_target", "expected_statuses", "timeout_seconds", "response_mode", "max_response_bytes", "artifact_filename", "auth_mode", "auth_ref"] as const;
export type HttpField = typeof HTTP_FIELDS[number];
export type HttpValueMode = "text" | "json";
export type HttpParse = { ok: true; value: unknown } | { ok: false; error: "json" | "finite" };
type Translate = (key: string, fallback: string) => string;
const fallback: Translate = (_key, value) => value;
function finiteJson(value: unknown): boolean {
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(finiteJson);
  if (value && typeof value === "object") return Object.values(value).every(finiteJson);
  return true;
}
export function formatHttpValue(value: unknown): { mode: HttpValueMode; text: string } {
  if (value === undefined) return { mode: "text", text: "" };
  return typeof value === "string" ? { mode: "text", text: value } : { mode: "json", text: JSON.stringify(value, null, 2) ?? "" };
}
export function parseHttpValue(mode: HttpValueMode, text: string): HttpParse {
  if (mode === "text") return { ok: true, value: text };
  try {
    const value: unknown = JSON.parse(text);
    return finiteJson(value) ? { ok: true, value } : { ok: false, error: "finite" };
  } catch { return { ok: false, error: "json" }; }
}
export function parseHttpStatuses(text: string): { ok: true; value: number[] } | { ok: false } {
  if (!text.trim()) return { ok: true, value: [] };
  const tokens = text.split(",").map((value) => value.trim());
  if (tokens.some((value) => !/^-?\d+$/.test(value))) return { ok: false };
  const value = tokens.map(Number);
  // No rounding, overflow or JSON.stringify(Infinity)->null at this boundary.
  if (!value.every(Number.isSafeInteger)) return { ok: false };
  return { ok: true, value };
}
export function httpReadinessDisplay(config: Record<string, unknown>, t: Translate = fallback): Partial<Record<HttpField, string>> {
  const messages: Partial<Record<HttpField, string>> = {};
  const method = config.method;
  const source = method && typeof method === "object" && !Array.isArray(method) ? (method as Record<string, unknown>).source : undefined;
  const methodText = typeof method === "string" ? method.trim() : "";
  if (!(typeof source === "string" && source.trim()) && !(methodText.includes("{{") && methodText.includes("}}")) && !["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].includes(methodText.toUpperCase())) {
    messages.method = t("react.workflows.http_method_readiness", "Choose a method or configure a template/source in Advanced before running.");
  }
  if (!stringFromUnknownValue(config.url).trim()) messages.url = t("react.workflows.http_url_readiness", "A URL is needed to run this step.");
  if (!stringFromUnknownValue(config.response_target).trim()) messages.response_target = t("react.workflows.http_target_readiness", "A response target is needed to run this step.");
  if (!Array.isArray(config.expected_statuses) || !config.expected_statuses.length || !config.expected_statuses.every((code) => Number.isInteger(code) && code >= 100 && code <= 599)) {
    messages.expected_statuses = t("react.workflows.http_status_readiness", "Running requires a nonempty list of integer HTTP statuses from 100 to 599.");
  }
  if (stringFromUnknownValue(config.auth_mode || "none").trim().toLowerCase() !== "none" && !stringFromUnknownValue(config.auth_ref).trim()) messages.auth_ref = t("react.workflows.http_auth_readiness", "This authentication mode needs a project secret reference before running.");
  return messages;
}
export function httpIssueField(path: string, stepId: string): HttpField | undefined {
  const prefixes = [`workflow.steps.${stepId}.override.http_request.`, "operations[0].patch.override.http_request."];
  const prefix = prefixes.find((candidate) => path.startsWith(candidate));
  if (!prefix) return undefined;
  const suffix = path.slice(prefix.length);
  return HTTP_FIELDS.find((field) => suffix === field || suffix.startsWith(`${field}.`) || suffix.startsWith(`${field}[`));
}
