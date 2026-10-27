import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/* eslint-disable react-refresh/only-export-components */
import { useId, useLayoutEffect, useState } from "react";

import { Loader2 } from "lucide-react";

import type {
  ApprovalDecisionPayload,
  JobApprovalPayload,
  JobDefinitionPayload,
} from "@/pages/jobs/detail/api";
import {
  firstReadableText,
  objectOrEmpty,
  readableText,
  type Entity,
} from "@/pages/jobs/detail/job-detail-core";
import type { CanonicalJobDetailView } from "@/pages/jobs/detail/job-detail-view-model";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { ReadableDataFields } from "./readable-data-fields";
import { archivedApprovalDestinations } from "./job-definition-graph";

function reviewWorkspaceKind(gateKind: string, stage: string): string {
  if (gateKind === "autonomy_gate") return "autonomy_gate";
  if (stage === "Orchestrator") return "clarifying_answer";
  return "document_review";
}

export function stringListFromUnknown(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => firstStringFromUnknownValue([item, ""]).trim())
    .filter(isTruthy);
}

export function approvalGateFromResponse(value: unknown): Entity {
  const record = objectOrEmpty(value);
  const payload = objectOrEmpty(record.payload);
  const gate: Entity = { ...payload };
  for (const [key, candidate] of Object.entries({
    approval_id: record.approval_id || payload.approval_id,
    job_id: record.job_id || payload.job_id,
    project_id: record.project_id || payload.project_id,
    task_summary: record.task_summary || payload.task_summary,
    thread_id: payload.thread_id || record.thread_id,
  })) {
    if (candidate !== null && candidate !== undefined && stringFromUnknownValue(candidate).trim() !== "") {
      gate[key] = candidate;
    }
  }
  return gate;
}

export function approvalGateWithThreadFallback(
  value: unknown,
  fallbackThreadId: unknown,
  detailApprovalSources: readonly unknown[] = [],
): Entity {
  const gate = approvalGateFromResponse(value);
  // The job detail response carries the same approval identity in its own shapes.
  // Without this the decision panel can render with no approval_id and refuse to
  // submit even though the id the operator needs is already on the page.
  const approvalId = firstReadableText(
    gate.approval_id,
    ...detailApprovalSources.map((source) => objectOrEmpty(source).approval_id),
    ...detailApprovalSources.map((source) => objectOrEmpty(source).id),
  );
  const threadId = firstReadableText(
    gate.thread_id,
    ...detailApprovalSources.map((source) => objectOrEmpty(source).thread_id),
    fallbackThreadId,
  );
  const resolved: Entity = { ...gate };
  if (approvalId) {
    resolved.approval_id = approvalId;
  }
  if (threadId) {
    resolved.thread_id = threadId;
  }
  return resolved;
}

export function approvalResponseFromStream(
  current: JobApprovalPayload | undefined,
  frame: Entity,
  currentJobId: string,
): JobApprovalPayload {
  const approvalId = firstReadableText(frame.approval_id, frame.interrupt_id, frame.id);
  const reviewWorkspace = objectOrEmpty(frame.review_workspace);
  const response: JobApprovalPayload = {
    job_id: firstReadableText(frame.job_id, current?.job_id, currentJobId),
    approval_id: approvalId || undefined,
    payload: {
      ...frame,
      approval_id: approvalId || undefined,
    },
    thread_id: firstReadableText(frame.thread_id, current?.thread_id) || undefined,
  };
  if (current?.project_id !== undefined) {
    response.project_id = current.project_id;
  }
  if (current?.task_summary) {
    response.task_summary = current.task_summary;
  }
  if (Object.keys(reviewWorkspace).length) {
    response.review_workspace = reviewWorkspace;
  }
  return response;
}

export function projectedReviewWorkspaceFromApproval(value: unknown): Entity {
  const record = objectOrEmpty(value);
  const direct = objectOrEmpty(record.review_workspace);
  if (Object.keys(direct).length) {
    return direct;
  }
  const payload = objectOrEmpty(record.payload);
  return objectOrEmpty(payload.review_workspace);
}

