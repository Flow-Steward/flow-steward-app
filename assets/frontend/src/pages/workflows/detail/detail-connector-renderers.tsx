import { Button } from "@/components/ui/button";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/* eslint-disable @typescript-eslint/no-explicit-any */
import type { ReactNode } from "react";

import {
  connectorInputMappingFromRows,
  connectorInputMappingRows,
  connectorOutputMappingRows,
  collectOptionsFromUnknown,
  contractItemName,
  isPlatformOwnedConnectorInput,
  mappingObjectFromRaw,
  objectFromEntries,
  objectOrEmpty,
  textOrEmpty,
  uniqueSelectOptions,
  workflowVariableSuggestionPanelId,
  type ConnectorInputMappingRow,
  type ConnectorInputMode,
} from "@/pages/workflows/detail/detail-helpers";
import { isTruthy } from "@/lib/utils/unknown-values";
import { HeldMappingRows } from "@/pages/workflows/detail/detail-held-mapping-rows";
import {
  ExternalResourcePicker,
  UnavailableExternalResourcePicker,
  type ExternalResourcePickerHostDeclaration,
} from "@/components/extensions/external-resource-picker";
import { createExternalResourcePickerBridge } from "@/components/extensions/external-resource-picker-bridge";
import { executeExtensionAction } from "@/pages/extensions/api";
import { extensionBrowserAssetUrl } from "@/pages/extensions/extension-manifest-render-model";
import { manifestActionOutcome } from "@/pages/extensions/extension-manifest-utils";

type WorkflowConnectorRenderContext = Record<string, any>;

function patchRow<T>(rows: T[], index: number, patch: Partial<T>): T[] {
  return rows.map((row, rowIndex) =>
    rowIndex === index ? { ...row, ...patch } : row,
  );
}

function externalPickerDeclaration(
  parameter: Record<string, unknown> | undefined,
): ExternalResourcePickerHostDeclaration | null {
  const picker = parameter?.external_resource_picker;
  if (textOrEmpty(parameter?.widget_type || parameter?.widget) !== "resource_picker") {
    return null;
  }
  if (!picker || typeof picker !== "object" || Array.isArray(picker)) return null;
  return picker as ExternalResourcePickerHostDeclaration;
}

function renderConnectorExternalPicker({
  parameter,
  row,
  index,
  selectedConnectionId,
  selectedConnectorId,
  selectedExtensionInstallId,
  selectedExtensionReleaseId,
  selectedExtensionDescriptorSha256,
  selectedProjectId,
  updateRow,
}: {
  parameter: Record<string, unknown> | undefined;
  row: ConnectorInputMappingRow;
  index: number;
  selectedConnectionId: string;
  selectedConnectorId: unknown;
  selectedExtensionInstallId: unknown;
  selectedExtensionReleaseId: unknown;
  selectedExtensionDescriptorSha256: unknown;
  selectedProjectId: unknown;
  updateRow: (index: number, patch: Partial<ConnectorInputMappingRow>) => void;
}, t: I18nContextValue["t"] = fallbackContext.t) {
  const declaration = externalPickerDeclaration(parameter);
  if (!declaration) return null;
  const connectorId = textOrEmpty(selectedConnectorId);
  const extensionId = connectorId.startsWith("extension.")
    ? connectorId.slice("extension.".length)
    : "";
  try {
    const bridge = createExternalResourcePickerBridge(declaration, {
      executeBootstrap: async (actionId) => {
        const payload = await executeExtensionAction(extensionId, actionId, {
          context: { project_id: textOrEmpty(selectedProjectId) },
          input: {},
          target: { connection_id: selectedConnectionId },
        });
        const outcome = manifestActionOutcome(payload, t);
        if (!outcome.ok) throw new Error("external_picker_bootstrap_failed");
        return outcome.result;
      },
      resolveAssetScript: (assetRef) => extensionBrowserAssetUrl({
        extensionId,
        extensionInstallId: textOrEmpty(selectedExtensionInstallId),
        extensionReleaseId: textOrEmpty(selectedExtensionReleaseId),
        extensionDescriptorSha256: textOrEmpty(selectedExtensionDescriptorSha256),
        projectId: textOrEmpty(selectedProjectId),
        assetRef,
      }),
    });
    return (
      <ExternalResourcePicker
        bridge={bridge}
        declaration={declaration}
        disabled={!extensionId || !selectedConnectionId || !selectedProjectId}
        onSelection={(selections) => {
          const selection = selections[0] || {};
          const selectedValue = textOrEmpty(selection[row.key])
            || textOrEmpty(Object.values(selection)[0]);
          if (selectedValue) updateRow(index, { mode: "literal", value: selectedValue });
        }}
      />
    );
  } catch {
    return <UnavailableExternalResourcePicker />;
  }
}

