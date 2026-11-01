import { connectorLiteralError, invalidConnectorLiteralNames } from "./detail-connector-literal";

describe("connector literal schema validation", () => {
  it.each([
    { value: "123", schema: { type: "string", pattern: "^(?P<id>[0-9]+)$" } },
    { value: "١٢٣", schema: { type: "string", pattern: String.raw`^\d+$` } },
    { value: "123", schema: { type: "string", not: { pattern: "^[0-9]+$" } } },
    { value: "123", schema: { type: "string", oneOf: [{ pattern: "^(?P<id>[0-9]+)$" }, { const: "123" }] } },
    { value: "123", schema: { type: "string", if: { pattern: "^[0-9]+$" }, then: { const: "other" }, else: { const: "123" } } },
    { value: '{"items":["123"]}', schema: { type: "object", properties: { items: { type: "array", items: { allOf: [{ pattern: "^(?P<id>[0-9]+)$" }] } } } } },
  ])("defers Python pattern evaluation to the backend for $schema", ({ value, schema }) => {
    const parameter = { name: "value", widget_type: schema.type === "object" ? "json" : "text", schema };
    expect(connectorLiteralError(parameter, value)).toBe("");
    expect(invalidConnectorLiteralNames({ value: `literal:${JSON.stringify(schema.type === "object" ? JSON.parse(value) : value)}` }, [parameter])).toEqual([]);
  });

  it.each([
    { type: "string", pattern: "^(?P<id>[0-9]+)$", minLength: 4 },
    { type: "string", allOf: [{ pattern: "^(?P<id>[0-9]+)$" }, { const: "other" }] },
    { type: "string", anyOf: [{ pattern: "^(?P<id>[0-9]+)$", maxLength: 2 }, { const: "other" }] },
    { type: "string", oneOf: [{ const: "123" }, { const: "123" }, { pattern: "^(?P<id>[0-9]+)$" }] },
    { type: "string", if: { pattern: "^(?P<id>[0-9]+)$" }, then: { const: "other" }, else: { const: "different" } },
  ])("still rejects values that definitely violate $schema", (schema) => {
    expect(connectorLiteralError({ widget_type: "text", schema }, "123")).toBe("schema");
  });

  it.each([
    { schema: { enum: [{ a: 1, nested: { x: true, y: [1, { p: "v", q: null }] } }] }, value: '{"nested":{"y":[1,{"q":null,"p":"v"}],"x":true},"a":1}' },
    { schema: { const: { a: 1, nested: { x: true, y: [1, { p: "v", q: null }] } } }, value: '{"nested":{"y":[1,{"q":null,"p":"v"}],"x":true},"a":1}' },
  ])("compares nested JSON objects by member values for $schema", ({ schema, value }) => {
    const parameter = { name: "value", widget_type: "json", schema };
    expect(connectorLiteralError(parameter, value)).toBe("");
    expect(invalidConnectorLiteralNames({ value: `literal:${value}` }, [parameter])).toEqual([]);
    expect(connectorLiteralError(parameter, '{"nested":{"y":[{"q":null,"p":"v"},1],"x":true},"a":1}')).toBe("schema");
  });

  it.each(["enum", "const"])("matches Python boolean-number equality inside %s without equating null and zero", (keyword) => {
    const expected = { items: [true, 0, null] };
    const parameter = { widget_type: "json", schema: { [keyword]: keyword === "enum" ? [expected] : expected } };
    expect(connectorLiteralError(parameter, '{"items":[1,false,null]}')).toBe("");
    expect(connectorLiteralError(parameter, '{"items":[1,false,0]}')).toBe("schema");
  });

  it.each([
    { schema: { type: "string", minLength: 1 }, text: "" },
    { schema: { type: "string", enum: ["ready"] }, text: "" },
    { schema: { type: "string", enum: [""] }, text: " " },
  ])("validates explicit optional text without trimming $schema", ({ schema, text }) => {
    const parameter = { name: "value", widget_type: "text", value_type: "text", schema };
    expect(connectorLiteralError(parameter, text)).toBe("schema");
    expect(invalidConnectorLiteralNames({ value: `literal:${JSON.stringify(text)}` }, [parameter])).toEqual(["value"]);
  });

  it("preserves unconstrained empty text and legacy typed blanks", () => {
    const text = { name: "text", widget_type: "text", value_type: "text", schema: { type: "string" } };
    const count = { name: "count", widget_type: "number", schema: { type: "number" } };
    expect(connectorLiteralError(text, "")).toBe("");
    expect(invalidConnectorLiteralNames({ text: 'literal:""', count: "literal:" }, [text, count])).toEqual([]);
  });
});
