import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { UserAuthenticatorAction, UserRecord } from "@/pages/settings/shared/types";

/** The system roles, lowest first (ADR 0066, ADR 0076 §7): a user holds one of them. */
export const ROLE_OPTIONS = ["viewer", "operator", "admin"];
export type EditorState = {
  displayName: string;
  email: string;
  password: string;
  /** One system role: a higher one already includes everything a lower one can do. */
  role: string;
  active: boolean;
  mustChangePassword: boolean;
};
export type EditorMode = "closed" | "add" | "edit";

/** The role's human name. */
export function roleLabel(role: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (role === "admin") return t("react.settings.role_admin", "Admin");
  if (role === "operator") return t("react.settings.role_operator", "Operator");
  if (role === "viewer") return t("react.settings.role_viewer", "Viewer");
  return role;
}

/** The highest system role among `roles`, or "" when there is none. */
export function highestSystemRole(roles: readonly string[]): string {
  const held = new Set(roles.map((role) => String(role).trim().toLowerCase()));
  return [...ROLE_OPTIONS].reverse().find((role) => held.has(role)) || "";
}

export function userLabel(user: UserRecord) {
  return user.display_name || user.email || user.username || user.id;
}

/** `remove`: take the person out of the acting account only (ADR 0085). */
export type ConfirmActionKind = "reset-password" | "deactivate" | "delete" | "remove" | UserAuthenticatorAction;
export type ConfirmAction = { kind: ConfirmActionKind; user: UserRecord; error?: string };

export function isUserAuthenticatorAction(kind: ConfirmActionKind): kind is UserAuthenticatorAction {
  return kind === "require" || kind === "unrequire" || kind === "reset";
}

export function userAuthenticatorActionLabel(kind: UserAuthenticatorAction, t: I18nContextValue["t"]): string {
  switch (kind) {
    case "require":
      return t("react.settings.require_authenticator", "Require authenticator");
    case "unrequire":
      return t("react.settings.remove_individual_authenticator_requirement", "Remove individual authenticator requirement");
    case "reset":
      return t("react.settings.reset_authenticator", "Reset authenticator");
  }
}
