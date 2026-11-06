/**
 * The one place that loads ECharts, modularly: line and bar charts, the grid,
 * tooltip, legend and aria components, and the SVG renderer. Nothing else of the
 * library reaches a bundle. Tests replace this module.
 */
import { BarChart, LineChart } from "echarts/charts";
import { AriaComponent, GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { init, use as registerEChartsParts, type EChartsCoreOption } from "echarts/core";
import { SVGRenderer } from "echarts/renderers";

registerEChartsParts([
  BarChart,
  LineChart,
  AriaComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  SVGRenderer,
]);

export type MountedChart = {
  update: (option: EChartsCoreOption) => void;
  resize: () => void;
  dispose: () => void;
};

export function mountReportChart(element: HTMLElement, option: EChartsCoreOption): MountedChart {
  const chart = init(element, undefined, { renderer: "svg" });
  chart.setOption(option, true);
  return {
    update: (next) => chart.setOption(next, true),
    resize: () => chart.resize(),
    dispose: () => chart.dispose(),
  };
}
