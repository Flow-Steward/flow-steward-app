import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { localizedWORKFLOW_INTERFACE_INPUT_MODES } from "@/pages/workflows/detail/detail-automation-core";
import { stringList } from "@/pages/workflows/detail/detail-workflow-validation";
import { textOrEmpty } from "@/pages/workflows/detail/detail-test-run";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type SelectOption = {
  disabled?: boolean;
  label: string;
  value: string;
};

export type WorkflowInterfaceFieldDraft = {
  draftId: string;
  description: string;
  label: string;
  name: string;
  nameTouched?: boolean;
  options: string;
  required: boolean;
  type: string;
  /**
   * The editor's working value for a configured default. Always held as the
   * editor's own shape — text for object/array, a real boolean for checkboxes —
   * and converted on the way out. Empty for a secret: a configured secret has
   * no value to show, only a state.
   */
  default: unknown;
  /**
   * Whether a default is configured. Kept separately from `default` because
   * `false`, `0` and `""` are values an operator can legitimately mean, and
   * reading the value alone cannot tell those from "nothing set".
   */
  defaultConfigured: boolean;
  /**
   * A secret the operator has typed but not yet saved. Never populated from the
   * server: there is nothing to populate it from, which is the point.
   */
  secretDraft?: string;
  /** What to do with this secret when the workflow is saved. */
  secretAction?: "set" | "clear" | "";
  /** For a file input: the stored file test runs use when they pass none. */
  sampleFile?: WorkflowSampleFile | null;
  /** A file picked to replace it, uploaded when the workflow is saved. */
  sampleFileDraft?: File | null;
  /** What to do with the test file when the workflow is saved. */
  sampleFileAction?: "set" | "clear" | "";
  /** Stored author metadata; public computed/secret/file authority is not replayed. */
  originalAuthoredRow?: Record<string, unknown>;
};

export type WorkflowSampleFile = {
  filename?: string;
  content_type?: string;
  size_bytes?: number;
};

let workflowDraftSequence = 0;

export function nextWorkflowDraftId(prefix: string): string {
  workflowDraftSequence += 1;
  return `${prefix}-${workflowDraftSequence}`;
}

export function createWorkflowInterfaceFieldDraft(
  patch: Partial<WorkflowInterfaceFieldDraft> = {},
): WorkflowInterfaceFieldDraft {
  return {
    draftId: nextWorkflowDraftId("workflow-field"),
    description: "",
    label: "",
    name: "",
    options: "",
    required: false,
    type: "string",
    default: "",
    defaultConfigured: false,
    secretDraft: "",
    secretAction: "",
    sampleFile: null,
    sampleFileDraft: null,
    sampleFileAction: "",
    ...patch,
  };
}

/** Whether a field of this type stores its value outside the contract. */
export function isSecretFieldType(type: string): boolean {
  return textOrEmpty(type) === "secret";
}

/** Whether a field takes a file, which has a test file instead of a default. */
export function isFileFieldType(type: string): boolean {
  return textOrEmpty(type) === "file";
}

function sampleFileFromContract(value: unknown): WorkflowSampleFile | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const sample = value as Record<string, unknown>;
  const filename = textOrEmpty(stringFromUnknownValue(sample.filename));
  if (!filename) return null;
  const size = Number(sample.size_bytes);
  return {
    filename,
    content_type: textOrEmpty(stringFromUnknownValue(sample.content_type)),
    size_bytes: Number.isFinite(size) && size >= 0 ? size : undefined,
  };
}

