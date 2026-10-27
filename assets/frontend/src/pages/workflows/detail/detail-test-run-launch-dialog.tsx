import { Link } from "react-router";
import type { WorkflowNormalRunController } from "./detail-normal-run-controller";
import { useEffect, useId, useRef, type ChangeEvent, type ReactNode } from "react";
import { DsAlert, DsDialog, RequiredLabel } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import type { WorkflowChangeSetController } from "./detail-change-set";
import type { useWorkflowTestRunController } from "./detail-test-run-controller";
import { TestRunEffectWarning } from "./detail-test-run-effect-warning";
import type { CanvasRunBarProps } from "@/pages/workflows/builder/canvas-run-bar";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import { stepKindKeepsOutput } from "@/pages/workflows/shared/kept-test-output-api";
import type { WorkflowTestRunEvidence } from "./detail-test-run-evidence";
import {
  inputSupportsBoolean,
  inputSupportsJson,
  inputSupportsNumber,
  inputSupportsParagraph,
  workflowTestRunIsPending,
} from "./detail-helpers";

export type WorkflowTestRunLaunchContext = ReturnType<typeof useWorkflowTestRunController> & {
  canvasChangeSet: WorkflowChangeSetController;
  normalRun?: WorkflowNormalRunController;
  canvasRunBar?: Pick<CanvasRunBarProps, "selection" | "history" | "historyDisabled" | "navigationBusy" | "onSelectHistory">;
  /** Opens a step named in the dialog's notes, after the dialog closes. */
  openStepFromLaunch?: (nodeId: string) => unknown;
  closeWorkflowTestRunLaunch: () => void;
  requestWorkflowTestRunResults: () => void;
  renderProjectLinkControl: () => ReactNode;
  selectedProjectId: string;
  workflowTitle: string;
  workflowDrawerNotice: string;
  workflowDrawerNoticeTitle: string;
  workflowRunsBlocked: boolean;
  workflowTestRunBlockedReason: string;
  workflowTestRunLaunchOpen: boolean;
  /** The applied graph: which steps will fail as set now, which return a kept output. */
  testRunGraph?: WorkflowCanvasGraph | null;
  testRunEvidence?: WorkflowTestRunEvidence | null;
};

