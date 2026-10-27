import type { WorkflowStepRow } from "@/pages/workflows/detail/detail-core";
import {
  IF_CONSTANT_OPERATORS,
  IF_OPERATOR_OPTIONS,
  STEP_KIND_OPTIONS,
} from "@/pages/workflows/detail/detail-automation-core";
import {
  jsonText,
  objectOrEmpty,
  stepOverrideObjectFromDraft,
  valueFromMapEditorText,
  type ConnectorInputMode,
  type ConnectorInputMappingRow,
} from "@/pages/workflows/detail/detail-interface";
import { textOrEmpty } from "@/pages/workflows/detail/detail-test-run";
import { stringList } from "@/pages/workflows/detail/detail-workflow-validation";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { trimRepeatedEdges } from "@/lib/utils/string-normalization";

import type { IfConditionDraft } from "@/pages/workflows/detail/detail-if-condition-draft";
export type { IfConditionDraft } from "@/pages/workflows/detail/detail-if-condition-draft";
export {
  ifConditionRawSourceText,
  isLikelyIfSelector,
  unsupportedIfConditionDraft,
  ifConditionDraftFromUnknown,
} from "@/pages/workflows/detail/detail-if-condition-draft";

export function isSetSourceCopy(value: unknown): value is { source: string } {
  return value !== null && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === 1 && typeof (value as Record<string, unknown>).source === "string" &&
    ((value as { source: string }).source.trim() === "state" ||
      (value as { source: string }).source.trim().startsWith("state."));
}

export const MAX_WORKFLOW_BATCH_SIZE = 1000;

export function clampWorkflowBatchSize(value: unknown): number {
  const parsed = Number.parseInt(stringFromUnknownValue(value), 10);
  if (!Number.isFinite(parsed)) {
    return 1;
  }
  return Math.min(MAX_WORKFLOW_BATCH_SIZE, Math.max(1, parsed));
}

export function ifConditionFromDraft(
  draft: IfConditionDraft
): Record<string, unknown> {
  const operator = IF_OPERATOR_OPTIONS.some(
    (option) => option.value === draft.operator,
  )
    ? draft.operator
    : "equals";
  const selector = textOrEmpty(draft.selector);
  if (IF_CONSTANT_OPERATORS.has(operator)) {
    return { operator };
  }
  if (operator === "exists" || operator === "truthy") {
    return { selector, operator };
  }
  let value = valueFromMapEditorText(draft.value);
  if (draft.value.trim().startsWith('"')) {
    try {
      value = JSON.parse(draft.value);
    } catch {
      // An unfinished quoted value remains editable text.
    }
  }
  return { selector, operator, value };
}

export type SwitchCaseRow = {
  case_id: string;
  when: string;
  label: string;
};

/** Switch cases as the editor rows: a stable case_id names the port, `when` is the match. */
export function switchCaseRows(value: unknown): SwitchCaseRow[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.reduce<SwitchCaseRow[]>((rows, item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return rows;
    }
    const row = item as Record<string, unknown>;
    rows.push({
      case_id: textOrEmpty(row.case_id),
      when: row.when === undefined || row.when === null ? "" : stringFromUnknownValue(row.when),
      label: textOrEmpty(row.label),
    });
    return rows;
  }, []);
}

/** A case id not used by the switch yet. It never changes once the case exists. */
export function nextSwitchCaseId(rows: SwitchCaseRow[]): string {
  const taken = new Set(rows.map((row) => row.case_id));
  for (let index = rows.length + 1; index < rows.length + 1000; index += 1) {
    const candidate = `case_${index}`;
    if (!taken.has(candidate)) {
      return candidate;
    }
  }
  return `case_${Date.now()}`;
}

export function switchCasesFromRows(rows: SwitchCaseRow[]): Array<Record<string, unknown>> {
  return rows.map((row) => {
    const next: Record<string, unknown> = { case_id: row.case_id, when: row.when };
    if (row.label.trim()) {
      next.label = row.label.trim();
    }
    return next;
  });
}

export function contractItemName(value: unknown): string {
  const row = objectOrEmpty(value);
  return textOrEmpty(row.name || row.key || row.id || row.field);
}

export function contractItemType(value: unknown): string {
  const row = objectOrEmpty(value);
  return textOrEmpty(row.type || row.value_type || row.schema_type) || "value";
}

