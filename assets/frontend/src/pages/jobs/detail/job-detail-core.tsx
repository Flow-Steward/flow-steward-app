import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable react-refresh/only-export-components */

import { Copy } from "lucide-react";

import { formatUserDateTime } from "@/lib/time/user-timezone";
import type { JobFailureSummary } from "@/pages/jobs/detail/api";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

export type Entity = Record<string, unknown>;

export type JobViewState = {
  selectedStepIndex: number;
  selectedStepKey?: string;
  stepSheetOpen: boolean;
};

export type ApprovalGateLiveState = {
  revision: number;
  kind: "unknown" | "active" | "none";
  frame: Entity | null;
  closedApprovalId?: string;
};

export type JobStreamViewState = {
  approvalGate: ApprovalGateLiveState;
  done: boolean;
  /** Files steps finished during the run; kept apart from the bounded event stream. */
  liveFiles: Entity[];
  liveStepRows: Entity[];
  streamEvents: Array<Record<string, unknown>>;
};

export const DEFAULT_JOB_VIEW_STATE: JobViewState = {
  selectedStepIndex: 0,
  stepSheetOpen: false,
};

export const DEFAULT_JOB_STREAM_VIEW_STATE: JobStreamViewState = {
  approvalGate: { revision: 0, kind: "unknown", frame: null },
  done: false,
  liveFiles: [],
  liveStepRows: [],
  streamEvents: [],
};

export function objectOrEmpty(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

export function structuredObjectFromText(value: unknown): Entity {
  if (typeof value !== "string") {
    return {};
  }
  let text = value.trim();
  if (!text) {
    return {};
  }
  if (text.startsWith("```")) {
    text = text.slice(3);
    if (text.slice(0, 4).toLowerCase() === "json") {
      text = text.slice(4);
    }
    const withoutClosingFence = text.trimEnd();
    text = (withoutClosingFence.endsWith("```")
      ? withoutClosingFence.slice(0, -3)
      : withoutClosingFence).trim();
  }
  if (!text.startsWith("{") && !text.startsWith("[")) {
    return {};
  }
  try {
    const parsed = JSON.parse(text) as unknown;
    return objectOrEmpty(parsed);
  } catch {
    return {};
  }
}

export function listFromUnknown(value: unknown): Entity[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is Entity => Boolean(item) && typeof item === "object" && !Array.isArray(item));
}

export function normalizeValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "string" || typeof value === "number") {
    return stringFromUnknownValue(value);
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (Array.isArray(value)) {
    const readableItems = value
      .map((item) => readableText(item))
      .filter(isTruthy)
      .slice(0, 4);
    return readableItems.length ? readableItems.join(", ") : "Recorded details have no text summary.";
  }
  return readableText(value) || "Recorded details have no text summary.";
}

export function readableText(value: unknown, limit = 260): string {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    const structured = structuredObjectFromText(value);
    if (Object.keys(structured).length) {
      return readableText(structured, limit);
    }
    const text = stringFromUnknownValue(value).trim();
    return text.length > limit ? `${text.slice(0, limit - 1).trim()}…` : text;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => readableText(item, limit))
      .filter(isTruthy)
      .slice(0, 4)
      .join(", ");
  }
  if (typeof value === "object") {
    const record = value as Entity;
    for (const key of ["summary", "message", "result", "detail", "title", "reason", "error", "failure_reason", "path", "url", "href"]) {
      const text = readableText(record[key], limit);
      if (text) {
        return text;
      }
    }
  }
  return "";
}

export function firstReadableText(...values: unknown[]): string {
  for (const value of values) {
    const text = readableText(value);
    if (text) {
      return text;
    }
  }
  return "";
}

export function jsonStringFieldFromText(value: unknown, fieldName: string): string {
  if (typeof value !== "string") {
    return "";
  }
  const pattern = new RegExp(String.raw`"${escapeRegExp(fieldName)}"\s*:\s*"((?:\\.|[^"\\])*)"`);
  const match = pattern.exec(value);
  if (!match) {
    return "";
  }
  try {
    return firstStringFromUnknownValue([JSON.parse(`"${match[1]}"`), ""]).trim();
  } catch {
    return match[1].replaceAll(String.raw`\"`, '"').trim();
  }
}

export function structuredPayloadSummary(value: unknown): string {
  const structured = structuredObjectFromText(value);
  return (
    firstReadableText(structured.summary, structured.message, structured.result) ||
    jsonStringFieldFromText(value, "summary")
  );
}

export function workflowPath(workflowId: string) {
  return `/registry/workflows/${encodeURIComponent(workflowId)}`;
}

export function approvalQueryKey(jobId: string) {
  return ["jobs", "detail", jobId, "approval"] as const;
}

export function dispatchApprovalsRefresh() {
  if (globalThis.window === undefined) {
    return;
  }
  globalThis.dispatchEvent(new CustomEvent("platform:approvals-refresh"));
}

export function escapeRegExp(value: string): string {
  return value.replaceAll(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`);
}

export function safeLinkHref(value: unknown): string {
  const href = readableText(value, 500);
  if (href.startsWith("/") || href.startsWith("http://") || href.startsWith("https://")) {
    return href;
  }
  return "";
}

export function copyToClipboard(value: string) {
  if (!value || globalThis.navigator === undefined || !navigator.clipboard) {
    return;
  }
  ignoreAsyncResult(navigator.clipboard.writeText(value).catch(() => undefined));
}

export function CopyValueButton({ label, value }: Readonly<{ label: string; value: string }>) {
  const { t } = useI18n();
  if (!value) {
    return null;
  }
  return (
    <button
      aria-label={t("react.jobs.copy_value1", "Copy {value1}", { value1: String(label) })}
      className="fs-icon-button"
      onClick={() => copyToClipboard(value)}
      type="button"
    >
      <Copy aria-hidden="true" className="h-3.5 w-3.5" />
    </button>
  );
}

export function safeEntityId(value: unknown): string | null {
  const normalized = firstStringFromUnknownValue([value, ""]).trim();
  if (!normalized) {
    return null;
  }
  return normalized;
}

export function normalizeTimestamp(value: unknown, timezone?: string): string {
  return formatUserDateTime(value, timezone);
}

export function humanizeDiagnosticLabel(value: string): string {
  const special: Record<string, string> = {
    id: "ID",
    http: "HTTP",
    url: "URL",
  };
  return firstStringFromUnknownValue([value, ""])
    .replaceAll(/[_-]+/g, " ")
    .split(" ")
    .filter(isTruthy)
    .map((part) => special[part.toLowerCase()] || part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function failureSummaryFromDetail(value: unknown): JobFailureSummary {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  return value as JobFailureSummary;
}


export function normalizedStepSemanticPart(...values: unknown[]): string {
  return firstReadableText(...values)
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, " ")
    .trim();
}
