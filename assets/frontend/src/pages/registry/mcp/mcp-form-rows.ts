import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { type KeyValueRow } from "./key-value-rows";

export type ScopeRow = { id: number; value: string };

let formRowSequence = 0;

export function scopeRows(values: unknown[]): ScopeRow[] {
  return values.map((value) => ({ id: ++formRowSequence, value: String(value) }));
}

export function rowsFromRecord(value: unknown): KeyValueRow[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>).map(([key, item], index) => ({
    id: index + 1,
    key,
    value: stringFromUnknownValue(item),
  }));
}