/** Presentation only: inputs, mutations and run lifetime remain owned by the page. */
export function WorkflowTestRunLaunchDialog({ ctx }: Readonly<{ ctx: WorkflowTestRunLaunchContext }>) {
  const { t } = useI18n();
  const id = useId();
  const initialFocusRef = useRef<HTMLElement | null>(null);
  const open = ctx.workflowTestRunLaunchOpen;
  const normalRun = ctx.normalRun;
  const busy = ctx.workflowTestRunMutation.isPending || Boolean(normalRun?.busy);
  const currentStartError = ctx.workflowTestRunMutationIsCurrent !== false && ctx.workflowTestRunMutation.isError;
  const applying = ctx.canvasChangeSet.status === "applying";
  const graph = ctx.testRunGraph;
  const kept = ctx.testRunEvidence?.kept;
  const keptSteps = (graph?.steps || []).filter((step) => kept?.has(step.step_id || step.id) && stepKindKeepsOutput(step.kind));
  const keptIds = new Set(keptSteps.map((step) => step.id));
  // Said ahead: steps that cannot run as they are set; the run still starts and stops at the first.
  const failing = (graph?.validation_issues || []).filter((issue) => issue.scope === "step" && (issue.severity || issue.level) === "error")
    .flatMap((issue) => {
      const step = graph?.steps.find((row) => row.id === issue.scope_id || row.step_id === (issue.step_id || issue.scope_id));
      if (!step || keptIds.has(step.id)) return [];
      const reason = issue.message_key ? t(issue.message_key, issue.message || "", issue.message_params) : issue.message || (issue.title_key ? t(issue.title_key, issue.title, issue.title_params) : issue.title);
      return [{ key: `${step.id}:${issue.code || reason}`, stepId: step.step_id || step.id, nodeId: step.id, name: step.display_name || step.id, reason }];
    });
  const blocked = Boolean(ctx.workflowTestRunBlockedReason) || busy || applying ||
    workflowTestRunIsPending(ctx.workflowTestRunPayload) || ctx.workflowRunsBlocked ||
    ctx.workflowTestRunHasValidationErrors || !ctx.selectedProjectId.trim();

  useEffect(() => {
    if (!open) return;
    const layer = initialFocusRef.current?.closest("dialog")?.parentElement;
    if (!layer) return;
    // DsDialog owns its focus trap. Isolate this launcher's actual background,
    // including the separately portalled contextual drawer, without unmounting
    // editor drafts or changing the shared overlay contract.
    const backgrounds = Array.from(document.body.children)
      .filter((element): element is HTMLElement => element instanceof HTMLElement && element !== layer && !element.matches(".fs-toast"))
      .map((element) => ({ element, inert: element.inert, hidden: element.getAttribute("aria-hidden") }));
    for (const { element } of backgrounds) {
      element.inert = true;
      element.setAttribute("aria-hidden", "true");
    }
    return () => {
      for (const { element, inert, hidden } of backgrounds) {
        element.inert = inert;
        if (hidden === null) element.removeAttribute("aria-hidden");
        else element.setAttribute("aria-hidden", hidden);
      }
    };
  }, [open]);

  return (
    <DsDialog
      open={open}
      onClose={ctx.closeWorkflowTestRunLaunch}
      title={t("react.workflows.start_a_test_run", "Start a test run")}
      eyebrow={t("registry.workflow_detail.run_lab.tab.test_run", "Test Run")}
      description={t("react.workflows.start_a_test_run_description", "Runs {workflow} with these inputs. Follow it on the canvas.", { workflow: ctx.workflowTitle })}
      variant="form"
      initialFocusRef={initialFocusRef}
      busy={busy || applying}
      footer={<>
        <Button type="button" variant="secondary" disabled={busy || applying} onClick={ctx.closeWorkflowTestRunLaunch}>{t("common.cancel", "Cancel")}</Button>
        {normalRun ? <Button type="button" variant="secondary" disabled={busy || normalRun.blocked || !normalRun.acknowledged} onClick={() => ignoreAsyncResult(normalRun!.start())}>
          {t("react.workflows.normal_run", "Run workflow")}
        </Button> : null}
        <Button type="button" variant="primary" aria-label={t("react.workflows.run_workflow_test_run", "Run workflow test run")} disabled={blocked} onClick={ctx.startWorkflowTestRun}>
          {ctx.workflowTestRunMutation.isPending ? t("react.workflows.starting_test_run", "Starting Test Run...") : t("react.workflows.start_test_run", "Start test run")}
        </Button>
      </>}
    >
      <div
        className="fs-dialog-form workflow-test-run-launch"
        data-workflow-test-run-launch
        ref={(element) => {
          initialFocusRef.current = element?.querySelector<HTMLElement>('select:not(:disabled), input:not(:disabled):not([readonly]):not([type="hidden"]), textarea:not(:disabled)') || null;
        }}
      >
        {/* Caller-scoped anatomy; shared Form Dialog defaults stay unchanged. */}
        <style>{`.fs-dialog:has([data-workflow-test-run-launch]) { --fs-form-dialog-body-padding: var(--fs-space-200) var(--fs-space-300) var(--fs-space-300); } .fs-dialog:has([data-workflow-test-run-launch]) > .fs-dialog-header, .fs-dialog:has([data-workflow-test-run-launch]) > .fs-dialog-footer { flex-shrink: 0; } .workflow-test-run-launch [role="note"] { color: var(--fs-text-primary); border-color: var(--fs-status-warning-border); background: var(--fs-status-warning-bg); }`}</style>
        <TestRunEffectWarning />
        {failing.length ? <div data-testid="workflow-test-run-will-fail"><DsAlert tone="warning" title={failing.length === 1
          ? t("react.workflows.test_run_one_step_will_fail", "One step will fail as it is set now")
          : t("react.workflows.test_run_steps_will_fail", "{count} steps will fail as they are set now", { count: String(failing.length) })}>
          <div className="workflow-run-ahead">
            {failing.map((row) => <p key={row.key}><b>{row.name}</b>: {row.reason}{" "}
              {ctx.openStepFromLaunch ? <Button size="sm" type="button" variant="ghost" onClick={() => {
                ctx.closeWorkflowTestRunLaunch();
                ignoreAsyncResult(Promise.resolve(ctx.openStepFromLaunch?.(row.nodeId)));
              }}>{t("react.workflows.run_bar_open_step", "Open step")}</Button> : null}</p>)}
            <p>{t("react.workflows.test_run_still_runs", "The test still runs and stops at the first of them.")}</p>
          </div>
        </DsAlert></div> : null}
        {keptSteps.length ? <div data-testid="workflow-test-run-kept-outputs"><DsAlert tone="neutral" title={keptSteps.length === 1
          ? t("react.workflows.test_run_one_kept_output", "1 step returns its kept test output")
          : t("react.workflows.test_run_kept_outputs", "{count} steps return their kept test output", { count: String(keptSteps.length) })}>
          <p>{keptSteps.length === 1
            ? t("react.workflows.test_run_one_kept_output_detail", "{step} does not run; its kept value stands in for it.", { step: keptSteps[0].display_name || keptSteps[0].id })
            : t("react.workflows.test_run_kept_outputs_detail", "{steps} do not run; their kept values stand in for them.", { steps: keptSteps.map((step) => step.display_name || step.id).join(", ") })}</p>
        </DsAlert></div> : null}
        {ctx.workflowTestRunBlockedReason ? <div data-testid="workflow-test-run-blocked" role="status">
          <DsAlert title={t("react.workflows.test_run_cannot_start", "The test cannot start yet")} tone="warning">
            <p>{ctx.workflowTestRunBlockedReason}</p>
            {ctx.canvasChangeSet.hasChanges ? <Button type="button" variant="secondary" disabled={busy || applying} onClick={() => ignoreAsyncResult(ctx.canvasChangeSet.apply())}>{t("react.workflows.apply_changes", "Apply changes")}</Button> : null}
          </DsAlert>
        </div> : null}
        {ctx.renderProjectLinkControl()}
        {ctx.workflowTestRunInputRows.length ? <fieldset className="fs-form-section">
          <legend className="fs-field-label">{t("react.workflows.workflow_inputs", "Workflow inputs")}</legend>
          {ctx.workflowTestRunInterfaceDisabled && ctx.workflowTestRunInputRows.some((row) => row.fixed) ? <p role="note" className="fs-field-helper">{t("react.workflows.test_run_inactive_defaults", "Configured defaults and test files are not applied while the workflow interface is disabled.")}</p> : null}
          {ctx.workflowTestRunInputRows.map((row, index) => {
            const controlId = `${id}-input-${index}`;
            const error = ctx.workflowTestRunFieldErrors[`inputs.${row.name}`];
            const hasDescription = Boolean(row.description?.trim());
            const requirement = row.fixed && ctx.workflowTestRunInterfaceDisabled && (row.type !== "file" || row.testFile) ? t("react.workflows.configured_inactive", "Configured, inactive") : row.testFile ? t("react.workflows.test_file", "Test file") : row.fixed && row.type !== "file" ? t("react.workflows.fixed", "Fixed") : row.required ? t("project_edit.fields.required", "Required") : t("react.workflows.optional", "Optional");
            const shared = {
              id: controlId,
              className: "fs-input w-full",
              disabled: busy || applying,
              required: row.required,
              "aria-invalid": Boolean(error),
              "aria-describedby": [hasDescription ? `${controlId}-help` : undefined, error ? `${controlId}-error` : undefined].filter(Boolean).join(" ") || undefined,
              onBlur: () => ctx.touchWorkflowTestRunField(`inputs.${row.name}`),
              value: ctx.workflowTestRunInputs[row.name] || "",
              onChange: (event: ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => {
                const value = event.target.value;
                ctx.updateWorkflowTestRunInput(row.name, value);
              },
            };
            return <div className="fs-field" key={row.name}>
              <label className="fs-field-label" htmlFor={row.fixed ? undefined : controlId}>{row.required ? <RequiredLabel>{row.label}</RequiredLabel> : row.label}<span className="ml-2 text-[var(--fs-text-tertiary)]">{requirement} · {row.type}</span></label>
              {row.fixed ? <p className="text-sm text-[var(--fs-text-secondary)]">{row.defaultPreview || t("react.workflows.configured", "configured")}</p>
                : row.options.length || inputSupportsBoolean(row.type) ? <select {...shared}>
                  <option value="">{t("extensions.repeater.select_placeholder", "Select…")}</option>
                  {(row.options.length ? row.options : ["true", "false"]).map((value) => <option key={value} value={value}>{value}</option>)}
                </select>
                : inputSupportsJson(row.type) || inputSupportsParagraph(row.type) ? <textarea {...shared} rows={4} />
                : <input {...shared} type={row.type === "secret" ? "password" : "text"} inputMode={inputSupportsNumber(row.type) ? "decimal" : undefined} autoComplete={row.type === "secret" ? "off" : undefined} />}
              {hasDescription ? <p id={`${controlId}-help`} className="fs-field-helper">{row.description}</p> : null}
              {error ? <p id={`${controlId}-error`} className="fs-field-helper error" role="alert">{error}</p> : null}
            </div>;
          })}
        </fieldset> : null}
        <div className="fs-field">
          <label htmlFor={`${id}-summary`}><RequiredLabel>{t("react.workflows.test_run_summary", "Test Run Summary")}</RequiredLabel></label>
          <textarea id={`${id}-summary`} className="fs-input w-full" rows={3} required aria-required="true" aria-label={t("react.workflows.test_run_summary", "Test Run Summary")} aria-invalid={Boolean(ctx.workflowTestRunFieldErrors.task_summary)} aria-describedby={ctx.workflowTestRunFieldErrors.task_summary ? `${id}-summary-error` : undefined} disabled={busy || applying} value={ctx.workflowTestRunSummary} onBlur={() => ctx.touchWorkflowTestRunField("task_summary")} onChange={(event) => ctx.updateWorkflowTestRunSummary(event.target.value)} />
          {ctx.workflowTestRunFieldErrors.task_summary ? <p id={`${id}-summary-error`} className="fs-field-helper error" role="alert">{ctx.workflowTestRunFieldErrors.task_summary}</p> : null}
        </div>
        {normalRun ? <fieldset className="fs-form-section">
          <legend className="fs-field-label">{t("react.workflows.normal_run", "Run workflow")}</legend>
          {normalRun.fileFields.map((field) => <div className="fs-field" key={String(field.name)}>
            <label className="fs-field-label" htmlFor={`${id}-actual-${String(field.name)}`}>
              {t("react.workflows.normal_run_actual_file", "Actual file for {name}", { name: String(field.label || field.name) })}
            </label>
            <input key={`${normalRun.fileOwner}-${normalRun.jobId}-${String(field.name)}`} id={`${id}-actual-${String(field.name)}`} type="file" disabled={busy || applying} required={Boolean(field.required)}
              onChange={(event) => normalRun!.setFile(String(field.name), event.target.files?.[0])} />
          </div>)}
          <label className="fs-field-label">
            <input type="checkbox" checked={normalRun.acknowledged} disabled={busy || applying}
              onChange={(event) => normalRun!.setAcknowledged(event.target.checked)} />
            {t("react.workflows.normal_run_acknowledgement", "I understand this workflow run can create external changes and use live credentials.")}
          </label>
          <p className="fs-field-helper">{t("react.workflows.normal_run_help", "Uses the saved workflow and actual files. Test files and kept test outputs are not used.")}</p>
          {normalRun.message ? <DsAlert tone="danger" title={normalRun.message} /> : null}
          {normalRun.jobId ? <Link to={`/jobs/${encodeURIComponent(normalRun.jobId)}`}>{t("react.workflows.normal_run_job", "Open workflow Job Details")}</Link> : null}
        </fieldset> : null}
        {ctx.workflowDrawerNotice ? <DsAlert title={ctx.workflowDrawerNoticeTitle} tone="warning"><p>{ctx.workflowDrawerNotice}</p></DsAlert> : null}
        {ctx.canvasChangeSet.applyError ? <DsAlert title={ctx.canvasChangeSet.applyError} tone="danger" /> : null}
        {currentStartError && ctx.workflowTestRunMessage ? <DsAlert title={ctx.workflowTestRunMessage} tone="danger" /> : null}
      </div>
    </DsDialog>
  );
}
