import { describe, expect, it } from "vitest";

import {
  bindingPossible,
  componentIds,
  componentProblems,
  formatOfType,
  freeId,
  moveItem,
  newComponent,
  readDefinition,
  reportIssuesByPlace,
  rowFieldSuggestions,
  withDatasets,
  without,
  type DefinitionComponent,
  type ReportDefinition,
} from "./detail-template-report-model";

const inputs = [
  { name: "kpis", type: "object" },
  { name: "regions", type: "array" },
  { name: "title", type: "text" },
];

describe("report definition model", () => {
  it("gives new components ids that are free across sections", () => {
    const components: DefinitionComponent[] = [
      { id: "metric_1", type: "metric", label: "A", value: { input: "kpis", path: "a" }, format: { type: "number" } },
      {
        id: "section_1",
        type: "section",
        title: "S",
        children: [{ id: "metric_2", type: "metric", label: "B", value: { input: "kpis", path: "b" }, format: { type: "number" } }],
      },
    ];
    const taken = componentIds(components);
    expect(taken).toEqual(["metric_1", "section_1", "metric_2"]);
    expect(newComponent("metric", taken, "").id).toBe("metric_3");
    expect(freeId("rows", [])).toBe("rows_1");
  });

  it("keeps fields the editor does not show when it reads a stored definition", () => {
    const stored = {
      schema_version: 1,
      title: "Weekly sales",
      locale: "en-US",
      time_zone: "UTC",
      summary: [{ text: "Hello" }],
      components: [{ id: "m", type: "metric", label: "R", value: { input: "kpis", path: "r", default: 0 }, format: { type: "number" } }],
    };
    expect(readDefinition(stored)).toBe(stored);
    expect(readDefinition("not a definition").components).toEqual([]);
  });

  it("writes datasets only when there is one", () => {
    const definition: ReportDefinition = { ...readDefinition(null), datasets: [{ id: "rows_1", source: { input: "regions" } }] };
    expect(withDatasets(definition, [])).not.toHaveProperty("datasets");
    expect(withDatasets(definition, definition.datasets ?? []).datasets).toHaveLength(1);
  });

  it("drops keys without leaving them undefined", () => {
    expect(without({ input: "kpis", path: "a" }, "path")).toEqual({ input: "kpis" });
    expect(Object.keys(without({ input: "kpis", path: "a" }, "path"))).toEqual(["input"]);
  });

  it("keeps a currency and its digits across format changes that allow them", () => {
    expect(formatOfType("percent", { type: "currency", currency: "USD", maximum_fraction_digits: 1 })).toEqual({
      type: "percent",
      maximum_fraction_digits: 1,
    });
    expect(formatOfType("currency", { type: "currency", currency: "USD" })).toEqual({ type: "currency", currency: "USD" });
    expect(formatOfType("date", { type: "number", maximum_fraction_digits: 2 })).toEqual({ type: "date" });
  });

  it.each([
    ["text", false, false, true],
    ["text", false, true, false],
    ["object", false, false, false],
    ["object", false, true, true],
    ["array", true, false, true],
    ["object", true, true, true],
    ["number", true, false, false],
  ])("input %s, dataset %s, path %s: possible %s", (type, dataset, hasPath, possible) => {
    expect(bindingPossible(type, { dataset, hasPath })).toBe(possible);
  });

  it("names the problems of a component next to it", () => {
    const metric: DefinitionComponent = { id: "m", type: "metric", label: "", value: { input: "kpis" }, format: { type: "number" } };
    expect(componentProblems(metric, inputs, [])).toEqual(["label_missing", "binding_impossible"]);
    const table: DefinitionComponent = { id: "t", type: "table", caption: "C", dataset: "rows_9", columns: [], empty_text: "" };
    expect(componentProblems(table, inputs, ["rows_1"])).toEqual(["dataset_unknown", "columns_missing"]);
  });

  it("moves items within bounds", () => {
    expect(moveItem(["a", "b", "c"], 2, -1)).toEqual(["a", "c", "b"]);
    expect(moveItem(["a", "b"], 0, -1)).toEqual(["a", "b"]);
  });

  it("suggests the row fields a producing step declares, one level below the source", () => {
    const sources = [
      { name: "regions", source: "state.outputs.regions" },
      { name: "kpis", source: "state.outputs.kpis" },
    ];
    const paths = [
      "state.outputs.regions",
      "state.outputs.regions.region",
      "state.outputs.regions.revenue",
      "state.outputs.regions.region.code",
      "state.outputs.kpis.rows.region",
      "state.outputs.other.region",
    ];
    expect(rowFieldSuggestions({ id: "rows_1", source: { input: "regions" } }, sources, paths)).toEqual([
      "region",
      "revenue",
    ]);
    expect(rowFieldSuggestions({ id: "rows_2", source: { input: "kpis", path: "rows" } }, sources, paths)).toEqual([
      "region",
    ]);
    expect(rowFieldSuggestions({ id: "rows_3", source: { input: "unknown" } }, sources, paths)).toEqual([]);
  });

  it("places server issues on the component, rows source or report they name", () => {
    const definition: ReportDefinition = {
      schema_version: 1,
      title: "R",
      locale: "en-US",
      time_zone: "UTC",
      datasets: [{ id: "regions", source: { input: "regions" } }],
      components: [
        { id: "total", type: "metric", label: "T", value: { input: "kpis" }, format: { type: "number" } },
        {
          id: "details",
          type: "section",
          title: "D",
          children: [{ id: "inner", type: "metric", label: "I", value: { input: "kpis" }, format: { type: "number" } }],
        },
      ],
    };
    const base = "workflow.steps.build.override.template.report";
    const issues = [
      { path: `${base}.components[0].value.input`, message: "unknown input" },
      { path: `${base}.components[1].children[0].format`, message: "bad format" },
      { path: `${base}.datasets[0].source`, message: "not rows" },
      { path: `${base}.locale`, message: "bad locale" },
      { path: "workflow.steps.other.override.template.report.locale", message: "other step" },
      { path: `${base}.components[9]`, message: "gone" },
    ];
    expect(reportIssuesByPlace(issues, "build", definition)).toEqual({
      components: { total: ["unknown input"], inner: ["bad format"] },
      datasets: { regions: ["not rows"] },
      report: ["bad locale", "gone"],
    });
  });
});
