/** Loads the chart engine on demand; a separate module so tests can replace the loader. */
import type * as ReportChartEngine from "./report-chart-engine";

export function loadReportChartEngine(): Promise<typeof ReportChartEngine> {
  return import("./report-chart-engine");
}
