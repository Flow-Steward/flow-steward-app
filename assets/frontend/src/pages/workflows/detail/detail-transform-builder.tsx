import { useI18n } from "@/lib/i18n/useI18n";
import { RequiredLabel } from "@/components/shared/design-system";
import { transformRequiredFieldErrors } from "./detail-transform-fields";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useId, useMemo, useState } from "react";

import {
  previewWorkflowTransform,
  type TransformActionContract,
  type TransformPreviewResponse,
} from "@/pages/workflows/shared/api";
import {
  listFromUnknown,
  objectOrEmpty,
  textOrEmpty,
  workflowVariableSuggestionPanelId,
} from "@/pages/workflows/detail/detail-helpers";
import { valueFromMapEditorText } from "@/pages/workflows/detail/detail-interface";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { useStableRowKeys } from "@/lib/utils/stable-row-keys";

type TransformBuilderContext = Record<string, any>;

type ParamMode = "current" | "literal" | "selector";

const INPUT_CLASS =
  "fs-input mt-1 w-full";
const SMALL_BUTTON_CLASS =
  "fs-button secondary sm";
const DANGER_BUTTON_CLASS =
  "fs-button danger sm";
const VALUE_TYPES = ["text", "number", "boolean", "object", "array", "json"];
const ROW_FILTER_OPERATORS = [
  "equals",
  "not_equals",
  "exists",
  "truthy",
  "gt",
  "gte",
  "lt",
  "lte",
  "contains",
  "in",
  "not_in",
];

function stringifyForEditor(value: unknown): string {
  if (value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return stringFromUnknownValue(value);
  }
}

function parseEditorValue(value: string): unknown {
  return valueFromMapEditorText(value);
}

function transformFields(value: unknown): Array<Record<string, unknown>> {
  const rows = listFromUnknown(value);
  if (rows.length) {
    return rows;
  }
  return [{ from: "", to: "", type: "text", actions: [] }];
}

function actionRows(value: unknown): Array<Record<string, unknown>> {
  return listFromUnknown(value).map((row) => ({
    action: textOrEmpty(row.action),
    params: objectOrEmpty(row.params),
  }));
}

function isSourceRef(value: unknown): value is Record<string, unknown> {
  const row = objectOrEmpty(value);
  const keys = ["current", "literal", "selector"].filter((key) => key in row);
  return keys.length === 1;
}

function paramMode(value: unknown): ParamMode {
  if (!isSourceRef(value)) {
    return "literal";
  }
  if ("selector" in value) {
    return "selector";
  }
  if ("current" in value) {
    return "current";
  }
  return "literal";
}

function paramEditorText(value: unknown): string {
  if (isSourceRef(value)) {
    if ("selector" in value) {
      return textOrEmpty(value.selector);
    }
    if ("literal" in value) {
      return stringifyForEditor(value.literal);
    }
    return "";
  }
  return stringifyForEditor(value);
}

function fieldActionKey(field: Record<string, unknown>): "actions" | "actions_each" {
  return textOrEmpty(field.from).includes("[*]") ? "actions_each" : "actions";
}

function fieldDeclaredPipelineType(field: Record<string, unknown>): string {
  if (fieldActionKey(field) === "actions_each") {
    return textOrEmpty(field.item_type) || "json";
  }
  return textOrEmpty(field.type) || "json";
}

function typeCompatible(
  inputType: string,
  valueType: string,
  options: { allowUnknownValue?: boolean } = {},
): boolean {
  const input = textOrEmpty(inputType) || "json";
  const value = textOrEmpty(valueType) || "json";
  return input === "json" || input === value || (options.allowUnknownValue === true && value === "json");
}

function pipelineActionInputType(
  actions: TransformActionContract[],
  rows: Array<Record<string, unknown>>,
  actionIndex: number,
): string {
  if (actionIndex <= 0) {
    return "json";
  }
  const previousActionId = textOrEmpty(rows[actionIndex - 1]?.action);
  const previousAction = actions.find((action) => action.action_id === previousActionId);
  return textOrEmpty(previousAction?.output_type) || "json";
}

