import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { ReactNode } from "react";

import {
  fetchWorkflowTestRunState,
  type StepTestRunResult,
  type WorkflowTestRunResult,
} from "@/pages/workflows/shared/simulate-api";
import { objectOrEmpty } from "@/pages/workflows/detail/detail-interface";
import { isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export function entityOrEmpty(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function textOrEmpty(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  return stringFromUnknownValue(value).trim();
}

export function httpRequestLabel(value: unknown): string {
  const request = objectOrEmpty(objectOrEmpty(value).request);
  const method = textOrEmpty(request.method);
  const url = textOrEmpty(request.url);
  if (!method && !url) {
    return "—";
  }
  return `${method || "—"} ${url || "—"}`;
}

/** Saved request configuration is visible for review, never a Test Run override. */
export function savedHttpTestRunSummary(value: unknown, configuredLabel: string): Record<string, string> {
  const config = objectOrEmpty(value);
  const savedMethod = textOrEmpty(config.method).toUpperCase() || "GET";
  const method = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].includes(savedMethod)
    ? savedMethod : configuredLabel;
  const savedUrl = textOrEmpty(config.url);
  let url = configuredLabel;
  if (savedUrl && !savedUrl.includes("{{") && !savedUrl.includes("}}")) {
    try {
      const parsed = new URL(savedUrl);
      if (parsed.protocol === "https:" || parsed.protocol === "http:") {
        parsed.username = "";
        parsed.password = "";
        parsed.search = "";
        parsed.hash = "";
        url = parsed.toString();
      }
    } catch {
      // Invalid and unresolved destinations are edited in the saved configuration.
    }
  }
  return { method, url };
}

export function normalizedType(value: unknown): string {
  return textOrEmpty(value).toLowerCase();
}

export function inputSupportsNumber(type: unknown): boolean {
  return ["int", "integer", "float", "number"].includes(normalizedType(type));
}

export function inputSupportsBoolean(type: unknown): boolean {
  return ["bool", "boolean"].includes(normalizedType(type));
}

export function inputSupportsJson(type: unknown): boolean {
  return ["json", "object", "array"].includes(normalizedType(type));
}

export function inputSupportsParagraph(type: unknown): boolean {
  return ["paragraph", "multiline", "textarea", "markdown"].includes(
    normalizedType(type),
  );
}

/**
 * A test value that is an empty text on purpose. An empty field means "not
 * given", which a template reads as missing (and its default applies); this
 * value is sent as "" instead. It cannot be typed, and it never leaves the page.
 */
export const EXPLICIT_EMPTY_TEST_VALUE = "\u0000empty";

/** The field's text as typed; the shared string helpers trim. */
function rawTestRunValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** Text inputs: anything that is not a number, a boolean or JSON. */
export function inputIsText(type: unknown): boolean {
  return !inputSupportsNumber(type) && !inputSupportsBoolean(type) && !inputSupportsJson(type);
}

/**
 * Whether a field holds a value to send. A text keeps every character, so
 * spaces alone are a value; other types need something besides spaces.
 */
export function testRunValueGiven(type: unknown, rawValue: string): boolean {
  const raw = rawTestRunValue(rawValue);
  if (raw === EXPLICIT_EMPTY_TEST_VALUE) return inputIsText(type);
  return inputIsText(type) ? raw !== "" : Boolean(raw.trim());
}

export function parseTestRunInputValue(type: unknown, rawValue: string): unknown {
  const normalized = normalizedType(type);
  const raw = rawTestRunValue(rawValue);
  if (raw === EXPLICIT_EMPTY_TEST_VALUE) {
    return "";
  }
  // A text is sent exactly as typed: line breaks and edge spaces are data.
  if (inputIsText(normalized)) {
    return raw;
  }
  const text = raw.trim();
  if (!text) {
    return "";
  }
  if (inputSupportsBoolean(normalized)) {
    if (text === "true") {
      return true;
    }
    if (text === "false") {
      return false;
    }
  }
  if (inputSupportsNumber(normalized)) {
    const parsed = Number(text);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  if (inputSupportsJson(normalized)) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

export function testRunPayloadFromInputs(
  rows: Array<{
    name: string;
    type?: string;
  }>,
  values: Record<string, string>,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const row of rows) {
    const name = textOrEmpty(row.name);
    if (!name) {
      continue;
    }
    const rawValue = rawTestRunValue(values[name]);
    if (!testRunValueGiven(row.type, rawValue)) {
      continue;
    }
    payload[name] = parseTestRunInputValue(row.type, rawValue);
  }
  return payload;
}

export function testRunInputBindingsFromRows(
  rows: Array<{
    name: string;
    source?: string;
    type?: string;
  }>,
  values: Record<string, string>,
): Array<Record<string, unknown>> {
  const bindings: Array<Record<string, unknown> | null> = rows.map((row) => {
      const name = textOrEmpty(row.name);
      if (!name) {
        return null;
      }
      const rawValue = rawTestRunValue(values[name]);
      if (!testRunValueGiven(row.type, rawValue)) {
        return null;
      }
      const source = textOrEmpty(row.source);
      if (!source?.startsWith("state.")) {
        return null;
      }
      return {
        name,
        source,
        type: textOrEmpty(row.type) || "text",
      };
    });
  return bindings.filter(
    (row): row is Record<string, unknown> => row !== null,
  );
}

export function testRunStatus(value: unknown): string {
  const normalized = normalizedType(value);
  if (["completed", "pass", "success"].includes(normalized)) {
    return "pass";
  }
  if (["failed", "fail", "error"].includes(normalized)) {
    return "fail";
  }
  if (
    normalized === "awaiting_approval"
    || normalized === "awaiting_timer"
    || normalized === "awaiting_child"
  ) {
    return normalized;
  }
  if (normalized === "in_progress" || normalized === "resume_pending") {
    return "in_progress";
  }
  return normalized || "unknown";
}

export function testRunBannerMessage(
  kind: "workflow" | "step",
  payload: { final_status?: unknown; status?: unknown } | null | undefined, t: I18nContextValue["t"] = fallbackContext.t
): string {
  const label = kind === "workflow" ? "Workflow" : "Step";
  const status = testRunStatus(payload?.final_status || payload?.status);
  if (status === "fail") {
    return t("react.workflows.value0_test_run_failed", "{value0} test run failed.", { value0: label });
  }
  if (status === "in_progress") {
    return t("react.workflows.value0_test_run_is_still_in_progress", "{value0} test run is still in progress.", { value0: label });
  }
  if (status === "queued" || status === "pending") {
    return t("react.workflows.value0_test_run_is_still_value1", "{value0} test run is still {value1}.", { value0: label, value1: status });
  }
  if (status === "awaiting_approval") {
    return t("react.workflows.value0_test_run_is_waiting_for_approval", "{value0} test run is waiting for approval.", { value0: label });
  }
  if (status === "awaiting_timer") {
    return t("react.workflows.value0_test_run_is_waiting_on_a_wait_step", "{value0} test run is paused on a Wait step.", { value0: label });
  }
  if (status === "awaiting_child") {
    return t("react.workflows.value0_test_run_is_waiting_for_a_child_workflow", "{value0} test run is waiting for a child workflow.", { value0: label });
  }
  if (status === "pass") {
    return t("react.workflows.value0_test_run_completed_successfully", "{value0} test run completed successfully.", { value0: label });
  }
  return t("react.workflows.value0_test_run_completed", "{value0} test run completed.", { value0: label });
}

export function stepTestRunFailureDetail(
  payload: {
    error?: unknown;
    final_report?: unknown;
    final_status?: unknown;
    status?: unknown;
  } | null | undefined,
): string {
  if (testRunStatus(payload?.final_status || payload?.status) !== "fail") {
    return "";
  }
  return textOrEmpty(payload?.error) || textOrEmpty(payload?.final_report);
}

export function workflowTestRunIsPending(
  payload: { final_status?: unknown; status?: unknown } | null | undefined,
): boolean {
  const normalized = normalizedType(payload?.final_status || payload?.status);
  return (
    normalized === "in_progress"
    || normalized === "resume_pending"
    || normalized === "pending"
    || normalized === "queued"
    || normalized === "awaiting_approval"
    || normalized === "awaiting_timer"
    || normalized === "awaiting_child"
  );
}

export function workflowTestRunNeedsApproval(
  payload: WorkflowTestRunResult | null | undefined,
): boolean {
  if (!payload) {
    return false;
  }
  return Boolean(payload.needs_user_approval && textOrEmpty(payload.thread_id));
}

export async function waitForWorkflowTestRunState(
  threadId: string,
  {
    attempts = 40,
    includeAwaitingApproval = false,
    intervalMs = 1500,
  }: {
    attempts?: number;
    includeAwaitingApproval?: boolean;
    intervalMs?: number;
  } = {},
): Promise<WorkflowTestRunResult> {
  let latest = await fetchWorkflowTestRunState(threadId, { presentation: "background" });
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const awaitingDecision = testRunStatus(
      latest.final_status || latest.status,
    ) === "awaiting_approval";
    if (
      !workflowTestRunIsPending(latest)
      || (awaitingDecision && !includeAwaitingApproval)
    ) {
      return latest;
    }
    await new Promise((resolve) => globalThis.setTimeout(resolve, intervalMs));
    latest = await fetchWorkflowTestRunState(threadId, { presentation: "background" });
  }
  return latest;
}

export function mergeStepTestRunState(
  submitted: StepTestRunResult,
  durable: WorkflowTestRunResult,
): StepTestRunResult {
  return {
    ...submitted,
    ...durable,
    workflow_id: durable.workflow_id || submitted.workflow_id,
    step_id: durable.step_id || submitted.step_id,
    step_kind: durable.step_kind || submitted.step_kind,
  };
}

export function compactValuePreview(value: unknown): string {
  if (value === null || value === undefined) {
    return "null";
  }
  if (typeof value === "string") {
    const normalized = value.trim();
    return normalized.length > 140 ? `${normalized.slice(0, 137)}...` : normalized || '""';
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return stringFromUnknownValue(value);
  }
  if (Array.isArray(value)) {
    return value.length ? `${value.length} item(s)` : "[]";
  }
  if (typeof value === "object") {
    const keys = Object.keys(objectOrEmpty(value));
    return keys.length ? `${keys.length} field(s)` : "{}";
  }
  return stringFromUnknownValue(value);
}

export function previewableObjectEntries(
  value: Record<string, unknown>,
): Array<[string, unknown]> {
  return Object.entries(value)
    .filter(([, entryValue]) => entryValue !== undefined && entryValue !== null && entryValue !== "")
    .sort(([leftKey], [rightKey]) => {
      const leftIsIdLike = /(^id$|_id$)/i.test(leftKey);
      const rightIsIdLike = /(^id$|_id$)/i.test(rightKey);
      if (leftIsIdLike === rightIsIdLike) {
        return 0;
      }
      return leftIsIdLike ? 1 : -1;
    });
}

function compactArrayValuePreview(
  value: unknown[],
  maxDepth: number,
  maxEntries: number,
  t: I18nContextValue["t"],
): string {
  if (!value.length) return "[]";
  const scalarItems = value.filter(
    (entryValue): entryValue is string | number | boolean =>
      typeof entryValue === "string" ||
      typeof entryValue === "number" ||
      typeof entryValue === "boolean",
  );
  if (!scalarItems.length) return t("react.workflows.display_preview_item_count", "{count} item(s)", { count: value.length });
  const shownItems = scalarItems
    .slice(0, Math.min(2, maxEntries))
    .map((entryValue) => compactValuePreview(entryValue));
  const remainingItems = scalarItems.length - shownItems.length;
  return remainingItems > 0
    ? t("react.workflows.display_preview_more_items", "{preview}; +{count} more item(s)", { preview: shownItems.join(", "), count: remainingItems })
    : shownItems.join(", ");
}

function compactNestedValuePreview(entryValue: unknown, maxDepth: number, t: I18nContextValue["t"]): string {
  if (Array.isArray(entryValue)) {
    return compactLabeledValuePreview(entryValue, {
      maxDepth: Math.max(1, maxDepth - 1),
      maxEntries: 2,
    }, t);
  }
  if (maxDepth > 1) {
    return compactLabeledValuePreview(entryValue, {
      maxDepth: maxDepth - 1,
      maxEntries: 2,
    }, t);
  }
  return compactValuePreview(entryValue);
}

function compactObjectValuePreview(
  value: unknown,
  maxDepth: number,
  maxEntries: number,
  t: I18nContextValue["t"],
): string {
  const entries = previewableObjectEntries(objectOrEmpty(value));
  if (!entries.length) return "{}";
  const previewParts: string[] = [];
  for (const [key, entryValue] of entries) {
    if (previewParts.length >= maxEntries) break;
    if (["string", "number", "boolean"].includes(typeof entryValue)) {
      previewParts.push(`${key}: ${compactValuePreview(entryValue)}`);
      continue;
    }
    const preview = compactNestedValuePreview(entryValue, maxDepth, t);
    previewParts.push(`${key}: ${preview}`);
  }
  const remainingEntries = entries.length - previewParts.length;
  return remainingEntries > 0
    ? t("react.workflows.display_preview_more_fields", "{preview}; +{count} more field(s)", { preview: previewParts.join("; "), count: remainingEntries })
    : previewParts.join("; ");
}

export function compactLabeledValuePreview(
  value: unknown,
  {
    maxDepth = 2,
    maxEntries = 3,
  }: {
    maxDepth?: number;
    maxEntries?: number;
  } = {},
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  if (value === null || value === undefined) {
    return compactValuePreview(value);
  }
  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return compactValuePreview(value);
  }
  if (Array.isArray(value)) {
    return compactArrayValuePreview(value, maxDepth, maxEntries, t);
  }
  return compactObjectValuePreview(value, maxDepth, maxEntries, t);
}

