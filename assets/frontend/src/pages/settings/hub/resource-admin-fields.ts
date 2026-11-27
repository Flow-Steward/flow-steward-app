import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { trimRepeatedEdges } from "@/lib/utils/string-normalization";
import { isSameOriginApiPath } from "@/pages/settings/hub/resource-report";
import { normalizeChoiceOptions, type ChoiceOption } from "@/pages/settings/hub/choice-options";

/**
 * The host-rendered form of a settings record (ADR 0082, section 6): the
 * resource_admin field vocabulary — text, choices, help, advanced, visible_when —
 * shared by module pages and Core pages that draw a form a module describes.
 */

/** What a form needs of a resource: a key for control ids and its fields. */
export type FormResource = { fields: ControlSurfaceField[]; key: string };

export function resourceSaveLabel(editing: boolean, t: I18nContextValue["t"] = fallbackContext.t): string {
  return editing ? t("registry.common.save", "Save") : t("projects.modal.create", "Create");
}

export type ControlSurfaceFieldType =
  | "checkbox"
  | "csv"
  | "json"
  | "multi_choice"
  | "number"
  | "reference"
  | "secret"
  | "select"
  | "textarea"
  | "text";

export const FIELD_TYPES: readonly ControlSurfaceFieldType[] = [
  "checkbox",
  "csv",
  "json",
  "multi_choice",
  "number",
  "reference",
  "secret",
  "select",
  "textarea",
];

export type ControlSurfaceField = {
  /** Shown under a collapsed "Advanced" section of the form. */
  advanced: boolean;
  createOnly: boolean;
  defaultValue: string | boolean | number | string[];
  /** Shown instead of the form when a required reference has nothing to choose. */
  emptyOptionsLabel: string;
  /** One plain-language line under the control. */
  help: string;
  /** With a single allowed option the field is not drawn at all. */
  hiddenWhenSingle: boolean;
  key: string;
  label: string;
  omitBlankOnUpdate: boolean;
  /** Declared options; a reference field loads its own from `optionsPath`. */
  options: ChoiceOption[];
  optionsPath: string;
  placeholder: string;
  required: boolean;
  type: ControlSurfaceFieldType;
  /** Shown only while another field's value contains this text. */
  visibleWhen: { contains: string; field: string } | null;
};

/** Loaded options of every `options_path` of a surface. */
export type OptionsByPath = Record<string, ChoiceOption[]>;

export type FormValue = string | boolean | string[];

export type FormValues = Record<string, FormValue>;

export function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => firstStringFromUnknownValue([item, ""]).trim()).filter(isTruthy);
}

export function normalizeField(value: unknown): ControlSurfaceField | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const key = firstStringFromUnknownValue([record.key, ""]).trim();
  const label = firstStringFromUnknownValue([record.label, key]).trim();
  if (!key || !label) {
    return null;
  }
  const rawType = firstStringFromUnknownValue([record.type, "text"]).trim();
  const type = FIELD_TYPES.find((candidate) => candidate === rawType) ?? "text";
  let defaultValue: ControlSurfaceField["defaultValue"] = firstStringFromUnknownValue([record.default, ""]);
  if (typeof record.default === "boolean" || typeof record.default === "number") {
    defaultValue = record.default;
  } else if (Array.isArray(record.default)) {
    defaultValue = stringList(record.default);
  }
  const optionsPath = firstStringFromUnknownValue([record.options_path, ""]).trim();
  if ((type === "reference" || optionsPath) && !isSameOriginApiPath(optionsPath)) {
    return null;
  }
  const condition = record.visible_when;
  let visibleWhen: ControlSurfaceField["visibleWhen"] = null;
  if (condition && typeof condition === "object" && !Array.isArray(condition)) {
    const conditionRecord = condition as Record<string, unknown>;
    const field = firstStringFromUnknownValue([conditionRecord.field, ""]).trim();
    const contains = firstStringFromUnknownValue([conditionRecord.contains, ""]);
    visibleWhen = field && contains ? { contains, field } : null;
  }
  return {
    advanced: record.advanced === true,
    createOnly: Boolean(record.create_only),
    defaultValue,
    emptyOptionsLabel: firstStringFromUnknownValue([record.empty_options_label, ""]).trim(),
    help: firstStringFromUnknownValue([record.help, ""]).trim(),
    hiddenWhenSingle: record.hidden_when_single === true,
    key,
    label,
    omitBlankOnUpdate: Boolean(record.omit_blank_on_update),
    options: normalizeChoiceOptions(record.options),
    optionsPath,
    placeholder: firstStringFromUnknownValue([record.placeholder, ""]).trim(),
    required: Boolean(record.required),
    type,
    visibleWhen,
  };
}

