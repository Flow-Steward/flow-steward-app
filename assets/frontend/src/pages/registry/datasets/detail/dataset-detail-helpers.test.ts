import { normalizeSchemaSnapshot, paramsSchemaFromFields, queryParameterFields } from "./dataset-detail-helpers";

describe("saved query parameter schema helpers", () => {
  it("extracts placeholders only from SQL code", () => {
    const fields = queryParameterFields(
      "SELECT ':literal' AS note -- :comment\nFROM events WHERE event = :event /* :block */",
      {},
    );

    expect(fields).toEqual([
      { name: "event", type: "string", format: "", required: true },
    ]);
  });

  it("preserves optional existing SQL properties while requiring new placeholders", () => {
    const fields = queryParameterFields(
      "SELECT event FROM events WHERE event = :event",
      { type: "object", properties: { event: { type: "string" } } },
    );

    expect(fields).toEqual([
      { name: "event", type: "string", format: "", required: false },
    ]);
    expect(paramsSchemaFromFields(fields)).toEqual({
      type: "object",
      properties: { event: { type: "string" } },
    });
    expect(queryParameterFields("SELECT :new_value", {})[0]?.required).toBe(true);
  });

  it("coerces unsupported runtime parameter types to text", () => {
    const fields = queryParameterFields(
      "SELECT event FROM events WHERE event = :event",
      { type: "object", properties: { event: { type: "array" } }, required: ["event"] },
    );

    expect(fields[0]?.type).toBe("string");
  });

  it("deduplicates schema resources by their fully qualified identity", () => {
    expect(normalizeSchemaSnapshot({
      resources: [
        { resource_id: "public.account", columns: [{ name: "id", type: "integer" }] },
        { resource_id: "public.account", columns: [{ name: "id", type: "integer" }] },
      ],
    }).resources).toHaveLength(1);
  });
});
