import { Button } from "@/components/ui/button";
import { DsAlert } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import {
  hasPreviewContent,
  hasStructuredEntries,
  objectOrEmpty,
  renderKeyValueSummary,
  renderPreviewSummary,
  textOrEmpty,
  workflowTestRunArtifacts,
  workflowTestRunFinalResult,
  workflowTestRunNeedsApproval,
  workflowTestRunOutputs,
  workflowTraceInputValue,
  workflowTraceOutputValue,
} from "@/pages/workflows/detail/detail-helpers";
import type { JobDetailInspectionRequest } from "@/pages/jobs/detail";
import type { ReactNode } from "react";
import type { DrawerGeometry } from "@/components/shared/design-system-overlays";
import type { JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import type { ReviewCommentDraft } from "@/pages/jobs/detail/job-detail-review-panels";
import type { WorkflowTestRunEvidence } from "./detail-test-run-evidence";
import { WorkflowTestRunResults } from "./detail-test-run-results";

type WorkflowTestRunSurfaceContext = Record<string, any> & {
  selectedTestRunControlled?: boolean;
  approvalCommentDraft?: ReviewCommentDraft;
  workflowRunInspectionRequest?: JobDetailInspectionRequest | null;
  stepInspectorHost?: DrawerGeometry | null;
  stepInspectorChrome?: ReactNode;
  selectedWorkflowTestRun?: { jobId: string; workflowId: string; projectId: string; generation: number; isCurrent: () => boolean } | null;
  onWorkflowExecutionSnapshotChange?: (snapshot: JobExecutionSnapshot | null) => void;
  /** The compact results the builder shows instead of the whole Job Details page. */
  testRunEvidence?: WorkflowTestRunEvidence | null;
};
export function WorkflowTestRunSurface({
  ctx,
}: Readonly<{
  ctx: WorkflowTestRunSurfaceContext;
}>) {
  const { t } = useI18n();
  const {
    drawerMode,
    drawerTab,
    selectedProjectId,
    setWorkflowTestRunApprovalComment,
    workflowId,
    workflowTestRunApprovalComment,
    workflowTestRunApprovalMutation,
    workflowTestRunJobId,
    workflowTestRunMessage,
    workflowTestRunMutation,
    workflowTestRunPayload,
  } = ctx;
  const controlled = ctx.selectedTestRunControlled === true;
  const evidence = ctx.testRunEvidence;
  return (
    <>
          {drawerMode === "workflow-test-run" && drawerTab === "test-run" ? (
            <div className="space-y-3" id="workflow-test-run-surface">
              {ctx.workflowTestRunMutationIsCurrent !== false && workflowTestRunMutation.isError && (workflowTestRunJobId || workflowTestRunPayload) ? (
                <p className="text-sm font-medium text-[var(--fs-text-tertiary)]" role="status">{t("react.workflows.previous_test_run_after_failure", "Previous test run — the latest start request failed.")}</p>
              ) : null}
              {ctx.workflowTestRunApprovalMutationIsCurrent !== false && workflowTestRunApprovalMutation.isError && workflowTestRunMessage ? (
                <DsAlert title={workflowTestRunMessage} tone="danger" />
              ) : null}
              {controlled && evidence?.verified ? (
                <WorkflowTestRunResults commentDraft={ctx.approvalCommentDraft} evidence={evidence} inspect={ctx.workflowRunInspectionRequest?.kind === "step" && ctx.workflowRunInspectionRequest.isCurrent()
                  ? { serial: ctx.workflowRunInspectionRequest.serial, jobId: ctx.workflowRunInspectionRequest.jobId, stepExecutionId: ctx.workflowRunInspectionRequest.stepExecutionId } : null} />
              ) : controlled ? (
                <p role="status" className="text-sm text-[var(--fs-text-secondary)]">{ctx.selectedRunVerificationError || (ctx.selectedRunLoading
                  ? t("react.workflows.run_bar_loading", "Loading Test Run…")
                  : t("react.workflows.run_bar_unavailable", "Test Run could not be verified"))}</p>
              ) : workflowTestRunPayload ? (
                <div className="rounded-lg border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] p-3 text-xs text-[var(--fs-text-primary)]">
                  {(() => {
                    const runOutputs = workflowTestRunOutputs(workflowTestRunPayload);
                    const runArtifacts = workflowTestRunArtifacts(workflowTestRunPayload);
                    const finalResult = workflowTestRunFinalResult(workflowTestRunPayload);
                    const traceRows = Array.isArray(workflowTestRunPayload.trace)
                      ? workflowTestRunPayload.trace
                      : [];
                    return (
                      <>
                  <p className="font-medium text-[var(--fs-text-primary)]">{t("react.workflows.latest_workflow_test_run", "Latest workflow test run")}</p>
                  <div className="mt-2 space-y-1">
                    <p>{t("react.workflows.status", "Status:")}{" "}{textOrEmpty(workflowTestRunPayload.final_status) || textOrEmpty(workflowTestRunPayload.status) || t("project_detail.connections.status.unknown", "unknown")}</p>
                    <p>{t("react.workflows.workflow", "Workflow:")}{" "}{textOrEmpty(workflowTestRunPayload.workflow_id) || workflowId}</p>
                    <p>{t("settings.notifications.project", "Project:")}{" "}{textOrEmpty(workflowTestRunPayload.project_id) || selectedProjectId || "—"}</p>
                    <p>{t("react.workflows.execution", "Execution:")}{" "}{textOrEmpty(workflowTestRunPayload.execution_id) || t("react.workflows.ephemeral_session", "Ephemeral session")}</p>
                  </div>
                  {textOrEmpty(workflowTestRunPayload.final_report) ? (
                    <p className="mt-3 text-[var(--fs-text-primary)]">
                      {textOrEmpty(workflowTestRunPayload.final_report)}
                    </p>
                  ) : null}
                  {hasPreviewContent(finalResult) ? (
                    <div className="mt-3 rounded-lg border border-[var(--fs-status-success-border)] bg-[var(--fs-status-success-bg)] p-3">
                      <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-status-success)]">
                        {t("react.workflows.final_result", "Final result")}</p>
                      <div className="mt-2">
                        {renderPreviewSummary(
                          finalResult,
                          t("react.workflows.display_no_final_preview", "This run did not publish a final result preview."), undefined, t
                        )}
                      </div>
                    </div>
                  ) : null}
                  {workflowTestRunNeedsApproval(workflowTestRunPayload) ? (
                    <div className="mt-3 rounded-lg border border-amber-300/20 bg-amber-500/10 p-3 text-[var(--fs-text-primary)]">
                      <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-primary)]">
                        {t("react.workflows.review_decision", "Review decision")}</p>
                      <p className="mt-2 text-sm text-[var(--fs-text-primary)]">
                        {textOrEmpty(workflowTestRunPayload.approval_context)
                          || t("react.workflows.review_the_generated_work_before_continuing_this_workflow_test_run", "Review the generated work before continuing this workflow test run.")}
                      </p>
                      <label className="mt-3 block text-xs text-[var(--fs-text-primary)]">
                        <span>{t("jobs.detail.review.review_comment", "Review comment")}</span>
                        <textarea
                          aria-label={t("react.workflows.workflow_test_run_approval_comment", "Workflow test run approval comment")}
                          className="fs-input mt-1 w-full"
                          onChange={(event) => setWorkflowTestRunApprovalComment(event.target.value)}
                          rows={3}
                          value={workflowTestRunApprovalComment}
                        />
                      </label>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <Button
                          variant="primary"
                          disabled={workflowTestRunApprovalMutation.isPending}
                          onClick={() =>
                            workflowTestRunApprovalMutation.mutate({ approved: true })
                          }
                          type="button"
                        >
                          {workflowTestRunApprovalMutation.isPending
                            ? t("react.workflows.continuing", "Continuing...")
                            : t("jobs.detail.review.approve_continue", "Approve and continue")}
                        </Button>
                        <Button
                          variant="secondary"
                          disabled={workflowTestRunApprovalMutation.isPending}
                          onClick={() =>
                            workflowTestRunApprovalMutation.mutate({ approved: false })
                          }
                          type="button"
                        >
                          {t("react.workflows.reject_and_continue", "Reject and continue")}</Button>
                      </div>
                    </div>
                  ) : null}
                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                    <div>
                      <p className="text-[var(--fs-text-tertiary)]">{t("react.workflows.final_outputs", "Final outputs")}</p>
                      <div className="mt-1">
                        {renderKeyValueSummary(
                          runOutputs,
                          t("react.workflows.display_no_named_outputs", "This run did not publish named outputs."),
                          t,
                        )}
                      </div>
                    </div>
                    <div>
                      <p className="text-[var(--fs-text-tertiary)]">{t("react.workflows.final_artifacts", "Final artifacts")}</p>
                      <div className="mt-1">
                        {renderKeyValueSummary(
                          runArtifacts,
                          t("react.workflows.display_no_named_artifacts", "This run did not publish named artifacts."),
                          t,
                        )}
                      </div>
                    </div>
                  </div>
                  {traceRows.length ? (
                    <div className="mt-3 space-y-2">
                      {traceRows.map((row: unknown, index: number) => {
                        const trace = objectOrEmpty(row);
                        const traceArtifacts = objectOrEmpty(trace.artifacts);
                        const traceSourcePayload = objectOrEmpty(trace.source_payload);
                        const previousTrace =
                          index > 0
                            ? objectOrEmpty(traceRows[index - 1])
                            : {};
                        const previousTraceOutput = workflowTraceOutputValue(previousTrace);
                        const traceInput = workflowTraceInputValue(
                          traceSourcePayload,
                          previousTraceOutput,
                        );
                        const traceOutput = workflowTraceOutputValue(trace);
                        return (
                          <div
                            className="rounded-lg border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] px-3 py-2"
                            key={`workflow-test-run-trace-${textOrEmpty(trace.step_id) || textOrEmpty(trace.step_label) || String(index)}`}
                          >
                            <p className="font-medium text-[var(--fs-text-primary)]">
                              {textOrEmpty(trace.step_label) || textOrEmpty(trace.step_id) || `Step ${index + 1}`}
                            </p>
                            <p className="mt-1 text-[var(--fs-text-tertiary)]">
                              {textOrEmpty(trace.group_id) ? `${t("react.workflows.group_label", "group:")} ${textOrEmpty(trace.group_id)} · ` : ""}
                              {t("react.workflows.status", "Status:")}{" "}
                              {textOrEmpty(trace.status) || t("project_detail.connections.status.unknown", "unknown")}
                            </p>
                            <p className="text-[var(--fs-text-tertiary)]">
                              {textOrEmpty(trace.summary) || textOrEmpty(trace.error_summary) || t("react.workflows.no_summary_returned", "No summary returned.")}
                            </p>
                            {hasPreviewContent(traceInput) || hasPreviewContent(traceOutput) ? (
                              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                                <div className="rounded-lg border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] p-2">
                                  <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-[var(--fs-text-primary)]">
                                    {t("registry.tool_detail.schemas.input", "Input")}</p>
                                  <div className="mt-2">
                                    {renderPreviewSummary(
                                      traceInput,
                                      t("react.workflows.display_no_input_snapshot", "No explicit input snapshot was captured."), undefined, t
                                    )}
                                  </div>
                                </div>
                                <div className="rounded-lg border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] p-2">
                                  <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-[var(--fs-text-primary)]">
                                    {t("registry.tool_detail.schemas.output", "Output")}</p>
                                  <div className="mt-2">
                                    {renderPreviewSummary(
                                      traceOutput,
                                      t("react.workflows.display_no_output_snapshot", "No output snapshot was captured."), undefined, t
                                    )}
                                  </div>
                                </div>
                              </div>
                            ) : null}
                            {hasStructuredEntries(traceArtifacts) ||
                            hasStructuredEntries(traceSourcePayload) ? (
                              <details className="mt-2 rounded-lg border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] p-2">
                                <summary className="cursor-pointer text-[11px] text-[var(--fs-text-primary)]">
                                  {t("react.workflows.step_payload", "Step payload")}</summary>
                                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                                  <div>
                                    <p className="text-[11px] text-[var(--fs-text-tertiary)]">{t("react.workflows.raw_inputs", "Raw inputs")}</p>
                                    <pre className="mt-1 overflow-x-auto rounded bg-[var(--fs-bg-surface)] p-2 text-[11px] text-[var(--fs-text-primary)]">
                                      {JSON.stringify(traceSourcePayload, null, 2)}
                                    </pre>
                                  </div>
                                  <div>
                                    <p className="text-[11px] text-[var(--fs-text-tertiary)]">{t("react.workflows.raw_result", "Raw result")}</p>
                                    <pre className="mt-1 overflow-x-auto rounded bg-[var(--fs-bg-surface)] p-2 text-[11px] text-[var(--fs-text-primary)]">
                                      {JSON.stringify(traceArtifacts, null, 2)}
                                    </pre>
                                  </div>
                                </div>
                              </details>
                            ) : null}
                          </div>
                        );
                      })}
                    </div>
                  ) : null}
                  <details className="mt-3 rounded-lg border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] p-3">
                    <summary className="cursor-pointer text-[var(--fs-text-primary)]">
                      {t("react.workflows.raw_workflow_test_run_payload", "Raw workflow test run payload")}</summary>
                    <div className="mt-3 grid gap-3 sm:grid-cols-2">
                      <div>
                        <p className="text-[var(--fs-text-tertiary)]">{t("react.workflows.raw_outputs", "Raw outputs")}</p>
                        <pre className="mt-1 overflow-x-auto rounded bg-[var(--fs-bg-surface)] p-2 text-[11px] text-[var(--fs-text-primary)]">
                          {JSON.stringify(runOutputs, null, 2)}
                        </pre>
                      </div>
                      <div>
                        <p className="text-[var(--fs-text-tertiary)]">{t("react.workflows.raw_artifacts", "Raw artifacts")}</p>
                        <pre className="mt-1 overflow-x-auto rounded bg-[var(--fs-bg-surface)] p-2 text-[11px] text-[var(--fs-text-primary)]">
                          {JSON.stringify(runArtifacts, null, 2)}
                        </pre>
                      </div>
                    </div>
                    <div className="mt-3">
                      <p className="text-[var(--fs-text-tertiary)]">{t("react.workflows.full_response", "Full response")}</p>
                      <pre className="mt-1 overflow-x-auto rounded bg-[var(--fs-bg-surface)] p-2 text-[11px] text-[var(--fs-text-primary)]">
                        {JSON.stringify(workflowTestRunPayload, null, 2)}
                      </pre>
                    </div>
                  </details>
                      </>
                    );
                  })()}
                </div>
              ) : null}
            </div>
          ) : null}
    </>
  );
}