export function reviewWorkspaceFromApprovalGate(approvalGate: Entity, canMutate: boolean, t: I18nContextValue["t"] = fallbackContext.t): Entity {
  if (!Object.keys(approvalGate).length) {
    return {};
  }

  // /api/jobs/{id}/approval returns the raw gate payload, while /api/v2/jobs may
  // already include the backend semantic projection. Keep this fallback small and
  // only supply readable labels when the backend projection is absent.
  const stage = firstReadableText(approvalGate.stage, approvalGate.stage_label, t("react.jobs.review", "Review"));
  const message = readableText(
    approvalGate.message ?? approvalGate.summary ?? approvalGate.task_summary,
    5000,
  );
  const content = approvalGate.content ?? approvalGate.review_content ?? approvalGate.markdown ?? approvalGate.technical_details;
  const technicalDetails = readableText(approvalGate.technical_details, 5000);
  const gateKind = firstStringFromUnknownValue([approvalGate.gate_kind, ""]).trim().toLowerCase();
  const kind = reviewWorkspaceKind(gateKind, stage);

  const isAutonomyGate = kind === "autonomy_gate";

  return {
    kind,
    title: stage.toLowerCase() === "review" || stage === t("react.jobs.review", "Review")
      ? t("react.jobs.review", "Review")
      : t("react.jobs.value1_review", "{value1} review", { value1: String(stage) }),
    stage_label: stage,
    intro: message || t("react.jobs.active_human_decision", "A human decision is active for this run."),
    thread_id: approvalGate.thread_id,
    question: kind === "clarifying_answer" ? message || content : undefined,
    content,
    content_is_present: content !== undefined && content !== null && content !== "",
    details_title: isAutonomyGate ? t("react.jobs.decision_details", "Decision details") : undefined,
    details: isAutonomyGate ? technicalDetails || content : undefined,
    comment_required_on_reject: true,
    read_only: !canMutate,
    approve_label: isAutonomyGate ? t("jobs.detail.review.approve_continue", "Approve and continue") : t("jobs.actions.approve", "Approve"),
    reject_label: t("jobs.detail.review.reject", "Reject"),
    submit_label: t("jobs.detail.review.send_answer", "Send answer"),
  };
}

export function resolveReviewWorkspace({
  approvalPayload,
  approvalGate,
  canMutate,
  canonicalDetail,
  currentJobId = canonicalDetail.jobId,
  runDefinition,
}: {
  approvalPayload: JobApprovalPayload | undefined;
  approvalGate?: Entity;
  canMutate: boolean;
  canonicalDetail: CanonicalJobDetailView;
  currentJobId?: string;
  runDefinition?: JobDefinitionPayload;
}, t: I18nContextValue["t"] = fallbackContext.t): Entity {
  const currentGate = approvalGate || approvalGateFromResponse(approvalPayload);
  const withRunDestinations = (workspace: Entity): Entity => {
    if (currentGate.gate_kind !== "approval") return workspace;
    const sameJob = canonicalDetail.jobId === currentJobId && firstReadableText(currentGate.job_id) === currentJobId;
    return {
      ...workspace,
      destinations: archivedApprovalDestinations(sameJob ? runDefinition : undefined, {
        jobId: currentJobId, workflowId: canonicalDetail.selectedWorkflowId,
        stepId: firstReadableText(currentGate.step_id), stepExecutionId: firstReadableText(currentGate.step_execution_id),
      }, t),
    };
  };
  const projected = canonicalDetail.sections.reviewWorkspace;
  const jobPayload = canonicalDetail.job;
  if (Object.keys(projected).length) {
    return withRunDestinations(projected);
  }
  const projectedFromApproval = projectedReviewWorkspaceFromApproval(approvalPayload);
  if (Object.keys(projectedFromApproval).length) {
    return withRunDestinations(projectedFromApproval);
  }
  const gate = approvalGateFromResponse(approvalPayload);
  if (Object.keys(gate).length) {
    return withRunDestinations(reviewWorkspaceFromApprovalGate(gate, canMutate, t));
  }
  if (canonicalDetail.statusCode !== "awaiting_approval") {
    return {};
  }
  return {
    kind: "document_review",
    title: t("jobs.detail.review.workspace_title", "Review workspace"),
    intro: t("react.jobs.active_human_decision", "A human decision is active for this run."),
    thread_id: firstReadableText(jobPayload.thread_id),
    content: canonicalDetail.taskSummary,
    content_is_present: Boolean(
      canonicalDetail.taskSummary,
    ),
    comment_required_on_reject: true,
    read_only: !canMutate,
    approve_label: t("jobs.detail.review.approve", "Approve"),
    reject_label: t("jobs.detail.review.reject", "Reject"),
  };
}

