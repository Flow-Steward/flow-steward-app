/**
 * The operator form of a workflow as Workflow Settings edits it (ADR 0075):
 * `settings.operator` next to `settings.callable`. The contract owns the
 * fields, their types, requirements and options; this draft only says how each
 * one is shown, how the result reads, and what the jobs list offers. The
 * server checks the whole object on save and names every problem by its path.
 */
import type { ConditionRule, FormFieldView } from "@/components/workflow-inputs/form-field";
import type { OperatorFilterType, OperatorIssue, OperatorRuntimeView } from "@/pages/operator/types";

export type OperatorCompatibility = {
  widgets_by_kind?: Record<string, string[]>;
  widgets_by_string_format?: Record<string, string[]>;
  filter_types_by_kind?: Record<string, string[]>;
  job_columns?: string[];
  job_filters?: Record<string, string[]>;
  job_status_options?: string[];
  column_formats?: string[];
  result_displays?: string[];
  condition_operators_by_kind?: Record<string, string[]>;
};

/** The `operator` block of `GET /api/workflows/{id}/contract`. */
export type OperatorEditorView = OperatorRuntimeView & { compatibility?: OperatorCompatibility };

export type OperatorFieldLayout = "" | "table" | "block" | "row";

export type OperatorFieldConfig = {
  widget: string;
  label: string;
  instructions: string;
  placeholder: string;
  prepend: string;
  append: string;
  width: number | null;
  option_labels: Record<string, string>;
  hidden: boolean;
  conditional_logic: ConditionRule[][];
  layout: OperatorFieldLayout;
  button_label: string;
  sub_fields: Record<string, OperatorFieldConfig>;
};

export type OperatorResultConfig = { label: string; display: string; hidden: boolean; decimals: number | null };

export type OperatorValueSource = "job" | "input" | "output";

export type OperatorColumnDraft = { id: string; source: OperatorValueSource; path: string; label: string; format: string };

export type OperatorFilterDraft = { id: string; source: OperatorValueSource; path: string; type: OperatorFilterType | ""; label: string };

export type OperatorSettingsDraft = {
  published: boolean;
  menu_title: string;
  /** The stored acknowledgement ({by, at, revision}), sent back so the server keeps it. */
  effects_acknowledged: Record<string, unknown> | null;
  /** Acknowledge the real effects with this save: the server stamps who, when and which revision. */
  acknowledge: boolean;
  submission: { mode: "foreach" | "bulk"; target_input: string };
  form: { order: string[]; fields: Record<string, OperatorFieldConfig> };
  result: { order: string[]; fields: Record<string, OperatorResultConfig> };
  list: { columns: OperatorColumnDraft[]; filters: OperatorFilterDraft[] };
};

export const OPERATOR_SETTINGS_PATH = "settings.operator";
export const FORM_FIELDS_PATH = `${OPERATOR_SETTINGS_PATH}.form.fields`;

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export function emptyFieldConfig(): OperatorFieldConfig {
  return {
    widget: "", label: "", instructions: "", placeholder: "", prepend: "", append: "",
    width: null, option_labels: {}, hidden: false, conditional_logic: [], layout: "",
    button_label: "", sub_fields: {},
  };
}

function conditionGroups(value: unknown): ConditionRule[][] {
  if (!Array.isArray(value)) return [];
  return value
    .filter(Array.isArray)
    .map((group) => (group as unknown[]).map(record).map((rule) => ({
      field: text(rule.field),
      operator: text(rule.operator),
      value: rule.value ?? null,
    })));
}

export function fieldConfigFromStored(raw: unknown): OperatorFieldConfig {
  const row = record(raw);
  const layout = text(row.layout);
  const width = typeof row.width === "number" && Number.isFinite(row.width) ? row.width : null;
  return {
    widget: text(row.widget),
    label: text(row.label),
    instructions: text(row.instructions),
    placeholder: text(row.placeholder),
    prepend: text(row.prepend),
    append: text(row.append),
    width,
    option_labels: Object.fromEntries(Object.entries(record(row.option_labels)).filter(([, label]) => typeof label === "string")) as Record<string, string>,
    hidden: row.hidden === true,
    conditional_logic: conditionGroups(row.conditional_logic),
    layout: (["table", "block", "row"].includes(layout) ? layout : "") as OperatorFieldLayout,
    button_label: text(row.button_label),
    sub_fields: Object.fromEntries(Object.entries(record(row.sub_fields)).map(([name, sub]) => [name, fieldConfigFromStored(sub)])),
  };
}

