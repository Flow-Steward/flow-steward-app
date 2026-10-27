import { formatSpend, spendRows } from "@/pages/dashboard/cost-display";

describe("dashboard cost display", () => {
  it("preserves unavailable and partial cost states instead of coercing them to zero", () => {
    const rows = spendRows([
      { name: "Unknown project", spend_usd: null, availability: "unavailable" },
      { name: "Partial project", spend_usd: 1.25, availability: "partial" },
    ]);

    expect(rows[0]).toEqual({
      name: "Unknown project",
      spend_usd: null,
      availability: "unavailable",
    });
    expect(formatSpend(rows[0])).toBe("Unavailable");
    expect(formatSpend(rows[1])).toContain("1.25");
    expect(formatSpend(rows[1])).toContain("partial");
  });
});
