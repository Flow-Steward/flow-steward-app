import { describe, expect, it } from "vitest";

import { buildReportChartOption } from "./report-chart-option";
import type { ReportFormatContext } from "./report-format";
import { defaultReportLabels } from "./report-labels";
import type { ReportChartComponent, ReportDataset } from "./report-types";

const ctx: ReportFormatContext = { locale: "en-US", timeZone: "UTC", labels: defaultReportLabels };

const hostile = '<img src=x onerror="alert(1)">';

/** The hostile string as a literal pattern: its parentheses are not a group. */
function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

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
    expect(element).toHaveTextContent(new RegExp(escapeRegExp(hostile)));
    expect(element).toHaveTextContent(/€10\.00/);
  });

  it("uses only the line and bar features: no zoom, toolbox or custom graphics", () => {
    for (const key of ["dataZoom", "toolbox", "graphic", "brush", "visualMap"]) {
      expect(Object.keys(option)).not.toContain(key);
    }
  });
});

describe("value axes and legend", () => {
  const mixed: ReportChartComponent = {
    ...chart,
    chart_type: "bar",
    series: [
      { id: "revenue", field: "revenue", label: "Revenue", format: { type: "currency", currency: "EUR" } },
      { id: "margin", field: "margin", label: "Margin", format: { type: "percent", maximum_fraction_digits: 0 } },
      { id: "cost", field: "cost", label: "Cost", format: { type: "currency", currency: "EUR" } },
    ],
  };
  const rows: ReportDataset = {
    id: "regions",
    fields: ["region", "revenue", "margin", "cost"],
    rows: [["EMEA", 1000, 0.18, 400]],
  };
  type AxisOption = {
    yAxis: Array<{ name?: string; position: string; axisLabel: { formatter: (value: number) => string } }>;
    series: Array<{ yAxisIndex: number }>;
    legend: { selectedMode: boolean };
  };
  const option = buildReportChartOption(mixed, rows, ctx) as unknown as AxisOption;

  it("gives each unit its own named axis and never labels a percent as a currency", () => {
    expect(option.yAxis).toHaveLength(2);
    expect(option.yAxis.map((axis) => axis.name)).toEqual(["EUR", "%"]);
    expect(option.yAxis.map((axis) => axis.position)).toEqual(["left", "right"]);
    expect(option.series.map((series) => series.yAxisIndex)).toEqual([0, 1, 0]);
    expect(option.yAxis[0].axisLabel.formatter(1000)).toBe("€1,000.00");
    expect(option.yAxis[1].axisLabel.formatter(0.18)).toBe("18%");
  });

  it("keeps one unnamed axis when every series shares a unit", () => {
    const single = buildReportChartOption(chart, dataset, ctx) as unknown as AxisOption;
    expect(single.yAxis).toHaveLength(1);
    expect(single.yAxis[0].name).toBeUndefined();
  });

  it("shows a legend that cannot hide series", () => {
    expect(option.legend.selectedMode).toBe(false);
  });
});
