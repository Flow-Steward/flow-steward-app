import { expandPeriod, periodLabel } from "@/lib/time/period";

describe("period", () => {
  it("expands presets from the given today, both ends included", () => {
    expect(expandPeriod({ kind: "all" }, "2026-09-27")).toEqual({ fromDate: "", toDate: "" });
    expect(expandPeriod({ kind: "preset", preset: "today" }, "2026-09-27")).toEqual({ fromDate: "2026-09-27", toDate: "2026-09-27" });
    expect(expandPeriod({ kind: "preset", preset: "last7" }, "2026-09-27")).toEqual({ fromDate: "2026-09-21", toDate: "2026-09-27" });
    expect(expandPeriod({ kind: "preset", preset: "last30" }, "2026-03-01")).toEqual({ fromDate: "2026-01-31", toDate: "2026-03-01" });
  });

  it("keeps a preset relative: tomorrow it is a different window", () => {
    const preset = { kind: "preset", preset: "last7" } as const;
    expect(expandPeriod(preset, "2026-09-28").fromDate).toBe("2026-09-22");
  });

  it("passes a custom range through unchanged", () => {
    expect(expandPeriod({ kind: "custom", from: "2026-06-03", to: "2026-06-17" }, "2026-09-27")).toEqual({
      fromDate: "2026-06-03",
      toDate: "2026-06-17",
    });
  });

  it("names the applied value in words", () => {
    expect(periodLabel({ kind: "all" }, "All events")).toBe("All events");
    expect(periodLabel({ kind: "preset", preset: "last30" }, "All events")).toBe("Last 30 days");
    expect(periodLabel({ kind: "custom", from: "2026-06-03", to: "2026-06-17" }, "All events")).toMatch(/3.*–.*17/);
  });

  it("uses the UI locale and translated preset labels without changing the query range", () => {
    const presets = { today: "Šiandien", last7: "Paskutinės 7 dienos", last30: "Paskutinės 30 dienų" };
    expect(periodLabel({ kind: "preset", preset: "last30" }, "Visi įvykiai", "lt", presets)).toBe("Paskutinės 30 dienų");
    const custom = { kind: "custom", from: "2026-06-03", to: "2026-06-17" } as const;
    const formatter = new Intl.DateTimeFormat("lt", { day: "numeric", month: "short", timeZone: "UTC" });
    expect(periodLabel(custom, "Visi įvykiai", "lt", presets)).toBe(`${formatter.format(Date.UTC(2026, 5, 3))} – ${formatter.format(Date.UTC(2026, 5, 17))}`);
    expect(expandPeriod(custom, "2026-09-27")).toEqual({ fromDate: "2026-06-03", toDate: "2026-06-17" });
  });
});
