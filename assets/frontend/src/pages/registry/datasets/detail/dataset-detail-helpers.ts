import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { AssignmentSelectorRow } from "@/components/shared/assignment-selector";
import type { DatasetQueryTemplate, DatasetRecord } from "@/pages/registry/datasets/api";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export type DatasetEditDraft = {
  display_name: string;
  description: string;
  workspace_id: string;
  availability_mode: "all_workspace_projects" | "specific_projects_only";
  connection_id: string;
  tags_csv: string;
  read_only: boolean;
  enabled: boolean;
};

export type QueryDraft = {
  display_name: string;
  description: string;
  query_language: string;
  sql: string;
  params_json: string;
  output_json: string;
};

export type DatasetColumn = {
  data_type?: string;
  name: string;
  value_type?: string;
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

export type QueryPreviewState = {
  outputSchema: Record<string, unknown>;
  queryName: string;
  rowCount: number;
  rows: Array<Record<string, unknown>>;
  truncated: boolean;
};

export type QuerySchemaType = "string" | "integer" | "number" | "boolean";

export type QueryParameterField = {
  format: string;
  name: string;
  required: boolean;
  type: QuerySchemaType;
};

export type QueryOutputField = {
  format: string;
  name: string;
  resourceId: string;
  type: QuerySchemaType;
};

export const EMPTY_QUERY_DRAFT: QueryDraft = {
  display_name: "",
  description: "",
  query_language: "sql",
  sql: "",
  params_json: "{}",
  output_json: "{}",
};

function projectAssignmentBadge(
  workspaceWide: boolean,
  selected: boolean, t: I18nContextValue["t"] = fallbackContext.t
): NonNullable<AssignmentSelectorRow["badges"]>[number] {
  if (workspaceWide) {
    return { label: t("react.registry.workspace_wide_variant_175e8123", "workspace-wide"), tone: "info" };
  }
  return selected
    ? { label: t("react.workflows.linked_variant_2272bea6", "linked"), tone: "linked" }
    : { label: t("react.registry.available", "available"), tone: "available" };
}

export function stringifyJson(value: unknown): string {
  return JSON.stringify(value && typeof value === "object" ? value : {}, null, 2);
}

export function csvStrings(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(isTruthy);
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function objectValue(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

export function parseObjectJson(value: string, label: string, t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  const trimmed = value.trim();
  if (!trimmed) {
    return {};
  }
  const parsed = JSON.parse(trimmed) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(t("react.registry.value0_must_be_a_json_object", "{value0} must be a JSON object.", { value0: label }));
  }
  return parsed as Record<string, unknown>;
}

const QUERY_SCHEMA_TYPES = new Set<QuerySchemaType>([
  "string",
  "integer",
  "number",
  "boolean",
]);

function schemaType(value: unknown): QuerySchemaType {
  return typeof value === "string" && QUERY_SCHEMA_TYPES.has(value as QuerySchemaType)
    ? (value as QuerySchemaType)
    : "string";
}

function skipSqlLine(sql: string, index: number, markerWidth: number): number {
  const newline = sql.indexOf("\n", index + markerWidth);
  return newline < 0 ? sql.length : newline + 1;
}

function skipSqlBlockComment(sql: string, index: number): number {
  const end = sql.indexOf("*/", index + 2);
  return end < 0 ? sql.length : end + 2;
}

function skipSqlSingleQuotedValue(sql: string, index: number): number {
  let cursor = index + 1;
  while (cursor < sql.length) {
    if (sql[cursor] === "'") {
      return cursor + (sql[cursor + 1] === "'" ? 2 : 1);
    }
    cursor += sql[cursor] === "\\" && cursor + 1 < sql.length ? 2 : 1;
  }
  return cursor;
}

function skipSqlQuotedIdentifier(sql: string, index: number, quote: string): number {
  let cursor = index + 1;
  while (cursor < sql.length && sql[cursor] !== quote) {
    cursor += 1;
  }
  return cursor + 1;
}

function sqlParameterEnd(sql: string, index: number): number {
  let end = index + 2;
  while (end < sql.length && /\w/.test(sql[end])) {
    end += 1;
  }
  return end;
}

function sqlCommentEnd(
  sql: string,
  index: number,
  char: string,
  following: string,
): number | null {
  if (char === "-" && following === "-") {
    return skipSqlLine(sql, index, 2);
  }
  if (char === "/" && following === "*") {
    return skipSqlBlockComment(sql, index);
  }
  if (char === "#") {
    return skipSqlLine(sql, index, 1);
  }
  return null;
}

function parameterNamesFromSql(sql: string): string[] {
  const names: string[] = [];
  let index = 0;
  while (index < sql.length) {
    const char = sql[index];
    const following = sql[index + 1] || "";
    const commentEnd = sqlCommentEnd(sql, index, char, following);
    if (commentEnd !== null) {
      index = commentEnd;
      continue;
    }
    if (char === "'") {
      index = skipSqlSingleQuotedValue(sql, index);
      continue;
    }
    if (char === '"' || char === "`") {
      index = skipSqlQuotedIdentifier(sql, index, char);
      continue;
    }
    if (char === ":" && following !== ":" && /[A-Za-z_]/.test(following)) {
      const end = sqlParameterEnd(sql, index);
      names.push(sql.slice(index + 1, end));
      index = end;
      continue;
    }
    index += 1;
  }
  return names;
}

function schemaFieldFromColumn(column: DatasetColumn): Pick<QueryOutputField, "format" | "type"> {
  const valueType = String(column.value_type || "").toLowerCase();
  if (valueType === "integer") {
    return { type: "integer", format: "" };
  }
  if (valueType === "number") {
    return { type: "number", format: "" };
  }
  if (valueType === "boolean") {
    return { type: "boolean", format: "" };
  }
  if (valueType === "datetime") {
    const sourceType = String(column.data_type || "").toLowerCase();
    return { type: "string", format: sourceType && !sourceType.includes("time") ? "date" : "date-time" };
  }
  return { type: "string", format: "" };
}

export function queryParameterFields(
  sql: string,
  schema: Record<string, unknown>,
): QueryParameterField[] {
  const properties = objectValue(schema.properties);
  const required = new Set(
    Array.isArray(schema.required)
      ? schema.required.filter((name): name is string => typeof name === "string")
      : [],
  );
  const sqlParameters = new Set(parameterNamesFromSql(sql));
  const names = Array.from(new Set([...sqlParameters, ...Object.keys(properties)]));
  return names.map((name) => {
    const property = objectValue(properties[name]);
    return {
      name,
      type: schemaType(property.type),
      format: typeof property.format === "string" ? property.format : "",
      required: required.has(name) || (sqlParameters.has(name) && !Object.hasOwn(properties, name)),
    };
  });
}

export function paramsSchemaFromFields(fields: QueryParameterField[]): Record<string, unknown> {
  if (!fields.length) {
    return {};
  }
  const required = fields.filter((field) => field.required).map((field) => field.name);
  return {
    type: "object",
    properties: Object.fromEntries(
      fields.map((field) => [
        field.name,
        {
          type: field.type,
          ...(field.type === "string" && field.format ? { format: field.format } : {}),
        },
      ]),
    ),
    ...(required.length ? { required } : {}),
  };
}

export function paramsSchemaForSql(sql: string, schema: Record<string, unknown>): Record<string, unknown> {
  const properties = { ...objectValue(schema.properties) };
  const added = parameterNamesFromSql(sql).filter((name) => !Object.hasOwn(properties, name));
  if (!added.length) return schema;
  for (const name of added) properties[name] = { type: "string" };
  const required = Array.isArray(schema.required) ? schema.required : [];
  return {
    ...schema,
    type: schema.type ?? "object",
    properties,
    required: Array.from(new Set([...required, ...added])),
  };
}

export function queryOutputFields(resources: DatasetResource[]): QueryOutputField[] {
  const seenNames = new Set<string>();
  return resources.flatMap((resource) =>
    resource.columns.flatMap((column) => {
      if (seenNames.has(column.name)) {
        return [];
      }
      seenNames.add(column.name);
      return [{ resourceId: resource.resource_id, name: column.name, ...schemaFieldFromColumn(column) }];
    }),
  );
}

export function outputSchemaWithField(
  schema: Record<string, unknown>,
  field: QueryOutputField,
  selected: boolean,
): Record<string, unknown> {
  const properties = { ...objectValue(schema.properties) };
  if (selected && !Object.hasOwn(properties, field.name)) {
    properties[field.name] = {
      type: field.type,
      ...(field.type === "string" && field.format ? { format: field.format } : {}),
    };
  } else if (!selected) {
    delete properties[field.name];
  }
  return {
    ...schema,
    type: schema.type ?? "object",
    properties,
    ...(!selected && Array.isArray(schema.required)
      ? { required: schema.required.filter((name) => name !== field.name) }
      : {}),
  };
}

export function outputSchemaFromFields(fields: QueryOutputField[]): Record<string, unknown> {
  return fields.reduce<Record<string, unknown>>(
    (schema, field) => outputSchemaWithField(schema, field, true),
    {},
  );
}

export function outputSchemaWithFormat(
  schema: Record<string, unknown>,
  fieldName: string,
  format: string,
): Record<string, unknown> {
  const properties = { ...objectValue(schema.properties) };
  const property = objectValue(properties[fieldName]);
  if (!Object.keys(property).length) {
    return schema;
  }
  properties[fieldName] = {
    ...property,
    ...(format ? { format } : {}),
  };
  if (!format) {
    delete (properties[fieldName] as Record<string, unknown>).format;
  }
  return { ...schema, type: schema.type ?? "object", properties };
}

function previewParameterValue(schema: unknown): unknown {
  const property = objectValue(schema);
  if (property.default !== undefined) {
    return property.default;
  }
  switch (property.type) {
    case "boolean":
      return false;
    case "integer":
    case "number":
      return 0;
    case "array":
      return [];
    case "object":
      return {};
    default:
      return "example";
  }
}

export function previewParamsJsonForTemplate(template: DatasetQueryTemplate): string {
  const paramsSchema = objectValue(template.params_schema_jsonb);
  const properties = objectValue(paramsSchema.properties);
  const sql = firstStringFromUnknownValue([objectValue(template.query_definition_jsonb).sql, ""]);
  const sqlParameterNames = parameterNamesFromSql(sql);
  const parameterNames = Array.from(new Set([...Object.keys(properties), ...sqlParameterNames]));
  return JSON.stringify(
    Object.fromEntries(
      parameterNames.map((name) => [name, previewParameterValue(properties[name])]),
    ),
    null,
    2,
  );
}

export function normalizePreviewState(queryName: string, payload: unknown, t: I18nContextValue["t"] = fallbackContext.t): QueryPreviewState {
  const response = isRecord(payload) ? payload : {};
  const result = isRecord(response.result) ? response.result : {};
  const rows = Array.isArray(result.rows)
    ? result.rows.filter(isRecord).map((row) => ({ ...row }))
    : [];
  return {
    outputSchema: objectValue(result.output_schema),
    queryName: queryName.trim() || t("react.registry.display_saved_query_preview", "Saved query preview"),
    rowCount: Number(result.row_count || rows.length || 0),
    rows,
    truncated: Boolean(result.truncated),
  };
}

export function normalizeSchemaSnapshot(value: unknown): DatasetSchemaSnapshot {
  const payload = isRecord(value) ? value : {};
  const resources = Array.isArray(payload.resources) ? payload.resources : [];
  const normalizedResources = resources
    .filter(isRecord)
    .map((resource) => ({
      resource_id: firstStringFromUnknownValue([resource.resource_id, resource.name, ""]).trim(),
      resource_type: firstStringFromUnknownValue([resource.resource_type, resource.type, ""]).trim(),
      columns: Array.isArray(resource.columns)
        ? resource.columns
            .filter(isRecord)
            .map((column) => ({
              name: firstStringFromUnknownValue([column.name, ""]).trim(),
              data_type: firstStringFromUnknownValue([column.data_type, column.type, ""]).trim(),
              value_type: firstStringFromUnknownValue([column.value_type, ""]).trim(),
            }))
            .filter((column) => column.name)
        : [],
    }))
    .filter((resource) => resource.resource_id);
  const resourcesById = new Map<string, DatasetResource>();
  normalizedResources.forEach((resource) => {
    const existing = resourcesById.get(resource.resource_id);
    if (!existing) {
      resourcesById.set(resource.resource_id, resource);
      return;
    }
    const columnsByName = new Map(existing.columns.map((column) => [column.name, column]));
    resource.columns.forEach((column) => columnsByName.set(column.name, column));
    resourcesById.set(resource.resource_id, { ...existing, columns: [...columnsByName.values()] });
  });
  return {
    schema_version: firstStringFromUnknownValue([payload.schema_version, ""]),
    metadata: isRecord(payload.metadata) ? payload.metadata : {},
    resources: [...resourcesById.values()],
  };
}

export function connectionSchemaSnapshot(
  connection:
    | {
        metadata_jsonb?: Record<string, unknown>;
      }
    | undefined,
): DatasetSchemaSnapshot {
  const metadata = isRecord(connection?.metadata_jsonb) ? connection.metadata_jsonb : {};
  return normalizeSchemaSnapshot(metadata.last_schema_snapshot);
}

function connectorIdFromDataConnection(connection: {
  connector_ref_jsonb?: Record<string, unknown>;
}): string {
  const ref = connection.connector_ref_jsonb || {};
  return firstStringFromUnknownValue([ref.connector_id, ""]).trim();
}

export function resolveConnectorIdForConnection(
  connection: {
    connector_ref_jsonb?: Record<string, unknown>;
  },
): string {
  return connectorIdFromDataConnection(connection);
}

export function scopeSelectionFromDataset(
  dataset: DatasetRecord | undefined,
): Record<string, string[] | null> {
  const explicitScope = objectValue(dataset?.resource_scope_jsonb);
  const scopedResources = Array.isArray(explicitScope.resources) ? explicitScope.resources : [];
  if (scopedResources.length) {
    return Object.fromEntries(
      scopedResources
        .filter(isRecord)
        .map((resource) => [
          firstStringFromUnknownValue([resource.resource_id, ""]).trim(),
          Array.isArray(resource.columns)
            ? resource.columns.map((column) => firstStringFromUnknownValue([column, ""]).trim()).filter(isTruthy)
            : null,
        ])
        .filter(([resourceId]) => resourceId),
    );
  }

  const snapshot = normalizeSchemaSnapshot(dataset?.schema_snapshot_jsonb);
  return Object.fromEntries(
    snapshot.resources.map((resource) => [
      resource.resource_id,
      resource.columns.length ? resource.columns.map((column) => column.name) : null,
    ]),
  );
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

export function draftFromDataset(dataset?: DatasetRecord): DatasetEditDraft {
  const metadata = objectValue(dataset?.metadata_jsonb);
  return {
    display_name: dataset?.display_name || "",
    description: dataset?.description || "",
    workspace_id: dataset?.workspace_id || "",
    availability_mode:
      dataset?.availability_mode === "all_workspace_projects"
        ? "all_workspace_projects"
        : "specific_projects_only",
    connection_id: firstStringFromUnknownValue([metadata.connection_id, ""]),
    tags_csv: (dataset?.tags || []).join(", "),
    read_only: dataset?.read_only ?? true,
    enabled: dataset?.enabled ?? true,
  };
}

export function queryDraftFromTemplate(query: DatasetQueryTemplate): QueryDraft {
  return {
    display_name: query.display_name || "",
    description: query.description || "",
    query_language: query.query_language || "sql",
    sql: firstStringFromUnknownValue([query.query_definition_jsonb?.sql, ""]),
    params_json: stringifyJson(query.params_schema_jsonb),
    output_json: stringifyJson(query.output_schema_jsonb),
  };
}

export function buildProjectSelectorRows({
  availabilityMode,
  projectCatalog,
  selectedProjectIdSet,
}: {
  availabilityMode: DatasetEditDraft["availability_mode"];
  projectCatalog:
    | {
        available_projects?: Array<Record<string, unknown>>;
      }
    | undefined;
  selectedProjectIdSet: Set<string>;
}, t: I18nContextValue["t"] = fallbackContext.t): AssignmentSelectorRow[] {
  const projects = Array.isArray(projectCatalog?.available_projects)
    ? projectCatalog.available_projects
    : [];
  return projects.map((project) => {
    const projectId = firstStringFromUnknownValue([project.id, ""]).trim();
    const title = firstStringFromUnknownValue([project.name, project.display_name, projectId, "Project"]);
    const selected = availabilityMode === "all_workspace_projects" || selectedProjectIdSet.has(projectId);
    return {
      id: projectId,
      title,
      subtitle: projectId ? `#${projectId}` : "",
      badges: [
        projectAssignmentBadge(availabilityMode === "all_workspace_projects", selected, t),
      ],
      disabled: availabilityMode === "all_workspace_projects" || !projectId,
      selected,
      filterKeys: [
        "all",
        selected ? "linked" : "available",
        availabilityMode === "all_workspace_projects" ? "workspace-wide" : "",
      ].filter(isTruthy),
      searchText: `${title} ${projectId}`.toLowerCase(),
    };
  });
}

export function projectFilterItemsFromRows(projectSelectorRows: AssignmentSelectorRow[], t: I18nContextValue["t"] = fallbackContext.t) {
  return [
    { key: "all", label: t("users.filter.all_states", "All"), count: projectSelectorRows.length },
    { key: "linked", label: t("react.registry.linked", "Linked"), count: projectSelectorRows.filter((row) => row.selected).length },
    {
      key: "available",
      label: t("react.registry.available_variant_e6744473", "Available"),
      count: projectSelectorRows.filter((row) => !row.selected).length,
    },
    {
      key: "workspace-wide",
      label: t("react.registry.workspace_wide", "Workspace-wide"),
      count: projectSelectorRows.filter((row) => (row.filterKeys || []).includes("workspace-wide")).length,
    },
  ];
}
