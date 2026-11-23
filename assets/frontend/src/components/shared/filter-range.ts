/**
 * A range filter value in `AdvancedTableFilters` is one string, `from..to`;
 * either end may be empty ("10.." is "at least 10").
 */

/** The two ends of a range filter value; a missing end is "". */
export function filterRangeParts(value: string | undefined): [string, string] {
  const text = value || "";
  const at = text.indexOf("..");
  if (at < 0) return [text.trim(), ""];
  return [text.slice(0, at).trim(), text.slice(at + 2).trim()];
}

/** `from..to`, or "" when both ends are empty. */
export function filterRangeValue(from: string, to: string): string {
  return from.trim() || to.trim() ? `${from.trim()}..${to.trim()}` : "";
}
