import { fetchJson } from "@/lib/http/client";

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
