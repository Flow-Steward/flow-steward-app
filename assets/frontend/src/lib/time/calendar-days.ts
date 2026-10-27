// Whole calendar days as YYYY-MM-DD strings, per the canon date contract.
// No time and no offset, so the arithmetic runs in UTC and never shifts a day.

const DAY_MS = 86_400_000;

// en-CA is chosen because its format is already YYYY-MM-DD, not for its locale.
export function todayIn(timeZone: string, now = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone }).format(now);
}

function utcTime(isoDay: string): number {
  const [year, month, day] = isoDay.split("-").map(Number);
  return Date.UTC(year, month - 1, day);
}

export function addDays(isoDay: string, days: number): string {
  return new Date(utcTime(isoDay) + days * DAY_MS).toISOString().slice(0, 10);
}

export function wholeDaysBetween(fromIsoDay: string, toIsoDay: string): number {
  return Math.round((utcTime(toIsoDay) - utcTime(fromIsoDay)) / DAY_MS);
}

// The reader sees their own locale's format; the stored value never surfaces.
export function formatCalendarDay(isoDay: string): string {
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(utcTime(isoDay));
}
