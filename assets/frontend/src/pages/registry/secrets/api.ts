import { fetchJson } from "@/lib/http/client";

/** Metadata of one catalog secret; the value is write-only and never returned. */
export type CatalogSecret = {
  id: string;
  name: string;
  description: string;
  usage_count?: number;
  created_at?: string | null;
  created_by?: string;
  value_updated_at?: string | null;
  value_updated_by?: string;
  metadata_updated_at?: string | null;
  metadata_updated_by?: string;
  usages?: CatalogSecretUsage[];
};

/** One consumer of a secret, named so a person can find it and remove its binding. */
export type CatalogSecretUsage = {
  /** "project_connection" or "workflow_input" today; a newer server may name another. */
  consumer_kind: string;
  connection_id: string | null;
  connection_name?: string;
  workflow_id?: string | null;
  workflow_name?: string;
  project_id?: string | null;
  project_name?: string;
  field: string;
  /** A data connection, MCP connector or LLM provider install that reads the secret. */
  consumer_id?: string | null;
  consumer_name?: string;
};

/**
 * The installation's secrets. Every project may use every secret, so Registry →
 * Secrets and each picker share this one list, and one refresh serves them all.
 */
export const CATALOG_SECRETS_QUERY_KEY = ["secrets", "all"] as const;

export async function fetchCatalogSecrets() {
  const payload = await fetchJson<{ secrets?: CatalogSecret[] }>("/api/secrets");
  return Array.isArray(payload?.secrets) ? payload.secrets : [];
}

/** What deleting a secret would unbind; `revision` names exactly this list. */
export type CatalogSecretDeletePlan = {
  secret: CatalogSecret;
  usages: CatalogSecretUsage[];
  revision: string;
};

export async function fetchCatalogSecretDeletePlan(secretId: string) {
  return fetchJson<CatalogSecretDeletePlan>(`/api/secrets/${encodeURIComponent(secretId)}/delete-plan`);
}

export async function fetchCatalogSecret(secretId: string) {
  return fetchJson<CatalogSecret>(`/api/secrets/${encodeURIComponent(secretId)}`);
}

export async function createCatalogSecret(payload: {
  name: string;
  description: string;
  value: string;
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

/** With the confirmed plan's revision a used secret is deleted together with its bindings. */
export async function deleteCatalogSecret(secretId: string, planRevision?: string) {
  const query = planRevision ? `?plan_revision=${encodeURIComponent(planRevision)}` : "";
  return fetchJson<{ deleted: boolean; removed_bindings?: number }>(
    `/api/secrets/${encodeURIComponent(secretId)}${query}`,
    { method: "DELETE" },
  );
}
