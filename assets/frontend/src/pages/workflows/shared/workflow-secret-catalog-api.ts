/**
 * Catalog secrets in a workflow's secret inputs (ADR 0057).
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

/** What one connector step's input goes to, in the project the status is for. */
export type WorkflowSecretReceiver = {
  project_id?: string;
  step_id: string;
  /** "extension", "tool_extension", "host_tool", "mcp" or "unpinned" today; a newer server may name another. */
  kind: string;
  tool_id?: string;
  install_id?: string;
  release_id?: string;
  descriptor_sha256?: string;
  connector_id?: string;
  endpoint?: string;
  transport?: string;
  remote_tool?: string;
  /** A built-in tool's implementation module. */
  implementation?: string;
  manifest_sha256?: string;
};

export type WorkflowSecretApprovalStatus = {
  workflow_id: string;
  /** Secret inputs the saved workflow declares. */
  secret_inputs?: string[];
  status: "approved" | "approval_required" | "delegation_optional" | "not_required";
  fingerprint: string;
  secrets: { field: string; secret_id: string }[];
  destinations: WorkflowSecretDestination[];
  dynamic_http_steps: string[];
  /** Who each connector step sends its input to: a pinned extension release, or nothing pinned. */
  receivers?: WorkflowSecretReceiver[];
  approved_destinations: WorkflowSecretDestination[];
};

/** One query per project, workflow and revision, shared by the builder and the project page. */
export function workflowSecretApprovalQueryKey(projectId: string, workflowId: string, revision?: number | null) {
  return ["workflows", workflowId, "secret-approval", projectId, revision ?? null];
}

/** A linked workflow of a project that declares a secret input, as an operator may see it. */
export type ProjectWorkflowSecretStatus = WorkflowSecretApprovalStatus & { display_name: string };

export function projectWorkflowSecretsQueryKey(projectId: string) {
  return ["workflows", "project-secret-approvals", projectId];
}

/** Every linked workflow with a secret input, readable from viewer up (no admin settings). */
export async function fetchProjectWorkflowSecretApprovals(projectId: string) {
  const payload = await fetchJson<{ workflows?: ProjectWorkflowSecretStatus[] }>(
    `/api/projects/${encodeURIComponent(projectId)}/workflow-secret-approvals`,
  );
  return Array.isArray(payload?.workflows) ? payload.workflows : [];
}

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

/** A value typed into a secret input: the server keeps it as a catalog secret bound to the input. */
export async function saveWorkflowInputSecretValue(projectId: string, workflowId: string, field: string, value: string) {
  return fetchJson<WorkflowSecretApprovalStatus>(
    `${workflowPath(projectId, workflowId)}/secret-bindings/${encodeURIComponent(field)}/value`,
    { method: "PUT", body: JSON.stringify({ value }) },
  );
}

export async function unbindWorkflowInputSecret(projectId: string, workflowId: string, field: string) {
  return fetchJson<WorkflowSecretApprovalStatus>(
    `${workflowPath(projectId, workflowId)}/secret-bindings/${encodeURIComponent(field)}`,
    { method: "DELETE" },
  );
}
