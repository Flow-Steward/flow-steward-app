import { fetchJson } from "@/lib/http/client";

/**
 * Where an account's secret values are kept (ADR 0066 amendment 2026-11-26).
 * Core keeps them encrypted in its own database; an add-on (a platform module)
 * may register an external secret storage. No answer here carries a value.
 */

/** A kind of external storage an installed add-on offers. */
export type SecretStorageKind = {
  kind: string;
  /** The plain-language name to show; never the kind id. */
  label: string;
  /** The add-on's form of the non-secret settings, in the resource_admin field vocabulary. */
  config_fields: unknown[];
  feature: string;
  /** "write" lets a value move there; "delete" and "rotate" are not used by this page. */
  capabilities: string[];
  /** Whether this account may set it up and put new values in it. */
  entitled: boolean;
};

/** One configured storage of the account. */
export type SecretStorageConfig = {
  id: string;
  backend: string;
  backend_label: string;
  name: string;
  config: Record<string, unknown>;
  enabled: boolean;
  /** False when the add-on that serves it is no longer installed. */
  installed: boolean;
};

/** A catalog secret whose value an external storage keeps. */
export type RemoteSecret = { secret_id: string; config_id: string; backend: string };

export type SecretStorageOverview = {
  backends: SecretStorageKind[];
  configs: SecretStorageConfig[];
  secrets: RemoteSecret[];
};

export type SecretStorageHealth = { config_id: string; healthy: boolean; code: string };

/** Under ["secrets"], so refreshing the catalog refreshes where its values are kept. */
export const SECRET_STORAGE_QUERY_KEY = ["secrets", "storage"] as const;

export async function fetchSecretStorage(): Promise<SecretStorageOverview> {
  const payload = await fetchJson<Partial<SecretStorageOverview>>("/api/secret-backends");
  return {
    backends: Array.isArray(payload?.backends) ? payload.backends : [],
    configs: Array.isArray(payload?.configs) ? payload.configs : [],
    secrets: Array.isArray(payload?.secrets) ? payload.secrets : [],
  };
}

export async function createSecretStorageConfig(payload: {
  backend: string;
  name: string;
  config: Record<string, unknown>;
  enabled: boolean;
}) {
  return fetchJson<SecretStorageConfig>("/api/secret-backends/configs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateSecretStorageConfig(
  configId: string,
  payload: { name?: string; config?: Record<string, unknown>; enabled?: boolean },
) {
  return fetchJson<SecretStorageConfig>(`/api/secret-backends/configs/${encodeURIComponent(configId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function checkSecretStorageConfig(configId: string) {
  return fetchJson<SecretStorageHealth>(`/api/secret-backends/configs/${encodeURIComponent(configId)}/health`);
}

/** The page asks one name in the storage; the add-on turns it into its own locator. */
function locatorFor(nameInStorage: string) {
  return { name: nameInStorage.trim() };
}

/** Make a secret that already exists in a storage a catalog secret; its value is checked, never copied. */
export async function adoptStoredSecret(payload: {
  configId: string;
  nameInStorage: string;
  name: string;
  description: string;
}) {
  return fetchJson<{ id: string; name: string }>("/api/secret-backends/adopt", {
    method: "POST",
    body: JSON.stringify({
      config_id: payload.configId,
      locator: locatorFor(payload.nameInStorage),
      name: payload.name,
      description: payload.description,
    }),
  });
}

/**
 * Write a local value to a storage. A secret that already exists under that name in
 * the storage is overwritten only with `replace` (the server answers 409 otherwise).
 */
export async function moveSecretToStorage(secretId: string, configId: string, nameInStorage: string, replace = false) {
  return fetchJson<{ secret_id: string }>(`/api/secret-backends/secrets/${encodeURIComponent(secretId)}/move`, {
    method: "POST",
    body: JSON.stringify({ config_id: configId, locator: locatorFor(nameInStorage), replace }),
  });
}

/** Copies the value back into this installation; the copy in the storage is not deleted. */
export async function moveSecretToLocal(secretId: string) {
  return fetchJson<{ secret_id: string }>(`/api/secret-backends/secrets/${encodeURIComponent(secretId)}/move-local`, {
    method: "POST",
  });
}

/** A storage a new value may go to: turned on, its add-on installed and this account entitled. */
export function usableStorages(overview: SecretStorageOverview | undefined): SecretStorageConfig[] {
  if (!overview) return [];
  const entitled = new Set(overview.backends.filter((backend) => backend.entitled).map((backend) => backend.kind));
  return overview.configs.filter((config) => config.enabled && config.installed && entitled.has(config.backend));
}

/** Usable storages that accept new values, so a local value can move there. */
export function writableStorages(overview: SecretStorageOverview | undefined): SecretStorageConfig[] {
  if (!overview) return [];
  const writable = new Set(
    overview.backends.filter((backend) => backend.capabilities.includes("write")).map((backend) => backend.kind),
  );
  return usableStorages(overview).filter((config) => writable.has(config.backend));
}
