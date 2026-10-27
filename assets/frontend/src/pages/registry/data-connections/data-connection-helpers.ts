import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export type ConnectorOption = {
  connector_id: string;
  display_name: string;
  label: string;
  local_connector_id: string;
  option_id: string;
  connection_schema: Record<string, unknown>;
  preset_config: Record<string, unknown>;
  secret_schema: Record<string, unknown>;
};

export type MessageTone = "success" | "error" | "info";

export function connectorOptionsFromCatalog(connector: Record<string, unknown>): ConnectorOption[] {
  const connectorId = firstStringFromUnknownValue([connector.connector_id, ""]).trim();
  const displayName = firstStringFromUnknownValue([
    connector.display_name,
    connectorId,
    "Connector",
  ]).trim();
  if (!connectorId) {
    return [];
  }
  const connectionSchema =
    connector.connection_schema && typeof connector.connection_schema === "object"
      ? (connector.connection_schema as Record<string, unknown>)
      : {};
  const secretSchema =
    connector.secret_schema && typeof connector.secret_schema === "object"
      ? (connector.secret_schema as Record<string, unknown>)
      : {};
  const rawCatalog = Array.isArray(connector.connector_catalog) ? connector.connector_catalog : [];
  const catalogOptions = rawCatalog
    .map((row) => {
      if (!row || typeof row !== "object" || Array.isArray(row)) {
        return null;
      }
      const item = row as Record<string, unknown>;
      const localConnectorId = firstStringFromUnknownValue([item.connector_type_id, ""]).trim();
      const label = firstStringFromUnknownValue([item.display_name, item.source_kind, ""]).trim();
      if (!localConnectorId || !label) {
        return null;
      }
      const presetConfig =
        item.preset_config && typeof item.preset_config === "object" && !Array.isArray(item.preset_config)
          ? (item.preset_config as Record<string, unknown>)
          : {};
      const variantConnectionSchema =
        item.connection_schema && typeof item.connection_schema === "object" && !Array.isArray(item.connection_schema)
          ? (item.connection_schema as Record<string, unknown>)
          : connectionSchema;
      const variantSecretSchema =
        item.secret_schema && typeof item.secret_schema === "object" && !Array.isArray(item.secret_schema)
          ? (item.secret_schema as Record<string, unknown>)
          : secretSchema;
      return {
        connector_id: connectorId,
        display_name: displayName,
        label,
        local_connector_id: localConnectorId,
        option_id: `${connectorId}::${localConnectorId}`,
        connection_schema: variantConnectionSchema,
        secret_schema: variantSecretSchema,
        preset_config: presetConfig,
      } satisfies ConnectorOption;
    })
    .filter(
      isTruthy,
    );
  if (catalogOptions.length) {
    return catalogOptions;
  }
  const localConnectorId = firstStringFromUnknownValue([
    connector.local_connector_id,
    connectorId,
  ]).trim();
  return [
    {
      connector_id: connectorId,
      display_name: displayName,
      label: displayName,
      local_connector_id: localConnectorId,
      option_id: `${connectorId}::${localConnectorId}`,
      connection_schema: connectionSchema,
      secret_schema: secretSchema,
      preset_config: {},
    },
  ];
}

export type ConnectionSchemaField = {
  default_value?: unknown;
  key: string;
  label: string;
  description: string;
  enum_values: string[];
  maximum?: number;
  minimum?: number;
  required: boolean;
  type: string;
};

export type ConnectionFormField = ConnectionSchemaField & {
  storage: "connection" | "secret";
};

export type ConnectionFormFieldGroups = {
  primary: ConnectionFormField[];
  advanced: ConnectionFormField[];
};

export type ConnectionFieldCluster = {
  legend: string;
  fields: ConnectionFormField[];
};

/**
 * The clusters the design system publishes for a Toolbox connection's secondary
 * settings — `forms/settings-section.md`: group by what the operator decides
 * together, never by data type. Keys a connector adds beyond these keep their
 * own group rather than being forced into one of them.
 */
function localizedADVANCED_CLUSTERS(t: I18nContextValue["t"] = fallbackContext.t): ReadonlyArray<{ legend: string; keys: readonly string[] }> {
  return [
  { legend: t("react.registry.runtime_environment", "Runtime environment"), keys: ["env", "role"] },
  { legend: t("react.registry.cloud_placement", "Cloud placement"), keys: ["project", "region", "instance", "cluster"] },
  { legend: t("settings.mcp.fields.transport", "Transport"), keys: ["protocol", "ip_type", "query_params"] },
  { legend: t("react.registry.sql_target", "SQL target"), keys: ["dialect", "schema", "account", "warehouse"] },
];
}

