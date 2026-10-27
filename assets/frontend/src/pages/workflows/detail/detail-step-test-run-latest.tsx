import { useEffect, useId, useRef, useState } from "react";

import { DsBadge } from "@/components/shared/design-system-primitives";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { stepKindKeepsOutput } from "@/pages/workflows/shared/kept-test-output-api";
import type { ReviewCommentDraft } from "@/pages/jobs/detail/job-detail-review-panels";
import type { TestRunApproval, WorkflowTestRunEvidence } from "./detail-test-run-evidence";
import { RunPassValues, RunValue } from "./detail-test-run-io";
import { formatRunDuration, keptOutputsFromPass, type RunPass } from "./detail-test-run-results-model";
import "@/pages/workflows/builder/canvas-test-run.css";

export type StepTestRunStep = { stepId: string; label: string; kind: string; local: boolean; declaredOutputs: readonly string[] };

/** A waiting Approval decided with its request in view; focus goes to the request, not a button. */
export function ApprovalDecision({ approval, commentDraft, focusRequest = true }: Readonly<{
  approval: TestRunApproval;
  /** The page's draft owner keeps a typed comment while panels switch, for this gate only. */
  commentDraft?: ReviewCommentDraft;
  focusRequest?: boolean;
}>) {
  const { t } = useI18n();
  const requestRef = useRef<HTMLParagraphElement>(null);
  const titleId = useId();
  const commentId = useId();
  const busy = useLoaderDelay(approval.pending);
  const [localComment, setLocalComment] = useState({ identity: approval.identity, value: "" });
  if (localComment.identity !== approval.identity) setLocalComment({ identity: approval.identity, value: "" });
  const comment = commentDraft ? commentDraft.identity === approval.identity ? commentDraft.value : "" : localComment.value;
  const setComment = (value: string) => {
    if (commentDraft) commentDraft.onChange(approval.identity, value);
    else setLocalComment({ identity: approval.identity, value });
  };
  useEffect(() => { if (focusRequest) requestRef.current?.focus(); }, [approval.identity, focusRequest]);
  return <section aria-labelledby={titleId} className="workflow-approval-decision" data-testid="workflow-approval-decision">
    <p className="workflow-approval-decision-title" id={titleId}>{t("react.workflows.approval_waiting_for_decision", "Waiting for your decision")}</p>
    <p className="workflow-approval-request" ref={requestRef} tabIndex={-1}>{approval.message
      || t("react.workflows.approval_asks_nothing", "This step asks no question. Write one under Settings › Message.")}</p>
    <div className="workflow-run-results-section">
      <p className="workflow-step-run-sub">{t("react.workflows.approval_shown_from_run", "Shown with the question · from this run")}</p>
      {approval.shown.length ? approval.shown.map((row) => <RunValue key={row.name} name={row.name} value={row.value} />)
        : <p className="workflow-step-run-sub">{t("react.workflows.approval_shows_nothing", "This step shows nothing with its question.")}</p>}
    </div>
    <div className="workflow-run-results-section">
      <p className="workflow-step-run-sub">{t("react.workflows.approval_what_happens_next", "What happens next")}</p>
      <ul className="workflow-approval-next">
        <li><b>{t("react.workflows.test_run_approved", "Approved")}</b> → {approval.approvedTo || t("react.workflows.approval_ends_run", "the run ends")}</li>
        <li><b>{t("react.workflows.test_run_rejected", "Rejected")}</b> → {approval.rejectedTo || t("react.workflows.approval_ends_run", "the run ends")}</li>
      </ul>
    </div>
    <div className="fs-field">
      <label className="fs-field-label" htmlFor={commentId}>{t("jobs.detail.review.comment_for_rejection", "Comment for rejection")}</label>
      <textarea className="fs-textarea" id={commentId} aria-describedby={`${commentId}-help`} onChange={(event) => setComment(event.target.value)} rows={2} value={comment} />
      <p className="fs-field-helper" id={`${commentId}-help`}>{t("react.workflows.approval_comment_help", "Optional. It is sent with your decision.")}</p>
    </div>
    {approval.error ? <p className="workflow-kept-error" role="alert">{approval.error}</p> : null}
    <div className="workflow-approval-actions">
      <Button disabled={approval.pending} onClick={() => approval.decide(false, comment)} type="button" variant="secondary">{t("react.workflows.approval_reject", "Reject")}</Button>
      <Button aria-busy={approval.pending || undefined} disabled={approval.pending} onClick={() => approval.decide(true, comment)} type="button" variant="primary">
        {busy ? <DsLoader variant="orbit-inline" /> : null}{t("react.workflows.approval_approve", "Approve")}</Button>
    </div>
  </section>;
}

