import { useContext, useMemo } from "react";

import { AuthContext } from "@/lib/auth/context";

export type FrontendCapability =
  | "administer_platform"
  | "create_projects"
  | "mutate_projects"
  | "read_registry";

const CAPABILITY_ROLES: Record<FrontendCapability, ReadonlySet<string>> = {
  administer_platform: new Set(["admin"]),
  create_projects: new Set(["admin"]),
  mutate_projects: new Set(["admin", "operator"]),
  read_registry: new Set(["admin", "operator"]),
};

export function hasCapability(
  roles: readonly string[],
  capability: FrontendCapability,
): boolean {
  const allowedRoles = CAPABILITY_ROLES[capability];
  return roles.some((role) => allowedRoles.has(String(role).trim().toLowerCase()));
}

export function useHasCapability(capability: FrontendCapability): boolean {
  const context = useContext(AuthContext);
  const session = context?.session;
  return useMemo(
    () => hasCapability(session?.user?.roles || session?.roles || [], capability),
    [capability, session],
  );
}
