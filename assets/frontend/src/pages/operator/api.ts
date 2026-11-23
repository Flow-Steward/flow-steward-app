import { fetchJson } from "@/lib/http/client";

import type { JobReview } from "./review-model";
import type { OperatorSectionDetail, OperatorSectionItem } from "./types";

/** The caller's menu: published workflows per project, sorted by project then title. */
export async function fetchOperatorSections(): Promise<OperatorSectionItem[]> {
  const payload = await fetchJson<{ sections?: OperatorSectionItem[] }>("/api/v2/projects/sections");
  return Array.isArray(payload?.sections) ? payload.sections : [];
}

/** One section with its runtime view; 404 when it is not in the caller's menu. */
export function fetchOperatorSection(projectId: string, workflowId: string): Promise<OperatorSectionDetail> {
  return fetchJson<OperatorSectionDetail>(
    `/api/v2/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}/section`,
  );
}

/**
 * The review a job waits for, here or down its Invoke chain, read with the
 * rights on the job that holds it: without read access only that it waits.
 * The section's project and workflow go with it: a job of another section is
 * not found.
 */
export function fetchJobReview(jobId: string, projectId: string, workflowId: string): Promise<JobReview> {
  const params = new URLSearchParams({ project_id: projectId, workflow_id: workflowId });
  return fetchJson<JobReview>(`/api/v2/jobs/${encodeURIComponent(jobId)}/review?${params.toString()}`);
}
