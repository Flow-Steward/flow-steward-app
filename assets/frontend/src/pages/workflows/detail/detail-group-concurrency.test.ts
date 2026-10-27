import { parseGroupConcurrency } from "./detail-group-concurrency";

describe("optional lane concurrency", () => {
  it.each(["", " \t\n "])("omits blank input %j", (raw) => {
    expect(parseGroupConcurrency(raw)).toEqual({ valid: true, value: undefined });
  });

  it.each([
    ["1", 1], ["2", 2], [" 002 ", 2], ["9007199254740991", 9007199254740991],
  ])("preserves decimal positive safe integer %s exactly", (raw, value) => {
    expect(parseGroupConcurrency(String(raw))).toEqual({ valid: true, value });
  });

  it.each(["0", "00", "-1", "1.5", "2x", "NaN", "Infinity", "1e2", "+2", "0x10", "2 3", "２", "9007199254740992", "9007199254740993", "9".repeat(400)])(
    "rejects %s instead of dropping, truncating or rounding it", (raw) => {
      expect(parseGroupConcurrency(raw)).toEqual({ valid: false });
    },
  );
});
