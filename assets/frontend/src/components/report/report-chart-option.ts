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

/** Room for each extra value axis beyond the first on its side. */
const AXIS_OFFSET = 56;

type ChartUnits = {
  axes: Array<{ format: ReportFormat; name: string }>;
  /** The axis index of each series. */
  seriesAxis: number[];
};

/** The unit of a numeric format: a currency code, percent, or a plain number. */
function unitOf(format: ReportFormat): { key: string; name: string } {
  if (format.type === "currency") return { key: `currency:${format.currency}`, name: format.currency };
  if (format.type === "percent") return { key: "percent", name: "%" };
  return { key: "number", name: "" };
}

/**
 * One value axis per distinct unit, in the order series first use them. Series
 * that share a unit share an axis; a series is never drawn against another
 * unit's scale or labels.
 */
export function chartUnits(chart: ReportChartComponent): ChartUnits {
  const axes: ChartUnits["axes"] = [];
  const index = new Map<string, number>();
  const seriesAxis = chart.series.map((series) => {
    const unit = unitOf(series.format);
    let position = index.get(unit.key);
    if (position === undefined) {
      position = axes.length;
      index.set(unit.key, position);
      axes.push({ format: series.format, name: unit.name });
    }
    return position;
  });
  return { axes, seriesAxis };
}

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
  const units = chartUnits(chart);
  const showSymbol = dataset.rows.length <= LINE_SYMBOL_MAX_POINTS;
  return {
    animation: !theme.reducedMotion,
    color: theme.palette,
    textStyle: { color: theme.text },
    aria: { enabled: true, label: { description: chart.description } },
    // ECharts 6 keeps axis labels inside the grid by default (outerBounds).
    grid: {
      // Axes alternate left and right; each one past the first on a side is offset.
      left: 8 + Math.max(0, Math.ceil(units.axes.length / 2) - 1) * AXIS_OFFSET,
      right: 16 + Math.max(0, Math.floor(units.axes.length / 2) - 1) * AXIS_OFFSET,
      top: chart.series.length > 1 || units.axes.length > 1 ? 40 : 16,
      bottom: 8,
    },
    // Read-only: a legend click would hide a series, which is filtering (v1 has none).
    legend:
      chart.series.length > 1
        ? { type: "scroll", top: 0, selectedMode: false, textStyle: { color: theme.text } }
        : undefined,
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
    yAxis: units.axes.map((axis, index) => ({
      type: "value",
      position: index % 2 === 0 ? "left" : "right",
      offset: Math.floor(index / 2) * AXIS_OFFSET,
      // With one unit the values speak for themselves; with several, each axis
      // names its unit so a percent is never read against a currency.
      name: units.axes.length > 1 && axis.name ? axis.name : undefined,
      nameTextStyle: { color: theme.text },
      axisLabel: {
        color: theme.text,
        formatter: (value: number) => formatReportValue(value, axis.format, ctx),
      },
      splitLine: { show: index === 0, lineStyle: { color: theme.grid } },
    })),
    series: chart.series.map((series, index) => ({
      id: series.id,
      name: series.label,
      type: chart.chart_type,
      data: values[index].map(numericOrNull),
      yAxisIndex: units.seriesAxis[index],
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
