/**
 * The review of an Approval step with fields (ADR 0075): what the server sends,
 * how each field is shown (the default widget of its type) and which edits a
 * decision sends.
 */
import type { FormFieldView, FormProblem, FormValues } from "@/components/workflow-inputs/form-field";
import { formProblems, parseLocaleNumber } from "@/components/workflow-inputs/form-field";
import { isHttpError } from "@/lib/http/errors";

import type { OperatorJobValues, OperatorReviewDecision } from "./types";

/** One field of a captured review: the workflow contract field with its editability. */
export type ReviewField = {
  name: string;
  label?: string;
  type?: string;
  required?: boolean;
  editable?: boolean;
  options?: string[];
  items?: Record<string, unknown> | null;
  properties?: unknown;
  min_items?: number | null;
  max_items?: number | null;
  format?: string | null;
  description?: string;
};

export type ReviewFile = { artifact_id?: string; filename?: string; content_type?: string; size_bytes?: number };

export type ApprovalReview = {
  fields: ReviewField[];
  values: Record<string, unknown>;
  invalid: Record<string, string>;
  original: ReviewFile[];
};

export type ReviewGate = {
  job_id: string;
  thread_id?: string;
  approval_id?: string | null;
  allowed_actions?: string[];
  payload?: Record<string, unknown>;
  review?: ApprovalReview | null;
  /** Whether this person may decide (ADR 0082, assigned approvers); absent means true. */
  can_decide?: boolean;
  /** The step's assigned approvers; empty when anyone who may decide approvals may. */
  approvers?: Array<{ username?: string; display_name?: string }>;
};

/** `GET /api/v2/jobs/{id}/review`: the approval this job waits for and the caller's access. */
export type JobReview = {
  job_id: string;
  status: string;
  waiting: boolean;
  review_job_id: string | null;
  access: "decide" | "read" | "none" | null;
  gate: ReviewGate | null;
  /** What the job submitted and, once it succeeded, returned. */
  job_values?: OperatorJobValues | null;
  /** The last decided typed review of the job or of the Invoke child that held it. */
  decision?: OperatorReviewDecision | null;
};

/** Up to this many choices a review field shows them all as radio buttons. */
export const RADIO_CHOICES = 4;

