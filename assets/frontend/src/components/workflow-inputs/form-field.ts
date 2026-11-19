/**
 * The operator form's field model (ADR 0075): one entry of the server's
 * `form_fields`, already ordered, resolved against the contract and free of
 * secrets. Kind, requirement, options and item bounds are the contract's; the
 * admin chose the widget, labels, presentation and conditions in Workflow
 * Settings. The server stays the authority: these checks only spare a round trip.
 */

export type FormFieldKind =
  | "string"
  | "select"
  | "select_list"
  | "number"
  | "boolean"
  | "file"
  | "file_list"
  | "object"
  | "object_list";

export type ConditionRule = { field: string; operator: string; value?: unknown };

export type FormFieldView = {
  name: string;
  kind: FormFieldKind | string;
  widget: string;
  widgets?: string[];
  label: string;
  instructions?: string;
  placeholder?: string;
  prepend?: string;
  append?: string;
  /** Percent of the form row; empty is the full row. */
  width?: number | null;
  required?: boolean;
  /** The contract fixes the value: never rendered, never sent. */
  fixed?: boolean;
  /** Hidden by the admin: the contract's default applies; never rendered, never sent. */
  hidden?: boolean;
  options?: string[];
  option_labels?: Record<string, string>;
  /** Groups joined by "or", rules inside a group joined by "and". */
  conditional_logic?: ConditionRule[][];
  layout?: "" | "table" | "block" | "row";
  button_label?: string;
  min_items?: number | null;
  max_items?: number | null;
  format?: string;
  sub_fields?: FormFieldView[];
};

export type FormValues = Record<string, unknown>;
export type FormFiles = Record<string, File[]>;

export type FormProblem =
  | { field: string; code: "required" }
  /** A number field holds text that is not a number; it is never dropped silently. */
  | { field: string; code: "invalid_number" }
  | { field: string; code: "min_items" | "max_items"; limit: number }
  | { field: string; code: "row_required" | "row_invalid_number"; row: number; subField: string };

export function isFileField(field: Pick<FormFieldView, "kind">): boolean {
  return field.kind === "file" || field.kind === "file_list";
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function isEmptyValue(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (typeof value === "string") return value.trim() === "";
  if (typeof value === "number") return !Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isEmptyValue);
  if (typeof value === "object" && !(value instanceof File)) return Object.values(value).every(isEmptyValue);
  return false;
}

/**
 * A number as a person types it in their locale: "318,50" and "318.50" are
 * 318.5, "1 234,56" and "1,234.56" are 1234.56. Spaces and apostrophes group
 * digits; with both separators the last one is the decimal mark, a single comma
 * alone is one too. Anything else (a letter, two decimal marks) is null: the
 * caller keeps the text and the server names the field.
 */
