import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { sourceDeclaration } from "@/pages/extensions/extension-locale";
import {
  stringFrom,
  listFromUnknown,
  stringListFromUnknown,
  objectOrEmpty,
  connectionSecretRefs,
  manifestActionOutcome,
} from "@/pages/extensions/extension-manifest-utils";
import { executeExtensionAction } from "@/pages/extensions/api";
import {
  saveProjectConnectionSecrets,
  updateProjectConnection,
  createProjectConnection,
  startProjectConnectionOAuth,
  deleteProjectConnection,
} from "@/pages/projects/connections/api";
import {
  manifestFieldPath,
  manifestFieldIsSecret,
  manifestFieldValue,
  connectionDataForValues,
  connectionDefaultsForValues,
  manifestFieldVisibleForValues,
  assertPreSaveResultMappingTarget,
  preSaveActionValue,
  buildPreSaveActionPayload,
  validatePreSaveActionResult,
  type PreSaveConfirmationRequest,
  connectionPreSaveActions,
  preSaveActionContext,
  preSaveActionShouldRun,
} from "./connection-form-support";
import { allTrue, choose, anyTrue, catalogLabel } from "./hub-support";
import {
  RequiredFieldError,
  PreSaveActionError,
  extensionActionFailureDetail,
  manifestActionSucceeded,
  manifestActionResult,
} from "./manifest-action-support";

type ConnectionSavePreparation = {
  config: Record<string, unknown>;
  configFieldSet: Set<string>;
  data: Record<string, unknown>;
  existing: Record<string, unknown> | undefined;
  existingRefs: Record<string, string>;
  fields: Record<string, unknown>[];
  fieldValues: Record<string, unknown>;
  oauthConnection: boolean;
  secrets: Record<string, string>;
  secretFields: Set<string>;
  selectedId: string;
};

function configuredConnectionFields(
  configFields: string[],
  fields: Record<string, unknown>[],
  fieldByPath: Map<string, Record<string, unknown>>,
  secretFields: Set<string>,
): string[] {
  if (configFields.length) return configFields;
  return fields
    .map(manifestFieldPath)
    .filter((path) => {
      const field = fieldByPath.get(path);
      return allTrue([
        Boolean(path),
        !field || !manifestFieldIsSecret(field, secretFields),
        !["display_name", "environment"].includes(path),
      ]);
    });
}

function applyConnectionConfigValues({
  config,
  configuredFields,
  data,
  fields,
  fieldValues,
  secretFields,
}: {
  config: Record<string, unknown>;
  configuredFields: string[];
  data: Record<string, unknown>;
  fields: Record<string, unknown>[];
  fieldValues: Record<string, unknown>;
  secretFields: Set<string>;
}) {
  for (const path of configuredFields) {
    const raw = fieldValues[path] ?? choose(path === "provider", data.provider, undefined);
    const field = fields.find((item) => manifestFieldPath(item) === path);
    const value = choose(Boolean(field), manifestFieldValue(field ?? {}, { [path]: raw }), raw);
    if (value !== undefined) {
      config[path] = value;
    } else if (allTrue([Boolean(field), raw === "", !manifestFieldIsSecret(field ?? {}, secretFields)])) {
      delete config[path];
    }
  }
}

function collectConnectionSecrets(
  fields: Record<string, unknown>[],
  fieldValues: Record<string, unknown>,
  secretFields: Set<string>,
  existingRefs: Record<string, string>,
): Record<string, string> {
  const secrets: Record<string, string> = {};
  for (const field of fields) {
    const path = manifestFieldPath(field);
    if (!manifestFieldIsSecret(field, secretFields)) continue;
    const secretValue = stringFrom(fieldValues[path]);
    if (secretValue) secrets[path] = secretValue;
    if (!existingRefs[path] && secretValue) existingRefs[path] = path;
  }
  return secrets;
}