export function ReviewTextBlock({ label, value }: Readonly<{ label: string; value: unknown }>) {
  let content = value;
  if (typeof content === "string") {
    const text = content.trim();
    if (text.startsWith("{") || text.startsWith("[")) {
      try { content = JSON.parse(text) as unknown; } catch { /* Keep the public bounded text if its preview is incomplete. */ }
    }
  }
  if (content === undefined || content === null || content === "") return null;
  return (
    <div className="min-w-0 fs-surface-radius border border-subtle fs-surface-inset px-3 py-3 text-sm fs-ink-primary">
      <p className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">{label}</p>
      {typeof content === "object" ? <div className="mt-2"><ReadableDataFields value={Array.isArray(content) || !Object.keys(objectOrEmpty(content)).length ? { value: content } : content} /></div>
        : <p className="mt-2 whitespace-pre-wrap break-words leading-6">{stringFromUnknownValue(content)}</p>}
    </div>
  );
}

function ReviewDestinations({ value }: Readonly<{ value: unknown }>) {
  const { t } = useI18n();
  const destinations = objectOrEmpty(value);
  if (!Object.keys(destinations).length) return null;
  return <dl className="grid min-w-0 gap-2 sm:grid-cols-2">
    {(["approved", "rejected"] as const).map((outcome) => <div key={outcome} className="min-w-0 fs-surface-radius border border-subtle fs-surface-inset px-3 py-2">
      <dt className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">{outcome === "approved" ? t("react.workflows.outcome_approved", "Approved") : t("react.workflows.outcome_rejected", "Rejected")}</dt>
      <dd className="mt-1 break-words text-sm fs-ink-primary">{typeof destinations[outcome] === "string" && destinations[outcome] ? destinations[outcome] : t("react.jobs.not_recorded", "Not recorded")}</dd>
    </div>)}
  </dl>;
}

export type ReviewCommentDraft = Readonly<{
  identity: string;
  value: string;
  onChange: (identity: string, value: string) => void;
}>;

type ReviewWorkspacePanelProps = Readonly<{
  commentDraft?: ReviewCommentDraft;
  approvalGate: Entity;
  canMutate: boolean;
  decisionUnavailableReason?: string;
  isLoading: boolean;
  onSubmit: (threadId: string, payload: ApprovalDecisionPayload) => Promise<void>;
  submitting: boolean;
  workspace: Entity;
}>;

export function ReviewWorkspacePanel(props: ReviewWorkspacePanelProps) {
  const approvalId = firstReadableText(
    props.workspace.approval_id,
    props.approvalGate.approval_id,
  );
  const threadId = firstReadableText(props.workspace.thread_id, props.approvalGate.thread_id);
  const commentIdentity = threadId && approvalId ? JSON.stringify([threadId, approvalId]) : "";
  return <StatefulReviewWorkspacePanel key={props.commentDraft ? commentIdentity : approvalId || "approval-missing"} {...props} commentIdentity={commentIdentity} />;
}

type ReviewMode = "answer" | "approve" | "reject" | "override" | "abort";
type ReviewSubmit = (mode: ReviewMode) => Promise<void>;

type ReviewPayloadResult = {
  error?: string;
  payload?: ApprovalDecisionPayload;
};

function clarifyingDecisionPayload(
  approvalId: string,
  answer: string,
  t: I18nContextValue["t"],
): ReviewPayloadResult {
  const trimmedAnswer = answer.trim();
  if (!trimmedAnswer) {
    return { error: t("react.jobs.answer_required", "Type an answer before sending it to the orchestrator.") };
  }
  return {
    payload: {
      approval_id: approvalId,
      approved: true,
      comment: trimmedAnswer,
    },
  };
}

