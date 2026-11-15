import { useCallback, useContext, useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { type z } from "@/lib/validation/zod";

import { AuthContext } from "@/lib/auth/context";

const FILTER_STORAGE_PREFIX = "fs.filters.v1:";

export function filterStorageKey(accountId: string, userId: string, scope: string): string {
  return `${FILTER_STORAGE_PREFIX}${JSON.stringify([accountId, userId, scope])}`;
}

/** No anonymous shared bucket: filter preferences belong to an authenticated identity. */
export function useFilterStorageKey(scope: string): string | null {
  const auth = useContext(AuthContext);
  const user = auth?.session?.user;
  return user?.id && user.account_id ? filterStorageKey(user.account_id, user.id, scope) : null;
}

export function readFilterState<T>(key: string | null, schema: z.ZodType<T>, fallback: T): T {
  if (!key) return fallback;
  try {
    const raw = globalThis.localStorage.getItem(key);
    if (raw === null) return fallback;
    const parsed = schema.safeParse(JSON.parse(raw));
    return parsed.success ? parsed.data : fallback;
  } catch {
    // Corrupt/obsolete data and restricted browser storage must not prevent filtering.
    return fallback;
  }
}

export function writeFilterState(key: string | null, value: unknown): void {
  if (!key) return;
  try {
    globalThis.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Preferences are best effort; the live filter still works when storage is blocked/full.
  }
}

/** Restore before the first request, and re-seed when the account or entity scope changes. */
export function useFilterState<T>(
  scope: string,
  schema: z.ZodType<T>,
  initial: T | (() => T),
  preferInitial = false,
): [T, Dispatch<SetStateAction<T>>] {
  const key = useFilterStorageKey(scope);
  const load = () => {
    const fallback = typeof initial === "function" ? (initial as () => T)() : initial;
    return { key, scope, value: preferInitial ? fallback : readFilterState(key, schema, fallback) };
  };
  const [snapshot, setSnapshot] = useState(load);
  let current = snapshot;
  if (snapshot.key !== key || snapshot.scope !== scope) {
    current = load();
    setSnapshot(current);
  }
  const value = current.value;
  useEffect(() => {
    const parsed = schema.safeParse(value);
    if (parsed.success) writeFilterState(key, parsed.data);
  }, [key, schema, value]);
  // Keep writes out of the updater: React may evaluate an updater more than once.
  const setValue = useCallback<Dispatch<SetStateAction<T>>>((next) => {
    setSnapshot((previous) => {
      if (previous.key !== key || previous.scope !== scope) return previous;
      const value = typeof next === "function" ? (next as (value: T) => T)(previous.value) : next;
      return Object.is(value, previous.value) ? previous : { ...previous, value };
    });
  }, [key, scope]);
  return [value, setValue];
}