function passStateLine(pass: RunPass, t: ReturnType<typeof useI18n>["t"]): string {
  const time = formatRunDuration(pass.durationMs, t);
  if (pass.state === "failed") return t("react.workflows.step_run_failed_reason", "Failed: {reason}", { reason: pass.error || t("react.workflows.test_run_failed", "Failed") });
  if (pass.state === "running") return t("react.workflows.run_bar_running", "Running");
  if (pass.state === "waiting") return t("react.workflows.test_run_waiting", "Waiting");
  if (pass.state === "skipped") return t("react.workflows.test_run_skipped", "Skipped");
  if (pass.state === "kept") return t("react.workflows.step_run_kept", "Returned its kept test output");
  return time ? t("react.workflows.step_run_done_in", "Done in {duration}", { duration: time }) : t("react.workflows.container_done", "Done");
}

function emptyKeptDraft(targets: readonly string[]): string {
  return JSON.stringify(Object.fromEntries((targets.length ? targets : ["state.output"]).map((target) => [target, null])), null, 2);
}

/** "Test output": a kept output stands in for the step in test runs; real runs are not affected. */
function KeptTestOutputSection({ evidence, step, latest }: Readonly<{ evidence: WorkflowTestRunEvidence; step: StepTestRunStep; latest?: RunPass }>) {
  const { t } = useI18n();
  const id = useId();
  const kept = evidence.kept.get(step.stepId);
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState("");
  const busy = useLoaderDelay(evidence.keptPending);
  if (!stepKindKeepsOutput(step.kind)) {
    return <section className="workflow-step-run" aria-labelledby={`${id}-title`}>
      <h3 className="workflow-step-run-title" id={`${id}-title`}>{t("react.workflows.kept_output_title", "Test output")}</h3>
      <p className="workflow-step-run-sub">{t("react.workflows.kept_output_always_runs", "This kind of step always runs in tests.")}</p>
    </section>;
  }
  const save = () => ignoreAsyncResult((async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(draft || "");
    } catch {
      setError(t("react.workflows.kept_output_json_invalid", "This is not valid JSON."));
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || !Object.keys(parsed).length) {
      setError(t("react.workflows.kept_output_json_object", "Write an object with one value per output, for example {\"state.result\": \"value\"}."));
      return;
    }
    const failure = await evidence.keepOutput(step.stepId, parsed as Record<string, unknown>, { kind: "manual" });
    setError(failure);
    if (!failure) setDraft(null);
  })());
  const latestKeepable = latest && (latest.state === "done" || latest.state === "kept") && Object.keys(latest.outputs).length && !latest.outputsCut ? latest : undefined;
  const sourceRun = kept?.source_kind === "run" ? evidence.runs.find((run) => run.jobId === kept.source_job_id) : undefined;
  return <section className="workflow-step-run" aria-labelledby={`${id}-title`} data-testid="workflow-kept-test-output">
    <h3 className="workflow-step-run-title" id={`${id}-title`}>{t("react.workflows.kept_output_title", "Test output")}
      {kept ? <DsBadge tone="neutral">{kept.source_kind === "manual" ? t("react.workflows.kept_output_edited", "Edited")
        : sourceRun ? t("react.workflows.kept_output_from_run", "From Run #{number}", { number: String(sourceRun.number) }) : t("react.workflows.kept_output_from_a_run", "From a run")}</DsBadge> : null}
    </h3>
    {kept && draft === null ? <>
      <p className="workflow-step-run-sub">{t("react.workflows.kept_output_in_use", "In test runs this step does not do its work: it returns these values. Real runs are not affected.")}</p>
      {Object.entries(kept.outputs).map(([name, value]) => <RunValue key={name} name={name} value={value} />)}
      <div className="workflow-kept-actions">
        <Button disabled={evidence.keptPending} onClick={() => { setError(""); setDraft(JSON.stringify(kept.outputs, null, 2)); }} size="sm" type="button" variant="secondary">{t("react.workflows.kept_output_edit", "Edit")}</Button>
        <Button aria-busy={evidence.keptPending || undefined} disabled={evidence.keptPending} onClick={() => ignoreAsyncResult((async () => setError(await evidence.clearOutput(step.stepId)))())} size="sm" type="button" variant="secondary">
          {busy ? <DsLoader variant="orbit-inline" /> : null}{t("react.workflows.kept_output_stop_using", "Stop using it")}</Button>
      </div>
    </> : null}
    {!kept && draft === null ? <>
      <p className="workflow-step-run-sub">{t("react.workflows.kept_output_offer", "In test runs this step does its work. Keep an output to stand in for it: the test then skips the step and goes on with those values. Real runs are not affected.")}</p>
      {step.local ? <p className="workflow-step-run-sub">{t("react.workflows.kept_output_apply_first", "Apply the changes before keeping a test output for this step.")}</p> : <div className="workflow-kept-actions">
        {latestKeepable ? <Button disabled={evidence.keptPending} onClick={() => ignoreAsyncResult((async () => {
          setError(await evidence.keepOutput(step.stepId, keptOutputsFromPass(latestKeepable, step.declaredOutputs), { kind: "run", jobId: evidence.jobId }));
        })())} size="sm" type="button" variant="secondary">{t("react.workflows.kept_output_keep_latest", "Keep the latest output")}</Button> : null}
        <Button disabled={evidence.keptPending} onClick={() => { setError(""); setDraft(emptyKeptDraft(step.declaredOutputs)); }} size="sm" type="button" variant="secondary">{t("react.workflows.kept_output_write", "Write it yourself")}</Button>
      </div>}
    </> : null}
    {draft !== null ? <div className="fs-field">
      <label className="fs-field-label" htmlFor={`${id}-json`}>{t("react.workflows.kept_output_values", "Values, as JSON")}</label>
      <textarea aria-describedby={error ? `${id}-error` : `${id}-help`} aria-invalid={Boolean(error)} className="fs-input workflow-kept-json w-full" id={`${id}-json`}
        onChange={(event) => { setDraft(event.target.value); setError(""); }} rows={6} spellCheck={false} value={draft} />
      <p className="fs-field-helper" id={`${id}-help`}>{t("react.workflows.kept_output_values_help", "One value per output; each is JSON and is checked on Save.")}</p>
      <div className="workflow-kept-actions">
        <Button onClick={() => { setDraft(null); setError(""); }} size="sm" type="button" variant="secondary">{t("common.cancel", "Cancel")}</Button>
        <Button aria-busy={evidence.keptPending || undefined} disabled={evidence.keptPending} onClick={save} size="sm" type="button" variant="primary">
          {busy ? <DsLoader variant="orbit-inline" /> : null}{t("common.save", "Save")}</Button>
      </div>
    </div> : null}
    {error ? <p className="workflow-kept-error" id={`${id}-error`} role="alert">{error}</p> : null}
  </section>;
}

