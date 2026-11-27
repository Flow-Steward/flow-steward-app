import { useContext, useMemo } from "react";

import type { AuthSession } from "@/lib/auth/AuthProvider";
import { AuthContext } from "@/lib/auth/context";
import { type PermissionKey, sessionPermissions } from "@/lib/auth/permissions";

const NO_PERMISSIONS: ReadonlySet<string> = new Set();

/**
 * Whether the person is limited to chosen projects (ADR 0076 §8): the bootstrap's
 * `auth.user.project_permissions` is a map, possibly empty. `null` or absent means
 * no limit, so `auth.user.permissions` applies in every project.
 */
export function sessionHasProjectLimit(session: Pick<AuthSession, "user"> | null | undefined): boolean {
  const raw = session?.user?.project_permissions;
  return Boolean(raw) && typeof raw === "object" && !Array.isArray(raw);
}

/**
 * The permissions the person holds in one project. Without a limit they are the
 * person's own permissions; with one, the keys the server computed for that
 * project, and none for a project the limit does not include. Without a project
 * id the person's own permissions apply: they are the ceiling of every project.
 */
export function sessionProjectPermissions(
  session: Pick<AuthSession, "user"> | null | undefined,
  projectId: string | null | undefined,
): ReadonlySet<string> {
  const id = String(projectId ?? "").trim();
  if (!id || !sessionHasProjectLimit(session)) return sessionPermissions(session);
  return keysWithin(session?.user?.project_permissions?.[id], sessionPermissions(session));
}

/** The current person's permissions in one project; outside an AuthProvider, none. */
export function useProjectPermissions(projectId: string | null | undefined): ReadonlySet<string> {
  const session = useContext(AuthContext)?.session;
  return useMemo(() => sessionProjectPermissions(session, projectId), [session, projectId]);
}

/**
 * Whether a person's project limit lets them do `key` in this project. Always
 * true without a limit: the control keeps whatever rule decided it before, such
 * as the access a review states.
 */
export function useProjectLimitAllows(projectId: string | null | undefined, key: PermissionKey): boolean {
  const session = useContext(AuthContext)?.session;
  return useMemo(
    () => !sessionHasProjectLimit(session) || sessionProjectPermissions(session, projectId).has(key),
    [session, projectId, key],
  );
}

function keysWithin(keys: unknown, ceiling: ReadonlySet<string>): ReadonlySet<string> {
  if (!Array.isArray(keys)) return NO_PERMISSIONS;
  return new Set(keys.map((key) => String(key).trim()).filter((key) => key && ceiling.has(key)));
}

/**
 * The permissions the person holds in one workspace (ADR 0076 §8b). Without a
 * limit they are the person's own; with one, the workspace's grant, read access
 * for a workspace shown only because a place inside it is granted, and none for
 * any other workspace.
 */
export function sessionWorkspacePermissions(
  session: Pick<AuthSession, "user"> | null | undefined,
  workspaceId: string | null | undefined,
): ReadonlySet<string> {
  const id = String(workspaceId ?? "").trim();
  if (!id || !sessionHasProjectLimit(session)) return sessionPermissions(session);
  return keysWithin(session?.user?.workspace_permissions?.[id], sessionPermissions(session));
}

/**
 * The permissions the person holds in one workflow of a project (ADR 0076 §8b):
 * the workflow's grant, else the project's (which already folds in its
 * workspace). In a project reached only through workflow grants, any other
 * workflow is out of reach. Without a workflow id this is the project's set.
 */
export function sessionWorkflowPermissions(
  session: Pick<AuthSession, "user"> | null | undefined,
  projectId: string | null | undefined,
  workflowId: string | null | undefined,
): ReadonlySet<string> {
  const project = String(projectId ?? "").trim();
  const workflow = String(workflowId ?? "").trim();
  if (!project || !workflow || !sessionHasProjectLimit(session)) return sessionProjectPermissions(session, projectId);
  const ceiling = sessionPermissions(session);
  const granted = session?.user?.workflow_permissions?.[project]?.[workflow];
  if (Array.isArray(granted)) return keysWithin(granted, ceiling);
  const restricted = session?.user?.project_workflow_ids?.[project];
  if (Array.isArray(restricted) && !restricted.map(String).includes(workflow)) return NO_PERMISSIONS;
  return sessionProjectPermissions(session, project);
}

/**
 * Whether a limited person reaches this workflow of the project at all: always
 * true without a limit and outside projects reached only through workflow grants.
 */
export function sessionWorkflowReachable(
  session: Pick<AuthSession, "user"> | null | undefined,
  projectId: string | null | undefined,
  workflowId: string | null | undefined,
): boolean {
  const project = String(projectId ?? "").trim();
  if (!project || !sessionHasProjectLimit(session)) return true;
  if (!session?.user?.project_permissions?.[project]) return false;
  const restricted = session?.user?.project_workflow_ids?.[project];
  if (!Array.isArray(restricted)) return true;
  return restricted.map(String).includes(String(workflowId ?? "").trim());
}

/** The current person's permissions in one workspace; outside an AuthProvider, none. */
export function useWorkspacePermissions(workspaceId: string | null | undefined): ReadonlySet<string> {
  const session = useContext(AuthContext)?.session;
  return useMemo(() => sessionWorkspacePermissions(session, workspaceId), [session, workspaceId]);
}

/** The current person's permissions in one workflow of a project. */
export function useWorkflowPermissions(
  projectId: string | null | undefined,
  workflowId: string | null | undefined,
): ReadonlySet<string> {
  const session = useContext(AuthContext)?.session;
  return useMemo(() => sessionWorkflowPermissions(session, projectId, workflowId), [session, projectId, workflowId]);
}

/**
 * Whether a person's limit lets them do `key` in this workflow of the project.
 * Always true without a limit, like {@link useProjectLimitAllows}.
 */
export function useWorkflowLimitAllows(
  projectId: string | null | undefined,
  workflowId: string | null | undefined,
  key: PermissionKey,
): boolean {
  const session = useContext(AuthContext)?.session;
  return useMemo(
    () => !sessionHasProjectLimit(session) || sessionWorkflowPermissions(session, projectId, workflowId).has(key),
    [session, projectId, workflowId, key],
  );
}
