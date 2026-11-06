import { describe, expect, it } from "vitest";

import { buildReportChartOption } from "./report-chart-option";
import type { ReportFormatContext } from "./report-format";
import { defaultReportLabels } from "./report-labels";
import type { ReportChartComponent, ReportDataset } from "./report-types";

const ctx: ReportFormatContext = { locale: "en-US", timeZone: "UTC", labels: defaultReportLabels };

const hostile = '<img src=x onerror="alert(1)">';

const chart: ReportChartComponent = {
  id: "region_chart",
  type: "chart",
  chart_type: "line",
  title: "Revenue",
  description: "Line chart of revenue per region.",
  dataset: "regions",
  category: { field: "region", label: "Region", format: { type: "text" } },
  series: [{ id: "revenue", field: "revenue", label: hostile, format: { type: "currency", currency: "EUR" } }],
  empty_text: "No data",
};

const dataset: ReportDataset = {
  id: "regions",
  fields: ["region", "revenue"],
  rows: [
    [hostile, 10],
    ["APAC", null],
    ["AMER", 0],
  ],
};

type Option = {
  series: Array<{ data: Array<number | null>; connectNulls: boolean }>;
  xAxis: { data: string[] };
  aria: { label: { description: string } };
  tooltip: { formatter: (params: unknown) => HTMLElement };
};

describe("buildReportChartOption", () => {
  const option = buildReportChartOption(chart, dataset, ctx) as unknown as Option;

  it("keeps nulls as gaps and zero as a value, in dataset order", () => {
    expect(option.series[0].data).toEqual([10, null, 0]);
    expect(option.series[0].connectNulls).toBe(false);
    expect(option.xAxis.data).toEqual([hostile, "APAC", "AMER"]);
  });

  it("describes the chart for assistive technology", () => {
    expect(option.aria.label.description).toBe(chart.description);
  });

  it("builds the tooltip from text nodes, never from report HTML", () => {
    const element = option.tooltip.formatter([{ seriesIndex: 0, dataIndex: 0 }]);
    expect(element.querySelector("img")).toBeNull();
    expect(element).toHaveTextContent(new RegExp(hostile));
    expect(element).toHaveTextContent(/€10\.00/);
  });

  it("uses only the line and bar features: no zoom, toolbox or custom graphics", () => {
    for (const key of ["dataZoom", "toolbox", "graphic", "brush", "visualMap"]) {
      expect(Object.keys(option)).not.toContain(key);
    }
  });
});