/** The step's Test Run tab: a waiting decision, the latest workflow test run's pass, its test output. */
export function StepTestRunLatest({ evidence, step, commentDraft }: Readonly<{ evidence?: WorkflowTestRunEvidence | null; step: StepTestRunStep | null; commentDraft?: ReviewCommentDraft }>) {
  const { t } = useI18n();
  const id = useId();
  if (!evidence || !step) return null;
  const passes = evidence.passes.filter((pass) => pass.stepId === step.stepId);
  const latest = passes.at(-1);
  const approval = evidence.approval?.stepId === step.stepId ? evidence.approval : null;
  return <div className="space-y-3" data-testid="workflow-step-test-run-latest">
    {approval ? <ApprovalDecision approval={approval} commentDraft={commentDraft} /> : null}
    {latest ? <section className="workflow-step-run" aria-labelledby={`${id}-latest`}>
      <h3 className="workflow-step-run-title" id={`${id}-latest`}>{t("react.workflows.latest_workflow_test_run", "Latest workflow test run")}</h3>
      <p className="workflow-step-run-state workflow-step-run-sub" data-run={latest.state}>{passStateLine(latest, t)}</p>
      {passes.length > 1 ? <p className="workflow-step-run-sub">{t("react.workflows.step_run_latest_of_passes", "The latest of {count} passes in this run.", { count: String(passes.length) })}</p> : null}
      {latest.state !== "running" ? <RunPassValues error={latest.state === "failed" ? latest.error : ""} inputs={latest.inputs} outputs={latest.outputs} /> : null}
    </section> : null}
    <KeptTestOutputSection evidence={evidence} latest={latest} step={step} />
  </div>;
}
