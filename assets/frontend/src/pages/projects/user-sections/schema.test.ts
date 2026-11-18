import { buildInputFields, fieldKind, inputPaths, resolveSchemaPath, unsupportedRequiredInputs } from "./schema";
import type { JsonSchema } from "./types";

const schema: JsonSchema = {
  type: "object",
  properties: {
    receipt: { type: "object", "x-fs-type": "file" },
    note: { type: "string", format: "textarea", title: "Note" },
    currency: { type: "string", enum: ["EUR", "USD"] },
    amount: { type: "number" },
    tags: { type: "array", items: { type: "string" } },
    scans: { type: "array", items: { type: "object", "x-fs-type": "file" } },
    lines: { type: "array", items: { type: "object", properties: { sku: { type: "string" } } } },
    free: { type: "object", additionalProperties: true },
    nullable: { type: ["string", "null"] },
  },
  required: ["receipt", "free"],
};

describe("section schema", () => {
  it.each([
    ["receipt", "file"],
    ["note", "text_long"],
    ["currency", "enum"],
    ["amount", "number"],
    ["tags", "string_list"],
    ["scans", "file_list"],
    ["lines", "object_list"],
    ["lines[].sku", "text"],
    ["free", ""],
    ["nullable", ""],
  ])("reads %s as %s, like the server", (path, kind) => {
    expect(fieldKind(resolveSchemaPath(schema, path))).toBe(kind);
  });

  it("builds every renderable input when the section lists none", () => {
    const fields = buildInputFields(schema);
    expect(fields.map((field) => field.name)).toEqual(["receipt", "note", "currency", "amount", "tags", "lines"]);
    expect(fields[0]).toMatchObject({ kind: "file", required: true });
    expect(fields[1]).toMatchObject({ label: "Note", widget: "textarea" });
  });

  it("keeps the section's order, labels and allowed widgets only", () => {
    const fields = buildInputFields(schema, {
      fields: [
        { path: "amount", order: 2, label: "Total" },
        { path: "currency", order: 1, widget: "radio" },
        { path: "note", order: 0, widget: "checkbox" },
        { path: "tags", visible: false },
      ],
    });
    expect(fields.map((field) => [field.name, field.label, field.widget])).toEqual([
      ["note", "Note", "textarea"],
      ["currency", "currency", "radio"],
      ["amount", "Total", "number"],
    ]);
  });

  it("names required inputs the form cannot render", () => {
    expect(unsupportedRequiredInputs(schema)).toEqual(["free"]);
  });

  it("offers described nested input paths for columns", () => {
    expect(inputPaths(schema)).toContain("lines[].sku");
    expect(inputPaths(schema)).not.toContain("free");
  });
});