export function renderKeyValueSummary(
  value: unknown,
  emptyLabel: string,
  t: I18nContextValue["t"] = fallbackContext.t,
): ReactNode {
  const entries = Object.entries(objectOrEmpty(value));
  if (!entries.length) {
    return <p className="fs-ink-tertiary">{emptyLabel}</p>;
  }
  return (
    <div className="space-y-2">
      {entries.map(([key, entryValue]) => (
        <div
          className="rounded-lg border border-default fs-surface-inset px-2 py-2"
          key={key}
        >
          <p className="font-mono fs-ink-primary">{key}</p>
          <p className="mt-1 fs-ink-secondary">{compactLabeledValuePreview(entryValue, {}, t)}</p>
        </div>
      ))}
    </div>
  );
}

export function hasPreviewContent(value: unknown): boolean {
  if (value === null || value === undefined) {
    return false;
  }
  if (typeof value === "string") {
    return value.trim().length > 0;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return true;
  }
  if (Array.isArray(value)) {
    return value.some((entryValue) => hasPreviewContent(entryValue));
  }
  return previewableObjectEntries(objectOrEmpty(value)).length > 0;
}

export function hasStructuredEntries(value: unknown): boolean {
  return Object.keys(objectOrEmpty(value)).length > 0;
}

export function filteredStatePatchPreview(value: unknown): Record<string, unknown> {
  const statePatch = objectOrEmpty(value);
  const entries = previewableObjectEntries(statePatch);
  if (!entries.length) {
    return statePatch;
  }
  const filteredEntries = entries.filter(([key]) => {
    if (
      (key === "workflow_data" || key === "reentry_context")
      && entries.some(([candidateKey]) => candidateKey !== key)
    ) {
      return false;
    }
    return true;
  });
  return Object.fromEntries(filteredEntries);
}