function resultConfigFromStored(raw: unknown): OperatorResultConfig {
  const row = record(raw);
  return {
    label: text(row.label),
    display: text(row.display) || "auto",
    hidden: row.hidden === true,
    decimals: typeof row.decimals === "number" ? row.decimals : null,
  };
}

function source(value: unknown): OperatorValueSource {
  return value === "job" || value === "output" ? value : "input";
}

/**
 * The editor's draft from the stored settings. Settings of fields the contract
 * no longer declares are dropped: the contract owns the fields, and a save of
 * the form writes only what it can show.
 */
export function operatorDraftFromStored(
  raw: unknown,
  known: { inputs?: readonly string[]; outputs?: readonly string[] } = {},
): OperatorSettingsDraft {
  const stored = record(raw);
  const form = record(stored.form);
  const result = record(stored.result);
  const list = record(stored.list);
  const submission = record(stored.submission);
  const inputs = known.inputs ? new Set(known.inputs) : null;
  const outputs = known.outputs ? new Set(known.outputs) : null;
  const ack = stored.effects_acknowledged;
  return {
    published: stored.published === true,
    menu_title: text(stored.menu_title),
    effects_acknowledged: ack && typeof ack === "object" && !Array.isArray(ack)
      ? Object.fromEntries(Object.entries(ack as Record<string, unknown>).filter(([key]) => key !== "confirm"))
      : null,
    acknowledge: false,
    submission: {
      mode: submission.mode === "foreach" ? "foreach" : "bulk",
      target_input: text(submission.target_input),
    },
    form: {
      order: strings(form.order).filter((name) => !inputs || inputs.has(name)),
      fields: Object.fromEntries(Object.entries(record(form.fields))
        .filter(([name]) => !inputs || inputs.has(name))
        .map(([name, value]) => [name, fieldConfigFromStored(value)])),
    },
    result: {
      order: strings(result.order).filter((name) => !outputs || outputs.has(name)),
      fields: Object.fromEntries(Object.entries(record(result.fields))
        .filter(([name]) => !outputs || outputs.has(name))
        .map(([name, value]) => [name, resultConfigFromStored(value)])),
    },
    list: {
      columns: (Array.isArray(list.columns) ? list.columns : []).map(record).map((column) => ({
        id: text(column.id),
        source: source(column.source),
        path: text(column.path),
        label: text(column.label),
        format: text(column.format) || "auto",
      })),
      filters: (Array.isArray(list.filters) ? list.filters : []).map(record).map((filter) => ({
        id: text(filter.id),
        source: source(filter.source),
        path: text(filter.path),
        type: text(filter.type) as OperatorFilterType,
        label: text(filter.label),
      })),
    },
  };
}

/** One field's settings with only what differs from the defaults; `{}` when nothing does. */
export function fieldConfigPayload(config: OperatorFieldConfig): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const key of ["widget", "label", "instructions", "placeholder", "prepend", "append", "button_label", "layout"] as const) {
    const value = config[key].trim();
    if (value) payload[key] = value;
  }
  if (typeof config.width === "number") payload.width = config.width;
  const labels = Object.fromEntries(Object.entries(config.option_labels).map(([option, label]) => [option, label.trim()]).filter(([, label]) => label));
  if (Object.keys(labels).length) payload.option_labels = labels;
  if (config.hidden) payload.hidden = true;
  const groups = config.conditional_logic.filter((group) => group.length > 0);
  if (groups.length) payload.conditional_logic = groups.map((group) => group.map((rule) => ({ field: rule.field, operator: rule.operator, value: rule.value ?? null })));
  const subFields = Object.fromEntries(Object.entries(config.sub_fields)
    .map(([name, sub]) => [name, fieldConfigPayload(sub)] as const)
    .filter(([, sub]) => Object.keys(sub).length > 0));
  if (Object.keys(subFields).length) payload.sub_fields = subFields;
  return payload;
}

