import { describe, expect, it } from "vitest";

import { identifierFromName } from "./identifier-from-name";

describe("identifierFromName", () => {
  it.each([
    ["Order Sync (EU)", "order_sync_eu"],
    ["  Review   Tool  ", "review_tool"],
    ["Café résumé", "cafe_resume"],
    ["2024 Report", "report"],
    ["Агент продаж", ""],
    ["", ""],
  ])("reads %j as %j", (name, expected) => {
    expect(identifierFromName(name)).toBe(expected);
  });

  it("stays within 64 characters without a trailing underscore", () => {
    const id = identifierFromName(`${"a".repeat(63)} b`);
    expect(id.length).toBeLessThanOrEqual(64);
    expect(id.endsWith("_")).toBe(false);
  });
});
