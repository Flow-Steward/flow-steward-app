/**
 * ReportSnapshot v1 as the renderer reads it (docs/architecture/workflow-report-schema-v1.md).
 *
 * The server validates every snapshot strictly before it is stored; the
 * renderer only dispatches on (kind, schema_version) and stays defensive about
 * shape, so a broken value shows a notice instead of throwing.
 */

export type ReportScalar = string | number | boolean | null;

type FractionDigits = {
  minimum_fraction_digits?: number;
  maximum_fraction_digits?: number;
};

export type ReportFormat =
  | { type: "text" }
  | { type: "boolean" }
  | { type: "date" }
  | { type: "datetime" }
  | ({ type: "number" } & FractionDigits)
  | ({ type: "percent" } & FractionDigits)
  | ({ type: "currency"; currency: string } & FractionDigits);

export type ReportSegment = { text: string } | { value: ReportScalar; format: ReportFormat };

export type ReportTextComponent = {
  id: string;
  type: "text";
  style: "paragraph" | "heading";
  segments: ReportSegment[];
};

export type ReportMetricComponent = {
  id: string;
  type: "metric";
  label: string;
  value: number | null;
  format: ReportFormat;
  comparison?: { label: string; value: number | null; format: ReportFormat };
};

export type ReportField = { field: string; label: string; format: ReportFormat };

export type ReportTableColumn = ReportField & { id: string };

export type ReportTableComponent = {
  id: string;
  type: "table";
  caption: string;
  dataset: string;
  columns: ReportTableColumn[];
  empty_text: string;
};

export type ReportChartComponent = {
  id: string;
  type: "chart";
  chart_type: "line" | "bar";
  title: string;
  description: string;
  dataset: string;
  category: ReportField;
  series: Array<ReportField & { id: string }>;
  empty_text: string;
};

export type ReportSectionComponent = {
  id: string;
  type: "section";
  title: string;
  children: ReportComponent[];
};

export type ReportComponent =
  | ReportTextComponent
  | ReportMetricComponent
  | ReportTableComponent
  | ReportChartComponent
  | ReportSectionComponent;

export type ReportDataset = { id: string; fields: string[]; rows: ReportScalar[][] };

export type ReportSnapshot = {
  kind: "fs.report";
  schema_version: 1;
  title: string;
  locale: string;
  time_zone: string;
  summary_text: string;
  datasets: ReportDataset[];
  components: ReportComponent[];
};

export const REPORT_KIND = "fs.report";
export const SUPPORTED_REPORT_VERSIONS: readonly number[] = [1];

export type ReportReadResult =
  | { status: "ok"; report: ReportSnapshot }
  | { status: "unsupported" }
  | { status: "invalid" };

/** Thrown inside the walk when a shape is unreadable; never leaves this module. */
class ShapeError extends Error {
  constructor(readonly status: "invalid" | "unsupported") {
    super(status);
  }
}

const FORMAT_TYPES = new Set(["text", "boolean", "number", "currency", "percent", "date", "datetime"]);
/** Schema v1 nests sections at most four levels deep. */
const MAX_DEPTH = 4;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isScalar(value: unknown): boolean {
  return value === null || ["string", "number", "boolean"].includes(typeof value);
}

function expectShape(condition: boolean): void {
  if (!condition) throw new ShapeError("invalid");
}

function checkFormat(value: unknown): void {
  expectShape(isRecord(value) && typeof value.type === "string");
  const format = value as Record<string, unknown>;
  if (!FORMAT_TYPES.has(String(format.type))) throw new ShapeError("unsupported");
  if (format.type === "currency") expectShape(typeof format.currency === "string");
}

function checkField(value: unknown): void {
  expectShape(isRecord(value) && typeof value.field === "string" && typeof value.label === "string");
  checkFormat((value as Record<string, unknown>).format);
}

function checkSegments(value: unknown): void {
  expectShape(Array.isArray(value));
  for (const segment of value as unknown[]) {
    expectShape(isRecord(segment));
    const item = segment as Record<string, unknown>;
    if ("text" in item) {
      expectShape(typeof item.text === "string");
    } else {
      expectShape(isScalar(item.value));
      checkFormat(item.format);
    }
  }
}

