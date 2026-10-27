import { fetchJson, type FetchJsonOptions } from "@/lib/http/client";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export type ProjectWorkflowBindings = {
  project_id: string;
  workflow_ids: string[];
  operator_default_workflow_id: string;
  project_workflows: Array<Record<string, unknown>>;
};
export type WorkflowBindingsSavePayload = {
  workflow_ids: string[];
  /** Omit to keep the project's current default; "" clears it. */
  operator_default_workflow_id?: string;
};

function toStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => firstStringFromUnknownValue([item, ""]).trim()).filter(isTruthy);
}

export function normalizeWorkflowBindingsSavePayload(
  payload: WorkflowBindingsSavePayload,
): WorkflowBindingsSavePayload {
  const workflowIds = toStringArray(payload.workflow_ids);
  if (payload.operator_default_workflow_id === undefined) {
    return { workflow_ids: workflowIds };
  }
  const defaultWorkflowId = firstStringFromUnknownValue([payload.operator_default_workflow_id, ""]).trim();
  return {
    workflow_ids: workflowIds,
    operator_default_workflow_id: defaultWorkflowId,
  };
}

export async function fetchProjectWorkflowBindings(projectId: string, options?: FetchJsonOptions) {
  return fetchJson<ProjectWorkflowBindings>(`/api/projects/${encodeURIComponent(projectId)}/workflows`, undefined, options);
}

export async function saveProjectWorkflowBindings(
  projectId: string,
  payload: WorkflowBindingsSavePayload,
) {
  return fetchJson<ProjectWorkflowBindings>(`/api/projects/${encodeURIComponent(projectId)}/workflows`, {
    method: "PUT",
    body: JSON.stringify(normalizeWorkflowBindingsSavePayload(payload)),
  });
}
