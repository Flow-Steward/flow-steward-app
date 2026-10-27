import { fetchJson } from "@/lib/http/client";

export type WorkflowReactionsPayload = {
  workflow_id?: string;
  scope_kind?: string;
  scope_id?: string;
  reactions?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export async function fetchWorkflowReactions({
  projectId = "",
  scopeId = "",
  scopeKind = "workflow",
  workflowId,
}: {
  projectId?: string;
  scopeId?: string;
  scopeKind?: string;
  workflowId: string;
}) {
  const params = new URLSearchParams();
  if (projectId) {
    params.set("project_id", projectId);
  }
  if (scopeKind && scopeKind !== "workflow") {
    params.set("scope_kind", scopeKind);
    params.set("scope_id", scopeId);
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return fetchJson<WorkflowReactionsPayload>(`/api/workflows/${encodeURIComponent(workflowId)}/reactions${suffix}`);
}

export async function updateWorkflowReactions({
  projectId = "",
  workflowId,
  reactions,
  scopeId = "",
  scopeKind = "workflow",
}: {
  projectId?: string;
  workflowId: string;
  reactions: Array<Record<string, unknown>>;
  scopeId?: string;
  scopeKind?: string;
}) {
  const params = new URLSearchParams();
  if (projectId) {
    params.set("project_id", projectId);
  }
  if (scopeKind && scopeKind !== "workflow") {
    params.set("scope_kind", scopeKind);
    params.set("scope_id", scopeId);
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return fetchJson<WorkflowReactionsPayload>(`/api/workflows/${encodeURIComponent(workflowId)}/reactions${suffix}`, {
    method: "POST",
    body: JSON.stringify({ reactions }),
  });
}
