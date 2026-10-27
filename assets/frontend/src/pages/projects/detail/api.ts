import { fetchJson } from "@/lib/http/client";

export type ProjectDetail = {
  id: string;
  name: string;
  workspace_id?: string;
  workspace_key?: string;
  workspace_name?: string;
  description?: string;
  operator_default_workflow_id?: string;
  data_connection_ids?: string[];
  assigned_data_connections?: Array<Record<string, unknown>>;
  available_data_connections?: Array<Record<string, unknown>>;
  default_agent_models?: Record<string, unknown>;
  available_providers?: Array<[string, string]>;
  available_models?: string[];
  providers_with_models?: Record<string, unknown>;
  agent_model_overrides?: Record<string, unknown>;
  [key: string]: unknown;
};

export type ProjectWorkflowPreviewStep = {
  step_id: string;
  step_kind?: string;
  display_name?: string;
  agent?: string | null;
  skill?: string | null;
  group_id?: string | null;
};

export type ProjectWorkflowPreview = {
  project_id: string;
  selected_workflow_id: string;
  selection_source?: string;
  step_count?: number;
  step_plan?: ProjectWorkflowPreviewStep[];
  [key: string]: unknown;
};

export type WorkflowContractField = {
  name: string;
  label?: string;
  type?: string;
  required?: boolean;
  default_configured?: boolean;
  description?: string;
  options?: string[];
};

export type WorkflowContractResponse = {
  workflow_id: string;
  display_name?: string;
  enabled?: boolean;
  interface?: {
    enabled?: boolean;
    trigger_channels?: string[];
    input_mode?: string;
    strict_inputs?: boolean;
    expected_inputs?: WorkflowContractField[];
    output_mode?: string;
    expected_outputs?: WorkflowContractField[];
    returned_files?: Array<Record<string, unknown>>;
    input_schema?: Record<string, unknown>;
    output_schema?: Record<string, unknown>;
  };
  definition_issues?: Array<Record<string, unknown>>;
};

export async function fetchProjectDetail(projectId: string) {
  return fetchJson<ProjectDetail>(`/api/projects/${encodeURIComponent(projectId)}`);
}

export async function fetchProjectWorkflowPreview(
  projectId: string,
  payload: { task_summary: string; workflow_id: string; mode?: string },
) {
  return fetchJson<ProjectWorkflowPreview>(
    `/api/projects/${encodeURIComponent(projectId)}/workflow-preview`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export async function fetchWorkflowContract(workflowId: string) {
  return fetchJson<WorkflowContractResponse>(
    `/api/workflows/${encodeURIComponent(workflowId)}/contract`,
  );
}

export type ProjectUpdateResult = {
  status?: string;
  project_id?: string;
  name?: string;
};

export type ProjectEditInput = {
  name: string;
  workspace_id: string;
  description?: string;
  data_connection_ids?: string[];
  project_rules?: string;
  default_provider?: string;
  default_model?: string;
  workflow_ids?: string[];
  operator_default_workflow_id?: string;
};

export async function updateProject(projectId: string, payload: ProjectEditInput) {
  return fetchJson<ProjectUpdateResult>(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}
