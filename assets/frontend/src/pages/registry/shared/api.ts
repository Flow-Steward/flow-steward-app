import { fetchJson, type FetchJsonOptions } from "@/lib/http/client";

export type RegistryEntity = Record<string, unknown>;

function listFromPayload(payload: unknown): RegistryEntity[] {
  if (Array.isArray(payload)) {
    return payload.filter((row): row is RegistryEntity => Boolean(row) && typeof row === "object");
  }
  if (payload && typeof payload === "object") {
    for (const value of Object.values(payload as Record<string, unknown>)) {
      if (Array.isArray(value)) {
        return value.filter((row): row is RegistryEntity => Boolean(row) && typeof row === "object");
      }
    }
  }
  return [];
}

export async function fetchRegistryList(endpoint: string, options?: FetchJsonOptions) {
  const payload = await fetchJson<unknown>(endpoint, {}, options);
  return listFromPayload(payload);
}

export async function fetchRegistryDetail(endpoint: string, id: string) {
  return fetchJson<RegistryEntity>(`${endpoint}/${encodeURIComponent(id)}`);
}

export async function createRegistryEntity(endpoint: string, payload: RegistryEntity) {
  return fetchJson<unknown>(endpoint, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateRegistryEntity(endpoint: string, id: string, payload: RegistryEntity) {
  return fetchJson<unknown>(`${endpoint}/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function patchRegistryEntity(endpoint: string, id: string, payload: RegistryEntity) {
  return fetchJson<unknown>(`${endpoint}/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function toggleRegistryEntity(endpoint: string, id: string) {
  return fetchJson<unknown>(`${endpoint}/${encodeURIComponent(id)}/toggle`, {
    method: "PATCH",
    body: JSON.stringify({}),
  });
}

export async function deleteRegistryEntity(endpoint: string, id: string) {
  return fetchJson<unknown>(`${endpoint}/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export type McpProjectedToolDeletePlan = {
  tool_id: string;
  connector_id: string;
  affected: {
    workflows: Array<{ workflow_id: string; display_name?: string }>;
  };
  warnings: string[];
  revision: string;
};

export async function fetchMcpProjectedToolDeletePlan(id: string) {
  return fetchJson<McpProjectedToolDeletePlan>(
    `/api/tools/${encodeURIComponent(id)}/delete-plan`,
    { cache: "no-store" },
  );
}

export async function deleteMcpProjectedToolConfirmed(id: string, revision: string) {
  return fetchJson<unknown>(`/api/tools/${encodeURIComponent(id)}/delete`, {
    method: "POST",
    body: JSON.stringify({ revision }),
  });
}