export function filteredTraceArtifactPreview(value: unknown): Record<string, unknown> {
  const artifacts = objectOrEmpty(value);
  const ignoredKeys = new Set([
    "step_kind",
    "step_id",
    "step_display_name",
    "assigned",
    "input_payload",
    "_state_patch",
    "workflow_outputs",
    "workflow_terminal",
    "connector_invocation",
    "connector_result",
    "http_request",
    "dataset_query",
    "_invoke_workflow",
  ]);
  return Object.fromEntries(
    previewableObjectEntries(artifacts).filter(([key]) => !ignoredKeys.has(key)),
  );
}

export function primaryArtifactValue(value: unknown): unknown {
  const result = objectOrEmpty(value);
  const preferredKeys = ["result", "output", "response", "connector_result"];
  for (const key of preferredKeys) {
    if (result[key] !== undefined && result[key] !== null && result[key] !== "") {
      return result[key];
    }
  }
  const ignoredKeys = new Set([
    "step_kind",
    "step_id",
    "_state_patch",
    "error",
    "input_payload",
    "connector_invocation",
    "connector_result",
  ]);
  for (const [key, entryValue] of Object.entries(result)) {
    if (ignoredKeys.has(key)) {
      continue;
    }
    if (entryValue !== undefined && entryValue !== null && entryValue !== "") {
      return { [key]: entryValue };
    }
  }
  return null;
}

