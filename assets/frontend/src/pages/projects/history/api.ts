import { fetchJson } from "@/lib/http/client";

export type ProjectHistoryResponse = {
  history?: Array<Record<string, unknown>>;
  revisions?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export type ProjectHistoryDiffResponse = {
  changes?: Array<Record<string, unknown>>;
  summary?: Record<string, unknown>;
  [key: string]: unknown;
};

export async function fetchProjectHistory(projectId: string) {
  return fetchJson<ProjectHistoryResponse>(`/api/projects/${encodeURIComponent(projectId)}/history`);
}

export async function fetchProjectHistoryDiff(projectId: string, revisionId: string) {
  return fetchJson<ProjectHistoryDiffResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/history/${encodeURIComponent(revisionId)}/diff`,
  );
}

export async function restoreProjectHistoryRevision(projectId: string, revisionId: string) {
  return fetchJson<Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/history/${encodeURIComponent(revisionId)}/restore`,
    { method: "POST" },
  );
}
