import { type DatasetPayload } from "@/pages/registry/datasets/api";

import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export type FormSubmitEvent = {
  currentTarget: HTMLFormElement;
  preventDefault: () => void;
};

export type DatasetDraft = {
  display_name: string;
  description: string;
  workspace_id: string;
  availability_mode: "all_workspace_projects" | "specific_projects_only";
  project_ids: string[];
  connection_id: string;
  tags_csv: string;
  read_only: boolean;
  enabled: boolean;
};

export type DatasetColumn = {
  name: string;
};

export type DatasetResource = {
  resource_id: string;
  resource_type?: string;
  columns: DatasetColumn[];
};

export type DatasetSchemaSnapshot = {
  schema_version?: string;
  resources: DatasetResource[];
  metadata?: Record<string, unknown>;
};

export const EMPTY_DRAFT: DatasetDraft = {
  display_name: "",
  description: "",
  workspace_id: "",
  availability_mode: "specific_projects_only",
  project_ids: [],
  connection_id: "",
  tags_csv: "",
  read_only: true,
  enabled: true,
};

export const CREATE_FORM_ID = "registry-dataset-create-form";

export function csvStrings(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(isTruthy);
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/** Keep connection refusals actionable without surfacing extension diagnostics. */
export function datasetIntrospectionFailure(response: unknown, fallback: string): string {
  if (!isRecord(response) || response.ok !== false || !Array.isArray(response.errors)) {
    return fallback;
  }
  const safeConnectionRefusal = response.errors.find((item) =>
    isRecord(item)
    && item.code === "connection_unavailable"
    && item.message === "The selected extension connection is unavailable.",
  );
  return isRecord(safeConnectionRefusal) ? String(safeConnectionRefusal.message) : fallback;
}

export function normalizeSchemaSnapshot(value: unknown): DatasetSchemaSnapshot {
  const payload = isRecord(value) ? value : {};
  const resources = Array.isArray(payload.resources) ? payload.resources : [];
  return {
    schema_version: firstStringFromUnknownValue([payload.schema_version, ""]),
    metadata: isRecord(payload.metadata) ? payload.metadata : {},
    resources: resources
      .filter(isRecord)
      .map((resource) => ({
        resource_id: firstStringFromUnknownValue([resource.resource_id, resource.name, ""]).trim(),
        resource_type: firstStringFromUnknownValue([resource.resource_type, resource.type, ""]).trim(),
        columns: Array.isArray(resource.columns)
          ? resource.columns
              .filter(isRecord)
              .map((column) => ({ name: firstStringFromUnknownValue([column.name, ""]).trim() }))
              .filter((column) => column.name)
          : [],
      }))
      .filter((resource) => resource.resource_id),
  };
}

export function connectionSchemaSnapshot(connection: {
  metadata_jsonb?: Record<string, unknown>;
} | undefined): DatasetSchemaSnapshot {
  const metadata = isRecord(connection?.metadata_jsonb) ? connection.metadata_jsonb : {};
  return normalizeSchemaSnapshot(metadata.last_schema_snapshot);
}

export function resourceScopeFromSelection(
  schemaSnapshot: DatasetSchemaSnapshot,
  selectedResources: Record<string, string[] | null>,
) {
  const knownResources = new Set(schemaSnapshot.resources.map((resource) => resource.resource_id));
  return {
    version: 1,
    resources: Object.entries(selectedResources)
      .filter(([resourceId]) => knownResources.has(resourceId))
      .map(([resource_id, columns]) => ({
        resource_id,
        columns: Array.isArray(columns) ? columns : null,
      })),
  };
}

export function draftToPayload(
  draft: DatasetDraft,
  resourceScopeJsonb: Record<string, unknown>,
): DatasetPayload {
  return {
    display_name: draft.display_name.trim(),
    workspace_id: draft.workspace_id.trim(),
    availability_mode: draft.availability_mode,
    description: draft.description.trim(),
    connection_id: draft.connection_id.trim(),
    read_only: draft.read_only,
    enabled: draft.enabled,
    schema_snapshot_jsonb: {},
    resource_scope_jsonb: resourceScopeJsonb,
    project_ids: draft.availability_mode === "specific_projects_only" ? draft.project_ids : [],
    tags: csvStrings(draft.tags_csv),
  };
}

export function stringValue(value: unknown): string {
  return firstStringFromUnknownValue([value, ""]).trim();
}

export function connectorIdFromDataConnection(connection: {
  connector_ref_jsonb?: Record<string, unknown>;
}): string {
  const ref = connection.connector_ref_jsonb || {};
  return firstStringFromUnknownValue([ref.connector_id, ""]).trim();
}

export function resolveConnectorIdForConnection(connection: {
  connector_ref_jsonb?: Record<string, unknown>;
}): string {
  return connectorIdFromDataConnection(connection);
}

export function registryLoadMessage(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) {
    return fallback;
  }
  const message = stringValue(error.message);
  return !message || message === "Not Found" ? fallback : message;
}