function resultConfigPayload(config: OperatorResultConfig): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  if (config.label.trim()) payload.label = config.label.trim();
  if (config.display && config.display !== "auto") payload.display = config.display;
  if (config.hidden) payload.hidden = true;
  if (typeof config.decimals === "number") payload.decimals = config.decimals;
  return payload;
}

function sameOrder(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((name, index) => name === right[index]);
}

/**
 * The whole `settings.operator` a save sends: it replaces the stored one, so a
 * cleared label or a field moved back to its default is gone. An order equal to
 * the contract's is not stored, so a field the contract adds later keeps its place.
 */
export function operatorSettingsPayload(
  draft: OperatorSettingsDraft,
  contractOrder: { inputs?: readonly string[]; outputs?: readonly string[] } = {},
): Record<string, unknown> {
  const formFields = Object.fromEntries(Object.entries(draft.form.fields)
    .map(([name, config]) => [name, fieldConfigPayload(config)] as const)
    .filter(([, config]) => Object.keys(config).length > 0));
  const resultFields = Object.fromEntries(Object.entries(draft.result.fields)
    .map(([name, config]) => [name, resultConfigPayload(config)] as const)
    .filter(([, config]) => Object.keys(config).length > 0));
  const formOrder = contractOrder.inputs && sameOrder(draft.form.order, contractOrder.inputs) ? [] : draft.form.order;
  const resultOrder = contractOrder.outputs && sameOrder(draft.result.order, contractOrder.outputs) ? [] : draft.result.order;
  let acknowledgement: Record<string, unknown> | null = draft.effects_acknowledged;
  if (draft.acknowledge) acknowledgement = { confirm: true };
  return {
    published: draft.published,
    menu_title: draft.menu_title.trim(),
    effects_acknowledged: acknowledgement,
    submission: draft.submission.mode === "foreach"
      ? { mode: "foreach", target_input: draft.submission.target_input }
      : { mode: "bulk", target_input: "" },
    form: { order: [...formOrder], fields: formFields },
    result: { order: [...resultOrder], fields: resultFields },
    list: {
      columns: draft.list.columns.filter((column) => column.path).map((column) => ({
        id: column.id || `${column.source}.${column.path}`,
        source: column.source,
        path: column.path,
        label: column.label.trim(),
        format: column.format || "auto",
      })),
      filters: draft.list.filters.filter((filter) => filter.path && filter.type).map((filter) => ({
        id: filter.id || `${filter.source}.${filter.path}`,
        source: filter.source,
        path: filter.path,
        type: filter.type,
        label: filter.label.trim(),
      })),
    },
  };
}

// --- Order -----------------------------------------------------------------

/** Names in the draft's order first, then the rest in the contract's order. */
export function mergedOrder(stored: readonly string[], contract: readonly string[]): string[] {
  const known = new Set(contract);
  const names = stored.filter((name, index) => known.has(name) && stored.indexOf(name) === index);
  return [...names, ...contract.filter((name) => !names.includes(name))];
}

export function moveInOrder(order: readonly string[], name: string, delta: number): string[] {
  const index = order.indexOf(name);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= order.length) return [...order];
  const next = [...order];
  next.splice(index, 1);
  next.splice(target, 0, name);
  return next;
}

export function moveAt<T>(rows: readonly T[], index: number, delta: number): T[] {
  const target = index + delta;
  if (target < 0 || target >= rows.length) return [...rows];
  const next = [...rows];
  const [row] = next.splice(index, 1);
  next.splice(target, 0, row);
  return next;
}

// --- Contract schema as the editor reads it -------------------------------

export function schemaProperties(schema: unknown): Record<string, Record<string, unknown>> {
  return Object.fromEntries(Object.entries(record(record(schema).properties)).map(([name, node]) => [name, record(node)]));
}