function selectedConnectorInputKeys(
  rows: ConnectorInputMappingRow[],
  currentIndex: number,
): Set<string> {
  return new Set(
    rows
      .map((candidate, candidateIndex) =>
        candidateIndex === currentIndex ? "" : textOrEmpty(candidate.key),
      )
      .filter(isTruthy),
  );
}

function applyConnectorInputKeyChange({
  index,
  nextKey,
  removeRow,
  row,
  updateRow,
}: {
  index: number;
  nextKey: string;
  removeRow: (index: number) => void;
  row: ConnectorInputMappingRow;
  updateRow: (index: number, patch: Partial<ConnectorInputMappingRow>) => void;
}) {
  if (!nextKey) {
    removeRow(index);
    return;
  }
  const patch: Partial<ConnectorInputMappingRow> = { key: nextKey };
  const previousStateDefault = row.key ? `state.${row.key}` : "";
  if (!row.retainedValue && row.mode === "state" && (!row.value || row.value === previousStateDefault)) {
    patch.value = `state.${nextKey}`;
  }
  updateRow(index, patch);
}

function connectorInputValueForMode(
  row: ConnectorInputMappingRow,
  nextMode: ConnectorInputMode,
): string {
  const stateDefault = row.key ? `state.${row.key}` : "";
  if (nextMode === "literal" && row.mode === "state" && row.value === stateDefault) {
    return "";
  }
  return row.value;
}

function connectorInputRowIsPlaceholder(
  row: ConnectorInputMappingRow,
  rowCount: number,
): boolean {
  return rowCount === 1 && !row.key && !row.value;
}

function connectorInputValueMissing(required: boolean, value: string): boolean {
  return required && !textOrEmpty(value).trim();
}

// By the parameter only: a row with none yet keeps its identity (and the focus) while typed into.
function connectorInputRowKey(row: ConnectorInputMappingRow): string {
  return `connector-input-${row.key || "placeholder"}`;
}

/** Whether the input mapping keeps this row: it names a parameter and maps it to something. */
function connectorInputRowStorable(row: ConnectorInputMappingRow): boolean {
  return Object.keys(connectorInputMappingFromRows([row])).length > 0;
}

function connectorInputOptionDisabled(
  optionValue: string,
  rowKey: string,
  selectedByOtherRows: Set<string>,
): boolean {
  return optionValue !== rowKey && selectedByOtherRows.has(optionValue);
}

// The State path field holds a state path: "project_" typed there is state.project_, so
// it stays one (and the suggestions narrow to state.project_config) instead of turning
// into a Raw string (item 11).
function asStatePath(typed: string): string {
  if (!typed || typed.startsWith("state.") || "state.".startsWith(typed)) return typed;
  return `state.${typed}`;
}

function renderConnectorStateInput(
  row: ConnectorInputMappingRow,
  renderWorkflowVariableInput: (props: Record<string, unknown>) => ReactNode,
  props: Record<string, unknown>,
) {
  if (row.mode !== "state") return null;
  return renderWorkflowVariableInput(props);
}

