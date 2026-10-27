import { isHttpError } from "@/lib/http/errors";
import { textOrEmpty } from "@/pages/workflows/detail/detail-test-run";
import { fallbackContext, type I18nContextValue, type TranslateParams } from "@/lib/i18n/context";
import { fetchJson } from "@/lib/http/client";
import type { WorkflowWebhookTrigger } from "@/pages/workflows/detail/detail-core";
import { objectOrEmpty } from "@/pages/workflows/detail/detail-interface";
import {
  SIGNED_WEBHOOK_AUTH_MODE,
  signedWebhookDraftFromAuthConfig,
  signedWebhookPlaceholderHeaders,
} from "@/pages/workflows/detail/detail-webhook-signature-builder";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export function workflowIOIssueMessage(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  const issue = objectOrEmpty(value);
  const message = textOrEmpty(issue.message) || t("react.workflows.validation_error", "Validation error");
  const key = textOrEmpty(issue.message_key);
  const params = Object.fromEntries(Object.entries(objectOrEmpty(issue.message_params)).filter(([, parameter]) =>
    parameter === null || ["string", "number", "boolean"].includes(typeof parameter),
  )) as TranslateParams;
  return key ? t(key, message, params) : message;
}

export function workflowValidationErrorMessage(
  error: unknown,
  fallback: string,
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  if (isHttpError(error)) {
    const payload = objectOrEmpty(error.payload);
    const detail = objectOrEmpty(objectOrEmpty(payload.error).details ?? payload.detail);
    const errors = objectOrEmpty(detail.validation).errors;
    if (Array.isArray(errors)) {
      const messages = errors.filter((issue) => Boolean(textOrEmpty(objectOrEmpty(issue).message)))
        .map((issue) => workflowIOIssueMessage(issue, t));
      if (messages.length) return messages.join("\n");
    }
  }
  return error instanceof Error ? error.message : fallback;
}

export function errorMessageFromPayload(
  payload: Record<string, unknown>,
  fallback: string,
): string {
  const detail = payload.detail;
  if (typeof detail === "string" && detail.trim()) {
    return detail.trim();
  }
  if (detail && typeof detail === "object") {
    const detailMap = detail as Record<string, unknown>;
    const message = firstStringFromUnknownValue([detailMap.message, detailMap.error, ""]).trim();
    if (message) {
      return message;
    }
  }
  const error = firstStringFromUnknownValue([payload.error, ""]).trim();
  return error || fallback;
}

export async function saveJson(
  path: string,
  init: RequestInit,
): Promise<Record<string, unknown>> {
  return fetchJson<Record<string, unknown>>(path, init);
}

export function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return "{}";
  }
}

export function parseJsonObjectDraft(
  draft: string,
  options: { label: string }, t: I18nContextValue["t"] = fallbackContext.t
): Record<string, unknown> {
  const { label } = options;
  try {
    const parsed = JSON.parse(firstStringFromUnknownValue([draft, "{}"]));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new TypeError(t("react.workflows.value0_must_be_a_json_object", "{value0} must be a JSON object.", { value0: label }));
    }
    return parsed as Record<string, unknown>;
  } catch (error) {
    if (error instanceof TypeError) {
      throw error;
    }
    throw new Error(t("react.workflows.value0_must_be_valid_json", "{value0} must be valid JSON.", { value0: label }));
  }
}

export function parseJsonArrayDraft(
  draft: string,
  options: { label: string }, t: I18nContextValue["t"] = fallbackContext.t
): Array<Record<string, unknown>> {
  const { label } = options;
  try {
    const parsed = JSON.parse(firstStringFromUnknownValue([draft, "[]"]));
    if (!Array.isArray(parsed)) {
      throw new TypeError(t("react.workflows.display_label_json_array", "{label} must be a JSON array.", { label }));
    }
    return parsed
      .filter((item) => Boolean(item) && typeof item === "object")
      .map((item) => objectOrEmpty(item));
  } catch (error) {
    if (
      error instanceof TypeError
    ) {
      throw error;
    }
    throw new Error(t("react.workflows.value0_must_be_valid_json", "{value0} must be valid JSON.", { value0: label }));
  }
}

export function webhookExamplePairs(payloadDraft: string, t: I18nContextValue["t"] = fallbackContext.t): Array<[string, string]> {
  try {
    const parsed = parseJsonObjectDraft(payloadDraft, { label: t("react.workflows.dry_run_body", "Dry-run body") }, t);
    const entries = Object.entries(parsed).slice(0, 3);
    if (entries.length) {
      return entries.map(([key, value]) => [key, stringFromUnknownValue(value ?? "")]);
    }
  } catch {
    // Fall back to a predictable sample if the draft is not a JSON object.
  }
  return [["title", "Sync inventory now"]];
}

function escapeShellSingleQuoted(value: string): string {
  return value.replaceAll("'", String.raw`\'`);
}

