import { Button } from "@/components/ui/button";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/* eslint-disable @typescript-eslint/no-explicit-any */
import type { ReactNode } from "react";

import {
  contractItemDescription,
  contractItemName,
  contractItemType,
  draftMapRowIssue,
  newDraftMapRow,
  objectOrEmpty,
  nextSwitchCaseId,
  persistWorkflowContractSectionState,
  switchCaseRows,
  switchCasesFromRows,
  type DraftMapRow,
  type SwitchCaseRow,
  workflowVariableSuggestionPanelId,
  type WorkflowContractSectionKey,
  type WorkflowVariableSuggestionGroup,
} from "@/pages/workflows/detail/detail-helpers";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { DsBadge, RequiredLabel } from "@/components/shared/design-system";
import { DraftMapRows } from "@/pages/workflows/detail/detail-held-mapping-rows";

type WorkflowContractRenderContext = Record<string, any>;

function contractIssueClasses(focused: boolean, severity: string, compact: boolean): string {
  if (!focused) return compact ? "border-default fs-ink-secondary" : "border-default";
  if (severity === "error") {
    return compact ? "border-danger fs-ink-danger" : "border-danger";
  }
  return compact ? "border-attention fs-ink-attention" : "border-attention";
}

function contractSectionToggleLabel(expanded: boolean, issueFocused: boolean, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (expanded) return t("react.workflows.hide", "Hide");
  return issueFocused ? t("react.workflows.fix_here", "Fix here") : t("react.workflows.show", "Show");
}

const MAP_EDITOR_INPUT_CLASS =
  "fs-input w-full";

