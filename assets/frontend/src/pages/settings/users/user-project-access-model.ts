import { useQuery } from "@tanstack/react-query";

import { useFeature } from "@/lib/auth/features";
import type { I18nContextValue } from "@/lib/i18n/context";
import type { UserRecord } from "@/pages/settings/shared/types";

import {
  PROJECT_ROLES,
  fetchProjectGrants,
  projectAccessQueryKeys,
  type PlaceCandidates,
  type PlaceGrant,
  type PlaceGrantInput,
  type ProjectAccessGrants,
  type ProjectRole,
} from "./user-project-access-api";
import { highestSystemRole } from "./user-form-model";

/** The entitlement that offers place access in Users & access (ADR 0082). */
export const SCOPED_ROLES_FEATURE = "scoped_roles";

/** A place of the picker: `workspace:<id>`, `project:<id>` or `workflow:<project>:<workflow>`. */
export type PlaceKey = string;
/** The role chosen for each place; a place without one has no grant of its own. */
export type PlaceRoles = Record<PlaceKey, ProjectRole>;

export const workspaceKey = (workspaceId: string): PlaceKey => `workspace:${workspaceId}`;
export const projectKey = (projectId: string): PlaceKey => `project:${projectId}`;
export const workflowKey = (projectId: string, workflowId: string): PlaceKey => `workflow:${projectId}:${workflowId}`;

export function isProjectRole(value: string): value is ProjectRole {
  return (PROJECT_ROLES as readonly string[]).includes(value);
}

/** The person's stored grants as the picker's roles. */
export function placeRolesFromGrants(grants: ReadonlyArray<PlaceGrant>, userId: string): PlaceRoles {
  const roles: PlaceRoles = {};
  for (const grant of grants) {
    if (grant.user_id !== userId || !isProjectRole(grant.role)) continue;
    if (grant.scope === "workspace") roles[workspaceKey(grant.id)] = grant.role;
    else if (grant.scope === "project") roles[projectKey(grant.id)] = grant.role;
    else if (grant.scope === "workflow" && grant.project_id) roles[workflowKey(grant.project_id, grant.id)] = grant.role;
  }
  return roles;
}

/** The picker's roles as the grants the endpoint takes. */
export function grantsFromPlaceRoles(roles: PlaceRoles): PlaceGrantInput[] {
  const grants: PlaceGrantInput[] = [];
  for (const [key, role] of Object.entries(roles)) {
    if (!isProjectRole(role)) continue;
    const [scope, first, ...rest] = key.split(":");
    if (scope === "workspace" && first) grants.push({ scope: "workspace", id: first, role });
    else if (scope === "project" && first) grants.push({ scope: "project", id: first, role });
    else if (scope === "workflow" && first && rest.length) grants.push({ scope: "workflow", id: rest.join(":"), project_id: first, role });
  }
  return grants;
}

/**
 * The role a place gets from the grant above it when it has none of its own
 * (ADR 0076 §8b): a project from its workspace, a workflow from its project or,
 * through it, the workspace. `null` when nothing above covers it.
 */
export function inheritedRole(
  roles: PlaceRoles,
  place: { workspaceId?: string; projectId?: string; workflow?: boolean },
): ProjectRole | null {
  if (place.workflow && place.projectId) {
    const project = roles[projectKey(place.projectId)];
    if (project) return project;
  }
  if ((place.projectId || place.workflow) && place.workspaceId) return roles[workspaceKey(place.workspaceId)] ?? null;
  return null;
}

/** The picker's tree: workspaces → projects → workflows, from the candidates and stored grants. */
export type PlaceTree = Array<{
  workspaceId: string;
  name: string;
  projects: Array<{ projectId: string; name: string; workflows: Array<{ workflowId: string; name: string }> }>;
}>;

