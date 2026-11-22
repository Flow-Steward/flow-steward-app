import { RotateCcw } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { DsAlert, DsBadge, DsCard } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { isFileField, optionLabel, type FormFieldView, type FormFiles, type FormValues } from "@/components/workflow-inputs/form-field";
import { WorkflowInputFields } from "@/components/workflow-inputs/workflow-input-fields";
import { useHasCapability } from "@/lib/auth/capabilities";
import { fetchArrayBuffer } from "@/lib/http/client";
import { useI18n } from "@/lib/i18n/useI18n";
import { formatDateTime } from "@/lib/time/date-time-format";
import { renderMarkdownBlocks } from "@/pages/extensions/extension-markdown";
import { formatStepDuration } from "@/pages/jobs/detail/job-detail-execution";
import { JobFileItem } from "@/pages/jobs/detail/job-detail-file-preview";

import { OperatorPageHead, OperatorPageShell } from "./operator-page-shell";
import { ReviewOriginal } from "./review-panel";
import { reviewFileUrl, reviewFiles, reviewFormField, reviewFormValues, type JobReview, type ReviewField, type ReviewFile } from "./review-model";
import { OperatorCell } from "./section-cell";
import { OperatorSubmitDialog, type SubmissionPrefill } from "./submit-dialog";
import type { OperatorListColumn, OperatorResultFieldConfig, OperatorReviewDecision, OperatorSectionDetail } from "./types";

type Row = { label: string; value: ReactNode; name: string };

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/** A value as one cell of the section's list shows it: files as download links. */
function ValueCell({
  jobId,
  source,
  name,
  value,
  format,
  decimals,
}: Readonly<{ jobId: string; source: OperatorListColumn["source"]; name: string; value: unknown; format?: string; decimals?: number | null }>) {
  const column: OperatorListColumn = { id: `${source}.${name}`, source, path: name, format };
  return <OperatorCell column={column} decimals={decimals} row={{ job_id: jobId, operator_values: { [column.id]: value } }} />;
}

/** Formatted text (WYSIWYG) through the Core Markdown renderer: React elements, never raw HTML. */
function FormattedText({ text }: Readonly<{ text: string }>) {
  return (
    <div className="fs-workflow-markdown-preview" data-testid="operator-formatted-text">
      {renderMarkdownBlocks(text)}
    </div>
  );
}

/**
 * An object or a list of objects shown read-only with the form's own layout:
 * a group, or the repeater table with one column per item property, scrolling
 * inside its card on a narrow screen.
 */
function StructuredValue({ field, value }: Readonly<{ field: FormFieldView; value: unknown }>) {
  return (
    <div className="fs-operator-readonly-form" data-testid={`operator-structured-${field.name}`}>
      <WorkflowInputFields disabled fields={[field]} onChange={() => undefined} values={{ [field.name]: value }} />
    </div>
  );
}

/** Files only: one file, or a list where every item is a stored file. */
function filesOf(value: unknown): ReviewFile[] {
  const files = reviewFiles(value);
  const count = Array.isArray(value) ? value.length : 1;
  return files.length && files.length === count ? files : [];
}

/** Result files with their name, size, preview on demand and download, as Job Details shows them. */
function ResultFiles({ jobId, files }: Readonly<{ jobId: string; files: ReviewFile[] }>) {
  return (
    <div className="grid gap-2">
      {files.map((file, index) => (
        <JobFileItem file={{ ...file, download_url: reviewFileUrl(jobId, file) }} key={`${file.artifact_id}-${index}`} />
      ))}
    </div>
  );
}

function isStructured(field: FormFieldView, value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (field.kind === "object_list") return Array.isArray(value);
  return field.kind === "object" && typeof value === "object" && !Array.isArray(value);
}

