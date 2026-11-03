/**
 * Catalog secrets in a workflow's secret inputs (ADR 0055).
 *
 * A binding is its own operation and never part of the workflow definition, so
 * none of these calls goes through the workflow save, and the save never carries
 * a binding. Approval covers the saved definition the server fingerprints.
 */
import { fetchJson } from "@/lib/http/client";

export type WorkflowSecretDestination = {
  kind?: string;
  scheme: string;
  host: string;
  port?: number | null;
  path_prefix?: string;
};

export type WorkflowSecretApprovalStatus = {
  workflow_id: string;
  status: "approved" | "approval_required" | "not_required";
  fingerprint: string;
  secrets: { field: string; secret_id: string }[];
  destinations: WorkflowSecretDestination[];
  dynamic_http_steps: string[];
  approved_destinations: WorkflowSecretDestination[];
};

function workflowPath(projectId: string, workflowId: string) {
  return `/api/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}`;
}

export async function fetchWorkflowSecretApproval(projectId: string, workflowId: string) {
  return fetchJson<WorkflowSecretApprovalStatus>(`${workflowPath(projectId, workflowId)}/secret-approval`);
}

/**
 * Approve the fingerprint on screen with the explicit exact-host rules a person
 * entered. A workflow that changed since it was shown is refused with 409.
 */
export async function approveWorkflowSecretUse(
  projectId: string,
  workflowId: string,
  fingerprint: string,
  destinations: WorkflowSecretDestination[],
) {
  return fetchJson<WorkflowSecretApprovalStatus>(`${workflowPath(projectId, workflowId)}/secret-approval`, {
    method: "POST",
    body: JSON.stringify({ fingerprint, destinations }),
  });
}

export async function bindWorkflowInputSecret(projectId: string, workflowId: string, field: string, secretId: string) {
  return fetchJson<WorkflowSecretApprovalStatus>(
    `${workflowPath(projectId, workflowId)}/secret-bindings/${encodeURIComponent(field)}`,
    { method: "PUT", body: JSON.stringify({ secret_id: secretId }) },
  );
}

export async function unbindWorkflowInputSecret(projectId: string, workflowId: string, field: string) {
  return fetchJson<WorkflowSecretApprovalStatus>(
    `${workflowPath(projectId, workflowId)}/secret-bindings/${encodeURIComponent(field)}`,
    { method: "DELETE" },
  );
}
