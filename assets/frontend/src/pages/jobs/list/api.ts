import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type JobListItem = {
  id: string;
  job_id?: string;
  status: string;
  task_source?: string | null;
  duration_ms?: number | null;
  task_summary?: string;
  task_summary_key?: string;
  task_summary_params?: Record<string, unknown>;
  display_summary?: string;
  project_name?: string;
  project_id?: string;
  selected_workflow?: string;
  selected_workflow_display_name?: string;
  started_at?: string;
  queued_at?: string;
  finished_at?: string;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
};

export type JobsListResponse = {
  jobs?: JobListItem[];
  groups?: Record<string, JobListItem[]>;
  page?: number;
  per_page?: number;
  total?: number;
  total_pages?: number;
};

export type JobsListFilters = {
  page: number;
  perPage: number;
  /** One stored status or a comma list of them. */
  status: string;
  /** Leaves out runs an operator has acknowledged. */
  hideAcknowledged?: boolean;
  projectId: string | null;
  workflowId?: string | null;
  taskSource?: "workflow_test_run";
  search?: string;
  workspaceId?: string | null;
};

/** `status` is a comma list; the API takes `status` once per value and matches any. */
export function jobStatusParams(status: string): string[] {
  return status.split(",").map((value) => value.trim()).filter(Boolean);
}

export async function fetchJobsList(filters: JobsListFilters) {
  const params = new URLSearchParams();
  params.set("page", stringFromUnknownValue(filters.page));
  params.set("per_page", stringFromUnknownValue(filters.perPage));
  jobStatusParams(filters.status).forEach((status) => params.append("status", status));
  if (filters.hideAcknowledged) {
    params.set("hide_acknowledged", "true");
  }
  if (filters.projectId?.trim()) {
    params.set("project_id", filters.projectId.trim());
  }
  if (filters.workflowId?.trim()) {
    params.set("workflow_id", filters.workflowId.trim());
  }
  if (filters.taskSource) {
    params.set("task_source", filters.taskSource);
  }
  if (filters.search?.trim()) {
    params.set("search", filters.search.trim());
  }
  if (filters.workspaceId?.trim()) {
    params.set("workspace_id", filters.workspaceId.trim());
  }
  const payload = await fetchJson<JobsListResponse>(`/api/v2/jobs?${params.toString()}`);
  // Preserve raw identity provenance before normalizeRow canonicalizes both aliases.
  // Only exact Test Run history is fail-closed; ordinary Jobs lists retain their
  // established alias fallback behavior.
  const scopedRows = (rows: JobListItem[]) => filters.taskSource === "workflow_test_run"
    ? rows.filter((row) => {
      if (!row || typeof row !== "object" || Array.isArray(row)) return false;
      const hasId = Object.hasOwn(row, "id");
      const hasJobId = Object.hasOwn(row, "job_id");
      if (!hasId || !hasJobId) return true;
      const id = firstStringFromUnknownValue([row.id, ""]).trim();
      const jobId = firstStringFromUnknownValue([row.job_id, ""]).trim();
      return Boolean(id && jobId && id === jobId);
    }) : rows;
  const normalizeRow = (row: JobListItem): JobListItem => {
    const jobId = firstStringFromUnknownValue([row.id, row.job_id, ""]).trim();
    return {
      ...row,
      id: jobId,
      job_id: jobId,
      project_id: row.project_id === undefined || row.project_id === null ? undefined : stringFromUnknownValue(row.project_id),
    };
  };
  const normalizedJobs = Array.isArray(payload.jobs)
    ? scopedRows(payload.jobs).map((row) => normalizeRow(row))
    : undefined;
  const normalizedGroups =
    payload.groups && typeof payload.groups === "object"
      ? Object.fromEntries(
          Object.entries(payload.groups).map(([groupKey, rows]) => [
            groupKey,
            Array.isArray(rows) ? scopedRows(rows).map((row) => normalizeRow(row)) : [],
          ]),
        )
      : undefined;
  return {
    ...payload,
    jobs: normalizedJobs,
    groups: normalizedGroups,
  };
}
