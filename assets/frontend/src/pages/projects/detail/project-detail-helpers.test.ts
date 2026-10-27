import { parseWorkflowInputs } from "@/pages/projects/detail/project-detail-helpers";
import { interpolate } from "@/lib/i18n/interpolate";
import type { I18nContextValue } from "@/lib/i18n/context";

describe("declared structured workflow inputs", () => {
  it.each([
    ["string", "", "Laukas „Payload“ yra privalomas."],
    ["number", "text", "Lauke „Payload“ turi būti skaičius."],
    ["object", "[]", "Lauke „Payload“ turi būti JSON objektas."],
    ["array", "{}", "Lauke „Payload“ turi būti JSON masyvas."],
    ["object", "{", "Lauke „Payload“ turi būti tinkamas JSON."],
  ])("localizes %s validation without changing the authored label", (type, raw, error) => {
    const messages: Record<string, string> = {
      "react.projects.input_required": "Laukas „{field}“ yra privalomas.",
      "react.projects.input_number_required": "Lauke „{field}“ turi būti skaičius.",
      "react.projects.field_json_object_required": "Lauke „{field}“ turi būti JSON objektas.",
      "react.projects.field_json_array_required": "Lauke „{field}“ turi būti JSON masyvas.",
      "react.projects.field_valid_json_required": "Lauke „{field}“ turi būti tinkamas JSON.",
    };
    const t: I18nContextValue["t"] = (key, text, params) => interpolate(messages[key] || text || key, params);
    expect(parseWorkflowInputs([{ name: "payload", label: "Payload", type, required: true }], { payload: raw }, t))
      .toEqual({ inputs: {}, error });
  });
  it.each([
    ["object", "[]"], ["object", "null"], ["object", "42"],
    ["array", "{}"], ["array", "null"], ["array", '"text"'],
  ])("rejects a %s input with the wrong JSON shape %s", (type, raw) => {
    const result = parseWorkflowInputs([{ name: "payload", label: "Payload", type, required: true }], { payload: raw });
    expect(result.inputs).toEqual({});
    expect(result.error).toBe(`Payload must be a JSON ${type}.`);
  });

  it("preserves nested JSON types in declared input payloads", () => {
    const result = parseWorkflowInputs([
      { name: "record", type: "object" }, { name: "rows", type: "array" },
    ], {
      record: '{"count":0,"active":false,"missing":null,"nested":{"list":[1,true]}}',
      rows: '[0,false,null,{"name":"001"},[2]]',
    });
    expect(result).toEqual({ error: "", inputs: {
      record: { count: 0, active: false, missing: null, nested: { list: [1, true] } },
      rows: [0, false, null, { name: "001" }, [2]],
    } });
  });
});
