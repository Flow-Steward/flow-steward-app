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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Dispatch on kind and version; an unknown version is never read best-effort. */
export function readReport(value: unknown): ReportReadResult {
  if (!isRecord(value) || value.kind !== REPORT_KIND) return { status: "invalid" };
  const version = value.schema_version;
  if (typeof version !== "number" || !SUPPORTED_REPORT_VERSIONS.includes(version)) {
    return { status: "unsupported" };
  }
  if (
    typeof value.title !== "string" ||
    typeof value.locale !== "string" ||
    typeof value.time_zone !== "string" ||
    !Array.isArray(value.datasets) ||
    !Array.isArray(value.components)
  ) {
    return { status: "invalid" };
  }
  return { status: "ok", report: value as unknown as ReportSnapshot };
}

export function isNumericFormat(format: ReportFormat): boolean {
  return format.type === "number" || format.type === "currency" || format.type === "percent";
}