export function splitMultilineList(value: string): string[] {
  return firstStringFromUnknownValue([value, ""])
    .split("\n")
    .map((item) => item.trim())
    .filter(isTruthy);
}

export function joinMultilineList(value: unknown): string {
  if (!Array.isArray(value)) {
    return "";
  }
  return value
    .map((item) => firstStringFromUnknownValue([item, ""]).trim())
    .filter(isTruthy)
    .join("\n");
}

export function webhookInputMappingDraftFromValue(
  value: unknown,
): Record<string, string> {
  if (!value || typeof value !== "object") {
    return {};
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .map(([key, rawValue]) => [key, firstStringFromUnknownValue([rawValue, ""]).trim()])
      .filter(([key, rawValue]) => Boolean(key.trim()) && Boolean(rawValue)),
  );
}

export function compactWebhookInputMapping(
  value: Record<string, string>,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(value)
      .map(([key, rawValue]) => [key, firstStringFromUnknownValue([rawValue, ""]).trim()])
      .filter(([key, rawValue]) => Boolean(key.trim()) && Boolean(rawValue)),
  );
}

function webhookAuthLines(authMode: string, authConfig: Record<string, unknown>): string[] {
  if (authMode === "basic_auth") {
    const username = firstStringFromUnknownValue([authConfig.username, "workflow"]).trim() || "workflow";
    return [`  -u '${username}:<password>' \\`];
  }
  if (authMode === "shared_secret_header") {
    const name = firstStringFromUnknownValue([authConfig.header_name, "X-Workflow-Secret"]).trim() || "X-Workflow-Secret";
    return [`  -H '${name}: <shared-secret>' \\`];
  }
  if (authMode === "hmac_sha256") {
    const name = firstStringFromUnknownValue([authConfig.header_name, "X-Workflow-Signature"]).trim() || "X-Workflow-Signature";
    const prefix = firstStringFromUnknownValue([authConfig.prefix, "sha256="]).trim() || "sha256=";
    return [`  -H '${name}: ${prefix}<hex-digest>' \\`];
  }
  if (authMode === SIGNED_WEBHOOK_AUTH_MODE) {
    const { draft, supported } = signedWebhookDraftFromAuthConfig(authConfig);
    const placeholders = supported
      ? signedWebhookPlaceholderHeaders(draft)
      : ([['X-Signature', '<hex-digest>']] as Array<[string, string]>);
    return placeholders.map(([name, value]) => `  -H '${name}: ${value}' \\`);
  }
  return ["  -H 'Authorization: Bearer <token>' \\"];
}

export function webhookCurlExample(
  webhook: WorkflowWebhookTrigger,
  payloadDraft: string, t: I18nContextValue["t"] = fallbackContext.t
): string {
  const authMode = firstStringFromUnknownValue([webhook.auth_mode, "bearer_token"]).trim();
  const authConfig = objectOrEmpty(webhook.auth_config);
  const allowedContentTypes = Array.isArray(webhook.allowed_content_types)
    ? webhook.allowed_content_types.filter((item) => Boolean(firstStringFromUnknownValue([item, ""]).trim()))
    : [];
  const contentType = allowedContentTypes.includes("application/json")
    ? "application/json"
    : firstStringFromUnknownValue([allowedContentTypes[0], "application/json"]);
  const lines = [`curl -X POST '${webhook.public_url}' \\`];
  lines.push(...webhookAuthLines(authMode, authConfig));
  if (firstStringFromUnknownValue([webhook.idempotency_mode, "none"]).trim().toLowerCase() === "header") {
    lines.push("  -H 'Idempotency-Key: <unique-key>' \\");
  }
  if (contentType === "multipart/form-data") {
    for (const [key, value] of webhookExamplePairs(payloadDraft, t)) {
      lines.push(`  -F '${key}=${escapeShellSingleQuoted(value)}' \\`);
    }
    lines.push("  -F 'files=@/path/to/sample.json'");
    return lines.join("\n");
  }
  if (contentType === "application/x-www-form-urlencoded") {
    lines.push(`  -H 'Content-Type: ${contentType}' \\`);
    const pairs = webhookExamplePairs(payloadDraft, t);
    pairs.forEach(([key, value], index) => {
      const suffix = index === pairs.length - 1 ? "" : " \\";
      lines.push(
        `  --data-urlencode '${key}=${escapeShellSingleQuoted(value)}'${suffix}`,
      );
    });
    return lines.join("\n");
  }
  if (contentType === "text/plain") {
    lines.push(`  -H 'Content-Type: ${contentType}' \\`, "  --data-raw 'Sync inventory now'");
    return lines.join("\n");
  }
  lines.push(
    `  -H 'Content-Type: ${contentType}' \\`,
    `  -d '${escapeShellSingleQuoted(payloadDraft)}'`,
  );
  return lines.join("\n");
}
