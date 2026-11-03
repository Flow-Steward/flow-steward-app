import { fetchJson } from "@/lib/http/client";

/** Metadata of one catalog secret; the value is write-only and never returned. */
export type CatalogSecret = {
  id: string;
  name: string;
  description: string;
  owner: "project" | "workspace";
  workspace_id: string;
  project_id: string | null;
  usage_count?: number;
  value_updated_at?: string | null;
  value_updated_by?: string;
  usages?: { consumer_kind: string; connection_id: string | null; field: string }[];
};

/** The project's catalog list, shared with Registry → Secrets so one refresh serves both. */
export function catalogSecretsQueryKey(projectId: string) {
  return ["secrets", "project", projectId];
}

export async function fetchCatalogSecrets(projectId: string) {
  const payload = await fetchJson<{ secrets?: CatalogSecret[] }>(
    `/api/secrets?project_id=${encodeURIComponent(projectId)}`,
  );
  return Array.isArray(payload?.secrets) ? payload.secrets : [];
}

export async function fetchCatalogSecret(secretId: string) {
  return fetchJson<CatalogSecret>(`/api/secrets/${encodeURIComponent(secretId)}`);
}

export async function createCatalogSecret(payload: {
  name: string;
  description: string;
  value: string;
  project_id?: string;
  workspace_id?: string;
}) {
  return fetchJson<CatalogSecret>("/api/secrets", { method: "POST", body: JSON.stringify(payload) });
}

export async function updateCatalogSecret(secretId: string, payload: { name: string; description: string }) {
  return fetchJson<CatalogSecret>(`/api/secrets/${encodeURIComponent(secretId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function replaceCatalogSecretValue(secretId: string, value: string) {
  return fetchJson<CatalogSecret>(`/api/secrets/${encodeURIComponent(secretId)}/value`, {
    method: "PUT",
    body: JSON.stringify({ value }),
  });
}

export async function deleteCatalogSecret(secretId: string) {
  return fetchJson<{ deleted: boolean }>(`/api/secrets/${encodeURIComponent(secretId)}`, { method: "DELETE" });
}
