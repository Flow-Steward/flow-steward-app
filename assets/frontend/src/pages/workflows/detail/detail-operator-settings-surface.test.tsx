import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import type { FormFieldView } from "@/components/workflow-inputs/form-field";
import type { OperatorIssue } from "@/pages/operator/types";
import { OperatorSettingsSurface } from "./detail-operator-settings-surface";
import {
  operatorDraftFromStored,
  operatorSettingsPayload,
  type OperatorEditorView,
  type OperatorSettingsDraft,
} from "./detail-operator-settings-model";

const compatibility = {
  widgets_by_kind: {},
  filter_types_by_kind: {
    string: ["search"], date: ["date_range"], number: ["number_range"], boolean: ["boolean"],
    select: ["choice", "choice_multi"], select_list: ["choice", "choice_multi"],
  },
  job_columns: ["created_at", "status", "submitted_by"],
  job_filters: { status: ["choice", "choice_multi"] },
  job_status_options: ["awaiting_review", "queued"],
  column_formats: ["auto", "datetime", "number", "text"],
  result_displays: ["auto", "number", "text"],
  condition_operators_by_kind: {
    string: ["==", "!=", "==contains", "==pattern", "==empty", "!=empty"],
    select: ["==", "!=", "==empty", "!=empty"],
    number: ["==", "!=", ">", "<", "==empty", "!=empty"],
    boolean: ["==", "!="],
    file_list: ["==empty", "!=empty"],
  },
};

function field(name: string, kind: string, widgets: string[], extra: Record<string, unknown> = {}): FormFieldView {
  return {
    name, kind, widget: widgets[0] ?? "", widgets, label: name, instructions: "", placeholder: "", prepend: "",
    append: "", width: null, required: false, fixed: false, hidden: false, options: [], option_labels: {},
    conditional_logic: [], layout: "", button_label: "", min_items: null, max_items: null, format: "", sub_fields: [],
    ...extra,
  } as FormFieldView;
}

const view: OperatorEditorView = {
  workflow_id: "wf.contracts",
  workflow_name: "Contracts",
  workflow_revision: 4,
  title: "Contracts",
  published: false,
  runnable: false,
  effects_acknowledged: false,
  settings: null,
  form_fields: [
    field("doc_type", "select", ["select", "radio", "button_group"], { options: ["contract", "invoice"], required: true }),
    field("files", "file_list", ["files"]),
    field("signed_on", "string", ["date", "text"], { format: "date" }),
    field("urgent", "boolean", ["true_false"]),
    field("urgent_reason", "string", ["textarea", "text", "wysiwyg"]),
    field("amount", "number", ["number"], { required: true }),
  ],
  filters: [],
  input_schema: {
    type: "object",
    required: ["doc_type", "amount"],
    properties: {
      doc_type: { type: "string", enum: ["contract", "invoice"], title: "Document type" },
      files: { type: "array", items: { type: "string", "x-fs-type": "file" } },
      signed_on: { type: "string", format: "date" },
      urgent: { type: "boolean" },
      urgent_reason: { type: "string", format: "textarea" },
      amount: { type: "number" },
    },
  },
  output_schema: { type: "object", properties: { total: { type: "number" }, verdict: { type: "string", enum: ["ok", "check"] }, report: { type: "string", "x-fs-type": "file" } } },
  issues: [],
  compatibility,
};

type HarnessProps = { channels?: string[]; saveIssues?: OperatorIssue[]; stored?: unknown; viewIssues?: OperatorIssue[] };

function renderSurface({ channels = ["ui", "api"], saveIssues = [], stored = null, viewIssues = [] }: HarnessProps = {}) {
  const drafts: OperatorSettingsDraft[] = [];
  const save = vi.fn();
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({ workflow_id: "wf.contracts", projects: [{ id: "p-1", name: "Accounting" }, { id: "p-2", name: "Legal" }] }), { headers: { "content-type": "application/json" } }));
  function Harness() {
    const [draft, setDraft] = useState<OperatorSettingsDraft>(() => operatorDraftFromStored(stored));
    const ctx = {
      workflowId: "wf.contracts",
      workflowContractQuery: { data: { operator: { ...view, issues: viewIssues } } },
      operatorSettingsDraft: draft,
      setOperatorSettingsDraft: (next: OperatorSettingsDraft) => { drafts.push(next); setDraft(next); },
      operatorSaveIssues: saveIssues,
      workflowInterfaceEnabledDraft: true,
      workflowInterfaceTriggerChannelsDraft: channels,
      handleSaveWorkflowSettings: save,
      setDrawerTab: vi.fn(),
      workflowFormDirty: drafts.length > 0,
      savingWorkflowSurface: "",
    };
    return <OperatorSettingsSurface ctx={ctx} />;
  }
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Harness /></QueryClientProvider>);
  const payload = () => operatorSettingsPayload(drafts.at(-1) ?? operatorDraftFromStored(stored), {
    inputs: ["doc_type", "files", "signed_on", "urgent", "urgent_reason", "amount"],
    outputs: ["total", "verdict", "report"],
  });
  return { drafts, payload, save, user: userEvent.setup() };
}

