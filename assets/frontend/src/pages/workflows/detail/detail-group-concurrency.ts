export type GroupConcurrency =
  | { valid: true; value: number | undefined }
  | { valid: false };

/** Blank omits the optional limit; decimal digits may have leading zeroes.
 * Safe integers preserve the submitted value exactly in JavaScript and JSON.
 */
export function parseGroupConcurrency(raw: string): GroupConcurrency {
  const trimmed = raw.trim();
  if (!trimmed) return { valid: true, value: undefined };
  if (!/^\d+$/.test(trimmed)) return { valid: false };
  const value = Number(trimmed);
  return Number.isSafeInteger(value) && value > 0
    ? { valid: true, value }
    : { valid: false };
}
