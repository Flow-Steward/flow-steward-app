/**
 * The ReportDefinition v1 a Template step in report format carries
 * (docs/architecture/workflow-report-schema-v1.md, sections 2-3), and the pure
 * edits the no-code editor makes to it. The editor writes the same contract the
 * AI Builder writes: nothing here is a UI-only format, and fields the editor
 * does not show (an authored summary, a binding default) are kept as they are.
 */
import type { ReportFormat, ReportScalar } from "@/components/report/report-types";

export type ValueBinding = { input: string; path?: string; default?: ReportScalar };
export type DatasetBinding = { input: string; path?: string; default?: [] };
export type DefinitionSegment = { text: string } | { value: ValueBinding; format: ReportFormat };
export type DefinitionField = { field: string; label: string; format: ReportFormat };

export type DefinitionComponent =
  | { id: string; type: "text"; style: "paragraph" | "heading"; segments: DefinitionSegment[] }
  | {
      id: string;
      type: "metric";
      label: string;
      value: ValueBinding;
      format: ReportFormat;
      comparison?: { label: string; value: ValueBinding; format: ReportFormat };
    }
  | {
      id: string;
      type: "table";
      caption: string;
      dataset: string;
      columns: Array<DefinitionField & { id: string }>;
      empty_text: string;
    }
  | {
      id: string;
      type: "chart";
      chart_type: "line" | "bar";
      title: string;
      description: string;
      dataset: string;
      category: DefinitionField;
      series: Array<DefinitionField & { id: string }>;
      empty_text: string;
    }
  | { id: string; type: "section"; title: string; children: DefinitionComponent[] };

export type ComponentType = DefinitionComponent["type"];
export type DefinitionDataset = { id: string; source: DatasetBinding };

export type ReportDefinition = {
  schema_version: 1;
  title: string;
  locale: string;
  time_zone: string;
  summary?: DefinitionSegment[];
  datasets?: DefinitionDataset[];
  components: DefinitionComponent[];
};

export const COMPONENT_TYPES: readonly ComponentType[] = ["text", "metric", "table", "chart", "section"];
/** Schema v1: sections nest at most four levels, counting the top level as one. */
export const MAX_DEPTH = 4;

export type FormatSlot = "value" | "metric" | "column" | "category" | "series";
const ALL_FORMATS: readonly ReportFormat["type"][] = [
  "text",
  "boolean",
  "number",
  "currency",
  "percent",
  "date",
  "datetime",
];
const NUMERIC_FORMATS: readonly ReportFormat["type"][] = ["number", "currency", "percent"];

/** The format types each slot accepts (schema v1 section 2.4). */
export const SLOT_FORMATS: Readonly<Record<FormatSlot, readonly ReportFormat["type"][]>> = {
  value: ALL_FORMATS,
  metric: NUMERIC_FORMATS,
  column: ALL_FORMATS,
  category: ["text", "number", "date", "datetime"],
  series: NUMERIC_FORMATS,
};

const ID_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** A BCP 47 tag of the schema's shape (language[-Script][-REGION]) from the browser, else en-US. */
function browserLocale(): string {
  const tag = globalThis.navigator?.language ?? "";
  return /^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|\d{3}))?$/.test(tag) ? tag : "en-US";
}

export function emptyDefinition(title = "Report"): ReportDefinition {
  return { schema_version: 1, title, locale: browserLocale(), time_zone: browserTimeZone(), components: [] };
}

/** The stored definition as the editor reads it; anything unreadable starts empty. */
export function readDefinition(value: unknown): ReportDefinition {
  const raw = record(value);
  if (!raw || !Array.isArray(raw.components)) return emptyDefinition();
  return raw as unknown as ReportDefinition;
}

/** Every component id in the tree; ids are unique across all depths. */
export function componentIds(components: readonly DefinitionComponent[]): string[] {
  return components.flatMap((component) =>
    component.type === "section" ? [component.id, ...componentIds(component.children)] : [component.id],
  );
}

/** The first `<base>_<n>` free in `taken`; ids are stable once given and never renumbered. */
export function freeId(base: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  const stem = ID_PATTERN.test(base) ? base.slice(0, 56) : "item";
  for (let index = 1; ; index += 1) {
    const candidate = `${stem}_${index}`;
    if (!used.has(candidate)) return candidate;
  }
}

