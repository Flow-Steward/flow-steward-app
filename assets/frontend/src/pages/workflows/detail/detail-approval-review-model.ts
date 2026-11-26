/**
 * The review fields of an Approval step (ADR 0075): `override.approval.review`.
 *
 *     {original, fields_from_workflow, fields: [{name, type, required, editable,
 *      from, label, options, items, properties, min_items, max_items, format,
 *      description}]}
 *
 * A field is a workflow contract field, edited with the contract editor's own
 * draft and payload, plus `editable` and `from` (the selector of its initial
 * value, the syntax of an Invoke input mapping). `fields_from_workflow` is only
 * an editor hint: the fields are saved in full and run as saved.
 */
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { WorkflowVariableSuggestionGroup } from "@/pages/workflows/detail/detail-core";
import {
  fieldDraftFromContract,
  workflowFieldPayload,
  type WorkflowInterfaceFieldDraft,
} from "@/pages/workflows/detail/detail-interface";

export type ReviewFieldDraft = WorkflowInterfaceFieldDraft & { editable: boolean; from: string };

export type ReviewDraft = { original: string; fieldsFromWorkflow: string; fields: ReviewFieldDraft[] };

/** Field types a reviewer can see: every contract type but a secret. */
export const REVIEW_FIELD_TYPES = ["string", "paragraph", "number", "boolean", "select", "object", "array", "file"] as const;

const REVIEW_ONLY_KEYS = ["editable", "from", "source"];
const PAYLOAD_KEY_ORDER = [
  "name", "type", "required", "editable", "from", "label", "options", "items", "properties",
  "min_items", "max_items", "format", "description",
];

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** One stored or contract row as a review field draft; a row without a type is a paragraph. */
export function reviewFieldDraft(row: Record<string, unknown>, patch: Partial<Pick<ReviewFieldDraft, "editable" | "from">> = {}): ReviewFieldDraft {
  const authored = Object.fromEntries(Object.entries(row).filter(([key]) => !REVIEW_ONLY_KEYS.includes(key)));
  const draft = fieldDraftFromContract({ ...authored, type: text(row.type) || "paragraph" }, authored);
  return {
    ...draft,
    defaultConfigured: false,
    default: "",
    editable: patch.editable ?? row.editable === true,
    from: patch.from ?? (text(row.from) || text(row.source)),
  };
}

export function reviewDraftFromConfig(review: unknown): ReviewDraft {
  const stored = record(review);
  return {
    original: text(stored.original),
    fieldsFromWorkflow: text(stored.fields_from_workflow),
    fields: (Array.isArray(stored.fields) ? stored.fields : []).map(record).map((row) => reviewFieldDraft(row)),
  };
}

function ordered(payload: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const key of PAYLOAD_KEY_ORDER) if (Object.hasOwn(payload, key)) result[key] = payload[key];
  for (const [key, value] of Object.entries(payload)) if (!Object.hasOwn(result, key)) result[key] = value;
  return result;
}

/** One field as stored; throws with a message when its described shape is not valid JSON. */
export function reviewFieldPayload(row: ReviewFieldDraft, t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  const [payload] = workflowFieldPayload([{ ...row, defaultConfigured: false }], t);
  delete payload.default;
  delete payload.default_configured;
  for (const key of REVIEW_ONLY_KEYS) delete payload[key];
  payload.editable = row.editable;
  if (row.from.trim()) payload.from = row.from.trim();
  return ordered(payload);
}

