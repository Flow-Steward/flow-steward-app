import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  stringFrom,
  objectOrEmpty,
  listFromUnknown,
  matchesVisibleIf,
  valueAtPath,
  stringListFromUnknown,
} from "@/pages/extensions/extension-manifest-utils";
import { replaceDelimitedTokens } from "@/lib/utils/template-tokens";
import { catalogLabel } from "./hub-support";

export function manifestFieldPath(field: Record<string, unknown>): string {
  return stringFrom(field.path || field.name || field.field_id || field.id);
}

export function manifestFieldWidget(field: Record<string, unknown>): string {
  return stringFrom(field.widget || field.type || "text").toLowerCase();
}

export function manifestFieldIsSecret(field: Record<string, unknown>, secretFields: Set<string>): boolean {
  const path = manifestFieldPath(field);
  return secretFields.has(path) || manifestFieldWidget(field) === "secret" || field.secret === true;
}

export function assertPreSaveResultMappingTarget({
  configFields,
  fields,
  secretFields,
  targetField,
}: {
  configFields: Set<string>;
  fields: Record<string, unknown>[];
  secretFields: Set<string>;
  targetField: string;
}, t: I18nContextValue["t"] = fallbackContext.t) {
  const field = fields.find((item) => manifestFieldPath(item) === targetField);
  if (
    !configFields.has(targetField)
    || secretFields.has(targetField)
    || (field ? manifestFieldIsSecret(field, secretFields) : false)
  ) {
    throw new Error(
      t("react.extensions.pre_save_result_mapping_target_value0_must_be_a_declared_non_secret_config_field", "Pre-save result mapping target '{value0}' must be a declared non-secret config field.", { value0: targetField }),
    );
  }
}

export function connectionTypeField(data: Record<string, unknown>): string {
  return stringFrom(data.connection_type_field || data.protocol_field || "");
}

function connectionTypeVariants(data: Record<string, unknown>): Record<string, unknown> {
  return objectOrEmpty(data.connection_types || data.variants);
}

export function connectionSelectedType(
  component: Record<string, unknown>,
  values: Record<string, unknown>,
): string {
  const data = objectOrEmpty(component.data);
  const defaults = objectOrEmpty(component.defaults);
  const typeField = connectionTypeField(data);
  const variants = connectionTypeVariants(data);
  return stringFrom(
    (typeField ? values[typeField] || defaults[typeField] : "")
      || data.connection_type
      || data.default_connection_type
      || Object.keys(variants)[0],
  );
}

function connectionVariantData(
  component: Record<string, unknown>,
  values: Record<string, unknown>,
): Record<string, unknown> {
  const data = objectOrEmpty(component.data);
  return objectOrEmpty(connectionTypeVariants(data)[connectionSelectedType(component, values)]);
}

export function connectionDataForValues(
  component: Record<string, unknown>,
  values: Record<string, unknown>,
): Record<string, unknown> {
  const data = objectOrEmpty(component.data);
  const selectedType = connectionSelectedType(component, values);
  const selectedVariant = connectionVariantData(component, values);
  return {
    ...data,
    ...selectedVariant,
    connection_type: stringFrom(
      selectedVariant.connection_type || selectedType || data.connection_type,
    ),
  };
}

export function connectionDefaultsForValues(
  component: Record<string, unknown>,
  values: Record<string, unknown>,
): Record<string, unknown> {
  return {
    ...objectOrEmpty(component.defaults),
    ...objectOrEmpty(connectionVariantData(component, values).defaults),
  };
}

export function connectionConfigFromRow(value: unknown): Record<string, unknown> {
  if (typeof value !== "string") {
    return objectOrEmpty(value);
  }
  try {
    return objectOrEmpty(JSON.parse(value));
  } catch {
    return {};
  }
}

export function manifestFieldVisibleForValues(
  field: Record<string, unknown>,
  values: Record<string, unknown>,
): boolean {
  const conditions = listFromUnknown(field.visible_if || field.visibleIf);
  return !conditions.length || conditions.every((condition) => matchesVisibleIf(values, condition));
}