/** Canon clusters "past about five fields"; a shorter set stays unlabelled. */
const CLUSTER_THRESHOLD = 5;

export function clusterAdvancedFields(
  fields: ConnectionFormField[], t: I18nContextValue["t"] = fallbackContext.t
): ConnectionFieldCluster[] {
  if (!fields.length) {
    return [];
  }
  const taken = new Set<string>();
  const clusters: ConnectionFieldCluster[] = [];
  for (const { legend, keys } of localizedADVANCED_CLUSTERS(t)) {
    const clustered = keys.flatMap((key) => {
      const field = fields.find((item) => item.key === key);
      if (!field) {
        return [];
      }
      taken.add(field.key);
      return [field];
    });
    if (clustered.length) {
      clusters.push({ legend, fields: clustered });
    }
  }
  const rest = fields.filter((field) => !taken.has(field.key));
  if (!rest.length) {
    return clusters;
  }
  if (!clusters.length && rest.length <= CLUSTER_THRESHOLD) {
    return [{ legend: "", fields: rest }];
  }
  return [...clusters, { legend: t("react.registry.other_settings", "Other settings"), fields: rest }];
}

const PRIMARY_CONNECTION_KEYS = ["host", "port", "database", "user"] as const;

export function groupConnectionFormFields(
  connectionFields: ConnectionSchemaField[],
  secretFields: ConnectionSchemaField[],
): ConnectionFormFieldGroups {
  const primaryConnectionSourceFields = PRIMARY_CONNECTION_KEYS.flatMap((key) => {
    const field = connectionFields.find((connectionField) => connectionField.key === key);
    return field ? [field] : [];
  });
  const passwordField = secretFields.find((field) => field.key === "password");
  const primaryConnectionFields = new Set(primaryConnectionSourceFields);

  return {
    primary: [
      ...primaryConnectionSourceFields.map((field) => ({ ...field, storage: "connection" as const })),
      ...(passwordField ? [{ ...passwordField, storage: "secret" as const }] : []),
    ],
    advanced: [
      ...connectionFields
        .filter((field) => !primaryConnectionFields.has(field))
        .map((field) => ({ ...field, storage: "connection" as const })),
      ...secretFields
        .filter((field) => field !== passwordField)
        .map((field) => ({ ...field, storage: "secret" as const })),
    ],
  };
}

function requiredNames(schema: Record<string, unknown>): string[] {
  return Array.isArray(schema.required)
    ? schema.required.map((item) => String(item).trim()).filter(Boolean)
    : [];
}

/** Does this value satisfy the subset of assertions a discriminator uses? */
function conditionHolds(
  condition: Record<string, unknown>,
  values: Record<string, string>,
): boolean {
  for (const name of requiredNames(condition)) {
    if (!values[name]?.trim()) {
      return false;
    }
  }
  const properties =
    condition.properties && typeof condition.properties === "object"
      ? (condition.properties as Record<string, Record<string, unknown>>)
      : {};
  return Object.entries(properties).every(([key, assertion]) => {
    const current = values[key]?.trim() ?? "";
    if ("const" in assertion) {
      return current === String(assertion.const);
    }
    if (Array.isArray(assertion.enum)) {
      return assertion.enum.map(String).includes(current);
    }
    return true;
  });
}

/**
 * Names the schema requires given what the operator has entered so far.
 *
 * A connector's requirements depend on which source was chosen: PostgreSQL
 * needs a host, SQLite needs only a file. The schema says so with allOf +
 * if/then keyed on the discriminator, and the form has to read the same
 * declaration the API enforces - otherwise the button says yes and the API says
 * no, which is how a connection with no host got created.
 */
export function conditionallyRequiredNames(
  schema: Record<string, unknown>,
  values: Record<string, string>,
): Set<string> {
  const names = new Set(requiredNames(schema));
  const branches = Array.isArray(schema.allOf) ? schema.allOf : [];
  for (const branch of branches) {
    if (!branch || typeof branch !== "object") {
      continue;
    }
    const rule = branch as Record<string, unknown>;
    const condition = rule.if;
    const consequence = rule.then;
    if (!condition || typeof condition !== "object") {
      continue;
    }
    if (!conditionHolds(condition as Record<string, unknown>, values)) {
      continue;
    }
    if (consequence && typeof consequence === "object") {
      for (const name of requiredNames(consequence as Record<string, unknown>)) {
        names.add(name);
      }
    }
  }
  return names;
}

