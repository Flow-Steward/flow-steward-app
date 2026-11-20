import { useMemo } from "react";

import { type PermissionKey, usePermissions } from "@/lib/auth/permissions";

export type FrontendCapability =
  | "administer_platform"
  | "manage_users"
  | "create_projects"
  | "mutate_projects"
  | "read_registry"
  | "run_workflows"
  | "manage_secret_values";

/**
 * ADR 0076: a surface shows when the person holds the permission of the routes it
 * calls. The keys come from the server (bootstrap `auth.user.permissions`); which
 * role holds which key is the server's matrix, not the interface's.
 */
export const CAPABILITY_PERMISSIONS: Readonly<Record<FrontendCapability, PermissionKey>> = {
  // Settings, monitoring, extensions, trash, security: the installation's configuration.
  administer_platform: "settings.manage",
  // Users & access: /api/auth/users and accounts.
  manage_users: "users.manage",
  create_projects: "projects.write",
  mutate_projects: "projects.write",
  // The registry pages are the admin's configuration surface; an operator's
  // registry read serves the sections, not these pages.
  read_registry: "registry.manage",
  run_workflows: "workflows.run",
  manage_secret_values: "secrets.value.write",
};

export function hasCapability(
  permissions: ReadonlySet<string> | readonly string[] | null | undefined,
  capability: FrontendCapability,
): boolean {
  if (!permissions) return false;
  const key = CAPABILITY_PERMISSIONS[capability];
  return "has" in permissions ? permissions.has(key) : permissions.includes(key);
}

export function useHasCapability(capability: FrontendCapability): boolean {
  const permissions = usePermissions();
  return useMemo(() => hasCapability(permissions, capability), [capability, permissions]);
}