/** The options a choice field offers: declared ones, or those its path loaded. */
export function fieldOptions(field: ControlSurfaceField, optionsByPath: OptionsByPath): ChoiceOption[] {
  if (field.optionsPath) {
    return optionsByPath[field.optionsPath] ?? [];
  }
  return field.options;
}

export function isChoiceField(field: ControlSurfaceField): boolean {
  return field.type === "select" || field.type === "reference" || field.type === "multi_choice";
}

/**
 * The one option a choice field allows, when it allows exactly one: the value is
 * then fixed, shown as read-only text and sent without asking. An optional
 * multi-choice field still lets the administrator leave its one option out.
 */
export function fixedOption(field: ControlSurfaceField, optionsByPath: OptionsByPath): ChoiceOption | null {
  if (!isChoiceField(field) || (field.type === "multi_choice" && !field.required)) {
    return null;
  }
  const options = fieldOptions(field, optionsByPath);
  return options.length === 1 ? options[0] : null;
}

/**
 * The value a field takes when the form is sent: the one allowed option of a
 * fixed choice, otherwise the form value. A choice that names no option the field
 * offers counts as no choice, once the field's options are known.
 */
export function effectiveFieldValue(
  field: ControlSurfaceField | undefined,
  key: string,
  values: FormValues,
  optionsByPath: OptionsByPath,
): FormValue {
  const value = values[key] ?? "";
  if (!field || !isChoiceField(field)) return value;
  const fixed = fixedOption(field, optionsByPath);
  if (fixed) return field.type === "multi_choice" ? [fixed.value] : fixed.value;
  const optionsKnown = field.optionsPath ? field.optionsPath in optionsByPath : field.options.length > 0;
  if (!optionsKnown) return value;
  const offered = new Set(fieldOptions(field, optionsByPath).map((option) => option.value));
  if (Array.isArray(value)) return value.filter((item) => offered.has(item));
  const text = stringFromUnknownValue(value);
  return offered.has(text) ? text : "";
}

/**
 * Whether a field's `visible_when` condition holds for the current values. The
 * referenced field is read at its effective value (see `effectiveFieldValue`), so
 * a choice fixed to its single option counts even when the form holds another.
 */
export function fieldConditionHolds(
  field: ControlSurfaceField,
  values: FormValues,
  fields: readonly ControlSurfaceField[] = [],
  optionsByPath: OptionsByPath = {},
): boolean {
  if (!field.visibleWhen) return true;
  const key = field.visibleWhen.field;
  const referenced = fields.find((item) => item.key === key);
  const other = effectiveFieldValue(referenced, key, values, optionsByPath);
  const text = Array.isArray(other) ? other.join(",") : stringFromUnknownValue(other ?? "");
  return text.includes(field.visibleWhen.contains);
}

export function stableControlToken(value: unknown): string {
  const token = firstStringFromUnknownValue([value, ""])
    .trim()
    .toLowerCase()
    .replaceAll(/[^a-z0-9_-]+/g, "-");
  return trimRepeatedEdges(token, "-");
}

export function fieldControlId(resource: FormResource, field: ControlSurfaceField, editingId: string) {
  return [
    "settings",
    stableControlToken(resource.key),
    stableControlToken(field.key),
    stableControlToken(editingId || "new"),
  ]
    .filter(isTruthy)
    .join("-");
}

export function defaultFieldValue(field: ControlSurfaceField): FormValue {
  if (field.type === "checkbox") {
    return Boolean(field.defaultValue);
  }
  if (field.type === "multi_choice") {
    return Array.isArray(field.defaultValue) ? field.defaultValue : stringList([field.defaultValue]);
  }
  if (field.type === "secret") {
    return "";
  }
  return firstStringFromUnknownValue([field.defaultValue, ""]);
}