function structuredDefaultValue(fieldType: "array" | "object", value: unknown): unknown {
  const text = stringFromUnknownValue(value).trim();
  if (!text) return undefined;
  try {
    const parsed: unknown = JSON.parse(text);
    if (fieldType === "array") return Array.isArray(parsed) ? parsed : undefined;
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed
      : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Convert one editor default into the value the contract stores.
 *
 * Returns `undefined` when there is nothing to send, which is not the same as
 * sending `null`: a null would come back as a configured default of null.
 */
export function contractDefaultValue(
  type: string,
  value: unknown,
): unknown {
  const fieldType = textOrEmpty(type) || "string";
  if (fieldType === "boolean") {
    return Boolean(value);
  }
  if (fieldType === "number") {
    const text = stringFromUnknownValue(value).trim();
    if (!text) {
      return undefined;
    }
    const parsed = Number(text);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  if (fieldType === "object" || fieldType === "array") {
    return structuredDefaultValue(fieldType, value);
  }
  const text = stringFromUnknownValue(value);
  if (fieldType === "string" || fieldType === "paragraph") {
    return text;
  }
  const normalized = text.trim();
  return normalized || undefined;
}

/** Read one contract row back into the editor's working shape. */
export function fieldDraftFromContract(
  row: Record<string, unknown>,
  originalAuthoredRow: Record<string, unknown> = row,
): WorkflowInterfaceFieldDraft {
  const type = textOrEmpty(stringFromUnknownValue(row.type)) || "string";
  const configured = Boolean(row.default_configured);
  let draftDefault: unknown = "";
  if (!isSecretFieldType(type) && row.default !== undefined && row.default !== null) {
    if (type === "object" || type === "array") {
      draftDefault = jsonText(row.default) || JSON.stringify(row.default) || "";
    } else if (type === "boolean") {
      draftDefault = Boolean(row.default);
    } else {
      draftDefault = stringFromUnknownValue(row.default);
    }
  }
  return createWorkflowInterfaceFieldDraft({
    name: textOrEmpty(stringFromUnknownValue(row.name)),
    label: textOrEmpty(stringFromUnknownValue(row.label)),
    description: textOrEmpty(stringFromUnknownValue(row.description)),
    options: Array.isArray(row.options)
      ? row.options.map((option) => stringFromUnknownValue(option)).join(", ")
      : "",
    required: Boolean(row.required),
    type,
    default: draftDefault,
    defaultConfigured: configured,
    sampleFile: isFileFieldType(type) ? sampleFileFromContract(row.sample_file) : null,
    originalAuthoredRow,
  });
}

export type ConnectorInputMode = "state" | "literal" | "expression";

export type ConnectorInputMappingRow = {
  key: string;
  mode: ConnectorInputMode;
  value: string;
  // Editor-only provenance for values the string controls cannot distinguish.
  retainedValue?: { key: string; value: null | "" };
};

export function collectOptionsFromUnknown(
  value: unknown,
  idKeys: string[],
  labelKeys: string[],
): SelectOption[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.reduce<SelectOption[]>((options, item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return options;
    }
    const row = item as Record<string, unknown>;
    const rawValue =
      idKeys.map((key) => textOrEmpty(row[key])).find(Boolean) || "";
    if (!rawValue) {
      return options;
    }
    const rawLabel =
      labelKeys.map((key) => textOrEmpty(row[key])).find(Boolean) || rawValue;
    options.push({ value: rawValue, label: rawLabel });
    return options;
  }, []);
}

export function uniqueSelectOptions(options: SelectOption[]): SelectOption[] {
  const seen = new Set<string>();
  const normalized: SelectOption[] = [];
  options.forEach((option) => {
    const value = textOrEmpty(option.value);
    if (!value || seen.has(value)) {
      return;
    }
    seen.add(value);
    normalized.push({
      value,
      label: textOrEmpty(option.label) || value,
      disabled: option.disabled === true,
    });
  });
  return normalized;
}

export function listFromUnknown(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is Record<string, unknown> =>
      Boolean(item) && typeof item === "object",
  );
}

export function objectOrEmpty(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function domIdSegment(value: string): string {
  return (
    firstStringFromUnknownValue([value, ""])
      .trim()
      .replaceAll(/[^A-Za-z0-9_-]+/g, "_") || "item"
  );
}

export function commaList(value: string): string[] {
  return firstStringFromUnknownValue([value, ""])
    .split(",")
    .map((item) => item.trim())
    .filter(isTruthy);
}

export function jsonText(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return "";
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "";
  }
}

export function httpRequestBodyEditorText(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "";
  }
}

export function jsonObjectFromDraft(value: string, t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  const normalized = firstStringFromUnknownValue([value, ""]).trim();
  if (!normalized) {
    return {};
  }
  const parsed = JSON.parse(normalized);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(t("react.workflows.override_json_must_be_an_object", "Override JSON must be an object."));
  }
  return parsed as Record<string, unknown>;
}

