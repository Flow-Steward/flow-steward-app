import {
  cronHasFiveFields, formatScheduleRun, friendlyScheduleFromCron, nextCronRuns, zoneWallClock,
} from "./detail-workflow-schedule-cron";

const NOW = new Date("2026-10-21T10:30:00Z"); // a Wednesday

function wall(runs: Date[] | null) {
  return (runs || []).map((run) => `${run.getFullYear()}-${run.getMonth() + 1}-${run.getDate()} ${run.getHours()}:${String(run.getMinutes()).padStart(2, "0")}`);
}

it("reopens the shapes Repeat writes and leaves the rest custom", () => {
  expect(friendlyScheduleFromCron("0 9 * * *")).toEqual({ mode: "daily", time: "09:00", weekday: "1", monthDay: "1" });
  expect(friendlyScheduleFromCron("30 7 * * 5")).toEqual({ mode: "weekly", time: "07:30", weekday: "5", monthDay: "1" });
  expect(friendlyScheduleFromCron("0 9 * * 7")).toMatchObject({ mode: "weekly", weekday: "0" });
  expect(friendlyScheduleFromCron("0 9 15 * *")).toEqual({ mode: "monthly", time: "09:00", weekday: "1", monthDay: "15" });
  expect(friendlyScheduleFromCron("0 9 * * 1-5")).toBeNull();
  expect(friendlyScheduleFromCron("*/15 * * * *")).toBeNull();
  expect(friendlyScheduleFromCron("0 9 * 1 *")).toBeNull();
});

it("counts five fields", () => {
  expect(cronHasFiveFields("0 9 * * 1-5")).toBe(true);
  expect(cronHasFiveFields("0 9 *")).toBe(false);
  expect(cronHasFiveFields(" 0  9 * * *  ")).toBe(true);
});

it("reads the next runs on the schedule's own wall clock", () => {
  // 10:30 UTC is 13:30 in Vilnius: today's 09:00 has passed there.
  expect(wall(nextCronRuns("0 9 * * *", "Europe/Vilnius", 2, NOW))).toEqual(["2026-10-22 9:00", "2026-10-23 9:00"]);
  // In New York it is 06:30, so today's 09:00 is still ahead.
  expect(wall(nextCronRuns("0 9 * * *", "America/New_York", 1, NOW))).toEqual(["2026-10-21 9:00"]);
});

it("reads ranges, steps, lists and names", () => {
  expect(wall(nextCronRuns("15 10 * * 1-5", "UTC", 3, NOW))).toEqual(["2026-10-22 10:15", "2026-10-23 10:15", "2026-10-26 10:15"]);
  expect(wall(nextCronRuns("*/20 11 * * *", "UTC", 3, NOW))).toEqual(["2026-10-21 11:00", "2026-10-21 11:20", "2026-10-21 11:40"]);
  expect(wall(nextCronRuns("0 8 1,15 * *", "UTC", 2, NOW))).toEqual(["2026-11-1 8:00", "2026-11-15 8:00"]);
  expect(wall(nextCronRuns("0 8 * jan mon", "UTC", 1, NOW))).toEqual(["2027-1-4 8:00"]);
});

it("treats day of month and day of week as either-or when both are set", () => {
  expect(wall(nextCronRuns("0 8 25 * 5", "UTC", 2, NOW))).toEqual(["2026-10-23 8:00", "2026-10-25 8:00"]);
});

it("returns null for syntax it does not read, so the form defers to the server", () => {
  expect(nextCronRuns("0 9 L * *", "UTC", 1, NOW)).toBeNull();
  expect(nextCronRuns("0 25 * * *", "UTC", 1, NOW)).toBeNull();
  expect(nextCronRuns("0 9 *", "UTC", 1, NOW)).toBeNull();
});

it("falls back to the host clock for an unknown timezone and formats 24-hour time", () => {
  expect(zoneWallClock(NOW, "Not/AZone").getTime()).toBe(new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate(), NOW.getHours(), NOW.getMinutes()).getTime());
  expect(formatScheduleRun(new Date(2026, 9, 22, 9, 5), "en-GB")).toBe("Thu 22 Oct · 09:05");
});