type Fields = Record<string, unknown>;

function checkText(component: Fields): void {
  expectShape(component.style === "paragraph" || component.style === "heading");
  checkSegments(component.segments);
}

function checkNumberSlot(slot: Fields): void {
  expectShape(typeof slot.label === "string");
  expectShape(slot.value === null || typeof slot.value === "number");
  checkFormat(slot.format);
}

function checkMetric(component: Fields): void {
  checkNumberSlot(component);
  if (component.comparison !== undefined) {
    expectShape(isRecord(component.comparison));
    checkNumberSlot(component.comparison as Fields);
  }
}

function checkIdentifiedFields(value: unknown, minimum: number): void {
  expectShape(Array.isArray(value) && value.length >= minimum);
  for (const item of value as unknown[]) {
    checkField(item);
    expectShape(typeof (item as Fields).id === "string");
  }
}

function checkTable(component: Fields): void {
  expectShape(typeof component.caption === "string" && typeof component.dataset === "string");
  expectShape(typeof component.empty_text === "string");
  checkIdentifiedFields(component.columns, 1);
}

function checkChart(component: Fields): void {
  expectShape(component.chart_type === "line" || component.chart_type === "bar");
  expectShape(typeof component.title === "string" && typeof component.description === "string");
  expectShape(typeof component.dataset === "string" && typeof component.empty_text === "string");
  checkField(component.category);
  checkIdentifiedFields(component.series, 1);
}

function checkComponent(value: unknown, depth: number): void {
  expectShape(isRecord(value) && typeof value.id === "string" && typeof value.type === "string");
  const component = value as Fields;
  const checks: Record<string, (item: Fields) => void> = {
    text: checkText,
    metric: checkMetric,
    table: checkTable,
    chart: checkChart,
    section: (item) => {
      expectShape(typeof item.title === "string" && depth < MAX_DEPTH);
      checkComponents(item.children, depth + 1);
    },
  };
  const check = checks[String(component.type)];
  if (!check) throw new ShapeError("unsupported");
  check(component);
}

function checkComponents(value: unknown, depth: number): void {
  expectShape(Array.isArray(value));
  for (const component of value as unknown[]) checkComponent(component, depth);
}

function checkDatasets(value: unknown): void {
  expectShape(Array.isArray(value));
  for (const dataset of value as unknown[]) {
    expectShape(isRecord(dataset) && typeof dataset.id === "string");
    const item = dataset as Record<string, unknown>;
    expectShape(Array.isArray(item.fields) && (item.fields as unknown[]).every((field) => typeof field === "string"));
    const width = (item.fields as unknown[]).length;
    expectShape(Array.isArray(item.rows));
    for (const row of item.rows as unknown[]) {
      expectShape(Array.isArray(row) && row.length === width && row.every(isScalar));
    }
  }
}

/**
 * Dispatch on kind and version, then check the whole v1 shape before anything
 * renders: an unknown version or component type is "unsupported", a malformed
 * value is "invalid". The server validates strictly; this guard only keeps the
 * page from crashing on, or silently dropping part of, a value it cannot read.
 */
export function readReport(value: unknown): ReportReadResult {
  if (!isRecord(value) || value.kind !== REPORT_KIND) return { status: "invalid" };
  const version = value.schema_version;
  if (typeof version !== "number" || !SUPPORTED_REPORT_VERSIONS.includes(version)) {
    return { status: "unsupported" };
  }
  try {
    expectShape(typeof value.title === "string" && typeof value.locale === "string");
    expectShape(typeof value.time_zone === "string");
    checkDatasets(value.datasets);
    checkComponents(value.components, 1);
  } catch (error) {
    if (error instanceof ShapeError) return { status: error.status };
    throw error;
  }
  return { status: "ok", report: value as unknown as ReportSnapshot };
}

export function isNumericFormat(format: ReportFormat): boolean {
  return format.type === "number" || format.type === "currency" || format.type === "percent";
}
