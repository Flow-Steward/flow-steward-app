import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { wholeDaysBetween, formatCalendarDay, addDays } from "@/lib/time/calendar-days";

export function toCsv(value: unknown): string {
  if (!Array.isArray(value)) {
    return "";
  }
  return value.map(String).join(", ");
}

export function toStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map(String).filter(Boolean);
}

/**
 * A destructive action usually removes its own trigger. Canon's Modal / Dialog
 * contract then sends focus to the nearest stable element of the region, and to
 * the region's heading when it is empty, never to body. The ids are tried in
 * order once the refreshed region has rendered.
 */
export function focusFirstAvailable(ids: readonly string[]) {
  requestAnimationFrame(() =>
    requestAnimationFrame(() => {
      for (const id of ids) {
        const element = document.getElementById(id);
        if (element && !(element as HTMLButtonElement).disabled) {
          element.focus();
          return;
        }
      }
    }),
  );
}

export function neighbourIds(keys: readonly string[], key: string, prefix: string): string[] {
  const index = keys.indexOf(key);
  if (index < 0) return [];
  return [keys[index + 1], keys[index - 1]].filter(Boolean).map((neighbour) => `${prefix}${neighbour}`);
}

export function serviceAccountKey(value: unknown): string {
  return stringFromUnknownValue(value ?? "").trim();
}

export function tokenId(value: unknown): string {
  return stringFromUnknownValue(value ?? "").trim();
}

export type TokenDrawerMode = "closed" | "create" | "edit";
export type AccountDrawerMode = "closed" | "create" | "detail";

export function toggleStringSelection(values: string[], value: string): string[] {
  return values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
}

export function authorityIncludesScope(grantedScopes: string[], scope: string): boolean {
  if (
    grantedScopes.includes(scope) ||
    grantedScopes.includes("admin:all") ||
    grantedScopes.includes("*:*")
  ) {
    return true;
  }
  const separator = scope.includes(":") ? ":" : ".";
  const namespace = scope.split(separator, 1)[0];
  return grantedScopes.includes(`${namespace}:*`);
}

export async function copyValue(value: string) {
  await navigator.clipboard?.writeText(value);
}

export type ServiceAccountFieldErrors = { key: string; scopes: string };
export type RestrictionOption = { id: string; label: string; description?: string };

export const TOKEN_EXPIRY_MODES = ["30", "90", "custom"] as const;
export type TokenExpiryMode = (typeof TOKEN_EXPIRY_MODES)[number];

export function customExpiryError(date: string, today: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (wholeDaysBetween(today, date) >= 1) return "";
  return t("react.settings.value1_is_not_in_the_future_choose_value2_or_later", "{value1} is not in the future. Choose {value2} or later.", { value1: String(formatCalendarDay(date)), value2: String(formatCalendarDay(addDays(today, 1))) });
}

export type EntityRecord = Record<string, unknown>;
