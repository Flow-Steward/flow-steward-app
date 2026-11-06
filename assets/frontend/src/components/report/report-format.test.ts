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
