import { fetchJson } from "@/lib/http/client";

export type WorkflowDependencyStatus =
  | "resolved"
  | "missing"
  | "conflict"
  | "requires_mapping"
  | "requires_extension_install"
  | "requires_project_connection"
  | "protected_reference"
  | "blocked";

export type WorkflowImportDependency = {
  dependency_key: string;
  dependency_type: string;
  ref: string;
  label?: string;
  status: WorkflowDependencyStatus;
  blocking?: boolean;
  required?: boolean;
  sources?: string[];
  resolution_actions?: string[];
  message?: string;
  metadata?: Record<string, unknown>;
};

export type WorkflowImportPreflight = {
  schema_version: string;
  kind: "workflow_import_preflight";
  ok: boolean;
  can_apply: boolean;
  summary: {
    workflow_count: number;
    reference_count: number;
    dependency_count: number;
    blocking_count: number;
  };
  workflows: Array<{
    workflow_id: string;
    display_name?: string;
    portability_kind?: string;
  }>;
  dependencies: WorkflowImportDependency[];
  required_resolution_actions: string[];
  topology_issues?: WorkflowImportTopologyIssue[];
};

/** A finding about the shape of the workflow graph, which resolutions cannot fix. */
export type WorkflowImportTopologyIssue = {
  code: string;
  message: string;
  path?: string;
  severity?: string;
  workflow_id?: string;
  blocking: boolean;
};

export type WorkflowImportResolution = {
  dependency_key: string;
  dependency_type: string;
  ref: string;
  action: string;
  target_ref?: string;
  target_workflow_id?: string;
  project_id?: string;
  extension_id?: string;
  connector_id?: string;
  extension_install_id?: string;
};

export type WorkflowImportResult = {
  status: "imported";
  workflow_id: string;
  imported_workflows: Array<{
    workflow_id: string;
    source_workflow_id: string;
    enabled: boolean;
    import_state: string;
  }>;
  preflight: WorkflowImportPreflight;
  /** The project the import was associated with, echoed back by the server. */
  project_id?: string;
};

export type WorkflowImportDependencyOption = {
  value: string;
  label: string;
  description?: string;
  enabled?: boolean;
  status?: string;
  metadata?: Record<string, unknown>;
};

export type WorkflowImportDependencyOptions = {
  kind: "workflow_import_dependency_options";
  options: Record<string, WorkflowImportDependencyOption[]>;
};

export async function fetchWorkflowImportOptions({
  projectId,
}: {
  projectId?: string;
} = {}) {
  const query = new URLSearchParams();
  if (projectId?.trim()) {
    query.set("project_id", projectId.trim());
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return fetchJson<WorkflowImportDependencyOptions>(`/api/workflows/import/options${suffix}`);
}

export async function preflightWorkflowImport({
  bundleYaml,
  targetWorkflowId,
  resolutions = [],
}: {
  bundleYaml: string;
  targetWorkflowId?: string;
  resolutions?: WorkflowImportResolution[];
}) {
  return fetchJson<WorkflowImportPreflight>("/api/workflows/import/preflight", {
    method: "POST",
    body: JSON.stringify({
      bundle_yaml: bundleYaml,
      target_workflow_id: targetWorkflowId || "",
      resolutions,
    }),
  });
}

export async function applyWorkflowImport({
  bundleYaml,
  targetWorkflowId,
  projectId,
  resolutions = [],
}: {
  bundleYaml: string;
  targetWorkflowId?: string;
  projectId?: string;
  resolutions?: WorkflowImportResolution[];
}) {
  return fetchJson<WorkflowImportResult>("/api/workflows/import/apply", {
    method: "POST",
    body: JSON.stringify({
      bundle_yaml: bundleYaml,
      target_workflow_id: targetWorkflowId || "",
      // Omitted rather than sent empty, so a caller that never picks a project
      // keeps the request scope's own project on the server side.
      ...(projectId ? { project_id: projectId } : {}),
      resolutions,
    }),
  });
}
