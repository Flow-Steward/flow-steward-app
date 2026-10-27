import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { type ConnectionFormField } from "./data-connection-helpers";

import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type FormSubmitEvent = {
  currentTarget: HTMLFormElement;
  preventDefault: () => void;
};

export type ConnectionDraft = {
  connection_id: string;
  workspace_id: string;
  connector_option_id: string;
  display_name: string;
  description: string;
  environment: string;
  enabled: boolean;
  tags_csv: string;
  connection_values: Record<string, string>;
  touched_connection_fields?: string[];
  secret_values: Record<string, string>;
  existing_secret_keys: string[];
  original_connector_option_id?: string;
  original_connection_config?: Record<string, unknown>;
  original_metadata?: Record<string, unknown>;
};

export const EMPTY_DRAFT: ConnectionDraft = {
  connection_id: "",
  workspace_id: "",
  connector_option_id: "",
  display_name: "",
  description: "",
  environment: "dev",
  enabled: true,
  tags_csv: "",
  connection_values: {},
  touched_connection_fields: [],
  secret_values: {},
  existing_secret_keys: [],
};

export function storedSecretKeys(secretRefs: Record<string, unknown>): string[] {
  const refs = secretRefs.refs;
  return refs && typeof refs === "object" && !Array.isArray(refs)
    ? Object.keys(refs)
    : [];
}

export function draftValue(value: unknown): string {
  if (value === undefined || value === null) {
    return "";
  }
  if (typeof value === "object") {
    return JSON.stringify(value, null, 2) ?? "";
  }
  return stringFromUnknownValue(value);
}

export function valueForSchemaField(
  value: string,
  field: {
    label: string;
    type: string;
    minimum?: number;
    maximum?: number;
    required: boolean;
  }, t: I18nContextValue["t"] = fallbackContext.t
): unknown {
  const trimmed = value.trim();
  if (!trimmed) {
    if (field.required) {
      throw new Error(t("react.workflows.value0_is_required", "{value0} is required.", { value0: field.label }));
    }
    return undefined;
  }
  if (field.type === "integer" || field.type === "number") {
    const parsed = numericSchemaFieldValue(trimmed, field, t);
    if (field.type === "integer" && !Number.isInteger(parsed)) {
      throw new Error(t("react.registry.value0_must_be_a_whole_number", "{value0} must be a whole number.", { value0: field.label }));
    }
    return parsed;
  }
  if (field.type === "boolean") {
    return trimmed === "true";
  }
  if (field.type === "object" || field.type === "array") {
    return jsonSchemaFieldValue(trimmed, t);
  }
  return trimmed;
}

export function numericSchemaFieldValue(
  value: string,
  field: { label: string; minimum?: number; maximum?: number }, t: I18nContextValue["t"] = fallbackContext.t
): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    throw new TypeError(t("react.registry.display_numeric_connection_valid_number", "Numeric connection fields must contain a valid number."));
  }
  if (field.minimum !== undefined && parsed < field.minimum) {
    throw new Error(t("react.registry.value0_must_be_at_least_value1", "{value0} must be at least {value1}.", { value0: field.label, value1: field.minimum }));
  }
  if (field.maximum !== undefined && parsed > field.maximum) {
    throw new Error(t("react.registry.value0_must_be_at_most_value1", "{value0} must be at most {value1}.", { value0: field.label, value1: field.maximum }));
  }
  return parsed;
}

export function jsonSchemaFieldValue(value: string, t: I18nContextValue["t"] = fallbackContext.t): unknown {
  try {
    return JSON.parse(value);
  } catch {
    throw new Error(t("react.registry.object_and_array_connection_fields_must_contain_valid_json", "Object and array connection fields must contain valid JSON."));
  }
}

export function connectionFieldInputType(field: ConnectionFormField): string {
  if (field.storage === "secret") {
    return "password";
  }
  if (field.type === "integer" || field.type === "number") {
    return "number";
  }
  return "text";
}

export function saveButtonLabel(saving: boolean, editing: boolean, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (saving) {
    return editing ? t("settings.retention.saving", "Saving...") : t("react.registry.creating", "Creating...");
  }
  return editing ? t("react.extensions.save_connection", "Save connection") : t("react.registry.create_connection", "Create connection");
}