function firstPreviewValue(values: unknown[]): unknown {
  return values.find((value) => hasPreviewContent(value)) ?? null;
}

type TraceOutputContext = {
  connectorResult: Record<string, unknown>;
  invokeWorkflow: Record<string, unknown>;
  primaryValue: unknown;
  statePatch: Record<string, unknown>;
  workflowOutputs: Record<string, unknown>;
  workflowTerminal: Record<string, unknown>;
};

function knownTraceOutput(stepKind: string, ctx: TraceOutputContext): { matched: boolean; value: unknown } {
  if (stepKind === "set_variable" || stepKind === "collect") {
    return { matched: true, value: firstPreviewValue([ctx.statePatch, ctx.primaryValue]) };
  }
  if (stepKind === "connector") {
    const preferred = firstPreviewValue([
      ctx.connectorResult.result,
      ctx.statePatch,
      ctx.primaryValue,
    ]);
    return { matched: true, value: preferred ?? ctx.connectorResult };
  }
  if (stepKind === "invoke_workflow") {
    const mappedOutputs = objectOrEmpty(ctx.invokeWorkflow.mapped_outputs);
    const value = firstPreviewValue([mappedOutputs, ctx.statePatch]);
    return { matched: value !== null, value };
  }
  if (stepKind === "stop_success" || stepKind === "stop_fail") {
    const value = firstPreviewValue([ctx.workflowOutputs, ctx.workflowTerminal, ctx.statePatch]);
    return {
      matched: value !== null,
      value,
    };
  }
  return { matched: false, value: null };
}

