import { userLabel, type EditorState, type EditorMode } from "./user-form-model";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { createUser, updateUser } from "@/pages/settings/shared/api";
import type { CreateUserInput, UpdateUserInput, UserRecord } from "@/pages/settings/shared/types";

export const EMPTY_EDITOR: EditorState = {
  displayName: "",
  email: "",
  password: "",
  roles: ["viewer"],
  active: true,
  mustChangePassword: true,
};

export function initials(value: string) {
  return value.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "U";
}

export function userStatusLabel(user: UserRecord, busyUserId: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (busyUserId === user.id) {
    return t("react.settings.updating", "Updating…");
  }
  return user.is_active ? t("account_settings.status.active", "Active") : t("users.filter.inactive", "Inactive");
}

export function editorForUser(user: UserRecord): EditorState {
  return {
    displayName: user.display_name,
    email: user.email,
    password: "",
    roles: user.roles.length ? user.roles : ["viewer"],
    active: user.is_active,
    mustChangePassword: user.must_change_password,
  };
}

export function editorValidationError(
  editor: EditorState,
  mode: EditorMode, t: I18nContextValue["t"] = fallbackContext.t,
): string {
  if (!editor.displayName.trim()) return t("react.settings.display_name_is_required", "Display name is required.");
  if (!editor.roles.length) return t("users.validation.select_role", "Select at least one role.");
  if (mode === "add" && (!editor.email.includes("@") || editor.password.length < 10)) {
    return t("react.settings.enter_a_valid_email_and_a_temporary_password_of_at_least_10_characters", "Enter a valid email and a temporary password of at least 10 characters.");
  }
  return "";
}

export function createUserPayload(editor: EditorState): CreateUserInput {
  return {
    display_name: editor.displayName.trim(),
    email: editor.email.trim(),
    password: editor.password,
    roles: editor.roles,
    is_active: editor.active,
    must_change_password: editor.mustChangePassword,
  };
}

export function updateUserPayload(editor: EditorState, user: UserRecord): UpdateUserInput {
  const initial = editorForUser(user);
  const payload: UpdateUserInput = {};
  if (editor.displayName.trim() !== initial.displayName) payload.display_name = editor.displayName.trim();
  if (JSON.stringify(editor.roles) !== JSON.stringify(initial.roles)) payload.roles = editor.roles;
  if (editor.active !== initial.active) payload.is_active = editor.active;
  if (editor.mustChangePassword !== initial.mustChangePassword) payload.must_change_password = editor.mustChangePassword;
  return payload;
}

export async function persistUserEditor(
  mode: EditorMode,
  editor: EditorState,
  editingUser: UserRecord | null,
  t: I18nContextValue["t"],
): Promise<string> {
  if (mode === "add") {
    const result = await createUser(createUserPayload(editor));
    const warnings = Array.isArray(result.warnings)
      ? result.warnings.filter((warning): warning is string => typeof warning === "string" && Boolean(warning.trim()))
      : [];
    return [t("react.settings.user_added_feedback", "{name} was added.", { name: editor.displayName.trim() }), ...warnings].join(" ");
  }
  if (!editingUser) {
    throw new Error(t("react.settings.choose_user_access_save", "Choose a user before saving access changes."));
  }
  const payload = updateUserPayload(editor, editingUser);
  if (!Object.keys(payload).length) {
    throw new Error(t("react.settings.change_setting_before_save", "Change at least one setting before saving."));
  }
  const result = await updateUser(editingUser.id, payload);
  if (
    payload.display_name !== undefined
    && stringFromUnknownValue(result.user?.display_name).trim() !== payload.display_name
  ) {
    throw new Error(t("react.settings.saved_display_name_mismatch", "The saved display name did not match the submitted value."));
  }
  return t("react.settings.user_access_updated", "Access for {name} was updated.", { name: userLabel(editingUser) });
}
