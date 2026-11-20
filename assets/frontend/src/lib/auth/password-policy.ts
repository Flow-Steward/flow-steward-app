import type { I18nContextValue } from "@/lib/i18n/context";

export const PASSWORD_MIN_LENGTH = 12;

/** Password rules shown by the forms. The server may require a greater minimum length. */
export function passwordRequirements(t: I18nContextValue["t"], count = PASSWORD_MIN_LENGTH) {
  return t(
    "react.auth.password_composition",
    "Use at least {count} characters, including uppercase and lowercase letters, a number and a special character.",
    { count },
  );
}