export function parseLocaleNumber(text: string): number | null {
  let compact = text.trim().replaceAll(/[\s\u00a0\u202f'’]/g, "");
  if (!compact) return null;
  const lastComma = compact.lastIndexOf(",");
  const lastDot = compact.lastIndexOf(".");
  if (lastComma >= 0 && lastDot >= 0) {
    const decimal = lastComma > lastDot ? "," : ".";
    const group = decimal === "," ? "." : ",";
    compact = compact.replaceAll(group, "").replace(decimal, ".");
  } else if (lastComma >= 0) {
    compact = compact.split(",").length === 2 ? compact.replace(",", ".") : compact.replaceAll(",", "");
  }
  if (!/^[+-]?(\d+\.?\d*|\.\d+)$/.test(compact)) return null;
  const parsed = Number(compact);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Whether a number field holds typed text that is not a number. The widget keeps
 * such text as typed; sending would drop it (optional) or misreport it as
 * missing (required), so the form names the field instead.
 */
export function isInvalidNumberText(field: Pick<FormFieldView, "kind">, raw: unknown): boolean {
  return field.kind === "number" && typeof raw === "string" && raw.trim() !== "" && parseLocaleNumber(raw) === null;
}

/** What an option shows: the admin's label, or the option itself. */
export function optionLabel(field: Pick<FormFieldView, "option_labels">, option: string): string {
  return field.option_labels?.[option] || option;
}

function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function sameValue(kind: string, value: unknown, expected: unknown): boolean {
  if (kind === "boolean") return (value === true) === (expected === true);
  if (kind === "number") {
    const left = toNumber(value);
    return left !== null && left === toNumber(expected);
  }
  return String(value ?? "") === String(expected ?? "");
}

function containsValue(value: unknown, expected: unknown): boolean {
  if (Array.isArray(value)) return value.map(String).includes(String(expected ?? ""));
  return String(value ?? "").includes(String(expected ?? ""));
}

/** One ACF-style rule over another field's current value. */
export function conditionRuleMet(rule: ConditionRule, kind: string, value: unknown): boolean {
  switch (rule.operator) {
    case "==empty":
      return isEmptyValue(value);
    case "!=empty":
      return !isEmptyValue(value);
    case "==":
      return sameValue(kind, value, rule.value);
    case "!=":
      return !sameValue(kind, value, rule.value);
    case ">":
    case "<": {
      const left = toNumber(value);
      const right = toNumber(rule.value);
      if (left === null || right === null) return false;
      return rule.operator === ">" ? left > right : left < right;
    }
    case "==contains":
      return containsValue(value, rule.value);
    case "!=contains":
      return !containsValue(value, rule.value);
    case "==pattern":
      try {
        return new RegExp(String(rule.value ?? "")).test(String(value ?? ""));
      } catch {
        return false;
      }
    default:
      return false;
  }
}

/** A field's current value as conditions read it: files for a file field. */
function currentValue(field: FormFieldView, values: FormValues, files: FormFiles): unknown {
  return isFileField(field) ? files[field.name] ?? [] : values[field.name];
}

/**
 * The fields shown now, in order: never hidden or fixed ones, and only those
 * whose condition holds (any group whose rules all hold). A field that its own
 * condition hides reads as empty to the conditions of others.
 */
export function shownFields(fields: readonly FormFieldView[], values: FormValues, files: FormFiles = {}): FormFieldView[] {
  const byName = new Map(fields.map((field) => [field.name, field]));
  const memo = new Map<string, boolean>();
  const visiting = new Set<string>();
  const isShown = (field: FormFieldView): boolean => {
    if (field.hidden || field.fixed) return false;
    const known = memo.get(field.name);
    if (known !== undefined) return known;
    // A cycle of conditions cannot be decided; the server refuses one on save.
    if (visiting.has(field.name)) return true;
    visiting.add(field.name);
    const groups = field.conditional_logic ?? [];
    const shown =
      groups.length === 0 ||
      groups.some((group) =>
        group.every((rule) => {
          const other = byName.get(rule.field);
          const value = other && isShown(other) ? currentValue(other, values, files) : undefined;
          return conditionRuleMet(rule, other?.kind ?? "", value);
        }),
      );
    visiting.delete(field.name);
    memo.set(field.name, shown);
    return shown;
  };
  return fields.filter(isShown);
}

/** The JSON value one shown field sends; undefined leaves the input to the contract. */
function fieldValue(field: FormFieldView, raw: unknown): unknown {
  switch (field.kind) {
    case "object": {
      const value = collectInputs(field.sub_fields ?? [], asRecord(raw));
      return Object.keys(value).length ? value : undefined;
    }
    case "object_list": {
      const rows = asList(raw)
        .map((row) => collectInputs(field.sub_fields ?? [], asRecord(row)))
        .filter((row) => Object.keys(row).length > 0);
      return rows.length ? rows : undefined;
    }
    case "select_list": {
      const items = asList(raw).map(String).filter((item) => item !== "");
      return items.length ? items : undefined;
    }
    case "number":
      return typeof raw === "number" && Number.isFinite(raw) ? raw : undefined;
    case "boolean":
      if (typeof raw === "boolean") return raw;
      // An untouched required switch means "no", not "missing".
      return field.required ? false : undefined;
    default:
      return typeof raw === "string" && raw.trim() !== "" ? raw : undefined;
  }
}

/**
 * The non-file inputs a submission sends: shown fields only, empty optional
 * values left out so the contract's defaults apply. Hidden, fixed and
 * condition-hidden fields are never sent.
 */
export function collectInputs(fields: readonly FormFieldView[], values: FormValues, files: FormFiles = {}): Record<string, unknown> {
  const inputs: Record<string, unknown> = {};
  for (const field of shownFields(fields, values, files)) {
    if (isFileField(field)) continue;
    const value = fieldValue(field, values[field.name]);
    if (value !== undefined) inputs[field.name] = value;
  }
  return inputs;
}

/** The value of one shown, non-file field as a submission would send it. */
export function collectFieldValue(field: FormFieldView, values: FormValues): unknown {
  return fieldValue(field, values[field.name]);
}

/** Files of the shown file fields, one entry per file, in upload order. */
export function collectFiles(fields: readonly FormFieldView[], values: FormValues, files: FormFiles): { inputName: string; file: File }[] {
  return shownFields(fields, values, files)
    .filter(isFileField)
    .flatMap((field) => (files[field.name] ?? []).map((file) => ({ inputName: field.name, file })));
}

function itemCount(field: FormFieldView, values: FormValues, files: FormFiles): number {
  if (isFileField(field)) return (files[field.name] ?? []).length;
  const value = fieldValue(field, values[field.name]);
  return Array.isArray(value) ? value.length : 0;
}

/**
 * What the form refuses before sending, in field order: a number field holding
 * text that is not a number, a shown required field left empty, a list outside
 * its contract bounds, a required sub-field left empty or a number sub-field
 * not a number in a filled repeater row or group. `exempt` skips the bounds of the For
 * each target, whose runs each get one item.
 */
export function formProblems(
  fields: readonly FormFieldView[],
  values: FormValues,
  files: FormFiles = {},
  exempt = "",
): FormProblem[] {
  const problems: FormProblem[] = [];
  for (const field of shownFields(fields, values, files)) {
    const listLike = ["file_list", "select_list", "object_list"].includes(field.kind);
    const count = itemCount(field, values, files);
    const empty = isFileField(field) ? count === 0 : fieldValue(field, values[field.name]) === undefined;
    if (isInvalidNumberText(field, values[field.name])) {
      problems.push({ field: field.name, code: "invalid_number" });
      continue;
    }
    if (field.required && empty) {
      problems.push({ field: field.name, code: "required" });
      continue;
    }
    if (listLike && field.name !== exempt && !empty) {
      if (typeof field.min_items === "number" && count < field.min_items) {
        problems.push({ field: field.name, code: "min_items", limit: field.min_items });
        continue;
      }
      if (typeof field.max_items === "number" && count > field.max_items) {
        problems.push({ field: field.name, code: "max_items", limit: field.max_items });
        continue;
      }
    }
    const rows =
      field.kind === "object_list"
        ? asList(values[field.name]).map(asRecord)
        : field.kind === "object" && !isEmptyValue(values[field.name])
          ? [asRecord(values[field.name])]
          : [];
    rows.forEach((row, index) => {
      if (field.kind === "object_list" && isEmptyValue(row)) return;
      const inner = formProblems(field.sub_fields ?? [], row).find((problem) => problem.code === "required" || problem.code === "invalid_number");
      if (inner) {
        problems.push({
          field: field.name,
          code: inner.code === "invalid_number" ? "row_invalid_number" : "row_required",
          row: index + 1,
          subField: inner.field,
        });
      }
    });
  }
  return problems;
}

/**
 * Starting values: the contract's defaults where the input schema declares
 * them, and a repeater's minimum number of empty rows.
 */
export function initialFormValues(fields: readonly FormFieldView[], inputSchema?: Record<string, unknown> | null): FormValues {
  const properties = asRecord(asRecord(inputSchema).properties);
  const values: FormValues = {};
  for (const field of fields) {
    if (field.hidden || field.fixed || isFileField(field)) continue;
    const declared = asRecord(properties[field.name]).default;
    if (declared !== undefined && declared !== null) {
      values[field.name] = declared;
    } else if (field.kind === "object_list" && typeof field.min_items === "number" && field.min_items > 0) {
      values[field.name] = Array.from({ length: field.min_items }, () => ({}));
    }
  }
  return values;
}
