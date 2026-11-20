import type { UserRecord } from "@/pages/settings/shared/types";

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

/** The highest system role among `roles`, or "" when there is none. */
export function highestSystemRole(roles: readonly string[]): string {
  const held = new Set(roles.map((role) => String(role).trim().toLowerCase()));
  return [...ROLE_OPTIONS].reverse().find((role) => held.has(role)) || "";
}

export function userLabel(user: UserRecord) {
  return user.display_name || user.email || user.username || user.id;
}

export type ConfirmActionKind = "reset" | "deactivate" | "delete";
