import {
  emptyFieldConfig,
  issueTarget,
  mergedOrder,
  operatorDraftFromStored,
  operatorIssuesFromError,
  operatorSettingsPayload,
  previewFormFields,
  valuePathOptions,
} from "./detail-operator-settings-model";
import { HttpError } from "@/lib/http/errors";
import type { FormFieldView } from "@/components/workflow-inputs/form-field";

const inputSchema = {
  type: "object",
  properties: {
    doc_type: { type: "string", enum: ["contract", "invoice"], title: "Document type" },
    urgent: { type: "boolean" },
    urgent_reason: { type: "string", format: "textarea" },
    companies: { type: "array", items: { type: "object", properties: { name: { type: "string" }, website: { type: "string", format: "uri" } } } },
    mail_password: { type: "string", writeOnly: true },
  },
};

describe("operator settings payload", () => {
  it("sends only what differs from the defaults, and an order equal to the contract's as empty", () => {
    const draft = operatorDraftFromStored(null);
    draft.form.order = ["doc_type", "urgent", "urgent_reason"];
    draft.form.fields.urgent_reason = { ...emptyFieldConfig(), hidden: false, conditional_logic: [[{ field: "urgent", operator: "==", value: true }]] };
    draft.form.fields.doc_type = { ...emptyFieldConfig(), option_labels: { contract: " Contract ", invoice: "" }, widget: "radio" };
    draft.form.fields.urgent = { ...emptyFieldConfig(), hidden: true };
    draft.form.fields.companies = { ...emptyFieldConfig(), sub_fields: { name: emptyFieldConfig(), website: { ...emptyFieldConfig(), label: "Site" } } };

    expect(operatorSettingsPayload(draft, { inputs: ["doc_type", "urgent", "urgent_reason"], outputs: [] })).toEqual({
      published: false,
      menu_title: "",
      effects_acknowledged: null,
      submission: { mode: "bulk", target_input: "" },
      form: {
        order: [],
        fields: {
          urgent_reason: { conditional_logic: [[{ field: "urgent", operator: "==", value: true }]] },
          doc_type: { widget: "radio", option_labels: { contract: "Contract" } },
          urgent: { hidden: true },
          companies: { sub_fields: { website: { label: "Site" } } },
        },
      },
      result: { order: [], fields: {} },
      list: { columns: [], filters: [] },
    });
  });

  it("keeps a moved order, sends the stored acknowledgement back, and confirms with {confirm: true}", () => {
    const draft = operatorDraftFromStored({
      published: true,
      effects_acknowledged: { by: "admin-1", at: "2026-11-19T10:00:00Z", revision: 4 },
      submission: { mode: "foreach", target_input: "files" },
    });
    draft.form.order = ["urgent", "doc_type"];
    const kept = operatorSettingsPayload(draft, { inputs: ["doc_type", "urgent"] });
    expect(kept.effects_acknowledged).toEqual({ by: "admin-1", at: "2026-11-19T10:00:00Z", revision: 4 });
    expect((kept.form as { order: string[] }).order).toEqual(["urgent", "doc_type"]);
    expect(kept.submission).toEqual({ mode: "foreach", target_input: "files" });

    expect(operatorSettingsPayload({ ...draft, acknowledge: true }).effects_acknowledged).toEqual({ confirm: true });
  });

  it("gives columns and filters their default ids and leaves out unfinished rows", () => {
    const draft = operatorDraftFromStored(null);
    draft.list.columns = [
      { id: "", source: "job", path: "created_at", label: " Date ", format: "datetime" },
      { id: "", source: "input", path: "", label: "", format: "auto" },
    ];
    draft.list.filters = [
      { id: "", source: "input", path: "doc_type", type: "choice_multi", label: "" },
      { id: "", source: "output", path: "total", type: "", label: "" },
    ];
    const list = operatorSettingsPayload(draft).list;
    expect(list).toEqual({
      columns: [{ id: "job.created_at", source: "job", path: "created_at", label: "Date", format: "datetime" }],
      filters: [{ id: "input.doc_type", source: "input", path: "doc_type", type: "choice_multi", label: "" }],
    });
  });

  it("drops settings of fields the contract no longer declares", () => {
    const draft = operatorDraftFromStored(
      { form: { order: ["gone", "urgent"], fields: { gone: { label: "Old" }, urgent: { label: "Urgent?" } } }, result: { fields: { lost: { label: "x" } } } },
      { inputs: ["urgent"], outputs: ["total"] },
    );
    expect(draft.form.order).toEqual(["urgent"]);
    expect(Object.keys(draft.form.fields)).toEqual(["urgent"]);
    expect(draft.result.fields).toEqual({});
  });
});

