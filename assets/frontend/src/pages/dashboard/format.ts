import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/** Shared dashboard formatters. Kept out of the component files so fast refresh
 *  stays intact and the metric captions and queue rows cannot drift apart. */

/** Coarse age: the caption answers "should I step in", not "how long exactly". */
export function formatAge(ms: number | null, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (ms === null || !Number.isFinite(ms)) {
    return "";
  }
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) {
    return t("react.dashboard.under_a_minute", "under a minute");
  }
  if (minutes < 60) {
    return `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours}h`;
  }
  return `${Math.floor(hours / 24)}d`;
}

/** "4h ago" for a queue row; under a minute reads "just now". */
export function formatAgeAgo(ms: number | null, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (ms === null || !Number.isFinite(ms)) {
    return "";
  }
  if (ms < 60_000) {
    return t("dashboard.time.just_now", "just now");
  }
  return t("react.dashboard.age_ago", "{age} ago", { age: formatAge(ms, t) });
}
