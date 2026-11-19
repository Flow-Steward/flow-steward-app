import type { ReactNode } from "react";
import { Link } from "react-router";

import { DsAlert, DsBadge, DsCard, DsPageHeader } from "@/components/shared/design-system";
import { optionLabel, type FormFieldView } from "@/components/workflow-inputs/form-field";
import { WorkflowInputFields } from "@/components/workflow-inputs/workflow-input-fields";
import { useHasCapability } from "@/lib/auth/capabilities";
import { useI18n } from "@/lib/i18n/useI18n";
import { formatDateTime } from "@/lib/time/date-time-format";
import { renderMarkdownBlocks } from "@/pages/extensions/extension-markdown";

import { reviewFormField, reviewFormValues, type JobReview, type ReviewField } from "./review-model";
import { OperatorCell } from "./section-cell";
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
    let shown: ReactNode;
    if (config?.display === "wysiwyg" && typeof value === "string") {
      shown = <FormattedText text={value} />;
    } else if (structure && isStructured(structure, value)) {
      shown = <StructuredValue field={structure} value={value} />;
    } else {
      shown = <ValueCell decimals={config?.decimals ?? null} format={RESULT_FORMATS[config?.display ?? ""] ?? "auto"} jobId={jobId} name={name} source="output" value={value} />;
    }
    rows.push({ name, label, value: shown });
  }
  return rows;
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
          <>
            {value}
            {change ? (
              <span className="fs-operator-corrected" data-testid={`decision-corrected-${field.name}`}>
                {" "}
                <DsBadge tone="orange">{t("react.operator_review.corrected", "Corrected")}</DsBadge>{" "}
                {t("react.operator_review.was", "Was: {value}", { value: change.before === null || change.before === undefined ? "—" : typeof change.before === "object" ? JSON.stringify(change.before) : String(change.before) })}
              </span>
            ) : null}
          </>
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
      {decision.comment ? <p className="fs-field-helper">{decision.comment}</p> : null}
    </DsCard>
  );
}

/**
 * A job of a section as operators and viewers see it (ADR 0075): what was
 * submitted with the form's labels, the result as the admin configured it once
 * the run succeeded, files as download links and the decided review (here or on
 * the Invoke child that held it). No step graph; an admin keeps a link to the
 * full Job Details.
 */
export function OperatorJobView({ section, jobId, review }: Readonly<{ section: OperatorSectionDetail; jobId: string; review: JobReview | undefined }>) {
  const { t } = useI18n();
  const isAdmin = useHasCapability("administer_platform");
  const values = review?.job_values ?? null;
  const status = values?.status || review?.status || "";
  const inputs = values?.inputs ?? {};
  const outputs = values?.outputs ?? null;
  const submitted: Row[] = section.operator.form_fields
    .filter((field) => !field.hidden && !field.fixed && inputs[field.name] !== undefined && inputs[field.name] !== null)
    .map((field) => ({
      name: field.name,
      label: field.label || field.name,
      value: <SubmittedValue field={field} jobId={jobId} value={inputs[field.name]} />,
    }));
  const result = outputs ? resultRows(section, outputs, jobId) : [];
  const by = record(values?.submitted_by);
  const byName = String(by.name ?? by.display_name ?? by.id ?? "");
  const author = byName && by.kind === "service_account" ? t("react.operator.author_api", "{name} (API)", { name: byName }) : byName;
  return (
    <>
      <DsPageHeader
        action={
          isAdmin ? (
            <Link className="fs-link" data-testid="operator-job-details-link" to={`/jobs/${encodeURIComponent(jobId)}`}>
              {t("react.operator_job.full_details", "Full job details")}
            </Link>
          ) : undefined
        }
        description={author ? t("react.operator_job.submitted_by", "Submitted by {name}", { name: author }) : ""}
        eyebrow={section.title}
        title={t("react.operator_job.title", "Job")}
      />
      <div className="grid gap-4" data-testid="operator-job">
        <div>
          <ValueCell jobId={jobId} name="status" source="job" value={status} />
        </div>
        {review?.waiting && review.access === "none" ? (
          <DsAlert title={t("react.operator_review.waiting_elsewhere", "This job waits for a decision by people who have access to it.")} tone="neutral" />
        ) : null}
        <DsCard testId="operator-job-submitted" title={t("react.operator_job.submitted", "Submitted")}>
          {submitted.length ? (
            <ValueList rows={submitted} testId="operator-job-inputs" />
          ) : (
            <p className="fs-empty-copy">{t("react.operator_job.nothing_submitted", "No values were submitted.")}</p>
          )}
        </DsCard>
        {review?.decision ? <ReviewDecisionCard decision={review.decision} /> : null}
        {outputs ? (
          <DsCard testId="operator-job-result" title={t("react.operator_job.result", "Result")}>
            {result.length ? (
              <ValueList rows={result} testId="operator-job-outputs" />
            ) : (
              <p className="fs-empty-copy">{t("react.operator_job.no_result", "This run returned nothing to show.")}</p>
            )}
          </DsCard>
        ) : null}
      </div>
    </>
  );
}
