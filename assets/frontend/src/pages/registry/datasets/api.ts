import { fetchJson, type FetchJsonOptions } from "@/lib/http/client";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export type DatasetMetadata = Record<string, unknown> & { connection_resolution?: "project_scope" };

export type DatasetRecord = {
  source_install_id?: string;
  source_release_id?: string;
  dataset_id: string;
  workspace_id: string;
  availability_mode: "all_workspace_projects" | "specific_projects_only";
  workspace_resolution_status?: string;
  display_name: string;
  description?: string;
  connector_kind: string;
  connector_id?: string;
  runtime_kind: string;
  query_language: string;
  read_only: boolean;
  enabled: boolean;
  schema_snapshot_jsonb?: Record<string, unknown>;
  resource_scope_jsonb?: Record<string, unknown>;
  connection_schema_jsonb?: Record<string, unknown>;
  metadata_jsonb?: DatasetMetadata;
  project_ids?: string[];
  tags?: string[];
};

export type DatasetPayload = {
  dataset_id?: string;
  display_name: string;
  workspace_id: string;
  availability_mode: "all_workspace_projects" | "specific_projects_only";
  description?: string;
  connection_id: string;
  read_only: boolean;
  enabled: boolean;
  schema_snapshot_jsonb?: Record<string, unknown>;
  resource_scope_jsonb?: Record<string, unknown>;
  metadata_jsonb?: Record<string, unknown>;
  project_ids?: string[];
  tags?: string[];
};

export type DatasetConnector = {
  connector_id: string;
  local_connector_id?: string;
  display_name: string;
  description?: string;
  connector_kind: string;
  runtime_kind: string;
  query_language: string;
  connection_schema?: Record<string, unknown>;
  secret_schema?: Record<string, unknown>;
  capabilities?: Record<string, unknown>;
  safety_profile?: Record<string, unknown>;
  preview_support?: boolean;
  schema_refresh_support?: boolean;
  runtime_adapter_ref?: string;
  source_install_id?: string;
  source_release_id?: string;
  extension_id?: string;
  connector_catalog?: Array<Record<string, unknown>>;
};

export type DataConnectionRecord = {
  connection_id: string;
  display_name: string;
  description?: string;
  workspace_id: string;
  workspace_display_name?: string;
  source_kind: string;
  environment: string;
  enabled: boolean;
  health_status: string;
  query_capable?: boolean;
  query_capability_issue?: string;
  last_checked_at?: string;
  last_schema_refresh_at?: string;
  linked_dataset_count: number;
  tags?: string[];
  connector_ref_jsonb?: Record<string, unknown>;
  connection_config_jsonb?: Record<string, unknown>;
  secret_refs_jsonb?: Record<string, unknown>;
  metadata_jsonb?: Record<string, unknown>;
};

export type DataConnectionPayload = {
  connection_id?: string;
  workspace_id: string;
  connector_id: string;
  display_name: string;
  description?: string;
  environment?: string;
  enabled?: boolean;
  tags?: string[];
  connection_config_jsonb?: Record<string, unknown>;
  secret_refs_jsonb?: Record<string, unknown>;
  metadata_jsonb?: Record<string, unknown>;
};

export type ProjectDatasetCatalog = {
  available_datasets: DatasetRecord[];
  dataset_ids: string[];
  explicit_dataset_ids?: string[];
  project_id: string;
  workspace_id?: string;
};

export type DatasetProjectCatalog = {
  available_projects: Array<Record<string, unknown>>;
  project_ids: string[];
  explicit_project_ids?: string[];
  dataset_id: string;
  workspace_id: string;
  availability_mode: "all_workspace_projects" | "specific_projects_only";
};

export type DatasetQueryTemplate = {
  source_install_id?: string;
  source_release_id?: string;
  query_template_id: string;
  dataset_id: string;
  display_name: string;
  description?: string;
  query_language: string;
  query_definition_jsonb?: Record<string, unknown>;
  params_schema_jsonb?: Record<string, unknown>;
  output_schema_jsonb?: Record<string, unknown>;
  validation_result_jsonb?: Record<string, unknown>;
  enabled: boolean;
  version: number;
  created_at?: string;
  updated_at?: string;
};

export type DatasetQueryTemplatePayload = Omit<DatasetQueryTemplate, "query_template_id" | "source_install_id" | "source_release_id"> & {
  query_template_id?: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function resourceScopeFromMetadata(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    return {};
  }
  return isRecord(value.resource_scope_jsonb) ? value.resource_scope_jsonb : {};
}

