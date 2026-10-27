import { InlineConfirm } from "@/components/shared/inline-confirm";
import { Button } from "@/components/ui/button";
import { AgentAdvancedFields, type AgentStepEditorContext } from "./detail-agent-step-editor";
import type { HttpFieldFeedback } from "./detail-http-request-intent";
import { useI18n } from "@/lib/i18n/useI18n";
import { isToolCategory, localizedToolCategoryOptions } from "./detail-tool-categories";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, type ReactNode } from "react";
import { stepNumericFieldErrors, type StepNumericField } from "./detail-step-numeric-fields";

import {
  AutomationRuleEditor,
  EXPLICIT_EMPTY_TEST_VALUE,
  inputIsText,
  inputSupportsBoolean,
  inputSupportsJson,
  inputSupportsNumber,
  inputSupportsParagraph,
  objectOrEmpty,
  renderKeyValueSummary,
  savedHttpTestRunSummary,
  stepTestRunFailureDetail,
  renderStepResultSummary,
  localizedSTEP_KIND_OPTIONS,
  textOrEmpty,
} from "@/pages/workflows/detail/detail-helpers";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { TemplateTestRunResult } from "@/pages/workflows/detail/detail-template-preview";
import type { StepTestRunResult } from "@/pages/workflows/shared/simulate-api";
import { TestRunEffectWarning } from "./detail-test-run-effect-warning";
import { StepTestRunLatest } from "./detail-step-test-run-latest";
import { declaredStepOutputTargets } from "./detail-test-run-results-model";
import { WorkflowStepIcon } from "@/pages/workflows/shared/workflow-step-icon";
import { FOR_EACH_ONLY_STEP_KINDS } from "@/pages/workflows/builder/change-set";
import { HookBindingEditor, HookTestRunPanel } from "@/pages/workflows/detail/detail-hook-binding-editor";
import { WorkflowHookProjectPicker } from "@/pages/workflows/detail/detail-schedule-project-context";
import type { HookFrame } from "@/pages/workflows/builder/hook-frames";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { DrawerFooter, DrawerFooterSpacer } from "./detail-drawer-footer";
import { SaveStateButton } from "./detail-save-button";
import type { PendingKindChange } from "./detail-step-actions";

type StepEditorSurfaceContext = Record<string, any> & AgentStepEditorContext & {
  hookTestRunBlockedReason: string;
  onHookTestRunStarted: (jobId: string, hookId: string) => void;
};
type StepTestRunInputRow = {
  name: string;
  options?: string[];
  required?: boolean;
  requiredByStep?: boolean;
  source?: string;
  type?: string;
};

function choose<T>(condition: boolean, yes: T, no: T): T {
  return condition ? yes : no;
}

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}


/** Kinds with exactly one outcome: the only kinds that can be inserted into an existing connection. */
const SINGLE_OUTCOME_STEP_KINDS = new Set([
  "agent",
  "connector",
  "dataset_query",
  "invoke_workflow",
  "set_variable",
  "collect",
  "batch",
  "http_request",
  "transform",
  "tabular_feed",
  "file_from_base64",
  "template",
  "wait",
]);

type StepSaveLabels = { label: string; savingLabel: string; doneLabel: string; doneAnnouncement: string };

/** Save parameters sends the step's settings; a step in the unapplied structure keeps them there (✓ Updated). */
function stepSaveLabels(
  t: (key: string, fallback: string) => string,
  selectedStep: { local?: boolean } | null,
  kindChange: "" | "new" | "staged" = "",
): StepSaveLabels {
  const staged = {
    savingLabel: t("react.workflows.save_parameters_saving", "Saving parameters…"),
    doneLabel: t("react.workflows.save_done_updated", "Updated"),
    doneAnnouncement: t("react.workflows.change_set_step_updated", "The new step's parameters are kept with the unapplied structure."),
  };
  if (selectedStep?.local) return { label: t("react.workflows.update_new_step", "Update new step"), ...staged };
  if (kindChange === "staged") return { label: t("react.workflows.update_kind_change", "Update kind change"), ...staged };
  if (kindChange === "new") return { label: t("react.workflows.save_parameters", "Save parameters"), ...staged };
  return {
    label: t("react.workflows.save_parameters", "Save parameters"),
    savingLabel: t("react.workflows.save_parameters_saving", "Saving parameters…"),
    doneLabel: t("react.workflows.save_done_saved", "Saved"),
    doneAnnouncement: t("react.workflows.status_parameters_saved", "Parameters saved"),
  };
}

