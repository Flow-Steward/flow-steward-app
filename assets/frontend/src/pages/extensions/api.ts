import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

type Entity = Record<string, unknown>;

export type ExtensionRecord = {
  extension_id: string;
  display_name?: string;
  description?: string;
  version?: string;
  active?: boolean;
  has_ui?: boolean;
  state?: string;
  author?: string;
  vendor_name?: string;
  category?: string;
  tags?: string[];
  support_url?: string;
  website_url?: string;
  broken?: boolean;
  broken_reason?: string;
  /** The install record exists but its files do not: only Uninstall applies. */
  files_missing?: boolean;
  /** The bundle's icon.png: whether it has one, and the URL Flow Steward serves it from. */
  has_icon?: boolean;
  icon_url?: string;
  [key: string]: unknown;
};

export type ExtensionInstallResult = {
  extension: ExtensionRecord | null;
  installed_path?: string;
};

export type ExtensionArchiveLifecycleAvailability = {
  available: boolean;
  reason_code: string;
  message: string;
  remediation: {
    install_update: string[];
    uninstall: string[];
  };
};

export type ExtensionHealthRecord = {
  extension_id: string;
  status: string;
  latency_ms?: number | null;
  [key: string]: unknown;
};

export type LlmProviderModel = {
  id: string;
  model_id: string;
  display_label?: string;
  enabled?: boolean;
  verification_status?: string;
  verification_error_safe?: string;
  lifecycle?: string;
  /** Required capabilities the provider reports this model lacks (before verification). */
  missing_capabilities?: string[];
  [key: string]: unknown;
};

export type LlmProviderSource = {
  id: string;
  extension_install_id: string;
  display_label?: string;
  provider_key?: string;
  has_credentials?: boolean;
  models?: LlmProviderModel[];
  [key: string]: unknown;
};

export type ExtensionUninstallPlanRow = {
  kind: string;
  id: string;
  /** Plain-language name of what the row is ("Project connection"). */
  label?: string;
  /** The thing's own name; never an id. */
  display_name?: string;
  /** The project the row belongs to, when it belongs to one. */
  project_name?: string;
  count?: number;
  reason?: string;
  /** Plain-language form of `reason`, naming the extension. */
  explanation?: string;
  [key: string]: unknown;
};

export type ExtensionUninstallPlan = {
  extension_id: string;
  extension_name?: string;
  extension_version?: string;
  install_id: string;
  release_id: string;
  revision: string;
  remove: ExtensionUninstallPlanRow[];
  affected: ExtensionUninstallPlanRow[];
  retained?: ExtensionUninstallPlanRow[];
};

export type ExtensionCallbackRegistration = {
  id: string;
  extension_id: string;
  callback_id: string;
  project_id?: string;
  connection_id?: string | null;
  enabled?: boolean;
  auth_type?: string;
  callback_url?: string;
  callback_token?: string;
  callback_secret?: string;
  configuration?: Record<string, unknown>;
  webhook_status?: Record<string, unknown>;
  last_seen_at?: string | null;
  last_error?: string;
  [key: string]: unknown;
};

