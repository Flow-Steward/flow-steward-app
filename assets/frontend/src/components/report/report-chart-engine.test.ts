import { beforeEach, describe, expect, it, vi } from "vitest";

import { mountReportChart } from "./report-chart-engine";

const instance = vi.hoisted(() => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }));

vi.mock("echarts/core", () => ({ init: vi.fn(() => instance), use: vi.fn() }));
vi.mock("echarts/charts", () => ({ BarChart: {}, LineChart: {} }));
vi.mock("echarts/components", () => ({
  AriaComponent: {},
  GridComponent: {},
  LegendComponent: {},
  TooltipComponent: {},
}));
vi.mock("echarts/renderers", () => ({ SVGRenderer: {} }));


describe("mountReportChart", () => {
  beforeEach(() => {
    instance.setOption.mockReset();
    instance.dispose.mockReset();
    instance.resize.mockReset();
  });

  it("disposes the chart it created when the option cannot be applied", () => {
    instance.setOption.mockImplementation(() => {
      throw new Error("bad option");
    });
    expect(() => mountReportChart(document.createElement("div"), {})).toThrow("bad option");
    expect(instance.dispose).toHaveBeenCalledTimes(1);
  });

  it("leaves a drawn chart to its caller and disposes it once on cleanup", () => {
    const mounted = mountReportChart(document.createElement("div"), {});
    expect(instance.setOption).toHaveBeenCalledTimes(1);
    expect(instance.dispose).not.toHaveBeenCalled();
    mounted.resize();
    mounted.dispose();
    expect(instance.resize).toHaveBeenCalledTimes(1);
    expect(instance.dispose).toHaveBeenCalledTimes(1);
  });
});
