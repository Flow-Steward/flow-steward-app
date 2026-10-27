/**
 * Schedule timing on the client: what a five-field cron expression means in
 * plain words, and when it runs next, read on the schedule's own timezone.
 * The server stays the authority on save; this only lets the form show the
 * next runs while the author changes the timing.
 */

export type ScheduleRepeat = "daily" | "weekly" | "monthly" | "custom";

export type FriendlySchedule = { mode: ScheduleRepeat; time: string; weekday: string; monthDay: string };

const MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
const DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

type FieldSpec = { min: number; max: number; names?: string[]; nameBase?: number };
const FIELD_SPECS: FieldSpec[] = [
  { min: 0, max: 59 },
  { min: 0, max: 23 },
  { min: 1, max: 31 },
  { min: 1, max: 12, names: MONTH_NAMES, nameBase: 1 },
  { min: 0, max: 7, names: DAY_NAMES, nameBase: 0 },
];

const pad = (value: number) => String(value).padStart(2, "0");

function cronParts(expression: string): string[] {
  return expression.trim().split(/\s+/).filter(Boolean);
}

/** Five fields: minute, hour, day of month, month, day of week. */
export function cronHasFiveFields(expression: string): boolean {
  return cronParts(expression).length === 5;
}

function fieldValue(token: string, spec: FieldSpec): number | null {
  if (/^\d+$/.test(token)) return Number(token);
  const index = spec.names?.indexOf(token.toLowerCase()) ?? -1;
  return index >= 0 ? index + (spec.nameBase ?? 0) : null;
}

/** The values one field allows, or null when the syntax is beyond plain cron. */
function parseField(source: string, spec: FieldSpec): Set<number> | null {
  const values = new Set<number>();
  for (const part of source.split(",")) {
    const [range, stepText] = part.split("/");
    if (part.split("/").length > 2) return null;
    const step = stepText === undefined ? 1 : Number(stepText);
    if (!Number.isInteger(step) || step < 1) return null;
    let low = spec.min;
    let high = spec.max;
    if (range !== "*") {
      const bounds = range.split("-");
      if (bounds.length > 2) return null;
      const first = fieldValue(bounds[0], spec);
      const last = bounds.length === 2 ? fieldValue(bounds[1], spec) : stepText === undefined ? first : spec.max;
      if (first === null || last === null || first < spec.min || last > spec.max || first > last) return null;
      low = first;
      high = last;
    }
    for (let value = low; value <= high; value += step) values.add(value);
  }
  return values;
}

/** The plain shapes the Repeat choice writes; anything else stays a custom cron. */
export function friendlyScheduleFromCron(expression: string): FriendlySchedule | null {
  const parts = cronParts(expression);
  if (parts.length !== 5 || !/^\d+$/.test(parts[0]) || !/^\d+$/.test(parts[1])) return null;
  const [minute, hour, dom, month, dow] = parts;
  if (Number(minute) > 59 || Number(hour) > 23 || month !== "*") return null;
  const time = `${pad(Number(hour))}:${pad(Number(minute))}`;
  if (dom === "*" && dow === "*") return { mode: "daily", time, weekday: "1", monthDay: "1" };
  if (dom === "*" && /^[0-7]$/.test(dow)) return { mode: "weekly", time, weekday: String(Number(dow) % 7), monthDay: "1" };
  if (dow === "*" && /^\d+$/.test(dom) && Number(dom) >= 1 && Number(dom) <= 31) return { mode: "monthly", time, weekday: "1", monthDay: String(Number(dom)) };
  return null;
}

/** The current wall clock in the timezone, as a plain local Date holding those fields. */
export function zoneWallClock(now: Date, timezone: string): Date {
  try {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
      timeZone: timezone, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric",
    }).formatToParts(now).map((part) => [part.type, part.value]));
    return new Date(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute));
  } catch {
    return new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), now.getMinutes());
  }
}

/**
 * The next runs as wall-clock times in the schedule's timezone. Null when the
 * expression uses syntax this preview does not read (the server still checks it).
 */
export function nextCronRuns(expression: string, timezone: string, count: number, now = new Date()): Date[] | null {
  const parts = cronParts(expression);
  if (parts.length !== 5) return null;
  const fields = parts.map((part, index) => parseField(part, FIELD_SPECS[index]));
  if (fields.includes(null)) return null;
  const [minutes, hours, days, months, weekdaysRaw] = fields as Set<number>[];
  const weekdays = new Set(Array.from(weekdaysRaw, (day) => day % 7));
  const domRestricted = parts[2] !== "*";
  const dowRestricted = parts[4] !== "*";
  const sortedHours = Array.from(hours).sort((left, right) => left - right);
  const sortedMinutes = Array.from(minutes).sort((left, right) => left - right);
  const start = zoneWallClock(now, timezone);
  const runs: Date[] = [];
  for (let offset = 0; offset < 1500 && runs.length < count; offset += 1) {
    const day = new Date(start.getFullYear(), start.getMonth(), start.getDate() + offset);
    if (!months.has(day.getMonth() + 1)) continue;
    const domMatch = days.has(day.getDate());
    const dowMatch = weekdays.has(day.getDay());
    // Cron reads day of month and day of week as either-or when both are set.
    const dayMatches = domRestricted && dowRestricted ? domMatch || dowMatch : domMatch && dowMatch;
    if (!dayMatches) continue;
    for (const hour of sortedHours) {
      for (const minute of sortedMinutes) {
        const run = new Date(day.getFullYear(), day.getMonth(), day.getDate(), hour, minute);
        if (run > start) runs.push(run);
        if (runs.length >= count) return runs;
      }
    }
  }
  return runs;
}

/** "Mon, 5 Oct · 09:00": the wall-clock run, day first, 24-hour time. */
export function formatScheduleRun(run: Date, locale?: string): string {
  const day = run.toLocaleDateString(locale, { weekday: "short", day: "numeric", month: "short" });
  return `${day} · ${pad(run.getHours())}:${pad(run.getMinutes())}`;
}
