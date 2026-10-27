import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type WorkspaceRecord = {
  id: string;
  tenant_id: string;
  account_id: string;
  workspace_key: string;
  display_name: string;
  description: string;
  status: "active";
  deleted_at?: string;
  metadata_jsonb: Record<string, unknown>;
  projects_count: number;
  jobs_count: number;
  updated_at?: string;
};

export type WorkspacePayload = {
  id?: string;
  workspace_key?: string;
  display_name: string;
  description?: string;
  metadata_jsonb?: Record<string, unknown>;
};

export type WorkspaceKeyChangePreview = {
  workspace_id: string;
  current_workspace_key: string;
  requested_workspace_key: string;
  conflict: boolean;
  conflict_workspace_id: string;
  impact: { projects_count: number; jobs_count: number };
  confirmation_workspace_id: string;
};

export type WorkspaceDeleteProjectPlanItem = {
  project_id: string;
  label: string;
  status: "active" | "trashed";
  can_reassign: boolean;
  current_workspace_id: string;
  current_workspace_label: string;
};

export type WorkspaceDeleteTargetWorkspace = {
  workspace_id: string;
  label: string;
};

export type WorkspaceDeleteDatasetPlanItem = {
  dataset_id: string;
  label: string;
  status: "active" | "trashed";
};

export type WorkspaceDeleteDataConnectionPlanItem = {
  connection_id: string;
  label: string;
  status: "active" | "trashed";
  linked_dataset_count: number;
  allowed_actions: Array<"block" | "reassign" | "delete">;
};

export type WorkspaceDeletePlan = {
  status: string;
  action_type: "trash";
  workspace: {
    workspace_id: string;
    label: string;
    status: string;
  };
  projects: WorkspaceDeleteProjectPlanItem[];
  datasets: WorkspaceDeleteDatasetPlanItem[];
  data_connections: WorkspaceDeleteDataConnectionPlanItem[];
  target_workspaces: WorkspaceDeleteTargetWorkspace[];
  warnings: string[];
  validation_errors: string[];
  summary?: Record<string, unknown>;
};

export type WorkspaceDeleteProjectAction = {
  project_id: string;
  action: "trash" | "reassign";
  target_workspace_id?: string;
};

export type WorkspaceDeleteDataConnectionAction = {
  connection_id: string;
  action: "block" | "reassign" | "delete";
  target_workspace_id?: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function normalizeWorkspace(row: Record<string, unknown>): WorkspaceRecord {
  return {
    id: firstStringFromUnknownValue([row.id, ""]),
    tenant_id: firstStringFromUnknownValue([row.tenant_id, ""]),
    account_id: firstStringFromUnknownValue([row.account_id, ""]),
    workspace_key: firstStringFromUnknownValue([row.workspace_key, row.key, ""]),
    display_name: firstStringFromUnknownValue([row.display_name, row.name, row.workspace_key, "Workspace"]),
    description: firstStringFromUnknownValue([row.description, ""]),
    status: "active",
    deleted_at: row.deleted_at ? stringFromUnknownValue(row.deleted_at) : "",
    metadata_jsonb: isRecord(row.metadata_jsonb) ? row.metadata_jsonb : {},
    projects_count: Number(row.projects_count || row.project_count || 0),
    jobs_count: Number(row.jobs_count || row.job_count || 0),
    updated_at: row.updated_at ? stringFromUnknownValue(row.updated_at) : "",
  };
}

function workspacesFromPayload(payload: unknown): WorkspaceRecord[] {
  if (Array.isArray(payload)) {
    return payload.filter(isRecord).map(normalizeWorkspace);
  }
  if (isRecord(payload)) {
    const rows = Array.isArray(payload.workspaces) ? payload.workspaces : [];
    return rows.filter(isRecord).map(normalizeWorkspace);
  }
  return [];
}

export async function fetchWorkspaces(params?: {
  search?: string;
  status?: string;
  includeArchived?: boolean;
}) {
  const query = new URLSearchParams();
  if (params?.search) query.set("search", params.search);
  if (params?.status) query.set("status", params.status);
  if (params?.includeArchived) query.set("include_archived", "true");
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return workspacesFromPayload(await fetchJson<unknown>(`/api/workspaces${suffix}`));
}

export async function fetchWorkspace(workspaceId: string) {
  const payload = await fetchJson<unknown>(`/api/workspaces/${encodeURIComponent(workspaceId)}`);
  return normalizeWorkspace(isRecord(payload) ? payload : {});
}

export async function createWorkspace(payload: WorkspacePayload) {
  return normalizeWorkspace(
    await fetchJson<Record<string, unknown>>("/api/workspaces", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

export async function updateWorkspace(workspaceId: string, payload: WorkspacePayload) {
  return normalizeWorkspace(
    await fetchJson<Record<string, unknown>>(`/api/workspaces/${encodeURIComponent(workspaceId)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

export async function previewWorkspaceKeyChange(workspaceId: string, workspaceKey: string) {
  return fetchJson<WorkspaceKeyChangePreview>(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/key-change/preview`,
    {
      method: "POST",
      body: JSON.stringify({ workspace_key: workspaceKey }),
    },
  );
}

export async function changeWorkspaceKey(
  workspaceId: string,
  workspaceKey: string,
  confirmWorkspaceId: string,
) {
  return normalizeWorkspace(
    await fetchJson<Record<string, unknown>>(
      `/api/workspaces/${encodeURIComponent(workspaceId)}/key-change`,
      {
        method: "POST",
        body: JSON.stringify({
          workspace_key: workspaceKey,
          confirm_workspace_id: confirmWorkspaceId,
        }),
      },
    ),
  );
}

export async function archiveWorkspace(workspaceId: string) {
  await fetchJson<Record<string, unknown>>(`/api/trash/workspace/${encodeURIComponent(workspaceId)}/trash`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  return fetchWorkspace(workspaceId);
}

export async function fetchWorkspaceDeletePlan(workspaceId: string) {
  return fetchJson<WorkspaceDeletePlan>(
    `/api/trash/workspace/${encodeURIComponent(workspaceId)}/delete-plan`,
    {
      method: "POST",
      body: JSON.stringify({}),
    },
  );
}

export async function executeWorkspaceDeletePlan(
  workspaceId: string,
  projectActions: WorkspaceDeleteProjectAction[],
  connectionActions: WorkspaceDeleteDataConnectionAction[],
) {
  return fetchJson<Record<string, unknown>>(
    `/api/trash/workspace/${encodeURIComponent(workspaceId)}/delete-execute`,
    {
      method: "POST",
      body: JSON.stringify({
        project_actions: projectActions,
        connection_actions: connectionActions,
      }),
    },
  );
}

export async function restoreWorkspace(workspaceId: string) {
  return normalizeWorkspace(
    await fetchJson<Record<string, unknown>>(`/api/workspaces/${encodeURIComponent(workspaceId)}/restore`, {
      method: "PATCH",
    }),
  );
}

export async function fetchWorkspaceProjects(workspaceId: string) {
  const payload = await fetchJson<unknown>(`/api/workspaces/${encodeURIComponent(workspaceId)}/projects`);
  if (!isRecord(payload) || !Array.isArray(payload.projects)) return [];
  return payload.projects.filter(isRecord);
}