/** A contract JSON Schema node as the form's field model, to show a value by its type. */
function schemaFormField(name: string, label: string, node: unknown): FormFieldView | null {
  const schema = record(node);
  const type = String(schema.type ?? "");
  const subFields = (properties: unknown) =>
    Object.entries(record(properties))
      .map(([key, sub]) => schemaFormField(key, String(record(sub).title ?? "") || key, sub))
      .filter((sub): sub is FormFieldView => sub !== null);
  if (type === "object") {
    const sub_fields = subFields(schema.properties);
    return sub_fields.length ? { name, label, kind: "object", widget: "group", sub_fields } : null;
  }
  if (type === "array" && record(schema.items).type === "object") {
    const sub_fields = subFields(record(schema.items).properties);
    return sub_fields.length ? { name, label, kind: "object_list", widget: "repeater", layout: "table", sub_fields } : null;
  }
  if (type === "number" || type === "integer") return { name, label, kind: "number", widget: "number" };
  if (type === "boolean") return { name, label, kind: "boolean", widget: "true_false" };
  if (type === "string") return { name, label, kind: "string", widget: "text" };
  return null;
}

function ValueList({ rows, testId }: Readonly<{ rows: Row[]; testId: string }>) {
  return (
    <dl className="fs-operator-values" data-testid={testId}>
      {rows.map((row) => (
        <div className="fs-operator-value-row" data-value={row.name} key={row.name}>
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A submitted value with the form's options shown by their labels. */
function shownInput(field: FormFieldView, value: unknown): unknown {
  if (field.kind === "select" && typeof value === "string") return optionLabel(field, value);
  if (field.kind === "select_list" && Array.isArray(value)) return value.map((item) => optionLabel(field, String(item)));
  return value;
}

/** A submitted value by its widget: formatted text, a group or table, else as the list shows it. */
function SubmittedValue({ field, value, jobId }: Readonly<{ field: FormFieldView; value: unknown; jobId: string }>) {
  if (field.widget === "wysiwyg" && typeof value === "string") return <FormattedText text={value} />;
  if (isStructured(field, value)) return <StructuredValue field={field} value={value} />;
  return <ValueCell jobId={jobId} name={field.name} source="input" value={shownInput(field, value)} />;
}

const RESULT_FORMATS: Record<string, string> = {
  text: "text",
  wysiwyg: "text",
  number: "number",
  date: "date",
  datetime: "datetime",
  boolean: "boolean",
};

/** Result outputs in the configured order, then the contract's, without hidden ones. */
function resultRows(section: OperatorSectionDetail, outputs: Record<string, unknown>, jobId: string): Row[] {
  const result = section.operator.settings?.result;
  const configs: Record<string, OperatorResultFieldConfig> = result?.fields ?? {};
  const properties = record(record(section.operator.output_schema).properties);
  const names = [...(result?.order ?? []), ...Object.keys(properties), ...Object.keys(outputs)];
  const seen = new Set<string>();
  const rows: Row[] = [];
  for (const name of names) {
    if (seen.has(name)) continue;
    seen.add(name);
    const config = configs[name];
    const value = outputs[name];
    if (config?.hidden || value === undefined || value === null) continue;
    const label = config?.label || String(record(properties[name]).title ?? "") || name;
    const structure = schemaFormField(name, label, properties[name]);
    const files = filesOf(value);
    const formatted = config?.display === "wysiwyg" && typeof value === "string";
    let shown: ReactNode;
    if (formatted) {
      shown = <FormattedText text={value} />;
    } else if (files.length) {
      shown = <ResultFiles files={files} jobId={jobId} />;
    } else if (structure && isStructured(structure, value)) {
      shown = <StructuredValue field={structure} value={value} />;
    } else {
      shown = <ValueCell decimals={config?.decimals ?? null} format={RESULT_FORMATS[config?.display ?? ""] ?? "auto"} jobId={jobId} name={name} source="output" value={value} />;
    }
    rows.push({ name, label, value: shown });
  }
  return rows;
}

/**
 * "Send again": the job's visible values and its uploaded files, downloaded
 * from the job, so the operator corrects what is needed and sends a new run
 * without choosing the files again. A file that cannot be read is left out
 * and named, with how many files its input had: the form does not send a
 * shorter set until the operator adds them again or confirms sending without.
 * An aborted load (the operator left the job) rejects instead of counting.
 */
async function resendPrefill(
  fields: readonly FormFieldView[],
  inputs: Record<string, unknown>,
  jobId: string,
  signal: AbortSignal,
): Promise<SubmissionPrefill> {
  const values: FormValues = {};
  const files: FormFiles = {};
  const missing: NonNullable<SubmissionPrefill["missing"]> = [];
  const expected: Record<string, number> = {};
  for (const field of fields) {
    const value = inputs[field.name];
    if (field.hidden || field.fixed || value === undefined || value === null) continue;
    if (!isFileField(field)) {
      values[field.name] = value;
      continue;
    }
    const stored = reviewFiles(value);
    expected[field.name] = stored.length;
    const read = await Promise.all(
      stored.map(async (file) => {
        try {
          const body = await fetchArrayBuffer(reviewFileUrl(jobId, file), { signal });
          return new File([body], file.filename || "file", { type: file.content_type || "application/octet-stream" });
        } catch (error) {
          if (signal.aborted) throw error;
          missing.push({ field: field.name, label: field.label || field.name, filename: file.filename || "" });
          return null;
        }
      }),
    );
    const kept = read.filter((file): file is File => file !== null);
    if (kept.length) files[field.name] = kept;
  }
  return { values, files, missing, expected };
}

function ReviewDecisionCard({ decision }: Readonly<{ decision: OperatorReviewDecision }>) {
  const { t } = useI18n();
  if (decision.access !== "read") {
    return (
      <DsCard testId="operator-job-review" title={t("react.operator_job.review", "Review")}>
        <p className="fs-field-helper">{t("react.operator_job.review_hidden", "This job was reviewed by people who have access to it.")}</p>
      </DsCard>
    );
  }
  const holder = decision.job_id ?? "";
  const changed = new Map((decision.changes ?? []).map((change) => [change.field, change]));
  const fields = decision.fields ?? [];
  const labelOf = (name: string) => fields.find((field) => field.name === name)?.label || name;
  const values = decision.values ?? {};
  const reviewFields: ReviewField[] = fields;
  // Lists of plain values are rows in the form's table, as on the review page.
  const formValues = reviewFormValues(reviewFields, values);
  const rows: Row[] = reviewFields
    .filter((field) => field.name in values)
    .map((field) => {
      const change = changed.get(field.name);
      const structure = reviewFormField(field);
      const value = structure && isStructured(structure, formValues[field.name]) ? (
        <StructuredValue field={structure} value={formValues[field.name]} />
      ) : (
        <ValueCell jobId={holder} name={field.name} source="output" value={values[field.name]} />
      );
      return {
        name: field.name,
        label: field.label || field.name,
        value: (
          <span className="fs-operator-decided-value">
            {value}
            {change ? (
              <span className="fs-operator-corrected" data-testid={`decision-corrected-${field.name}`}>
                <DsBadge tone="orange">{t("react.operator_review.corrected", "Corrected")}</DsBadge>
                <span>{t("react.operator_review.was", "Was: {value}", { value: change.before === null || change.before === undefined ? "—" : typeof change.before === "object" ? JSON.stringify(change.before) : String(change.before) })}</span>
              </span>
            ) : null}
          </span>
        ),
      };
    });
  const who = decision.actor || "—";
  const when = decision.timestamp ? formatDateTime(decision.timestamp) : "";
  return (
    <DsCard
      description={
        decision.approved
          ? t("react.operator_job.review_approved_by", "Approved by {actor} · {when}", { actor: who, when })
          : t("react.operator_job.review_rejected_by", "Rejected by {actor} · {when}", { actor: who, when })
      }
      testId="operator-job-review"
      title={t("react.operator_job.review", "Review")}
    >
      {rows.length ? <ValueList rows={rows} testId="operator-job-review-values" /> : null}
      {changed.size ? (
        <ul className="text-sm" aria-label={t("react.operator_job.review_changes", "Corrected in the review")} data-testid="operator-job-review-changes">
          {[...changed.values()].map((change) => (
            <li key={change.field}>
              {labelOf(change.field)}: <ValueCell jobId={holder} name={change.field} source="output" value={change.before ?? ""} /> →{" "}
              <ValueCell jobId={holder} name={change.field} source="output" value={change.after ?? ""} />
            </li>
          ))}
        </ul>
      ) : null}
      {decision.comment ? (
        <p className={decision.approved ? "fs-field-helper" : undefined} data-testid="operator-job-review-comment">
          {decision.approved ? decision.comment : t("react.operator_job.reject_reason", "Reason: {comment}", { comment: decision.comment })}
        </p>
      ) : null}
    </DsCard>
  );
}

/**
 * A job of a section as operators and viewers see it (ADR 0075), in the same
 * layout as its review: the uploaded files on the left, and on the right the
 * result as the admin configured it once the run succeeded, the decided
 * review (here or on the Invoke child that held it) and the other submitted
 * values. No step graph; an admin keeps a link to the full Job Details.
 */
export function OperatorJobView({
  section,
  jobId,
  review,
  listPath,
  steps,
  onResent,
}: Readonly<{
  section: OperatorSectionDetail;
  jobId: string;
  review: JobReview | undefined;
  /** The section's list this job was opened from. */
  listPath: string;
  /** The previous and next job of that list. */
  steps?: ReactNode;
  /** The jobs "Send again" started. */
  onResent?: (jobIds: string[]) => void;
}>) {
  const { t } = useI18n();
  const isAdmin = useHasCapability("administer_platform");
  const canRun = useHasCapability("run_workflows");
  const runnable = Boolean(section.runnable && section.operator.runnable);
  // The page keys this view by job, so its state never outlives its job; a
  // load still running when the operator leaves is aborted and its result ignored.
  const [resend, setResend] = useState<SubmissionPrefill | null>(null);
  const [preparing, setPreparing] = useState(false);
  const loading = useRef<AbortController | null>(null);
  useEffect(() => () => loading.current?.abort(), []);
  const values = review?.job_values ?? null;
  const status = values?.status || review?.status || "";
  const inputs = values?.inputs ?? {};
  const outputs = values?.outputs ?? null;
  const shownFields = section.operator.form_fields.filter(
    (field) => !field.hidden && !field.fixed && inputs[field.name] !== undefined && inputs[field.name] !== null,
  );
  // Every uploaded file in one previewer, paged; its fields leave the value list.
  const originals = shownFields.filter(isFileField).flatMap((field) => reviewFiles(inputs[field.name]));
  const submitted: Row[] = shownFields
    .filter((field) => !(isFileField(field) && filesOf(inputs[field.name]).length))
    .map((field) => ({
      name: field.name,
      label: field.label || field.name,
      value: <SubmittedValue field={field} jobId={jobId} value={inputs[field.name]} />,
    }));
  const result = outputs ? resultRows(section, outputs, jobId) : [];
  const by = record(values?.submitted_by);
  const byName = String(by.name ?? by.display_name ?? by.id ?? "");
  const author = byName && by.kind === "service_account" ? t("react.operator.author_api", "{name} (API)", { name: byName }) : byName;
  // The header names the job by what was sent, as the list does: its first file.
  const firstName = originals[0]?.filename ?? "";
  let title = t("react.operator_job.title", "Job");
  if (firstName && originals.length > 1) {
    title = t("react.operator_job.title_more", "{name} and {count} more", { name: firstName, count: String(originals.length - 1) });
  } else if (firstName) {
    title = firstName;
  }
  const meta = [values?.created_at ? formatDateTime(values.created_at) : "", author, formatStepDuration(values?.duration_ms)].filter(Boolean).join(" · ");
  const startResend = async () => {
    loading.current?.abort();
    const controller = new AbortController();
    loading.current = controller;
    setPreparing(true);
    try {
      const prepared = await resendPrefill(section.operator.form_fields, inputs, jobId, controller.signal);
      if (!controller.signal.aborted) setResend(prepared);
    } catch {
      // Aborted: the operator left this job; nothing opens.
    } finally {
      if (loading.current === controller) {
        loading.current = null;
        setPreparing(false);
      }
    }
  };
  return (
    <>
      <OperatorPageShell
        actions={
          <>
            <Link className="fs-button secondary" to={listPath}>
              {t("react.operator_queue.back", "Back to {title}", { title: section.title })}
            </Link>
            {isAdmin ? (
              <Link className="fs-button secondary" data-testid="operator-job-details-link" to={`/jobs/${encodeURIComponent(jobId)}`}>
                {t("react.operator_job.full_details", "Full job details")}
              </Link>
            ) : null}
            {canRun && runnable ? (
              <Button
                busy={preparing}
                busyLabel={t("react.operator_job.loading_files", "Loading the files…")}
                data-testid="operator-job-send-again"
                onClick={() => void startResend()}
                type="button"
                variant="secondary"
              >
                <RotateCcw aria-hidden="true" />
                {t("react.operator_job.send_again", "Send again")}
              </Button>
            ) : null}
          </>
        }
        head={
          <OperatorPageHead
            status={
              <span data-testid="operator-job-status">
                <ValueCell jobId={jobId} name="status" source="job" value={status} />
              </span>
            }
            steps={steps}
            sub={meta ? <span data-testid="operator-job-meta">{meta}</span> : null}
            testId="operator-job-head"
            title={title}
          />
        }
        original={originals.length ? <ReviewOriginal files={originals} jobId={jobId} layout="pane" /> : null}
        originalLabel={t("react.operator_review.original", "Original")}
        panelLabel={t("react.operator_job.title", "Job")}
        testId="operator-job"
      >
        {review?.waiting && review.access === "none" ? (
          <DsAlert title={t("react.operator_review.waiting_elsewhere", "This job waits for a decision by people who have access to it.")} tone="neutral" />
        ) : null}
        {outputs ? (
          <DsCard className="min-w-0" testId="operator-job-result" title={t("react.operator_job.result", "Result")}>
            {result.length ? (
              <ValueList rows={result} testId="operator-job-outputs" />
            ) : (
              <p className="fs-empty-copy">{t("react.operator_job.no_result", "This run returned nothing to show.")}</p>
            )}
          </DsCard>
        ) : (
          <DsCard className="min-w-0" testId="operator-job-result-pending" title={t("react.operator_job.result", "Result")}>
            <p className="fs-empty-copy">{t("react.operator_job.result_pending", "The result appears here when the run finishes.")}</p>
          </DsCard>
        )}
        {review?.decision ? <ReviewDecisionCard decision={review.decision} /> : null}
        {submitted.length || !originals.length ? (
          <DsCard className="min-w-0" testId="operator-job-submitted" title={t("react.operator_job.submitted", "Submitted")}>
            {submitted.length ? (
              <ValueList rows={submitted} testId="operator-job-inputs" />
            ) : (
              <p className="fs-empty-copy">{t("react.operator_job.nothing_submitted", "No values were submitted.")}</p>
            )}
          </DsCard>
        ) : null}
      </OperatorPageShell>
      {resend ? (
        <OperatorSubmitDialog
          onClose={() => setResend(null)}
          onSent={(jobIds) => onResent?.(jobIds)}
          open
          prefill={resend}
          section={section}
        />
      ) : null}
    </>
  );
}