export function defaultFormValues(resource: FormResource): FormValues {
  return Object.fromEntries(resource.fields.map((field) => [field.key, defaultFieldValue(field)]));
}

export function valuesFromRecord(resource: FormResource, record: Record<string, unknown>): FormValues {
  return Object.fromEntries(
    resource.fields.map((field) => {
      const value = record[field.key];
      if (field.type === "checkbox") {
        return [field.key, Boolean(value)];
      }
      if (field.type === "secret") {
        // A stored secret never comes back; blank keeps it on update.
        return [field.key, ""];
      }
      if (field.type === "multi_choice") {
        return [field.key, Array.isArray(value) ? stringList(value) : stringList([value ?? ""])];
      }
      if (field.type === "csv") {
        return [field.key, Array.isArray(value) ? value.join(", ") : stringFromUnknownValue(value ?? "")];
      }
      if (field.type === "json") {
        return [field.key, field.omitBlankOnUpdate ? "" : JSON.stringify(value ?? {}, null, 2)];
      }
      return [field.key, stringFromUnknownValue(value ?? defaultFieldValue(field))];
    }),
  );
}

export function payloadFromValues(
  resource: FormResource,
  values: FormValues,
  options: { editing: boolean; optionsByPath?: OptionsByPath },
  t: I18nContextValue["t"],
): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  const optionsByPath = options.optionsByPath ?? {};
  for (const field of resource.fields) {
    if (options.editing && field.createOnly) {
      continue;
    }
    const fixed = fixedOption(field, optionsByPath);
    if (fixed) {
      payload[field.key] = field.type === "multi_choice" ? [fixed.value] : fixed.value;
      continue;
    }
    if (!fieldConditionHolds(field, values, resource.fields, optionsByPath)) {
      // A field the form does not show is cleared, so a stale value never applies.
      payload[field.key] = field.type === "csv" || field.type === "multi_choice" ? [] : "";
      continue;
    }
    const value = values[field.key];
    const textValue = typeof value === "string" ? value.trim() : "";
    if (field.type === "secret" && textValue === "") {
      continue;
    }
    if (options.editing && field.omitBlankOnUpdate && textValue === "") {
      continue;
    }
    payload[field.key] = normalizedResourceFieldValue(
      field,
      value,
      textValue,
      options.editing,
      t,
    );
  }
  return payload;
}

export function normalizedResourceFieldValue(
  field: ControlSurfaceField,
  value: FormValue,
  textValue: string,
  editing: boolean,
  t: I18nContextValue["t"],
): unknown {
  if (
    field.required &&
    !editing &&
    field.type !== "checkbox" &&
    field.type !== "multi_choice" &&
    textValue === ""
  ) {
    throw new Error(t("react.settings.resource_field_required", "{field} is required.", { field: field.label }));
  }
  if (field.type === "checkbox") {
    return Boolean(value);
  }
  if (field.type === "multi_choice") {
    const items = Array.isArray(value) ? value : [];
    if (field.required && items.length === 0) {
      throw new Error(t("react.settings.resource_field_required", "{field} is required.", { field: field.label }));
    }
    return items;
  }
  if (field.type === "csv") {
    const items = textValue
      .split(",")
      .map((item) => item.trim())
      .filter(isTruthy);
    if (field.required && items.length === 0) {
      throw new Error(t("react.settings.resource_field_required", "{field} is required.", { field: field.label }));
    }
    return items;
  }
  if (field.type === "number") {
    return textValue ? Number(textValue) : 0;
  }
  if (field.type === "json") {
    return textValue ? JSON.parse(textValue) : {};
  }
  return textValue;
}


/** The fields a form shows now: create-only, conditional and single-choice rules applied. */
export function visibleFormFields(
  resource: FormResource,
  values: FormValues,
  editingId: string,
  optionsByPath: OptionsByPath,
): ControlSurfaceField[] {
  return resource.fields.filter((field) => {
    if (editingId && field.createOnly) return false;
    if (!fieldConditionHolds(field, values, resource.fields, optionsByPath)) return false;
    if (field.hiddenWhenSingle && fixedOption(field, optionsByPath)) return false;
    // An optional reference with nothing to choose has nothing to ask.
    if (field.optionsPath && !field.required && fieldOptions(field, optionsByPath).length === 0) return false;
    return true;
  });
}
