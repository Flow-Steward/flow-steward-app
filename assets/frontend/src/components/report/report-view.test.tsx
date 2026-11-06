import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import fixture from "./__fixtures__/sales-report-snapshot.json";
import { ReportView } from "./report-view";

const mounted: Array<{ element: HTMLElement; option: unknown }> = [];

vi.mock("./report-chart-engine", () => ({
  mountReportChart: (element: HTMLElement, option: unknown) => {
    mounted.push({ element, option });
    return { update: vi.fn(), resize: vi.fn(), dispose: vi.fn() };
  },
}));

function snapshot(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(fixture)) as Record<string, unknown>;
}

describe("ReportView", () => {
  beforeEach(() => {
    mounted.length = 0;
  });

  it("renders the frozen snapshot: title, metrics, table, chart and section", async () => {
    render(<ReportView report={snapshot()} />);

    expect(screen.getByRole("heading", { level: 2, name: "Weekly sales" })).toBeInTheDocument();
    expect(screen.getByText("€48,210.50")).toBeInTheDocument();
    expect(screen.getByText("vs previous week: 12.3%")).toBeInTheDocument();
    // Zero is a value; null is shown as a dash.
    expect(within(screen.getByRole("group", { name: "Orders" })).getByText("0")).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "Refund rate" })).getByText("—")).toBeInTheDocument();
    expect(screen.getByText(/Target met:/)).toHaveTextContent("Target met: No. Data as of");

    const table = screen.getByRole("table", { name: "Revenue by region" });
    const headers = within(table).getAllByRole("columnheader").map((cell) => cell.textContent);
    expect(headers).toEqual(["Region", "Revenue", "Margin"]);
    const apac = within(table).getByRole("row", { name: /APAC/ });
    expect(within(apac).getByText("€0.00")).toBeInTheDocument();
    expect(within(apac).getByText("—")).toBeInTheDocument();

    expect(screen.getByRole("heading", { name: "Returns and notes" })).toBeInTheDocument();
    expect(screen.getByText("No returns this week.")).toBeInTheDocument();
    expect(screen.getByText("No commentary this week.")).toBeInTheDocument();

    const figure = screen.getByRole("figure", { name: "Revenue by region" });
    expect(within(figure).getByRole("img")).toHaveAccessibleName(
      "Bar chart of weekly revenue in euros for each sales region.",
    );
    await waitFor(() => expect(mounted).toHaveLength(1));
  });

  it("offers every chart value as a table", () => {
    render(<ReportView report={snapshot()} />);
    const figure = screen.getByRole("figure", { name: "Revenue by region" });
    expect(within(figure).getByText("Show data")).toBeInTheDocument();
    expect(within(figure).getByRole("table", { name: "Data: Revenue by region", hidden: true })).toBeInTheDocument();
  });

  it("renders report strings as text, never as markup", () => {
    const hostile = '<img src=x onerror="alert(1)">';
    const report = snapshot();
    report.title = hostile;
    render(<ReportView report={report} />);
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(hostile);
    // The only image is the chart canvas; a parsed <img> would be a second one.
    expect(screen.getAllByRole("img")).toHaveLength(1);
  });

  it("shows the empty text of a chart without rows and mounts no chart", () => {
    const report = snapshot();
    const datasets = report.datasets as Array<{ rows: unknown[] }>;
    datasets[0].rows = [];
    render(<ReportView report={report} />);
    expect(screen.getAllByText("No regional sales this week.").length).toBeGreaterThan(0);
    expect(mounted).toHaveLength(0);
  });

  it("refuses an unknown schema version instead of guessing", () => {
    render(<ReportView report={{ ...snapshot(), schema_version: 2 }} />);
    expect(screen.getByRole("status")).toHaveTextContent("This report uses a format this version cannot show.");
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });

  it("says when a value is not a report at all", () => {
    render(<ReportView report={{ title: "not a report" }} />);
    expect(screen.getByRole("status")).toHaveTextContent("This result is not a readable report.");
  });

  it.each([
    ["a null component", (report: Record<string, unknown>) => {
      report.components = [null];
    }],
    ["a section child without a type", (report: Record<string, unknown>) => {
      const components = report.components as Array<Record<string, unknown>>;
      const section = components.find((component) => component.type === "section") as Record<string, unknown>;
      Object.assign(section, { children: [{ id: "broken" }] });
    }],
    ["a row narrower than its fields", (report: Record<string, unknown>) => {
      const datasets = report.datasets as Array<{ rows: unknown[][] }>;
      datasets[0].rows[0] = ["EMEA"];
    }],
    ["a metric holding text", (report: Record<string, unknown>) => {
      const components = report.components as Array<Record<string, unknown>>;
      (components.find((component) => component.type === "metric") as Record<string, unknown>).value = "48210";
    }],
  ])("reports %s as unreadable instead of crashing or dropping it", (_name, breakIt) => {
    const report = snapshot();
    breakIt(report);
    render(<ReportView report={report} />);
    expect(screen.getByRole("status")).toHaveTextContent("This result is not a readable report.");
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });

  it("reports a component type it does not know as unsupported, not as empty", () => {
    const report = snapshot();
    (report.components as unknown[]).push({ id: "gauge", type: "gauge" });
    render(<ReportView report={report} />);
    expect(screen.getByRole("status")).toHaveTextContent("This report uses a format this version cannot show.");
  });
});
