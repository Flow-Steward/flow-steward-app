import { AgentBindingFields, type AgentStepEditorContext } from "./detail-agent-step-editor";
import { DsSelect, RequiredLabel } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";
import { ConnectorStepSetup, DatasetQueryStepSetup, HttpRequestStepSetup } from "./detail-connection-step-setup";
import { TabularFeedBuilder } from "./detail-tabular-feed-builder";
import { HookDeclarationEditor } from "./detail-hook-declaration-editor";
import type { WorkflowHookContract } from "@/pages/workflows/shared/hook-api";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  IF_CONSTANT_OPERATORS,
  IF_OPERATORS_WITH_VALUE,
  MAX_WORKFLOW_BATCH_SIZE,
  clampWorkflowBatchSize,
  contractRows,
  ifConditionDraftFromUnknown,
  isSetSourceCopy,
  localizedIF_OPERATOR_OPTIONS,
  objectOrEmpty,
  textOrEmpty,
} from "@/pages/workflows/detail/detail-helpers";
import { TemplateStepSetup } from "@/pages/workflows/detail/detail-template-step";
import { WorkflowTransformBuilder } from "@/pages/workflows/detail/detail-transform-builder";
import { useEffect, useId, useState, type ReactNode } from "react";
import { numericDraftFromInput, numericInputValue, stepNumericFieldErrors } from "./detail-step-numeric-fields";
import { toggledReturnedFiles, type ReturnedFileEntry, type StepFileResult } from "./detail-returned-files";

type WorkflowStepSmartSetupContext = Record<string, any>;

