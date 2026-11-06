/**
 * The few interface strings the report renderer shows itself.
 *
 * The renderer takes them from its host instead of calling the app's i18n: the
 * MCP Apps bundle runs in a sandboxed frame that cannot load the app catalog.
 * Report content (titles, labels, empty texts) comes from the snapshot.
 */
export type ReportLabels = {
  /** Shown for a null value. */
  empty: string;
  yes: string;
  no: string;
  /** The disclosure that reveals a chart's values as a table. */
  showData: string;
  /** Caption of that table; `{title}` is replaced by the chart title. */
  chartDataCaption: string;
  /** A report written in a schema version this build cannot show. */
  unsupported: string;
  /** A value that is not a report at all. */
  invalid: string;
};

export const defaultReportLabels: ReportLabels = {
  empty: "—",
  yes: "Yes",
  no: "No",
  showData: "Show data",
  chartDataCaption: "Data: {title}",
  unsupported: "This report uses a format this version cannot show.",
  invalid: "This result is not a readable report.",
};
