import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ReportDefinitionEditor } from "./detail-template-report-editor";
import type { ReportDefinition } from "./detail-template-report-model";

const inputs = [
  { name: "kpis", type: "object" },
  { name: "regions", type: "array" },
];

/** A definition as the AI Builder writes it, with fields the editor does not show. */
const stored: ReportDefinition = {
  schema_version: 1,
  title: "Weekly sales",
  locale: "en-US",
  time_zone: "Europe/Berlin",
  summary: [{ text: "Revenue " }, { value: { input: "kpis", path: "revenue" }, format: { type: "currency", currency: "EUR" } }],
  datasets: [{ id: "regions", source: { input: "regions" } }],
  components: [
    {
      id: "revenue_total",
      type: "metric",
      label: "Revenue",
      value: { input: "kpis", path: "revenue", default: 0 },
      format: { type: "currency", currency: "EUR", maximum_fraction_digits: 2 },
    },
    {
      id: "region_table",
      type: "table",
      caption: "Revenue by region",
      dataset: "regions",
      columns: [{ id: "region", field: "region", label: "Region", format: { type: "text" } }],
      empty_text: "No sales.",
    },
  ],
};

function Harness({ initial, onChange }: Readonly<{ initial: ReportDefinition; onChange: (next: ReportDefinition) => void }>) {
  const [value, setValue] = useState<ReportDefinition>(initial);
  return (
    <ReportDefinitionEditor
      inputClass=""
      inputs={inputs}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
      value={value}
    />
  );
}

function last(spy: ReturnType<typeof vi.fn>): ReportDefinition {
  return spy.mock.calls.at(-1)?.[0] as ReportDefinition;
}

describe("ReportDefinitionEditor", () => {
  it("round-trips a stored definition: an edit changes only what was edited", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial={structuredCloneSafe(stored)} onChange={onChange} />);

    await user.type(screen.getByRole("textbox", { name: "Report title" }), "!");

    expect(last(onChange)).toEqual({ ...stored, title: "Weekly sales!" });
  });

  it("builds a metric, a rows source and a table only through the form", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const empty: ReportDefinition = { schema_version: 1, title: "R", locale: "en-US", time_zone: "UTC", components: [] };
    render(<Harness initial={empty} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Add rows source" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Rows from rows_1" }), "regions");

    await user.selectOptions(screen.getByRole("combobox", { name: "Component" }), "metric");
    await user.click(screen.getByRole("button", { name: "Add component" }));
    await user.type(screen.getByRole("textbox", { name: "Label" }), "Revenue");
    await user.selectOptions(screen.getByRole("combobox", { name: "Value" }), "kpis");
    await user.type(screen.getByRole("textbox", { name: "Value: Path inside the input (optional)" }), "revenue");

    await user.selectOptions(screen.getByRole("combobox", { name: "Component" }), "table");
    await user.click(screen.getByRole("button", { name: "Add component" }));
    await user.click(screen.getByRole("button", { name: "Add column" }));
    await user.type(screen.getByRole("textbox", { name: "Field of 1" }), "region");

    const result = last(onChange);
    expect(result.datasets).toEqual([{ id: "rows_1", source: { input: "regions" } }]);
    expect(result.components[0]).toEqual({
      id: "metric_1",
      type: "metric",
      label: "Revenue",
      value: { input: "kpis", path: "revenue" },
      format: { type: "number" },
    });
    expect(result.components[1]).toMatchObject({ id: "table_1", type: "table", dataset: "rows_1" });
    expect((result.components[1] as { columns: Array<{ field: string }> }).columns[0].field).toBe("region");
  });

  it("puts a component's problems next to it", async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ ...stored, components: [] }} onChange={vi.fn()} />);
    await user.selectOptions(screen.getByRole("combobox", { name: "Component" }), "chart");
    await user.click(screen.getByRole("button", { name: "Add component" }));
    const chart = screen.getByRole("listitem", { name: "Chart chart_1" });
    expect(within(chart).getByRole("status")).toHaveTextContent("Add at least one series.");
  });

  it("reorders components with the keyboard-reachable buttons", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial={structuredCloneSafe(stored)} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Move Table region_table up" }));
    expect(last(onChange).components.map((component) => component.id)).toEqual(["region_table", "revenue_total"]);
  });

  it("shows the server's last issues next to the component and offers known row fields", () => {
    render(
      <ReportDefinitionEditor
        inputClass=""
        inputs={[
          { name: "kpis", type: "object", source: "state.outputs.kpis" },
          { name: "regions", type: "array", source: "state.outputs.regions" },
        ]}
        onChange={vi.fn()}
        stepId="build"
        validationIssues={[
          {
            path: "workflow.steps.build.override.template.report.components[0].value.path",
            message: "kpis.revenue is missing and has no default",
          },
        ]}
        value={stored}
        variablePaths={["state.outputs.regions.region", "state.outputs.regions.revenue"]}
      />,
    );
    const metric = screen.getByRole("listitem", { name: "Key number revenue_total" });
    expect(within(metric).getByRole("status")).toHaveTextContent("kpis.revenue is missing and has no default");
    const table = screen.getByRole("listitem", { name: "Table region_table" });
    expect(within(table).getByRole("combobox", { name: "Field of Region" })).toHaveAttribute("list");
  });
});

function structuredCloneSafe<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}