export function workflowInputModeHelp(mode: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const normalized = textOrEmpty(mode);
  if (normalized === "json_schema") {
    return t("react.workflows.advanced_integration_mode_the_launcher_sends_one_json_object_and_flow_steward_validates_it_against_the_schema_", "Advanced integration mode. The launcher sends one JSON object and Flow Steward validates it against the schema below. Files are still handled separately as attachments or artifacts.");
  }
  if (normalized === "passthrough") {
    return t("react.workflows.advanced_integration_mode_flow_steward_accepts_the_whole_incoming_json_object_as_workflow_input_without_turnin", "Advanced integration mode. Flow Steward accepts the whole incoming JSON object as workflow input without turning it into form fields. This does not upload or parse files.");
  }
  return t("react.workflows.normal_mode_flow_steward_builds_a_launch_form_from_expected_inputs_and_api_mcp_webhook_schedule_callers_send_t", "Normal mode. Flow Steward builds a launch form from Expected inputs, and API/MCP/webhook/schedule callers send those same field names.");
}

export function workflowInputModeOptions(
  currentMode: string,
  showAdvancedModes: boolean,
  t: I18nContextValue["t"] = fallbackContext.t,
  allowStructuredInputs = false,
): SelectOption[] {
  const normalizedCurrent = textOrEmpty(currentMode) || "fields";
  return localizedWORKFLOW_INTERFACE_INPUT_MODES(t)
    .filter((mode) =>
      showAdvancedModes || mode.value === "fields" || mode.value === normalizedCurrent ||
      (allowStructuredInputs && mode.value === "json_schema"),
    )
    .map((mode) => mode.value === "json_schema" && allowStructuredInputs
      ? { ...mode, label: t("react.workflows.structured_inputs", "Structured inputs") }
      : mode);
}

export function workflowFieldDraftsFromUnknown(
  value: unknown,
  authoredValue: unknown = value,
): WorkflowInterfaceFieldDraft[] {
  const authoredRows = listFromUnknown(authoredValue);
  return listFromUnknown(value).map((row) => {
    const authored = authoredRows.find((item) => textOrEmpty(item.name) === textOrEmpty(row.name)) || {};
    const draft = fieldDraftFromContract({
      ...row,
      name: textOrEmpty(row.name || row.key || row.id),
      options: stringList(row.options),
      type:
        textOrEmpty(row.type) === "text"
          ? "string"
          : textOrEmpty(row.type) || "string",
    }, authored);
    return draft;
  });
}

function configuredDefaultRequirement(type: string, t: I18nContextValue["t"]): string {
  switch (type) {
    case "array": return t("react.workflows.display_valid_array", "a valid array");
    case "number": return t("react.workflows.display_valid_number", "a valid number");
    case "object": return t("react.workflows.display_valid_object", "a valid object");
    case "select": return t("react.workflows.display_declared_option", "a declared option");
    default: return t("react.workflows.display_valid_value", "a valid value");
  }
}

function workflowFieldPayloadRow(row: WorkflowInterfaceFieldDraft, t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  const name = textOrEmpty(row.name);
  if (!name) {
    throw new Error(t("react.registry.name_is_required", "Name is required."));
  }
  const type = textOrEmpty(row.type) || "string";
  const payload: Record<string, unknown> = { ...row.originalAuthoredRow };
  for (const key of ["default_secret_ref", "default_configured", "sample_file", "source", "returned_files"]) {
    delete payload[key];
  }
  payload.name = name;
  payload.type = type;
  payload.required = Boolean(row.required);
  delete payload.default;
  delete payload.label;
  delete payload.description;
  delete payload.options;
  if (textOrEmpty(row.label)) {
    payload.label = textOrEmpty(row.label);
  }
  if (textOrEmpty(row.description)) {
    payload.description = textOrEmpty(row.description);
  }
  const options = commaList(row.options);
  if (type === "select" && options.length) {
    payload.options = options;
  }
  // A secret's value never travels with the contract: it is set through its
  // own write-only endpoint, and this update must not be a second way in. A
  // file input has no default: its test file is uploaded on its own endpoint.
  if (!row.defaultConfigured || isSecretFieldType(type) || isFileFieldType(type)) {
    return payload;
  }
  const configured = contractDefaultValue(type, row.default);
  if (configured === undefined) {
    const requirement = configuredDefaultRequirement(type, t);
    throw new Error(t("react.workflows.configured_default_for_value0_must_be_value1", "Configured default for '{value0}' must be {value1}.", { value0: name, value1: requirement }));
  }
  if (type === "select" && !options.includes(stringFromUnknownValue(configured))) {
    throw new Error(t("react.workflows.configured_default_for_value0_must_be_a_declared_option", "Configured default for '{value0}' must be a declared option.", { value0: name }));
  }
  payload.default = configured;
  return payload;
}