function validateRequiredConnectionFields({
  config,
  existingRefs,
  fieldByPath,
  fields,
  fieldValues,
  requiredSecretFields,
  secretFields,
  secrets,
}: {
  config: Record<string, unknown>;
  existingRefs: Record<string, string>;
  fieldByPath: Map<string, Record<string, unknown>>;
  fields: Record<string, unknown>[];
  fieldValues: Record<string, unknown>;
  requiredSecretFields: Set<string>;
  secretFields: Set<string>;
  secrets: Record<string, string>;
}, t: I18nContextValue["t"] = fallbackContext.t) {
  for (const field of fields) {
    const path = manifestFieldPath(field);
    if (!field.required && !requiredSecretFields.has(path)) continue;
    const configured = stringFrom(fieldValues[path] ?? config[path]);
    const hasExistingSecret = allTrue([
      manifestFieldIsSecret(field, secretFields),
      Boolean(existingRefs[path]),
    ]);
    if (!configured && !hasExistingSecret) {
      throw new RequiredFieldError(
        t("react.extensions.display_field_required", "{field} is required.", { field: stringFrom(field.label || path, t("react.extensions.required_field", "Required field")) }),
        path,
      );
    }
  }
  for (const path of requiredSecretFields) {
    if (anyTrue([fieldByPath.has(path), Boolean(existingRefs[path]), Boolean(secrets[path])])) continue;
    throw new RequiredFieldError(t("react.extensions.display_field_required", "{field} is required.", { field: path }), path);
  }
}

export function prepareConnectionSave({
  component,
  connectionMatchesCurrentInstall,
  oauthConnectionTypes,
  projectConnections,
  values,
}: {
  component: Record<string, unknown>;
  connectionMatchesCurrentInstall: (row: Record<string, unknown>) => boolean;
  oauthConnectionTypes: string[];
  projectConnections: unknown;
  values: Record<string, unknown>;
}, t: I18nContextValue["t"] = fallbackContext.t): ConnectionSavePreparation {
  const data = connectionDataForValues(component, values);
  const fieldValues = { ...connectionDefaultsForValues(component, values), ...values };
  const fields = listFromUnknown(component.fields).filter((field) =>
    manifestFieldVisibleForValues(field, fieldValues));
  const selectedId = stringFrom(fieldValues.connection_id);
  const rows = Array.isArray(projectConnections) ? projectConnections : [];
  const existing = rows.find((row) => allTrue([
    stringFrom(row.connection_id) === selectedId,
    connectionMatchesCurrentInstall(row),
  ]));
  if (selectedId && !existing) {
    throw new Error(t("react.extensions.this_connection_is_not_available_in_the_current_installed_release", "This connection is not available in the current installed release."));
  }
  const configFields = stringListFromUnknown(data.config_fields);
  const requiredSecretFields = new Set(stringListFromUnknown(data.required_secret_fields));
  const secretFields = new Set([...stringListFromUnknown(data.secret_fields), ...requiredSecretFields]);
  const config = choose(
    Boolean(existing),
    { ...objectOrEmpty(existing?.connection_config) },
    {},
  );
  const existingRefs = connectionSecretRefs(existing?.secret_refs);
  const fieldByPath = new Map(fields.map((field) => [manifestFieldPath(field), field]));
  const configuredFields = configuredConnectionFields(configFields, fields, fieldByPath, secretFields);
  applyConnectionConfigValues({ config, configuredFields, data, fields, fieldValues, secretFields });
  const secrets = collectConnectionSecrets(fields, fieldValues, secretFields, existingRefs);
  validateRequiredConnectionFields({
    config,
    existingRefs,
    fieldByPath,
    fields,
    fieldValues,
    requiredSecretFields,
    secretFields,
    secrets,
  }, t);
  return {
    config,
    configFieldSet: new Set(configFields),
    data,
    existing,
    existingRefs,
    fields,
    fieldValues,
    oauthConnection: oauthConnectionTypes.includes(stringFrom(data.connection_type)),
    secrets,
    secretFields,
    selectedId,
  };
}

