import type { AuthUser } from "@/lib/auth/AuthProvider";
import matrix from "@/test/fixtures/role-permissions.json";

export type SystemRole = "admin" | "operator" | "viewer";

/**
 * What each system role holds, as the bootstrap's `auth.user.permissions` sends it
 * (ADR 0076). The fixture is the server's matrix: a Python test compares it with
 * `core/application/auth/permissions.py`.
 */
export const ROLE_PERMISSIONS: Readonly<Record<SystemRole, readonly string[]>> = {
  admin: matrix.admin,
  operator: matrix.operator,
  viewer: matrix.viewer,
};

/** The permission list a session user with `roles` carries: the highest role's. */
export function permissionsForRoles(roles: readonly string[]): string[] {
  const held = new Set(roles.map((role) => role.trim().toLowerCase()));
  const highest = (["admin", "operator", "viewer"] as const).find((role) => held.has(role));
  return highest ? [...ROLE_PERMISSIONS[highest]] : [];
}

/** A signed-in person with `role` and that role's permissions, for session fixtures. */
export function sessionUser(role: SystemRole, overrides: Partial<AuthUser> = {}): AuthUser {
  return {
    id: "user-1",
    email: `${role}@example.com`,
    username: `${role}@example.com`,
    display_name: role,
    roles: [role],
    permissions: [...ROLE_PERMISSIONS[role]],
    account_id: "account-1",
    account_ids: ["account-1"],
    account_memberships: [],
    ...overrides,
  };
}
