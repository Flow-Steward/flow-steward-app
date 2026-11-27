import { fetchJson } from "@/lib/http/client";

/**
 * The access of people to workspaces, projects and workflows (ADR 0076 §8b), read
 * and written through the endpoint the bootstrap names for the `scoped_roles`
 * feature (ADR 0082). Core holds the data; the endpoint belongs to the module that
 * offers the feature.
 *
 * - `GET {base}/grants[?user_id=]` → `{ grants: PlaceGrant[], limited_user_ids?: string[] }`
 * - `GET {base}/candidates` → `{ workspaces, projects, workflows }`
 * - `PUT {base}/users/{user_id}/grants` `{ grants: [{ scope, id, role, project_id? }] }`: limit the person
 * - `DELETE {base}/users/{user_id}/grants`: lift the limit
 */

/** The roles a person can hold in a place; the person's own role stays the ceiling. */
export const PROJECT_ROLES = ["operator", "viewer"] as const;
export type ProjectRole = (typeof PROJECT_ROLES)[number];
export type PlaceScope = "workspace" | "project" | "workflow";

export type PlaceGrant = {
  user_id: string;
  scope: PlaceScope;
  /** The granted place: a workspace id, a project id or a workflow id. */
  id: string;
  name?: string;
  role: string;
  workspace_id?: string;
  workspace_name?: string;
  /** A project grant's project, or a workflow grant's project. */
  project_id?: string;
  project_name?: string;
  workflow_id?: string;
  workflow_name?: string;
  username?: string;
  display_name?: string;
};
export type WorkspaceCandidate = { workspace_id: string; name: string };
export type ProjectCandidate = { project_id: string; workspace_id: string; name: string };
export type WorkflowCandidate = { workflow_id: string; project_id: string; name: string };
export type PlaceCandidates = {
  workspaces: WorkspaceCandidate[];
  projects: ProjectCandidate[];
  workflows: WorkflowCandidate[];
};
export type PlaceGrantInput = { scope: PlaceScope; id: string; role: ProjectRole; project_id?: string };
export type ProjectAccessGrants = { grants: PlaceGrant[]; limitedUserIds: ReadonlySet<string> };

export const projectAccessQueryKeys = {
  all: (base: string) => ["scoped-roles", base] as const,
  grants: (base: string, userId = "") => ["scoped-roles", base, "grants", userId] as const,
  candidates: (base: string) => ["scoped-roles", base, "candidates"] as const,
};

function userGrantsPath(base: string, userId: string): string {
  return `${base}/users/${encodeURIComponent(userId)}/grants`;
}

function list<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

/**
 * The account's grants, or one person's. A person is limited when the server
 * lists them as limited or holds a grant for them: a limit whose places were all
 * deleted still limits, so it is listed without grants.
 */
export async function fetchProjectGrants(base: string, userId = ""): Promise<ProjectAccessGrants> {
  const query = userId ? `?${new URLSearchParams({ user_id: userId }).toString()}` : "";
  const response = await fetchJson<{ grants?: PlaceGrant[]; limited_user_ids?: string[] }>(`${base}/grants${query}`, { method: "GET" });
  const grants = list<PlaceGrant>(response?.grants);
  const limited = new Set<string>(list<string>(response?.limited_user_ids).map(String));
  for (const grant of grants) if (grant.user_id) limited.add(grant.user_id);
  return { grants, limitedUserIds: limited };
}

export async function fetchPlaceCandidates(base: string): Promise<PlaceCandidates> {
  const response = await fetchJson<Partial<PlaceCandidates>>(`${base}/candidates`, { method: "GET" });
  return {
    workspaces: list<WorkspaceCandidate>(response?.workspaces),
    projects: list<ProjectCandidate>(response?.projects),
    workflows: list<WorkflowCandidate>(response?.workflows),
  };
}

export async function limitUserToPlaces(base: string, userId: string, grants: ReadonlyArray<PlaceGrantInput>): Promise<void> {
  await fetchJson(userGrantsPath(base, userId), { method: "PUT", body: JSON.stringify({ grants }) });
}

export async function liftUserProjectLimit(base: string, userId: string): Promise<void> {
  await fetchJson(userGrantsPath(base, userId), { method: "DELETE" });
}