function normalizeDataset(row: Record<string, unknown>): DatasetRecord {
  return {
    source_install_id: firstStringFromUnknownValue([row.source_install_id, ""]).trim(),
    source_release_id: firstStringFromUnknownValue([row.source_release_id, ""]).trim(),
    dataset_id: firstStringFromUnknownValue([row.dataset_id, row.id, ""]),
    workspace_id: firstStringFromUnknownValue([row.workspace_id, ""]),
    availability_mode:
      row.availability_mode === "all_workspace_projects"
        ? "all_workspace_projects"
        : "specific_projects_only",
    workspace_resolution_status: firstStringFromUnknownValue([row.workspace_resolution_status, ""]),
    display_name: firstStringFromUnknownValue([row.display_name, row.name, row.dataset_id, "Dataset"]),
    description: firstStringFromUnknownValue([row.description, ""]),
    connector_kind: firstStringFromUnknownValue([row.connector_kind, "extension"]),
    connector_id: firstStringFromUnknownValue([row.connector_id, ""]),
    runtime_kind: firstStringFromUnknownValue([row.runtime_kind, "local_extension"]),
    query_language: firstStringFromUnknownValue([row.query_language, "sql"]),
    read_only: row.read_only === undefined ? true : Boolean(row.read_only),
    enabled: row.enabled === undefined ? true : Boolean(row.enabled),
    schema_snapshot_jsonb: isRecord(row.schema_snapshot_jsonb) ? row.schema_snapshot_jsonb : {},
    resource_scope_jsonb: isRecord(row.resource_scope_jsonb)
      ? row.resource_scope_jsonb
      : resourceScopeFromMetadata(row.metadata_jsonb),
    connection_schema_jsonb: isRecord(row.connection_schema_jsonb) ? row.connection_schema_jsonb : {},
    metadata_jsonb: isRecord(row.metadata_jsonb) ? row.metadata_jsonb : {},
    project_ids: Array.isArray(row.project_ids)
      ? row.project_ids.map((value) => firstStringFromUnknownValue([value, ""]).trim()).filter(isTruthy)
      : [],
    tags: Array.isArray(row.tags)
      ? row.tags.map((value) => firstStringFromUnknownValue([value, ""]).trim()).filter(isTruthy)
      : [],
  };
}

function normalizeConnector(row: Record<string, unknown>): DatasetConnector {
  return {
    connector_id: firstStringFromUnknownValue([row.connector_id, row.id, ""]),
    local_connector_id: firstStringFromUnknownValue([row.local_connector_id, row.connector_id, ""]),
    display_name: firstStringFromUnknownValue([row.display_name, row.name, row.connector_id, "Dataset connector"]),
    description: firstStringFromUnknownValue([row.description, ""]),
    connector_kind: firstStringFromUnknownValue([row.connector_kind, "extension"]),
    runtime_kind: firstStringFromUnknownValue([row.runtime_kind, "local_extension"]),
    query_language: firstStringFromUnknownValue([row.query_language, "sql"]),
    connection_schema: isRecord(row.connection_schema) ? row.connection_schema : {},
    secret_schema: isRecord(row.secret_schema) ? row.secret_schema : {},
    capabilities: isRecord(row.capabilities) ? row.capabilities : {},
    safety_profile: isRecord(row.safety_profile) ? row.safety_profile : {},
    preview_support: Boolean(row.preview_support),
    schema_refresh_support: Boolean(row.schema_refresh_support),
    runtime_adapter_ref: firstStringFromUnknownValue([row.runtime_adapter_ref, ""]),
    source_install_id: firstStringFromUnknownValue([row.source_install_id, ""]),
    source_release_id: firstStringFromUnknownValue([row.source_release_id, ""]),
    extension_id: firstStringFromUnknownValue([row.extension_id, ""]),
    connector_catalog: Array.isArray(row.connector_catalog)
      ? row.connector_catalog.filter(isRecord).map((item) => ({ ...item }))
      : [],
  };
}

