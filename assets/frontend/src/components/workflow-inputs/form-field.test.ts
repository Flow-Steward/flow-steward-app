import {
  collectFiles,
  collectInputs,
  conditionRuleMet,
  formProblems,
  initialFormValues,
  parseLocaleNumber,
  shownFields,
  type FormFieldView,
} from "./form-field";

function field(name: string, kind: string, extra: Partial<FormFieldView> = {}): FormFieldView {
  return { name, kind, widget: "", label: name, ...extra };
}

const urgent = field("urgent", "boolean", { widget: "true_false" });
const reason = field("urgent_reason", "string", {
  widget: "textarea",
  conditional_logic: [[{ field: "urgent", operator: "==", value: true }]],
});
const fields: FormFieldView[] = [
  field("doc_type", "select", { widget: "select", options: ["invoice", "contract"], required: true }),
  urgent,
  reason,
  field("ocr_model", "string", { hidden: true }),
  field("tenant", "string", { fixed: true, hidden: true }),
  field("pages", "file_list", { widget: "files", min_items: 1, max_items: 4 }),
];

describe("conditional logic", () => {
  it("shows a field only while its condition holds, and never hidden or fixed ones", () => {
    expect(shownFields(fields, { urgent: false }).map((item) => item.name)).toEqual(["doc_type", "urgent", "pages"]);
    expect(shownFields(fields, { urgent: true }).map((item) => item.name)).toEqual(["doc_type", "urgent", "urgent_reason", "pages"]);
  });

  it("joins rules with AND inside a group and groups with OR", () => {
    const amount = field("amount", "number");
    const note = field("note", "string", {
      conditional_logic: [
        [
          { field: "amount", operator: ">", value: 100 },
          { field: "doc_type", operator: "==", value: "invoice" },
        ],
        [{ field: "doc_type", operator: "==", value: "contract" }],
      ],
    });
    const form = [fields[0], amount, note];
    const names = (values: Record<string, unknown>) => shownFields(form, values).map((item) => item.name);
    expect(names({ amount: 500, doc_type: "invoice" })).toContain("note");
    expect(names({ amount: 50, doc_type: "invoice" })).not.toContain("note");
    expect(names({ amount: 50, doc_type: "contract" })).toContain("note");
  });

  it("treats a field its own condition hides as empty for the conditions of others", () => {
    const followUp = field("follow_up", "string", { conditional_logic: [[{ field: "urgent_reason", operator: "!=empty" }]] });
    const form = [urgent, reason, followUp];
    expect(shownFields(form, { urgent: false, urgent_reason: "left over" }).map((item) => item.name)).toEqual(["urgent"]);
  });

  it("evaluates the ACF operators", () => {
    expect(conditionRuleMet({ field: "x", operator: "==contains", value: "b" }, "select_list", ["a", "b"])).toBe(true);
    expect(conditionRuleMet({ field: "x", operator: "!=contains", value: "c" }, "select_list", ["a", "b"])).toBe(true);
    expect(conditionRuleMet({ field: "x", operator: "==pattern", value: "^INV-" }, "string", "INV-7")).toBe(true);
    expect(conditionRuleMet({ field: "x", operator: "==pattern", value: "(" }, "string", "x")).toBe(false);
    expect(conditionRuleMet({ field: "x", operator: "<", value: 3 }, "number", "")).toBe(false);
    expect(conditionRuleMet({ field: "x", operator: "==empty" }, "file_list", [])).toBe(true);
  });
});

describe("what a submission sends", () => {
  it("omits condition-hidden, hidden and fixed fields and empty optional values", () => {
    const values = { doc_type: "invoice", urgent: false, urgent_reason: "stale", ocr_model: "x", tenant: "y" };
    expect(collectInputs(fields, values)).toEqual({ doc_type: "invoice", urgent: false });
    expect(collectInputs(fields, { ...values, urgent: true })).toEqual({ doc_type: "invoice", urgent: true, urgent_reason: "stale" });
  });

  it("sends a repeater's filled rows and a group's filled sub-fields", () => {
    const companies = field("companies", "object_list", {
      sub_fields: [field("name", "string", { required: true }), field("website", "string")],
    });
    const address = field("address", "object", { sub_fields: [field("city", "string"), field("zip", "string")] });
    expect(
      collectInputs([companies, address], {
        companies: [{ name: "Acme", website: "" }, {}, { name: "Beta", website: "https://beta.test" }],
        address: { city: "Vilnius", zip: "" },
      }),
    ).toEqual({
      companies: [{ name: "Acme" }, { name: "Beta", website: "https://beta.test" }],
      address: { city: "Vilnius" },
    });
  });

  it("lists every file of a shown file field once, in upload order", () => {
    const one = new File(["1"], "1.jpg");
    const two = new File(["2"], "2.jpg");
    expect(collectFiles(fields, {}, { pages: [one, two] })).toEqual([
      { inputName: "pages", file: one },
      { inputName: "pages", file: two },
    ]);
  });
});

describe("client checks", () => {
  it("asks for a shown required field and respects item bounds", () => {
    const pages = Array.from({ length: 5 }, (_, index) => new File([String(index)], `${index}.jpg`));
    expect(formProblems(fields, {}, {})).toEqual([{ field: "doc_type", code: "required" }]);
    expect(formProblems(fields, { doc_type: "invoice" }, { pages })).toEqual([{ field: "pages", code: "max_items", limit: 4 }]);
    // The For each target sends one item per run: its bounds do not apply here.
    expect(formProblems(fields, { doc_type: "invoice" }, { pages }, "pages")).toEqual([]);
  });

  it("asks for a required sub-field in a filled repeater row", () => {
    const companies = field("companies", "object_list", { sub_fields: [field("name", "string", { required: true }), field("email", "string")] });
    expect(formProblems([companies], { companies: [{ name: "Acme" }, { email: "a@b.test" }] })).toEqual([
      { field: "companies", code: "row_required", row: 2, subField: "name" },
    ]);
  });

  it("starts from the contract's defaults and a repeater's minimum rows", () => {
    const companies = field("companies", "object_list", { min_items: 2 });
    expect(initialFormValues([fields[0], companies], { properties: { doc_type: { default: "invoice" } } })).toEqual({
      doc_type: "invoice",
      companies: [{}, {}],
    });
  });
});

describe("parseLocaleNumber", () => {
  it.each([
    ["318,50", 318.5],
    ["318.50", 318.5],
    ["1 234,56", 1234.56],
    ["1\u00a0234,56", 1234.56],
    ["1.234,56", 1234.56],
    ["1,234.56", 1234.56],
    ["-2", -2],
    [" 7 ", 7],
  ])("reads %s as %s", (text, expected) => {
    expect(parseLocaleNumber(text)).toBe(expected);
  });

  it.each(["", "318,5О", "12abc", "1.2.3", "1,2,3.4.5"])("refuses %j", (text) => {
    expect(parseLocaleNumber(text)).toBeNull();
  });
});