export function contractItemDescription(value: unknown): string {
  const row = objectOrEmpty(value);
  return textOrEmpty(row.description || row.help || row.summary);
}

export function contractRows(value: unknown): Array<Record<string, unknown>> {
  if (Array.isArray(value)) {
    return value.filter(
      (item): item is Record<string, unknown> =>
        Boolean(item) && typeof item === "object" && !Array.isArray(item),
    );
  }
  const row = objectOrEmpty(value);
  const properties = objectOrEmpty(row.properties);
  if (Object.keys(properties).length) {
    const required = new Set(stringList(row.required));
    return Object.entries(properties).map(([name, schema]) => ({
      ...objectOrEmpty(schema),
      name,
      required: required.has(name),
    }));
  }
  return contractItemName(row) ? [row] : [];
}

export function decodeConnectorInputValue(value: unknown): {
  mode: ConnectorInputMode;
  value: string;
} {
  if (value === null || value === undefined) {
    return { mode: "state", value: "" };
  }
  if (typeof value !== "string") {
    return {
      mode: "literal",
      value:
        typeof value === "number" || typeof value === "boolean"
          ? stringFromUnknownValue(value)
          : JSON.stringify(value ?? ""),
    };
  }
  const text = textOrEmpty(value);
  if (text.startsWith("literal:")) {
    return { mode: "literal", value: text.slice("literal:".length) };
  }
  // A state path being typed ("s", "sta", "state.") is still one (item 11): it read back
  // as a Raw string on the first letter, and the State path field could not be typed in.
  if (text.startsWith("state.") || "state.".startsWith(text)) {
    return { mode: "state", value: text };
  }
  if (!text) {
    return { mode: "state", value: "" };
  }
  return { mode: "expression", value: text };
}

export function encodeConnectorInputValue(row: ConnectorInputMappingRow): string {
  const value = textOrEmpty(row.value);
  if (row.mode === "literal") {
    return `literal:${value}`;
  }
  return value;
}

// AC17-R65: inputs the platform supplies, which a step author must never be asked for.
// `connection_ref` is resolved from the chosen connection and injected by the runtime at
// core/application/orchestration/connector_step_runtime.py:2053, after the author's own
// mapping is spread, so anything typed here is overwritten before the extension sees it.
// Asking for it made the decisive field of the step a free-text box whose key had to be
// guessed and whose value is an opaque conn_... identifier.
const PLATFORM_OWNED_CONNECTOR_INPUTS = new Set(["connection_ref"]);

export function isPlatformOwnedConnectorInput(name: unknown): boolean {
  // Narrowing rather than coercing: String() on an unknown turns an object into
  // "[object Object]", which no caller means and which no entry in the set can
  // match. Every caller passes a key that is already a string, so this changes
  // no answer -- it removes a way of asking the question that never made sense.
  return PLATFORM_OWNED_CONNECTOR_INPUTS.has(
    (typeof name === "string" ? name : "").trim(),
  );
}

export function withoutPlatformOwnedConnectorInputs(
  mapping: Record<string, unknown>,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(mapping).filter(([key]) => !isPlatformOwnedConnectorInput(key)),
  );
}

function connectorInputRow(key: string, value: unknown, present: boolean): ConnectorInputMappingRow {
  const row: ConnectorInputMappingRow = { key, ...decodeConnectorInputValue(value) };
  if (present && (value === null || value === "")) row.retainedValue = { key, value };
  return row;
}

export function connectorInputMappingRows(
  mapping: unknown,
  parameters: Array<Record<string, unknown>>,
): ConnectorInputMappingRow[] {
  const mappingObject = objectOrEmpty(mapping);
  const rows: ConnectorInputMappingRow[] = [];
  const seen = new Set<string>();
  parameters.forEach((parameter) => {
    const key = contractItemName(parameter);
    if (!key || isPlatformOwnedConnectorInput(key)) {
      return;
    }
    if (!(key in mappingObject) && parameter.required !== true) {
      return;
    }
    seen.add(key);
    rows.push(connectorInputRow(key, mappingObject[key], Object.hasOwn(mappingObject, key)));
  });
  Object.entries(mappingObject).forEach(([key, value]) => {
    if (seen.has(key) || isPlatformOwnedConnectorInput(key)) {
      return;
    }
    rows.push(connectorInputRow(key, value, true));
  });
  return rows.length ? rows : [{ key: "", mode: "state", value: "" }];
}