export function placeTree(candidates: PlaceCandidates | undefined, grants: ReadonlyArray<PlaceGrant>, userId: string): PlaceTree {
  const workspaces = new Map<string, { name: string; projects: Map<string, { name: string; workflows: Map<string, string> }> }>();
  const workspace = (id: string, name = "") => {
    if (!workspaces.has(id)) workspaces.set(id, { name: name || id, projects: new Map() });
    return workspaces.get(id)!;
  };
  const projectWorkspace = new Map<string, string>();
  const project = (workspaceId: string, id: string, name = "") => {
    const projects = workspace(workspaceId).projects;
    if (!projects.has(id)) projects.set(id, { name: name || id, workflows: new Map() });
    projectWorkspace.set(id, workspaceId);
    return projects.get(id)!;
  };
  for (const item of candidates?.workspaces ?? []) if (item.workspace_id) workspace(item.workspace_id, item.name);
  for (const item of candidates?.projects ?? []) if (item.project_id && item.workspace_id) project(item.workspace_id, item.project_id, item.name);
  for (const item of candidates?.workflows ?? []) {
    const workspaceId = projectWorkspace.get(item.project_id);
    if (workspaceId && item.workflow_id) project(workspaceId, item.project_id).workflows.set(item.workflow_id, item.name || item.workflow_id);
  }
  // Places the person holds stay listed even when the candidates no longer offer them.
  for (const grant of grants) {
    if (grant.user_id !== userId) continue;
    if (grant.scope === "workspace") workspace(grant.id, grant.name);
    const projectId = grant.scope === "project" ? grant.id : grant.project_id;
    const workspaceId = grant.workspace_id || (projectId ? projectWorkspace.get(projectId) : "");
    if (!projectId || !workspaceId) continue;
    if (grant.scope === "project") workspace(workspaceId, grant.workspace_name);
    const node = project(workspaceId, projectId, grant.project_name);
    if (grant.scope === "workflow") node.workflows.set(grant.id, node.workflows.get(grant.id) ?? (grant.name || grant.id));
  }
  return [...workspaces.entries()].map(([workspaceId, item]) => ({
    workspaceId,
    name: item.name,
    projects: [...item.projects.entries()].map(([projectId, node]) => ({
      projectId,
      name: node.name,
      workflows: [...node.workflows.entries()].map(([workflowId, name]) => ({ workflowId, name })),
    })),
  }));
}

/**
 * "All projects" without a limit; otherwise what the limit covers, such as
 * "Accounting (workspace), 2 projects, 3 workflows", or "No access".
 */
export function projectAccessLabel(user: UserRecord, data: ProjectAccessGrants | undefined, t: I18nContextValue["t"]): string {
  const all = t("react.settings.project_access_all", "All projects");
  if (!data || highestSystemRole(user.roles) === "admin" || !data.limitedUserIds.has(user.id)) return all;
  const own = data.grants.filter((grant) => grant.user_id === user.id);
  const workspaces = own.filter((grant) => grant.scope === "workspace");
  const projects = new Set(own.filter((grant) => grant.scope === "project").map((grant) => grant.id)).size;
  const workflows = own.filter((grant) => grant.scope === "workflow").length;
  const parts: string[] = [];
  if (workspaces.length === 1) {
    parts.push(t("react.settings.project_access_workspace_named", "{name} (workspace)", { name: workspaces[0].name || workspaces[0].id }));
  } else if (workspaces.length > 1) {
    parts.push(t("react.settings.project_access_workspace_count", "{count} workspaces", { count: String(workspaces.length) }));
  }
  if (projects === 1) parts.push(t("react.settings.project_access_one", "1 project"));
  else if (projects > 1) parts.push(t("react.settings.project_access_count", "{count} projects", { count: String(projects) }));
  if (workflows === 1) parts.push(t("react.settings.project_access_workflow_one", "1 workflow"));
  else if (workflows > 1) parts.push(t("react.settings.project_access_workflow_count", "{count} workflows", { count: String(workflows) }));
  return parts.length ? parts.join(", ") : t("react.settings.project_access_no_access", "No access");
}

/**
 * The "Project access" column of Users & access: shown only while the account
 * holds the feature. Without it every person reaches the places their role
 * reaches, and a stored limit still applies on the server.
 */
export function useProjectAccessColumn(): { enabled: boolean; data: ProjectAccessGrants | undefined } {
  const feature = useFeature(SCOPED_ROLES_FEATURE);
  const grantsQuery = useQuery({
    queryKey: projectAccessQueryKeys.grants(feature.endpoint),
    queryFn: () => fetchProjectGrants(feature.endpoint),
    enabled: feature.enabled,
  });
  return { enabled: feature.enabled, data: grantsQuery.data };
}