type SetLiteralType = "string" | "number" | "boolean" | "null" | "json";
type SetAssignmentRow = {
  id: string;
  target: string;
  originalTarget: unknown;
  targetDirty: boolean;
  mode: "literal" | "source";
  literalType: SetLiteralType;
  literalText: string;
  source: string;
  original: unknown;
  valueDirty: boolean;
};
let setAssignmentSequence = 0;
function setAssignmentRow(target: unknown = "", value: unknown = ""): SetAssignmentRow {
  const exactSource = isSetSourceCopy(value);
  const literalType: SetLiteralType = value === null ? "null" : typeof value === "string" ? "string" :
    typeof value === "number" ? "number" : typeof value === "boolean" ? "boolean" : "json";
  const literalText = typeof value === "string" ? value : value === null ? "" : JSON.stringify(value) ?? "";
  return { id: `set-assignment-${++setAssignmentSequence}`, target: typeof target === "string" ? target : textOrEmpty(target),
    originalTarget: target, targetDirty: false, mode: exactSource ? "source" : "literal",
    literalType, literalText, source: exactSource ? (value as { source: string }).source : "",
    original: value, valueDirty: false };
}
function setRowsFromConfig(config: Record<string, unknown>): SetAssignmentRow[] {
  const assignments = objectOrEmpty(config.assignments);
  const entries = Object.entries(assignments);
  if (entries.length) return entries.map(([target, value]) => setAssignmentRow(target, value));
  if (Object.hasOwn(config, "target")) return [setAssignmentRow(config.target, config.value)];
  return [setAssignmentRow()];
}
function setAssignmentValue(row: SetAssignmentRow): { valid: boolean; value: unknown; issue?: "source_object" } {
  if (!row.valueDirty) return { valid: true, value: row.original };
  if (row.mode === "source") {
    const source = row.source.trim();
    return { valid: source === "state" || source.startsWith("state."), value: { source } };
  }
  if (row.literalType === "string") return { valid: true, value: row.literalText };
  if (row.literalType === "null") return { valid: true, value: null };
  if (row.literalType === "boolean") return { valid: true, value: row.literalText === "true" };
  if (row.literalType === "number") {
    const number = Number(row.literalText);
    return { valid: row.literalText.trim() !== "" && Number.isFinite(number), value: number };
  }
  try {
    const value: unknown = JSON.parse(row.literalText);
    if (isSetSourceCopy(value)) return { valid: false, value: row.original, issue: "source_object" };
    return { valid: value !== null && typeof value === "object", value };
  } catch { return { valid: false, value: row.original }; }
}
function effectiveSetTarget(row: SetAssignmentRow): string {
  return row.targetDirty || typeof row.originalTarget !== "string"
    ? row.target.trim() : row.originalTarget;
}
function setAssignmentIssue(rows: SetAssignmentRow[], t: ReturnType<typeof useI18n>["t"]): string {
  const seen = new Set<string>();
  for (const row of rows) {
    const target = effectiveSetTarget(row);
    const retainedNonstring = !row.targetDirty && typeof row.originalTarget !== "string";
    if (!target && (rows.length > 1 || row.targetDirty || row.valueDirty) && !retainedNonstring)
      return t("react.workflows.assignment_target_required", "Give each assignment a target.");
    if (target && seen.has(target)) return t("react.workflows.assignment_target_unique", "Each assignment needs a different target.");
    if (target) seen.add(target);
    const candidate = setAssignmentValue(row);
    if (!candidate.valid && candidate.issue === "source_object")
      return t("react.workflows.assignment_source_object_hint", "Use Previous value to copy another value; this JSON would run as a source.");
    if (target && !candidate.valid) return t("react.workflows.assignment_value_invalid", "Enter a valid assignment value.");
  }
  return "";
}
function setAssignmentBasis(config: Record<string, unknown>): string {
  return JSON.stringify({ assignments: config.assignments, hasTarget: Object.hasOwn(config, "target"),
    target: config.target, hasValue: Object.hasOwn(config, "value"), value: config.value });
}
function SetVariableAssignments({ ctx }: Readonly<{ ctx: WorkflowStepSmartSetupContext }>) {
  const { t } = useI18n();
  const config = objectOrEmpty(ctx.stepKindConfig);
  const basis = setAssignmentBasis(config);
  const [local, setLocal] = useState(() => ({ basis, rows: setRowsFromConfig(config) }));
  let rows = local.rows;
  if (local.basis !== basis) {
    rows = setRowsFromConfig(config);
    setLocal({ basis, rows });
  }
  const issue = setAssignmentIssue(rows, t);
  const issueId = useId();
  const onStepInputValidationChange = ctx.onStepInputValidationChange;
  useEffect(() => {
    onStepInputValidationChange?.(issueId, issue);
    return () => onStepInputValidationChange?.(issueId, "");
  }, [onStepInputValidationChange, issue, issueId]);
  function commit(next: SetAssignmentRow[], removed = false) {
    if (setAssignmentIssue(next, t)) {
      setLocal({ basis, rows: next });
      return;
    }
    const assignments: Record<string, unknown> = Object.create(null);
    for (const row of next) {
      const target = effectiveSetTarget(row);
      if (!target || Object.hasOwn(assignments, target)) continue;
      const candidate = setAssignmentValue(row);
      assignments[target] = candidate.valid ? candidate.value : row.original;
    }
    if (removed && !Object.keys(assignments).length && (Object.hasOwn(config, "target") || Object.hasOwn(config, "value"))) {
      // Removing the last authored row explicitly clears the legacy pair in one draft update.
      const nextConfig: Record<string, unknown> = { ...config, assignments };
      delete nextConfig.target;
      delete nextConfig.value;
      setLocal({ basis: setAssignmentBasis(nextConfig), rows: next });
      ctx.updateStepKindConfig("set_variable", { assignments, target: undefined, value: undefined });
      return;
    }
    const legacy = Object.hasOwn(config, "target") && !Object.keys(objectOrEmpty(config.assignments)).length && next.length === 1;
    if (legacy) {
      const row = next[0];
      const candidate = setAssignmentValue(row);
      const nextConfig = { ...config, ...(row.targetDirty ? { target: row.target.trim() } : {}),
        ...(candidate.valid && row.valueDirty ? { value: candidate.value } : {}) };
      setLocal({ basis: setAssignmentBasis(nextConfig), rows: next });
      if (row.targetDirty) ctx.replaceStepKindConfigField("set_variable", "target", row.target.trim());
      if (candidate.valid && row.valueDirty) ctx.replaceStepKindConfigField("set_variable", "value", candidate.value);
      return;
    }
    const nextConfig = { ...config, assignments };
    setLocal({ basis: setAssignmentBasis(nextConfig), rows: next });
    ctx.replaceStepKindConfigField("set_variable", "assignments", assignments);
  }
  const update = (id: string, patch: Partial<SetAssignmentRow>) => commit(rows.map((row) => row.id === id ? { ...row, ...patch } : row));
  return <div className="rounded-xl border border-default fs-surface-inset p-3">
    <div className="flex items-center justify-between gap-2">
      <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.assignments", "Assignments")}</p>
      <button className="fs-button secondary sm" onClick={() => commit([...rows, setAssignmentRow()])} type="button">{t("react.workflows.add_assignment", "Add assignment")}</button>
    </div>
    <div className="mt-2 space-y-3">{rows.map((row, index) => <div className="space-y-2 rounded border border-default p-2" key={row.id}>
      <label className="block text-xs fs-ink-secondary"><span>{t("react.workflows.assignment_target", "Target")}</span>
        {ctx.renderWorkflowDestinationInput({ ariaLabel: t("react.workflows.assignment_number_target", "Assignment {number} target", { number: index + 1 }), className: "fs-input mt-1 w-full",
          onChange: (value: string) => update(row.id, { target: value, targetDirty: true }), panelId: `set-target-${row.id}`, placeholder: t("react.workflows.variable_name", "Variable name"), value: row.target })}</label>
      <label className="block text-xs fs-ink-secondary"><span>{t("react.workflows.assignment_value_mode", "Value mode")}</span>
        <select aria-label={t("react.workflows.assignment_number_value_mode", "Assignment {number} value mode", { number: index + 1 })} className="fs-input mt-1 w-full"
          onChange={(event) => update(row.id, { mode: event.target.value as SetAssignmentRow["mode"], valueDirty: true })} value={row.mode}>
          <option value="literal">{t("react.workflows.literal_value", "Value")}</option><option value="source">{t("react.workflows.previous_value", "Previous value")}</option>
        </select></label>
      {row.mode === "source" ? <label className="block text-xs fs-ink-secondary"><span>{t("react.workflows.previous_value", "Previous value")}</span>
        {ctx.renderWorkflowVariableInput({ ariaLabel: t("react.workflows.assignment_number_previous_value", "Assignment {number} previous value", { number: index + 1 }), className: "fs-input mt-1 w-full",
          onChange: (value: string) => update(row.id, { source: value, valueDirty: true }), panelId: `set-source-${row.id}`, placeholder: t("react.workflows.select_previous_value", "Select a previous value"), value: row.source })}</label>
        : <div className="grid gap-2 sm:grid-cols-2"><label className="block text-xs fs-ink-secondary"><span>{t("react.workflows.literal_type", "Value type")}</span>
          <select aria-label={t("react.workflows.assignment_number_literal_type", "Assignment {number} literal type", { number: index + 1 })} className="fs-input mt-1 w-full"
            onChange={(event) => update(row.id, { literalType: event.target.value as SetLiteralType, literalText: event.target.value === "boolean" ? "false" : event.target.value === "number" ? "" : row.literalText, valueDirty: true })} value={row.literalType}>
            <option value="string">{t("react.workflows.type_string", "Text")}</option><option value="number">{t("react.workflows.type_number", "Number")}</option>
            <option value="boolean">{t("react.workflows.type_boolean", "Boolean")}</option><option value="null">{t("react.workflows.type_null", "Null")}</option>
            <option value="json">{t("react.workflows.type_object_or_array", "Object or array")}</option>
          </select></label>
          {row.literalType === "null" ? null : <label className="block text-xs fs-ink-secondary"><span>{t("react.workflows.literal_value", "Value")}</span>
            {row.literalType === "boolean" ? <select aria-label={t("react.workflows.assignment_number_value", "Assignment {number} value", { number: index + 1 })} className="fs-input mt-1 w-full"
              onChange={(event) => update(row.id, { literalText: event.target.value, valueDirty: true })} value={row.literalText}><option value="false">{t("react.workflows.false", "False")}</option><option value="true">{t("react.workflows.true", "True")}</option></select>
              : <input aria-label={t("react.workflows.assignment_number_value", "Assignment {number} value", { number: index + 1 })} className="fs-input mt-1 w-full"
                onChange={(event) => update(row.id, { literalText: event.target.value, valueDirty: true })} value={row.literalText} />}</label>}
        </div>}
      <button className="fs-button danger sm" onClick={() => commit(rows.filter((item) => item.id !== row.id), true)} type="button">{t("react.workflows.remove_assignment", "Remove assignment")}</button>
    </div>)}</div>
    {issue ? <p className="fs-field-helper error" id={issueId} role="alert">{issue}</p> : null}
  </div>;
}