function standardDecisionPayload({
  approvalId,
  comment,
  mode,
  overrideContent,
  overrideSupported,
  rejectRequiresComment,
}: {
  approvalId: string;
  comment: string;
  mode: ReviewMode;
  overrideContent: string;
  overrideSupported: boolean;
  rejectRequiresComment: boolean;
}, t: I18nContextValue["t"]): ReviewPayloadResult {
  const isReject = mode === "reject" || mode === "abort";
  const isOverride = mode === "override";
  const trimmedComment = comment.trim();
  const trimmedOverride = overrideContent.trim();
  if (mode === "reject" && rejectRequiresComment && !trimmedComment) {
    return { error: t("react.jobs.rejection_comment_required", "A rejection comment is required for this review gate.") };
  }
  if (isOverride && overrideSupported && !trimmedOverride) {
    return {
      error: t("react.jobs.override_content_required", "Add override content before continuing with the override action."),
    };
  }
  const payload: ApprovalDecisionPayload = {
    approval_id: approvalId,
    approved: !isReject,
    comment: trimmedComment,
  };
  if (mode === "abort") {
    payload.action = "abort";
  }
  if (isOverride && overrideSupported) {
    payload.action = "override";
    payload.override_content = trimmedOverride;
  }
  return { payload };
}

function decisionPayload({
  answer,
  approvalId,
  comment,
  kind,
  mode,
  overrideContent,
  overrideSupported,
  rejectRequiresComment,
}: {
  answer: string;
  approvalId: string;
  comment: string;
  kind: string;
  mode: ReviewMode;
  overrideContent: string;
  overrideSupported: boolean;
  rejectRequiresComment: boolean;
}, t: I18nContextValue["t"]): ReviewPayloadResult {
  if (kind === "clarifying_answer") {
    return clarifyingDecisionPayload(approvalId, answer, t);
  }
  return standardDecisionPayload({
    approvalId,
    comment,
    mode,
    overrideContent,
    overrideSupported,
    rejectRequiresComment,
  }, t);
}

function ReviewWorkspaceHeader({
  intro,
  stage,
  title,
}: Readonly<{ intro: string; stage: string; title: string }>) {
  return (
    <div className="min-w-0">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <h3 className="min-w-0 break-words text-lg font-medium fs-ink-primary">
          {title}
        </h3>
        {stage ? <span className="fs-badge orange max-w-full">{stage}</span> : null}
      </div>
      <p className="mt-1 break-words text-sm fs-ink-secondary">{intro}</p>
    </div>
  );
}

function ReviewLoadingNotice({
  isLoading,
}: Readonly<{ isLoading: boolean }>) {
  const { t } = useI18n();
  if (!isLoading) {
    return null;
  }
  return (
    <div className="inline-flex items-center gap-2 fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-secondary">
      <Loader2 className="h-4 w-4 animate-spin" />
      {t("react.jobs.loading_approval_workspace", "Loading approval workspace…")} </div>
  );
}

function AutonomyReviewContent({ workspace }: Readonly<{ workspace: Entity }>) {
  const { t } = useI18n();
  return (
    <>
      <ReviewTextBlock
        label={t("jobs.detail.review.what_orchestrator_wants", "What the orchestrator wants to do")}
        value={workspace.content}
      />
      {workspace.details ? (
        <details className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2">
          <summary className="cursor-pointer text-sm fs-ink-primary">
            {firstReadableText(workspace.details_title, t("jobs.detail.review.decision_details", "Decision details"))}
          </summary>
          <p className="mt-2 whitespace-pre-wrap break-words text-sm fs-ink-secondary">
            {readableText(workspace.details, 5000)}
          </p>
        </details>
      ) : null}
    </>
  );
}

function ReviewWorkspaceContent({
  kind,
  workspace,
}: Readonly<{
  kind: string;
  workspace: Entity;
}>) {
  const { t } = useI18n();
  if (kind === "clarifying_answer") {
    return (
      <ReviewTextBlock
        label={t("jobs.detail.review.clarifying_question", "Clarifying question")}
        value={workspace.question || workspace.content}
      />
    );
  }
  if (kind === "autonomy_gate") {
    return <AutonomyReviewContent workspace={workspace} />;
  }
  return <>
    <ReviewTextBlock label={t("jobs.detail.review.content", "Review content")} value={workspace.content} />
    <ReviewDestinations value={workspace.destinations} />
  </>;
}

