/**
 * Every date and time the interface shows, in the reader's own format: the date pattern and
 * 12/24-hour clock they chose in their profile, in their timezone, with month names in the
 * interface language. The signed-in session sets the preferences once; every formatter reads them.
 */

export const DATE_FORMATS = ["D MMM YYYY", "MMM D, YYYY", "DD.MM.YYYY", "DD/MM/YYYY", "MM/DD/YYYY", "YYYY-MM-DD"] as const;
export const TIME_FORMATS = ["24h", "12h"] as const;
export type DateFormat = (typeof DATE_FORMATS)[number];
export type TimeFormat = (typeof TIME_FORMATS)[number];

export const DEFAULT_DATE_FORMAT: DateFormat = "D MMM YYYY";
export const DEFAULT_TIME_FORMAT: TimeFormat = "24h";

export type DateTimePreferences = {
  dateFormat: DateFormat;
  timeFormat: TimeFormat;
  /** IANA zone; empty means the browser's own. */
  timezone: string;
  /** Interface language, for month names and the AM/PM marks. */
  locale: string;
};

export type DateTimeOptions = Partial<DateTimePreferences> & {
  /** Show seconds in the time. */
  seconds?: boolean;
};

export function isDateFormat(value: unknown): value is DateFormat {
  return typeof value === "string" && (DATE_FORMATS as readonly string[]).includes(value);
}

export function isTimeFormat(value: unknown): value is TimeFormat {
  return typeof value === "string" && (TIME_FORMATS as readonly string[]).includes(value);
}

export function browserTimezone(): string {
  if (globalThis.Intl === undefined) return "UTC";
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

const defaults = (): DateTimePreferences => ({ dateFormat: DEFAULT_DATE_FORMAT, timeFormat: DEFAULT_TIME_FORMAT, timezone: "", locale: "en" });

let current: DateTimePreferences = defaults();

/** The signed-in session's choice. An unknown or empty value falls back to the default. */
export function setDateTimePreferences(next: { dateFormat?: unknown; timeFormat?: unknown; timezone?: unknown; locale?: unknown }): void {
  current = {
    dateFormat: isDateFormat(next.dateFormat) ? next.dateFormat : DEFAULT_DATE_FORMAT,
    timeFormat: isTimeFormat(next.timeFormat) ? next.timeFormat : DEFAULT_TIME_FORMAT,
    timezone: typeof next.timezone === "string" ? next.timezone.trim() : "",
    locale: typeof next.locale === "string" && next.locale.trim() ? next.locale.trim() : "en",
  };
}

export function dateTimePreferences(): DateTimePreferences {
  return current;
}

/** Back to the defaults (sign-out, tests). */
export function resetDateTimePreferences(): void {
  current = defaults();
}

/** A date from an ISO string, epoch milliseconds or a Date; null when it is not one. */
export function toDate(value: unknown): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const date = value instanceof Date ? new Date(value.getTime()) : new Date(typeof value === "number" ? value : String(value));
  return Number.isNaN(date.getTime()) ? null : date;
}

type Parts = { year: string; month: string; day: string; hour: number; minute: string; second: string };

function validZone(zone: string): string | undefined {
  if (!zone) return undefined;
  try {
    new Intl.DateTimeFormat("en", { timeZone: zone });
    return zone;
  } catch {
    return undefined;
  }
}

function resolve(options: DateTimeOptions | undefined): DateTimePreferences & { seconds: boolean } {
  const base = current;
  return {
    dateFormat: isDateFormat(options?.dateFormat) ? options.dateFormat : base.dateFormat,
    timeFormat: isTimeFormat(options?.timeFormat) ? options.timeFormat : base.timeFormat,
    timezone: (options?.timezone ?? base.timezone) || browserTimezone(),
    locale: options?.locale || base.locale || "en",
    seconds: Boolean(options?.seconds),
  };
}

function parts(date: Date, zone: string | undefined): Parts {
  const found: Record<string, string> = {};
  for (const part of new Intl.DateTimeFormat("en-US", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZone: zone,
  }).formatToParts(date)) {
    found[part.type] = part.value;
  }
  return { year: found.year, month: found.month, day: found.day, hour: Number(found.hour) % 24, minute: found.minute, second: found.second };
}

