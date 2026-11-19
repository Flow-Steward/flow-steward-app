import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { DsAlert, DsBadge, DsCard } from "@/components/shared/design-system";
import type { FormFieldView, FormValues } from "@/components/workflow-inputs/form-field";
import { WorkflowInputFields } from "@/components/workflow-inputs/workflow-input-fields";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { submitApprovalDecision, type ApprovalDecisionPayload } from "@/pages/jobs/detail/api";
import { JobFileItem } from "@/pages/jobs/detail/job-detail-file-preview";
import { filePreviewKind, filePreviewUrl } from "@/pages/jobs/detail/job-detail-file-preview-model";
import { ReadableDataFields } from "@/pages/jobs/detail/readable-data-fields";

import {
  gateActions,
  reviewConflict,
  reviewEdits,
  reviewFieldError,
  reviewFileUrl,
  reviewFiles,
  reviewFormField,
  reviewFormValues,
  reviewNumberProblems,
  reviewValueChanged,
  type JobReview,
  type ReviewField,
  type ReviewFile,
} from "./review-model";

/** One original file at a time, with its preview, and paging through several. */
export function ReviewOriginal({ jobId, files }: Readonly<{ jobId: string; files: readonly ReviewFile[] }>) {
  const { t } = useI18n();
  const [index, setIndex] = useState(0);
  if (!files.length) {
    return <p className="fs-empty-copy">{t("react.operator_review.no_original", "This review has no original file.")}</p>;
  }
  const at = Math.min(index, files.length - 1);
  const file = files[at];
  const href = reviewFileUrl(jobId, file);
  const kind = filePreviewKind(file);
  const preview = kind ? filePreviewUrl(href) : "";
  const name = file.filename || t("react.operator_review.original", "Original");
  return (
    <div className="fs-operator-original" data-testid="review-original">
      {files.length > 1 ? (
        <div className="fs-operator-original-pager">
          <button
            aria-label={t("react.operator_review.previous_file", "Previous file")}
            className="fs-icon-button"
            disabled={at === 0}
            onClick={() => setIndex(at - 1)}
            type="button"
          >
            <ChevronLeft aria-hidden="true" />
          </button>
          <span>{t("react.operator_review.file_position", "File {position} of {total}", { position: String(at + 1), total: String(files.length) })}</span>
          <button
            aria-label={t("react.operator_review.next_file", "Next file")}
            className="fs-icon-button"
            disabled={at >= files.length - 1}
            onClick={() => setIndex(at + 1)}
            type="button"
          >
            <ChevronRight aria-hidden="true" />
          </button>
        </div>
      ) : null}
      <JobFileItem file={{ ...file, download_url: href }} fallbackName={name} />
      {kind === "image" && preview ? <img alt={name} className="fs-operator-original-image" src={preview} /> : null}
      {kind === "pdf" && preview ? <iframe className="fs-operator-original-frame" src={preview} title={name} /> : null}
    </div>
  );
}

