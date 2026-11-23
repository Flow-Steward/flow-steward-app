import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { ApprovalReviewFields } from "./detail-approval-review-fields";
import { reviewDraftFromConfig, reviewPayload, sourceSuggestions } from "./detail-approval-review-model";
import type { WorkflowVariableSuggestionGroup } from "./detail-core";

const suggestions: WorkflowVariableSuggestionGroup[] = [
  {
    key: "step",
    label: "Step variables",
    options: [
      { value: "state.total", label: "Recognize → Total", meta: "", valueType: "number", stepId: "recognize", variant: "state_alias", description: "" },
      { value: "state.supplier", label: "Recognize → Supplier", meta: "", valueType: "string", stepId: "recognize", variant: "state_alias", description: "" },
      { value: "state.document_date", label: "Recognize → Document date", meta: "", valueType: "", stepId: "recognize", variant: "state_alias", description: "" },
      { value: "state.notes.total", label: "Notes → Total", meta: "", valueType: "string", stepId: "notes", variant: "state_alias", description: "" },
      { value: "state.scan", label: "Recognize → Scan", meta: "", valueType: "file", stepId: "recognize", variant: "state_alias", description: "" },
    ],
  },
];

const callable = {
  workflows: [
    {
      workflow_id: "accounting_export",
      display_name: "Accounting export",
      expected_inputs: [
        { name: "supplier", type: "string", required: true, label: "Supplier" },
        { name: "total", type: "number", required: true },
        { name: "document_date", type: "string", format: "date" },
        { name: "lines", type: "array", items: { type: "object", properties: [{ name: "item", type: "string" }, { name: "qty", type: "number" }] } },
        { name: "api_key", type: "secret" },
      ],
    },
  ],
};

function renderReview({ review, issues = [] }: { review?: unknown; issues?: unknown[] } = {}) {
  const writes: unknown[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify(callable), { headers: { "content-type": "application/json" } }));
  function Harness() {
    const [config, setConfig] = useState<Record<string, unknown>>(review === undefined ? { message: "Check" } : { message: "Check", review });
    const ctx = {
      workflowId: "contracts",
      stepAuthoredId: "check",
      stepKindConfig: config,
      stepValidationErrors: issues,
      availableWorkflowVariableSuggestions: suggestions,
      workflowInterfaceInputsDraft: [{ name: "files", type: "array", itemType: "file", label: "Pages" }],
      replaceStepKindConfigField: (_kind: string, key: string, value: unknown) => {
        writes.push(value);
        setConfig((current) => {
          const next = { ...current, [key]: value };
          if (value === undefined) delete next[key];
          return next;
        });
      },
      renderWorkflowVariableInput: ({ ariaLabel, onChange, value }: { ariaLabel: string; onChange: (value: string) => void; value: string }) => (
        <input aria-label={ariaLabel} onChange={(event) => onChange(event.target.value)} value={value} />
      ),
      onStepInputValidationChange: vi.fn(),
    };
    return <ApprovalReviewFields ctx={ctx} />;
  }
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Harness /></QueryClientProvider>);
  return { last: () => writes.at(-1) as Record<string, unknown> | undefined, user: userEvent.setup() };
}

afterEach(() => { vi.restoreAllMocks(); });

it("takes typed fields from a workflow's inputs, with their sources matched by name and type", async () => {
  const { last, user } = renderReview();
  await user.click(screen.getByRole("button", { name: "Take fields from a workflow" }));
  const dialog = screen.getByRole("dialog", { name: "Take fields from a workflow" });
  await user.click(within(dialog).getByRole("combobox", { name: "Workflow" }));
  await waitFor(() => expect(screen.getByRole("option", { name: "Accounting export" })).toBeInTheDocument());
  await user.click(screen.getByRole("option", { name: "Accounting export" }));
  await user.click(within(dialog).getByRole("button", { name: "Take fields" }));

  expect(last()).toEqual({
    fields_from_workflow: "accounting_export",
    fields: [
      { name: "supplier", type: "string", required: true, editable: true, from: "state.supplier", label: "Supplier" },
      { name: "total", type: "number", required: true, editable: true, from: "state.total" },
      { name: "document_date", type: "string", required: false, editable: true, format: "date" },
      {
        name: "lines", type: "array", required: false, editable: true,
        items: { type: "object", properties: [{ name: "item", type: "string" }, { name: "qty", type: "number" }] },
      },
    ],
  });
  expect(screen.getByText("Fields taken from accounting_export.")).toBeInTheDocument();
});

