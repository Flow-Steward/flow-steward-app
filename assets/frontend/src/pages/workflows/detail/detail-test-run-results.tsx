import { Fragment, useEffect, useRef, useState } from "react";

import { DsAlert, DsBadge, type DsBadgeTone } from "@/components/shared/design-system-primitives";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { stepKindKeepsOutput } from "@/pages/workflows/shared/kept-test-output-api";
import type { WorkflowTestRunEvidence } from "./detail-test-run-evidence";
import { RunPassValues, RunValue } from "./detail-test-run-io";
import { Link } from "react-router";
import { ApprovalDecision } from "./detail-step-test-run-latest";
import type { ReviewCommentDraft } from "@/pages/jobs/detail/job-detail-review-panels";
import { formatRunDuration, keptOutputsFromPass, type RunPass } from "./detail-test-run-results-model";
import "@/pages/workflows/builder/canvas-test-run.css";

const BADGE_TONES: Record<WorkflowTestRunEvidence["statusTone"], DsBadgeTone> = {
  success: "green", danger: "danger", attention: "orange", running: "orange", neutral: "neutral",
};

function PassTime({ pass }: Readonly<{ pass: RunPass }>) {
  const { t } = useI18n();
  let label = formatRunDuration(pass.durationMs, t);
  if (pass.state === "failed") label = t("react.workflows.test_run_failed", "Failed");
  else if (pass.state === "running") label = t("react.workflows.run_bar_running", "Running");
  else if (pass.state === "waiting") label = t("react.workflows.test_run_waiting", "Waiting");
  else if (pass.state === "skipped") label = t("react.workflows.test_run_skipped", "Skipped");
  else if (pass.state === "kept") label = t("react.workflows.run_mark_kept", "Kept");
  else if (pass.decision) label = pass.decision;
  return <span className="workflow-run-pass-time" data-run={pass.state}>{label}</span>;
}

function PassRow({ pass, open, evidence, onToggle }: Readonly<{ pass: RunPass; open: boolean; evidence: WorkflowTestRunEvidence; onToggle: () => void }>) {
  const { t } = useI18n();
  const [keepNotice, setKeepNotice] = useState<{ tone: "danger" | "neutral"; text: string } | null>(null);
  const finished = pass.state === "done" || pass.state === "kept";
  const keepReason = !stepKindKeepsOutput(pass.stepKind) ? t("react.workflows.kept_output_always_runs", "This kind of step always runs in tests.")
    : !finished ? t("react.workflows.kept_output_needs_finished_pass", "Only a pass that finished can be kept.")
      : !Object.keys(pass.outputs).length ? t("react.workflows.kept_output_no_outputs", "This pass has no outputs to keep.")
        : pass.outputsCut ? t("react.workflows.kept_output_cut_short", "The run kept only the start of this output. Write it yourself in the step's Test Run tab.")
          : "";
  const meta = [pass.kindLabel, pass.place].filter(Boolean).join(" · ");
  const dataId = `workflow-run-pass-${pass.key}`;
  return <li className="workflow-run-pass" data-open={open || undefined} data-testid="workflow-run-pass" data-step-id={pass.stepId}>
    <button aria-controls={open ? dataId : undefined} aria-expanded={open} className="workflow-run-pass-row" onClick={onToggle} type="button">
      <span className="workflow-run-pass-icon" data-run={pass.state} aria-hidden="true"><span className="workflow-run-pass-dot" /></span>
      <span className="workflow-run-pass-text">
        <span className="workflow-run-pass-title">{pass.label}</span>
        {meta ? <span className="workflow-run-pass-meta">{meta}</span> : null}
        {pass.state === "failed" && pass.error ? <span className="workflow-run-pass-reason">{pass.error}</span> : null}
      </span>
      <PassTime pass={pass} />
    </button>
    {open ? <div className="workflow-run-pass-data" id={dataId}>
      {pass.message && pass.state !== "failed" ? <p className="workflow-run-pass-meta">{pass.message}</p> : null}
      {pass.state === "running" ? <p className="workflow-run-pass-meta">{t("react.workflows.test_run_pass_running", "Running — its output appears when it finishes.")}</p>
        : <RunPassValues error={pass.state === "failed" ? pass.error : ""} inputs={pass.inputs} outputs={pass.outputs} />}
      <div className="workflow-run-pass-actions">
        <Button onClick={() => evidence.openStep(pass.stepId)} size="sm" type="button" variant="secondary">{t("react.workflows.run_bar_open_step", "Open step")}</Button>
        <Button disabled={Boolean(keepReason) || evidence.keptPending} onClick={() => ignoreAsyncResult((async () => {
          setKeepNotice(null);
          const error = await evidence.keepOutput(pass.stepId, keptOutputsFromPass(pass, []), { kind: "run", jobId: evidence.jobId });
          setKeepNotice(error ? { tone: "danger", text: error } : { tone: "neutral", text: t("react.workflows.kept_output_kept", "Kept as test output. Test runs return it instead of running {step}.", { step: pass.label }) });
        })())} size="sm" title={keepReason || undefined} type="button" variant="secondary">{t("react.workflows.kept_output_keep", "Keep as test output")}</Button>
      </div>
      {keepReason && finished ? <p className="workflow-run-pass-meta">{keepReason}</p> : null}
      {keepNotice ? <p className={keepNotice.tone === "danger" ? "workflow-kept-error" : "workflow-run-pass-meta"} role="status">{keepNotice.text}</p> : null}
    </div> : null}
  </li>;
}