function shownValue(value: unknown, t: ReturnType<typeof useI18n>["t"]): string {
  if (value === undefined || value === null || value === "") return t("react.operator_review.empty_value", "empty");
  if (typeof value === "boolean") return value ? t("react.operator.filter_yes", "Yes") : t("react.operator.filter_no", "No");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** A field the form cannot edit (a file, a list of plain values): its value, read-only. */
function ReadOnlyReviewValue({ field, value, jobId }: Readonly<{ field: ReviewField; value: unknown; jobId: string }>) {
  const files = field.type === "file" || (field.type === "array" && (field.items as { type?: string } | null)?.type === "file") ? reviewFiles(value) : [];
  return (
    <div className="fs-field fs-workflow-field" data-field={field.name}>
      <span className="fs-field-label">{field.label || field.name}</span>
      {files.length ? (
        <div className="grid gap-2">
          {files.map((file, index) => (
            <JobFileItem file={{ ...file, download_url: reviewFileUrl(jobId, file) }} key={`${file.artifact_id}-${index}`} />
          ))}
        </div>
      ) : (
        <ReadableDataFields value={{ [field.label || field.name]: value }} />
      )}
    </div>
  );
}

type Decided = { approved: boolean; status: string; actor: string };

/**
 * The review of an Approval step with fields (ADR 0075): the original on the
 * left, the fields on the right with the default widget of their type. An
 * edited field says it was corrected and what it held; a refused value is
 * shown at its field. Who may only read sees the same page without inputs or
 * buttons. Ctrl+Enter (Cmd+Enter) approves and opens the next job.
 */
export function OperatorReviewPanel({
  review,
  onApprovedNext,
  onSkip,
  onDecided,
}: Readonly<{
  review: JobReview;
  /** After "Approve and next": open the next job of the list. */
  onApprovedNext: () => void;
  /** Leave this one undecided and open the next job. */
  onSkip?: () => void;
  onDecided?: () => void;
}>) {
  const { t } = useI18n();
  const gate = review.gate;
  const captured = gate?.review;
  const reviewJobId = gate?.job_id ?? "";
  const payload = gate?.payload ?? {};
  const canDecide = review.access === "decide";
  // Only the decisions the gate allows, as the existing review panel does.
  const actions = gateActions(gate);
  const canApprove = canDecide && actions.has("approve");
  const canReject = canDecide && actions.has("reject");
  const fields = useMemo(() => captured?.fields ?? [], [captured]);
  const capturedValues = useMemo(() => captured?.values ?? {}, [captured]);
  // Lists of plain values are edited as rows; the decision sends them back as lists.
  const initial = useMemo(() => reviewFormValues(fields, capturedValues), [fields, capturedValues]);
  const formFields = useMemo(() => {
    const shown: FormFieldView[] = [];
    const other: ReviewField[] = [];
    for (const field of fields) {
      const form = reviewFormField(field);
      if (form) shown.push(form);
      else other.push(field);
    }
    return { shown, other };
  }, [fields]);
  const readOnly = useMemo(() => new Set(fields.filter((field) => !field.editable).map((field) => field.name)), [fields]);
  const [values, setValues] = useState<FormValues>(initial);
  const [comment, setComment] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState("");
  const [pending, setPending] = useState<"approve" | "approve_next" | "reject" | null>(null);
  const [decided, setDecided] = useState<Decided | null>(null);

  const approveLabel = String(payload.approve_label || "") || t("jobs.actions.approve", "Approve");
  const rejectLabel = String(payload.reject_label || "") || t("jobs.detail.review.reject", "Reject");
  const message = String(payload.message || "");

  async function decide(mode: "approve" | "approve_next" | "reject") {
    if (!canDecide || pending || decided || !gate) return;
    setFormError("");
    if (!gate.thread_id || !gate.approval_id) {
      setFormError(t("react.jobs.this_approval_gate_has_no_approval_id_recorded_refresh_before_deciding", "This approval gate has no approval ID recorded. Refresh before deciding."));
      return;
    }
    const approved = mode !== "reject";
    if (approved ? !canApprove : !canReject) return;
    if (!approved && !comment.trim()) {
      setFormError(t("react.jobs.rejection_comment_required", "A rejection comment is required for this review gate."));
      return;
    }
    if (approved) {
      const numbers = reviewNumberProblems(fields, initial, values);
      if (numbers.length) {
        const errors: Record<string, string> = {};
        for (const entry of numbers) {
          if (entry.code === "row_invalid_number") {
            const sub = formFields.shown.find((field) => field.name === entry.field)?.sub_fields?.find((item) => item.name === entry.subField);
            errors[entry.field] ??= t("react.operator.row_field_invalid_number", "Enter a number in {field} in row {row}.", { field: sub?.label ?? entry.subField, row: String(entry.row) });
          } else {
            errors[entry.field] ??= t("react.operator.field_invalid_number", "Enter a number.");
          }
        }
        setFieldErrors(errors);
        return;
      }
    }
    const decision: ApprovalDecisionPayload = { approval_id: gate.approval_id, approved, comment: comment.trim() };
    const edits = approved ? reviewEdits(fields, initial, values) : {};
    if (Object.keys(edits).length) decision.values = edits;
    setPending(mode);
    setFieldErrors({});
    try {
      const result = await submitApprovalDecision(gate.thread_id, decision);
      if (mode === "approve_next") {
        onApprovedNext();
        return;
      }
      setDecided({ approved, status: String(result.status ?? ""), actor: String(result.actor ?? "") });
      onDecided?.();
    } catch (error) {
      const refused = reviewFieldError(error);
      if (refused) {
        // The person stays on this job with the field named.
        setFieldErrors({ [refused.field]: refused.message });
      } else if (reviewConflict(error)) {
        setFormError(t("react.operator_review.already_decided", "This review was already decided elsewhere. Open the next job."));
      } else {
        setFormError(error instanceof Error ? error.message : t("react.jobs.approval_request_failed", "Approval request failed."));
      }
    } finally {
      setPending(null);
    }
  }

  const decideRef = useRef(decide);
  useEffect(() => {
    decideRef.current = decide;
  });
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.defaultPrevented) return;
      event.preventDefault();
      ignoreAsyncResult(decideRef.current("approve_next"));
    };
    globalThis.addEventListener("keydown", onKey);
    return () => globalThis.removeEventListener("keydown", onKey);
  }, []);

  if (!gate || !captured) return null;

  const notes: Record<string, ReactNode> = {};
  for (const field of fields) {
    if (field.editable && reviewValueChanged(initial[field.name], values[field.name])) {
      notes[field.name] = (
        <span className="fs-operator-corrected" data-testid={`review-corrected-${field.name}`}>
          <DsBadge tone="orange">{t("react.operator_review.corrected", "Corrected")}</DsBadge>{" "}
          {t("react.operator_review.was", "Was: {value}", { value: shownValue(capturedValues[field.name], t) })}
        </span>
      );
    }
  }
  const errors = { ...captured.invalid, ...fieldErrors };
  const locked = !canDecide || Boolean(decided) || Boolean(pending);

  return (
    <div className="fs-operator-review" data-testid="operator-review">
      <DsCard className="min-w-0" title={t("react.operator_review.original", "Original")}>
        <ReviewOriginal files={captured.original} jobId={reviewJobId} />
      </DsCard>
      <DsCard className="min-w-0" description={message || undefined} title={t("react.operator_review.fields", "Check and correct")}>
        <div className="grid gap-4">
          {decided ? (
            <DsAlert
              title={decided.approved ? t("react.operator_review.approved", "Approved with these values") : t("react.operator_review.rejected", "Rejected")}
              tone="success"
            >
              <p>
                {t("react.operator_review.decided_status", "Run status: {status}", { status: decided.status || "—" })}
                {decided.actor ? ` · ${t("react.operator_review.decided_by", "Decided by {actor}", { actor: decided.actor })}` : ""}
              </p>
            </DsAlert>
          ) : null}
          <WorkflowInputFields
            disabled={locked}
            errors={errors}
            fields={formFields.shown}
            notes={notes}
            onChange={(name, next) => {
              setValues((current) => ({ ...current, [name]: next }));
              setFieldErrors((current) => {
                if (!(name in current)) return current;
                const rest = { ...current };
                delete rest[name];
                return rest;
              });
            }}
            readOnlyFields={readOnly}
            values={values}
          />
          {formFields.other.map((field) => (
            <ReadOnlyReviewValue field={field} jobId={reviewJobId} key={field.name} value={values[field.name]} />
          ))}
          {canDecide && !decided ? (
            <>
              <label className="fs-field min-w-0">
                <span className="fs-field-label">{t("jobs.detail.review.comment", "Comment")}</span>
                <textarea
                  className="fs-textarea"
                  data-testid="review-comment"
                  disabled={Boolean(pending)}
                  onChange={(event) => setComment(event.target.value)}
                  value={comment}
                />
                <span className="fs-field-helper">
                  {t("jobs.detail.review.comment_rejection_requirement", "Required when rejecting. Optional when approving.")}
                </span>
              </label>
              {formError ? (
                <div className="fs-field-helper error" role="alert">
                  {formError}
                </div>
              ) : null}
              <div className="flex flex-wrap gap-2">
                {canApprove ? (
                  <>
                  <Button
                    aria-keyshortcuts="Control+Enter Meta+Enter"
                    busy={pending === "approve_next"}
                    busyLabel={t("react.projects.secret_approval.approving", "Approving…")}
                    data-testid="review-approve-next"
                    onClick={() => ignoreAsyncResult(decide("approve_next"))}
                    type="button"
                    variant="primary"
                  >
                    {t("react.operator_review.approve_and_next", "{label} and next", { label: approveLabel })}
                  </Button>
                  <Button
                    busy={pending === "approve"}
                    busyLabel={t("react.projects.secret_approval.approving", "Approving…")}
                    data-testid="review-approve"
                    onClick={() => ignoreAsyncResult(decide("approve"))}
                    type="button"
                    variant="secondary"
                  >
                    {approveLabel}
                  </Button>
                  </>
                ) : null}
                {canReject ? (
                  <Button
                    busy={pending === "reject"}
                    busyLabel={t("react.jobs.rejecting", "Rejecting…")}
                    data-testid="review-reject"
                    onClick={() => ignoreAsyncResult(decide("reject"))}
                    type="button"
                    variant="danger"
                  >
                    {rejectLabel}
                  </Button>
                ) : null}
                {onSkip ? (
                  <Button data-testid="review-skip" onClick={onSkip} type="button" variant="secondary">
                    {t("react.operator_review.skip", "Skip")}
                  </Button>
                ) : null}
              </div>
            </>
          ) : null}
          {!canDecide ? (
            <p className="fs-field-helper">
              {t("react.jobs.viewer_mode_is_read_only_an_operator_or_admin_has_to_decide_this_gate", "Viewer mode is read-only. An operator or admin has to decide this gate.")}
            </p>
          ) : null}
        </div>
      </DsCard>
    </div>
  );
}
