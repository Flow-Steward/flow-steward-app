import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/**
 * Presentation of one result row.
 *
 * The canonical rules this encodes: one metadata line that never restates the entity type
 * (the row icon and the group heading already carry it), a status badge only for
 * actionable operational state, and a query match highlighted inside the title.
 */

import type { GlobalSearchItem } from "@/components/layout/app-shell-helpers";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export type StatusTone = "neutral" | "success" | "danger";

export type ResultStatus = {
  label: string;
  tone: StatusTone;
};

/** Only operational state an operator can act on earns a badge. */
const BADGED_TONES: Record<string, StatusTone> = {
  red: "danger",
  green: "success",
  amber: "neutral",
  blue: "neutral",
};

function itemMeta(item: GlobalSearchItem): Record<string, unknown> {
  const meta = item.meta;
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) {
    return {};
  }
  return meta as Record<string, unknown>;
}

export function resultStatus(item: GlobalSearchItem): ResultStatus | null {
  const meta = itemMeta(item);
  const label = firstStringFromUnknownValue([meta.status_label, meta.status, ""]).trim();
  if (!label) {
    return null;
  }
  const tone = firstStringFromUnknownValue([meta.status_tone, ""])
    .trim()
    .toLowerCase();
  // A tone the server did not publish stays neutral rather than guessing a colour.
  return { label, tone: BADGED_TONES[tone] ?? "neutral" };
}

/** Compact relative age. Absolute timestamps are too wide for a single-line row. */
export function relativeAge(timestamp: unknown, now: number, t: I18nContextValue["t"] = fallbackContext.t): string {
  const text = firstStringFromUnknownValue([timestamp, ""]).trim();
  if (!text) {
    return "";
  }
  const parsed = Date.parse(text);
  if (Number.isNaN(parsed)) {
    return "";
  }
  const minutes = Math.floor((now - parsed) / 60000);
  if (minutes < 1) {
    return t("react.shared.relative_now", "now");
  }
  if (minutes < 60) {
    return t("react.shared.relative_minutes", "{count}m", { count: minutes });
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return t("react.shared.relative_hours", "{count}h", { count: hours });
  }
  const days = Math.floor(hours / 24);
  return t("react.shared.relative_days", "{count}d", { count: days });
}

/**
 * The metadata segments for one row, in canonical order: the context it belongs to, then a
 * stable identifier, then its age. Empty segments are dropped rather than padded.
 */
export function resultMetaSegments(item: GlobalSearchItem, now: number, t: I18nContextValue["t"] = fallbackContext.t): string[] {
  const meta = itemMeta(item);
  const segments: string[] = [];
  const context = firstStringFromUnknownValue([item.project_name, meta.selected_workflow, ""]).trim();
  if (context) {
    segments.push(context);
  }
  const jobId = firstStringFromUnknownValue([item.job_id, ""]).trim();
  if (jobId) {
    segments.push(`#${jobId}`);
  }
  const age = relativeAge(item.timestamp, now, t);
  if (age) {
    segments.push(age);
  }
  return segments;
}

export function resultTitle(item: GlobalSearchItem, t: I18nContextValue["t"] = fallbackContext.t): string {
  return firstStringFromUnknownValue([item.title, item.id, t("react.shared.search_result", "Search result")]).trim();
}

export type TitleSegment = {
  text: string;
  match: boolean;
};

/**
 * Split a title around every case-insensitive occurrence of the query so the matched run
 * can be marked. Returns one unmatched segment when there is nothing to highlight.
 */
export function highlightSegments(title: string, query: string): TitleSegment[] {
  const needle = query.trim();
  if (!needle || !title) {
    return [{ text: title, match: false }];
  }
  const haystack = title.toLocaleLowerCase();
  const lowered = needle.toLocaleLowerCase();
  const segments: TitleSegment[] = [];
  let cursor = 0;
  let found = haystack.indexOf(lowered, cursor);
  while (found !== -1) {
    if (found > cursor) {
      segments.push({ text: title.slice(cursor, found), match: false });
    }
    segments.push({ text: title.slice(found, found + needle.length), match: true });
    cursor = found + needle.length;
    found = haystack.indexOf(lowered, cursor);
  }
  if (cursor < title.length) {
    segments.push({ text: title.slice(cursor), match: false });
  }
  return segments.length ? segments : [{ text: title, match: false }];
}
