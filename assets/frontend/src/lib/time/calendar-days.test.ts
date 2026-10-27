import { addDays, todayIn, wholeDaysBetween } from "@/lib/time/calendar-days";

describe("calendar days", () => {
  it("reads today in the user's zone, not the browser's", () => {
    // 23:30 UTC on 27 Sep is already 28 Sep in Tokyo and still 27 Sep in New York.
    const now = new Date(Date.UTC(2026, 8, 27, 23, 30));
    expect(todayIn("Asia/Tokyo", now)).toBe("2026-09-28");
    expect(todayIn("America/New_York", now)).toBe("2026-09-27");
  });

  it("counts whole days across a month and a daylight-saving change", () => {
    expect(addDays("2026-09-27", 1)).toBe("2026-09-28");
    expect(addDays("2026-10-31", 1)).toBe("2026-11-01");
    expect(wholeDaysBetween("2026-10-20", "2026-10-30")).toBe(10);
    expect(wholeDaysBetween("2026-09-27", "2026-09-27")).toBe(0);
    expect(wholeDaysBetween("2026-09-27", "2026-09-26")).toBe(-1);
  });
});
