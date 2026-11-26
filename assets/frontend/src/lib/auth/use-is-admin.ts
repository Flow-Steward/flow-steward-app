import { useHasPermission } from "@/lib/auth/permissions";

/**
 * True when the person administers the installation: they hold `settings.manage`
 * (ADR 0076), which only the admin role holds. Workflow authoring, workspaces and
 * service accounts are the administrator's too, so their surfaces gate on this hook.
 *
 * Reads the auth context directly (rather than {@link useAuth}) so it degrades to a
 * safe non-admin default when rendered outside an AuthProvider instead of throwing.
 */
export function useIsAdmin(): boolean {
  return useHasPermission("settings.manage");
}

/**
 * True when the person may read the global workflow registry (GET /api/workflows/*):
 * the `registry.read` permission, which admins and operators hold. Viewers are
 * denied, so registry-read surfaces (e.g. the workflow overview) gate their queries
 * on this hook to avoid firing retried 403s after a denied navigation.
 */
export function useCanReadWorkflowRegistry(): boolean {
  return useHasPermission("registry.read");
}