export function defaultFormat(slot: FormatSlot): ReportFormat {
  return slot === "metric" || slot === "series" ? { type: "number" } : { type: "text" };
}

/** A format of another type keeps nothing of the previous one that would not fit. */
export function formatOfType(type: ReportFormat["type"], previous?: ReportFormat): ReportFormat {
  const digits =
    previous && "maximum_fraction_digits" in previous && NUMERIC_FORMATS.includes(type)
      ? { maximum_fraction_digits: previous.maximum_fraction_digits }
      : {};
  if (type === "currency") {
    const currency = previous?.type === "currency" ? previous.currency : "EUR";
    return { type, currency, ...digits };
  }
  if (type === "number" || type === "percent") return { type, ...digits };
  return { type } as ReportFormat;
}

export function newComponent(
  type: ComponentType,
  taken: Iterable<string>,
  datasetId: string,
): DefinitionComponent {
  const id = freeId(type, taken);
  switch (type) {
    case "text":
      return { id, type, style: "paragraph", segments: [{ text: "" }] };
    case "metric":
      return { id, type, label: "", value: { input: "" }, format: defaultFormat("metric") };
    case "table":
      return { id, type, caption: "", dataset: datasetId, columns: [], empty_text: "" };
    case "chart":
      return {
        id,
        type,
        chart_type: "bar",
        title: "",
        description: "",
        dataset: datasetId,
        category: { field: "", label: "", format: defaultFormat("category") },
        series: [],
        empty_text: "",
      };
    default:
      return { id, type: "section", title: "", children: [] };
  }
}

export function moveItem<T>(items: readonly T[], index: number, delta: number): T[] {
  const target = index + delta;
  if (target < 0 || target >= items.length) return [...items];
  const next = [...items];
  const [moved] = next.splice(index, 1);
  next.splice(target, 0, moved);
  return next;
}

export function replaceAt<T>(items: readonly T[], index: number, item: T): T[] {
  return items.map((current, at) => (at === index ? item : current));
}

/** A copy of `value` without `keys`; an absent optional field is left out, never set to undefined. */
export function without<T extends object, K extends keyof T>(value: T, ...keys: K[]): Omit<T, K> {
  const copy: Partial<T> = { ...value };
  for (const key of keys) delete copy[key];
  return copy as Omit<T, K>;
}

export function removeAt<T>(items: readonly T[], index: number): T[] {
  return items.filter((_, at) => at !== index);
}

/** The definition with `datasets` written only when there is one, as the schema allows. */
export function withDatasets(definition: ReportDefinition, datasets: DefinitionDataset[]): ReportDefinition {
  const rest = without(definition, "datasets");
  return datasets.length ? { ...rest, datasets } : rest;
}

/** Input types a binding may read, with and without a path (schema v1 section 3.2). */
export function bindingPossible(inputType: string, { dataset, hasPath }: { dataset: boolean; hasPath: boolean }): boolean {
  if (dataset) return hasPath ? ["object", "array", "json"].includes(inputType) : ["array", "json"].includes(inputType);
  return hasPath ? ["object", "array", "json"].includes(inputType) : ["text", "number", "boolean", "json"].includes(inputType);
}

export type ComponentProblem =
  | "input_unknown"
  | "binding_impossible"
  | "dataset_unknown"
  | "field_missing"
  | "label_missing"
  | "series_missing"
  | "columns_missing";

type InputType = { name: string; type: string };

function bindingProblem(
  binding: { input: string; path?: string } | undefined,
  inputs: readonly InputType[],
  dataset: boolean,
): ComponentProblem | null {
  const input = inputs.find((row) => row.name && row.name === binding?.input);
  if (!input) return "input_unknown";
  return bindingPossible(input.type, { dataset, hasPath: Boolean(binding?.path) }) ? null : "binding_impossible";
}

/**
 * What the editor can see is wrong with one component, before the server
 * judges it. The server's validation stays authoritative; this only puts the
 * obvious problems next to the component that has them.
 */
