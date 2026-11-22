import type { I18nContextValue } from "@/lib/i18n/context";
import type { UserAuthenticatorAction, UserRecord } from "@/pages/settings/shared/types";

export const ROLE_OPTIONS = ["viewer", "operator", "admin"];
export type EditorState = {
  displayName: string;
  email: string;
  password: string;
  roles: string[];
  active: boolean;
  mustChangePassword: boolean;
};
export type EditorMode = "closed" | "add" | "edit";

export function userLabel(user: UserRecord) {
  return user.display_name || user.email || user.username || user.id;
}

export type ConfirmActionKind = "reset-password" | "deactivate" | "delete" | UserAuthenticatorAction;
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