export function workflowTraceOutputValue(trace: Record<string, unknown>): unknown {
  const stepKind = textOrEmpty(trace.step_kind);
  const artifacts = objectOrEmpty(trace.artifacts);
  const statePatch = filteredStatePatchPreview(artifacts._state_patch);
  const workflowOutputs = objectOrEmpty(objectOrEmpty(artifacts.workflow_outputs).outputs);
  const workflowTerminal = objectOrEmpty(artifacts.workflow_terminal);
  const connectorResult = objectOrEmpty(artifacts.connector_result);
  const invokeWorkflow = objectOrEmpty(artifacts._invoke_workflow);
  const primaryValue = primaryArtifactValue(artifacts);

  const knownOutput = knownTraceOutput(stepKind, {
    connectorResult,
    invokeWorkflow,
    primaryValue,
    statePatch,
    workflowOutputs,
    workflowTerminal,
  });
  if (knownOutput.matched) return knownOutput.value;
  const generalOutput = firstPreviewValue([primaryValue, statePatch, workflowOutputs]);
  if (generalOutput !== null) return generalOutput;
  const filteredArtifacts = filteredTraceArtifactPreview(artifacts);
  if (hasPreviewContent(filteredArtifacts)) {
    return filteredArtifacts;
  }
  return null;
}

export function workflowTraceInputValue(
  sourcePayload: unknown,
  previousOutputValue: unknown,
): unknown {
  const sourceObject = objectOrEmpty(sourcePayload);
  const preferredKeys = [
    "inputs",
    "input_payload",
    "payload",
    "body",
    "params",
    "request",
    "query",
    "workflow_outputs",
  ];
  for (const key of preferredKeys) {
    if (hasPreviewContent(sourceObject[key])) {
      return sourceObject[key];
    }
  }
  if (hasPreviewContent(sourcePayload)) {
    return sourcePayload;
  }
  if (hasPreviewContent(previousOutputValue)) {
    return previousOutputValue;
  }
  return null;
}