const STRING_WIDGETS: Record<string, string> = {
  date: "date",
  "date-time": "date_time",
  time: "time",
  email: "email",
  uri: "url",
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/** Nested fields (`properties` of an object, of the items of a list) as review fields. */
function nestedFields(value: unknown): ReviewField[] {
  if (Array.isArray(value)) {
    return value.filter((item): item is ReviewField => Boolean(item) && typeof record(item).name === "string");
  }
  return Object.entries(record(value)).map(([name, node]) => ({ name, ...record(node) }) as ReviewField);
}

/**
 * A review field as the operator form's field model, with the default widget of
 * its type (ADR "Показ: виджет по типу"). A file or a list the form cannot edit
 * (for example a list of plain strings) has no form field: the page shows its
 * value read-only instead.
 */
export function reviewFormField(field: ReviewField): FormFieldView | null {
  const type = String(field.type || "paragraph");
  const base = {
    name: field.name,
    label: field.label || field.name,
    instructions: field.description || undefined,
    required: Boolean(field.required),
    options: field.options ?? [],
    min_items: field.min_items ?? null,
    max_items: field.max_items ?? null,
    format: field.format ?? undefined,
  };
  switch (type) {
    case "string":
      return { ...base, kind: "string", widget: STRING_WIDGETS[String(field.format || "")] ?? "text" };
    case "paragraph":
      return { ...base, kind: "string", widget: "textarea" };
    case "number":
      return { ...base, kind: "number", widget: "number" };
    case "boolean":
      return { ...base, kind: "boolean", widget: "true_false" };
    case "select":
      // A few choices are seen at once as radio buttons; a longer list stays a dropdown.
      return { ...base, kind: "select", widget: base.options.length && base.options.length <= RADIO_CHOICES ? "radio" : "select" };
    case "object": {
      const subFields = nestedFields(field.properties).map(reviewFormField).filter((item): item is FormFieldView => item !== null);
      return subFields.length ? { ...base, kind: "object", widget: "group", sub_fields: subFields } : null;
    }
    case "array": {
      const items = record(field.items);
      const itemType = String(items.type || "");
      const scalar = SCALAR_LIST_ITEMS[itemType];
      if (scalar) {
        // A list of plain values is edited as a one-column repeater; the page
        // turns each value into a row and back (reviewFormValues, reviewEdits).
        return {
          ...base,
          kind: "object_list",
          widget: "repeater",
          layout: "table",
          sub_fields: [{ name: LIST_ITEM_KEY, label: base.label, kind: scalar.kind, widget: scalar.widget }],
        };
      }
      if (itemType === "select") {
        const options = Array.isArray(items.options) ? items.options.map(String) : base.options;
        return { ...base, options, kind: "select_list", widget: "multiselect" };
      }
      if (itemType === "object") {
        const subFields = nestedFields(items.properties).map(reviewFormField).filter((item): item is FormFieldView => item !== null);
        return subFields.length ? { ...base, kind: "object_list", widget: "repeater", layout: "table", sub_fields: subFields } : null;
      }
      return null;
    }
    default:
      return null;
  }
}

/** The one sub-field of a list of plain values edited as rows. */
export const LIST_ITEM_KEY = "value";

/** Plain list items the review page edits, with the default widget of their type. */
const SCALAR_LIST_ITEMS: Record<string, { kind: "string" | "number"; widget: string }> = {
  string: { kind: "string", widget: "text" },
  paragraph: { kind: "string", widget: "textarea" },
  number: { kind: "number", widget: "number" },
};

function scalarList(field: ReviewField): boolean {
  return field.type === "array" && Boolean(SCALAR_LIST_ITEMS[String(record(field.items).type || "")]);
}

/** One value as the form holds it: a list of plain values as rows, at any depth. */
function toFormValue(field: ReviewField, value: unknown): unknown {
  if (scalarList(field)) return Array.isArray(value) ? value.map((item) => ({ [LIST_ITEM_KEY]: item })) : value;
  if (field.type === "object" && value && typeof value === "object" && !Array.isArray(value)) {
    return toFormRecord(nestedFields(field.properties), record(value));
  }
  if (field.type === "array" && String(record(field.items).type || "") === "object" && Array.isArray(value)) {
    const properties = nestedFields(record(field.items).properties);
    return value.map((item) => (item && typeof item === "object" && !Array.isArray(item) ? toFormRecord(properties, record(item)) : item));
  }
  return value;
}

/** The form's value back as the review field's JSON value. */
function fromFormValue(field: ReviewField, value: unknown): unknown {
  if (scalarList(field)) {
    if (!Array.isArray(value)) return value;
    const numeric = String(record(field.items).type || "") === "number";
    return value
      .map((row) => record(row)[LIST_ITEM_KEY])
      .filter((item) => item !== undefined && item !== null && item !== "")
      .map((item) => (numeric && typeof item === "string" ? (parseLocaleNumber(item) ?? item) : item));
  }
  if (field.type === "object" && value && typeof value === "object" && !Array.isArray(value)) {
    return fromFormRecord(nestedFields(field.properties), record(value));
  }
  if (field.type === "array" && String(record(field.items).type || "") === "object" && Array.isArray(value)) {
    const properties = nestedFields(record(field.items).properties);
    return value.map((item) => (item && typeof item === "object" && !Array.isArray(item) ? fromFormRecord(properties, record(item)) : item));
  }
  return value;
}

function toFormRecord(fields: readonly ReviewField[], values: Record<string, unknown>): Record<string, unknown> {
  const next = { ...values };
  for (const field of fields) if (field.name in next) next[field.name] = toFormValue(field, next[field.name]);
  return next;
}

function fromFormRecord(fields: readonly ReviewField[], values: Record<string, unknown>): Record<string, unknown> {
  const next = { ...values };
  for (const field of fields) if (field.name in next) next[field.name] = fromFormValue(field, next[field.name]);
  return next;
}

/** The captured values as the review form holds them. */
export function reviewFormValues(fields: readonly ReviewField[], values: Readonly<Record<string, unknown>>): FormValues {
  return toFormRecord(fields, { ...values });
}

/** A file value of a review (`file` or a list of files) as file summaries. */
export function reviewFiles(value: unknown): ReviewFile[] {
  const items = Array.isArray(value) ? value : [value];
  return items.filter((item): item is ReviewFile => Boolean(record(item).artifact_id));
}

/** Download address of a file the job holding the approval owns. */
export function reviewFileUrl(jobId: string, file: ReviewFile): string {
  return `/api/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(String(file.artifact_id || ""))}`;
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value as object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stable((value as Record<string, unknown>)[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

/** Whether the person changed this value from the one the review opened with. */
export function reviewValueChanged(initial: unknown, current: unknown): boolean {
  const empty = (value: unknown) => value === undefined || value === null || value === "";
  if (empty(initial) && empty(current)) return false;
  return stable(initial) !== stable(current);
}

/** The JSON value a decision sends for one field: a number typed in the person's locale as a number. */
function sentValue(field: ReviewField, formValue: unknown): unknown {
  const value = fromFormValue(field, formValue);
  if (field.type === "number") {
    if (value === "" || value === undefined) return null;
    if (typeof value === "string") return parseLocaleNumber(value) ?? value;
  }
  return value === undefined ? null : value;
}

/**
 * The `values` of a decision: the edited fields only, and only editable ones.
 * Text that is not a number stays text, so the server refuses it naming the field.
 */
export function reviewEdits(fields: readonly ReviewField[], initial: FormValues, current: FormValues): Record<string, unknown> {
  const edits: Record<string, unknown> = {};
  for (const field of fields) {
    if (!field.editable) continue;
    if (reviewValueChanged(initial[field.name], current[field.name])) {
      edits[field.name] = sentValue(field, current[field.name]);
    }
  }
  return edits;
}

/**
 * Edited fields whose number text is not a number, nested ones included: the
 * page names them and sends no decision rather than dropping or sending text.
 */
export function reviewNumberProblems(fields: readonly ReviewField[], initial: FormValues, current: FormValues): FormProblem[] {
  const edited = fields
    .filter((field) => field.editable && reviewValueChanged(initial[field.name], current[field.name]))
    .map(reviewFormField)
    .filter((field): field is FormFieldView => field !== null);
  return formProblems(edited, current).filter((problem) => problem.code === "invalid_number" || problem.code === "row_invalid_number");
}

/** A refused decision that names one field (422 with `field`): shown at that field. */
export function reviewFieldError(error: unknown): { field: string; message: string } | null {
  if (!isHttpError(error) || error.status !== 422) return null;
  const payload = record(error.payload);
  const field = typeof payload.field === "string" ? payload.field : "";
  if (!field) return null;
  return { field, message: error.message };
}

/** A decision the gate no longer takes: decided elsewhere or replaced. */
export function reviewConflict(error: unknown): boolean {
  if (!isHttpError(error) || error.status !== 409) return false;
  const code = record(error.payload).code;
  return code === "approval_already_resolved" || code === "stale_approval_gate";
}

/** The decisions the review page itself offers; any other the existing review panel handles. */
const REVIEW_PAGE_ACTIONS = new Set(["approve", "reject"]);

/** The gate's allowed decisions, normalized. */
export function gateActions(gate: ReviewGate | null | undefined): Set<string> {
  return new Set((gate?.allowed_actions ?? []).map((action) => String(action).trim().toLowerCase()).filter(Boolean));
}

/**
 * Whether the review page can offer every decision this gate allows. A gate
 * that also offers override or abort (a recovery gate, an exhausted rework)
 * stays on the existing review panel, which knows those decisions.
 */
export function reviewPageDecides(gate: ReviewGate | null | undefined): boolean {
  return [...gateActions(gate)].every((action) => REVIEW_PAGE_ACTIONS.has(action));
}