function ReviewDraftFields({
  answer,
  comment,
  kind,
  overrideContent,
  overrideSupported,
  rejectRequiresComment,
  setAnswer,
  setComment,
  setOverrideContent,
  workspace,
}: Readonly<{
  answer: string;
  comment: string;
  kind: string;
  overrideContent: string;
  overrideSupported: boolean;
  rejectRequiresComment: boolean;
  setAnswer: (value: string) => void;
  setComment: (value: string) => void;
  setOverrideContent: (value: string) => void;
  workspace: Entity;
}>) {
  const { t } = useI18n();
  const rejectionCommentHintId = useId();
  return (
    <>
      {kind === "clarifying_answer" ? (
        <label className="fs-field min-w-0">
          <span className="fs-field-label">{t("jobs.detail.review.your_answer", "Your answer")}</span>
          <textarea
            className="fs-textarea"
            data-testid="approval-answer-input"
            id="approval-answer-input"
            name="approval_answer"
            onChange={(event) => setAnswer(event.target.value)}
            placeholder={t("jobs.detail.review.answer_placeholder", "Type the answer the orchestrator should receive.")}
            value={answer}
          />
        </label>
      ) : (
        <label className="fs-field min-w-0">
          <span className="fs-field-label" id={`${rejectionCommentHintId}-label`}>
            {rejectRequiresComment ? t("jobs.detail.review.comment_for_rejection", "Comment for rejection") : t("jobs.detail.review.comment", "Comment")}
          </span>
          <textarea
            aria-labelledby={`${rejectionCommentHintId}-label`}
            aria-describedby={rejectRequiresComment ? rejectionCommentHintId : undefined}
            className="fs-textarea"
            data-testid="approval-comment-input"
            id="approval-comment-input"
            name="approval_comment"
            onChange={(event) => setComment(event.target.value)}
            placeholder={t("jobs.detail.review.comment_placeholder_generic", "Add review feedback for the orchestrator.")}
            value={comment}
          />
          {rejectRequiresComment ? (
            <span className="fs-field-helper" id={rejectionCommentHintId}>
              {t("jobs.detail.review.comment_rejection_requirement", "Required when rejecting. Optional when approving.")}
            </span>
          ) : null}
        </label>
      )}
      {overrideSupported ? (
        <label className="fs-field min-w-0">
          <span className="fs-field-label">
            {firstReadableText(
              workspace.override_textarea_label,
              t("jobs.detail.review.override_content", "Override content"),
            )}
          </span>
          <textarea
            className="fs-textarea"
            data-testid="approval-override-input"
            id="approval-override-input"
            name="approval_override"
            onChange={(event) => setOverrideContent(event.target.value)}
            placeholder={firstReadableText(
              workspace.override_placeholder,
              t("jobs.detail.review.override_placeholder", "Paste the replacement content the orchestrator should continue with."),
            )}
            value={overrideContent}
          />
          {readableText(workspace.override_help) ? (
            <span className="mt-1 block text-xs fs-ink-tertiary">
              {readableText(workspace.override_help)}
            </span>
          ) : null}
        </label>
      ) : null}
    </>
  );
}