function normalizeDataConnection(row: Record<string, unknown>): DataConnectionRecord {
  return {
    connection_id: firstStringFromUnknownValue([row.connection_id, row.id, ""]),
    display_name: firstStringFromUnknownValue([row.display_name, row.name, row.connection_id, "Data connection"]),
    description: firstStringFromUnknownValue([row.description, ""]),
    workspace_id: firstStringFromUnknownValue([row.workspace_id, ""]),
    workspace_display_name: firstStringFromUnknownValue([row.workspace_display_name, ""]),
    source_kind: firstStringFromUnknownValue([row.source_kind, ""]),
    environment: firstStringFromUnknownValue([row.environment, "dev"]),
    enabled: row.enabled === undefined ? true : Boolean(row.enabled),
    health_status: firstStringFromUnknownValue([row.health_status, "unknown"]),
    query_capable: Boolean(row.query_capable),
    query_capability_issue: firstStringFromUnknownValue([row.query_capability_issue, ""]),
    last_checked_at: firstStringFromUnknownValue([row.last_checked_at, ""]),
    last_schema_refresh_at: firstStringFromUnknownValue([row.last_schema_refresh_at, ""]),
    linked_dataset_count: Number(row.linked_dataset_count || row.linked_datasets_count || 0),
    tags: Array.isArray(row.tags)
      ? row.tags.map((value) => firstStringFromUnknownValue([value, ""]).trim()).filter(isTruthy)
      : [],
    connector_ref_jsonb: isRecord(row.connector_ref_jsonb) ? row.connector_ref_jsonb : {},
    connection_config_jsonb: isRecord(row.connection_config_jsonb) ? row.connection_config_jsonb : {},
    secret_refs_jsonb: isRecord(row.secret_refs_jsonb) ? row.secret_refs_jsonb : {},
    metadata_jsonb: isRecord(row.metadata_jsonb) ? row.metadata_jsonb : {},
  };
}

function datasetsFromPayload(payload: unknown): DatasetRecord[] {
  if (Array.isArray(payload)) {
    return payload.filter(isRecord).map(normalizeDataset);
  }
  if (isRecord(payload)) {
    const rows = Array.isArray(payload.datasets) ? payload.datasets : [];
    return rows.filter(isRecord).map(normalizeDataset);
  }
  return [];
}

function connectorsFromPayload(payload: unknown): DatasetConnector[] {
  if (Array.isArray(payload)) {
    return payload.filter(isRecord).map(normalizeConnector);
  }
  if (isRecord(payload)) {
    const rows = Array.isArray(payload.connectors) ? payload.connectors : [];
    return rows.filter(isRecord).map(normalizeConnector);
  }
  return [];
}

function dataConnectionsFromPayload(payload: unknown): DataConnectionRecord[] {
  if (Array.isArray(payload)) {
    return payload.filter(isRecord).map(normalizeDataConnection);
  }
  if (isRecord(payload)) {
    const rows = Array.isArray(payload.connections) ? payload.connections : [];
    return rows.filter(isRecord).map(normalizeDataConnection);
  }
  return [];
}

function dataConnectionFromPayload(payload: unknown): DataConnectionRecord {
  if (isRecord(payload)) {
    if (isRecord(payload.connection)) {
      return normalizeDataConnection(payload.connection);
    }
    return normalizeDataConnection(payload);
  }
  return normalizeDataConnection({});
}

function projectDatasetCatalogFromPayload(payload: unknown): ProjectDatasetCatalog {
  if (!isRecord(payload)) {
    return { available_datasets: [], dataset_ids: [], project_id: "" };
  }
  const available = Array.isArray(payload.available_datasets) ? payload.available_datasets : [];
  const datasetIds = Array.isArray(payload.dataset_ids) ? payload.dataset_ids : [];
  return {
    available_datasets: available.filter(isRecord).map(normalizeDataset),
    dataset_ids: datasetIds.map((value) => firstStringFromUnknownValue([value, ""]).trim()).filter(isTruthy),
    explicit_dataset_ids: Array.isArray(payload.explicit_dataset_ids)
      ? payload.explicit_dataset_ids.map((value) => firstStringFromUnknownValue([value, ""]).trim()).filter(isTruthy)
      : [],
    project_id: firstStringFromUnknownValue([payload.project_id, ""]).trim(),
    workspace_id: firstStringFromUnknownValue([payload.workspace_id, ""]).trim(),
  };
}

function datasetProjectCatalogFromPayload(payload: unknown): DatasetProjectCatalog {
  if (!isRecord(payload)) {
    return {
      available_projects: [],
      project_ids: [],
      explicit_project_ids: [],
      dataset_id: "",
      workspace_id: "",
      availability_mode: "specific_projects_only",
    };
  }
  return {
    available_projects: Array.isArray(payload.available_projects)
      ? payload.available_projects.filter(isRecord).map((item) => ({ ...item }))
      : [],
    project_ids: Array.isArray(payload.project_ids)
      ? payload.project_ids.map((value) => firstStringFromUnknownValue([value, ""]).trim()).filter(isTruthy)
      : [],
    explicit_project_ids: Array.isArray(payload.explicit_project_ids)
      ? payload.explicit_project_ids.map((value) => firstStringFromUnknownValue([value, ""]).trim()).filter(isTruthy)
      : [],
    dataset_id: firstStringFromUnknownValue([payload.dataset_id, ""]).trim(),
    workspace_id: firstStringFromUnknownValue([payload.workspace_id, ""]).trim(),
    availability_mode:
      payload.availability_mode === "all_workspace_projects"
        ? "all_workspace_projects"
        : "specific_projects_only",
  };
}

