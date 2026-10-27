import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import {
  canvasGraphFixture,
  dryRunChangeSets,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail,
  postedChangeSets,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

// jsdom has no hit testing; the tested canvas clicks are directly on nodes.
const hitTestDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
beforeAll(() => Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [] }));
afterAll(() => {
  if (hitTestDescriptor) Object.defineProperty(document, "elementsFromPoint", hitTestDescriptor);
  else Reflect.deleteProperty(document, "elementsFromPoint");
});

it("preserves transform source and destination through local update and reopening", async () => {
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: "Add step" }));
  const palette = await screen.findByRole("listbox", { name: "Add step" });
  fireEvent.click(within(palette).getByRole("option", { name: "Transform" }));

  fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "Normalize audit" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "transform" } });
  // A new Transform starts with no Source (R2-20); the user picks one.
  fireEvent.change(await screen.findByRole("textbox", { name: "Transform source" }), { target: { value: "state.inputs.audit" } });
  fireEvent.change(await screen.findByLabelText("From"), { target: { value: "audit_input" } });
  fireEvent.change(screen.getByLabelText("To"), { target: { value: "result" } });
  fireEvent.click(screen.getByRole("button", { name: "Update new step" }));
  // The button says the update landed (✓ Updated) for a moment, then reads Update new step again.
  expect(await screen.findByRole("button", { name: "Updated" })).toBeDisabled();
  fireEvent.click(await screen.findByRole("button", { name: "Update new step" }, { timeout: 3000 }));
  expect(screen.getByLabelText("From")).toHaveValue("audit_input");
  expect(screen.getByLabelText("To")).toHaveValue("result");
  fireEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
  fireEvent.click(await screen.findByRole("group", { name: "Open step Normalize audit" }));
  expect(await screen.findByLabelText("From")).toHaveValue("audit_input");
  expect(screen.getByLabelText("To")).toHaveValue("result");
  await waitFor(() => {
    const operations = dryRunChangeSets().flatMap((request) => request.operations);
    expect(operations).toEqual(expect.arrayContaining([expect.objectContaining({
      op: "create_step",
      step: expect.objectContaining({ override: expect.objectContaining({ transform: expect.objectContaining({
        fields: expect.arrayContaining([expect.objectContaining({ from: "audit_input", to: "result" })]),
      }) }) }),
    })]));
  });
}, 15_000);


describe.each([false, true])("Transform required fields with workflow enabled=%s", (enabled) => {
  it.each([
    { name: "Transform source", error: "Source is required.", restored: "state.inputs.audit" },
    { name: "Transform result target", error: "Result target is required.", restored: "state.audit_transformed" },
  ])("shows the missing $name error, refuses Save, and recovers without losing the other field", async ({ name, error, restored }) => {
    const transform = {
      input: { source: "state.inputs.audit" },
      result_target: "state.audit_transformed",
      fields: [{ from: "name", to: "name", type: "text", actions: [] }],
    };
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({
      enabled,
      revision: 7,
      steps: [{ step_id: "normalize_audit", display_name: "Normalize audit", step_kind: "transform", override: { transform } }],
    }));
    mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
      enabled,
      entry: { kind: "step", id: "normalize_audit" },
      steps: [{ id: "normalize_audit", step_id: "normalize_audit", display_name: "Normalize audit", kind: "transform", config_summary: "Normalize audit", ports: [{ outcome: { name: "success" }, label: "Next" }] }],
    }));
    renderPage({ surface: "builder" });
    fireEvent.click(await screen.findByRole("group", { name: "Open step Normalize audit" }));
    const field = await screen.findByRole("textbox", { name });
    const source = screen.getByRole("textbox", { name: "Transform source" });
    const target = screen.getByRole("textbox", { name: "Transform result target" });
    expect(source).toBeRequired();
    expect(target).toBeRequired();
    fireEvent.change(field, { target: { value: "   " } });
    fireEvent.blur(field);
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription(error);
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    expect(postedChangeSets()).toEqual([]);
    expect(source).toHaveValue(name === "Transform source" ? "   " : "state.inputs.audit");
    expect(target).toHaveValue(name === "Transform result target" ? "   " : "Audit Transformed");
    fireEvent.change(field, { target: { value: restored } });
    fireEvent.blur(field);
    expect(field).not.toHaveAttribute("aria-invalid", "true");
    expect(field).not.toHaveAccessibleDescription(error);
    // Restored to the saved value, Save rests; a label edit sends the restored transform.
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Normalize the audit" } });
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(postedChangeSets()[0]).toMatchObject({
      expected_revision: 7,
      operations: [{ op: "update_step_config", step_id: "normalize_audit", patch: { override: { transform } } }],
    });
  }, 15_000);
});
