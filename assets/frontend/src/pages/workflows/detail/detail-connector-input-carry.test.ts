import { carryConnectorInputMapping } from "./detail-connector-input-carry";

const text = (name: string) => ({ name, value_type: "text" });

describe("carrying a step's input mapping to another action", () => {
  it("drops inputs the next action does not declare and names only the filled ones", () => {
    const carried = carryConnectorInputMapping(
      {
        ship_from: "state.warehouse",
        amazon_order_id: "state.order_id",
        ship_date: "",
        items: "state.items",
      },
      [{ name: "items", type: "array", required: true }, { name: "source_address", type: "object", required: true }],
    );
    expect(carried.kept).toEqual({ items: "state.items" });
    expect(carried.dropped).toEqual(["ship_from", "amazon_order_id"]);
  });

  it("keeps a shared input byte for byte, including a typed literal", () => {
    const previous = {
      msku: 'literal:"MUG-1"',
      quantity: "literal:5",
      currency_code: 'literal:"GBP"',
    };
    const carried = carryConnectorInputMapping(previous, [
      text("msku"),
      { name: "quantity", type: "integer" },
    ]);
    expect(carried.kept).toEqual({ msku: 'literal:"MUG-1"', quantity: "literal:5" });
    expect(carried.dropped).toEqual(["currency_code"]);
  });

  it("drops a literal the next action's input of the same name would refuse", () => {
    const carried = carryConnectorInputMapping(
      { quantity: 'literal:"many"' },
      [{ name: "quantity", type: "integer" }],
    );
    expect(carried.kept).toEqual({});
    expect(carried.dropped).toEqual(["quantity"]);
  });

  it("keeps bare optional typed placeholders empty without a dropped-value notice", () => {
    const parameters = [
      { name: "note", type: "integer" },
      { name: "note", type: "json" },
      { name: "note", value_type: "text", widget_type: "select", schema: { type: "string", enum: ["ready"] } },
    ];
    expect(parameters.map((parameter) => carryConnectorInputMapping({ note: "literal:" }, [parameter])))
      .toEqual(parameters.map(() => ({ kept: {}, dropped: [] })));
  });

  it("preserves explicit empty, whitespace and state-like string literals byte for byte", () => {
    const values = ['literal:""', 'literal:"   "', 'literal:"state."'];
    expect(values.map((note) => carryConnectorInputMapping({ note }, [text("note")])))
      .toEqual(values.map((note) => ({ kept: { note }, dropped: [] })));
  });

  it("still ignores empty state placeholders and validates an explicit literal's next schema", () => {
    expect(carryConnectorInputMapping({ note: "state." }, [text("note")]))
      .toEqual({ kept: {}, dropped: [] });
    expect(carryConnectorInputMapping({ note: "" }, [text("note")]))
      .toEqual({ kept: {}, dropped: [] });
    expect(carryConnectorInputMapping({ note: 'literal:""' }, [{ ...text("note"), schema: { type: "string", minLength: 1 } }]))
      .toEqual({ kept: {}, dropped: ["note"] });
  });

  it("keeps state paths and raw strings by name: they resolve at run time as before", () => {
    const carried = carryConnectorInputMapping(
      { marketplace: "state.marketplace", note: "{{ state.note }}" },
      [text("marketplace"), text("note")],
    );
    expect(carried.kept).toEqual({ marketplace: "state.marketplace", note: "{{ state.note }}" });
    expect(carried.dropped).toEqual([]);
  });

  it("never carries a platform-owned input and keeps nothing without a next action", () => {
    expect(carryConnectorInputMapping({ connection_ref: "state.x", sku: "state.sku" }, [text("sku"), text("connection_ref")]))
      .toEqual({ kept: { sku: "state.sku" }, dropped: [] });
    expect(carryConnectorInputMapping({ sku: "state.sku", empty: "state." }, []))
      .toEqual({ kept: {}, dropped: ["sku"] });
    expect(carryConnectorInputMapping(undefined, [text("sku")])).toEqual({ kept: {}, dropped: [] });
  });

  it("keeps a same-name nested input only when its value fits the next action's schema", () => {
    const address = { type: "object", required: ["name", "address_line1", "country_code"], properties: { name: { type: "string" }, address_line1: { type: "string" }, country_code: { type: "string" } } };
    const nextParameters = [{ name: "ship_from", type: "json", schema: address }];
    const partial = carryConnectorInputMapping({ ship_from: 'literal:{"name":"W1"}' }, nextParameters);
    expect(partial).toEqual({ kept: {}, dropped: ["ship_from"] });
    const complete = 'literal:{"name":"W1","address_line1":"1 Street","country_code":"GB"}';
    expect(carryConnectorInputMapping({ ship_from: complete }, nextParameters))
      .toEqual({ kept: { ship_from: complete }, dropped: [] });
  });

  it("carries the names __proto__ and constructor as own keys without touching any prototype", () => {
    const previous = JSON.parse('{"__proto__":"state.proto_value","constructor":"state.ctor","toString":"state.text"}') as Record<string, unknown>;
    const carried = carryConnectorInputMapping(previous, [text("__proto__"), text("constructor"), text("toString")]);
    expect(Object.getPrototypeOf(carried.kept)).toBe(Object.prototype);
    expect(Object.keys(carried.kept)).toEqual(["__proto__", "constructor", "toString"]);
    expect(Object.getOwnPropertyDescriptor(carried.kept, "__proto__")?.value).toBe("state.proto_value");
    expect(Object.getOwnPropertyDescriptor(carried.kept, "constructor")?.value).toBe("state.ctor");
    expect(carried.dropped).toEqual([]);
    expect(({} as Record<string, unknown>).proto_value).toBeUndefined();

    const undeclared = carryConnectorInputMapping(previous, []);
    expect(Object.keys(undeclared.kept)).toEqual([]);
    expect(Object.getPrototypeOf(undeclared.kept)).toBe(Object.prototype);
    expect(undeclared.dropped).toEqual(["__proto__", "constructor", "toString"]);
  });
});