function asEntity(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

function metadataText(value: unknown, fallbacks: string[] = []): string {
  if (typeof value === "string") {
    return value.trim();
  }
  const entity = asEntity(value);
  for (const key of fallbacks) {
    const candidate = entity[key];
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
  }
  return "";
}

export function normalizeExtensionRecord(value: unknown): ExtensionRecord | null {
  const entity = asEntity(value);
  const compiledManifest = asEntity(entity.compiled_manifest);
  const manifest = asEntity(entity.manifest);
  const vendor = asEntity(entity.vendor);
  const manifestVendor = asEntity(manifest.vendor);
  const compiledVendor = asEntity(compiledManifest.vendor);
  const extensionId = firstStringFromUnknownValue([entity.extension_id, entity.extension_key, entity.id, ""]).trim();
  if (!extensionId) {
    return null;
  }
  return {
    ...entity,
    extension_id: extensionId,
    display_name: firstStringFromUnknownValue([entity.display_name, entity.name, extensionId]),
    description: firstStringFromUnknownValue([entity.description, ""]),
    version: firstStringFromUnknownValue([entity.version, entity.latest_version, compiledManifest.version, ""]),
    active: firstStringFromUnknownValue([entity.state, ""]).trim().toLowerCase() === "active",
    has_ui: Boolean(entity.has_ui),
    state: firstStringFromUnknownValue([entity.state, "disabled"]),
    author:
      metadataText(entity.author, ["name"])
      || metadataText(compiledManifest.author, ["name"])
      || metadataText(manifest.author, ["name"]),
    vendor_name:
      metadataText(entity.vendor_name)
      || metadataText(compiledManifest.vendor_name)
      || metadataText(manifest.vendor_name)
      || metadataText(vendor, ["vendor_name", "name"])
      || metadataText(compiledVendor, ["vendor_name", "name"])
      || metadataText(manifestVendor, ["vendor_name", "name"]),
    category: firstStringFromUnknownValue([
      entity.category,
      asEntity(entity.listing).category,
      asEntity(compiledManifest.listing).category,
      asEntity(manifest.listing).category,
      "",
    ]),
    tags: [
      ...new Set(
        [entity.tags, asEntity(entity.listing).tags, asEntity(compiledManifest.listing).tags]
          .flatMap((entry) => Array.isArray(entry) ? entry : [])
          .map((entry) => String(entry || "").trim())
          .filter(Boolean),
      ),
    ],
    support_url:
      metadataText(entity.support_url)
      || metadataText(compiledManifest.support_url)
      || metadataText(manifest.support_url)
      || metadataText(vendor, ["support_url"])
      || metadataText(compiledVendor, ["support_url"])
      || metadataText(manifestVendor, ["support_url"]),
    website_url:
      metadataText(entity.website_url)
      || metadataText(compiledManifest.website_url)
      || metadataText(manifest.website_url)
      || metadataText(vendor, ["website_url", "url"])
      || metadataText(compiledVendor, ["website_url", "url"])
      || metadataText(manifestVendor, ["website_url", "url"]),
    broken: Boolean(entity.broken),
    broken_reason: firstStringFromUnknownValue([entity.broken_reason, ""]),
    files_missing: Boolean(entity.files_missing),
  };
}

export async function fetchExtensions(
  state: "active" | "disabled" | "all" = "active",
): Promise<ExtensionRecord[]> {
  const query = state === "all" ? "" : `?state=${encodeURIComponent(state)}`;
  const payload = await fetchJson<unknown>(`/api/extensions${query}`, { cache: "no-store" });
  if (Array.isArray(payload)) {
    return payload.map((item) => normalizeExtensionRecord(item)).filter(isTruthy);
  }
  const entity = asEntity(payload);
  const rows = Array.isArray(entity.extensions) ? entity.extensions : [];
  return rows
    .map((item) => normalizeExtensionRecord(item))
    .filter(isTruthy);
}

export async function fetchExtensionArchiveLifecycleAvailability(): Promise<ExtensionArchiveLifecycleAvailability> {
  const payload = asEntity(await fetchJson<unknown>(
    "/api/extensions/capabilities/archive-lifecycle",
    { cache: "no-store" },
  ));
  const remediation = asEntity(payload.remediation);
  return {
    available: payload.available === true,
    reason_code: firstStringFromUnknownValue([payload.reason_code, "unavailable"]),
    message: firstStringFromUnknownValue([payload.message, ""]),
    remediation: {
      install_update: Array.isArray(remediation.install_update)
        ? remediation.install_update
          .map((value) => firstStringFromUnknownValue([value, ""]))
          .filter(Boolean)
        : [],
      uninstall: Array.isArray(remediation.uninstall)
        ? remediation.uninstall
          .map((value) => firstStringFromUnknownValue([value, ""]))
          .filter(Boolean)
        : [],
    },
  };
}

export async function fetchExtension(extensionId: string): Promise<ExtensionRecord | null> {
  const payload = await fetchJson<unknown>(
    `/api/extensions/${encodeURIComponent(extensionId)}`,
    { cache: "no-store" },
  );
  return normalizeExtensionRecord(payload);
}

export async function setExtensionEnabled(
  extensionId: string,
  enabled: boolean,
  approvedBrowserAuthorityImpactHash = "",
) {
  const action = enabled ? "enable" : "disable";
  const body: Record<string, unknown> = { enabled };
  if (approvedBrowserAuthorityImpactHash) {
    body.approved_browser_authority_impact_hash = approvedBrowserAuthorityImpactHash;
  }
  return fetchJson<unknown>(`/api/extensions/${encodeURIComponent(extensionId)}/${action}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function installExtensionFromFile(
  file: File,
  approvedBrowserAuthorityImpactHash = "",
): Promise<ExtensionInstallResult> {
  const form = new FormData();
  form.set("file", file);
  if (approvedBrowserAuthorityImpactHash) {
    form.set("approved_browser_authority_impact_hash", approvedBrowserAuthorityImpactHash);
  }
  const payload = await fetchJson<unknown>("/api/extensions/install", {
    method: "POST",
    body: form,
  });
  const entity = asEntity(payload);
  return {
    extension: normalizeExtensionRecord(entity.extension),
    installed_path: firstStringFromUnknownValue([entity.installed_path, ""]),
  };
}

export async function fetchExtensionUninstallPlan(extensionId: string) {
  return fetchJson<ExtensionUninstallPlan>(
    `/api/extensions/${encodeURIComponent(extensionId)}/uninstall-plan`,
    { cache: "no-store" },
  );
}

export async function confirmExtensionUninstall(extensionId: string, revision: string) {
  return fetchJson<unknown>(`/api/extensions/${encodeURIComponent(extensionId)}/uninstall`, {
    method: "POST",
    body: JSON.stringify({ revision }),
  });
}

export async function checkExtensionHealth(extensionId: string): Promise<ExtensionHealthRecord> {
  const payload = await fetchJson<unknown>(
    `/api/extensions/${encodeURIComponent(extensionId)}/health`,
  );
  const entity = asEntity(payload);
  const nestedHealth = asEntity(entity.health);
  const status = firstStringFromUnknownValue([nestedHealth.status, nestedHealth.state, entity.status, entity.state, "unknown"]).trim() || "unknown";
  const nestedLatency = nestedHealth.latency_ms;
  const rootLatency = entity.latency_ms;
  return {
    ...nestedHealth,
    extension_id: firstStringFromUnknownValue([entity.extension_id, extensionId]),
    status,
    latency_ms: [nestedLatency, rootLatency].find(
      (value): value is number => typeof value === "number",
    ) ?? null,
  };
}

export async function fetchExtensionPreview(extensionId: string, projectId?: string) {
  const params = new URLSearchParams();
  const normalizedProjectId = firstStringFromUnknownValue([projectId, ""]).trim();
  if (normalizedProjectId) {
    params.set("project_id", normalizedProjectId);
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return fetchJson<unknown>(
    `/api/extensions/${encodeURIComponent(extensionId)}/preview${suffix}`,
  );
}

export async function fetchExtensionCompiledManifest(extensionId: string) {
  return fetchJson<unknown>(
    `/api/extensions/${encodeURIComponent(extensionId)}/compiled-manifest`,
    { cache: "no-store" },
  );
}

export async function fetchExtensionCallbacks(
  extensionId: string,
  projectId: string,
): Promise<ExtensionCallbackRegistration[]> {
  const params = new URLSearchParams();
  const normalizedProjectId = firstStringFromUnknownValue([projectId, ""]).trim();
  if (!normalizedProjectId) {
    throw new Error("project_id is required to list extension callbacks");
  }
  params.set("project_id", normalizedProjectId);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const payload = await fetchJson<unknown>(
    `/api/extensions/${encodeURIComponent(extensionId)}/callbacks${suffix}`,
    { cache: "no-store" },
  );
  const entity = asEntity(payload);
  const rows = Array.isArray(entity.callbacks) ? entity.callbacks : [];
  return rows
    .filter((row): row is Entity => Boolean(row) && typeof row === "object" && !Array.isArray(row))
    .map((row) => ({
      ...row,
      id: firstStringFromUnknownValue([row.id, ""]),
      extension_id: firstStringFromUnknownValue([row.extension_id, extensionId]),
      callback_id: firstStringFromUnknownValue([row.callback_id, ""]),
    }));
}

export async function createExtensionCallbackRegistration(
  extensionId: string,
  callbackId: string,
  payload: {
    project_id: string;
    connection_id?: string | null;
    enabled?: boolean;
    configure?: boolean;
    configure_action_id?: string;
  },
) {
  return fetchJson<ExtensionCallbackRegistration>(
    `/api/extensions/${encodeURIComponent(extensionId)}/callbacks/${encodeURIComponent(callbackId)}`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export async function refreshExtensionCallbackStatus(
  extensionId: string,
  registrationId: string,
  payload: { action_id?: string; runtime?: Record<string, unknown> },
) {
  return fetchJson<Record<string, unknown>>(
    `/api/extensions/${encodeURIComponent(extensionId)}/callbacks/${encodeURIComponent(registrationId)}/status`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export type ExtensionEffectResolutionRef = {
  command_id: string;
  effect_id: string;
  fenced_version: number;
  approval_id: "";
};
export type ExtensionExternalEffect = {
  effect_id: string;
  observed_version: number;
  operation_id: string;
  status: string;
  decision_allowed: boolean;
  retry_supported: boolean;
  retry_requires_original_request: true;
  resolution_ref: ExtensionEffectResolutionRef | null;
  retry_unavailable_reason: "retry_in_progress" | null;
};
export type ExtensionEffectDecision = {
  observed_version: number;
  action: "approve" | "reject";
  comment: string;
};
export type ExtensionEffectDecisionResult = {
  resolution_ref: ExtensionEffectResolutionRef;
  action: "approve" | "reject";
  observed_version: number;
  fenced_version: number;
};

export async function fetchExtensionExternalEffects(projectId: string, installId: string) {
  return fetchJson<{ effects: ExtensionExternalEffect[]; truncated: boolean }>(
    `/api/projects/${encodeURIComponent(projectId)}/extensions/${encodeURIComponent(installId)}/external-effects`,
  );
}

export async function resolveExtensionExternalEffect(projectId: string, installId: string, effectId: string, decision: ExtensionEffectDecision) {
  return fetchJson<ExtensionEffectDecisionResult>(
    `/api/projects/${encodeURIComponent(projectId)}/extensions/${encodeURIComponent(installId)}/external-effects/${encodeURIComponent(effectId)}/resolution`,
    { method: "POST", body: JSON.stringify(decision) },
  );
}

export async function executeExtensionAction(
  extensionId: string,
  actionId: string,
  payload: Record<string, unknown>,
  idempotencyKey: string = globalThis.crypto.randomUUID(),
) {
  return fetchJson<unknown>(
    `/api/extensions/${encodeURIComponent(extensionId)}/action/${encodeURIComponent(actionId)}`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload),
    },
  );
}

export async function executeExtensionQuery(
  extensionId: string,
  queryId: string,
  payload: Record<string, unknown>,
) {
  return fetchJson<unknown>(
    `/api/extensions/${encodeURIComponent(extensionId)}/query/${encodeURIComponent(queryId)}`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export async function fetchLlmProviderSources(): Promise<LlmProviderSource[]> {
  const payload = asEntity(await fetchJson<unknown>("/api/llm-provider-extensions", { cache: "no-store" }));
  return Array.isArray(payload.sources) ? payload.sources as LlmProviderSource[] : [];
}

export async function saveLlmProviderCredentials(
  installId: string,
  payload: { secrets: Record<string, string>; connection_config?: Record<string, string>; remote_base_url?: string },
) {
  // `verification_invalidated` is true only when the key or an effective
  // setting changed; re-saving the same settings keeps verified models.
  // `changed` is false when the save left the key, settings and endpoint as they were.
  return fetchJson<{ status?: string; changed?: boolean; verification_invalidated?: boolean }>(`/api/llm-provider-extensions/${encodeURIComponent(installId)}/credentials`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** Which provider secrets are saved (names only, never values) and the last check. */
export type LlmProviderConnectionStatus = {
  install_id: string;
  saved_secrets: { name: string; saved_at?: string | null }[];
  /** `status` is "ok" or a safe error category such as "authentication_error". */
  last_check: { status: string; checked_at?: string | null } | null;
};

export async function fetchLlmProviderStatus(installId: string) {
  return fetchJson<LlmProviderConnectionStatus>(
    `/api/llm-provider-extensions/${encodeURIComponent(installId)}/status`,
    { cache: "no-store" },
  );
}

export async function refreshLlmProviderModels(
  installId: string,
  connection_config?: Record<string, string>,
) {
  return fetchJson<unknown>(`/api/llm-provider-extensions/${encodeURIComponent(installId)}/refresh-models`, {
    method: "POST",
    body: JSON.stringify({ connection_config: connection_config || {} }),
  });
}

export async function setLlmProviderModelEnabled(modelId: string, enabled: boolean) {
  return fetchJson<unknown>(`/api/llm-provider-extensions/models/${encodeURIComponent(modelId)}/${enabled ? "enable" : "disable"}`, { method: "POST" });
}
