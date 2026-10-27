import { fetchJson, type FetchJsonOptions } from "@/lib/http/client";
import { readStoredWorkspaceId } from "@/lib/utils/workspace-selection";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type ProjectListItem = {
  id: string;
  name: string;
  operator_default_workflow_id?: string;
  updated_at?: string;
  [key: string]: unknown;
};

export type ProjectsListResponse = {
  projects: ProjectListItem[];
  page: number;
  per_page: number;
  total: number;
  total_pages: number;
};

export type ModelSelectionOptionsResponse = {
  all_models?: unknown;
  provider_models?: unknown;
  provider_options?: unknown;
  provider_labels?: unknown;
};

export async function fetchModelSelectionOptions() {
  return fetchJson<ModelSelectionOptionsResponse>("/api/model-selection/options");
}

export async function fetchProjectsList({
  page,
  perPage,
  search,
  workspaceId,
  includeAllWorkspaces = false,
  hasJobs = "",
  sort = "",
  direction = "",
}: {
  page: number;
  perPage: number;
  search: string;
  workspaceId?: string;
  includeAllWorkspaces?: boolean;
  /** "yes" or "no". */
  hasJobs?: string;
  /** "name" or "job_count"; the endpoint whitelists the sortable keys. */
  sort?: string;
  direction?: "asc" | "desc" | "";
}, options?: FetchJsonOptions) {
  const params = new URLSearchParams({
    page: stringFromUnknownValue(page),
    per_page: stringFromUnknownValue(perPage),
  });
  if (search.trim()) {
    params.set("search", search.trim());
  }
  if (hasJobs) {
    params.set("has_jobs", hasJobs);
  }
  // Sorting is server-owned: sorting the loaded page only would reorder 25 rows
  // and leave the other pages untouched.
  if (sort) {
    params.set("sort", sort);
    params.set("direction", direction || "asc");
  }
  let selectedWorkspaceId = readStoredWorkspaceId();
  if (workspaceId !== undefined) {
    selectedWorkspaceId = workspaceId.trim();
  } else if (includeAllWorkspaces) {
    selectedWorkspaceId = "";
  }
  if (selectedWorkspaceId) {
    params.set("workspace_id", selectedWorkspaceId);
  }
  return fetchJson<ProjectsListResponse>(`/api/v2/projects?${params.toString()}`, undefined, options);
}