export function renderPreviewSummary(
  value: unknown,
  emptyLabel: string,
  { maxEntries = 4 }: { maxEntries?: number } = {}, t: I18nContextValue["t"] = fallbackContext.t
): ReactNode {
  if (!hasPreviewContent(value)) {
    return <p className="text-[var(--fs-text-tertiary)]">{emptyLabel}</p>;
  }
  if (
    typeof value === "string"
    || typeof value === "number"
    || typeof value === "boolean"
    || Array.isArray(value)
  ) {
    return <p className="text-[var(--fs-text-primary)]">{compactLabeledValuePreview(value, {}, t)}</p>;
  }

  const entries = previewableObjectEntries(objectOrEmpty(value));
  const shownEntries = entries.slice(0, maxEntries);
  const remainingEntries = entries.length - shownEntries.length;
  return (
    <div className="space-y-1">
      {shownEntries.map(([key, entryValue]) => (
        <p className="text-[var(--fs-text-primary)]" key={key}>
          <span className="font-mono text-[var(--fs-text-primary)]">{key}</span>
          {": "}
          {compactLabeledValuePreview(entryValue, { maxEntries: 2 }, t)}
        </p>
      ))}
      {remainingEntries > 0 ? (
        <p className="text-[var(--fs-text-tertiary)]">+{remainingEntries}{" "}{t("react.workflows.more_field_s", "more field(s)")}</p>
      ) : null}
    </div>
  );
}

function renderDataStepResult(
  kind: string,
  payload: StepTestRunResult,
  result: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t
): ReactNode | null {
  if (kind === "dataset_query") {
    const runtimeResult = objectOrEmpty(objectOrEmpty(result.dataset_query).result);
    const rows = Array.isArray(runtimeResult.rows) ? runtimeResult.rows : [];
    const rowCount =
      Number(runtimeResult.row_count) ||
      Number(objectOrEmpty(result.state_patch).row_count) ||
      rows.length;
    return (
      <div className="space-y-1">
        <p>{t("react.workflows.rows_returned", "Rows returned:")}{" "}{Number.isFinite(rowCount) ? rowCount : 0}</p>
        <p>{t("react.workflows.target", "Target:")}{" "}{textOrEmpty(objectOrEmpty(payload.inputs).result_target) || "—"}</p>
        <p>{t("react.workflows.preview", "Preview:")}{" "}{rows.length ? t("react.workflows.value0_row_s_available", "{value0} row(s) available", { value0: rows.length }) : t("react.workflows.no_rows_returned", "No rows returned")}</p>
      </div>
    );
  }
  if (kind === "http_request") {
    const httpResult = objectOrEmpty(result.http_request);
    const isArtifact =
      textOrEmpty(httpResult.response_mode) === "artifact" ||
      Boolean(textOrEmpty(httpResult.artifact_handle));
    return (
      <div className="space-y-1">
        <p>{t("react.workflows.request", "Request:")}{" "}{httpRequestLabel(httpResult)}</p>
        <p>{t("react.workflows.status", "Status:")}{" "}{textOrEmpty(httpResult.status_code) || "—"}</p>
        {isArtifact ? (
          <>
            <p>{t("react.workflows.artifact", "Artifact:")}{" "}{textOrEmpty(httpResult.artifact_handle) || "—"}</p>
            <p>{t("react.workflows.filename", "Filename:")}{" "}{textOrEmpty(httpResult.filename) || "—"}</p>
            <p>{t("react.workflows.size", "Size:")}{" "}{textOrEmpty(httpResult.size_bytes) || "0"}{" "}{t("react.workflows.bytes", "bytes")}</p>
            <p>{t("react.workflows.content_type", "Content type:")}{" "}{textOrEmpty(httpResult.content_type) || "—"}</p>
          </>
        ) : (
          <p>{t("react.workflows.response", "Response:")}{" "}{compactLabeledValuePreview(httpResult.response_body ?? httpResult.response_excerpt ?? null, {}, t)}</p>
        )}
      </div>
    );
  }
  if (kind !== "connector") return null;
  const invocation = objectOrEmpty(result.connector_invocation);
  const connectorResult = objectOrEmpty(result.connector_result);
  const action = [
    result.function_name,
    result.action_id,
    invocation.action_id,
    result.tool_id,
    result.connector_id,
  ].map((value) => textOrEmpty(value)).find(isTruthy) || "—";
  return (
    <div className="space-y-1">
      <p>{t("react.workflows.action", "Action:")}{" "}{action}</p>
      <p>{t("react.workflows.status", "Status:")}{" "}{textOrEmpty(invocation.status) || textOrEmpty(payload.status) || "—"}</p>
      <p>{t("react.workflows.result", "Result:")}{" "}{compactLabeledValuePreview(connectorResult.result ?? connectorResult, {}, t)}</p>
    </div>
  );
}