export function componentProblems(
  component: DefinitionComponent,
  inputs: readonly InputType[],
  datasetIds: readonly string[],
): ComponentProblem[] {
  const problems = new Set<ComponentProblem>();
  const add = (problem: ComponentProblem | null) => {
    if (problem) problems.add(problem);
  };
  switch (component.type) {
    case "text":
      for (const segment of component.segments) {
        if ("value" in segment) add(bindingProblem(segment.value, inputs, false));
      }
      break;
    case "metric":
      if (!component.label.trim()) add("label_missing");
      add(bindingProblem(component.value, inputs, false));
      if (component.comparison) add(bindingProblem(component.comparison.value, inputs, false));
      break;
    case "table":
      if (!datasetIds.includes(component.dataset)) add("dataset_unknown");
      if (!component.columns.length) add("columns_missing");
      if (component.columns.some((column) => !column.field.trim() || !column.label.trim())) add("field_missing");
      break;
    case "chart":
      if (!datasetIds.includes(component.dataset)) add("dataset_unknown");
      if (!component.series.length) add("series_missing");
      if (
        [component.category, ...component.series].some((field) => !field.field.trim() || !field.label.trim())
      ) {
        add("field_missing");
      }
      break;
    default:
      if (!component.title.trim()) add("label_missing");
  }
  return [...problems];
}

/** Problems of a data source: an unknown input or a type that cannot give rows. */
export function datasetProblem(dataset: DefinitionDataset, inputs: readonly InputType[]): ComponentProblem | null {
  return bindingProblem(dataset.source, inputs, true);
}

/**
 * Field names known for a rows source, from the builder's typed variable
 * discovery: the members a producing step declares (a Transform's fields, a
 * Set variable object) one level below the state path the source reads. An
 * empty list means the shape is not known before a run, not that it is empty.
 */
export function rowFieldSuggestions(
  dataset: DefinitionDataset | undefined,
  inputs: ReadonlyArray<{ name: string; source?: string }>,
  variablePaths: readonly string[],
): string[] {
  const input = inputs.find((row) => row.name && row.name === dataset?.source.input);
  const source = (input?.source ?? "").trim();
  if (!dataset || !source.startsWith("state.")) return [];
  const base = dataset.source.path ? `${source}.${dataset.source.path}` : source;
  const prefix = `${base}.`;
  const fields = variablePaths
    .filter((path) => path.startsWith(prefix))
    .map((path) => path.slice(prefix.length))
    .filter((field) => field && !/[.[\]]/.test(field));
  return [...new Set(fields)];
}

export type ReportIssueMap = {
  /** Messages per component id, at any depth. */
  components: Record<string, string[]>;
  /** Messages per rows source id. */
  datasets: Record<string, string[]>;
  /** Messages about the report as a whole (title, language, time zone, size). */
  report: string[];
};

const COMPONENT_STEP = /^(?:components|children)\[(\d+)\]/;

function componentAt(components: readonly DefinitionComponent[], rest: string): { id: string } | null {
  let list: readonly DefinitionComponent[] = components;
  let remaining = rest;
  let found: DefinitionComponent | null = null;
  for (;;) {
    const match = COMPONENT_STEP.exec(remaining);
    const next = match ? list[Number(match[1])] : undefined;
    if (!match || !next) return found;
    found = next;
    remaining = remaining.slice(match[0].length).replace(/^\./, "");
    list = next.type === "section" ? next.children : [];
  }
}

/**
 * The server's validation issues of this step's report, placed on the
 * component or rows source their path names. Paths follow schema v1 section 7:
 * `workflow.steps.<step id>.override.template.report.<path in the definition>`.
 */
export function reportIssuesByPlace(
  issues: unknown,
  stepId: string,
  definition: ReportDefinition,
): ReportIssueMap {
  const result: ReportIssueMap = { components: {}, datasets: {}, report: [] };
  if (!Array.isArray(issues) || !stepId) return result;
  const prefix = `workflow.steps.${stepId}.override.template.report`;
  for (const raw of issues) {
    const issue = record(raw);
    const path = typeof issue?.path === "string" ? issue.path : "";
    const message = typeof issue?.message === "string" ? issue.message : "";
    if (!message || (path !== prefix && !path.startsWith(`${prefix}.`))) continue;
    const rest = path.slice(prefix.length).replace(/^\./, "");
    const dataset = /^datasets\[(\d+)\]/.exec(rest);
    const datasetId = dataset ? definition.datasets?.[Number(dataset[1])]?.id : undefined;
    const component = componentAt(definition.components, rest);
    if (datasetId) (result.datasets[datasetId] ??= []).push(message);
    else if (component) (result.components[component.id] ??= []).push(message);
    else result.report.push(message);
  }
  return result;
}