function shortMonth(date: Date, locale: string, zone: string | undefined): string {
  try {
    return new Intl.DateTimeFormat(locale, { month: "short", timeZone: zone }).format(date);
  } catch {
    return new Intl.DateTimeFormat("en", { month: "short", timeZone: zone }).format(date);
  }
}

function dayPeriod(date: Date, locale: string, zone: string | undefined): string {
  const pick = (tag: string) => new Intl.DateTimeFormat(tag, { hour: "numeric", hour12: true, timeZone: zone }).formatToParts(date).find((part) => part.type === "dayPeriod")?.value;
  try {
    return pick(locale) || pick("en-US") || "";
  } catch {
    return pick("en-US") || "";
  }
}

function renderDate(date: Date, prefs: DateTimePreferences, zone: string | undefined): string {
  const { year, month, day } = parts(date, zone);
  const dayNumber = String(Number(day));
  switch (prefs.dateFormat) {
    case "MMM D, YYYY": return `${shortMonth(date, prefs.locale, zone)} ${dayNumber}, ${year}`;
    case "DD.MM.YYYY": return `${day}.${month}.${year}`;
    case "DD/MM/YYYY": return `${day}/${month}/${year}`;
    case "MM/DD/YYYY": return `${month}/${day}/${year}`;
    case "YYYY-MM-DD": return `${year}-${month}-${day}`;
    default: return `${dayNumber} ${shortMonth(date, prefs.locale, zone)} ${year}`;
  }
}

function renderTime(date: Date, prefs: DateTimePreferences & { seconds: boolean }, zone: string | undefined): string {
  const { hour, minute, second } = parts(date, zone);
  const tail = prefs.seconds ? `:${minute}:${second}` : `:${minute}`;
  if (prefs.timeFormat === "12h") {
    return `${hour % 12 || 12}${tail} ${dayPeriod(date, prefs.locale, zone)}`.trim();
  }
  return `${String(hour).padStart(2, "0")}${tail}`;
}

/** "9 Nov 2026" in the reader's pattern; the raw text when it is not a date, "—" when empty. */
export function formatDate(value: unknown, options?: DateTimeOptions): string {
  const date = toDate(value);
  if (!date) return value === null || value === undefined || value === "" ? "—" : String(value);
  const prefs = resolve(options);
  return renderDate(date, prefs, validZone(prefs.timezone));
}

/** "16:13" or "4:13 PM" in the reader's clock. */
export function formatTime(value: unknown, options?: DateTimeOptions): string {
  const date = toDate(value);
  if (!date) return value === null || value === undefined || value === "" ? "—" : String(value);
  const prefs = resolve(options);
  return renderTime(date, prefs, validZone(prefs.timezone));
}

/** "9 Nov 2026, 16:13": the date and the time, both in the reader's format. */
export function formatDateTime(value: unknown, options?: DateTimeOptions): string {
  const date = toDate(value);
  if (!date) return value === null || value === undefined || value === "" ? "—" : String(value);
  const prefs = resolve(options);
  const zone = validZone(prefs.timezone);
  return `${renderDate(date, prefs, zone)}, ${renderTime(date, prefs, zone)}`;
}

/** A wall-clock time with no zone ("09:05" of a schedule's own day), in the reader's clock. */
export function formatWallClock(hours: number, minutes: number, options?: DateTimeOptions): string {
  const prefs = resolve(options);
  const mm = String(minutes).padStart(2, "0");
  if (prefs.timeFormat !== "12h") return `${String(hours).padStart(2, "0")}:${mm}`;
  const period = dayPeriod(new Date(Date.UTC(2026, 0, 1, hours, minutes)), prefs.locale, "UTC");
  return `${hours % 12 || 12}:${mm} ${period}`.trim();
}

/** A calendar date with no time of day ("2026-11-09" from a date picker), never shifted by a timezone. */
export function formatCalendarDate(value: string, options?: DateTimeOptions): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || "").trim());
  if (!match) return formatDate(value, options);
  return formatDate(`${match[1]}-${match[2]}-${match[3]}T12:00:00Z`, { ...options, timezone: "UTC" });
}