export function workflowFieldPayload(
  rows: WorkflowInterfaceFieldDraft[],
  t: I18nContextValue["t"] = fallbackContext.t,
): Array<Record<string, unknown>> {
  return rows.map((row) => workflowFieldPayloadRow(row, t));
}

export function workflowOutputFieldPayload(
  rows: WorkflowInterfaceFieldDraft[],
  t: I18nContextValue["t"] = fallbackContext.t,
): Array<Record<string, unknown>> {
  return workflowFieldPayload(rows, t)
    .filter((row) => !["secret", "file"].includes(textOrEmpty(row.type)))
    .map((source) => {
      const row = { ...source };
      delete row.default;
      delete row.default_configured;
      return row;
    });
}

export function stepOverrideObjectFromDraft(value: string, t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  try {
    return jsonObjectFromDraft(value, t);
  } catch {
    return {};
  }
}

export function csvNumbers(value: unknown, fallback: number[] = []): number[] {
  const source = Array.isArray(value) ? value.join(", ") : textOrEmpty(value);
  const parsed = source
    .split(",")
    .map((item) => Number.parseInt(item.trim(), 10))
    .filter((item) => Number.isFinite(item) && item >= 100 && item <= 599);
  return parsed.length ? parsed : fallback;
}

export function configEntries(value: unknown): Array<{ key: string; value: string }> {
  const row = objectOrEmpty(value);
  return Object.entries(row).map(([key, entryValue]) => ({
    key,
    value:
      typeof entryValue === "string" ||
      typeof entryValue === "number" ||
      typeof entryValue === "boolean"
        ? stringFromUnknownValue(entryValue)
        : JSON.stringify(entryValue, null, 2),
  }));
}

export function valueFromMapEditorText(value: string): unknown {
  const text = firstStringFromUnknownValue([value, ""]).trim();
  if (!text) {
    return "";
  }
  if (text === "true") {
    return true;
  }
  if (text === "false") {
    return false;
  }
  if (text === "null") {
    return null;
  }
  if (/^-?\d+(\.\d+)?$/.test(text)) {
    return Number(text);
  }
  if (text.startsWith("{") || text.startsWith("[")) {
    try {
      return JSON.parse(text);
    } catch {
      return value;
    }
  }
  return value;
}

export function objectFromEntries(
  rows: Array<{ key: string; value: string }>,
): Record<string, unknown> {
  return rows.reduce<Record<string, unknown>>((next, row) => {
    const key = textOrEmpty(row.key);
    if (key) {
      next[key] = valueFromMapEditorText(row.value);
    }
    return next;
  }, {});
}

/** A row of a config map editor: what is typed, kept even while it cannot be saved. */
export type DraftMapRow = { id: string; key: string; value: string };

let draftMapRowSequence = 0;

/** A new draft row; its id stays with it while its fields are typed into. */
export function newDraftMapRow(key = "", value = ""): DraftMapRow {
  draftMapRowSequence += 1;
  return { id: `draft-map-row-${draftMapRowSequence}`, key, value };
}

/** What a map keeps of its draft rows: those with a key, the first of rows with equal keys. */
export function storableDraftMapRows(rows: DraftMapRow[]): Array<{ key: string; value: string }> {
  const seen = new Set<string>();
  return rows.flatMap((row) => {
    const key = row.key.trim();
    if (!key || seen.has(key)) return [];
    seen.add(key);
    return [{ key, value: row.value }];
  });
}

/** Why a draft row is not saved: no key, or a key an earlier row already uses. */
export function draftMapRowIssue(rows: DraftMapRow[], index: number): "" | "missing_key" | "duplicate_key" {
  const row = rows[index];
  const key = row.key.trim();
  if (!key) return row.value.trim() ? "missing_key" : "";
  return rows.slice(0, index).some((earlier) => earlier.key.trim() === key) ? "duplicate_key" : "";
}