function normalizeFieldActionPipeline(
  field: Record<string, unknown>,
): Record<string, unknown> {
  const nextField: Record<string, unknown> = { ...field };
  const key = fieldActionKey(nextField);
  const opposite = key === "actions" ? "actions_each" : "actions";
  if (!(key in nextField)) {
    const carriedRows = actionRows(nextField[opposite]);
    nextField[key] = carriedRows.length ? carriedRows : [];
  }
  delete nextField[opposite];
  return nextField;
}

function sortedActions(actions: TransformActionContract[]): TransformActionContract[] {
  return [...actions].sort((left, right) => {
    const category = left.category.localeCompare(right.category);
    return category || left.display_name.localeCompare(right.display_name);
  });
}

function previewSampleForMode(mode: string): string {
  if (mode === "list") {
    return JSON.stringify([{ name: " Ada ", total: 12.5 }], null, 2);
  }
  return JSON.stringify({ name: " Ada ", total: 12.5 }, null, 2);
}

export function WorkflowTransformBuilder({ ctx }: Readonly<{ ctx: TransformBuilderContext }>) {
  const { t } = useI18n();
  const {
    onStepInputValidationChange,
    renderWorkflowVariableInput,
    renderWorkflowDestinationInput,
    replaceStepKindConfigField,
    stepKindConfig,
    transformActionsQuery,
    updateStepKindConfig,
    workflowId,
  } = ctx;
  const fieldId = useId();
  const sourceErrorId = `${fieldId}-source-error`;
  const targetErrorId = `${fieldId}-target-error`;
  const { source: sourceError = "", result_target: targetError = "" } = transformRequiredFieldErrors(objectOrEmpty(stepKindConfig), t);
  useEffect(() => {
    onStepInputValidationChange?.(sourceErrorId, sourceError);
    onStepInputValidationChange?.(targetErrorId, targetError);
  }, [onStepInputValidationChange, sourceErrorId, sourceError, targetErrorId, targetError]);
  useEffect(() => () => {
    onStepInputValidationChange?.(sourceErrorId, "");
    onStepInputValidationChange?.(targetErrorId, "");
  }, [onStepInputValidationChange, sourceErrorId, targetErrorId]);
  const actions = useMemo(
    () => sortedActions(transformActionsQuery?.data?.actions || []),
    [transformActionsQuery?.data?.actions],
  );
  const [sampleJson, setSampleJson] = useState(() =>
    previewSampleForMode(textOrEmpty(stepKindConfig.mode) || "object"),
  );
  const [preview, setPreview] = useState<TransformPreviewResponse | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [previewPending, setPreviewPending] = useState(false);

  const fields = transformFields(stepKindConfig.fields);
  const rowFilter = objectOrEmpty(stepKindConfig.row_filter);
  const rowFilterSelector = textOrEmpty(rowFilter.selector).trim();

  function updateRowFilter(patch: Record<string, unknown>) {
    const next = { ...rowFilter, ...patch };
    // A row filter needs a selector: without one there is no filter, and the server
    // rejects a half-filled one. The operator shown as "equals" is stored as such.
    replaceStepKindConfigField(
      "transform",
      "row_filter",
      textOrEmpty(next.selector).trim() ? { operator: "equals", ...next } : undefined,
    );
  }
  const fieldKeyState = useStableRowKeys(fields.length, "transform-field");

  function updateFields(nextFields: Array<Record<string, unknown>>) {
    replaceStepKindConfigField("transform", "fields", nextFields);
  }

  function updateField(index: number, patch: Record<string, unknown>) {
    updateFields(
      fields.map((field, rowIndex) =>
        rowIndex === index ? normalizeFieldActionPipeline({ ...field, ...patch }) : field,
      ),
    );
  }

  function replaceField(index: number, nextField: Record<string, unknown>) {
    updateFields(
      fields.map((field, rowIndex) =>
        rowIndex === index ? normalizeFieldActionPipeline(nextField) : field,
      ),
    );
  }

  function removeField(index: number) {
    fieldKeyState.remove(index);
    const nextFields = fields.filter((_, rowIndex) => rowIndex !== index);
    updateFields(nextFields.length ? nextFields : [{ from: "", to: "", type: "text", actions: [] }]);
  }

  function moveField(index: number, direction: "down" | "up") {
    const target = direction === "up" ? index - 1 : index + 1;
    if (target < 0 || target >= fields.length) {
      return;
    }
    const nextFields = fields.slice();
    const [moved] = nextFields.splice(index, 1);
    nextFields.splice(target, 0, moved);
    fieldKeyState.move(index, target);
    updateFields(nextFields);
  }

  function updateFieldActions(index: number, nextActions: Array<Record<string, unknown>>) {
    const field = fields[index] || {};
    const key = fieldActionKey(field);
    const opposite = key === "actions" ? "actions_each" : "actions";
    const nextField = { ...field, [key]: nextActions };
    delete nextField[opposite];
    replaceField(index, nextField);
  }

  function addAction(index: number) {
    const field = fields[index] || {};
    const nextActions = actionRows(field[fieldActionKey(field)]);
    const inputType = pipelineActionInputType(actions, nextActions, nextActions.length);
    const preferredType = nextActions.length === 0 ? fieldDeclaredPipelineType(field) : inputType;
    const match =
      actions.find((action) => typeCompatible(action.input_type, preferredType)) ||
      actions.find((action) =>
        typeCompatible(action.input_type, inputType, { allowUnknownValue: nextActions.length === 0 }),
      );
    nextActions.push({ action: match?.action_id || "", params: {} });
    updateFieldActions(index, nextActions);
  }

  function updateAction(
    fieldIndex: number,
    actionIndex: number,
    patch: Record<string, unknown>,
  ) {
    const field = fields[fieldIndex] || {};
    const rows = actionRows(field[fieldActionKey(field)]);
    updateFieldActions(
      fieldIndex,
      rows.map((row, rowIndex) => (rowIndex === actionIndex ? { ...row, ...patch } : row)),
    );
  }

  function moveAction(fieldIndex: number, actionIndex: number, direction: "down" | "up") {
    const field = fields[fieldIndex] || {};
    const rows = actionRows(field[fieldActionKey(field)]);
    const target = direction === "up" ? actionIndex - 1 : actionIndex + 1;
    if (target < 0 || target >= rows.length) {
      return;
    }
    const nextRows = rows.slice();
    const [moved] = nextRows.splice(actionIndex, 1);
    nextRows.splice(target, 0, moved);
    updateFieldActions(fieldIndex, nextRows);
  }

  function duplicateAction(fieldIndex: number, actionIndex: number) {
    const field = fields[fieldIndex] || {};
    const rows = actionRows(field[fieldActionKey(field)]);
    const duplicate = structuredClone(rows[actionIndex] || {});
    rows.splice(actionIndex + 1, 0, duplicate);
    updateFieldActions(fieldIndex, rows);
  }

  function removeAction(fieldIndex: number, actionIndex: number) {
    const field = fields[fieldIndex] || {};
    const rows = actionRows(field[fieldActionKey(field)]);
    updateFieldActions(
      fieldIndex,
      rows.filter((_, rowIndex) => rowIndex !== actionIndex),
    );
  }

  function updateActionParam(
    fieldIndex: number,
    actionIndex: number,
    paramName: string,
    value: unknown,
  ) {
    const field = fields[fieldIndex] || {};
    const rows = actionRows(field[fieldActionKey(field)]);
    const row = rows[actionIndex] || {};
    const params = { ...objectOrEmpty(row.params), [paramName]: value };
    updateAction(fieldIndex, actionIndex, { params });
  }

  async function runPreview( ) {
    setPreview(null);
    setPreviewError("");
    let sampleData: unknown;
    try {
      sampleData = JSON.parse(sampleJson || "null");
    } catch {
      setPreviewError(t("react.workflows.sample_data_must_be_valid_json", "Sample data must be valid JSON."));
      return;
    }
    setPreviewPending(true);
    try {
      const response = await previewWorkflowTransform(
        textOrEmpty(workflowId),
        objectOrEmpty(stepKindConfig),
        sampleData,
      );
      setPreview(response);
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : t("react.workflows.preview_failed", "Preview failed."));
    } finally {
      setPreviewPending(false);
    }
  }

  const previewObject = objectOrEmpty(preview);
  const previewErrors = Array.isArray(previewObject.errors) ? previewObject.errors : [];
  const previewErrorKeyState = useStableRowKeys(
    previewErrors.length,
    "transform-preview-error",
  );
  const hasPreview = preview !== null;
  const previewOk = hasPreview && previewObject.ok !== false;

  return (
    <div className="space-y-4 rounded-xl border border-default fs-surface-inset p-3">
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="block text-xs fs-ink-secondary">
          <RequiredLabel>{t("react.workflows.source", "Source")}</RequiredLabel>
          {renderWorkflowVariableInput({
            ariaDescribedBy: sourceError ? sourceErrorId : undefined,
            ariaInvalid: Boolean(sourceError),
            ariaLabel: t("react.workflows.transform_source", "Transform source"),
            ariaRequired: true,
            className: INPUT_CLASS,
            onChange: (nextValue: string) =>
              updateStepKindConfig("transform", {
                input: { ...objectOrEmpty(stepKindConfig.input), source: nextValue },
              }),
            // Its own panel: without one, both of these opened at once (item 11).
            panelId: workflowVariableSuggestionPanelId("transform-source"),
            placeholder: "state.inputs.payload",
            value: stringFromUnknownValue(objectOrEmpty(stepKindConfig.input).source),
          })}
          {sourceError ? <span className="fs-field-helper error block" id={sourceErrorId} role="alert">{sourceError}</span> : null}
        </label>
        <label className="block text-xs fs-ink-secondary">
          <RequiredLabel>{t("react.workflows.result_target_variant_6c946b46", "Result target")}</RequiredLabel>
          {renderWorkflowDestinationInput({
            ariaDescribedBy: targetError ? targetErrorId : undefined,
            ariaInvalid: Boolean(targetError),
            ariaLabel: t("react.workflows.transform_result_target", "Transform result target"),
            ariaRequired: true,
            className: INPUT_CLASS,
            onChange: (nextValue: string) =>
              replaceStepKindConfigField("transform", "result_target", nextValue),
            panelId: workflowVariableSuggestionPanelId("transform-result-target"),
            placeholder: "state.normalized_payload",
            value: stringFromUnknownValue(stepKindConfig.result_target),
          })}
          {targetError ? <span className="fs-field-helper error block" id={targetErrorId} role="alert">{targetError}</span> : null}
        </label>
        <label className="block text-xs fs-ink-secondary">
          <span>{t("registry.workflow_detail.run_lab.test_run.mode", "Mode")}</span>
          <select
            className={INPUT_CLASS}
            onChange={(event) => {
              replaceStepKindConfigField("transform", "mode", event.target.value);
              setSampleJson(previewSampleForMode(event.target.value));
            }}
            value={textOrEmpty(stepKindConfig.mode) || "object"}
          >
            <option value="object">{t("react.workflows.object_variant_62a6da87", "Object")}</option>
            <option value="list">{t("react.workflows.list", "List")}</option>
          </select>
        </label>
        <label className="block text-xs fs-ink-secondary">
          <span>{t("registry.tool_detail.schemas.output", "Output")}</span>
          <select
            className={INPUT_CLASS}
            onChange={(event) =>
              replaceStepKindConfigField("transform", "output_mode", event.target.value)
            }
            value={textOrEmpty(stepKindConfig.output_mode) || "mapped_only"}
          >
            <option value="mapped_only">{t("react.workflows.mapped_only", "Mapped only")}</option>
            <option value="merge_with_input">{t("react.workflows.merge_with_input", "Merge with input")}</option>
          </select>
        </label>
      </div>

      {textOrEmpty(stepKindConfig.mode) === "list" ? (
        <div className="grid gap-2 rounded-lg border border-default fs-surface-inset p-3 sm:grid-cols-3">
          <label className="block text-xs fs-ink-secondary">
            <span>{t("react.workflows.row_selector", "Row selector")}</span>
            <input
              className={INPUT_CLASS}
              onChange={(event) =>
                updateRowFilter({ selector: event.target.value })
              }
              placeholder="status"
              value={textOrEmpty(rowFilter.selector)}
            />
          </label>
          <label className="block text-xs fs-ink-secondary">
            <span>{t("users.role.operator", "Operator")}</span>
            <select
              className={INPUT_CLASS}
              onChange={(event) =>
                updateRowFilter({ operator: event.target.value })
              }
              disabled={!rowFilterSelector}
              value={textOrEmpty(rowFilter.operator) || "equals"}
            >
              {ROW_FILTER_OPERATORS.map((operator) => (
                <option key={operator} value={operator}>
                  {operator}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-xs fs-ink-secondary">
            <span>{t("project_edit.fields.value", "Value")}</span>
            <input
              className={INPUT_CLASS}
              onChange={(event) =>
                updateRowFilter({ value: parseEditorValue(event.target.value) })
              }
              disabled={!rowFilterSelector}
              value={stringifyForEditor(rowFilter.value)}
            />
          </label>
        </div>
      ) : null}

      <div className="space-y-3">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">
            {t("react.workflows.fields", "Fields")}</p>
          <button
            className={SMALL_BUTTON_CLASS}
            onClick={() => {
              fieldKeyState.append();
              updateFields([...fields, { from: "", to: "", type: "text", actions: [] }]);
            }}
            type="button"
          >
            {t("react.workflows.add_field", "Add field")}</button>
        </div>
        {fields.map((field, index) => {
          const sourceMode = "value" in field ? "literal" : "source";
          const fieldType = textOrEmpty(field.type) || "text";
          const pipelineKey = fieldActionKey(field);
          const rows = actionRows(field[pipelineKey]);
          const fieldKey = fieldKeyState.keys[index];
          return (
            <div
              className="space-y-3 rounded-lg border border-default fs-surface-inset p-3"
              key={fieldKey}
            >
              <div className="grid gap-2 md:grid-cols-[120px_1fr_1fr_120px]">
                <label className="block text-xs fs-ink-secondary">
                  <span>{t("registry.tool_detail.schemas.input", "Input")}</span>
                  <select
                    className={INPUT_CLASS}
                    onChange={(event) => {
                      if (event.target.value === "literal") {
                        const nextField: Record<string, unknown> = {
                          ...field,
                          value: { literal: "" },
                        };
                        delete nextField.from;
                        delete nextField.default;
                        delete nextField.required;
                        replaceField(index, nextField);
                      } else {
                        const nextField: Record<string, unknown> = { ...field, from: "" };
                        delete nextField.value;
                        replaceField(index, nextField);
                      }
                    }}
                    value={sourceMode}
                  >
                    <option value="source">{t("react.workflows.source", "Source")}</option>
                    <option value="literal">{t("react.workflows.literal", "Literal")}</option>
                  </select>
                </label>
                {sourceMode === "literal" ? (
                  <label className="block text-xs fs-ink-secondary">
                    <span>{t("react.workflows.literal", "Literal")}</span>
                    <textarea
                      className="fs-textarea mt-1 w-full min-h-20 font-mono text-xs"
                      onChange={(event) =>
                        updateField(index, { value: { literal: parseEditorValue(event.target.value) } })
                      }
                      value={stringifyForEditor(objectOrEmpty(field.value).literal)}
                    />
                  </label>
                ) : (
                  <label className="block text-xs fs-ink-secondary">
                    <span>{t("react.workflows.from", "From")}</span>
                    <input
                      className={INPUT_CLASS}
                      onChange={(event) => updateField(index, { from: event.target.value })}
                      placeholder="customer.email"
                      value={textOrEmpty(field.from)}
                    />
                  </label>
                )}
                <label className="block text-xs fs-ink-secondary">
                  <span>{t("react.workflows.to", "To")}</span>
                  <input
                    className={INPUT_CLASS}
                    onChange={(event) => updateField(index, { to: event.target.value })}
                    placeholder="customer.email"
                    value={textOrEmpty(field.to)}
                  />
                </label>
                <label className="block text-xs fs-ink-secondary">
                  <span>{t("trash.columns.type", "Type")}</span>
                  <select
                    className={INPUT_CLASS}
                    onChange={(event) => updateField(index, { type: event.target.value })}
                    value={fieldType}
                  >
                    {VALUE_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {type}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              <div className="grid gap-2 sm:grid-cols-3">
                {fieldType === "array" ? (
                  <label className="block text-xs fs-ink-secondary">
                    <span>{t("react.workflows.item_type", "Item type")}</span>
                    <select
                      className={INPUT_CLASS}
                      onChange={(event) => updateField(index, { item_type: event.target.value })}
                      value={textOrEmpty(field.item_type) || "json"}
                    >
                      {VALUE_TYPES.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : null}
                {sourceMode === "source" ? (
                  <>
                    <label className="flex items-center gap-2 pt-5 text-xs fs-ink-secondary">
                      <input
                        checked={Boolean(field.required)}
                        onChange={(event) => updateField(index, { required: event.target.checked })}
                        type="checkbox"
                      />
                      <span>{t("project_edit.fields.required", "Required")}</span>
                    </label>
                    <label className="block text-xs fs-ink-secondary">
                      <span>{t("react.workflows.default_variant_21b111cb", "Default")}</span>
                      <input
                        className={INPUT_CLASS}
                        onChange={(event) => updateField(index, { default: parseEditorValue(event.target.value) })}
                        value={stringifyForEditor(field.default)}
                      />
                    </label>
                  </>
                ) : null}
              </div>

              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-tertiary">
                    {pipelineKey === "actions_each" ? t("react.workflows.actions_each", "Actions each") : t("extensions.table.actions", "Actions")}
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <button
                      className={SMALL_BUTTON_CLASS}
                      disabled={!actions.length}
                      onClick={() => addAction(index)}
                      type="button"
                    >
                      {t("react.workflows.add_action", "Add action")}</button>
                    <button
                      className={SMALL_BUTTON_CLASS}
                      disabled={index === 0}
                      onClick={() => moveField(index, "up")}
                      type="button"
                    >
                      {t("react.workflows.up", "Up")}</button>
                    <button
                      className={SMALL_BUTTON_CLASS}
                      disabled={index >= fields.length - 1}
                      onClick={() => moveField(index, "down")}
                      type="button"
                    >
                      {t("react.workflows.down", "Down")}</button>
                    <button className={DANGER_BUTTON_CLASS} onClick={() => removeField(index)} type="button">
                      {t("react.workflows.delete_field", "Delete field")}</button>
                  </div>
                </div>
                {rows.map((row, actionIndex) => {
                  const actionId = textOrEmpty(row.action);
                  const selectedAction = actions.find((action) => action.action_id === actionId);
                  const params = objectOrEmpty(row.params);
                  const properties = selectedAction?.params_schema?.properties || {};
                  const actionInputType = pipelineActionInputType(actions, rows, actionIndex);
                  const compatibleActions = actions.filter((action) =>
                    typeCompatible(action.input_type, actionInputType, {
                      allowUnknownValue: actionIndex === 0,
                    }),
                  );
                  return (
                    <div
                      className="space-y-2 rounded border border-default fs-surface-inset p-2"
                      key={`${actionIndex}-${actionId || "action"}`}
                    >
                      <div className="grid gap-2 md:grid-cols-[1fr_auto]">
                        <label className="block text-xs fs-ink-secondary">
                          <span>{t("security_overview.columns.action", "Action")}</span>
                          <select
                            className={INPUT_CLASS}
                            onChange={(event) =>
                              updateAction(index, actionIndex, {
                                action: event.target.value,
                                params: {},
                              })
                            }
                            value={actionId}
                          >
                            <option value="">{t("react.workflows.select_action", "Select action")}</option>
                            {compatibleActions.map((action) => (
                              <option key={action.action_id} value={action.action_id}>
                                {action.category} / {action.display_name}
                              </option>
                            ))}
                          </select>
                        </label>
                        <div className="flex flex-wrap items-end gap-2">
                          <button
                            className={SMALL_BUTTON_CLASS}
                            disabled={actionIndex === 0}
                            onClick={() => moveAction(index, actionIndex, "up")}
                            type="button"
                          >
                            {t("react.workflows.up", "Up")}</button>
                          <button
                            className={SMALL_BUTTON_CLASS}
                            disabled={actionIndex >= rows.length - 1}
                            onClick={() => moveAction(index, actionIndex, "down")}
                            type="button"
                          >
                            {t("react.workflows.down", "Down")}</button>
                          <button
                            className={SMALL_BUTTON_CLASS}
                            onClick={() => duplicateAction(index, actionIndex)}
                            type="button"
                          >
                            {t("react.workflows.duplicate", "Duplicate")}</button>
                          <button
                            className={DANGER_BUTTON_CLASS}
                            onClick={() => removeAction(index, actionIndex)}
                            type="button"
                          >
                            {t("settings.model_providers.delete", "Delete")}</button>
                        </div>
                      </div>
                      {Object.entries(properties).length ? (
                        <div className="grid gap-2 md:grid-cols-2">
                          {Object.entries(properties).map(([paramName, schema]) => {
                            const rawValue = params[paramName];
                            const mode = paramMode(rawValue);
                            const sources = selectedAction?.param_sources?.[paramName] || {};
                            const sourceAllowed = Boolean(sources.selector || sources.current);
                            return (
                              <div className="space-y-1" key={paramName}>
                                <label className="block text-xs fs-ink-secondary">
                                  <span>{paramName}</span>
                                  {sourceAllowed ? (
                                    <select
                                      className={INPUT_CLASS}
                                      onChange={(event) => {
                                        const nextMode = event.target.value as ParamMode;
                                        if (nextMode === "selector") {
                                          updateActionParam(index, actionIndex, paramName, {
                                            selector: "",
                                          });
                                        } else if (nextMode === "current") {
                                          updateActionParam(index, actionIndex, paramName, {
                                            current: true,
                                          });
                                        } else {
                                          updateActionParam(index, actionIndex, paramName, "");
                                        }
                                      }}
                                      value={mode}
                                    >
                                      <option value="literal">{t("react.workflows.literal", "Literal")}</option>
                                      {sources.selector ? <option value="selector">{t("react.workflows.source", "Source")}</option> : null}
                                      {sources.current ? <option value="current">{t("shell.account.current_badge", "Current")}</option> : null}
                                    </select>
                                  ) : null}
                                </label>
                                {mode === "selector" ? (
                                  <input
                                    aria-label={t("react.workflows.value0_source", "{value0} source", { value0: paramName })}
                                    className={INPUT_CLASS}
                                    onChange={(event) =>
                                      updateActionParam(index, actionIndex, paramName, {
                                        selector: event.target.value,
                                      })
                                    }
                                    placeholder="customer.last_name"
                                    value={paramEditorText(rawValue)}
                                  />
                                ) : null}
                                {mode === "current" ? (
                                  <p className="rounded border border-default fs-surface-inset px-2 py-2 text-xs fs-ink-secondary">
                                    {t("react.workflows.current_value", "Current value")}</p>
                                ) : null}
                                {mode !== "selector" && mode !== "current" ? (
                                  <input
                                    className={INPUT_CLASS}
                                    onChange={(event) =>
                                      updateActionParam(
                                        index,
                                        actionIndex,
                                        paramName,
                                        parseEditorValue(event.target.value),
                                      )
                                    }
                                    placeholder={textOrEmpty(schema.type) || "json"}
                                    value={paramEditorText(rawValue)}
                                  />
                                ) : null}
                              </div>
                            );
                          })}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <label className="block text-xs fs-ink-secondary">
          <span>{t("react.workflows.sample_json", "Sample JSON")}</span>
          <textarea
            className="fs-textarea mt-1 w-full min-h-44 font-mono text-xs"
            onChange={(event) => setSampleJson(event.target.value)}
            value={sampleJson}
          />
        </label>
        <div className="min-w-0 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">
              {t("registry.workflow_detail.run_lab.tab.preview", "Preview")}</p>
            <button
              className={SMALL_BUTTON_CLASS}
              disabled={previewPending || !workflowId}
              onClick={() => {
                void runPreview( );
              }}
              type="button"
            >
              {previewPending ? t("react.workflows.running", "Running...") : t("react.workflows.run_preview", "Run preview")}
            </button>
          </div>
          {transformActionsQuery?.isError ? (
            <p className="rounded border border-attention fs-surface-attention px-3 py-2 text-xs fs-ink-attention">
              {t("react.workflows.action_catalog_unavailable", "Action catalog unavailable.")}</p>
          ) : null}
          {previewError ? (
            <p className="rounded border border-danger fs-surface-danger px-3 py-2 text-xs fs-ink-danger">
              {previewError}
            </p>
          ) : null}
          {!hasPreview ? (
            <div className="min-h-44 rounded border border-default fs-surface-inset p-3 text-xs fs-ink-secondary">
              {t("react.workflows.no_preview_yet", "No preview yet.")}</div>
          ) : (
            <div className="space-y-2">
              {!previewOk ? (
                <div className="space-y-2 rounded border border-danger fs-surface-danger p-3 text-xs fs-ink-danger">
                  <p className="font-medium uppercase tracking-[0.12em] fs-ink-danger">
                    {t("registry.common.errors", "Errors")}</p>
                  {previewErrors.length ? (
                    <ul className="space-y-1">
                      {previewErrors.map((rawError, errorIndex) => {
                        const error = objectOrEmpty(rawError);
                        const errorKey = previewErrorKeyState.keys[errorIndex];
                        const parts = [
                          // Runtime errors carry `reason`; validation errors carry
                          // `message`, `code` and `path`.
                          textOrEmpty(error.message) ||
                            textOrEmpty(error.reason) ||
                            textOrEmpty(error.code) ||
                            "error",
                          textOrEmpty(error.path),
                          textOrEmpty(error.field_to) ? `field ${textOrEmpty(error.field_to)}` : "",
                          textOrEmpty(error.action) ? `action ${textOrEmpty(error.action)}` : "",
                          typeof error.element_index === "number"
                            ? `row ${String(error.element_index)}`
                            : "",
                          textOrEmpty(error.detail),
                        ].filter(Boolean);
                        return <li key={errorKey}>{parts.join(" · ")}</li>;
                      })}
                    </ul>
                  ) : (
                    <p>{textOrEmpty(previewObject.detail) || t("react.workflows.preview_failed", "Preview failed.")}</p>
                  )}
                </div>
              ) : null}
              {/* Output and state patch stack: side by side they shared half of an
                  already half-width column. min-w-0 keeps long JSON scrolling
                  inside its box instead of widening the column past the panel. */}
              <div className="grid gap-2">
                <div className="min-w-0">
                  <p className="mb-1 text-xs font-medium uppercase tracking-[0.12em] fs-ink-tertiary">
                    {t("registry.tool_detail.schemas.output", "Output")}</p>
                  <pre className="min-h-32 overflow-auto rounded border border-default fs-surface-inset p-3 text-xs fs-ink-primary">
                    {stringifyForEditor(previewObject.output)}
                  </pre>
                </div>
                <div className="min-w-0">
                  <p className="mb-1 text-xs font-medium uppercase tracking-[0.12em] fs-ink-tertiary">
                    {t("react.workflows.state_patch", "State patch")}</p>
                  <pre className="min-h-32 overflow-auto rounded border border-default fs-surface-inset p-3 text-xs fs-ink-primary">
                    {stringifyForEditor(previewObject.state_patch)}
                  </pre>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
