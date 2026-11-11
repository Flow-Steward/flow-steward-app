import { afterEach, describe, expect, it } from "vitest";

import {
  DATE_FORMATS,
  dateTimePreferences,
  formatCalendarDate,
  formatDate,
  formatDateTime,
  formatTime,
  formatWallClock,
  resetDateTimePreferences,
  setDateTimePreferences,
} from "./date-time-format";

const AT = "2026-12-31T16:05:09Z";

afterEach(() => resetDateTimePreferences());

describe("date and time in the reader's own format", () => {
  it("draws every offered date pattern with the day past the 12th", () => {
    const drawn = DATE_FORMATS.map((dateFormat) => formatDate(AT, { dateFormat, timezone: "UTC", locale: "en" }));
    expect(drawn).toEqual(["31 Dec 2026", "Dec 31, 2026", "31.12.2026", "31/12/2026", "12/31/2026", "2026-12-31"]);
  });

  it("draws the 24-hour and the 12-hour clock, with seconds on request", () => {
    expect(formatTime(AT, { timezone: "UTC" })).toBe("16:05");
    expect(formatTime(AT, { timezone: "UTC", seconds: true })).toBe("16:05:09");
    expect(formatTime(AT, { timezone: "UTC", timeFormat: "12h", locale: "en" })).toBe("4:05 PM");
    expect(formatTime("2026-12-31T00:30:00Z", { timezone: "UTC", timeFormat: "12h", locale: "en" })).toBe("12:30 AM");
    expect(formatTime("2026-12-31T00:30:00Z", { timezone: "UTC" })).toBe("00:30");
  });

  it("reads the instant in the reader's zone, the day included", () => {
    expect(formatDateTime(AT, { timezone: "Europe/Vilnius", dateFormat: "YYYY-MM-DD" })).toBe("2026-12-31, 18:05");
    expect(formatDateTime("2026-12-31T23:30:00Z", { timezone: "Europe/Vilnius", dateFormat: "DD.MM.YYYY" })).toBe("01.01.2027, 01:30");
    expect(formatDateTime(AT, { timezone: "America/New_York", dateFormat: "MM/DD/YYYY", timeFormat: "12h", locale: "en" })).toBe("12/31/2026, 11:05 AM");
  });

  it("follows the session's choice once it is set, and falls back from an unknown one", () => {
    setDateTimePreferences({ dateFormat: "MMM D, YYYY", timeFormat: "12h", timezone: "UTC", locale: "en" });
    expect(formatDateTime(AT)).toBe("Dec 31, 2026, 4:05 PM");

    setDateTimePreferences({ dateFormat: "%d.%m", timeFormat: "am/pm", timezone: "UTC" });
    expect(dateTimePreferences()).toMatchObject({ dateFormat: "D MMM YYYY", timeFormat: "24h", locale: "en" });
    expect(formatDateTime(AT)).toBe("31 Dec 2026, 16:05");
  });

  it("names the month in the interface language", () => {
    expect(formatDate(AT, { timezone: "UTC", locale: "lt" })).toMatch(/^31 \S+ 2026$/);
    expect(formatDate(AT, { timezone: "UTC", locale: "lt" })).not.toContain("Dec");
  });

  it("keeps a calendar day from a date picker on its own day in any zone", () => {
    setDateTimePreferences({ dateFormat: "DD.MM.YYYY", timezone: "Pacific/Kiritimati" });
    expect(formatCalendarDate("2026-11-09")).toBe("09.11.2026");
  });

  it("draws a schedule's wall-clock time in the reader's clock", () => {
    expect(formatWallClock(9, 5)).toBe("09:05");
    expect(formatWallClock(21, 5, { timeFormat: "12h", locale: "en" })).toBe("9:05 PM");
  });

  it("shows a dash for nothing and the raw text for a value that is not a date", () => {
    expect(formatDateTime("")).toBe("—");
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime("soon")).toBe("soon");
    expect(formatDate(undefined)).toBe("—");
  });

  it("ignores a zone the browser does not know instead of failing", () => {
    expect(formatTime(AT, { timezone: "Mars/Olympus" })).toMatch(/^\d{2}:\d{2}$/);
  });
});
