import {
  EXPLICIT_EMPTY_TEST_VALUE,
  testRunInputBindingsFromRows,
  testRunPayloadFromInputs,
} from "@/pages/workflows/detail/detail-test-run";

/**
 * A Test Run must hand a step the same data a real run would. Text values lost
 * their line breaks (one-line field) and their edge spaces (trimmed), and an
 * empty text could not be told apart from a value that was not given.
 */
describe("Test Run values", () => {
  const rows = [
    { name: "body", type: "text", source: "state.outputs.body" },
    { name: "footer", type: "text", source: "state.outputs.footer" },
    { name: "count", type: "number", source: "state.outputs.count" },
    { name: "items", type: "array", source: "state.outputs.items" },
  ];

  it("sends a text exactly as typed, line breaks and edge spaces included", () => {
    const body = "  Первая строка\n\nВторая строка  ";
    const payload = testRunPayloadFromInputs(rows, { body });

    expect(payload.body).toBe(body);
    expect(testRunPayloadFromInputs(rows, { body: "   " }).body).toBe("   ");
  });

  it("leaves out an empty field, so the step sees the value as missing", () => {
    const payload = testRunPayloadFromInputs(rows, { body: "x", footer: "" });

    expect(payload).not.toHaveProperty("footer");
    expect(testRunInputBindingsFromRows(rows, { body: "x", footer: "" }).map((row) => row.name)).toEqual([
      "body",
    ]);
  });

  it("sends an empty text, and binds it, when it is marked as given", () => {
    const values = { body: "x", footer: EXPLICIT_EMPTY_TEST_VALUE };

    expect(testRunPayloadFromInputs(rows, values).footer).toBe("");
    expect(testRunInputBindingsFromRows(rows, values).map((row) => row.name)).toEqual(["body", "footer"]);
  });

  it("still reads numbers and JSON around spaces, and skips them when blank", () => {
    const payload = testRunPayloadFromInputs(rows, { count: " 0 ", items: " [] " });

    expect(payload.count).toBe(0);
    expect(payload.items).toEqual([]);
    expect(testRunPayloadFromInputs(rows, { count: "  " })).not.toHaveProperty("count");
  });

  it("does not send an empty text to an input that is no longer text", () => {
    // Empty text was set while footer was text; footer is an object now.
    const objectRows = [{ name: "footer", type: "object", source: "state.outputs.footer" }];
    const values = { footer: EXPLICIT_EMPTY_TEST_VALUE };

    expect(testRunPayloadFromInputs(objectRows, values)).not.toHaveProperty("footer");
    expect(testRunInputBindingsFromRows(objectRows, values)).toEqual([]);
  });
});