describe("operator settings helpers", () => {
  it("merges the draft order with the contract's", () => {
    expect(mergedOrder(["c", "x", "a"], ["a", "b", "c"])).toEqual(["c", "a", "b"]);
  });

  it("knows where each issue path is edited", () => {
    expect(issueTarget("settings.operator.form.fields.amount.hidden")).toEqual({ tab: "form", field: ["amount"], fieldTab: "general" });
    expect(issueTarget("settings.operator.form.fields.companies.sub_fields.email.widget")).toEqual({ tab: "form", field: ["companies", "email"], fieldTab: "general" });
    expect(issueTarget("settings.operator.form.fields.reason.conditional_logic[0][0].value")).toEqual({ tab: "form", field: ["reason"], fieldTab: "conditions" });
    expect(issueTarget("settings.operator.form.fields.doc_type.option_labels.x")).toEqual({ tab: "form", field: ["doc_type"], fieldTab: "presentation" });
    expect(issueTarget("settings.operator.list.filters[0].type")).toEqual({ tab: "list" });
    expect(issueTarget("settings.operator.result.fields.total")).toEqual({ tab: "result" });
    expect(issueTarget("settings.operator.submission.target_input")).toEqual({ tab: "publication" });
  });

  it("reads the issues of a refused save", () => {
    const error = new HttpError({
      status: 400,
      url: "/api/workflows/wf",
      payload: { detail: { code: "workflow_operator_settings_invalid", message: "no", issues: [{ code: "form.widget_incompatible", message: "bad", path: "settings.operator.form.fields.a.widget", severity: "error" }] } },
    });
    expect(operatorIssuesFromError(error)).toEqual([{ code: "form.widget_incompatible", message: "bad", path: "settings.operator.form.fields.a.widget", severity: "error" }]);
    expect(operatorIssuesFromError(new HttpError({ status: 409, url: "/x", payload: { detail: "conflict" } }))).toBeNull();
  });

  it("lists column paths into groups and lists of groups, never secrets", () => {
    expect(valuePathOptions(inputSchema).map((option) => option.path)).toEqual([
      "doc_type", "urgent", "urgent_reason", "companies", "companies[].name", "companies[].website",
    ]);
  });

  it("previews the form with the draft's widget, label, hidden fields and order", () => {
    const fields: FormFieldView[] = [
      { name: "doc_type", kind: "select", widget: "select", widgets: ["select", "radio", "button_group"], label: "Document type", options: ["contract", "invoice"] },
      { name: "urgent", kind: "boolean", widget: "true_false", widgets: ["true_false"], label: "urgent" },
    ];
    const draft = operatorDraftFromStored({
      form: { order: ["urgent"], fields: { doc_type: { widget: "radio", option_labels: { contract: "Contract" } }, urgent: { hidden: true } } },
    });
    const preview = previewFormFields(fields, draft, inputSchema);
    expect(preview.map((field) => [field.name, field.widget, field.hidden])).toEqual([["urgent", "true_false", true], ["doc_type", "radio", false]]);
    expect(preview[1].label).toBe("Document type");
    expect(preview[1].option_labels).toEqual({ contract: "Contract" });
  });
});