function normalizeQueryTemplate(row: Record<string, unknown>): DatasetQueryTemplate {
  return {
    source_install_id: firstStringFromUnknownValue([row.source_install_id, ""]).trim(),
    source_release_id: firstStringFromUnknownValue([row.source_release_id, ""]).trim(),
    query_template_id: firstStringFromUnknownValue([row.query_template_id, row.id, ""]),
    dataset_id: firstStringFromUnknownValue([row.dataset_id, ""]),
    display_name: firstStringFromUnknownValue([row.display_name, row.name, row.query_template_id, "Saved query"]),
    description: firstStringFromUnknownValue([row.description, ""]),
    query_language: firstStringFromUnknownValue([row.query_language, "sql"]),
    query_definition_jsonb: isRecord(row.query_definition_jsonb) ? row.query_definition_jsonb : {},
    params_schema_jsonb: isRecord(row.params_schema_jsonb) ? row.params_schema_jsonb : {},
    output_schema_jsonb: isRecord(row.output_schema_jsonb) ? row.output_schema_jsonb : {},
    validation_result_jsonb: isRecord(row.validation_result_jsonb) ? row.validation_result_jsonb : {},
    enabled: row.enabled === undefined ? true : Boolean(row.enabled),
    version: Number(row.version || 1),
    created_at: firstStringFromUnknownValue([row.created_at, ""]),
    updated_at: firstStringFromUnknownValue([row.updated_at, ""]),
  };
}

function queryTemplatesFromPayload(payload: unknown): DatasetQueryTemplate[] {
  if (Array.isArray(payload)) {
    return payload.filter(isRecord).map(normalizeQueryTemplate);
  }
  if (isRecord(payload)) {
    const rows = Array.isArray(payload.query_templates) ? payload.query_templates : [];
    return rows.filter(isRecord).map(normalizeQueryTemplate);
  }
  return [];
}

export async function fetchDatasets() {
  const payload = await fetchJson<unknown>("/api/datasets");
  return datasetsFromPayload(payload);
}

export async function fetchDatasetConnectors() {
  const payload = await fetchJson<unknown>("/api/datasets/connectors");
  return connectorsFromPayload(payload);
}

export async function fetchDataConnections(params?: { workspaceId?: string }) {
  const search = new URLSearchParams();
  if (params?.workspaceId) {
    search.set("workspace_id", params.workspaceId);
  }
  const suffix = search.toString() ? `?${search.toString()}` : "";
  const payload = await fetchJson<unknown>(`/api/datasets/data-connections${suffix}`);
  return dataConnectionsFromPayload(payload);
}