export function manifestFieldValue(
  field: Record<string, unknown>,
  values: Record<string, unknown>,
): unknown {
  const path = manifestFieldPath(field);
  const raw = values[path];
  const widget = manifestFieldWidget(field);
  if (raw === undefined || raw === null || raw === "") {
    return undefined;
  }
  if (widget === "integer" || widget === "number") {
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return raw;
}

export function oauthConnectionTypesFromManifest(value: unknown): string[] {
  return listFromUnknown(objectOrEmpty(value).connection_types)
    .filter(
      (row) => stringFrom(objectOrEmpty(row.auth).type).toLowerCase() === "oauth2",
    )
    .map((row) => stringFrom(row.connection_type_id || row.connection_type))
    .filter(Boolean);
}

export function connectionActionTarget(
  component: Record<string, unknown>,
  values: Record<string, unknown>,
) {
  if (stringFrom(component.type) !== "connection_form") {
    return {};
  }
  const selectedId = stringFrom(values.connection_id);
  if (selectedId) {
    return {
      connection_id: selectedId,
      project_id: stringFrom(values.project_id),
    };
  }
  return {};
}

export function connectionPreSaveActions(data: Record<string, unknown>): Array<Record<string, unknown>> {
  const actions = listFromUnknown(data.pre_save_actions || data.preSaveActions);
  if (actions.length) {
    return actions;
  }
  const single = objectOrEmpty(data.pre_save_action || data.preSaveAction);
  return Object.keys(single).length ? [single] : [];
}

export function preSaveActionContext(
  data: Record<string, unknown>,
  fieldValues: Record<string, unknown>,
  config: Record<string, unknown>,
) {
  return {
    ...data,
    ...fieldValues,
    ...config,
    data,
    values: fieldValues,
    config,
  };
}

export function preSaveActionShouldRun(
  action: Record<string, unknown>,
  context: Record<string, unknown>,
): boolean {
  const conditions = listFromUnknown(
    action.run_when || action.runWhen || action.visible_if || action.visibleIf || action.if,
  );
  return !conditions.length || conditions.every((condition) => matchesVisibleIf(context, condition));
}

export function preSaveActionValue(context: Record<string, unknown>, path: unknown): unknown {
  const key = stringFrom(path);
  if (!key) {
    return undefined;
  }
  return valueAtPath(context, key);
}

function renderPreSaveTemplate(template: unknown, context: Record<string, unknown>): string {
  return replaceDelimitedTokens(stringFrom(template), "{{", "}}", (rawPath) => {
    const value = preSaveActionValue(context, rawPath.trim());
    return value === undefined || value === null ? "" : stringFrom(value);
  });
}

export function buildPreSaveActionPayload({
  action,
  componentId,
  context,
  pageId,
  projectId,
}: {
  action: Record<string, unknown>;
  componentId: string;
  context: Record<string, unknown>;
  pageId: string;
  projectId: string;
}): Record<string, unknown> {
  const connection = objectOrEmpty(action.target_connection || action.targetConnection);
  const configFields = stringListFromUnknown(connection.config_fields || connection.configFields);
  const targetConfig = Object.fromEntries(
    configFields
      .map((field) => [field, preSaveActionValue(context, `config.${field}`)] as const)
      .filter(([, value]) => value !== undefined),
  );
  const valuesFields = stringListFromUnknown(action.values_fields || action.valuesFields);
  const actionValues = Object.fromEntries(
    valuesFields
      .map((field) => [field, preSaveActionValue(context, field)] as const)
      .filter(([, value]) => value !== undefined),
  );
  const targetConnectionType = stringFrom(
    connection.connection_type_id || connection.connection_type || context.connection_type,
  );
  return {
    component_id: componentId,
    context: projectId ? { project_id: projectId } : {},
    page_id: pageId,
    target: targetConnectionType
      ? {
          connection: {
            connection_type_id: targetConnectionType,
            config: targetConfig,
          },
        }
      : {},
    values: actionValues,
    data: objectOrEmpty(action.data),
  };
}

/** What a pre-save action shows the operator before the save goes ahead. */
export type PreSaveConfirmationRequest = {
  title: string;
  message: string;
  confirmLabel: string;
};

export async function validatePreSaveActionResult(
  action: Record<string, unknown>,
  resultContext: Record<string, unknown>,
  confirmPreSave: (request: PreSaveConfirmationRequest) => Promise<boolean>, t: I18nContextValue["t"] = fallbackContext.t
) {
  const confirmation = objectOrEmpty(action.confirmation);
  for (const field of stringListFromUnknown(confirmation.required_result_fields)) {
    if (!stringFrom(preSaveActionValue(resultContext, `result.${field}`))) {
      throw new Error(t("react.extensions.value0_was_not_returned", "{value0} was not returned.", { value0: catalogLabel(field) }));
    }
  }
  const message = renderPreSaveTemplate(confirmation.message, resultContext);
  if (message && !(await confirmPreSave({
    title: stringFrom(confirmation.title, t("react.extensions.continue_saving_this_connection", "Continue saving this connection?")),
    message,
    confirmLabel: stringFrom(confirmation.confirm_label, t("react.extensions.save_connection", "Save connection")),
  }))) {
    throw new Error(stringFrom(confirmation.reject_error, t("react.extensions.action_confirmation_was_rejected", "Action confirmation was rejected.")));
  }
}
