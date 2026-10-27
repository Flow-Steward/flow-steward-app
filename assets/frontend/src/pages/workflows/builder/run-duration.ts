import type { I18nContextValue } from "@/lib/i18n/context";
import { formatElapsed } from "@/pages/jobs/detail/job-detail-execution";

/**
 * How long a step or a run took, as the run marks and the run bar say it:
 * "< 0.1 s", "0.4 s", "2.4 s", "14 s", then minutes and hours.
 */
export function formatRunDuration(milliseconds: number | null | undefined, t: I18nContextValue["t"]): string {
  if (typeof milliseconds !== "number" || !Number.isFinite(milliseconds) || milliseconds < 0) return "";
  if (milliseconds < 100) return t("react.workflows.run_duration_under_tenth", "< 0.1 s");
  if (milliseconds < 10_000) {
    return t("react.workflows.run_duration_seconds", "{value} s", { value: (Math.round(milliseconds / 100) / 10).toFixed(1) });
  }
  if (milliseconds < 60_000) return t("react.workflows.run_duration_seconds", "{value} s", { value: String(Math.round(milliseconds / 1000)) });
  return formatElapsed(milliseconds, t);
}
