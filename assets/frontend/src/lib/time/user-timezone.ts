import { useContext } from "react";

import { AuthContext } from "@/lib/auth/context";
import { browserTimezone, formatDateTime } from "@/lib/time/date-time-format";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export { browserTimezone };

/** The date and time in the reader's own date pattern and clock, in the given zone (the reader's by default). */
export function formatUserDateTime(value: unknown, timezone?: string): string {
  return formatDateTime(value, timezone ? { timezone } : undefined);
}

export function useUserTimezone(): string {
  const auth = useContext(AuthContext);
  const session = (auth?.session || {}) as Record<string, unknown>;
  const user = (session.user || {}) as Record<string, unknown>;
  return firstStringFromUnknownValue([user.timezone, session.timezone, browserTimezone()]).trim() || browserTimezone();
}