/** The whole `review`, or undefined when the step has none (an Approval without fields works as before). */
export function reviewPayload(draft: ReviewDraft, t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> | undefined {
  if (!draft.fields.length && !draft.original.trim() && !draft.fieldsFromWorkflow.trim()) return undefined;
  const payload: Record<string, unknown> = {};
  if (draft.original.trim()) payload.original = draft.original.trim();
  if (draft.fieldsFromWorkflow.trim()) payload.fields_from_workflow = draft.fieldsFromWorkflow.trim();
  payload.fields = draft.fields.map((row) => reviewFieldPayload(row, t));
  return payload;
}

// --- Sources -----------------------------------------------------------------

const PRODUCER_TYPES: Record<string, string[]> = {
  string: ["string", "text", "paragraph"],
  paragraph: ["string", "text", "paragraph"],
  select: ["string", "select"],
  number: ["number", "integer", "float"],
  boolean: ["boolean", "bool"],
  array: ["array", "list"],
  object: ["object", "json", "dict"],
  file: ["file", "artifact_handle"],
};

/** Whether a producer's type fits a review field's type; unknown when the producer does not say. */
export function sourceTypeFit(fieldType: string, producerType: string | undefined): "match" | "unknown" | "mismatch" {
  const producer = (producerType ?? "").trim().toLowerCase();
  if (!producer) return "unknown";
  return (PRODUCER_TYPES[fieldType] ?? [fieldType]).includes(producer) ? "match" : "mismatch";
}

function normalized(name: string): string {
  return name.toLowerCase().replaceAll(/[^a-z0-9]/g, "");
}

export type SourceSuggestion = { value: string; label: string; fit: "match" | "unknown" };

/**
 * Earlier values that look like this field's source: the same name, and a type
 * that fits or is not known. Type matches come first; a known misfit is left out.
 */
export function sourceSuggestions(
  field: Pick<ReviewFieldDraft, "name" | "type">,
  groups: readonly WorkflowVariableSuggestionGroup[] | undefined,
): SourceSuggestion[] {
  const wanted = normalized(field.name);
  if (!wanted) return [];
  const found: SourceSuggestion[] = [];
  const seen = new Set<string>();
  for (const group of groups ?? []) {
    for (const option of group.options) {
      const last = option.value.split(".").at(-1) ?? "";
      if (normalized(last) !== wanted || seen.has(option.value)) continue;
      const fit = sourceTypeFit(field.type, option.valueType);
      if (fit === "mismatch") continue;
      seen.add(option.value);
      found.push({ value: option.value, label: option.label || option.value, fit });
    }
  }
  return found.sort((left, right) => (left.fit === right.fit ? 0 : left.fit === "match" ? -1 : 1));
}

/**
 * «Take fields from a workflow»: the workflow's inputs with their types,
 * requirements and options, editable, each with its best-matching source.
 * Fields already listed keep their editability and source; others stay after.
 */
export function fieldsFromWorkflowInputs(
  inputs: readonly Record<string, unknown>[],
  current: readonly ReviewFieldDraft[],
  groups: readonly WorkflowVariableSuggestionGroup[] | undefined,
): ReviewFieldDraft[] {
  const byName = new Map(current.map((row) => [row.name, row]));
  const taken = new Set<string>();
  const fields: ReviewFieldDraft[] = [];
  for (const input of inputs) {
    const name = text(input.name);
    if (!name || text(input.type) === "secret" || taken.has(name)) continue;
    taken.add(name);
    const existing = byName.get(name);
    const draft = reviewFieldDraft(
      Object.fromEntries(Object.entries(input).filter(([key]) => !["default", "default_configured", "caller_may_supply", "sample_file", "default_secret_ref"].includes(key))),
      { editable: existing ? existing.editable : true, from: existing?.from ?? "" },
    );
    if (!draft.from) {
      const best = sourceSuggestions(draft, groups).find((suggestion) => suggestion.fit === "match");
      if (best) draft.from = best.value;
    }
    fields.push(draft);
  }
  return [...fields, ...current.filter((row) => !taken.has(row.name))];
}

// --- Readiness issues ---------------------------------------------------------

export type ReviewIssue = { code: string; message: string; path: string };

export type ReviewIssuesByPlace = { general: ReviewIssue[]; original: ReviewIssue[]; fields: Map<number, ReviewIssue[]> };

/** The workflow's last readiness issues of this step's review, by the place they name. */
export function reviewIssuesByPlace(issues: unknown, stepId: string): ReviewIssuesByPlace {
  const result: ReviewIssuesByPlace = { general: [], original: [], fields: new Map() };
  if (!Array.isArray(issues) || !stepId) return result;
  const prefix = `workflow.steps.${stepId}.override.approval.review`;
  for (const raw of issues) {
    const issue = record(raw);
    const path = typeof issue.path === "string" ? issue.path : "";
    const message = typeof issue.message === "string" ? issue.message : "";
    if (!message || (path !== prefix && !path.startsWith(`${prefix}.`))) continue;
    const entry = { code: String(issue.code ?? ""), message, path };
    const rest = path.slice(prefix.length).replace(/^\./, "");
    const field = /^fields\[(\d+)\]/.exec(rest);
    if (field) {
      const index = Number(field[1]);
      result.fields.set(index, [...(result.fields.get(index) ?? []), entry]);
    } else if (rest === "original") {
      result.original.push(entry);
    } else {
      result.general.push(entry);
    }
  }
  return result;
}