function StandardDecisionButtons({
  overrideSupported,
  reworkExhausted,
  submit,
  submitting,
  workspace,
}: Readonly<{
  overrideSupported: boolean;
  reworkExhausted: boolean;
  submit: ReviewSubmit;
  submitting: boolean;
  workspace: Entity;
}>) {
  const { t } = useI18n();
  return (
    <>
      <button
        className="fs-button primary"
        data-testid="approval-approve-button"
        disabled={submitting}
        onClick={() => ignoreAsyncResult(submit("approve"))}
        type="button"
      >
        {firstReadableText(workspace.approve_label, t("jobs.actions.approve", "Approve"))}
      </button>
      {overrideSupported ? (
        <button
          className="fs-button accent"
          data-testid="approval-override-button"
          disabled={submitting}
          onClick={() => ignoreAsyncResult(submit("override"))}
          type="button"
        >
          {firstReadableText(
            workspace.override_label,
            t("jobs.detail.review.override_continue", "Override and continue"),
          )}
        </button>
      ) : null}
      {reworkExhausted ? (
        <button
          className="fs-button danger"
          data-testid="approval-abort-button"
          disabled={submitting}
          onClick={() => ignoreAsyncResult(submit("abort"))}
          type="button"
        >
          {t("react.jobs.cancel_run", "Cancel run")} </button>
      ) : (
        <button
          className="fs-button danger"
          data-testid="approval-reject-button"
          disabled={submitting}
          onClick={() => ignoreAsyncResult(submit("reject"))}
          type="button"
        >
          {firstReadableText(workspace.reject_label, t("jobs.detail.review.reject", "Reject"))}
        </button>
      )}
    </>
  );
}

function ReviewDecisionButtons({
  kind,
  overrideSupported,
  reworkExhausted,
  submit,
  submitting,
  workspace,
}: Readonly<{
  kind: string;
  overrideSupported: boolean;
  reworkExhausted: boolean;
  submit: ReviewSubmit;
  submitting: boolean;
  workspace: Entity;
}>) {
  const { t } = useI18n();
  if (kind === "clarifying_answer") {
    return (
      <button
        className="fs-button primary"
        data-testid="approval-answer-submit"
        disabled={submitting}
        onClick={() => ignoreAsyncResult(submit("answer"))}
        type="button"
      >
        {firstReadableText(workspace.submit_label, t("jobs.detail.review.send_answer", "Send answer"))}
      </button>
    );
  }
  return (
    <StandardDecisionButtons
      overrideSupported={overrideSupported}
      reworkExhausted={reworkExhausted}
      submit={submit}
      submitting={submitting}
      workspace={workspace}
    />
  );
}

