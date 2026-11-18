import { fetchJson } from "@/lib/http/client";

import type { ProjectUserSection, ProjectUserSectionView, SectionJobRow } from "./types";

export type SectionProject = {
  id: string;
  name: string;
  workflow_ids: string[];
  user_sections: ProjectUserSectionView[];
};

/** The project with its sections resolved against each workflow's contract. */
export async function fetchSectionProject(projectId: string): Promise<SectionProject> {
  const payload = await fetchJson<Record<string, unknown>>(`/api/projects/${encodeURIComponent(projectId)}`);
  const sections = Array.isArray(payload.user_sections) ? (payload.user_sections as ProjectUserSectionView[]) : [];
  const workflowIds = Array.isArray(payload.project_workflow_ids)
    ? (payload.project_workflow_ids as string[])
    : Array.isArray(payload.workflow_ids)
      ? (payload.workflow_ids as string[])
      : [];
  return {
    id: String(payload.id ?? projectId),
    name: String(payload.name ?? ""),
    workflow_ids: workflowIds.map(String),
    user_sections: sections,
  };
}

/** Saves the project's sections with the existing project update; other settings stay. */
export async function saveProjectSections(projectId: string, sections: ProjectUserSection[]) {
  return fetchJson<Record<string, unknown>>(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: "PUT",
    body: JSON.stringify({ user_sections: sections }),
  });
}

export type SectionJobsPage = {
  jobs: SectionJobRow[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
};

export type SectionJobsFilters = {
  page: number;
  perPage: number;
  status?: string;
  fromDate?: string;
  toDate?: string;
  taskSource?: "" | "manual_ui" | "mcp_workflow";
};

/** One page of a section's history with its column values (one server query per page). */
export async function fetchSectionJobs(projectId: string, sectionId: string, filters: SectionJobsFilters) {
  const params = new URLSearchParams({
    project_id: projectId,
    section_id: sectionId,
    page: String(filters.page),
    per_page: String(filters.perPage),
  });
  if (filters.status) params.append("status", filters.status);
  if (filters.fromDate) params.set("from_date", filters.fromDate);
  if (filters.toDate) params.set("to_date", filters.toDate);
  if (filters.taskSource) params.set("task_source", filters.taskSource);
  const payload = await fetchJson<SectionJobsPage>(`/api/v2/jobs?${params.toString()}`);
  return {
    ...payload,
    jobs: (payload.jobs ?? []).map((row) => ({
      ...row,
      job_id: String(row.job_id ?? (row as Record<string, unknown>).id ?? ""),
    })),
  } satisfies SectionJobsPage;
}