/**
 * Fixed sets a branch narrows a field to, given what the operator has chosen.
 *
 * Toolbox declares these per source rather than per connector: `dialect` only
 * means something for Spanner, `protocol` only for ClickHouse. The schema says
 * so with the same allOf + if/then it uses for conditional requirements, so
 * this reads the same declaration `conditionallyRequiredNames` does — a field
 * whose values live in a branch would otherwise render as free text and the
 * operator could not know the valid strings.
 */
export function conditionalEnumValues(
  schema: Record<string, unknown>,
  values: Record<string, string>,
): Map<string, string[]> {
  const narrowed = new Map<string, string[]>();
  const branches = Array.isArray(schema.allOf) ? schema.allOf : [];
  for (const branch of branches) {
    if (!branch || typeof branch !== "object") {
      continue;
    }
    const rule = branch as Record<string, unknown>;
    const condition = rule.if;
    const consequence = rule.then;
    if (!condition || typeof condition !== "object") {
      continue;
    }
    if (!conditionHolds(condition as Record<string, unknown>, values)) {
      continue;
    }
    if (!consequence || typeof consequence !== "object") {
      continue;
    }
    const properties = (consequence as Record<string, unknown>).properties;
    if (!properties || typeof properties !== "object") {
      continue;
    }
    for (const [key, property] of Object.entries(properties as Record<string, unknown>)) {
      if (!property || typeof property !== "object") {
        continue;
      }
      const branchEnum = (property as Record<string, unknown>).enum;
      if (Array.isArray(branchEnum)) {
        narrowed.set(key, branchEnum.map(String).filter(Boolean));
      }
    }
  }
  return narrowed;
}

export function schemaFields(
  schema: Record<string, unknown>,
  { omit = [], values = {} }: { omit?: string[]; values?: Record<string, string> } = {},
): ConnectionSchemaField[] {
  const properties =
    schema.properties && typeof schema.properties === "object"
      ? (schema.properties as Record<string, unknown>)
      : {};
  const omitted = new Set(omit);
  const required = conditionallyRequiredNames(schema, values);
  const narrowed = conditionalEnumValues(schema, values);
  return Object.entries(properties)
    .filter(([key, value]) => !omitted.has(key) && value && typeof value === "object")
    .map(([key, value]) => {
      const property = value as Record<string, unknown>;
      // A set declared on the property itself wins; a branch only narrows a
      // field the property left open.
      const enumValues = Array.isArray(property.enum)
        ? property.enum.map(String).filter(Boolean)
        : (narrowed.get(key) ?? []);
      return {
        default_value: property.default,
        key,
        label: firstStringFromUnknownValue([property.title, key]).trim(),
        description: firstStringFromUnknownValue([property.description, ""]).trim(),
        enum_values: enumValues,
        maximum: typeof property.maximum === "number" ? property.maximum : undefined,
        minimum: typeof property.minimum === "number" ? property.minimum : undefined,
        required: required.has(key),
        type: firstStringFromUnknownValue([property.type, "string"]).trim() || "string",
      };
    });
}

export function formatDate(value?: string): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString();
}

export function csvStrings(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(isTruthy);
}

function errorMessageFromValue(value: unknown): string {
  if (Array.isArray(value)) {
    for (const item of value) {
      const message = errorMessageFromValue(item);
      if (message) {
        return message;
      }
    }
    return "";
  }
  if (value && typeof value === "object") {
    const payload = value as Record<string, unknown>;
    return (
      errorMessageFromValue(payload.error) ||
      errorMessageFromValue(payload.detail) ||
      errorMessageFromValue(payload.message) ||
      errorMessageFromValue(payload.code)
    );
  }
  const message = firstStringFromUnknownValue([value, ""]).trim();
  if (!message) {
    return "";
  }
  if (message.startsWith("{") || message.startsWith("[")) {
    try {
      return errorMessageFromValue(JSON.parse(message)) || message;
    } catch {
      return message;
    }
  }
  return message;
}

export function mutationMessage(
  response: unknown,
  options: { success: string; failure: string },
): string {
  const { success, failure } = options;
  if (!response || typeof response !== "object") {
    return success;
  }
  const payload = response as Record<string, unknown>;
  if (payload.ok === false) {
    const detail = errorMessageFromValue(payload.errors);
    const exposesEndpoint = [
      /\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b/i,
      /(?:postgres(?:ql)?|mysql|redis|mongodb):\/\//i,
      /\b(?:at|host|hostname)\s+[a-z0-9.-]+(?::\d+)?/i,
    ].some((pattern) => pattern.test(detail));
    if (exposesEndpoint) {
      return failure;
    }
    return detail || failure;
  }
  return success;
}

export function mutationTone(response: unknown): MessageTone {
  if (!response || typeof response !== "object") {
    return "success";
  }
  return (response as Record<string, unknown>).ok === false ? "error" : "success";
}