/** The kind of a JSON Schema node, as the server's `field_kind`: "" when the form cannot show it. */
export function schemaFieldKind(node: unknown): string {
  const schema = record(node);
  if (!Object.keys(schema).length) return "";
  if (schema["x-fs-type"] === "file") return "file";
  if (schema.writeOnly || schema["x-fs-type"] === "secret") return "secret";
  const type = schema.type === "integer" ? "number" : schema.type;
  if (type === "string") return Array.isArray(schema.enum) && schema.enum.length ? "select" : "string";
  if (type === "number" || type === "boolean") return type;
  if (type === "object") return Object.keys(record(schema.properties)).length ? "object" : "";
  if (type === "array") {
    const items = schemaFieldKind(schema.items);
    return ({ select: "select_list", file: "file_list", object: "object_list" } as Record<string, string>)[items] ?? "";
  }
  return "";
}

/** The kind a filter sees: a date string is a date. */
export function schemaFilterKind(node: unknown): string {
  const kind = schemaFieldKind(node);
  const format = text(record(node).format);
  return kind === "string" && (format === "date" || format === "date-time") ? "date" : kind;
}

export function isListLikeInput(node: unknown): boolean {
  const schema = record(node);
  return schemaFieldKind(schema) === "file" || schema.type === "array";
}

export type ValuePathOption = { path: string; kind: string; title: string };

/**
 * The value paths a column may show: every top-level property, the properties
 * of a group (`customer.vat`) and of a list of groups (`companies[].name`).
 */
export function valuePathOptions(schema: unknown, nested = true): ValuePathOption[] {
  const options: ValuePathOption[] = [];
  for (const [name, node] of Object.entries(schemaProperties(schema))) {
    const kind = schemaFieldKind(node);
    if (kind === "secret") continue;
    options.push({ path: name, kind, title: text(node.title) });
    if (!nested) continue;
    if (kind === "object") {
      for (const [sub, subNode] of Object.entries(schemaProperties(node))) {
        options.push({ path: `${name}.${sub}`, kind: schemaFieldKind(subNode), title: text(subNode.title) });
      }
    } else if (kind === "object_list") {
      for (const [sub, subNode] of Object.entries(schemaProperties(node.items))) {
        options.push({ path: `${name}[].${sub}`, kind: schemaFieldKind(subNode), title: text(subNode.title) });
      }
    }
  }
  return options;
}

/** Filter types for one value; files, groups and unsupported values have none. */
export function filterTypesFor(
  view: Pick<OperatorEditorView, "input_schema" | "output_schema" | "compatibility">,
  sourceName: OperatorValueSource,
  path: string,
): string[] {
  const compatibility = view.compatibility ?? {};
  if (sourceName === "job") return compatibility.job_filters?.[path] ?? [];
  if (path.includes(".")) return [];
  const node = schemaProperties(sourceName === "input" ? view.input_schema : view.output_schema)[path];
  if (!node) return [];
  return compatibility.filter_types_by_kind?.[schemaFilterKind(node)] ?? [];
}

// --- Issues ----------------------------------------------------------------

export function fieldPath(names: readonly string[]): string {
  return names.reduce((path, name, index) => index === 0 ? `${FORM_FIELDS_PATH}.${name}` : `${path}.sub_fields.${name}`, "");
}

/** Issues at a path or under it. */
export function issuesUnder(issues: readonly OperatorIssue[], prefix: string): OperatorIssue[] {
  return issues.filter((issue) => {
    const path = issue.path ?? "";
    return path === prefix || path.startsWith(`${prefix}.`) || path.startsWith(`${prefix}[`);
  });
}

/** Issues of one field itself, not of its sub-fields. */
export function ownFieldIssues(issues: readonly OperatorIssue[], prefix: string): OperatorIssue[] {
  return issuesUnder(issues, prefix).filter((issue) => !(issue.path ?? "").startsWith(`${prefix}.sub_fields.`));
}

export type OperatorSubTab = "form" | "result" | "list" | "publication";
export type OperatorFieldTab = "general" | "presentation" | "conditions";

export type IssueTarget = { tab: OperatorSubTab; field?: string[]; fieldTab?: OperatorFieldTab };

const PRESENTATION_KEYS = new Set(["instructions", "placeholder", "prepend", "append", "width", "option_labels", "layout", "button_label", "sub_fields"]);

