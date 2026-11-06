import { describe, expect, it } from "vitest";

import { formatReportValue, type ReportFormatContext } from "./report-format";
import { defaultReportLabels } from "./report-labels";

const ctx: ReportFormatContext = {
  locale: "en-US",
  timeZone: "Europe/Berlin",
  labels: defaultReportLabels,
};

describe("formatReportValue", () => {
  it("formats currency, percent and plain numbers in the report locale", () => {
    expect(formatReportValue(48210.5, { type: "currency", currency: "EUR", maximum_fraction_digits: 2 }, ctx)).toBe(
      "€48,210.50",
    );
    expect(formatReportValue(0.123, { type: "percent", maximum_fraction_digits: 1 }, ctx)).toBe("12.3%");
    expect(formatReportValue(21500, { type: "number" }, ctx)).toBe("21,500");
  });

  it("keeps zero and false as values and shows null as a dash", () => {
    expect(formatReportValue(0, { type: "number" }, ctx)).toBe("0");
    expect(formatReportValue(false, { type: "boolean" }, ctx)).toBe("No");
    expect(formatReportValue(true, { type: "boolean" }, ctx)).toBe("Yes");
    expect(formatReportValue(null, { type: "currency", currency: "EUR" }, ctx)).toBe("—");
  });

  it("uses the locale of the report, not of the browser", () => {
    const german = { ...ctx, locale: "de-DE" };
    expect(formatReportValue(21500.5, { type: "number", maximum_fraction_digits: 1 }, german)).toBe("21.500,5");
  });

  it("never shifts a calendar date by a time zone", () => {
    const farEast = { ...ctx, timeZone: "Pacific/Kiritimati" };
    expect(formatReportValue("2026-11-02", { type: "date" }, farEast)).toBe("Nov 2, 2026");
    expect(formatReportValue("2026-11-02", { type: "date" }, { ...ctx, timeZone: "Pacific/Pago_Pago" })).toBe(
      "Nov 2, 2026",
    );
  });

  it.each([
    ["0001-01-01", "Jan 1, 1"],
    ["0004-02-29", "Feb 29, 4"],
    ["0099-12-31", "Dec 31, 99"],
    ["0100-03-01", "Mar 1, 100"],
    ["1582-10-10", "Oct 10, 1582"],
    ["2024-02-29", "Feb 29, 2024"],
    ["2026-11-02", "Nov 2, 2026"],
  ])("keeps the year and day of %s in every zone", (value, expected) => {
    for (const timeZone of ["UTC", "Pacific/Kiritimati", "Pacific/Pago_Pago"]) {
      expect(formatReportValue(value, { type: "date" }, { ...ctx, timeZone })).toBe(expected);
    }
  });

  it("presents the date in the locale's own calendar without rewriting its year", () => {
    // Buddhist calendar: 2026 is 2569 BE; Persian calendar: November 2026 is in 1405 AP.
    expect(formatReportValue("2026-11-02", { type: "date" }, { ...ctx, locale: "th-TH" })).toContain("2569");
    expect(formatReportValue("2026-11-02", { type: "date" }, { ...ctx, locale: "fa-IR" })).toContain("۱۴۰۵");
    expect(formatReportValue("0001-01-01", { type: "date" }, { ...ctx, locale: "th-TH" })).toContain("544");
  });

  it("does not invent a date that does not exist", () => {
    expect(formatReportValue("2026-02-30", { type: "date" }, ctx)).toBe("2026-02-30");
  });

  it("shows a date-time in the report time zone", () => {
    const text = formatReportValue("2026-11-08T18:30:00Z", { type: "datetime" }, ctx);
    expect(text).toContain("Nov 8, 2026");
    expect(text).toContain("7:30");
  });

  it("accepts a maximum below a currency's usual minimum digits", () => {
    expect(formatReportValue(1234.56, { type: "currency", currency: "EUR", maximum_fraction_digits: 0 }, ctx)).toBe(
      "€1,235",
    );
  });

  it("falls back instead of throwing on an unknown locale", () => {
    const text = formatReportValue(1, { type: "number" }, { ...ctx, locale: "zz-INVALID-tag" });
    expect(text).toBe("1");
  });
});