function ReviewDecisionControls({
  answer,
  canDecide,
  comment,
  decisionUnavailableReason,
  error,
  kind,
  overrideContent,
  overrideSupported,
  rejectRequiresComment,
  reworkExhausted,
  setAnswer,
  setComment,
  setOverrideContent,
  submit,
  submitting,
  workspace,
}: Readonly<{
  answer: string;
  canDecide: boolean;
  comment: string;
  decisionUnavailableReason?: string;
  error: string;
  kind: string;
  overrideContent: string;
  overrideSupported: boolean;
  rejectRequiresComment: boolean;
  reworkExhausted: boolean;
  setAnswer: (value: string) => void;
  setComment: (value: string) => void;
  setOverrideContent: (value: string) => void;
  submit: ReviewSubmit;
  submitting: boolean;
  workspace: Entity;
}>) {
  const { t } = useI18n();
  if (!canDecide) {
    return (
      <div className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-secondary">
        {decisionUnavailableReason ||
          t("react.jobs.viewer_mode_is_read_only_an_operator_or_admin_has_to_decide_this_gate", "Viewer mode is read-only. An operator or admin has to decide this gate.")}
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <ReviewDraftFields
        answer={answer}
        comment={comment}
        kind={kind}
        overrideContent={overrideContent}
        overrideSupported={overrideSupported}
        rejectRequiresComment={rejectRequiresComment && !reworkExhausted}
        setAnswer={setAnswer}
        setComment={setComment}
        setOverrideContent={setOverrideContent}
        workspace={workspace}
      />
      {error ? (
        <div role="alert" className="fs-surface-radius border border-danger fs-surface-danger px-3 py-2 text-sm fs-ink-danger">
          {error}
        </div>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <ReviewDecisionButtons
          kind={kind}
          overrideSupported={overrideSupported}
          reworkExhausted={reworkExhausted}
          submit={submit}
          submitting={submitting}
          workspace={workspace}
        />
      </div>
    </div>
  );
}

function StatefulReviewWorkspacePanel({
  approvalGate,
  canMutate,
  commentDraft,
  commentIdentity,
  decisionUnavailableReason,
  isLoading,
  onSubmit,
  submitting,
  workspace,
}: ReviewWorkspacePanelProps & { commentIdentity: string }) {
  const { t } = useI18n();
  const [comment, setComment] = useState(() => commentDraft?.identity === commentIdentity ? commentDraft.value : "");
  const persistComment = commentDraft?.onChange;
  useLayoutEffect(() => {
    if (commentIdentity) persistComment?.(commentIdentity, comment);
  }, [commentIdentity, comment, persistComment]);
  const [answer, setAnswer] = useState("");
  const [overrideContent, setOverrideContent] = useState("");
  const [error, setError] = useState("");

  const kind = firstStringFromUnknownValue([
    workspace.kind,
    "document_review",
  ]).trim();
  const threadId = firstReadableText(
    workspace.thread_id,
    approvalGate.thread_id,
  );
  const approvalId = firstReadableText(
    workspace.approval_id,
    approvalGate.approval_id,
  );
  const canDecide =
    canMutate && !decisionUnavailableReason && !workspace.read_only;
  const rejectRequiresComment = Boolean(
    workspace.comment_required_on_reject,
  );
  const overrideSupported = Boolean(workspace.override_content_supported);
  const allowedActions = stringListFromUnknown(approvalGate.allowed_actions);
  const reworkExhausted =
    Boolean(approvalGate.rework_budget_exhausted) ||
    (allowedActions.includes("abort") && !allowedActions.includes("reject"));

  const submit: ReviewSubmit = async (mode) => {
    setError("");
    if (!threadId) {
      setError(t("react.jobs.this_approval_gate_has_no_thread_id_recorded", "This approval gate has no thread ID recorded."));
      return;
    }
    if (!approvalId) {
      setError(
        t("react.jobs.this_approval_gate_has_no_approval_id_recorded_refresh_before_deciding", "This approval gate has no approval ID recorded. Refresh before deciding."),
      );
      return;
    }
    if (!canDecide) {
      setError(
        decisionUnavailableReason ||
          t("react.jobs.viewer_mode_is_read_only_an_operator_or_admin_has_to_decide_this_gate", "Viewer mode is read-only. An operator or admin has to decide this gate."),
      );
      return;
    }
    const result = decisionPayload({
      answer,
      approvalId,
      comment,
      kind,
      mode,
      overrideContent,
      overrideSupported,
      rejectRequiresComment,
    }, t);
    if (result.error) {
      setError(result.error);
      return;
    }
    if (!result.payload) {
      return;
    }
    try {
      await onSubmit(threadId, result.payload);
    } catch (submitError) {
      setError(
        submitError instanceof Error
          ? submitError.message
          : t("react.jobs.approval_request_failed", "Approval request failed."),
      );
    }
  };

  // A live fallback may announce a gate before its canonical identity arrives.
  // Keep the operator out of that temporary form until a decision can be bound
  // to both the current approval and its thread.
  if (canDecide && (!approvalId || !threadId)) {
    return <ReviewLoadingNotice isLoading />;
  }

  const title = firstReadableText(workspace.title, t("jobs.detail.review.workspace_title", "Review workspace"));
  const intro = firstReadableText(
    workspace.intro,
    t("react.jobs.active_human_decision", "A human decision is active for this run."),
  );
  const stage = firstReadableText(workspace.stage_label);

  return (
    <div
      className="space-y-4"
      data-review-kind={kind}
      data-testid="job-review-workspace-body"
    >
      <ReviewWorkspaceHeader intro={intro} stage={stage} title={title} />
      <ReviewLoadingNotice isLoading={isLoading && !threadId} />
      <ReviewWorkspaceContent kind={kind} workspace={workspace} />
      <ReviewDecisionControls
        answer={answer}
        canDecide={canDecide}
        comment={comment}
        decisionUnavailableReason={decisionUnavailableReason}
        error={error}
        kind={kind}
        overrideContent={overrideContent}
        overrideSupported={overrideSupported}
        rejectRequiresComment={rejectRequiresComment}
        reworkExhausted={reworkExhausted}
        setAnswer={setAnswer}
        setComment={setComment}
        setOverrideContent={setOverrideContent}
        submit={submit}
        submitting={submitting}
        workspace={workspace}
      />
    </div>
  );
}
