import { fetchJson } from "@/lib/http/client";
import type { WorkflowChangeIssue } from "@/pages/workflows/builder/api";
import type { WorkflowAuthoringContent } from "@/pages/workflows/detail/workflow-edit-session";

/**
 * The server side of an edit session conflict (Task 6): compare the local copy
 * with the server, and keep it on the author's explicit choice. The server
 * stores no session; both calls are guarded by the workflow's own policy.
 */

export type EditSessionChange = { path: string; change: string; from: unknown; to: unknown };
export type EditSessionComparison = {
  changes: EditSessionChange[];
  summary: { total: number; added: number; removed: number; changed: number };
};
export type EditSessionCompareResult = {
  workflow_id: string;
  current_revision: number;
  current_content: Record<string, unknown>;
  base_to_local: EditSessionComparison;
  base_to_server: EditSessionComparison;
  local_to_server: EditSessionComparison;
};
export type EditSessionApplyResult = {
  workflow_id: string;
  revision: number;
  applied: boolean;
  valid: boolean;
  issues: WorkflowChangeIssue[];
  blocking_issues: WorkflowChangeIssue[];
};

function positiveRevision(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function sessionUrl(workflowId: string, action: "compare" | "apply"): string {
  return `/api/workflows/${encodeURIComponent(workflowId)}/edit-session/${action}`;
}

/** Three comparisons of the local copy and its base with the current server version. Writes nothing. */
export async function compareWorkflowEditSession(
  workflowId: string,
  payload: { base_content: WorkflowAuthoringContent; local_content: WorkflowAuthoringContent },
): Promise<EditSessionCompareResult> {
  const result = await fetchJson<EditSessionCompareResult>(sessionUrl(workflowId, "compare"), {
    method: "POST",
    body: JSON.stringify(payload),
  });
  if (!positiveRevision(result?.current_revision)) {
    throw new Error("The server did not name the current revision.");
  }
  return result;
}

/**
 * "Keep my version": the reviewed copy, written as one revision only while the
 * server is still at the reviewed revision. A missing or invalid revision is
 * refused here, before any request.
 */
export async function applyWorkflowEditSession(
  workflowId: string,
  payload: { content: WorkflowAuthoringContent; expected_revision: number; dry_run?: boolean },
): Promise<EditSessionApplyResult> {
  if (!positiveRevision(payload.expected_revision)) {
    throw new Error("The reviewed revision is unknown. Compare the versions again.");
  }
  return fetchJson<EditSessionApplyResult>(sessionUrl(workflowId, "apply"), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
