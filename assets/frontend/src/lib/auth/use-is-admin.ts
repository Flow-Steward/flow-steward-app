import { useContext, useMemo } from "react";

import { AuthContext } from "@/lib/auth/context";

/**
 * True when the current session has the `admin` role. Workflow authoring
 * (create/import/clone/edit/workflow authoring) is admin-only on the backend, so the
 * UI gates the same surfaces on this hook to keep API and UI RBAC in parity.
 *
 * Reads the auth context directly (rather than {@link useAuth}) so it degrades to a
 * safe non-admin default when rendered outside an AuthProvider instead of throwing.
 */
export function useIsAdmin(): boolean {
  const context = useContext(AuthContext);
  const session = context?.session;
  return useMemo(() => {
    const roles = session?.user?.roles || session?.roles || [];
    return roles.some((role) => String(role).toLowerCase() === "admin");
  }, [session]);
}

/**
 * True when the current session may read the global workflow registry
 * (GET /api/workflows/*), which the backend allows for `operator` and `admin`.
 * Viewers are denied, so registry-read surfaces (e.g. the workflow overview)
 * gate their queries on this hook to avoid firing retried 403s after a denied
 * navigation.
 *
 * Reads the auth context directly so it degrades to a safe denied default when
 * rendered outside an AuthProvider instead of throwing.
 */
export function useCanReadWorkflowRegistry(): boolean {
  const context = useContext(AuthContext);
  const session = context?.session;
  return useMemo(() => {
    const roles = session?.user?.roles || session?.roles || [];
    return roles.some((role) => {
      const normalized = String(role).toLowerCase();
      return normalized === "admin" || normalized === "operator";
    });
  }, [session]);
}
