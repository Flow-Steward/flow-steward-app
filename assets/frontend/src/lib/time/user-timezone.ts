import { useContext } from "react";

import { AuthContext } from "@/lib/auth/context";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export function browserTimezone(): string {
  if (globalThis.Intl === undefined) {
    return "UTC";
  }
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

export function formatUserDateTime(value: unknown, timezone = browserTimezone()): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  const date = new Date(stringFromUnknownValue(value));
  if (Number.isNaN(date.getTime())) {
    return stringFromUnknownValue(value);
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: timezone || browserTimezone(),
  }).format(date);
}

export function useUserTimezone(): string {
  const auth = useContext(AuthContext);
  const session = (auth?.session || {}) as Record<string, unknown>;
  const user = (session.user || {}) as Record<string, unknown>;
  return firstStringFromUnknownValue([user.timezone, session.timezone, browserTimezone()]).trim() || browserTimezone();
}