function applyPreSaveResultMapping({
  action,
  config,
  configFieldSet,
  fields,
  fieldValues,
  resultContext,
  secretFields,
}: {
  action: Record<string, unknown>;
  config: Record<string, unknown>;
  configFieldSet: Set<string>;
  fields: Record<string, unknown>[];
  fieldValues: Record<string, unknown>;
  resultContext: Record<string, unknown>;
  secretFields: Set<string>;
}, t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> | null {
  const resultMapping = objectOrEmpty(action.result_mapping || action.resultMapping);
  let mappedFieldValues: Record<string, unknown> | null = null;
  for (const [targetField, rawSourcePath] of Object.entries(resultMapping)) {
    const sourcePath = stringFrom(rawSourcePath);
    if (!targetField || !sourcePath) continue;
    assertPreSaveResultMappingTarget({ configFields: configFieldSet, fields, secretFields, targetField }, t);
    const directValue = preSaveActionValue(resultContext, sourcePath);
    const value = choose(
      directValue === undefined && !sourcePath.includes("."),
      preSaveActionValue(resultContext, `result.${sourcePath}`),
      directValue,
    );
    if (value === undefined) continue;
    config[targetField] = value;
    mappedFieldValues = { ...(mappedFieldValues || fieldValues), [targetField]: value };
  }
  return mappedFieldValues;
}

async function executeConnectionPreSaveAction({
  action,
  componentId,
  confirmPreSave,
  config,
  configFieldSet,
  context,
  extensionKey,
  fields,
  fieldValues,
  pageId,
  projectId,
  secretFields,
}: {
  action: Record<string, unknown>;
  componentId: string;
  confirmPreSave: (request: PreSaveConfirmationRequest) => Promise<boolean>;
  config: Record<string, unknown>;
  configFieldSet: Set<string>;
  context: Record<string, unknown>;
  extensionKey: string;
  fields: Record<string, unknown>[];
  fieldValues: Record<string, unknown>;
  pageId: string;
  projectId: string;
  secretFields: Set<string>;
}, t: I18nContextValue["t"] = fallbackContext.t): Promise<Record<string, unknown> | null> {
  const actionId = stringFrom(action.action_id || action.actionId);
  const configuredFailure = stringFrom(
    action.failure_message || action.failureMessage,
    t("react.extensions.action_value0_failed", "Action {value0} failed.", { value0: catalogLabel(actionId) }),
  );
  let actionPayload: unknown;
  try {
    actionPayload = await executeExtensionAction(
      extensionKey,
      actionId,
      buildPreSaveActionPayload({ action, componentId, context, pageId, projectId }),
    );
  } catch (error) {
    throw new PreSaveActionError(
      extensionActionFailureDetail(error, t) || configuredFailure,
      action,
    );
  }
  if (!manifestActionSucceeded(actionPayload, t)) {
    const outcome = manifestActionOutcome(actionPayload, t);
    throw new PreSaveActionError(outcome.message || configuredFailure, action);
  }
  const resultContext = {
    ...context,
    response: actionPayload,
    result: manifestActionResult(actionPayload),
  };
  await validatePreSaveActionResult(action, resultContext, confirmPreSave, t);
  return applyPreSaveResultMapping({
    action,
    config,
    configFieldSet,
    fields,
    fieldValues,
    resultContext,
    secretFields,
  }, t);
}

export async function runConnectionPreSaveActions({
  componentId,
  confirmPreSave,
  data,
  extensionKey,
  initialFieldValues,
  pageId,
  preparation,
  projectId,
  reportStatus,
  saveMappedValues,
}: {
  componentId: string;
  confirmPreSave: (request: PreSaveConfirmationRequest) => Promise<boolean>;
  data: Record<string, unknown>;
  extensionKey: string;
  initialFieldValues: Record<string, unknown>;
  pageId: string;
  preparation: ConnectionSavePreparation;
  projectId: string;
  reportStatus: (message: string) => void;
  saveMappedValues: (values: Record<string, unknown>) => void;
}, t: I18nContextValue["t"] = fallbackContext.t): Promise<Record<string, unknown>> {
  let fieldValues = initialFieldValues;
  for (const action of connectionPreSaveActions(data)) {
    const context = preSaveActionContext(data, fieldValues, preparation.config);
    const actionId = stringFrom(action.action_id || action.actionId);
    if (!preSaveActionShouldRun(action, context) || !actionId) continue;
    reportStatus(stringFrom(
      action.status || action.loading_message || action.loadingMessage,
      t("react.extensions.running_value0", "Running {value0}...", { value0: catalogLabel(actionId) }),
    ));
    const mappedFieldValues = await executeConnectionPreSaveAction({
      action,
      componentId,
      confirmPreSave,
      config: preparation.config,
      configFieldSet: preparation.configFieldSet,
      context,
      extensionKey,
      fields: preparation.fields,
      fieldValues,
      pageId,
      projectId,
      secretFields: preparation.secretFields,
    }, t);
    if (!mappedFieldValues) continue;
    fieldValues = mappedFieldValues;
    saveMappedValues(fieldValues);
  }
  return fieldValues;
}

export function connectionPayloadForSave({
  component,
  extensionInstallId,
  extensionKey,
  fieldValues,
  preparation,
}: {
  component: Record<string, unknown>;
  extensionInstallId: string;
  extensionKey: string;
  fieldValues: Record<string, unknown>;
  preparation: ConnectionSavePreparation;
}): Record<string, unknown> {
  return {
    connection_kind: stringFrom(preparation.data.connection_kind, "extension"),
    // The stored name is the declaration's own title, never a translation of it.
    display_name: stringFrom(
      fieldValues.display_name || sourceDeclaration(component).title || extensionKey,
    ),
    enabled: choose(Boolean(preparation.existing), preparation.existing?.enabled !== false, false),
    connection_type: stringFrom(preparation.data.connection_type),
    environment: stringFrom(fieldValues.environment),
    extension_id: extensionKey,
    ...choose(Boolean(extensionInstallId), { extension_install_id: extensionInstallId }, {}),
    connection_config: preparation.config,
    secret_refs: { refs: preparation.existingRefs },
    tags: stringListFromUnknown(preparation.data.tags),
  };
}

async function saveConnectionSecretsWhenPresent(
  projectId: string,
  connectionId: string,
  secrets: Record<string, string>,
) {
  if (!Object.keys(secrets).length) return;
  await saveProjectConnectionSecrets(projectId, connectionId, secrets);
}

export async function persistPreparedConnection({
  onAuthorization,
  onConnectionCreated,
  onOAuthStart,
  payload,
  preparation,
  projectId,
  returnPath,
}: {
  onAuthorization: (payload: Record<string, unknown>) => void;
  onConnectionCreated: (connectionId: string) => void;
  onOAuthStart: () => void;
  payload: Record<string, unknown>;
  preparation: ConnectionSavePreparation;
  projectId: string;
  returnPath: string;
}, t: I18nContextValue["t"] = fallbackContext.t): Promise<string> {
  if (preparation.existing) {
    await updateProjectConnection(projectId, preparation.selectedId, payload);
    await saveConnectionSecretsWhenPresent(projectId, preparation.selectedId, preparation.secrets);
    return preparation.selectedId;
  }
  const created = await createProjectConnection(projectId, payload);
  const connectionId = stringFrom(created.connection_id);
  if (!connectionId) throw new Error(t("react.extensions.connection_was_created_without_a_connection_id", "Connection was created without a connection id."));
  onConnectionCreated(connectionId);
  await saveConnectionSecretsWhenPresent(projectId, connectionId, preparation.secrets);
  await updateProjectConnection(projectId, connectionId, { enabled: true });
  const oauthIntent = stringFrom(preparation.data.oauth_intent);
  if (preparation.oauthConnection && oauthIntent) {
    onOAuthStart();
    const oauthPayload = await startProjectConnectionOAuth(
      projectId,
      connectionId,
      { intent: oauthIntent, return_path: returnPath },
    );
    onAuthorization(oauthPayload);
  }
  return connectionId;
}

export async function recoverFailedConnectionSave({
  connectionId,
  oauthStartAttempted,
  projectId,
  refetch,
}: {
  connectionId: string;
  oauthStartAttempted: boolean;
  projectId: string;
  refetch: () => Promise<unknown>;
}) {
  if (!connectionId) return;
  if (oauthStartAttempted) {
    await refetch();
    return;
  }
  await deleteProjectConnection(projectId, connectionId).catch(() => undefined);
}