/** The outcome a branching step took: the case id of a switch case, else the outcome name. */
export function selectedOutcomeLabel(result: Record<string, unknown>): string {
  const outcome = objectOrEmpty(result._outcome);
  const outputPayload = objectOrEmpty(result.output_payload);
  return (
    textOrEmpty(outcome.case_id) ||
    textOrEmpty(outcome.name) ||
    textOrEmpty(outputPayload.case_id) ||
    textOrEmpty(outputPayload.outcome) ||
    textOrEmpty(outputPayload.name) ||
    textOrEmpty(result.outcome)
  );
}

function renderDecisionStepResult(
  kind: string,
  _payload: StepTestRunResult,
  result: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t
): ReactNode | null {
  if (kind === "switch") {
    return (
      <div className="space-y-1">
        <p>{t("react.workflows.selector", "Selector:")}{" "}{compactValuePreview(result.selector)}</p>
        <p>{t("react.workflows.selected_outcome", "Selected outcome:")}{" "}{selectedOutcomeLabel(result) || "—"}</p>
      </div>
    );
  }
  if (kind !== "if") return null;
  return (
    <div className="space-y-1">
      <p>{t("react.workflows.condition_result", "Condition result:")}{" "}{compactValuePreview(result.result)}</p>
      <p>{t("react.workflows.selected_outcome", "Selected outcome:")}{" "}{selectedOutcomeLabel(result) || "—"}</p>
    </div>
  );
}

function renderExecutionStepResult(
  kind: string,
  payload: StepTestRunResult,
  result: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t
): ReactNode | null {
  if (kind === "agent") {
    return (
      <div className="space-y-1">
        <p>{t("react.workflows.executor", "Executor:")}{" "}{textOrEmpty(objectOrEmpty(payload.agent).agent_id) || "—"}</p>
        <p>{t("registry.workflow_detail.step.skill_label", "Skill:")}{" "}{textOrEmpty(objectOrEmpty(payload.agent).skill) || "—"}</p>
        <p>{t("react.workflows.result", "Result:")}{" "}{compactLabeledValuePreview(primaryArtifactValue(result), {}, t)}</p>
      </div>
    );
  }
  if (kind === "invoke_workflow") {
    const invoke = objectOrEmpty(result._invoke_workflow);
    const mappedOutputs = objectOrEmpty(invoke.mapped_outputs);
    const declaredOutputs = Array.isArray(invoke.declared_outputs) && invoke.declared_outputs.length
      ? invoke.declared_outputs.join(", ")
      : "—";
    const returnedOutputs = Object.keys(mappedOutputs).length
      ? Object.entries(mappedOutputs).map(([key, value]) => `${key} -> ${textOrEmpty(value)}`).join(", ")
      : compactValuePreview(objectOrEmpty(payload.state_patch));
    return (
      <div className="space-y-1">
        <p>{t("react.workflows.child_workflow", "Child workflow:")}{" "}{textOrEmpty(invoke.child_workflow_id) || "—"}</p>
        <p>{t("react.workflows.status", "Status:")}{" "}{textOrEmpty(invoke.child_status) || textOrEmpty(payload.status) || "—"}</p>
        <p>{t("react.workflows.declared_outputs", "Declared outputs:")}{" "}{declaredOutputs}</p>
        <p>{t("react.workflows.returned_outputs", "Returned outputs:")}{" "}{returnedOutputs}</p>
      </div>
    );
  }
  if (kind !== "set_variable") return null;
  const assigned = Array.isArray(result.assigned)
    ? result.assigned.map((value) => textOrEmpty(value)).filter(isTruthy)
    : [];
  return (
    <div className="space-y-1">
      <p>{t("react.workflows.assigned_targets", "Assigned targets:")}{" "}{assigned.length ? assigned.join(", ") : "—"}</p>
      <p>{t("react.workflows.values", "Values:")}{" "}{compactLabeledValuePreview(payload.state_patch ?? null, {}, t)}</p>
    </div>
  );
}

