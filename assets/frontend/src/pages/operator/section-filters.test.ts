import { fallbackContext } from "@/lib/i18n/context";

import { operatorFilterFields, operatorFilterKey, operatorFiltersParam } from "./section-filters";
import type { OperatorFilterView } from "./types";

const filters: OperatorFilterView[] = [
  { id: "input.supplier", source: "input", path: "supplier", type: "search", label: "Supplier" },
  { id: "output.total", source: "output", path: "total", type: "number_range", label: "Total" },
  { id: "input.document_date", source: "input", path: "document_date", type: "date_range", label: "Date" },
  { id: "input.urgent", source: "input", path: "urgent", type: "boolean", label: "Urgent" },
  { id: "input.doc_type", source: "input", path: "doc_type", type: "choice", label: "Type", options: ["invoice", "contract"] },
  { id: "input.tags", source: "input", path: "tags", type: "choice_multi", label: "Tags", options: ["a", "b", "c"] },
  { id: "job.status", source: "job", path: "status", type: "choice_multi", label: "Status", options: ["queued", "completed"] },
];

describe("operator filters", () => {
  it("offers each configured filter in the panel with the right kind of value", () => {
    const fields = operatorFilterFields(filters, fallbackContext.t);
    expect(fields.map((field) => [field.key, field.input ?? (field.multiple ? "multi" : "choice")])).toEqual([
      ["f.input.supplier", "text"],
      ["f.output.total", "number_range"],
      ["f.input.document_date", "date_range"],
      ["f.input.urgent", "choice"],
      ["f.input.doc_type", "choice"],
      ["f.input.tags", "multi"],
      ["f.job.status", "multi"],
    ]);
    expect(fields[3].options.map((option) => option.value)).toEqual(["true", "false"]);
    expect(fields[4].options.map((option) => option.value)).toEqual(["invoice", "contract"]);
  });

  it("serializes panel values to the API's filters shape", () => {
    const values = {
      [operatorFilterKey(filters[0])]: " acme ",
      [operatorFilterKey(filters[1])]: "10..250.5",
      [operatorFilterKey(filters[2])]: "2026-01-01..",
      [operatorFilterKey(filters[3])]: "false",
      [operatorFilterKey(filters[4])]: "invoice",
      [operatorFilterKey(filters[5])]: "a,c",
      [operatorFilterKey(filters[6])]: "",
    };
    expect(operatorFiltersParam(filters, values)).toEqual({
      "input.supplier": "acme",
      "output.total": { min: 10, max: 250.5 },
      "input.document_date": { from: "2026-01-01" },
      "input.urgent": false,
      "input.doc_type": "invoice",
      "input.tags": ["a", "c"],
    });
  });

  it("sends no filters when none holds a value", () => {
    expect(operatorFiltersParam(filters, { [operatorFilterKey(filters[1])]: ".." })).toBeNull();
    expect(operatorFiltersParam(filters, {})).toBeNull();
  });
});