type ApprovalOutputRow = { id: string; value: string; raw: unknown; edited: boolean; retained: boolean };
let approvalOutputSequence = 0;
function approvalRowsFromStored(value: unknown): ApprovalOutputRow[] {
  const rows = (Array.isArray(value) ? value : []).map((item) => ({ id: `approval-output-${++approvalOutputSequence}`,
    value: typeof item === "string" ? item : JSON.stringify(item) ?? "", raw: item, edited: false, retained: true }));
  return rows.length ? rows : [{ id: `approval-output-${++approvalOutputSequence}`, value: "", raw: "", edited: false, retained: false }];
}
function ApprovalShownOutputs({ ctx }: Readonly<{ ctx: WorkflowStepSmartSetupContext }>) {
  const { t } = useI18n();
  const stored = ctx.stepKindConfig.show_outputs;
  const basis = JSON.stringify(stored);
  const [local, setLocal] = useState(() => ({ basis, rows: approvalRowsFromStored(stored) }));
  let rows = local.rows;
  if (local.basis !== basis) {
    rows = approvalRowsFromStored(stored);
    setLocal({ basis, rows });
  }
  function commit(next: ApprovalOutputRow[]) {
    const shown = next.length ? next : approvalRowsFromStored([]);
    const values = shown.filter((row) => (row.retained && !row.edited) || row.value.trim() !== "")
      .map((row) => row.edited ? row.value : row.raw);
    setLocal({ basis: JSON.stringify(values), rows: shown });
    ctx.replaceStepKindConfigField("approval", "show_outputs", values);
  }
  return <div className="space-y-2">
    <div className="flex items-center justify-between gap-2">
      <p className="text-xs fs-ink-secondary">{t("react.workflows.approval_show_outputs", "Outputs shown to the reviewer")}</p>
      <button className="fs-button secondary sm" onClick={() => commit([...rows, { id: `approval-output-${++approvalOutputSequence}`, value: "", raw: "", edited: false, retained: false }])} type="button">
        {t("react.workflows.add_reviewer_output", "Add reviewer output")}</button>
    </div>
    {rows.map((row, index) => <div className="flex items-start gap-2" key={row.id}>
      <div className="min-w-0 flex-1">{ctx.renderWorkflowVariableInput({
        ariaLabel: t("react.workflows.reviewer_output_number", "Reviewer output {number}", { number: index + 1 }),
        className: "fs-input w-full", onChange: (value: string) => commit(rows.map((item) => item.id === row.id ? { ...item, value, edited: true } : item)),
        panelId: `approval-output-${row.id}`, placeholder: t("react.workflows.select_value_to_show", "Select a value to show"), value: row.value,
      })}</div>
      <button aria-label={t("react.workflows.remove_reviewer_output_number", "Remove reviewer output {number}", { number: index + 1 })}
        className="fs-button danger sm" disabled={rows.length === 1 && !row.value}
        onClick={() => commit(rows.filter((item) => item.id !== row.id))} type="button">{t("extensions.repeater.remove", "Remove")}</button>
    </div>)}
  </div>;
}