export function createWorkflowContractRenderers(ctx: WorkflowContractRenderContext, t: I18nContextValue["t"] = fallbackContext.t) {
  const {
    renderWorkflowVariableInput,
    replaceStepKindConfigField,
    selectedCanvasIssue,
    selectedIssueWorkflowContractSection,
    setWorkflowContractSectionsExpanded,
    stepKindConfig,
    updateConfigMapField,
    workflowContractSectionsExpanded,
  } = ctx;
  function toggleWorkflowContractSection(key: WorkflowContractSectionKey) {
    setWorkflowContractSectionsExpanded((current: Partial<Record<WorkflowContractSectionKey, boolean>>) => {
      const next = { ...current, [key]: !current[key] };
      persistWorkflowContractSectionState(next);
      return next;
    });
  }

  function renderWorkflowContractSection(
    key: WorkflowContractSectionKey,
    title: string,
    detail: string,
    children: ReactNode,
  ) {
    const expanded = Boolean(workflowContractSectionsExpanded[key]);
    const issueFocused = selectedIssueWorkflowContractSection === key;
    return (
      <div
        id={`workflow-contract-section-${key}`}
        className={[
          "rounded-lg border fs-surface-inset",
          contractIssueClasses(issueFocused, selectedCanvasIssue?.severity, false),
        ].map((value) => stringFromUnknownValue(value)).join(" ")}
      >
        <button
          aria-expanded={expanded}
          className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left"
          onClick={() => toggleWorkflowContractSection(key)}
          type="button"
        >
          <span className="min-w-0">
            <span className="block text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">
              {title}
            </span>
            <span className="mt-0.5 block truncate text-[11px] fs-ink-tertiary">
              {detail}
            </span>
          </span>
          <span
            className={[
              "shrink-0 rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.08em]",
              contractIssueClasses(issueFocused, selectedCanvasIssue?.severity, true),
            ].map((value) => stringFromUnknownValue(value)).join(" ")}
          >
            {contractSectionToggleLabel(expanded, issueFocused, t)}
          </span>
        </button>
        {expanded ? <div className="border-t border-default p-3">{children}</div> : null}
      </div>
    );
  }

  function renderConfigMapEditor(
    stepKind: string,
    field: string,
    label: string,
    emptyLabel: string,
    // Item 11: which side of a row names a variable -- a path, or a template.
    variables: { key?: "path"; value?: "path" | "template" } = {}
  ) {
    return (
      <DraftMapRows
        key={`${stepKind}:${field}`}
        store={(nextRows) => updateConfigMapField(stepKind, field, nextRows)}
        stored={stepKindConfig[field]}
      >
        {(rows, updateRows) => renderConfigMapRows(rows, updateRows, { emptyLabel, field, label, stepKind, variables })}
      </DraftMapRows>
    );
  }

  function configMapRowMessage(issue: string, keyIsPath: boolean): string {
    if (issue === "missing_key") {
      return keyIsPath
        ? t("react.workflows.map_row_path_missing", "Enter a path for this row. A row without one is not saved.")
        : t("react.workflows.map_row_key_missing", "Enter a key for this row. A row without one is not saved.");
    }
    return keyIsPath
      ? t("react.workflows.map_row_path_duplicate", "An earlier row already sets this path. Only the first one is saved.")
      : t("react.workflows.map_row_key_duplicate", "An earlier row already uses this key. Only the first one is saved.");
  }

  function renderConfigMapRows(
    rows: DraftMapRow[],
    updateRows: (next: DraftMapRow[]) => void,
    { emptyLabel, field, label, stepKind, variables }: {
      emptyLabel: string;
      field: string;
      label: string;
      stepKind: string;
      variables: { key?: "path"; value?: "path" | "template" };
    },
  ) {
    const updateRow = (index: number, patch: Partial<DraftMapRow>) => {
      updateRows(rows.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)));
    };
    const onlyEmptyRow = rows.length === 1 && !rows[0].key && !rows[0].value;
    return (
      <div className="rounded-xl border border-default fs-surface-inset p-3">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">
            {label}
          </p>
          <Button
            aria-label={t("react.workflows.add_value0", "Add {value0}", { value0: label })}
            variant="secondary"
            onClick={() => updateRows([...rows, newDraftMapRow()])}
            type="button"
          >{t("react.workflows.add", "+ Add")}</Button>
        </div>
        <div className="mt-2 space-y-2">
          {rows.map((row, index) => {
            const issue = draftMapRowIssue(rows, index);
            const messageId = issue ? `${stepKind}-${field}-row-${row.id}-message` : undefined;
            const keyLabel = t("react.workflows.value0_key_value1", "{value0} key {value1}", { value0: label, value1: index + 1 });
            const valueLabel = t("react.workflows.value0_value_value1", "{value0} value {value1}", { value0: label, value1: index + 1 });
            return (
              <div key={row.id}>
                <div className="flex items-start gap-2">
                  {/* The path is the longer of the two: it takes three fifths of a narrow drawer. */}
                  <div className="min-w-0 flex-[3]">
                    {variables.key
                      ? renderWorkflowVariableInput({
                          ariaDescribedBy: messageId,
                          ariaInvalid: Boolean(issue),
                          ariaLabel: keyLabel,
                          className: MAP_EDITOR_INPUT_CLASS,
                          onChange: (nextValue: string) => updateRow(index, { key: nextValue }),
                          panelId: workflowVariableSuggestionPanelId(`${stepKind}-${field}-key-${index}`),
                          placeholder: emptyLabel,
                          value: row.key,
                        })
                      : (
                        <input
                          aria-describedby={messageId}
                          aria-invalid={issue ? true : undefined}
                          aria-label={keyLabel}
                          className={MAP_EDITOR_INPUT_CLASS}
                          onChange={(event) => updateRow(index, { key: event.target.value })}
                          placeholder={emptyLabel}
                          value={row.key}
                        />
                      )}
                  </div>
                  <div className="min-w-0 flex-[2]">
                    {variables.value
                      ? renderWorkflowVariableInput({
                          ariaLabel: valueLabel,
                          className: MAP_EDITOR_INPUT_CLASS,
                          mode: variables.value,
                          onChange: (nextValue: string) => updateRow(index, { value: nextValue }),
                          panelId: workflowVariableSuggestionPanelId(`${stepKind}-${field}-value-${index}`),
                          placeholder: "value",
                          value: row.value,
                        })
                      : (
                        <input
                          aria-label={valueLabel}
                          className={MAP_EDITOR_INPUT_CLASS}
                          onChange={(event) => updateRow(index, { value: event.target.value })}
                          placeholder="value"
                          value={row.value}
                        />
                      )}
                  </div>
                  <Button
                    aria-label={t("react.workflows.remove_value0_value1", "Remove {value0} {value1}", { value0: label, value1: index + 1 })}
                    className="shrink-0"
                    disabled={onlyEmptyRow}
                    onClick={() => updateRows(rows.filter((_, rowIndex) => rowIndex !== index))}
                    size="sm"
                    type="button"
                    variant="ghost-danger"
                  >{t("extensions.repeater.remove", "Remove")}</Button>
                </div>
                {issue ? (
                  <p className="mt-1 text-[11px] fs-ink-attention" id={messageId}>
                    {configMapRowMessage(issue, variables.key === "path")}
                  </p>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  function renderWorkflowContractMappingEditor(
    field: "input_mapping" | "output_mapping" | "artifact_mapping",
    label: string,
    rows: Array<Record<string, unknown>>,
    placeholder: string,
    suggestions: WorkflowVariableSuggestionGroup[] = []
  ) {
    const mapping = objectOrEmpty(stepKindConfig[field]);
    if (!rows.length) {
      return renderConfigMapEditor(
        "invoke_workflow",
        field,
        label,
        placeholder,
        { value: "path" }
      );
    }
    return (
      <div className="rounded-xl border border-subtle fs-surface-inset p-3">
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">
          {label}
        </p>
        <div className="mt-2 space-y-2">
          {rows.map((row, index) => {
            const name = contractItemName(row);
            if (!name) {
              return null;
            }
            const type = contractItemType(row);
            const required = Boolean(row.required);
            const inputRequired = field === "input_mapping" && required;
            const description = contractItemDescription(row);
            const suggestionListId = suggestions.length
              ? workflowVariableSuggestionPanelId(`${field}-${name}`)
              : "";
            return (
              <label
                className="block rounded-lg border border-subtle bg-[var(--fs-bg-surface)] p-2 text-xs fs-ink-primary"
                key={`${field}-${name}-${index}`}
                title={description}
              >
                <span className="flex flex-wrap items-center gap-2">
                  <span className="font-mono fs-ink-primary">{inputRequired ? <RequiredLabel>{name}</RequiredLabel> : name}</span>
                  <span className="uppercase"><DsBadge>{type}</DsBadge></span>
                  {required ? (
                    <span className="uppercase"><DsBadge tone="orange">{t("registry.workflow_detail.outputs.required_badge", "required")}</DsBadge></span>
                  ) : null}
                </span>
                {renderWorkflowVariableInput({
                  ariaLabel: `${label} ${name}${inputRequired ? ` ${t("react.workflows.required_variant_89db19f6", "(required)")}` : ""}`,
                  ariaRequired: inputRequired,
                  className:
                    "fs-input mt-2",
                  onChange: (nextValue: string) => {
                    const nextMapping = { ...mapping, [name]: nextValue };
                    if (field === "input_mapping" && !required && nextValue === "") delete nextMapping[name];
                    replaceStepKindConfigField("invoke_workflow", field, nextMapping);
                  },
                  panelId: suggestionListId,
                  placeholder,
                  value: stringFromUnknownValue(mapping[name]),
                })}
              </label>
            );
          })}
        </div>
      </div>
    );
  }

  /**
   * Switch cases. Each case keeps a stable case_id: it names the outcome port the
   * canvas routes. Editing a case's match value never renames its port; adding or
   * removing a case changes the step's ports (a structural change).
   */
  function renderSwitchCasesEditor( ) {
    const rows = switchCaseRows(stepKindConfig.cases);
    const updateCases = (nextRows: SwitchCaseRow[]) => {
      replaceStepKindConfigField("switch", "cases", switchCasesFromRows(nextRows));
    };
    const updateRow = (index: number, patch: Partial<SwitchCaseRow>) => {
      updateCases(rows.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)));
    };
    const removeRow = (index: number) => {
      updateCases(rows.filter((_, rowIndex) => rowIndex !== index));
    };
    return (
      <div className="rounded-xl border border-default fs-surface-inset p-3">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.cases", "Cases")}</p>
          <Button
            variant="secondary"
            onClick={() => updateCases([...rows, { case_id: nextSwitchCaseId(rows), when: "", label: "" }])}
            type="button"
          >{t("react.workflows.add", "+ Add")}</Button>
        </div>
        <p className="mt-1 text-[11px] fs-ink-secondary">
          {t("react.workflows.switch_cases_ports_hint", "Each case is an outcome you connect on the canvas. Adding or removing a case changes the structure.")}
        </p>
        <div className="mt-2 space-y-2">
          {rows.map((row, index) => (
            <div
              className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]"
              key={`switch-case-${row.case_id || index}`}
            >
              <label className="block text-xs fs-ink-secondary">
                <span>{t("react.workflows.case_value_variant_cb8f79fc", "Case value")}</span>
                <input
                  aria-label={t("react.workflows.case_value_value0", "Case value {value0}", { value0: index + 1 })}
                  className="fs-input mt-1 w-full"
                  onChange={(event) => updateRow(index, { when: event.target.value })}
                  placeholder={t("react.workflows.case_value", "case value")}
                  value={row.when}
                />
              </label>
              <label className="block text-xs fs-ink-secondary">
                <span>{t("react.workflows.case_label", "Case label")}</span>
                <input
                  aria-label={t("react.workflows.case_label_value0", "Case label {value0}", { value0: index + 1 })}
                  className="fs-input mt-1 w-full"
                  onChange={(event) => updateRow(index, { label: event.target.value })}
                  placeholder={row.case_id}
                  value={row.label}
                />
                <span className="mt-1 block font-mono text-[10px] fs-ink-tertiary">case_id: {row.case_id || "—"}</span>
              </label>
              <Button
                variant="danger" className="self-end"
                onClick={() => removeRow(index)}
                type="button"
              >{t("extensions.repeater.remove", "Remove")}</Button>
            </div>
          ))}
        </div>
      </div>
    );
  }


  return {
    renderConfigMapEditor,
    renderSwitchCasesEditor,
    renderWorkflowContractMappingEditor,
    renderWorkflowContractSection,
  };
}