export function StepEditorSurface({
  ctx,
}: Readonly<{
  ctx: StepEditorSurfaceContext;
}>) {
  const { t } = useI18n();
  const editorRef = useRef<HTMLDivElement>(null);
  const overrideRef = useRef<HTMLTextAreaElement>(null);
  const {
    advancedMode,
    drawerMode,
    drawerTab,
    handleDeleteSelectedStep,
    handleSaveStep,
    handleStepKindChange,
    hookTestRunBlockedReason,
    onHookTestRunStarted,
    extensionConnectionReadinessBlocked,
    connectorMissingRequiredInputNames,
    connectorRequiredInputsMissing,
    renderProjectLinkControl,
    renderStepKindSmartSetup,
    renderStepStructureSetup,
    savingWorkflowSurface,
    selectedProjectId,
    selectedStep,
    selectedStepAuthoredId,
    selectedStepId,
    setStepDisplayName,
    setStepOptional,
    setStepOverrideDraft,
    setStepTestRunInputs,
    setStepTestRunSummary,
    stepDisplayName,
    stepKindChangeStaged,
    stepKindDraft,
    stepKindSelection,
    stepOptional,
    stepCreateContext,
    stepOverrideDraft,
    stepSkillOptions,
    stepTestRunBlockedReason,
    stepTestRunInputRows,
    stepTestRunInputs,
    stepTestRunMessage,
    stepTestRunMutation,
    stepTestRunPayload,
    stepTestRunSummary,
    workflowDrawerNotice,
    workflowDrawerNoticeTitle,
    workflowId,
    workflowRunsBlocked,
    structureLocked,
  } = ctx;
  const canAuthor = useIsAdmin();
  // A hook step is an Invoke whose target the project binds; its declaration stays the author's.
  const stepHookId = textOrEmpty(
    objectOrEmpty(objectOrEmpty(objectOrEmpty(objectOrEmpty(selectedStep?.raw).override).invoke_workflow).hook).hook_id,
  );
  const declarationPending = Boolean(ctx.hookDeclarationPending);
  const bindingBlockedReason = declarationPending ? t("react.workflows.hook_apply_before_steps", "Apply the declaration before adding its steps.") : hookTestRunBlockedReason;
  // R2-11: a hook is a place for steps; the drawer explains it and can clear it.
  const stepHookFrame = stepHookId && ctx.hookFrames instanceof Map
    ? [...(ctx.hookFrames as Map<string, HookFrame>).values()].find((frame) => frame.hookId === stepHookId) : undefined;
  const hookEditor = stepHookId ? (
    <HookBindingEditor
      canAuthor={canAuthor}
      hookId={stepHookId}
      projectId={selectedProjectId || ""}
      workflowId={workflowId}
      stepCount={stepHookFrame?.stepCount || 0}
      onClear={typeof ctx.clearHook === "function" ? async () => {
        const confirmed = typeof ctx.requestConfirmation === "function" ? await ctx.requestConfirmation({
          title: t("react.workflows.hook_clear_title", "Clear the hook?"),
          body: t("react.workflows.hook_clear_body", "Every step inside {hook} is removed. Undo or Discard brings them back until you apply.", { hook: stepHookFrame?.displayName || stepHookId }),
          confirmLabel: t("react.workflows.hook_clear", "Clear the hook"),
        }) : true;
        if (confirmed) ctx.clearHook(stepHookId);
      } : undefined}
      projectPicker={<WorkflowHookProjectPicker workflowId={ctx.ownerWorkflowId || workflowId} projectId={selectedProjectId || ""} onProjectChange={ctx.onProjectChange} />}
    />
  ) : null;
  // Collect, Break and Continue live only in a sequence For each body.
  const placementForEachId = textOrEmpty(stepCreateContext?.forEachId) || textOrEmpty(selectedStep?.forEachId);
  const placementLaneId = stepCreateContext ? textOrEmpty(stepCreateContext.laneId) : textOrEmpty(selectedStep?.laneId);
  const inSequenceForEach = Boolean(placementForEachId && !placementLaneId);
  let kindChangeMode: "" | "new" | "staged" = "";
  if (selectedStep && !selectedStep.local) {
    if (stepKindChangeStaged) kindChangeMode = "staged";
    else if ((stepKindDraft || "agent") !== (selectedStep.stepKind || "agent")) kindChangeMode = "new";
  }
  const stepTestRunFailure = stepTestRunFailureDetail(stepTestRunPayload);
  const testRunPayload = objectOrEmpty(stepTestRunPayload);
  const testRunResult = testRunPayload as unknown as StepTestRunResult;
  const testRunAgent = objectOrEmpty(testRunPayload.agent);
  const updateStepTestRunInput = (name: string, value: string) => {
    setStepTestRunInputs((current: Record<string, string>) => ({
      ...current,
      [name]: value,
    }));
  };

  useEffect(() => {
    const feedback = ctx.httpFieldFeedback?.find((entry: HttpFieldFeedback) => entry.focus !== false && ctx.httpIntent?.isCurrent(entry.token, entry.field));
    if (!feedback || drawerMode !== "step" || stepKindDraft !== "http_request") return;
    if (!ctx.httpIntent.isCurrent(feedback.token, feedback.field)) return;
    if (drawerTab === "advanced") overrideRef.current?.focus();
    else if (drawerTab === "smart-setup") {
      const field = editorRef.current?.querySelector<HTMLElement>(`[data-http-field="${feedback.field}"]`);
      (field?.querySelector<HTMLElement>("input,textarea") || field?.querySelector<HTMLElement>('[role="combobox"]'))?.focus();
    }
  }, [ctx.httpFieldFeedback, ctx.httpIntent, drawerMode, drawerTab, stepKindDraft]);

  async function saveStepWithNumericFocus() {
    if (stepKindDraft === "agent" && ctx.agentDrafts?.invalid) { ctx.agentDrafts.focusInvalid(); return; }
    if (stepKindDraft === "http_request" && ctx.httpIntent?.hasRaw()) {
      const field = editorRef.current?.querySelector<HTMLElement>('[data-http-raw-invalid="true"]');
      (field?.querySelector<HTMLElement>("input,textarea") || field?.querySelector<HTMLElement>('[role="combobox"]'))?.focus();
      return;
    }
    let firstInvalid: StepNumericField | undefined;
    try {
      const override = objectOrEmpty(JSON.parse(stepOverrideDraft));
      const errors = stepNumericFieldErrors(stepKindDraft, objectOrEmpty(override[stepKindDraft]), t);
      firstInvalid = Object.keys(errors)[0] as StepNumericField | undefined;
    } catch {
      // The existing Save action owns malformed JSON and all other Save guards.
    }
    if (firstInvalid && drawerTab === "smart-setup") {
      const control = editorRef.current?.querySelector<HTMLInputElement>(`[data-step-numeric-field="${firstInvalid}"]`);
      if (control) {
        control.focus();
        return;
      }
    }
    await handleSaveStep();
    if (firstInvalid && drawerTab === "advanced") overrideRef.current?.focus();
  }

  const footerTab = drawerTab === "smart-setup" || drawerTab === "advanced";
  const kindChangeQuestion: PendingKindChange | null = ctx.pendingKindChange?.stepId && ctx.pendingKindChange.stepId === selectedStep?.runtimeId
    ? ctx.pendingKindChange
    : null;

  /** Delete step at the start, Save at the end; a new step offers Cancel beside Add step to changes. */
  function renderStepFooter(): ReactNode {
    if (drawerMode !== "step" || structureLocked || !footerTab || stepHookId && drawerTab !== "smart-setup") return null;
    if (kindChangeQuestion) {
      return (
        <DrawerFooter testId="step-kind-change-confirm">
          <InlineConfirm
            cancelLabel={t("common.cancel", "Cancel")}
            confirmLabel={t("react.workflows.change_step_kind_confirm", "Change kind")}
            onCancel={() => kindChangeQuestion.resolve(false)}
            onConfirm={() => kindChangeQuestion.resolve(true)}
            pendingLabel={t("react.workflows.change_step_kind_confirm", "Change kind")}
            question={t("react.workflows.change_step_kind_title", "Change this step's kind?")}
            tone="primary"
          >
            <p>{t(
              "react.workflows.change_step_kind_body",
              "The step keeps its ID, place, name and error policy; its other settings are replaced. Connections on outcomes the new kind does not have are removed; connect the new outcomes before applying.",
            )}</p>
            {kindChangeQuestion.dropped.length ? (
              <p className="font-mono">{t("react.workflows.change_step_kind_removed", "Removed: {connections}.", { connections: kindChangeQuestion.dropped.join("; ") })}</p>
            ) : null}
          </InlineConfirm>
        </DrawerFooter>
      );
    }
    const saving = savingWorkflowSurface === "step";
    const blocked = Boolean(extensionConnectionReadinessBlocked || (drawerTab === "smart-setup" && connectorRequiredInputsMissing));
    if (!selectedStep) {
      return (
        <DrawerFooter>
          <DrawerFooterSpacer />
          <Button disabled={saving} onClick={() => ctx.cancelNewDrawerItem?.()} type="button" variant="secondary">
            {t("common.cancel", "Cancel")}
          </Button>
          <Button
            disabled={saving || blocked}
            id="step-submit-btn"
            onClick={() => ignoreAsyncResult(saveStepWithNumericFocus())}
            type="button"
            variant="primary"
          >
            {t("react.workflows.add_step_to_changes", "Add step to changes")}
          </Button>
        </DrawerFooter>
      );
    }
    const labels = stepSaveLabels(t, selectedStep, kindChangeMode);
    return (
      <DrawerFooter>
        <Button disabled={saving} onClick={() => ignoreAsyncResult(handleDeleteSelectedStep())} type="button" variant="ghost-danger">
          {t("react.workflows.delete_step", "Delete step")}
        </Button>
        <DrawerFooterSpacer />
        <SaveStateButton
          {...labels}
          dirty={Boolean(selectedStep.local || kindChangeMode || ctx.stepFormDirty || ctx.stepFormUnsaved || ctx.stepFormSaveState === "error")}
          disabled={blocked}
          doneSignal={ctx.stepSaveSerial?.count}
          failed={ctx.stepFormSaveState === "error"}
          id="step-submit-btn"
          onClick={() => ignoreAsyncResult(saveStepWithNumericFocus())}
          saving={saving}
        />
      </DrawerFooter>
    );
  }

  return (
        <div
          ref={editorRef}
          className={`workflow-builder-step-form ${choose(drawerMode === "step", "mt-3 block", "mt-3 hidden")}`}
          id="modal-add-step-panel"
        >
          <input id="step-id" readOnly type="hidden" value={selectedStepId} />
          {renderWhen(drawerMode === "step" && drawerTab === "smart-setup" && Boolean(structureLocked), (
            <div className="mt-2 space-y-2" data-testid="protected-step-view">
              <div className="fs-surface-radius border border-subtle fs-surface-inset p-3 text-xs fs-ink-secondary">
                <p className="inline-flex items-center gap-2 text-sm fs-ink-primary">
                  <WorkflowStepIcon kind={stepKindDraft} />
                  {stepDisplayName || selectedStepId}
                </p>
                <p className="mt-2">
                  {t("react.workflows.protected_step_hint", "This step belongs to a protected workflow. You can read it, but not change it or run it on its own.")}
                </p>
              </div>
              {hookEditor}
            </div>
          ))}
          {renderWhen(drawerMode === "step" && drawerTab === "smart-setup" && !structureLocked, (
            <div className="mt-2 space-y-2">
              <div
                className="space-y-2"
                data-skill-option-count={stepSkillOptions.length}
                id="step-executor-advanced"
              >
                <label className="block text-xs fs-ink-secondary">
                  <span>{t("react.workflows.step_label", "Step label")}</span>
                  <input
                    className="fs-input mt-1 w-full"
                    name="step_display_name"
                    onChange={(event) => {
                      if (stepKindDraft === "http_request") ctx.httpIntent?.touchStepParameters();
                      if (stepKindDraft === "agent") ctx.agentDrafts?.touchOverride();
                      setStepDisplayName(event.target.value);
                    }}
                    value={stepDisplayName}
                  />
                </label>
                <label className="block text-xs fs-ink-secondary">
                  <span className="inline-flex items-center gap-2"><WorkflowStepIcon kind={stepKindDraft} />{t("react.workflows.step_kind", "Step kind")}</span>
                  <select
                    className="fs-input mt-1 w-full"
                    name="step_kind"
                    onChange={(event) =>
                      ignoreAsyncResult(handleStepKindChange(event.target.value))
                    }
                    value={stepKindSelection || stepKindDraft}
                  >
                    {localizedSTEP_KIND_OPTIONS(t)
                      .flatMap((option) => option.value === "connector" ? localizedToolCategoryOptions(t) : [option])
                      .filter((option) => !stepCreateContext?.insert || stepCreateContext.insert.append || SINGLE_OUTCOME_STEP_KINDS.has(isToolCategory(option.value) ? "connector" : option.value))
                      .filter((option) =>
                        option.value === stepKindDraft ||
                        !FOR_EACH_ONLY_STEP_KINDS.has(option.value) ||
                        inSequenceForEach,
                      )
                      .map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                  </select>
                </label>
                {kindChangeMode ? (
                  <p className="text-[11px] fs-ink-attention" data-testid="step-kind-change-hint">
                    {t("react.workflows.step_kind_change_saving_hint", "Saving replaces this step's settings with the new kind's defaults.")}
                  </p>
                ) : null}
                {renderWhen(Boolean(!advancedMode && stepOptional), (
                  <div className="rounded-lg border border-default fs-surface-inset px-3 py-2 text-[11px] fs-ink-secondary">
                    {t("react.workflows.advanced_settings_already_configured", "Advanced settings already configured:")}{" "}
                    {t("react.workflows.optional_step_enabled", "Optional step enabled.")}
                  </div>
                ))}
                {stepHookId ? <>
                  {renderStepKindSmartSetup()}
                  <section className="flex flex-col gap-2" aria-label={t("react.workflows.hook_steps_section", "Steps of this hook")}>
                    <h3 className="text-sm font-medium fs-ink-primary">{t("react.workflows.hook_steps_section", "Steps of this hook")}</h3>
                    {declarationPending ? <p className="text-xs fs-ink-secondary" role="status">{bindingBlockedReason}</p> : hookEditor}
                  </section>
                </> : renderStepKindSmartSetup()}
                {renderStepStructureSetup()}
              </div>
            </div>
          ))}
          {renderWhen(drawerMode === "step" && drawerTab === "automation", (
            <div
              className="mt-2 rounded-xl border border-default fs-surface-inset p-3"
              id="step-automation-surface"
            >
              <p className="text-xs uppercase tracking-[0.12em] fs-ink-secondary">
                {t("react.workflows.step_automation", "Step automation")}</p>
              <p className="mt-1 text-xs fs-ink-secondary">
                {t("react.workflows.step_automation_hint", "React when this step starts, completes, fails or is blocked.")}</p>
              <AutomationRuleEditor
                onDirtyChange={ctx.onAutomationDirtyChange}
                projectId={selectedProjectId}
                scopeId={selectedStep?.runtimeId || selectedStepId}
                scopeKind="step"
                stepKind={selectedStep?.stepKind || stepKindDraft}
                workflowId={workflowId}
              />
            </div>
          ))}
          {/* B28: only the step drawer carries a Step Test Run. The workflow Test
              Run drawer shares the "test-run" tab key, and this section used
              to stay in its DOM (hidden by CSS but still clickable by assistive
              tools), offering a protected workflow's step run the server refuses. */}
          {renderWhen(drawerMode === "step" && drawerTab === "test-run" && Boolean(stepHookId), (
            declarationPending ? <p className="text-xs fs-ink-secondary" role="status">{bindingBlockedReason}</p> : <HookTestRunPanel key={`${workflowId}:${stepHookId}`} onTestRunStarted={onHookTestRunStarted} blockedReason={bindingBlockedReason} hookId={stepHookId} projectId={selectedProjectId || ""} workflowId={workflowId} />
          ))}
          {renderWhen(drawerMode === "step" && drawerTab === "test-run" && !stepHookId, (
            <div className="mt-2 space-y-2">
              <StepTestRunLatest commentDraft={ctx.approvalCommentDraft} evidence={ctx.testRunEvidence} step={selectedStep ? {
                stepId: selectedStep.runtimeId || selectedStepId, label: selectedStep.primaryLabel || selectedStepId,
                kind: selectedStep.stepKind, local: Boolean(selectedStep.local), declaredOutputs: declaredStepOutputTargets(ctx.stepContractPreviewQuery?.data),
              } : null} />
              {renderWhen(Boolean(workflowDrawerNotice), (
                <div className="rounded-lg border border-attention fs-surface-attention p-3 text-xs fs-ink-attention">
                  <p className="font-medium uppercase tracking-[0.12em] fs-ink-attention">
                    {workflowDrawerNoticeTitle}
                  </p>
                  <p className="mt-1 fs-ink-attention">
                    {workflowDrawerNotice}
                  </p>
                </div>
              ))}
              <TestRunEffectWarning />
              {selectedStep?.stepKind === "http_request" ? renderKeyValueSummary(
                savedHttpTestRunSummary(
                  objectOrEmpty(objectOrEmpty(selectedStep.raw).override).http_request,
                  t("react.workflows.uses_saved_configuration", "Uses saved configuration"),
                ),
                "—",
                t,
              ) : null}
              {renderWhen(!selectedProjectId, renderProjectLinkControl())}
              <label className="block text-xs uppercase tracking-[0.12em] fs-ink-secondary">
                <span>{t("react.workflows.step_test_run_summary", "Step Test Run Summary")}</span>
                <textarea
                  className="fs-textarea mt-1 min-h-24 w-full"
                  onChange={(event) => setStepTestRunSummary(event.target.value)}
                  value={stepTestRunSummary}
                />
              </label>
              {renderWhen(Boolean(stepTestRunInputRows.length), (
                <div className="space-y-2 rounded-lg border border-default fs-surface-inset p-3">
                  <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">
                    {t("react.workflows.step_inputs", "Step inputs")}</p>
                  <p className="text-[11px] leading-5 fs-ink-tertiary">
                    {t("react.workflows.step_test_input_help", "Test values are optional. Only this step runs; earlier steps and loop context are not loaded. A saved mapping alone does not preload a value. Leaving a value blank may make the test fail.")}
                  </p>
                  {stepTestRunInputRows.map((row: StepTestRunInputRow) => (
                    <label
                      className="block text-xs fs-ink-secondary"
                      key={`step-test-run-input-${row.name}`}
                    >
                      {row.name}
                      <span className="ml-2 fs-ink-tertiary">
                        {(row.requiredByStep ?? row.required) ? t("react.workflows.required_by_step", "Required by step") : t("react.workflows.optional", "Optional")} · {row.type}
                        {row.source ? ` · ${row.source}` : ""}
                      </span>
                      {Array.isArray(row.options) && row.options.length ? (
                        <select
                          className="fs-input mt-1 w-full"
                          onChange={(event) => updateStepTestRunInput(row.name, event.target.value)}
                          value={stepTestRunInputs[row.name] || ""}
                        >
                          <option value="">{t("extensions.repeater.select_placeholder", "Select…")}</option>
                          {row.options.map((option: string) => (
                            <option key={`${row.name}-${option}`} value={option}>
                              {option}
                            </option>
                          ))}
                        </select>
                      ) : null}
                      {(!Array.isArray(row.options) || !row.options.length) &&
                      inputSupportsBoolean(row.type) ? (
                        <select
                          className="fs-input mt-1 w-full"
                          onChange={(event) => updateStepTestRunInput(row.name, event.target.value)}
                          value={stepTestRunInputs[row.name] || ""}
                        >
                          <option value="">{t("extensions.repeater.select_placeholder", "Select…")}</option>
                          <option value="true">true</option>
                          <option value="false">false</option>
                        </select>
                      ) : null}
                      {(!Array.isArray(row.options) || !row.options.length) &&
                      (inputSupportsJson(row.type) || inputIsText(row.type)) ? (
                        // A text keeps line breaks and edge spaces, as a real run would.
                        <textarea
                          className="fs-textarea mt-1 w-full"
                          disabled={
                            inputIsText(row.type) &&
                            stepTestRunInputs[row.name] === EXPLICIT_EMPTY_TEST_VALUE
                          }
                          rows={inputIsText(row.type) && !inputSupportsParagraph(row.type) ? 2 : 4}
                          onChange={(event) => updateStepTestRunInput(row.name, event.target.value)}
                          value={
                            stepTestRunInputs[row.name] === EXPLICIT_EMPTY_TEST_VALUE
                              ? ""
                              : stepTestRunInputs[row.name] || ""
                          }
                        />
                      ) : null}
                      {(!Array.isArray(row.options) || !row.options.length) &&
                      inputSupportsNumber(row.type) ? (
                        <input
                          className="fs-input mt-1 w-full"
                          onChange={(event) => updateStepTestRunInput(row.name, event.target.value)}
                          type="number"
                          value={stepTestRunInputs[row.name] || ""}
                        />
                      ) : null}
                      {(!Array.isArray(row.options) || !row.options.length) && inputIsText(row.type) ? (
                        // An empty field means "not given"; this sends an empty text instead.
                        <span className="mt-1 flex items-center gap-2 text-[11px] fs-ink-secondary">
                          <input
                            aria-label={t("react.workflows.send_value0_as_empty_text", "Send {value0} as empty text", { value0: row.name })}
                            checked={stepTestRunInputs[row.name] === EXPLICIT_EMPTY_TEST_VALUE}
                            onChange={(event) =>
                              updateStepTestRunInput(
                                row.name,
                                event.target.checked ? EXPLICIT_EMPTY_TEST_VALUE : "",
                              )
                            }
                            type="checkbox"
                          />
                          {t("react.workflows.empty_text_not_missing", "Empty text (not missing)")}</span>
                      ) : null}
                    </label>
                  ))}
                </div>
              ))}
              {renderWhen(Boolean(stepTestRunBlockedReason), (
                <p
                  className="rounded-lg border border-attention fs-surface-attention px-3 py-2 text-xs fs-ink-attention"
                  data-testid="step-test-run-blocked"
                  role="status"
                >
                  {stepTestRunBlockedReason}
                </p>
              ))}
              <Button
                aria-label={t("react.workflows.run_step_test_run", "Run step test run")}
                variant="primary"
                disabled={
                  Boolean(stepTestRunBlockedReason) ||
                  stepTestRunMutation.isPending ||
                  workflowRunsBlocked ||
                  extensionConnectionReadinessBlocked ||
                  connectorRequiredInputsMissing ||
                  !firstStringFromUnknownValue([selectedProjectId, ""]).trim() ||
                  !firstStringFromUnknownValue([selectedStepId, ""]).trim()
                }
                onClick={() => stepTestRunMutation.mutate()}
                type="button"
              >
                {choose(stepTestRunMutation.isPending, t("react.workflows.display_running_step_test", "Running Step Test Run..."), t("registry.workflow_detail.run_lab.tab.test_run", "Test Run"))}
              </Button>
              {renderWhen(connectorRequiredInputsMissing, (
                <p
                  className="rounded-lg border border-attention fs-surface-attention px-3 py-2 text-xs fs-ink-attention"
                  role="alert"
                >
                  {t("react.workflows.complete_the_required_action_inputs_before_testing", "Complete the required action inputs before testing:")}{" "}{(
                    Array.isArray(connectorMissingRequiredInputNames)
                      ? connectorMissingRequiredInputNames
                      : []
                  ).join(", ")}.
                </p>
              ))}
              {renderWhen(Boolean(stepTestRunMessage), (
                <div className="rounded-lg border border-default fs-surface-inset px-3 py-2 text-xs fs-ink-primary">
                  {stepTestRunMessage}
                </div>
              ))}
              {renderWhen(Boolean(stepTestRunPayload), (
                <div className="rounded-xl border border-default fs-surface-inset p-3 text-xs fs-ink-primary">
                  <p className="font-medium uppercase tracking-[0.12em] fs-ink-secondary">
                    {t("react.workflows.step_test_run", "Step Test Run")}</p>
                  <div className="mt-2 space-y-1">
                    <p>{t("react.workflows.status", "Status:")}{" "}{textOrEmpty(testRunPayload.final_status) || textOrEmpty(testRunPayload.status) || t("project_detail.connections.status.unknown", "unknown")}</p>
                    <p>{t("react.workflows.step_kind_variant_5f7017f8", "Step kind:")}{" "}{textOrEmpty(testRunPayload.step_kind) || stepKindDraft}</p>
                    <p>{t("react.workflows.step_variant_41f3c275", "Step:")}{" "}{textOrEmpty(testRunPayload.step_id) || selectedStepAuthoredId}</p>
                  </div>
                  {renderWhen(Boolean(stepTestRunFailure), (
                    <div
                      className="mt-3 rounded-lg border border-danger fs-surface-danger p-3 fs-ink-danger"
                      role="alert"
                    >
                      <p className="font-medium uppercase tracking-[0.12em] fs-ink-danger">
                        {t("react.workflows.failure_reason", "Failure reason")}</p>
                      <p className="mt-1 whitespace-pre-wrap break-words">
                        {stepTestRunFailure}
                      </p>
                    </div>
                  ))}
                  {renderWhen(
                    Boolean(textOrEmpty(testRunPayload.summary))
                      && textOrEmpty(testRunPayload.summary) !== stepTestRunFailure,
                    (
                    <p className="mt-3 fs-ink-secondary">
                      {textOrEmpty(testRunPayload.summary)}
                    </p>
                  ))}
                  {renderWhen(stepKindDraft === "template", (
                    <TemplateTestRunResult
                      currentKey={`${textOrEmpty(stepOverrideDraft)}\n${JSON.stringify(stepTestRunInputs || {})}`}
                      result={testRunPayload.result}
                    />
                  ))}
                  {renderWhen(Boolean(testRunAgent.agent_id), (
                    <div className="mt-3 space-y-2 rounded-lg border border-default fs-surface-inset p-3">
                      <p>{t("react.workflows.agent_variant_687ebf2c", "Agent:")}{" "}{textOrEmpty(testRunAgent.agent_id)}</p>
                      <p>{t("registry.workflow_detail.step.skill_label", "Skill:")}{" "}{textOrEmpty(testRunAgent.skill)}</p>
                      <div>
                        <p className="fs-ink-secondary">{t("registry.workflow_detail.step.system_prompt", "System prompt")}</p>
                        <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded fs-surface-inset p-2 text-[11px] fs-ink-primary">
                          {textOrEmpty(testRunAgent.system_prompt) || t("react.workflows.display_not_returned", "Not returned")}
                        </pre>
                      </div>
                      <div>
                        <p className="fs-ink-secondary">{t("registry.workflow_detail.step.user_prompt", "User prompt")}</p>
                        <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded fs-surface-inset p-2 text-[11px] fs-ink-primary">
                          {textOrEmpty(testRunAgent.user_prompt) || t("react.workflows.display_not_returned", "Not returned")}
                        </pre>
                      </div>
                    </div>
                  ))}
                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                    <div>
                      {renderWhen(Boolean(textOrEmpty(testRunPayload.project_name)), (
                        <p className="mb-2 fs-ink-secondary">
                          <span className="fs-ink-secondary">{t("settings.notifications.project", "Project:")}{" "}</span>
                          {textOrEmpty(testRunPayload.project_name)}
                        </p>
                      ))}
                      <p className="fs-ink-secondary">{t("react.workflows.inputs_used", "Inputs used")}</p>
                      <div className="mt-1">
                        {renderKeyValueSummary(
                          // The project is the run's context, not one of the step's inputs.
                          Object.fromEntries(
                            Object.entries(objectOrEmpty(testRunPayload.inputs)).filter(
                              ([name]) => name !== "project_id",
                            ),
                          ),
                          t("react.workflows.display_step_no_inputs", "This step has no inputs."),
                          t,
                        )}
                      </div>
                    </div>
                    <section aria-label={t("react.workflows.step_test_run_result", "Step test run result")}>
                      <p className="fs-ink-secondary">{t("jobs.detail.inspector.result", "Result")}</p>
                      <div className="mt-1 rounded-lg border border-default fs-surface-inset p-3 fs-ink-primary">
                        {renderStepResultSummary(testRunResult, t)}
                      </div>
                    </section>
                  </div>
                  <details className="mt-3 rounded-lg border border-default fs-surface-inset p-3">
                    <summary className="cursor-pointer fs-ink-secondary">
                      {t("react.workflows.raw_debug_payload", "Raw debug payload")}</summary>
                    <div className="mt-3 grid gap-3 sm:grid-cols-2">
                      <div>
                        <p className="fs-ink-secondary">{t("react.workflows.raw_inputs", "Raw inputs")}</p>
                        <pre className="mt-1 overflow-x-auto rounded fs-surface-inset p-2 text-[11px] fs-ink-primary">
                          {JSON.stringify(testRunPayload.inputs || {}, null, 2)}
                        </pre>
                      </div>
                      <div>
                        <p className="fs-ink-secondary">{t("react.workflows.raw_result", "Raw result")}</p>
                        <pre className="mt-1 overflow-x-auto rounded fs-surface-inset p-2 text-[11px] fs-ink-primary">
                          {JSON.stringify(testRunPayload.result || {}, null, 2)}
                        </pre>
                      </div>
                    </div>
                  </details>
                </div>
              ))}
            </div>
          ))}
          {renderWhen(drawerTab === "advanced", (
            <div className="mt-2 space-y-2">
              <label className="inline-flex items-center gap-2 text-xs fs-ink-secondary">
                <input
                  checked={stepOptional}
                  name="optional"
                  onChange={(event) => {
                    if (stepKindDraft === "http_request") ctx.httpIntent?.touchStepParameters();
                    if (stepKindDraft === "agent") ctx.agentDrafts?.touchOverride();
                    setStepOptional(event.target.checked);
                  }}
                  type="checkbox"
                />{" "}
                <span>{t("registry.workflow_detail.step.optional_step", "Optional step")}</span>
              </label>
              {stepKindDraft === "agent" && ctx.agentDrafts ? <AgentAdvancedFields ctx={ctx} key={ctx.agentDrafts.editorKey} /> : null}
              <label className="block text-xs fs-ink-secondary">
                <span>{t("react.workflows.override_json", "Override JSON")}</span>
                <textarea
                  className="fs-textarea mt-1 min-h-28 w-full font-mono"
                  name="step_override_json"
                  ref={overrideRef}
                  onChange={(event) => {
                    if (stepKindDraft === "http_request") ctx.httpIntent?.touchOverride();
                    if (stepKindDraft === "agent") ctx.agentDrafts?.touchOverride();
                    setStepOverrideDraft(event.target.value);
                  }}
                  placeholder='{"if":{"condition":{"selector":"state.inputs.city","operator":"equals","value":"Berlin"}}}'
                  value={stepOverrideDraft}
                />
              </label>
            </div>
          ))}
          {renderStepFooter()}
        </div>
  );
}