/**
 * Test Run results: the kept test runs, the selected run's inputs and counts,
 * and one row per pass with where it ran. A row opens in place to what the
 * step read and wrote, and lights the step on the canvas.
 */
export function WorkflowTestRunResults({ evidence, inspect, commentDraft }: Readonly<{
  evidence: WorkflowTestRunEvidence;
  commentDraft?: ReviewCommentDraft;
  /** Open exactly this pass in place (Open step on an archived failure): its execution, never a same-step neighbour. */
  inspect?: Readonly<{ serial: number; jobId: string; stepExecutionId: string }> | null;
}>) {
  const { t } = useI18n();
  const [openKey, setOpenKey] = useState("");
  const [handledInspect, setHandledInspect] = useState(0);
  const litInspect = useRef(0);
  // Opening Results reads the run again, as the full Job Details did on open.
  const refreshOnOpen = useRef(evidence.refresh);
  useEffect(() => { refreshOnOpen.current(); }, [evidence.jobId]);
  const inspected = inspect?.jobId === evidence.jobId ? evidence.passes.find((pass) => pass.stepExecutionId && pass.stepExecutionId === inspect.stepExecutionId) : undefined;
  if (inspect && inspected && handledInspect !== inspect.serial) {
    setHandledInspect(inspect.serial);
    setOpenKey(inspected.key);
  }
  useEffect(() => {
    if (!inspect || !inspected || litInspect.current === inspect.serial) return;
    litInspect.current = inspect.serial;
    evidence.lightStep(inspected);
  }, [evidence, inspect, inspected]);
  const loader = useLoaderDelay(evidence.loading);
  const heading = evidence.isLatest
    ? t("react.workflows.test_run_latest", "Latest test run")
    : t("react.workflows.test_run_numbered", "Test run #{number}", { number: String(evidence.runNumber ?? "") });
  const toggle = (pass: RunPass) => {
    const next = openKey === pass.key ? "" : pass.key;
    setOpenKey(next);
    evidence.lightStep(next ? pass : null);
  };
  return <div className="workflow-run-results" data-testid="workflow-test-run-results" data-job-id={evidence.jobId}>
    {evidence.runs.length ? <section className="workflow-run-results-section" aria-labelledby="workflow-test-runs-heading">
      <h3 className="workflow-run-results-heading" id="workflow-test-runs-heading">{t("react.workflows.test_runs", "Test runs")}</h3>
      <ul className="workflow-run-runs">
        {evidence.runs.map((run) => <li key={run.jobId}>
          <button aria-current={run.jobId === evidence.jobId || undefined} aria-label={run.label} className="workflow-run-runs-row" data-testid={`workflow-test-run-row:${run.jobId}`}
            disabled={evidence.runsDisabled} onClick={() => evidence.selectRun(run.jobId)} type="button">
            <span className="workflow-run-runs-name">{t("react.workflows.test_run_number", "Run #{number}", { number: String(run.number) })}</span>
            <DsBadge tone={BADGE_TONES[run.tone]}>{run.status}</DsBadge>
            <span className="workflow-run-runs-meta">{run.meta}</span>
          </button>
        </li>)}
      </ul>
    </section> : null}
    {evidence.approval ? <ApprovalDecision approval={evidence.approval} commentDraft={commentDraft} focusRequest={false} /> : null}
    <section className="workflow-run-results-section" aria-labelledby="workflow-test-run-heading">
      <h3 className="workflow-run-results-heading" id="workflow-test-run-heading">
        {heading}{evidence.statusWord ? <DsBadge tone={BADGE_TONES[evidence.statusTone]}>{evidence.statusWord}</DsBadge> : null}
      </h3>
      {evidence.executedRevision ? <p className="workflow-run-pass-meta" data-testid="workflow-test-run-revision">
        {t("react.workflows.test_run_executed_revision", "Executed definition: revision {revision}", { revision: String(evidence.executedRevision) })}
        {evidence.executedDigest ? ` · ${evidence.executedDigest.slice(0, 12)}` : ""}</p> : null}
      {evidence.inputs.length ? <p className="workflow-run-inputs">{t("react.workflows.test_run_inputs", "Inputs:")}{" "}
        {evidence.inputs.map((input, index) => <Fragment key={input.name}>{index ? " · " : ""}{input.name} = {input.hidden ? t("react.workflows.test_run_value_hidden", "hidden") : input.value}</Fragment>)}
      </p> : null}
      {evidence.loading && !evidence.passes.length ? null : <p className="workflow-run-facts">
        <span><b>{evidence.counts.done}</b> {t("react.workflows.test_run_done_count", "done")}</span>
        <span><b>{evidence.counts.skipped}</b> {t("react.workflows.test_run_skipped_count", "skipped")}</span>
        <span><b>{evidence.counts.failed}</b> {t("react.workflows.test_run_failed_count", "failed")}</span>
        {evidence.duration ? <span><b>{evidence.duration}</b></span> : null}
      </p>}
      {Object.keys(evidence.runOutputs).length || Object.keys(evidence.runArtifacts).length ? <div className="workflow-run-results-section" data-testid="workflow-test-run-outputs">
        <h4 className="workflow-run-results-heading">{t("react.workflows.test_run_outputs", "Run outputs")}</h4>
        {Object.entries(evidence.runOutputs).map(([name, value]) => <RunValue key={`output:${name}`} name={name} value={value} />)}
        {Object.keys(evidence.runArtifacts).length ? <h4 className="workflow-run-results-heading">{t("react.workflows.test_run_artifacts", "Run artifacts")}</h4> : null}
        {Object.entries(evidence.runArtifacts).map(([name, value]) => <RunValue key={`artifact:${name}`} name={name} value={value} />)}
      </div> : null}
      {evidence.error ? <DsAlert title={evidence.error} tone="danger" /> : null}
      {loader && !evidence.passes.length ? <DsLoader variant="orbit-slot" label={t("react.workflows.run_bar_loading", "Loading Test Run…")} /> : null}
      {evidence.passes.length ? <ol className="workflow-run-timeline" aria-label={t("react.workflows.test_run_passes", "Steps in this run")}>
        {evidence.passes.map((pass) => <Fragment key={pass.key}>
          {pass.itemHeader ? <li className="workflow-run-pass-header" aria-hidden="true">{pass.itemHeader}</li> : null}
          <PassRow evidence={evidence} onToggle={() => toggle(pass)} open={openKey === pass.key} pass={pass} />
        </Fragment>)}
      </ol> : !evidence.loading ? <p className="workflow-run-pass-meta">{t("react.workflows.test_run_no_passes", "No step has run yet.")}</p> : null}
    </section>
    <div className="workflow-run-results-footer">
      <Link className="fs-button ghost sm mr-auto" to={`/jobs/${encodeURIComponent(evidence.jobId)}`}>{t("react.jobs.open_full_job_details", "Open full Job Details")}</Link>
      <Button onClick={evidence.changeInputs} size="sm" type="button" variant="secondary">{t("react.workflows.test_run_change_inputs", "Change inputs")}</Button>
      {evidence.runAgain ? <Button disabled={evidence.runAgain.disabled || evidence.runAgain.busy} onClick={() => ignoreAsyncResult(Promise.resolve(evidence.runAgain?.onInvoke()))}
        size="sm" title={evidence.runAgain.disabledReason || undefined} type="button" variant="primary">{t("react.workflows.run_bar_run_again", "Run again")}</Button> : null}
    </div>
  </div>;
}
