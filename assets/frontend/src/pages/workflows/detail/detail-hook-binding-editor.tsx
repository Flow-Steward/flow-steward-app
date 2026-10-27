import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { DsAlert, DsBadge, DsValueChip, RequiredLabel } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { contractRows } from "@/pages/workflows/detail/detail-step-editing";
import {
  parseTestRunInputValue,
  renderKeyValueSummary,
  testRunStatus,
  testRunValueGiven,
  waitForWorkflowTestRunState,
  workflowTestRunIsPending,
  workflowTestRunOutputs,
} from "@/pages/workflows/detail/detail-test-run";
import {
  fetchWorkflowHook,
  hookErrorIssues,
  previewWorkflowHookBinding,
  type WorkflowHook,
  type WorkflowHookIssue,
} from "@/pages/workflows/shared/hook-api";
import { testRunWorkflow } from "@/pages/workflows/shared/simulate-api";
import { TestRunEffectWarning } from "@/pages/workflows/detail/detail-test-run-effect-warning";

type HookBindingEditorProps = {
  projectId: string;
  workflowId: string;
  hookId: string;
  canAuthor: boolean;
  /** The steps the hook holds on the canvas (its hidden end excluded). */
  stepCount?: number;
  /** Removes every step of the hook (after a confirmation); Undo or Discard brings them back. */
  onClear?: () => void | Promise<void>;
  /** The project picker the "Choose a project" notice offers (R2-10). */
  projectPicker?: ReactNode;
};

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** The hook fields its steps receive: the properties of the hook's input schema. */
function hookInputFields(hook: WorkflowHook | undefined): string[] {
  const properties = hook?.contract.input_schema?.properties;
  return properties && typeof properties === "object" ? Object.keys(properties) : [];
}

/** The hook inputs and the mapped child inputs of a test, from the values typed in the drawer. */
function hookTestInputs(
  rows: Array<Record<string, unknown>>,
  values: Record<string, string>,
  inputMapping: Record<string, string>,
) {
  const inputs: Record<string, unknown> = {};
  for (const row of rows) {
    const name = text(row.name);
    const raw = values[name] ?? "";
    if (name && testRunValueGiven(row.type, raw)) inputs[name] = parseTestRunInputValue(row.type, raw);
  }
  const childInputs = Object.fromEntries(
    Object.entries(inputMapping)
      .filter(([, hookField]) => hookField in inputs)
      .map(([childInput, hookField]) => [childInput, inputs[hookField]]),
  );
  return { inputs, childInputs };
}

type HookTestResult = {
  status: string;
  jobId: string;
  childOutputs: Record<string, unknown>;
  output?: Record<string, unknown>;
  issues: WorkflowHookIssue[];
};

function renderFieldChips(label: string, names: string[]) {
  if (!names.length) return null;
  return (
    <p className="flex flex-wrap items-center gap-1 text-xs fs-ink-secondary">
      <span>{label}</span>
      {names.map((name) => <DsValueChip key={name}>{name}</DsValueChip>)}
    </p>
  );
}

/**
 * A hook in the step drawer (R2-11): a place on the canvas for steps. It says
 * in plain words what the hook receives, what its steps may change and what
 * happens when it is empty; its steps are edited on the canvas inside the hook
 * frame. No workflow is chosen or named here: to call a workflow of your own,
 * add an Invoke workflow step inside the hook. "Clear the hook" removes its steps.
 */