export function missingRequiredConnectorInputNames(
  mapping: unknown,
  parameters: Array<Record<string, unknown>>,
): string[] {
  const mappingObject = objectOrEmpty(mapping);
  return parameters.reduce<string[]>((missing, parameter) => {
    const key = contractItemName(parameter);
    if (!key || parameter.required !== true) {
      return missing;
    }
    const value = mappingObject[key];
    if (
      value === null
      || value === undefined
      || (typeof value === "string"
        && (!value.trim()
          || (value.startsWith("literal:") && !value.slice("literal:".length).trim())))
    ) {
      missing.push(key);
    }
    return missing;
  }, []);
}

export function connectorInputMappingFromRows(
  rows: ConnectorInputMappingRow[],
): Record<string, unknown> {
  const entries: Array<[string, unknown]> = [];
  rows.forEach((row) => {
    const key = textOrEmpty(row.key);
    if (!key || isPlatformOwnedConnectorInput(key)) return;
    const retained = row.retainedValue;
    const original = retained ? decodeConnectorInputValue(retained.value) : null;
    if (retained?.key === key && original?.mode === row.mode && original.value === row.value) {
      entries.push([key, retained.value]);
      return;
    }
    const encodedValue = encodeConnectorInputValue(row);
    if (encodedValue || row.mode === "literal") entries.push([key, encodedValue]);
  });
  return Object.fromEntries(entries);
}

export function connectorOutputMappingRows(
  mapping: unknown,
  outputs: Array<Record<string, unknown>>,
): Array<{ key: string; value: string }> {
  const mappingObject = objectOrEmpty(mapping);
  const rows: Array<{ key: string; value: string }> = [];
  const seen = new Set<string>();
  outputs.forEach((output) => {
    const key = contractItemName(output);
    if (!key || !(key in mappingObject)) {
      return;
    }
    seen.add(key);
    rows.push({
      key,
      value: textOrEmpty(mappingObject[key]),
    });
  });
  Object.entries(mappingObject).forEach(([key, value]) => {
    if (seen.has(key) || isPlatformOwnedConnectorInput(key)) {
      return;
    }
    rows.push({ key, value: textOrEmpty(value) });
  });
  return rows.length ? rows : [{ key: "", value: "" }];
}

export function connectorDefaultOutputMappingForOperation(
  operation: unknown,
): Record<string, string> {
  const operationRow = objectOrEmpty(operation);
  return contractRows(operationRow.returns || operationRow.outputs).reduce<
    Record<string, string>
  >((next, output) => {
    const key = contractItemName(output);
    if (key && output.required === true) {
      next[key] = `state.${key}`;
    }
    return next;
  }, {});
}

export function mappingObjectFromRaw(
  raw: Record<string, unknown>,
  primaryField: string,
  fallbackField: string,
): Record<string, unknown> {
  const primary = objectOrEmpty(raw[primaryField]);
  if (Object.keys(primary).length) {
    return primary;
  }
  return objectOrEmpty(raw[fallbackField]);
}

/** Match the existing runtime precedence before consuming imported input aliases. */
export function connectorInputMappingForEdit(rawStep: Record<string, unknown>): Record<string, unknown> {
  const connector = objectOrEmpty(objectOrEmpty(rawStep.override).connector);
  const candidates = [rawStep.input_mapping_jsonb, connector.input_mapping_jsonb, connector.input_mapping, rawStep.input_mapping];
  const mapping = candidates.map(objectOrEmpty).find((candidate) => Object.keys(candidate).length) || {};
  return withoutPlatformOwnedConnectorInputs(mapping);
}

/** An own canonical field, including {}, is an explicit edit rather than a fallback. */
export function connectorInputMappingForSave(connector: Record<string, unknown>): Record<string, unknown> {
  const mapping = Object.hasOwn(connector, "input_mapping") ? connector.input_mapping : connector.input_mapping_jsonb;
  return withoutPlatformOwnedConnectorInputs(objectOrEmpty(mapping));
}

export function withTabularWriteFormatDefault(
  config: Record<string, unknown>,
): Record<string, unknown> {
  if (config.operation !== "write" || "format" in config) return config;
  return { ...config, format: "csv" };
}