export async function createDataConnection(payload: DataConnectionPayload) {
  const response = await fetchJson<unknown>("/api/datasets/data-connections", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return dataConnectionFromPayload(response);
}

export async function toggleDataConnection(connectionId: string) {
  return fetchJson<unknown>(`/api/datasets/data-connections/${encodeURIComponent(connectionId)}/toggle`, {
    method: "PATCH",
    body: JSON.stringify({}),
  });
}

export async function deleteDataConnection(connectionId: string) {
  return fetchJson<unknown>(`/api/datasets/data-connections/${encodeURIComponent(connectionId)}`, {
    method: "DELETE",
  });
}

export async function testDataConnection(connectionId: string) {
  return fetchJson<unknown>(`/api/datasets/data-connections/${encodeURIComponent(connectionId)}/test`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function introspectDataConnection(connectionId: string) {
  return fetchJson<unknown>(
    `/api/datasets/data-connections/${encodeURIComponent(connectionId)}/introspect`,
    {
      method: "POST",
      body: JSON.stringify({}),
    },
  );
}

export async function fetchDataset(datasetId: string) {
  const payload = await fetchJson<unknown>(`/api/datasets/${encodeURIComponent(datasetId)}`);
  return normalizeDataset(isRecord(payload) ? payload : {});
}

export async function createDataset(payload: DatasetPayload) {
  return fetchJson<unknown>("/api/datasets", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateDataset(datasetId: string, payload: DatasetPayload) {
  return fetchJson<unknown>(`/api/datasets/${encodeURIComponent(datasetId)}`, {
    method: "PUT",
    body: JSON.stringify({ ...payload, dataset_id: datasetId }),
  });
}

export async function toggleDataset(datasetId: string) {
  return fetchJson<unknown>(`/api/datasets/${encodeURIComponent(datasetId)}/toggle`, {
    method: "PATCH",
    body: JSON.stringify({}),
  });
}

export async function deleteDataset(datasetId: string) {
  return fetchJson<unknown>(`/api/datasets/${encodeURIComponent(datasetId)}`, {
    method: "DELETE",
  });
}

export async function fetchProjectDatasetCatalog(projectId: string) {
  const payload = await fetchJson<unknown>(`/api/datasets/project/${encodeURIComponent(projectId)}`);
  return projectDatasetCatalogFromPayload(payload);
}

export async function updateProjectDatasets(projectId: string, datasetIds: string[]) {
  return fetchJson<unknown>(`/api/datasets/project/${encodeURIComponent(projectId)}`, {
    method: "PUT",
    body: JSON.stringify({ dataset_ids: datasetIds }),
  });
}

export async function fetchDatasetProjectCatalog(datasetId: string) {
  const payload = await fetchJson<unknown>(`/api/datasets/${encodeURIComponent(datasetId)}/projects`);
  return datasetProjectCatalogFromPayload(payload);
}

export async function updateDatasetProjects(datasetId: string, projectIds: string[]) {
  return fetchJson<unknown>(`/api/datasets/${encodeURIComponent(datasetId)}/projects`, {
    method: "PUT",
    body: JSON.stringify({ project_ids: projectIds }),
  });
}

export async function fetchDatasetQueryTemplates(params?: {
  datasetId?: string;
  projectId?: string;
}, options?: FetchJsonOptions) {
  const search = new URLSearchParams();
  if (params?.datasetId) {
    search.set("dataset_id", params.datasetId);
  }
  if (params?.projectId) {
    search.set("project_id", params.projectId);
  }
  const suffix = search.toString() ? `?${search.toString()}` : "";
  const payload = await fetchJson<unknown>(`/api/datasets/query-templates${suffix}`, {}, options);
  return queryTemplatesFromPayload(payload);
}

export async function createDatasetQueryTemplate(payload: DatasetQueryTemplatePayload) {
  return fetchJson<unknown>("/api/datasets/query-templates", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateDatasetQueryTemplate(
  queryTemplateId: string,
  payload: DatasetQueryTemplatePayload,
) {
  return fetchJson<unknown>(`/api/datasets/query-templates/${encodeURIComponent(queryTemplateId)}`, {
    method: "PUT",
    body: JSON.stringify({ ...payload, query_template_id: queryTemplateId }),
  });
}

export async function toggleDatasetQueryTemplate(queryTemplateId: string) {
  return fetchJson<unknown>(
    `/api/datasets/query-templates/${encodeURIComponent(queryTemplateId)}/toggle`,
    {
      method: "PATCH",
      body: JSON.stringify({}),
    },
  );
}

export async function deleteDatasetQueryTemplate(queryTemplateId: string) {
  return fetchJson<unknown>(`/api/datasets/query-templates/${encodeURIComponent(queryTemplateId)}`, {
    method: "DELETE",
  });
}

export async function validateDatasetQueryTemplate(queryTemplateId: string, params = {}, projectId?: string | null) {
  return fetchJson<unknown>(
    `/api/datasets/query-templates/${encodeURIComponent(queryTemplateId)}/validate`,
    {
      method: "POST",
      body: JSON.stringify({ params, ...(projectId ? { project_id: projectId } : {}) }),
    },
  );
}

export async function previewDatasetQueryTemplate(
  queryTemplateId: string,
  params = {},
  rowLimit = 100,
  projectId?: string | null,
) {
  return fetchJson<unknown>(
    `/api/datasets/query-templates/${encodeURIComponent(queryTemplateId)}/preview`,
    {
      method: "POST",
      body: JSON.stringify({ params, row_limit: rowLimit, ...(projectId ? { project_id: projectId } : {}) }),
    },
  );
}

export async function inferDatasetQueryTemplateOutput(queryTemplateId: string, params = {}) {
  return fetchJson<unknown>(
    `/api/datasets/query-templates/${encodeURIComponent(queryTemplateId)}/infer-output-schema`,
    {
      method: "POST",
      body: JSON.stringify({ params }),
    },
  );
}
