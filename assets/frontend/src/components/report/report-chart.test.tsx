import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReportChart } from "./report-chart";
import { loadReportChartEngine } from "./report-chart-loader";
import type { ReportFormatContext } from "./report-format";
import { defaultReportLabels } from "./report-labels";
import type { ReportChartComponent, ReportDataset } from "./report-types";

vi.mock("./report-chart-loader", () => ({ loadReportChartEngine: vi.fn() }));

const context: ReportFormatContext = { locale: "en-US", timeZone: "UTC", labels: defaultReportLabels };

/** A series may be named "category": schema ids are author-chosen. */
const chart: ReportChartComponent = {
  id: "sales",
  type: "chart",
  chart_type: "bar",
  title: "Sales",
  description: "Bar chart of sales per region.",
  dataset: "regions",
  category: { field: "region", label: "Region", format: { type: "text" } },
  series: [{ id: "category", field: "revenue", label: "Revenue", format: { type: "number" } }],
  empty_text: "No sales.",
};

const dataset: ReportDataset = {
  id: "regions",
  fields: ["region", "revenue"],
  rows: [
    ["EMEA", 120],
    ["APAC", 80],
  ],
};

type Mounted = { update: ReturnType<typeof vi.fn>; resize: ReturnType<typeof vi.fn>; dispose: ReturnType<typeof vi.fn> };

function engine(mount: (element: HTMLElement, option: unknown) => Mounted) {
  return { mountReportChart: vi.fn(mount) } as unknown as Awaited<ReturnType<typeof loadReportChartEngine>>;
}

function mounted(): Mounted {
  return { update: vi.fn(), resize: vi.fn(), dispose: vi.fn() };
}

function renderChart() {
  return render(<ReportChart chart={chart} context={context} dataset={dataset} labels={defaultReportLabels} />);
}

describe("ReportChart", () => {
  beforeEach(() => {
    vi.mocked(loadReportChartEngine).mockReset();
  });

  afterEach(() => {
    document.documentElement.removeAttribute("data-theme");
  });

  it("keeps the category column distinct from a series whose id is category", () => {
    vi.mocked(loadReportChartEngine).mockResolvedValue(engine(() => mounted()));
    renderChart();
    const table = screen.getByRole("table", { name: "Data: Sales", hidden: true });
    const emea = within(table).getByRole("row", { name: /EMEA/, hidden: true });
    const cells = within(emea).getAllByRole("cell", { hidden: true });
    expect(cells[0]).toHaveTextContent("EMEA");
    expect(cells[1]).toHaveTextContent("120");
  });

  it("shows a notice and opens the values table when the engine fails to load", async () => {
    vi.mocked(loadReportChartEngine).mockRejectedValue(new Error("chunk failed"));
    renderChart();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(defaultReportLabels.chartUnavailable));
    expect(screen.getByRole("table", { name: "Data: Sales" })).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("shows a notice when drawing throws", async () => {
    vi.mocked(loadReportChartEngine).mockResolvedValue(
      engine(() => {
        throw new Error("setOption failed");
      }),
    );
    renderChart();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(defaultReportLabels.chartUnavailable));
    expect(screen.getByRole("table", { name: "Data: Sales" })).toBeInTheDocument();
  });

  it("redraws with the new theme when the page switches from light to dark", async () => {
    const first = mounted();
    const second = mounted();
    const loaded = engine(vi.fn<(element: HTMLElement, option: unknown) => Mounted>().mockReturnValueOnce(first).mockReturnValueOnce(second));
    vi.mocked(loadReportChartEngine).mockResolvedValue(loaded);
    renderChart();
    await waitFor(() => expect(loaded.mountReportChart).toHaveBeenCalledTimes(1));

    document.documentElement.setAttribute("data-theme", "dark");

    await waitFor(() => expect(loaded.mountReportChart).toHaveBeenCalledTimes(2));
    expect(first.dispose).toHaveBeenCalledTimes(1);
    expect(second.dispose).not.toHaveBeenCalled();
  });

  it("draws nothing and disposes nothing when unmounted before the engine loads", async () => {
    let resolve: (value: Awaited<ReturnType<typeof loadReportChartEngine>>) => void = () => undefined;
    vi.mocked(loadReportChartEngine).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const loaded = engine(() => mounted());
    const { unmount } = renderChart();
    unmount();
    resolve(loaded);
    await Promise.resolve();
    await Promise.resolve();
    expect(loaded.mountReportChart).not.toHaveBeenCalled();
  });
});
