/**
 * The ECharts option of one report chart, built from the snapshot only.
 *
 * No option comes from report data except values and labels: a report holds no
 * chart library options (ADR 0059 decision 8). Labels reach the chart as SVG
 * text, and the tooltip is built from DOM nodes with textContent, so no string
 * from a report is ever parsed as HTML.
 */
import type { EChartsCoreOption } from "echarts/core";

import { formatReportValue, type ReportFormatContext } from "./report-format";
import type { ReportChartComponent, ReportDataset, ReportFormat, ReportScalar } from "./report-types";

export type ReportChartTheme = {
  palette: string[];
  text: string;
  grid: string;
  reducedMotion: boolean;
};

export const FALLBACK_CHART_THEME: ReportChartTheme = {
  palette: ["#1268c9", "#1b8dba", "#2ba38c", "#57c79e"],
  text: "#4a524e",
  grid: "#dde2df",
  reducedMotion: false,
};

/** Line markers stay readable up to this many points, then only the line is drawn. */
const LINE_SYMBOL_MAX_POINTS = 60;

type TooltipItem = { seriesIndex?: number; dataIndex?: number };

function column(dataset: ReportDataset, field: string): ReportScalar[] {
  const index = dataset.fields.indexOf(field);
  return dataset.rows.map((row) => (index >= 0 ? (row[index] ?? null) : null));
}

function numericOrNull(value: ReportScalar): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function tooltipElement(
  items: TooltipItem[],
  chart: ReportChartComponent,
  categories: string[],
  values: ReportScalar[][],
  ctx: ReportFormatContext,
): HTMLElement {
  const box = document.createElement("div");
  box.className = "fs-report-tooltip";
  const first = items[0];
  const dataIndex = first?.dataIndex ?? 0;
  const heading = document.createElement("div");
  heading.className = "fs-report-tooltip-title";
  heading.textContent = categories[dataIndex] ?? "";
  box.append(heading);
  for (const item of items) {
    const seriesIndex = item.seriesIndex ?? 0;
    const series = chart.series[seriesIndex];
    if (!series) continue;
    const line = document.createElement("div");
    line.textContent = `${series.label}: ${formatReportValue(
      values[seriesIndex]?.[item.dataIndex ?? 0] ?? null,
      series.format,
      ctx,
    )}`;
    box.append(line);
  }
  return box;
}

export function buildReportChartOption(
  chart: ReportChartComponent,
  dataset: ReportDataset,
  ctx: ReportFormatContext,
  theme: ReportChartTheme = FALLBACK_CHART_THEME,
): EChartsCoreOption {
  const categories = column(dataset, chart.category.field).map((value) =>
    formatReportValue(value, chart.category.format, ctx),
  );
  const values = chart.series.map((series) => column(dataset, series.field));
  const axisFormat: ReportFormat = chart.series[0]?.format ?? { type: "number" };
  const showSymbol = dataset.rows.length <= LINE_SYMBOL_MAX_POINTS;
  return {
    animation: !theme.reducedMotion,
    color: theme.palette,
    textStyle: { color: theme.text },
    aria: { enabled: true, label: { description: chart.description } },
    // ECharts 6 keeps axis labels inside the grid by default (outerBounds).
    grid: { left: 8, right: 16, top: chart.series.length > 1 ? 36 : 16, bottom: 8 },
    legend: chart.series.length > 1 ? { type: "scroll", top: 0, textStyle: { color: theme.text } } : undefined,
    tooltip: {
      trigger: "axis",
      confine: true,
      formatter: (params: unknown) =>
        tooltipElement(
          (Array.isArray(params) ? params : [params]) as TooltipItem[],
          chart,
          categories,
          values,
          ctx,
        ),
    },
    xAxis: {
      type: "category",
      data: categories,
      axisLabel: { hideOverlap: true, color: theme.text },
      axisLine: { lineStyle: { color: theme.grid } },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        color: theme.text,
        formatter: (value: number) => formatReportValue(value, axisFormat, ctx),
      },
      splitLine: { lineStyle: { color: theme.grid } },
    },
    series: chart.series.map((series, index) => ({
      id: series.id,
      name: series.label,
      type: chart.chart_type,
      data: values[index].map(numericOrNull),
      // A null is a gap in a line and a missing bar, never a zero.
      connectNulls: false,
      ...(chart.chart_type === "line" ? { showSymbol } : {}),
    })),
  };
}

const PALETTE_TOKENS = [
  "--fs-chart-seq-1",
  "--fs-chart-seq-2",
  "--fs-chart-seq-3",
  "--fs-chart-seq-4",
  "--fs-chart-positive",
  "--fs-chart-warning",
];

/** Chart colours from the design tokens, so light and dark themes both apply. */
export function readChartTheme(element: HTMLElement): ReportChartTheme {
  const style = getComputedStyle(element);
  const token = (name: string) => style.getPropertyValue(name).trim();
  const palette = PALETTE_TOKENS.map(token).filter(Boolean);
  return {
    palette: palette.length ? palette : FALLBACK_CHART_THEME.palette,
    text: token("--fs-text-secondary") || FALLBACK_CHART_THEME.text,
    grid: token("--fs-border-subtle") || FALLBACK_CHART_THEME.grid,
    reducedMotion:
      typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  };
}