/** Where an issue's path is edited: the sub-tab, and for a form field its name path and tab. */
export function issueTarget(path: string): IssueTarget {
  if (path.startsWith(`${FORM_FIELDS_PATH}.`)) {
    const parts = path.slice(FORM_FIELDS_PATH.length + 1).split(".");
    const field: string[] = [parts[0]];
    let index = 1;
    while (parts[index] === "sub_fields" && parts[index + 1]) {
      field.push(parts[index + 1]);
      index += 2;
    }
    const key = (parts[index] ?? "").replace(/\[.*$/, "");
    let fieldTab: OperatorFieldTab = "general";
    if (key === "conditional_logic") fieldTab = "conditions";
    else if (PRESENTATION_KEYS.has(key)) fieldTab = "presentation";
    return { tab: "form", field, fieldTab };
  }
  if (path.startsWith(`${OPERATOR_SETTINGS_PATH}.form`)) return { tab: "form" };
  if (path.startsWith(`${OPERATOR_SETTINGS_PATH}.result`)) return { tab: "result" };
  if (path.startsWith(`${OPERATOR_SETTINGS_PATH}.list`)) return { tab: "list" };
  return { tab: "publication" };
}

/** The issues a failed save reported (400 `workflow_operator_settings_invalid`), or null. */
export function operatorIssuesFromError(error: unknown): OperatorIssue[] | null {
  const payload = record(record(error).payload);
  const detail = record(payload.detail);
  if (detail.code !== "workflow_operator_settings_invalid") return null;
  return (Array.isArray(detail.issues) ? detail.issues : []).map(record).map((issue) => ({
    code: text(issue.code),
    message: text(issue.message),
    path: text(issue.path),
    severity: text(issue.severity) || "error",
  }));
}

// --- Preview ---------------------------------------------------------------

function previewField(view: FormFieldView, config: OperatorFieldConfig | undefined, node: Record<string, unknown>): FormFieldView {
  const settings = config ?? emptyFieldConfig();
  const widgets = view.widgets ?? [];
  const container = view.kind === "object_list" ? record(node.items) : node;
  const subNodes = schemaProperties(container);
  return {
    ...view,
    widget: settings.widget && widgets.includes(settings.widget) ? settings.widget : widgets[0] ?? view.widget,
    label: settings.label || text(node.title) || view.name,
    instructions: settings.instructions || text(node.description),
    placeholder: settings.placeholder,
    prepend: settings.prepend,
    append: settings.append,
    width: settings.width,
    hidden: settings.hidden || Boolean(view.fixed),
    option_labels: settings.option_labels,
    conditional_logic: settings.conditional_logic,
    layout: settings.layout || (view.kind === "object_list" ? "table" : ""),
    button_label: settings.button_label,
    sub_fields: (view.sub_fields ?? []).map((sub) => previewField(sub, settings.sub_fields[sub.name], subNodes[sub.name] ?? {})),
  };
}

/** The operator form as the draft would show it, for the live preview. */
export function previewFormFields(
  fields: readonly FormFieldView[],
  draft: OperatorSettingsDraft,
  inputSchema: unknown,
): FormFieldView[] {
  const byName = new Map(fields.map((field) => [field.name, field]));
  const nodes = schemaProperties(inputSchema);
  const contractOrder = Object.keys(nodes).filter((name) => byName.has(name));
  return mergedOrder(draft.form.order, contractOrder)
    .map((name) => byName.get(name))
    .filter((field): field is FormFieldView => Boolean(field))
    .map((field) => previewField(field, draft.form.fields[field.name], nodes[field.name] ?? {}));
}

/** The value a condition rule compares with, typed as the server expects for the other field's kind. */
export function conditionValue(kind: string, operator: string, raw: string): unknown {
  if (operator === "==empty" || operator === "!=empty") return null;
  if (kind === "boolean") return raw === "true";
  if (kind === "number" || operator === ">" || operator === "<") {
    const parsed = Number(raw);
    return raw.trim() !== "" && Number.isFinite(parsed) ? parsed : raw;
  }
  return raw;
}