export function HookBindingEditor({ projectId, workflowId, hookId, canAuthor, stepCount = 0, onClear, projectPicker }: Readonly<HookBindingEditorProps>) {
  const { t } = useI18n();
  const hookQuery = useQuery({
    queryKey: ["workflows", "hook", projectId, workflowId, hookId],
    queryFn: () => fetchWorkflowHook(projectId, workflowId, hookId),
    enabled: Boolean(projectId && workflowId && hookId),
    retry: false,
  });
  const hook = hookQuery.data;
  const [clearing, setClearing] = useState(false);

  if (!projectId) {
    return (
      <DsAlert title={t("react.workflows.hook_needs_project", "Choose a project")}>
        <p>{t("react.workflows.hook_needs_project_body", "Each project keeps its own steps in a hook. Choose the project to add or change them.")}</p>
        {projectPicker ? <div className="mt-2">{projectPicker}</div> : null}
      </DsAlert>
    );
  }
  if (hookQuery.isLoading) {
    return <p className="text-xs text-[var(--fs-text-tertiary)]">{t("react.workflows.loading_hook", "Loading the hook…")}</p>;
  }
  if (hookQuery.isError || !hook) {
    const issues = hookErrorIssues(hookQuery.error);
    return (
      <DsAlert tone="danger" title={t("react.workflows.hook_load_failed", "The hook could not be loaded")}>
        {issues.length ? issues.map((issue) => issue.message || issue.code).join(" ") : hookQuery.error instanceof Error ? hookQuery.error.message : ""}
      </DsAlert>
    );
  }
  const stage = hook.contract.stage_contract || {};
  const added = Object.keys(hook.contract.additional_outputs || {});
  async function clear() {
    if (!onClear || clearing) return;
    setClearing(true);
    try {
      await onClear();
    } finally {
      setClearing(false);
    }
  }

  return (
    <div className="space-y-3 fs-surface-radius border border-subtle fs-surface-inset p-3" data-testid="hook-binding-editor">
      <div>
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-primary">
          {t("react.workflows.hook", "Hook")}</p>
        <p className="mt-1 text-sm fs-ink-primary">{hook.display_name}</p>
        {hook.description ? <p className="mt-1 text-xs fs-ink-secondary">{hook.description}</p> : null}
        {stage.summary ? <p className="mt-1 text-xs fs-ink-secondary">{stage.summary}</p> : null}
        {stage.effects_after?.length ? (
          <p className="mt-1 flex flex-wrap items-center gap-1 text-[11px] fs-ink-tertiary">
            {t("react.workflows.hook_effects_after", "Not done yet at this point:")}
            {stage.effects_after.map((effect) => <DsValueChip key={effect}>{effect}</DsValueChip>)}
          </p>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs" data-testid="hook-state">
        {stepCount ? (
          <DsBadge tone="green">{stepCount === 1
            ? t("react.workflows.hook_steps_one", "1 step")
            : t("react.workflows.hook_steps_count", "{count} steps", { count: String(stepCount) })}</DsBadge>
        ) : (
          <>
            <DsBadge>{t("react.workflows.hook_empty", "Empty")}</DsBadge>
            <span className="fs-ink-secondary">{t("react.workflows.hook_empty_hint", "Nothing runs here yet: the run goes on with the inputs unchanged.")}</span>
          </>
        )}
      </div>
      <div className="space-y-1">
        {renderFieldChips(t("react.workflows.hook_receives", "It receives:"), hookInputFields(hook))}
        {renderFieldChips(t("react.workflows.hook_may_change", "Its steps may change:"), hook.contract.mutable_fields || [])}
        {renderFieldChips(t("react.workflows.hook_may_add", "Its steps may add:"), added)}
      </div>
      <p className="text-xs fs-ink-secondary">
        {t("react.workflows.hook_steps_on_canvas", "Its steps are on the canvas, inside the hook's frame: add one with Add step there. To run a workflow of your own here, add an Invoke workflow step.")}
      </p>
      <p className="text-xs fs-ink-secondary">
        {t("react.workflows.hook_results_fallback", "At the end of the hook each result is read by its name. A result no step sets keeps its input value (an added one its default), so an empty hook changes nothing.")}
      </p>
      {canAuthor && stepCount && onClear ? (
        <div>
          <button className="fs-button sm secondary" disabled={clearing} onClick={() => ignoreAsyncResult(clear())} type="button">
            {t("react.workflows.hook_clear", "Clear the hook")}</button>
        </div>
      ) : null}
    </div>
  );
}

type HookTestRunPanelProps = { projectId: string; workflowId: string; hookId: string; blockedReason?: string; onTestRunStarted?: (jobId: string, hookId: string) => void };

/**
 * The hook's TEST RUN tab (B14): its steps run on
 * typed hook inputs, with the values they set and
 * what the hook would write back. The workflow that declares the hook never runs.
 */
export function HookTestRunPanel(props: Readonly<HookTestRunPanelProps>) {
  return <HookTestRunPanelContent key={`${props.projectId}:${props.workflowId}:${props.hookId}`} {...props} />;
}

function HookTestRunPanelContent({ projectId, workflowId, hookId, blockedReason, onTestRunStarted }: Readonly<HookTestRunPanelProps>) {
  const { t } = useI18n();
  const hookQuery = useQuery({
    queryKey: ["workflows", "hook", projectId, workflowId, hookId],
    queryFn: () => fetchWorkflowHook(projectId, workflowId, hookId),
    enabled: Boolean(projectId && workflowId && hookId),
    retry: false,
  });
  const hook = hookQuery.data;
  const binding = hook?.binding || null;
  const [testValues, setTestValues] = useState<Record<string, string>>({});
  const [testResult, setTestResult] = useState<HookTestResult | null>(null);
  const [issues, setIssues] = useState<WorkflowHookIssue[]>([]);
  const [busy, setBusy] = useState(false);

  const runGeneration = useRef(0);
  useEffect(() => () => { runGeneration.current += 1; }, [projectId, workflowId, hookId]);

  async function runTest() {
    if (!hook || !binding || blockedReason) return;
    const generation = ++runGeneration.current;
    setBusy(true);
    setIssues([]);
    setTestResult(null);
    try {
      const { inputs, childInputs } = hookTestInputs(contractRows(hook.contract.input_schema), testValues, binding.input_mapping || {});
      let run = await testRunWorkflow({
        workflowId: binding.target_workflow_id,
        projectId,
        taskSummary: t("react.workflows.hook_test_summary", "Test of the hook {hook}", { hook: hook.display_name }),
        inputs: childInputs,
      });
      if (runGeneration.current !== generation) return;
      const initialJobId = text(run.job_id);
      if (initialJobId) onTestRunStarted?.(initialJobId, hookId);
      if (run.thread_id && workflowTestRunIsPending(run)) run = await waitForWorkflowTestRunState(run.thread_id);
      if (runGeneration.current !== generation) return;
      if (text(run.job_id) && text(run.job_id) !== initialJobId) onTestRunStarted?.(text(run.job_id), hookId);
      const status = testRunStatus(run.final_status || run.status);
      const childOutputs = workflowTestRunOutputs(run);
      const result: HookTestResult = { status, jobId: text(run.job_id), childOutputs, issues: [] };
      if (status === "pass") {
        const preview = await previewWorkflowHookBinding(projectId, workflowId, hookId, {
          expected_revision: hook.binding_revision,
          target_workflow_id: binding.target_workflow_id,
          input_mapping: binding.input_mapping || {},
          output_mapping: binding.output_mapping || {},
          hook_contract_version: hook.contract_version,
          hook_semantic_digest: hook.hook_semantic_digest,
        }, { inputs, child_outputs: childOutputs });
        result.output = preview.sample_output;
        result.issues = preview.issues || [];
      }
      if (runGeneration.current === generation) setTestResult(result);
    } catch (error) {
      if (runGeneration.current !== generation) return;
      const found = hookErrorIssues(error);
      setIssues(found.length ? found : [{ message: error instanceof Error ? error.message : String(error) }]);
    } finally {
      if (runGeneration.current === generation) setBusy(false);
    }
  }

  if (hookQuery.isLoading) {
    return <p className="text-xs text-[var(--fs-text-tertiary)]">{t("react.workflows.loading_hook", "Loading the hook…")}</p>;
  }
  if (!hook) return null;
  if (!binding) {
    return (
      <div className="mt-2 space-y-2" data-testid="hook-test">
        <TestRunEffectWarning />
        <p
          className="rounded-lg border border-amber-300/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-50"
          data-testid="step-test-run-blocked"
          role="status"
        >
          {t("react.workflows.test_run_blocked_empty_hook", "This hook is empty. Add a step to it before testing it.")}
        </p>
      </div>
    );
  }
  const targetId = binding.target_workflow_id;
  return (
    <div className="mt-2 space-y-2">
      <TestRunEffectWarning />
          {targetId ? (
            <div className="space-y-2 border-t border-subtle pt-3" data-testid="hook-test">
              <p className="text-[11px] font-medium uppercase tracking-[0.12em] fs-ink-tertiary">
                {t("react.workflows.hook_test", "Test the hook")}</p>
              <p className="text-xs fs-ink-secondary">
                {t(
                  "react.workflows.hook_test_hint",
                  "Runs a Test Run of the hook's steps on these inputs and shows what the hook would publish. The workflow that declares the hook does not run.",
                )}</p>
              {contractRows(hook.contract.input_schema).map((row) => {
                const name = text(row.name);
                return (
                  <label className="grid grid-cols-[1fr_1fr] items-center gap-2 text-xs fs-ink-secondary" key={name}>
                    <span className="font-mono">{row.required ? <RequiredLabel>{name}</RequiredLabel> : name}</span>
                    <input
                      aria-label={t("react.workflows.hook_test_value", "Test value for {name}", { name })}
                      aria-required={Boolean(row.required) || undefined}
                      required={Boolean(row.required)}
                      className="fs-input"
                      onChange={(event) => setTestValues((current) => ({ ...current, [name]: event.target.value }))}
                      value={testValues[name] ?? ""}
                    />
                  </label>
                );
              })}
              {blockedReason ? (
                <p className="rounded-lg border border-amber-300/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-50" data-testid="step-test-run-blocked" role="status">
                  {blockedReason}
                </p>
              ) : null}
              <button aria-label={t("react.workflows.run_hook_test_run", "Run hook test run")} className="fs-button sm" disabled={busy || Boolean(blockedReason)} onClick={() => ignoreAsyncResult(runTest())} type="button">
                {t("registry.workflow_detail.run_lab.tab.test_run", "Test Run")}</button>
              {testResult ? (
                <div className="space-y-2 text-xs fs-ink-secondary" data-testid="hook-test-result">
                  <p className="flex flex-wrap items-center gap-2">
                    <DsBadge tone={testResult.status === "pass" ? "green" : "danger"}>
                      {testResult.status === "pass"
                        ? t("react.workflows.hook_test_passed", "Test Run completed")
                        : t("react.workflows.hook_test_failed", "Test Run did not complete: {status}", { status: testResult.status })}
                    </DsBadge>
                    {testResult.jobId ? (
                      <Link className="fs-link" to={`/jobs/${encodeURIComponent(testResult.jobId)}`}>
                        {t("react.workflows.hook_test_open_run", "Open the Test Run")}</Link>
                    ) : null}
                  </p>
                  <div>
                    <p className="fs-ink-tertiary">{t("react.workflows.hook_test_child_result", "Values its steps set")}</p>
                    {renderKeyValueSummary(testResult.childOutputs, t("react.workflows.hook_test_no_result", "No result values."), t)}
                  </div>
                  {testResult.output ? (
                    <div>
                      <p className="fs-ink-tertiary">{t("react.workflows.hook_test_output", "The hook would publish")}</p>
                      {renderKeyValueSummary(testResult.output, t("react.workflows.hook_test_no_result", "No result values."), t)}
                    </div>
                  ) : null}
                  {testResult.issues.length ? (
                    <DsAlert tone="warning" title={t("react.workflows.hook_test_issues", "A run would fail here")}>
                      <ul className="list-disc pl-4">
                        {testResult.issues.map((issue, index) => <li key={`${issue.code || ""}-${index}`}>{issue.message || issue.code}</li>)}
                      </ul>
                    </DsAlert>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}
      {issues.length ? (
        <DsAlert tone="warning" title={t("react.workflows.hook_test_issues", "A run would fail here")}>
          <ul className="list-disc pl-4">
            {issues.map((issue, index) => <li key={`${issue.code || ""}-${index}`}>{issue.message || issue.code}</li>)}
          </ul>
        </DsAlert>
      ) : null}
    </div>
  );
}
