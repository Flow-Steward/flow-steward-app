import type { UserRecord } from "@/pages/settings/shared/types";

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

export type ConfirmActionKind = "reset" | "deactivate" | "delete";