export function createWorkflowConnectorRenderers(ctx: WorkflowConnectorRenderContext, t: I18nContextValue["t"] = fallbackContext.t) {
  const connectorOutputRows =
    ctx.connectorOutputRows as Array<Record<string, unknown>>;
  const connectorParameterRows =
    ctx.connectorParameterRows as Array<Record<string, unknown>>;
  const {
    renderWorkflowVariableInput,
    replaceStepKindConfigField,
    selectedStep,
    selectedConnectorId,
    selectedExtensionInstallId,
    selectedExtensionReleaseId,
    selectedExtensionDescriptorSha256,
    selectedProjectId,
    stepKindConfig,
  } = ctx;
  // One step editor's held rows never carry over to another step or action.
  const mappingEditorKey = [
    textOrEmpty(selectedStep?.runtimeId) || "new",
    selectedConnectorId,
    textOrEmpty(stepKindConfig.function_name),
  ].join(":");

  function renderConnectorInputMappingEditor( ) {
    return (
      <HeldMappingRows
        key={`input:${mappingEditorKey}`}
        rows={connectorInputMappingRows(stepKindConfig.input_mapping, connectorParameterRows)}
        storable={connectorInputRowStorable}
        store={(nextRows) =>
          replaceStepKindConfigField("connector", "input_mapping", connectorInputMappingFromRows(nextRows))
        }
      >
        {(rows, updateRows) => renderConnectorInputMappingRows(rows, updateRows)}
      </HeldMappingRows>
    );
  }

  function renderConnectorInputMappingRows(
    rows: ConnectorInputMappingRow[],
    updateRows: (nextRows: ConnectorInputMappingRow[]) => void,
  ) {
    const parameterOptions = uniqueSelectOptions([
      ...connectorParameterRows.map((row) => ({
        value: contractItemName(row),
        label: contractItemName(row),
      })),
      ...Object.keys(
        mappingObjectFromRaw(
          objectOrEmpty(selectedStep?.raw),
          "input_mapping_jsonb",
          "input_mapping",
        ),
      ).map((key) => ({ value: key, label: key })),
      ...rows.map((row) => ({ value: row.key, label: row.key })),
    ]).filter((option) => !isPlatformOwnedConnectorInput(option.value));
    // An action that declares no parameters (and has none saved) has nothing to map:
    // no block, not an empty row whose select only says "Choose parameter" (item 6).
    if (!parameterOptions.length) return null;
    const selectedParameterKeys = new Set(
      rows.map((row) => textOrEmpty(row.key)).filter(isTruthy),
    );
    const nextParameterOption = parameterOptions.find(
      (option) => option.value && !selectedParameterKeys.has(option.value),
    );
    const updateRow = (
      index: number,
      patch: Partial<ConnectorInputMappingRow>,
    ) => {
      const row = rows[index];
      const valueEdited = Object.hasOwn(patch, "value") || Object.hasOwn(patch, "mode");
      const retainedValue = valueEdited ? undefined : row.retainedValue
        ? { ...row.retainedValue, key: patch.key ?? row.key }
        : undefined;
      updateRows(patchRow(rows, index, { ...patch, retainedValue }));
    };
    const selectedConnectionId = textOrEmpty(
      objectOrEmpty(stepKindConfig.connection_binding).connection_id,
    );
    const removeRow = (index: number) => {
      updateRows(rows.filter((_, rowIndex) => rowIndex !== index));
    };
    const addRow = () => {
      if (!nextParameterOption) {
        return;
      }
      const nextRow: ConnectorInputMappingRow = {
        key: nextParameterOption.value,
        mode: "state",
        value: `state.${nextParameterOption.value}`,
      };
      const hasOnlyPlaceholder =
        rows.length === 1 && !rows[0].key && !rows[0].value;
      updateRows(hasOnlyPlaceholder ? [nextRow] : [...rows, nextRow]);
    };
    const missingRequiredNames = rows.reduce<string[]>((missing, row) => {
      const parameter = connectorParameterRows.find(
        (candidate) => contractItemName(candidate) === row.key,
      );
      if (parameter?.required === true && !textOrEmpty(row.value).trim()) {
        missing.push(row.key);
      }
      return missing;
    }, []);
    return (
      <div className="rounded-xl border border-default fs-surface-inset p-3">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.parameters", "Parameters")}</p>
          <Button
            aria-label={t("react.workflows.add_parameter_mapping", "Add parameter mapping")}
            variant="secondary"
            disabled={!nextParameterOption}
            onClick={addRow}
            type="button"
          >{t("react.workflows.add", "+ Add")}</Button>
        </div>
        {missingRequiredNames.length ? (
          <p
            className="mt-2 rounded-lg border border-attention fs-surface-attention px-3 py-2 text-xs fs-ink-attention"
            role="alert"
          >{t("react.workflows.required_action_inputs_are_missing", "Required action inputs are missing:")}{" "}{missingRequiredNames.join(", ")}.
          </p>
        ) : null}
        <div className="mt-2 space-y-2">
          {rows.map((row, index) => {
            const selectedByOtherRows = selectedConnectorInputKeys(rows, index);
            const isPlaceholder = connectorInputRowIsPlaceholder(row, rows.length);
            const parameter = connectorParameterRows.find(
              (candidate) => contractItemName(candidate) === row.key,
            );
            const required = parameter?.required === true;
            const valueMissing = connectorInputValueMissing(required, row.value);
            const valueLabel = t("react.workflows.parameter_value_value0", "Parameter value {value0}", { value0: index + 1 });
            const literalOptions = collectOptionsFromUnknown(
              parameter?.options,
              ["value"],
              ["label"],
            );
            return (
              <div
                className="grid gap-2 sm:grid-cols-[minmax(0,0.9fr)_132px_minmax(0,1.1fr)_auto]"
                key={connectorInputRowKey(row)}
              >
                <label className="block text-xs fs-ink-secondary">
                  <span>{t("react.registry.parameter", "Parameter")}</span>
                  <select
                    aria-label={t("react.workflows.parameter_value0", "Parameter {value0}", { value0: index + 1 })}
                    className="fs-input mt-1 w-full"
                    onChange={(event) => {
                      applyConnectorInputKeyChange({
                        index,
                        nextKey: event.target.value,
                        removeRow,
                        row,
                        updateRow,
                      });
                    }}
                    value={row.key}
                  >
                    <option value="">{t("react.workflows.choose_parameter", "Choose parameter")}</option>
                    {parameterOptions.map((option) => (
                      <option
                        disabled={connectorInputOptionDisabled(
                          option.value,
                          row.key,
                          selectedByOtherRows,
                        )}
                        key={option.value}
                        value={option.value}
                      >
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-xs fs-ink-secondary">
                  <span>{t("react.workflows.source", "Source")}</span>
                  <select
                    aria-label={t("react.workflows.parameter_source_value0", "Parameter source {value0}", { value0: index + 1 })}
                    className="fs-input mt-1 w-full"
                    onChange={(event) => {
                      const nextMode = event.target.value as ConnectorInputMode;
                      updateRow(index, {
                        mode: nextMode,
                        value: connectorInputValueForMode(row, nextMode),
                      });
                    }}
                    value={row.mode}
                  >
                    <option value="state">{t("react.workflows.state_path", "State path")}</option>
                    <option value="literal">{t("react.workflows.literal_value", "Literal value")}</option>
                    {row.mode === "expression" ? (
                      <option value="expression">{t("react.workflows.raw_string", "Raw string")}</option>
                    ) : null}
                  </select>
                </label>
                <div className="block text-xs fs-ink-secondary">
                  <label className="block">
                    <span>{t("project_edit.fields.value", "Value")}{required ? t("react.workflows.required_variant_89db19f6", "(required)") : ""}</span>
                    {renderConnectorStateInput(
                      row,
                      renderWorkflowVariableInput,
                      {
                        ariaDescribedBy: valueMissing
                          ? `connector-input-error-${index}`
                          : undefined,
                        ariaInvalid: valueMissing,
                        ariaLabel: valueLabel,
                        ariaRequired: required,
                        className: "fs-input mt-1 w-full",
                        onChange: (nextValue: string) =>
                          updateRow(index, { value: asStatePath(nextValue) }),
                        panelId: workflowVariableSuggestionPanelId(
                          `connector-input-${index}`,
                        ),
                        placeholder: "state.some_value",
                        value: row.value,
                      },
                    )}
                    {row.mode !== "state" && literalOptions.length ? (
                        <select
                          aria-describedby={valueMissing ? `connector-input-error-${index}` : undefined}
                          aria-invalid={valueMissing || undefined}
                          aria-label={valueLabel}
                          aria-required={required || undefined}
                          className="fs-input mt-1 w-full"
                          onChange={(event) =>
                            updateRow(index, { value: event.target.value })
                          }
                          value={row.value}
                        >
                          <option value="">{t("react.workflows.choose_value", "Choose value")}</option>
                          {literalOptions.map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                    ) : null}
                    {row.mode !== "state" && !literalOptions.length ? (
                        <input
                          aria-describedby={valueMissing ? `connector-input-error-${index}` : undefined}
                          aria-invalid={valueMissing || undefined}
                          aria-label={valueLabel}
                          aria-required={required || undefined}
                          className="fs-input mt-1 w-full"
                          onChange={(event) =>
                            updateRow(index, { value: event.target.value })
                          }
                          placeholder={
                            row.mode === "literal"
                              ? t("react.workflows.text_value", "text value")
                              : t("react.workflows.unchanged_string", "unchanged string")
                          }
                          value={row.value}
                        />
                    ) : null}
                  </label>
                  {valueMissing ? (
                    <p
                      className="mt-1 text-[11px] fs-ink-attention"
                      id={`connector-input-error-${index}`}
                    >
                      {row.key}{" "}{t("react.workflows.is_required_before_saving_or_testing_this_action", "is required before saving or testing this action.")}</p>
                  ) : null}
                  {renderConnectorExternalPicker({
                    parameter,
                    row,
                    index,
                    selectedConnectionId,
                    selectedConnectorId,
                    selectedExtensionInstallId,
                    selectedExtensionReleaseId,
                    selectedExtensionDescriptorSha256,
                    selectedProjectId,
                    updateRow,
                  }, t)}
                </div>
                {/* An empty placeholder row has nothing to remove. */}
                {isPlaceholder ? null : <Button
                  aria-label={t("react.workflows.remove_parameter_mapping_value0", "Remove parameter mapping {value0}", { value0: index + 1 })}
                  variant="ghost-danger" size="sm" className="self-end"
                  onClick={() => removeRow(index)}
                  type="button"
                >{t("extensions.repeater.remove", "Remove")}</Button>}
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  function renderConnectorOutputMappingEditor( ) {
    return (
      <HeldMappingRows
        key={`output:${mappingEditorKey}`}
        rows={connectorOutputMappingRows(stepKindConfig.output_mapping, connectorOutputRows)}
        storable={(row) => Boolean(textOrEmpty(row.key))}
        store={(nextRows) => replaceStepKindConfigField("connector", "output_mapping", objectFromEntries(nextRows))}
      >
        {(rows, updateRows) => renderConnectorOutputMappingRows(rows, updateRows)}
      </HeldMappingRows>
    );
  }

  function renderConnectorOutputMappingRows(
    rows: Array<{ key: string; value: string }>,
    updateRows: (nextRows: Array<{ key: string; value: string }>) => void,
  ) {
    const outputOptions = uniqueSelectOptions([
      ...connectorOutputRows.map((row) => ({
        value: contractItemName(row),
        label: contractItemName(row),
      })),
      ...Object.keys(
        mappingObjectFromRaw(
          objectOrEmpty(selectedStep?.raw),
          "output_mapping_jsonb",
          "output_mapping",
        ),
      ).map((key) => ({ value: key, label: key })),
      ...rows.map((row) => ({ value: row.key, label: row.key })),
    ]);
    // Nothing returned (and nothing saved): nothing to store in the workflow state.
    if (!outputOptions.length) return null;
    const selectedOutputKeys = new Set(
      rows.map((row) => textOrEmpty(row.key)).filter(isTruthy),
    );
    const nextOutputOption = outputOptions.find(
      (option) => option.value && !selectedOutputKeys.has(option.value),
    );
    const updateRow = (
      index: number,
      patch: Partial<{ key: string; value: string }>,
    ) => {
      updateRows(patchRow(rows, index, patch));
    };
    const removeRow = (index: number) => {
      updateRows(rows.filter((_, rowIndex) => rowIndex !== index));
    };
    const addRow = () => {
      if (!nextOutputOption) {
        return;
      }
      const nextRow = {
        key: nextOutputOption.value,
        value: `state.${nextOutputOption.value}`,
      };
      const hasOnlyPlaceholder =
        rows.length === 1 && !rows[0].key && !rows[0].value;
      updateRows(hasOnlyPlaceholder ? [nextRow] : [...rows, nextRow]);
    };
    return (
      <div className="rounded-xl border border-default fs-surface-inset p-3">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.output_mapping", "Output mapping")}</p>
          <Button
            aria-label={t("react.workflows.add_output_mapping", "Add output mapping")}
            variant="secondary"
            disabled={!nextOutputOption}
            onClick={addRow}
            type="button"
          >{t("react.workflows.add", "+ Add")}</Button>
        </div>
        <div className="mt-2 space-y-2">
          {rows.map((row, index) => {
            const selectedByOtherRows = new Set(
              rows
                .map((candidate, candidateIndex) =>
                  candidateIndex === index ? "" : textOrEmpty(candidate.key),
                )
                .filter(isTruthy),
            );
            const isPlaceholder = rows.length === 1 && !row.key && !row.value;
            return (
              <div
                className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]"
                key={`connector-output-${row.key || "placeholder"}`}
              >
                <label className="block text-xs fs-ink-secondary">
                  <span>{t("react.workflows.returned_field", "Returned field")}</span>
                  <select
                    aria-label={t("react.workflows.returned_field_value0", "Returned field {value0}", { value0: index + 1 })}
                    className="fs-input mt-1 w-full"
                    onChange={(event) => {
                      const nextKey = event.target.value;
                      if (!nextKey) {
                        removeRow(index);
                        return;
                      }
                      const previousDefault = row.key ? `state.${row.key}` : "";
                      updateRow(index, {
                        key: nextKey,
                        value:
                          !row.value || row.value === previousDefault
                            ? `state.${nextKey}`
                            : row.value,
                      });
                    }}
                    value={row.key}
                  >
                    <option value="">{t("react.workflows.choose_output", "Choose output")}</option>
                    {outputOptions.map((option) => (
                      <option
                        disabled={
                          option.value !== row.key &&
                          selectedByOtherRows.has(option.value)
                        }
                        key={option.value}
                        value={option.value}
                      >
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-xs fs-ink-secondary">
                  <span>{t("react.workflows.store_in_workflow_state", "Store in workflow state")}</span>
                  {renderWorkflowVariableInput({
                    ariaLabel: t("react.workflows.output_target_value0", "Output target {value0}", { value0: index + 1 }),
                    className: "fs-input mt-1 w-full",
                    onChange: (nextValue: string) => updateRow(index, { value: nextValue }),
                    panelId: workflowVariableSuggestionPanelId(`connector-output-${index}`),
                    placeholder: "state.result",
                    value: row.value,
                  })}
                </label>
                {/* An empty placeholder row has nothing to remove. */}
                {isPlaceholder ? null : <Button
                  aria-label={t("react.workflows.remove_output_mapping_value0", "Remove output mapping {value0}", { value0: index + 1 })}
                  variant="ghost-danger" size="sm" className="self-end"
                  onClick={() => removeRow(index)}
                  type="button"
                >{t("extensions.repeater.remove", "Remove")}</Button>}
              </div>
            );
          })}
        </div>
      </div>
    );
  }


  return {
    renderConnectorInputMappingEditor,
    renderConnectorOutputMappingEditor,
  };
}
