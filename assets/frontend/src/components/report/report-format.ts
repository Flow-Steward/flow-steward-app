/**
 * Display formatting of report values with the snapshot's locale and time zone.
 *
 * Every viewer sees a stored report the same way: the locale and time zone come
 * from the snapshot, never from the browser. A `date` is a calendar date and is
 * never shifted by a time zone; a `datetime` is shown in the report time zone.
 */
import type { ReportLabels } from "./report-labels";
import type { ReportFormat, ReportScalar } from "./report-types";

export type ReportFormatContext = {
  locale: string;
  timeZone: string;
  labels: Pick<ReportLabels, "empty" | "yes" | "no">;
};

const FALLBACK_LOCALE = "en-US";
const FALLBACK_TIME_ZONE = "UTC";
const DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

const numberFormats = new Map<string, Intl.NumberFormat>();
const dateFormats = new Map<string, Intl.DateTimeFormat>();

function digits(format: ReportFormat): { min?: number; max?: number } {
  if (format.type !== "number" && format.type !== "currency" && format.type !== "percent") {
    return {};
  }
  return { min: format.minimum_fraction_digits, max: format.maximum_fraction_digits };
}

function numberOptions(format: ReportFormat): Intl.NumberFormatOptions {
  const options: Intl.NumberFormatOptions = {};
  if (format.type === "currency") {
    options.style = "currency";
    options.currency = format.currency;
  } else if (format.type === "percent") {
    options.style = "percent";
  }
  const { min, max } = digits(format);
  if (max !== undefined) {
    options.maximumFractionDigits = max;
    // Engines differ on a maximum below the style's default minimum (two for a
    // currency); an explicit minimum never above the maximum works everywhere.
    options.minimumFractionDigits = Math.min(min ?? 0, max);
  } else if (min !== undefined) {
    options.minimumFractionDigits = min;
  }
  return options;
}

function cachedNumberFormat(locale: string, format: ReportFormat): Intl.NumberFormat {
  const key = `${locale}|${JSON.stringify(format)}`;
  let formatter = numberFormats.get(key);
  if (!formatter) {
    try {
      formatter = new Intl.NumberFormat(locale, numberOptions(format));
    } catch {
      formatter = new Intl.NumberFormat(FALLBACK_LOCALE, numberOptions(format));
    }
    numberFormats.set(key, formatter);
  }
  return formatter;
}

function cachedDateFormat(locale: string, timeZone: string, withTime: boolean): Intl.DateTimeFormat {
  const key = `${locale}|${timeZone}|${withTime}`;
  let formatter = dateFormats.get(key);
  if (!formatter) {
    const options: Intl.DateTimeFormatOptions = withTime
      ? { dateStyle: "medium", timeStyle: "short", timeZone }
      : { dateStyle: "medium", timeZone };
    try {
      formatter = new Intl.DateTimeFormat(locale, options);
    } catch {
      formatter = new Intl.DateTimeFormat(FALLBACK_LOCALE, { ...options, timeZone: FALLBACK_TIME_ZONE });
    }
    dateFormats.set(key, formatter);
  }
  return formatter;
}

function formatNumber(value: ReportScalar, format: ReportFormat, locale: string): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return String(value);
  try {
    return cachedNumberFormat(locale, format).format(value);
  } catch {
    return String(value);
  }
}

/** Before this year ICU formats dates on the Julian calendar, not the proleptic Gregorian one. */
const GREGORIAN_START_YEAR = 1583;
const GREGORIAN_CYCLE_YEARS = 400;

function formatDate(value: ReportScalar, ctx: ReportFormatContext): string {
  const match = typeof value === "string" ? DATE_PATTERN.exec(value) : null;
  if (!match) return String(value);
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  // A calendar date, shown in UTC so no zone can move it a day. An early year is
  // drawn on a stand-in year with the same place in the 400-year Gregorian
  // cycle (so 29 February survives), then given back its own number:
  // Date.UTC reads 0001-0099 as 19xx, and ICU would switch to the Julian calendar.
  const drawnYear =
    year < GREGORIAN_START_YEAR ? 2000 + (year % GREGORIAN_CYCLE_YEARS) : year;
  const moment = new Date(0);
  moment.setUTCFullYear(drawnYear, month - 1, day);
  if (moment.getUTCMonth() !== month - 1 || moment.getUTCDate() !== day) return String(value);
  const formatter = cachedDateFormat(ctx.locale, FALLBACK_TIME_ZONE, false);
  if (drawnYear === year) return formatter.format(moment);
  const yearText = new Intl.NumberFormat(formatter.resolvedOptions().locale, {
    useGrouping: false,
  }).format(year);
  return formatter
    .formatToParts(moment)
    .map((part) => (part.type === "year" ? yearText : part.value))
    .join("");
}

function formatDateTime(value: ReportScalar, ctx: ReportFormatContext): string {
  if (typeof value !== "string") return String(value);
  const moment = new Date(value);
  if (Number.isNaN(moment.getTime())) return value;
  return cachedDateFormat(ctx.locale, ctx.timeZone, true).format(moment);
}

/** One value as text for a person, in the report locale; never HTML. */
export function formatReportValue(
  value: ReportScalar,
  format: ReportFormat,
  ctx: ReportFormatContext,
): string {
  if (value === null || value === undefined) return ctx.labels.empty;
  switch (format.type) {
    case "boolean":
      return value ? ctx.labels.yes : ctx.labels.no;
    case "number":
    case "currency":
    case "percent":
      return formatNumber(value, format, ctx.locale);
    case "date":
      return formatDate(value, ctx);
    case "datetime":
      return formatDateTime(value, ctx);
    default:
      return String(value);
  }
}