function fieldTrigger(name: string): HTMLElement {
  const header = screen.getByTestId(`operator-field-row-${name}`);
  const trigger = header.closest("button");
  if (!trigger) throw new Error(`no trigger for ${name}`);
  return trigger;
}

async function choose(user: ReturnType<typeof userEvent.setup>, combobox: string, option: string) {
  await user.click(screen.getByRole("combobox", { name: combobox }));
  await user.click(screen.getByRole("option", { name: option }));
}

afterEach(() => { vi.restoreAllMocks(); });

it("lists the contract's fields in order with their key, type and widget", () => {
  renderSurface();
  const rows = screen.getAllByTestId(/^operator-field-row-/).map((row) => row.textContent);
  expect(rows).toHaveLength(6);
  expect(rows[0]).toContain("Document type");
  expect(rows[0]).toContain("doc_type · Choice");
  expect(rows[0]).toContain("Select");
  expect(rows[2]).toContain("signed_on · Text");
  expect(rows[2]).toContain("Date picker");
});

it("offers only the widgets compatible with the field's type and stores the choice", async () => {
  const { payload, user } = renderSurface();
  await user.click(fieldTrigger("signed_on"));
  await user.click(screen.getByRole("button", { name: "Choose widget" }));
  const dialog = screen.getByRole("dialog", { name: "Choose widget" });
  const options = within(within(dialog).getByTestId("operator-widget-options")).getAllByRole("radio");
  expect(options.map((radio) => radio.getAttribute("value"))).toEqual(["date", "text"]);
  // The design-system radio is pressed through its label.
  await user.click(within(dialog).getByText("Text"));
  expect(within(dialog).getByRole("radio", { name: "Text" })).toBeChecked();
  await user.click(within(dialog).getByRole("button", { name: "Use widget" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Choose widget" })).not.toBeInTheDocument());
  expect((payload().form as { fields: Record<string, unknown> }).fields).toEqual({ signed_on: { widget: "text" } });

  await user.click(fieldTrigger("doc_type"));
  await user.click(screen.getByRole("button", { name: "Choose widget" }));
  const radios = within(screen.getByTestId("operator-widget-options")).getAllByRole("radio");
  expect(radios.map((radio) => radio.getAttribute("value"))).toEqual(["select", "radio", "button_group"]);
});

it("turns hidden, option label and condition edits into the operator settings payload", async () => {
  const { payload, user } = renderSurface();
  await user.click(fieldTrigger("urgent"));
  await user.click(screen.getByRole("switch", { name: "Hidden from the operator" }));

  await user.click(fieldTrigger("doc_type"));
  await user.click(screen.getByRole("tab", { name: "Presentation" }));
  await user.type(screen.getByRole("textbox", { name: "Label of invoice" }), "Invoice");

  await user.click(fieldTrigger("urgent_reason"));
  await user.click(screen.getByRole("tab", { name: "Conditional logic" }));
  await user.click(screen.getByRole("button", { name: "Add rule group" }));
  await choose(user, "Rule 1.1: field", "urgent");
  await choose(user, "Rule 1.1: value", "True");

  expect((payload().form as { fields: Record<string, unknown> }).fields).toEqual({
    urgent: { hidden: true },
    doc_type: { option_labels: { invoice: "Invoice" } },
    urgent_reason: { conditional_logic: [[{ field: "urgent", operator: "==", value: true }]] },
  });
});

it("warns at once that a required field without a default cannot be hidden", async () => {
  const { user } = renderSurface();
  await user.click(fieldTrigger("amount"));
  await user.click(screen.getByRole("switch", { name: "Hidden from the operator" }));
  expect(screen.getByText("Required in the contract without a default: publishing is blocked while it is hidden.")).toBeInTheDocument();
});

it("reorders the form with the up and down buttons", async () => {
  const { payload, user } = renderSurface();
  const firstUp = screen.getAllByRole("button", { name: /^Move .+ up$/ })[0];
  // Two small icon buttons in a slot sized for them, the end one disabled.
  expect(firstUp).toBeDisabled();
  expect(firstUp).toHaveClass("fs-icon-button");
  expect(firstUp.closest(".fs-accordion-header")).toHaveAttribute("data-accessory-slot", "icon-pair");
  await user.click(screen.getByRole("button", { name: "Move amount up" }));
  expect((payload().form as { order: string[] }).order).toEqual(["doc_type", "files", "signed_on", "urgent", "amount", "urgent_reason"]);
});

it("shows a refused save's issue at the field it names, opened on its tab", async () => {
  renderSurface({
    saveIssues: [{ code: "form.hidden_required_without_default", message: "'amount' is required and hidden, but the contract gives it no default", path: "settings.operator.form.fields.amount.hidden", severity: "error" }],
    stored: { form: { fields: { amount: { hidden: true } } } },
  });
  const panel = await screen.findByTestId("operator-field-panel-amount");
  expect(within(panel).getByText("'amount' is required and hidden, but the contract gives it no default")).toBeInTheDocument();
  expect(within(screen.getByTestId("operator-field-row-amount")).getByText("Issues: 1")).toBeInTheDocument();
});

it("cannot publish without the UI channel, and says how to turn it on", async () => {
  const { user } = renderSurface({ channels: ["api"] });
  await user.click(screen.getByRole("tab", { name: "Publication" }));
  expect(screen.getByRole("switch", { name: "Show to operators" })).toBeDisabled();
  expect(screen.getByText(/Turn on the UI trigger channel in Smart setup first/)).toBeInTheDocument();
});

it("publishes, names the projects it appears in and acknowledges the real effects with {confirm: true}", async () => {
  const { payload, user } = renderSurface();
  await user.click(screen.getByRole("tab", { name: "Publication" }));
  const publish = screen.getByRole("switch", { name: "Show to operators" });
  expect(publish).toBeEnabled();
  await user.click(publish);
  await waitFor(() => expect(screen.getByText("Accounting, Legal")).toBeInTheDocument());
  expect(screen.getByTestId("operator-ack-status")).toHaveTextContent("Not confirmed");
  // The submission modes are one group, one option per line.
  expect(within(screen.getByRole("radiogroup", { name: "Submission" })).getAllByRole("radio")).toHaveLength(2);
  // A checkbox, so the confirmation can be taken back before the save.
  const confirm = screen.getByRole("checkbox", { name: "Confirm real effects" });
  expect(confirm).not.toBeChecked();
  const confirmLabel = confirm.closest("label");
  if (!confirmLabel) throw new Error("no label for the confirmation");
  await user.click(confirmLabel);
  expect(confirm).toBeChecked();
  expect(screen.getByTestId("operator-ack-status")).toHaveTextContent("Confirmed with the next save.");
  expect(payload()).toMatchObject({ published: true, effects_acknowledged: { confirm: true } });
  await user.click(confirmLabel);
  expect(confirm).not.toBeChecked();
  expect(screen.getByTestId("operator-ack-status")).toHaveTextContent("Not confirmed");
  expect(payload()).toMatchObject({ effects_acknowledged: null });
});

it("lists issues with a jump to the field they name", async () => {
  const { user } = renderSurface({
    viewIssues: [{ code: "form.widget_incompatible", message: "Widget 'textarea' does not fit the number field 'amount'", path: "settings.operator.form.fields.amount.widget", severity: "error" }],
  });
  await user.click(screen.getByRole("tab", { name: "Publication" }));
  const issues = screen.getByTestId("operator-issues");
  await user.click(within(issues).getByRole("button", { name: "Show" }));
  const panel = await screen.findByTestId("operator-field-panel-amount");
  expect(within(panel).getByText("Widget 'textarea' does not fit the number field 'amount'")).toBeInTheDocument();
});

it("edits the list's columns and filters, offering only filter types that fit", async () => {
  const { payload, user } = renderSurface();
  await user.click(screen.getByRole("tab", { name: "List" }));
  await user.click(screen.getByRole("button", { name: "Add column" }));
  await choose(user, "Column 1 source", "Run");
  await choose(user, "Column 1 value", "Created");
  await choose(user, "Column 1 format", "Date and time");
  await user.type(screen.getByRole("textbox", { name: "Column 1 heading" }), "Date");
  // One table row per column, under one row of headings.
  const columnTable = screen.getByRole("table", { name: "Columns" });
  expect(within(columnTable).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Source", "Value", "Heading", "Format", ""]);
  expect(within(columnTable).getAllByRole("row")).toHaveLength(2);

  await user.click(screen.getByRole("button", { name: "Add filter" }));
  // Source first, then a value of that source, as for columns.
  await choose(user, "Filter 1 source", "Input");
  await choose(user, "Filter 1 value", "amount");
  expect(within(screen.getByRole("table", { name: "Filters" })).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Source", "Value", "Filter type", "Label", ""]);
  await user.click(screen.getByRole("combobox", { name: "Filter 1 type" }));
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["Number range"]);
  await user.click(screen.getByRole("option", { name: "Number range" }));
  // Files cannot be filtered.
  await user.click(screen.getByRole("combobox", { name: "Filter 1 value" }));
  expect(screen.getByRole("option", { name: "amount" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "files" })).not.toBeInTheDocument();
  await user.keyboard("{Escape}");

  expect(payload().list).toEqual({
    columns: [{ id: "job.created_at", source: "job", path: "created_at", label: "Date", format: "datetime" }],
    filters: [{ id: "input.amount", source: "input", path: "amount", type: "number_range", label: "" }],
  });
});

it("sets the result's labels, display and order", async () => {
  const { payload, user } = renderSurface();
  await user.click(screen.getByRole("tab", { name: "Result" }));
  const total = screen.getByTestId("operator-result-row-total");
  await user.type(within(total).getByRole("textbox"), "Total");
  await user.clear(within(total).getByRole("spinbutton"));
  await user.type(within(total).getByRole("spinbutton"), "2");
  await user.click(screen.getByRole("button", { name: "Move verdict up" }));
  expect(payload().result).toEqual({ order: ["verdict", "total", "report"], fields: { total: { label: "Total", decimals: 2 } } });
});
