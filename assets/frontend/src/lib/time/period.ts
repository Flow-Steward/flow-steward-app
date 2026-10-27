import { addDays } from "@/lib/time/calendar-days";

export type PeriodPreset = "today" | "last7" | "last30";

export type PeriodValue =
  | { kind: "all" }
  | { kind: "preset"; preset: PeriodPreset }
  | { kind: "custom"; from: string; to: string };

export const PERIOD_PRESETS: PeriodPreset[] = ["today", "last7", "last30"];
export const PERIOD_PRESET_LABELS: Record<PeriodPreset, string> = {
  today: "Today",
  last7: "Last 7 days",
  last30: "Last 30 days",
};
// Days before today that each window reaches back; both ends are included.
const PRESET_SPAN: Record<PeriodPreset, number> = { today: 0, last7: 6, last30: 29 };

/**
 * A preset stays a preset until the request is built: "Last 7 days" is
 * re-derived from today every time, so tomorrow it is a different seven days.
 * `today` must come from the user's configured time zone.
 */
export function expandPeriod(value: PeriodValue, today: string): { fromDate: string; toDate: string } {
  if (value.kind === "all") return { fromDate: "", toDate: "" };
  if (value.kind === "preset") return { fromDate: addDays(today, -PRESET_SPAN[value.preset]), toDate: today };
  return { fromDate: value.from, toDate: value.to };
}

// Order and month abbreviation follow the reader's locale; the stored value is fixed.
function shortDay(isoDay: string, locale?: string): string {
  const [year, month, day] = isoDay.split("-").map(Number);
  return new Intl.DateTimeFormat(locale, { day: "numeric", month: "short", timeZone: "UTC" }).format(
    Date.UTC(year, month - 1, day),
  );
}

export function periodLabel(value: PeriodValue, allLabel: string, locale?: string, presetLabels: Readonly<Record<PeriodPreset, string>> = PERIOD_PRESET_LABELS): string {
  if (value.kind === "all") return allLabel;
  if (value.kind === "preset") return presetLabels[value.preset];
  return `${shortDay(value.from, locale)} – ${shortDay(value.to, locale)}`;
}
