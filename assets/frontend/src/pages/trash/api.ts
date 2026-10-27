import { fetchJson } from "@/lib/http/client";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type TrashItem = {
  entity_type: string;
  entity_id: string | number;
  label?: string;
  deleted_at?: string;
  deleted_by?: string;
  project_id?: string | null;
  tenant_id?: string | null;
  [key: string]: unknown;
};

export type TrashListResponse = {
  items: TrashItem[];
  total: number;
};

export type LifecyclePlanResponse = {
  action_type: string;
  plan_version: string;
  root_entity: { entity_type: string; entity_id: string; label: string };
  tree: Record<string, unknown>;
  impact_summary?: Record<string, unknown>;
  warnings?: string[];
  validation_errors?: string[];
};

export async function fetchTrashList(params: {
  entityType?: string;
  projectId?: string;
  limit?: number;
} = {}) {
  const search = new URLSearchParams();
  if (params.entityType) {
    search.set("entity_type", stringFromUnknownValue(params.entityType));
  }
  if (params.projectId) {
    search.set("project_id", stringFromUnknownValue(params.projectId));
  }
  if (params.limit) {
    search.set("limit", stringFromUnknownValue(params.limit));
  }
  const query = search.toString();
  const querySuffix = query ? `?${query}` : "";
  return fetchJson<TrashListResponse>(`/api/trash${querySuffix}`);
}

function projectQuery(projectId?: string): string {
  const normalized = stringFromUnknownValue(projectId).trim();
  return normalized ? `?project_id=${encodeURIComponent(normalized)}` : "";
}

export async function restoreTrashItem(
  entityType: string,
  entityId: string | number,
  projectId?: string,
) {
  return fetchJson<unknown>(
    `/api/trash/${encodeURIComponent(stringFromUnknownValue(entityType))}/${encodeURIComponent(stringFromUnknownValue(entityId))}/restore${projectQuery(projectId)}`,
    {
      method: "POST",
      body: JSON.stringify({}),
    },
  );
}

export async function hardDeleteTrashItem(
  entityType: string,
  entityId: string | number,
  projectId?: string,
) {
  return fetchJson<unknown>(
    `/api/trash/${encodeURIComponent(stringFromUnknownValue(entityType))}/${encodeURIComponent(stringFromUnknownValue(entityId))}/hard-delete${projectQuery(projectId)}`,
    {
      method: "POST",
      body: JSON.stringify({}),
    },
  );
}

export async function fetchLifecyclePlan(
  entityType: string,
  entityId: string | number,
  actionType: "restore" | "hard_delete",
  projectId?: string,
) {
  return fetchJson<LifecyclePlanResponse>(
    `/api/trash/${encodeURIComponent(stringFromUnknownValue(entityType))}/${encodeURIComponent(stringFromUnknownValue(entityId))}/plan${projectQuery(projectId)}`,
    {
      method: "POST",
      body: JSON.stringify({ action_type: actionType }),
    },
  );
}

export async function executeLifecyclePlan(
  entityType: string,
  entityId: string | number,
  actionType: "restore" | "hard_delete",
  selectedNodeIds: string[],
  planVersion: string,
  projectId?: string,
) {
  return fetchJson<unknown>(
    `/api/trash/${encodeURIComponent(stringFromUnknownValue(entityType))}/${encodeURIComponent(stringFromUnknownValue(entityId))}/execute${projectQuery(projectId)}`,
    {
      method: "POST",
      body: JSON.stringify({
        action_type: actionType,
        selected_node_ids: selectedNodeIds,
        plan_version: planVersion,
      }),
    },
  );
}
