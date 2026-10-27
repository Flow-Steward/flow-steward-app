import { fetchJson } from "@/lib/http/client";
import type {
  WorkflowImportPreflight,
  WorkflowImportResolution,
  WorkflowImportResult,
} from "@/pages/workflows/import/api";

/** One thing an operator must have before the template can run. */
export type WorkflowCatalogSetupRequirement = {
  kind: string;
  label: string;
  ref?: string;
  required?: boolean;
};

export type WorkflowCatalogItem = {
  item_id: string;
  version: string;
  name: string;
  summary: string;
  description?: string;
  categories: string[];
  tags: string[];
  integrations: string[];
  trigger_types: string[];
  setup_requirements: WorkflowCatalogSetupRequirement[];
  compatible_flow_steward?: { min?: string; max?: string };
  workflow_yaml_url: string;
  workflow_yaml_bytes: number;
  workflow_yaml_sha256: string;
  publication_status: string;
  verification_status: string;
  /** When the template first appeared in the catalog, and when this version did. */
  added_at?: string;
  updated_at?: string;
  /** Child workflows the template carries; they are created with it. */
  includes?: { item_id: string; name: string }[];
  /** The template file's bundle format, as the index names it ("" when it does not). */
  bundle_schema?: string;
  /** Whether this Core can import that format; Core decides, the UI only explains. */
  importable?: boolean;
};

export type WorkflowCatalogAvailability = {
  kind: "workflow_template_catalog_availability";
  configured: boolean;
  available: boolean;
  reason?: string;
  message?: string;
  catalog_revision?: string;
  published_at?: string;
  item_count: number;
  fetched_at?: string;
  age_seconds?: number;
  stale: boolean;
  last_refresh_error?: string;
};

export type WorkflowCatalogIndex = {
  kind: "workflow_template_catalog";
  schema_version: string;
  catalog_revision: string;
  published_at: string;
  availability: WorkflowCatalogAvailability;
  items: WorkflowCatalogItem[];
  /** Integration id -> the name to show; absent from catalogs published before it. */
  integration_labels?: Record<string, string>;
  /** Category id -> its name; the list is shared with the extension catalog. */
  category_labels?: Record<string, string>;
};

/** How the backend mapped each logical `extension_id` the template references. */
export type WorkflowCatalogExtensionResolution = {
  extension_id: string;
  outcome: "resolved" | "missing" | "ambiguous" | "incompatible";
  message: string;
  install_id?: string;
  installed_version?: string;
};

export type WorkflowCatalogPreflight = WorkflowImportPreflight & {
  catalog_item: {
    item_id: string;
    version: string;
    name: string;
    setup_requirements: WorkflowCatalogSetupRequirement[];
  };
  catalog_extension_resolutions: WorkflowCatalogExtensionResolution[];
};

export type WorkflowCatalogCreateResult = WorkflowImportResult & {
  catalog_item: { item_id: string; version: string };
  catalog_extension_resolutions: WorkflowCatalogExtensionResolution[];
};

/**
 * Load the whole cached index in one request. Search and facet filtering then
 * happen in the browser — there is no per-keystroke API call, and no template
 * YAML is fetched until one is selected.
 */
export async function fetchWorkflowCatalog() {
  return fetchJson<WorkflowCatalogIndex>("/api/workflows/catalog");
}

export async function fetchWorkflowCatalogAvailability() {
  return fetchJson<WorkflowCatalogAvailability>("/api/workflows/catalog/availability");
}

/**
 * The Flow Steward version this installation runs, which a template's
 * `compatible_flow_steward` range is checked against. The liveness probe is
 * where the build version is reported.
 */
export async function fetchInstallationVersion(): Promise<string> {
  const payload = await fetchJson<{ build_version?: string }>("/health/live");
  return typeof payload?.build_version === "string" ? payload.build_version : "";
}

type CatalogRequestBody = {
  targetWorkflowId?: string;
  projectId?: string;
  resolutions?: WorkflowImportResolution[];
};

function catalogRequestPayload({
  targetWorkflowId,
  projectId,
  resolutions = [],
}: CatalogRequestBody) {
  return JSON.stringify({
    target_workflow_id: targetWorkflowId || "",
    ...(projectId ? { project_id: projectId } : {}),
    resolutions,
  });
}

export async function preflightWorkflowCatalogItem(itemId: string, body: CatalogRequestBody = {}) {
  return fetchJson<WorkflowCatalogPreflight>(
    `/api/workflows/catalog/${encodeURIComponent(itemId)}/preflight`,
    { method: "POST", body: catalogRequestPayload(body) },
  );
}

export async function createWorkflowFromCatalogItem(
  itemId: string,
  body: CatalogRequestBody = {},
) {
  return fetchJson<WorkflowCatalogCreateResult>(
    `/api/workflows/catalog/${encodeURIComponent(itemId)}/create`,
    { method: "POST", body: catalogRequestPayload(body) },
  );
}