export function editableOverrideForRawStep(
  rawStep: Record<string, unknown>,
  fallbackStepKind = ""
): Record<string, unknown> {
  const raw = objectOrEmpty(rawStep);
  const override = objectOrEmpty(raw.override);
  const stepKind = textOrEmpty(fallbackStepKind || raw.step_kind);
  if (stepKind === "tabular_feed") {
    return { ...override, tabular_feed: withTabularWriteFormatDefault(objectOrEmpty(override.tabular_feed)) };
  }
  // Legacy target/value and assignments are both valid authored Set variable shapes.
  // Hydrating the editor preserves their exact property presence and typed values.
  if (stepKind === "http_request") {
    // Backfill partial http_request configurations for editing (BUG-4).
    // Stored values always win over defaults, including null and blank limits.
    const httpDefaults = objectOrEmpty(
      stepOverrideObjectFromDraft(defaultOverrideForStepKind("http_request"))
        .http_request,
    );
    const storedHttpConfig = objectOrEmpty(override.http_request);
    const editableHttpConfig = {
      ...httpDefaults,
      // Omitted limits use the stored mode's runtime default. The stored
      // spread still preserves explicit null, blank and custom limits.
      max_response_bytes:
        textOrEmpty(storedHttpConfig.response_mode).trim().toLowerCase() === "artifact"
          ? 100 * 1024 * 1024
          : httpDefaults.max_response_bytes,
      ...storedHttpConfig,
    };
    return {
      ...override,
      http_request: editableHttpConfig,
    };
  }
  if (stepKind === "connector") {
    const connector = objectOrEmpty(override.connector);
    const connectorId =
      textOrEmpty(connector.connector_id) || textOrEmpty(raw.connector_id);
    const derivedExtensionId = connectorId.startsWith("extension.")
      ? connectorId.slice("extension.".length)
      : "";
    const inputMapping = connectorInputMappingForEdit(raw);
    const editableConnector = { ...connector };
    delete editableConnector.input_mapping_jsonb;
    const outputMapping = Object.keys(objectOrEmpty(connector.output_mapping))
      .length
      ? objectOrEmpty(connector.output_mapping)
      : mappingObjectFromRaw(raw, "output_mapping_jsonb", "output_mapping");
    const connectionBinding = Object.keys(
      objectOrEmpty(connector.connection_binding),
    ).length
      ? objectOrEmpty(connector.connection_binding)
      : mappingObjectFromRaw(
          raw,
          "connection_binding_jsonb",
          "connection_binding",
        );
    return {
      ...override,
      connector: {
        ...editableConnector,
        connector_id: connectorId,
        connector_install_id:
          textOrEmpty(connector.connector_install_id) ||
          textOrEmpty(raw.connector_install_id),
        extension_id:
          textOrEmpty(connector.extension_id) || derivedExtensionId,
        operation_id:
          textOrEmpty(connector.operation_id) ||
          textOrEmpty(connector.function_name) ||
          textOrEmpty(connector.action_id) ||
          textOrEmpty(raw.operation_id),
        input_mapping: inputMapping,
        output_mapping: outputMapping,
        connection_binding: connectionBinding,
      },
    };
  }
  return override;
}

export function editableOverrideForStep(
  step: WorkflowStepRow
): Record<string, unknown> {
  return editableOverrideForRawStep(objectOrEmpty(step.raw), step.stepKind);
}

export function overrideForStepSave(
  override: Record<string, unknown>,
  stepKind: string,
): Record<string, unknown> {
  if (stepKind === "tabular_feed") {
    const config = withTabularWriteFormatDefault(objectOrEmpty(override.tabular_feed));
    return {
      ...override,
      tabular_feed: config.operation === "create_dataset" && !("rows" in config)
        ? { ...config, rows: [] }
        : config,
    };
  }
  if (stepKind === "http_request") {
    return override;
  }
  if (stepKind === "connector") {
    const runtimeConnectorConfig = { ...objectOrEmpty(override.connector) };
    delete runtimeConnectorConfig.connector_id;
    delete runtimeConnectorConfig.connector_install_id;
    if (Object.hasOwn(runtimeConnectorConfig, "input_mapping") || Object.hasOwn(runtimeConnectorConfig, "input_mapping_jsonb")) {
      runtimeConnectorConfig.input_mapping = connectorInputMappingForSave(runtimeConnectorConfig);
    }
    delete runtimeConnectorConfig.input_mapping_jsonb;
    return {
      ...override,
      connector: runtimeConnectorConfig,
    };
  }
  return override;
}

