import { useContext, useMemo } from "react";

import type { AuthSession } from "@/lib/auth/AuthProvider";
import { AuthContext } from "@/lib/auth/context";

/**
 * A key of the ADR 0076 permission catalogue, `core/application/auth/permissions.py`.
 * The same keys decide the HTTP routes and the MCP tools, so a surface gated on one
 * shows exactly what the server lets the person do.
 */
export type PermissionKey =
  | "account.self"
  | "account.profile.write"
  | "jobs.read"
  | "projects.read"
  | "workflows.read"
  | "reports.read"
  | "notify.read"
  | "secrets.read"
  | "extensions.read"
  | "platform.read"
  | "registry.read"
  | "workflows.run"
  | "approvals.decide"
  | "jobs.write"
  | "reports.write"
  | "notify.dispatch"
  | "extensions.use"
  | "secrets.value.write"
  | "projects.write"
  | "workspaces.manage"
  | "workflows.manage"
  | "registry.manage"
  | "extensions.manage"
  | "jobs.manage"
  | "secrets.bind"
  | "secrets.delete"
  | "secrets.approve"
  | "users.manage"
  | "settings.manage"
  | "platform.manage";

const NO_PERMISSIONS: ReadonlySet<string> = new Set();

/**
 * The permissions the server computed for the signed-in person: the bootstrap's
 * `auth.user.permissions`. A principal that is not role-based (`null`) and a session
 * without the list hold none; the interface never guesses from role names.
 */
export function sessionPermissions(session: Pick<AuthSession, "user"> | null | undefined): ReadonlySet<string> {
  const raw = session?.user?.permissions;
  if (!Array.isArray(raw)) return NO_PERMISSIONS;
  return new Set(raw.map((key) => String(key).trim()).filter(Boolean));
}

/**
 * The current person's permissions. Reads the auth context directly, so outside an
 * AuthProvider it degrades to no permissions instead of throwing.
 */
export function usePermissions(): ReadonlySet<string> {
  const context = useContext(AuthContext);
  const session = context?.session;
  return useMemo(() => sessionPermissions(session), [session]);
}

export function useHasPermission(key: PermissionKey): boolean {
  return usePermissions().has(key);
}