export function WorkflowStepKindSmartSetup({ ctx }: Readonly<{ ctx: WorkflowStepSmartSetupContext }>) {
  const { t } = useI18n();
  const numericErrorId = useId();
  const returnedFilesId = useId();
  const {
    availableWorkflowVariableSuggestions,
    batchResultTargetListId,
    batchSourcePathListId,
    callableWorkflowOptions,
    collectKeyListId,
    collectSourceListId,
    collectTargetListId,
    handleApplyIfConditionFix,
    renderSwitchCasesEditor,
    renderUnsupportedIfConditionNotice,
    renderWorkflowContractMappingEditor,
    renderWorkflowVariableInput,
    replaceStepKindConfigField,
    rootIfSelectorListId,
    rootSwitchSelectorListId,
    selectedCallableWorkflow,
    agentCatalogQuery,
    setStepAgentId,
    stepAgentId,
    stepAgentLocked,
    stepAgentOptions,
    stepKindConfig,
    stepKindDraft,
    updateIfConditionDraft,
    updateStepKindConfig,
  } = ctx;
    const inputClass =
      "fs-input mt-1";
    function renderAgentSetup() {
      if (ctx.agentDrafts) return <AgentBindingFields ctx={ctx as AgentStepEditorContext} key={ctx.agentDrafts.editorKey} />;
      return (
        <div className="space-y-2">
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <span>{t("react.workflows.agent", "Agent")}</span>
            <DsSelect
              ariaLabel={t("react.workflows.agent", "Agent")}
              className="mt-1"
              disabled={stepAgentLocked || agentCatalogQuery.isLoading}
              id="step-agent-select"
              onChange={(nextValue) => setStepAgentId(nextValue)}
              value={stepAgentId}
              options={[...stepAgentOptions.map((option: { label: string; value: string }) => (
                ({ value: option.value, label: option.label })
              ))]}
            />
          </label>
          {agentCatalogQuery.isLoading ? (
            <p className="text-xs text-[var(--fs-text-tertiary)]">{t("react.workflows.loading_agent_catalog", "Loading agent catalog…")}</p>
          ) : null}
          {agentCatalogQuery.isError ? (
            <p className="rounded-lg border border-[var(--fs-status-danger-border)] bg-[var(--fs-status-danger-bg)] px-3 py-2 text-xs text-[var(--fs-status-danger)]">
              {t("react.workflows.agent_catalog_could_not_be_loaded_reopen_this_step_to_retry", "Agent catalog could not be loaded. Reopen this step to retry.")}</p>
          ) : null}
          {!agentCatalogQuery.isLoading && !agentCatalogQuery.isError && !stepAgentOptions.length ? (
            <p className="rounded-lg border border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] px-3 py-2 text-xs text-[var(--fs-status-attention-ink)]">
              {t("react.workflows.no_agents_are_available_create_or_enable_an_agent_in_registry_before_saving_this_step", "No agents are available. Create or enable an agent in Registry before saving this step.")}</p>
          ) : null}
        </div>
      );
    }
    function renderIfSetup() {
      const ifDraft = ifConditionDraftFromUnknown(stepKindConfig.condition);
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.if_condition", "If condition")}</p>
          {renderUnsupportedIfConditionNotice(ifDraft, () =>
            ignoreAsyncResult(handleApplyIfConditionFix(ifDraft)),
          )}
          <div className="grid gap-2 sm:grid-cols-3">
            {!IF_CONSTANT_OPERATORS.has(ifDraft.operator) ? (
              <label className="block text-xs text-[var(--fs-text-secondary)]">
                <span>{t("react.workflows.selector_variant_f02a172a", "Selector")}</span>
                {/* The variable combobox keeps its keys from the canvas itself
                    (stopPropagation in renderWorkflowVariableInput). */}
                {renderWorkflowVariableInput({
                  ariaLabel: t("react.workflows.selector_variant_f02a172a", "Selector"),
                  className: inputClass,
                  onChange: (nextValue: string) =>
                    updateIfConditionDraft({ selector: nextValue }),
                  panelId: rootIfSelectorListId,
                  placeholder: "state.inputs.city",
                  value: ifDraft.selector,
                })}
              </label>
            ) : null}
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("users.role.operator", "Operator")}</span>
              <DsSelect
                ariaLabel={t("users.role.operator", "Operator")}
                className="mt-1"
                onChange={(nextValue) =>
                  updateIfConditionDraft({ operator: nextValue })}
                value={ifDraft.operator}
                options={[...localizedIF_OPERATOR_OPTIONS(t).map((option: { label: string; value: string }) => (
                  ({ value: option.value, label: option.label })
                ))]}
              />
            </label>
            {IF_OPERATORS_WITH_VALUE.has(ifDraft.operator) ? (
              <label className="block text-xs text-[var(--fs-text-secondary)]">
                <span>{t("project_edit.fields.value", "Value")}</span>
                <input
                  className={inputClass}
                  onChange={(event) =>
                    updateIfConditionDraft({ value: event.target.value })
                  }
                  placeholder="Berlin"
                  value={ifDraft.value}
                />
              </label>
            ) : null}
          </div>
          <p className="text-[11px] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.if_outcomes_hint", "Connect the True and False outcomes on the canvas or under Connections.")}
          </p>
        </div>
      );
    }
    function renderSwitchSetup() {
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.switch_routing", "Switch routing")}</p>
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <span>{t("react.workflows.selector_variant_f02a172a", "Selector")}</span>
            {renderWorkflowVariableInput({
              ariaLabel: t("react.workflows.selector_variant_f02a172a", "Selector"),
              className: inputClass,
              onChange: (nextValue: string) =>
                replaceStepKindConfigField("switch", "selector", nextValue),
              panelId: rootSwitchSelectorListId,
              placeholder: "task.mode",
              value: textOrEmpty(stepKindConfig.selector),
            })}
          </label>
          {renderSwitchCasesEditor()}
        </div>
      );
    }
    function renderTransformSetup() {
      return <WorkflowTransformBuilder ctx={ctx} />;
    }
    function renderTemplateSetup() {
      return <TemplateStepSetup ctx={ctx} inputClass={inputClass} />;
    }
    function renderTabularFeedSetup() {
      return <TabularFeedBuilder ctx={ctx} inputClass={inputClass} />;
    }
    function renderInvokeWorkflowSetup() {
      if (Object.hasOwn(stepKindConfig, "hook")) return <HookDeclarationEditor
        key={ctx.hookDeclarationEditorKey}
        declaration={objectOrEmpty(stepKindConfig.hook) as unknown as WorkflowHookContract}
        identityLocked={Boolean(ctx.hookDeclarationIdentityLocked)} readOnly={Boolean(ctx.hookDeclarationReadOnly)}
        issues={ctx.hookDeclarationIssues} renderVariableInput={renderWorkflowVariableInput}
        onChange={(next) => replaceStepKindConfigField("invoke_workflow", "hook", next)}
        onValidationChange={ctx.onStepInputValidationChange} />;
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.child_workflow_variant_e3ea54ba", "Child workflow")}</p>
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <RequiredLabel>{t("react.workflows.workflow_variant_2e2d5c56", "Workflow")}</RequiredLabel>
            <DsSelect
              ariaLabel={t("react.workflows.workflow_variant_2e2d5c56", "Workflow")}
              required
              className="mt-1"
              onChange={(nextValue) =>
                replaceStepKindConfigField(
                  "invoke_workflow",
                  "workflow_id",
                  nextValue,
                )}
              value={textOrEmpty(stepKindConfig.workflow_id)}
              options={[({ value: "", label: t("react.workflows.choose_callable_workflow", "Choose callable workflow") }), ...callableWorkflowOptions.map((option: { label: string; value: string }) => (
                ({ value: option.value, label: option.label })
              ))]}
            />
          </label>
          {renderWorkflowContractMappingEditor(
            "input_mapping",
            t("react.workflows.display_input_mapping", "Input mapping"),
            contractRows(selectedCallableWorkflow.expected_inputs),
            "state.city",
            availableWorkflowVariableSuggestions,
          )}
          {renderWorkflowContractMappingEditor(
            "output_mapping",
            t("react.workflows.output_mapping", "Output mapping"),
            contractRows(selectedCallableWorkflow.expected_outputs),
            "state.weather_summary",
            availableWorkflowVariableSuggestions,
          )}
          {renderWorkflowContractMappingEditor(
            "artifact_mapping",
            t("react.workflows.returned_files_mapping", "Returned files mapping"),
            contractRows(selectedCallableWorkflow.returned_files),
            "state.report_file",
            availableWorkflowVariableSuggestions,
          )}
        </div>
      );
    }
    function renderSetVariableSetup() {
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.set_variables", "Set variables")}</p>
          <SetVariableAssignments ctx={ctx} />
        </div>
      );
    }
    /** Collect adds one value per item to a variable that exists before the For each. */
    function renderCollectSetup() {
      const setKey = stepKindConfig.mode === "set_key";
      const rawKey = stepKindConfig.key;
      const keyFromVariable = Boolean(rawKey && typeof rawKey === "object");
      const keySource = keyFromVariable ? textOrEmpty((rawKey as Record<string, unknown>).source) : "";
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3" data-testid="collect-setup">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.step_kind_collect", "Collect")}</p>
          <p className="text-xs leading-5 text-[var(--fs-text-secondary)]">
            {t("react.workflows.collect_hint", "Adds one value to a variable for each item. Create the variable before the For each (a Set Variable to [] or {}, or a workflow input).")}
          </p>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.collect_target", "Variable to add to")}</span>
              {renderWorkflowVariableInput({
                ariaLabel: t("react.workflows.collect_target", "Variable to add to"),
                className: inputClass,
                onChange: (nextValue: string) => replaceStepKindConfigField("collect", "target", nextValue),
                panelId: collectTargetListId,
                placeholder: "state.emails",
                value: textOrEmpty(stepKindConfig.target),
              })}
            </label>
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.collect_mode", "How to add")}</span>
              <select
                aria-label={t("react.workflows.collect_mode", "How to add")}
                className={inputClass}
                onChange={(event) => {
                  const mode = event.target.value === "set_key" ? "set_key" : "append";
                  updateStepKindConfig("collect", { mode, key: mode === "set_key" ? stepKindConfig.key : undefined });
                }}
                value={setKey ? "set_key" : "append"}
              >
                <option value="append">{t("react.workflows.collect_mode_append", "Add to the end of a list")}</option>
                <option value="set_key">{t("react.workflows.collect_mode_set_key", "Set a key in an object")}</option>
              </select>
            </label>
            <label className="block text-xs text-[var(--fs-text-secondary)] sm:col-span-2">
              <span>{t("react.workflows.collect_source", "Value")}</span>
              {renderWorkflowVariableInput({
                ariaLabel: t("react.workflows.collect_source", "Value"),
                className: inputClass,
                onChange: (nextValue: string) => replaceStepKindConfigField("collect", "source", nextValue),
                panelId: collectSourceListId,
                placeholder: "state.order.email",
                value: textOrEmpty(stepKindConfig.source),
              })}
            </label>
          </div>
          {setKey ? (
            <div className="space-y-2">
              <label className="inline-flex items-center gap-2 text-xs text-[var(--fs-text-secondary)]">
                <input
                  checked={keyFromVariable}
                  onChange={(event) =>
                    replaceStepKindConfigField("collect", "key", event.target.checked ? { source: "" } : "")
                  }
                  type="checkbox"
                />
                <span>{t("react.workflows.collect_key_from_variable", "Read the key from a variable")}</span>
              </label>
              <label className="block text-xs text-[var(--fs-text-secondary)]">
                <span>{t("react.workflows.collect_key", "Key")}</span>
                {keyFromVariable ? (
                  renderWorkflowVariableInput({
                    ariaLabel: t("react.workflows.collect_key", "Key"),
                    className: inputClass,
                    onChange: (nextValue: string) => replaceStepKindConfigField("collect", "key", { source: nextValue }),
                    panelId: collectKeyListId,
                    placeholder: "state.order.id",
                    value: keySource,
                  })
                ) : (
                  <input
                    aria-label={t("react.workflows.collect_key", "Key")}
                    className={inputClass}
                    onChange={(event) => replaceStepKindConfigField("collect", "key", event.target.value)}
                    placeholder="a.b@example.test"
                    value={typeof rawKey === "string" ? rawKey : ""}
                  />
                )}
              </label>
              <p className="text-[11px] text-[var(--fs-text-tertiary)]">
                {t("react.workflows.collect_key_literal_hint", "The key is used exactly as written: dots do not create nested objects.")}
              </p>
            </div>
          ) : null}
        </div>
      );
    }
    /** Break and Continue have no settings: they end the item or the whole For each. */
    function renderLoopControlSetup() {
      return (
        <p className="rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3 text-xs leading-5 text-[var(--fs-text-secondary)]">
          {stepKindDraft === "break"
            ? t("react.workflows.break_hint", "Leaves the For each now. The items after this one are not processed; the For each finishes with Success.")
            : t("react.workflows.continue_hint", "Ends this item now and goes on with the next one.")}
        </p>
      );
    }
    function renderBatchSetup() {
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.batch_processing", "Batch processing")}</p>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.source_path", "Source path")}</span>
              {renderWorkflowVariableInput({
                ariaLabel: t("react.workflows.source_path", "Source path"),
                className: inputClass,
                onChange: (nextValue: string) =>
                  replaceStepKindConfigField("batch", "source_path", nextValue),
                panelId: batchSourcePathListId,
                placeholder: "state.items",
                value: textOrEmpty(stepKindConfig.source_path),
              })}
            </label>
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.result_target_variant_6c946b46", "Result target")}</span>
              {renderWorkflowVariableInput({
                ariaLabel: t("react.workflows.result_target_variant_6c946b46", "Result target"),
                className: inputClass,
                onChange: (nextValue: string) =>
                  replaceStepKindConfigField(
                    "batch",
                    "result_target",
                    nextValue,
                  ),
                panelId: batchResultTargetListId,
                placeholder: "state.batches",
                value: textOrEmpty(stepKindConfig.result_target),
              })}
            </label>
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.batch_size_variant_8cfe32a0", "Batch size")}</span>
              <input
                className={inputClass}
                max={MAX_WORKFLOW_BATCH_SIZE}
                min={1}
                onChange={(event) =>
                  replaceStepKindConfigField(
                    "batch",
                    "batch_size",
                    clampWorkflowBatchSize(event.target.value),
                  )
                }
                type="number"
                value={textOrEmpty(stepKindConfig.batch_size) || "10"}
              />
            </label>
            <label className="mt-6 inline-flex items-center gap-2 text-xs text-[var(--fs-text-secondary)]">
              <input
                checked={stepKindConfig.emit_partial_batch !== false}
                onChange={(event) =>
                  replaceStepKindConfigField(
                    "batch",
                    "emit_partial_batch",
                    event.target.checked,
                  )
                }
                type="checkbox"
              />
              <span>{t("react.workflows.emit_partial_final_batch", "Emit partial final batch")}</span>
            </label>
          </div>
        </div>
      );
    }
    function renderFileFromBase64Setup() {
      const key = "file_from_base64";
      const textField = (field: string, label: string, placeholder: string, hint = "") => (
        <label className="block text-xs text-[var(--fs-text-secondary)]" key={field}>
          <span>{label}</span>
          <input
            className={inputClass}
            onChange={(event) => replaceStepKindConfigField(key, field, event.target.value)}
            placeholder={placeholder}
            value={textOrEmpty(stepKindConfig[field])}
          />
          {hint ? <span className="mt-1 block text-[11px] text-[var(--fs-text-tertiary)]">{hint}</span> : null}
        </label>
      );
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.save_file_from_base64", "Save file from base64")}</p>
          {textField(
            "source",
            t("react.workflows.display_base64_source", "Base64 source"),
            "state.outputs.image_response.data[0].b64_json",
            t("react.workflows.display_base64_source_help", "A base64 string, a data: URL, or an object with b64_json / data and mime_type."),
          )}
          {textField("filename", t("react.workflows.display_file_name", "File name"), "generated.png", t("react.workflows.display_file_extension_help", "Optional. An extension is added from the file type."))}
          {textField("content_type", t("react.workflows.display_content_type", "Content type"), "image/png", t("react.workflows.display_file_type_help", "Optional. Otherwise recognised from the file itself."))}
          {textField("result_target", t("react.workflows.display_save_file_handle", "Save file handle to"), "state.outputs.generated_file")}
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <span>{t("react.workflows.size_limit_bytes_optional_can_only_lower_fs_base64_file_max_bytes", "Size limit, bytes (optional, can only lower FS_BASE64_FILE_MAX_BYTES)")}</span>
            <input
              className={inputClass}
              min={1}
              onChange={(event) =>
                replaceStepKindConfigField(
                  key,
                  "max_bytes",
                  event.target.value ? Number(event.target.value) : "",
                )
              }
              placeholder={t("react.workflows.server_limit", "Server limit")}
              type="number"
              value={textOrEmpty(stepKindConfig.max_bytes)}
            />
          </label>
        </div>
      );
    }
    function renderStopSetup() {
      const key = stepKindDraft;
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.stop_outcome", "Stop outcome")}</p>
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <span>{t("registry.workflow_detail.field.message", "Message")}</span>
            <input
              className={inputClass}
              onChange={(event) =>
                replaceStepKindConfigField(key, "message", event.target.value)
              }
              placeholder={t("react.workflows.optional_outcome_message", "Optional outcome message")}
              value={textOrEmpty(stepKindConfig.message)}
            />
          </label>
        </div>
      );
    }
    function renderApprovalSetup() {
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.approval_request", "Approval request")}</p>
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <span>{t("registry.workflow_detail.field.message", "Message")}</span>
            <textarea
              className="fs-textarea mt-1 min-h-20"
              onChange={(event) => replaceStepKindConfigField("approval", "message", event.target.value)}
              placeholder={t("react.workflows.approval_message_placeholder", "What should the reviewer decide?")}
              value={textOrEmpty(stepKindConfig.message)}
            />
          </label>
          <ApprovalShownOutputs ctx={ctx} />
          <p className="text-[11px] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.approval_outcomes_hint", "Connect the Approved and Rejected outcomes on the canvas or under Connections.")}
          </p>
        </div>
      );
    }
    function renderWaitSetup() {
      const mapping = objectOrEmpty(stepKindConfig.input_mapping);
      const mapped = "duration_ms" in mapping;
      const errors = stepNumericFieldErrors("wait", stepKindConfig, t);
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.wait_duration", "Wait duration")}</p>
          <div className="grid gap-2 sm:grid-cols-2">
            <button
              aria-pressed={!mapped}
              className={`fs-button sm ${mapped ? "secondary" : "primary"}`}
              onClick={() =>
                updateStepKindConfig("wait", {
                  duration_ms: mapped ? 60_000 : stepKindConfig.duration_ms,
                  input_mapping: undefined,
                })
              }
              type="button"
            >
              {t("react.workflows.wait_fixed_duration", "Fixed duration")}
            </button>
            <button
              aria-pressed={mapped}
              className={`fs-button sm ${mapped ? "primary" : "secondary"}`}
              onClick={() =>
                updateStepKindConfig("wait", {
                  duration_ms: undefined,
                  input_mapping: { ...mapping, duration_ms: mapped ? mapping.duration_ms : "state.wait_ms" },
                })
              }
              type="button"
            >
              {t("react.workflows.wait_mapped_duration", "Duration from a variable")}
            </button>
          </div>
          {mapped ? (
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.wait_duration_source", "Duration source (milliseconds)")}</span>
              <input
                className={inputClass}
                onChange={(event) =>
                  replaceStepKindConfigField("wait", "input_mapping", { duration_ms: event.target.value })
                }
                placeholder="state.wait_ms"
                value={textOrEmpty(mapping.duration_ms)}
                data-step-numeric-field="duration_source"
                aria-label={t("react.workflows.wait_duration_source", "Duration source (milliseconds)")}
                aria-invalid={errors.duration_source ? true : undefined}
                aria-describedby={errors.duration_source ? `${numericErrorId}-source` : undefined}
              />
              {errors.duration_source ? <span className="fs-field-helper error block" id={`${numericErrorId}-source`} role="alert">{errors.duration_source}</span> : null}
            </label>
          ) : (
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <RequiredLabel>{t("react.workflows.wait_duration_ms", "Duration, milliseconds")}</RequiredLabel>
              <input
                className={inputClass}
                min={0}
                onChange={(event) =>
                  replaceStepKindConfigField(
                    "wait",
                    "duration_ms",
                    numericDraftFromInput(event.target.value),
                  )
                }
                type="number"
                value={numericInputValue(stepKindConfig.duration_ms)}
                max={31_536_000_000}
                required
                data-step-numeric-field="duration_ms"
                aria-label={t("react.workflows.wait_duration_ms", "Duration, milliseconds")}
                aria-invalid={errors.duration_ms ? true : undefined}
                aria-describedby={errors.duration_ms ? `${numericErrorId}-duration` : undefined}
              />
              {errors.duration_ms ? <span className="fs-field-helper error block" id={`${numericErrorId}-duration`} role="alert">{errors.duration_ms}</span> : null}
            </label>
          )}
        </div>
      );
    }
    function renderConnectorSetup() {
      return <ConnectorStepSetup ctx={ctx} />;
    }
    function renderDatasetQuerySetup() {
      return <DatasetQueryStepSetup ctx={ctx} inputClass={inputClass} />;
    }
    function renderHttpRequestSetup() {
      return <HttpRequestStepSetup ctx={ctx} inputClass={inputClass} />;
    }
    const setupRenderers: Record<string, () => ReactNode> = {
      agent: renderAgentSetup,
      approval: renderApprovalSetup,
      batch: renderBatchSetup,
      connector: renderConnectorSetup,
      dataset_query: renderDatasetQuerySetup,
      file_from_base64: renderFileFromBase64Setup,
      break: renderLoopControlSetup,
      collect: renderCollectSetup,
      continue: renderLoopControlSetup,
      http_request: renderHttpRequestSetup,
      if: renderIfSetup,
      invoke_workflow: renderInvokeWorkflowSetup,
      set_variable: renderSetVariableSetup,
      stop_fail: renderStopSetup,
      stop_success: renderStopSetup,
      switch: renderSwitchSetup,
      tabular_feed: renderTabularFeedSetup,
      template: renderTemplateSetup,
      transform: renderTransformSetup,
      wait: renderWaitSetup,
    };
    /**
     * "Return from workflow" on each file result the step saves: ticked, the
     * file becomes one of the workflow's outputs; unticked, it stays inside.
     */
    function renderReturnedFileSettings() {
      const rows: StepFileResult[] = Array.isArray(ctx.stepFileResultRows) ? ctx.stepFileResultRows : [];
      if (!rows.length || typeof ctx.updateStepReturnedFiles !== "function") return null;
      const current: ReturnedFileEntry[] = Array.isArray(ctx.stepReturnedFiles) ? ctx.stepReturnedFiles : [];
      const label = t("react.workflows.return_from_workflow", "Return from workflow");
      return (
        <div className="space-y-2 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3" data-testid="step-returned-files">
          {rows.map((row) => {
            const entry = current.find((item) => item.result === row.result);
            const id = `${returnedFilesId}-${row.result}`;
            return (
              <div className="flex items-start gap-2 text-xs text-[var(--fs-text-primary)]" key={row.result}>
                <input
                  aria-describedby={`${id}-hint`}
                  aria-label={rows.length > 1 ? `${label}: ${row.result}` : undefined}
                  checked={Boolean(entry)}
                  className="mt-0.5 h-4 w-4 accent-[var(--fs-action-primary)]"
                  id={id}
                  onChange={(event) => ctx.updateStepReturnedFiles(
                    toggledReturnedFiles(current, row, event.target.checked, ctx.returnedFileTakenKeys || []),
                  )}
                  type="checkbox"
                />
                <span>
                  <label className="font-medium" htmlFor={id}>
                    {label}{rows.length > 1 ? <span className="font-mono text-[var(--fs-text-secondary)]">{" "}{row.result}</span> : null}
                  </label>
                  <span className="fs-field-helper mt-1 block" id={`${id}-hint`}>
                    {t("react.workflows.return_from_workflow_hint", "Make the result available to the calling workflow and in the run results")}
                    {entry ? <span className="font-mono">{" "}{t("react.workflows.return_from_workflow_output", "Output: {key}", { key: entry.key })}</span> : null}
                  </span>
                </span>
              </div>
            );
          })}
        </div>
      );
    }
    const kindSetup = setupRenderers[stepKindDraft]?.() ?? null;
    const returnedFiles = renderReturnedFileSettings();
    return returnedFiles ? <>{kindSetup}{returnedFiles}</> : kindSetup;
  }