export function defaultOverrideForStepKind(stepKind: string): string {
  if (stepKind === "if") {
    return jsonText({
      if: {
        condition: { selector: "", operator: "equals", value: "" },
      },
    });
  }
  if (stepKind === "switch") {
    return jsonText({
      switch: {
        selector: "",
        cases: [{ case_id: "case_1", when: "" }],
      },
    });
  }
  if (stepKind === "approval") {
    return jsonText({
      approval: {
        message: "",
        show_outputs: [],
      },
    });
  }
  if (stepKind === "wait") {
    return jsonText({
      wait: {
        duration_ms: 60_000,
      },
    });
  }
  if (stepKind === "stop_success" || stepKind === "stop_fail") {
    return jsonText({
      [stepKind]: {
        message: "",
      },
    });
  }
  if (stepKind === "set_variable") {
    return jsonText({
      set_variable: {
        assignments: {},
      },
    });
  }
  if (stepKind === "collect") {
    return jsonText({
      collect: {
        target: "",
        mode: "append",
        source: "",
      },
    });
  }
  if (stepKind === "batch") {
    return jsonText({
      batch: {
        source_path: "",
        result_target: "",
        batch_size: 10,
        emit_partial_batch: true,
      },
    });
  }
  if (stepKind === "http_request") {
    return jsonText({
      http_request: {
        method: "GET",
        url: "",
        headers: {},
        query_params: {},
        body_template: "",
        response_target: "",
        response_mode: "inline",
        max_response_bytes: 5 * 1024 * 1024,
        auth_mode: "none",
        auth_ref: "",
        timeout_seconds: 30,
        expected_statuses: [200],
      },
    });
  }
  if (stepKind === "transform") {
    return jsonText({
      transform: {
        // Starts empty: a sample path here reads like a real value and fails on Apply (R2-20).
        input: { source: "" },
        mode: "object",
        result_target: "state.transform_result",
        output_mode: "mapped_only",
        fields: [{ from: "", to: "", type: "text", actions: [] }],
      },
    });
  }
  if (stepKind === "file_from_base64") {
    return jsonText({
      file_from_base64: {
        source: "",
        filename: "",
        result_target: "state.outputs.generated_file",
      },
    });
  }
  if (stepKind === "template") {
    return jsonText({
      template: {
        text: "",
        format: "text",
        inputs: [],
        result_target: "state.outputs.text",
      },
    });
  }
  if (stepKind === "tabular_feed") {
    return jsonText({
      tabular_feed: {
        operation: "read",
        input: { artifact_handle: "" },
        result_target: "state.tabular_feed_result",
      },
    });
  }
  if (stepKind === "connector") {
    return jsonText({
      connector: {
        operation_id: "",
        input_mapping: {},
        connection_binding: {},
      },
    });
  }
  if (stepKind === "dataset_query") {
    return jsonText({
      dataset_query: {
        query_template_id: "",
        params: {},
        row_limit: 100,
        timeout_seconds: 30,
        result_target: "state.dataset_query_result",
      },
    });
  }
  if (stepKind === "invoke_workflow") {
    return jsonText({
      invoke_workflow: {
        workflow_id: "",
        input_mapping: {},
        output_mapping: {},
      },
    });
  }
  return "";
}

/**
 * The override draft after the step kind changes to `nextKind`.
 *
 * Each step kind keeps its config under a key named after the kind
 * (`set_variable`, `if`, ...). The previous kind's block is dropped, because the
 * server refuses an override key the new kind never reads ("Unknown step
 * override key 'set_variable'"). Cross-cutting keys (tools, extra_prompt,
 * config, ...) survive the switch, and a kind with its own block gets its
 * default when it has none yet.
 */
export function overrideDraftForStepKindChange(currentDraft: string, nextKind: string): string {
  const kindKeys = new Set(STEP_KIND_OPTIONS.map((option) => option.value));
  const kept = Object.fromEntries(
    Object.entries(stepOverrideObjectFromDraft(currentDraft))
      .filter(([key]) => key === nextKind || !kindKeys.has(key)),
  );
  const defaults = stepOverrideObjectFromDraft(defaultOverrideForStepKind(nextKind));
  const merged = { ...defaults, ...kept };
  return Object.keys(merged).length ? jsonText(merged) : "";
}

export function stepIdFromLabel(value: string): string {
  const token = firstStringFromUnknownValue([value, ""])
    .trim()
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, "_");
  const normalized = trimRepeatedEdges(token, "_");
  return normalized || `step_${Date.now()}`;
}