function renderControlStepResult(
  kind: string,
  payload: StepTestRunResult,
  result: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t
): ReactNode | null {
  if (kind === "batch") {
    return (
      <div className="space-y-1">
        <p>{t("react.workflows.source_item_count", "Source item count:")}{" "}{compactValuePreview(result.item_count)}</p>
        <p>{t("react.workflows.batch_size", "Batch size:")}{" "}{compactValuePreview(result.batch_size)}</p>
        <p>{t("react.workflows.batch_count", "Batch count:")}{" "}{compactValuePreview(result.batch_count)}</p>
        <p>{t("react.workflows.result_target", "Result target:")}{" "}{textOrEmpty(result.result_target) || "—"}</p>
      </div>
    );
  }
  if (kind === "collect") {
    return (
      <div className="space-y-1">
        <p>{t("react.workflows.collect_target_result", "Added to:")}{" "}{textOrEmpty(objectOrEmpty(result.collect).target) || "—"}</p>
        <p>{t("react.workflows.collect_count_result", "Values now:")}{" "}{compactValuePreview(objectOrEmpty(result.collect).count)}</p>
      </div>
    );
  }
  if (kind !== "stop_success" && kind !== "stop_fail") return null;
  const workflowTerminal = objectOrEmpty(result.workflow_terminal);
  return (
    <div className="space-y-1">
      <p>{t("react.workflows.terminal_reason", "Terminal reason:")}{" "}{textOrEmpty(payload.summary) || "—"}</p>
      <p>{t("react.workflows.final_status", "Final status:")}{" "}{textOrEmpty(workflowTerminal.status) || textOrEmpty(payload.final_status) || textOrEmpty(payload.status) || "—"}
      </p>
    </div>
  );
}

export function renderStepResultSummary(payload: StepTestRunResult, t: I18nContextValue["t"] = fallbackContext.t): ReactNode {
  const kind = textOrEmpty(payload.step_kind);
  const result = objectOrEmpty(payload.result);
  const renderers = [
    renderDataStepResult,
    renderDecisionStepResult,
    renderExecutionStepResult,
    renderControlStepResult,
  ];
  for (const renderer of renderers) {
    const rendered = renderer(kind, payload, result);
    if (rendered) return rendered;
  }
  return (
    <div className="space-y-1">
      <p>{t("react.workflows.summary", "Summary:")}{" "}{textOrEmpty(payload.summary) || t("react.workflows.no_summary_returned", "No summary returned.")}</p>
      <p>{t("react.workflows.result", "Result:")}{" "}{compactLabeledValuePreview(result, {}, t)}</p>
    </div>
  );
}
export function workflowTestRunOutputs(payload: WorkflowTestRunResult): Record<string, unknown> {
  const workflowOutputs = objectOrEmpty(payload.workflow_outputs);
  const nestedOutputs = objectOrEmpty(workflowOutputs.outputs);
  if (Object.keys(nestedOutputs).length) {
    return nestedOutputs;
  }
  return objectOrEmpty(payload.outputs);
}

export function workflowTestRunArtifacts(payload: WorkflowTestRunResult): Record<string, unknown> {
  const workflowOutputs = objectOrEmpty(payload.workflow_outputs);
  const nestedArtifacts = objectOrEmpty(workflowOutputs.artifacts);
  if (Object.keys(nestedArtifacts).length) {
    return nestedArtifacts;
  }
  return objectOrEmpty(payload.artifacts);
}

export function workflowTestRunFinalResult(payload: WorkflowTestRunResult): unknown {
  const outputs = workflowTestRunOutputs(payload);
  if (hasPreviewContent(outputs)) {
    return outputs;
  }
  const artifacts = workflowTestRunArtifacts(payload);
  const primaryValue = primaryArtifactValue(artifacts);
  if (hasPreviewContent(primaryValue)) {
    return primaryValue;
  }
  return artifacts;
}