it("suggests sources by name and type and writes the chosen one", async () => {
  const { last, user } = renderReview({ review: { fields: [{ name: "document_date", type: "string", editable: true }, { name: "total", type: "number" }] } });
  await user.click(screen.getByRole("button", { name: /document_date/ }));
  const matches = screen.getByTestId("review-source-suggestions-0");
  expect(within(matches).getAllByRole("button").map((button) => button.textContent)).toEqual(["Recognize → Document date (type unknown)"]);
  await user.click(within(matches).getByRole("button"));
  expect((last()?.fields as Array<Record<string, unknown>>)[0]).toMatchObject({ name: "document_date", from: "state.document_date" });

  await user.click(screen.getByRole("button", { name: /^total/ }));
  // A number field is offered the number, not the text of the same name.
  expect(within(screen.getByTestId("review-source-suggestions-1")).getAllByRole("button").map((button) => button.textContent)).toEqual(["Recognize → Total"]);
});

it("writes the review in the step's shape with the contract editor's controls", async () => {
  const { last, user } = renderReview();
  await user.click(screen.getByRole("button", { name: "Add field" }));
  await user.type(screen.getByRole("textbox", { name: "Name" }), "verdict");
  await user.selectOptions(screen.getByRole("combobox", { name: "Type" }), "select");
  await user.type(screen.getByRole("textbox", { name: "Options" }), "ok, check");
  await user.click(screen.getByRole("checkbox", { name: "Required" }));
  await user.click(screen.getByRole("checkbox", { name: "The reviewer may correct it" }));
  await user.type(screen.getByRole("textbox", { name: "Initial value of verdict" }), "state.verdict");
  await user.click(screen.getByRole("combobox", { name: "Original shown beside the fields" }));
  await user.click(screen.getByRole("option", { name: "Pages" }));

  expect(last()).toEqual({
    original: "state.inputs.files",
    fields: [{ name: "verdict", type: "select", required: true, editable: false, from: "state.verdict", options: ["ok", "check"] }],
  });

  await user.click(screen.getByRole("button", { name: "Remove field" }));
  await user.click(screen.getByRole("combobox", { name: "Original shown beside the fields" }));
  await user.click(screen.getByRole("option", { name: "No original" }));
  // No fields and no original: the Approval is as before, without a review.
  expect(last()).toBeUndefined();
});

it("shows the readiness issues of the review at the field and the original they name", () => {
  renderReview({
    review: { original: "state.notes", fields: [{ name: "total", type: "boolean", editable: true, from: "state.total" }] },
    issues: [
      { code: "workflow.approval_review_source_type_mismatch", message: "Approval review field 'total' is 'boolean' but its source 'state.total' is 'number'", path: "workflow.steps.check.override.approval.review.fields[0].from" },
      { code: "workflow.approval_review_original_not_file", message: "Approval review original 'state.notes' is 'string', not a file", path: "workflow.steps.check.override.approval.review.original" },
      { code: "workflow.other", message: "Another step's issue", path: "workflow.steps.other.override.approval.review.fields[0].from" },
    ],
  });
  const list = screen.getByTestId("approval-review-field-list");
  // A row with an issue stays open with it in view.
  expect(within(list).getByText("Approval review field 'total' is 'boolean' but its source 'state.total' is 'number'")).toBeInTheDocument();
  expect(screen.getByText("Approval review original 'state.notes' is 'string', not a file")).toBeInTheDocument();
  expect(screen.queryByText("Another step's issue")).not.toBeInTheDocument();
});

it("reads a stored review back to the same payload", () => {
  const review = {
    original: "state.inputs.files",
    fields_from_workflow: "accounting_export",
    fields: [
      { name: "total", type: "number", required: true, editable: true, from: "state.total", label: "Total", description: "As on the invoice" },
      { name: "lines", type: "array", editable: true, from: "state.lines", items: { type: "object", properties: [{ name: "item", type: "string" }] }, min_items: 1 },
      { name: "note", editable: false, from: "state.note" },
    ],
  };
  expect(reviewPayload(reviewDraftFromConfig(review))).toEqual({
    ...review,
    fields: [
      review.fields[0],
      { ...review.fields[1], required: false },
      { name: "note", type: "paragraph", required: false, editable: false, from: "state.note" },
    ],
  });
  expect(sourceSuggestions({ name: "total", type: "number" }, suggestions).map((item) => item.value)).toEqual(["state.total"]);
});
