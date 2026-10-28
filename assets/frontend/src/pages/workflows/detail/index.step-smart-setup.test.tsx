import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  fetchSpy,
  postedChangeSets,
  stepBodyFromCall,
  savedStepBodies,
  savedStepCall,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowContract,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowTransformActions,
  mockedFetchWorkflowValidation,
  mockedFindWorkflowLinkedProjectId,
  mockedPreviewWorkflowTransform,
  renderPage,
} from "./index.test-helpers";
import { objectOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import { extensionConnectionEligibility } from "@/pages/workflows/detail/detail-extension-connection";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

// Full-page edit/save/reopen journeys perform several async renders. Measured
// journey wall time exceeded the default 5s budget while the host was under load.
// Keep other cases and shared/component checks on their existing limits.
const SMART_SETUP_JOURNEY_TIMEOUT = 15_000;

function chooseSetupOption(control: HTMLElement, label: string) {
  if (control.getAttribute("aria-expanded") !== "true") {
    fireEvent.mouseDown(control);
    fireEvent.click(control);
  }
  const options = screen.getByRole("listbox", { name: `${control.getAttribute("aria-label")} options` });
  fireEvent.click(within(options).getByRole("option", { name: label }));
}

function setupOptions(control: HTMLElement) {
  if (control.getAttribute("aria-expanded") !== "true") {
    fireEvent.mouseDown(control);
    fireEvent.click(control);
  }
  return within(screen.getByRole("listbox", { name: `${control.getAttribute("aria-label")} options` }));
}

/** Save parameters rests until something changes: an unrelated label edit makes it live. */
function editStepLabel(value = "Edited step label") {
  fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value } });
}

// Wait for the actual fixture step to render before opening its editor.
// Query notifications and jsdom rendering may take more than the default second.
async function renderTabularStepButton() {
  renderPage();
  return screen.findByRole("button", { name: /Feed rows/i }, { timeout: 5_000 });
}

describe("WorkflowsDetailPage step smart setup", () => {
  it("opens an ordinary step immediately and retains its parameters when a new-step switch is cancelled", async () => {
    const boundary = prepareTabularRoundtrip({ operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.feed_dataset" });
    fireEvent.click(await renderTabularStepButton());
    const label = screen.getByRole("textbox", { name: "Step label" });
    fireEvent.change(label, { target: { value: "Unfinished feed parameters" } });
    fireEvent.click(screen.getByRole("button", { name: "+ Add Step" }));
    const dialog = await screen.findByRole("dialog", { name: "Discard unsaved parameters?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(label).toHaveValue("Unfinished feed parameters");
    expect(screen.getByRole("button", { name: "Save parameters" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add step to changes" })).not.toBeInTheDocument();
    expect(boundary.writes).toEqual([]);
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("asks before discarding invalid input and preserves it when Close is cancelled", async () => {
    const user = userEvent.setup();
    const boundary = prepareTabularRoundtrip({
      operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.feed_dataset",
    });
    await user.click(await renderTabularStepButton());
    const invalidRows = screen.getByRole("textbox", { name: "Rows" });
    fireEvent.change(invalidRows, { target: { value: "[{" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    expect(boundary.writes).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Close" }));
    const discard = await screen.findByRole("dialog", { name: "Discard invalid input?" });
    await user.click(within(discard).getByRole("button", { name: "Cancel" }));
    expect(invalidRows).toBeInTheDocument();
    expect(invalidRows).toHaveValue("[{");
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Discard invalid input?" })).getByRole("button", { name: "Discard input" }));
    expect(boundary.storedConfig().rows).toEqual([{ sku: "A1" }]);
    await user.click(screen.getByRole("button", { name: /Feed rows/i }));
    const rows = screen.getByRole("textbox", { name: "Rows" });
    expect(JSON.parse((rows as HTMLTextAreaElement).value)).toEqual([{ sku: "A1" }]);
    expect(rows).not.toHaveAttribute("aria-invalid", "true");
    // Do not edit Rows to clear its error: another-field save proves the old
    // parent error was cleared as well as the visible raw draft.
    fireEvent.change(screen.getByRole("textbox", { name: "Result target" }), {
      target: { value: "state.reopened_dataset" },
    });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.storedConfig()).toMatchObject({
      rows: [{ sku: "A1" }], result_target: "state.reopened_dataset",
    });
  }, SMART_SETUP_JOURNEY_TIMEOUT);


  it("distinguishes an omitted boolean runtime default from explicit false through save and reopen", async () => {
    const user = userEvent.setup();
    const boundary = prepareTabularRoundtrip({
      operation: "transform", input: { dataset_handle: "state.existing_dataset" },
      result_target: "state.feed_dataset",
      actions: [{ action: "map.values", target: "status", params: { mapping: { A: "Active" } } }],
    });
    await user.click(await renderTabularStepButton());
    expect(await screen.findByRole("combobox", { name: "keep_unmapped" })).toHaveTextContent("Runtime default (not set)");
    fireEvent.change(screen.getByRole("textbox", { name: "Result target" }), {
      target: { value: "state.changed_dataset" },
    });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.storedConfig().actions).toEqual([
      { action: "map.values", target: "status", params: { mapping: { A: "Active" } } },
    ]);
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Feed rows/i }));
    expect(await screen.findByRole("combobox", { name: "keep_unmapped" })).toHaveTextContent("Runtime default (not set)");
    screen.getByRole("combobox", { name: "keep_unmapped" }).focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("option", { name: "Runtime default (not set)" })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("option", { name: "false" })).toHaveFocus());
    await user.keyboard("{Home}{Enter}");
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(boundary.writes).toHaveLength(2));
    expect(boundary.storedConfig().actions).toEqual([
      { action: "map.values", target: "status", params: { mapping: { A: "Active" }, keep_unmapped: false } },
    ]);
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Feed rows/i }));
    expect(await screen.findByRole("combobox", { name: "keep_unmapped" })).toHaveTextContent("false");
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  // Only API I/O is replaced. Readback stores the actual request, not a
  // prearranged expected response; the page, draft and save handler stay real.
  function prepareTabularRoundtrip(initialConfig: Record<string, unknown>, otherConfig?: Record<string, unknown>) {
    const stepId = "11111111-1111-1111-1111-111111111081";
    const otherStepId = "11111111-1111-1111-1111-111111111082";
    let storedConfig = structuredClone(initialConfig);
    let otherStoredConfig = otherConfig ? structuredClone(otherConfig) : null;
    const writes: Array<Record<string, unknown>> = [];
    const fallbackFetch = fetchSpy.getMockImplementation();
    let revision = 1;
    mockedFetchWorkflowDetail.mockImplementation(async () => ({
      workflow_id: "weekly_report", display_name: "Weekly Report", enabled: true, revision,
      steps: [{
          id: stepId, step_id: "feed_rows", display_name: "Feed rows",
          step_kind: "tabular_feed",
          override: { tabular_feed: structuredClone(storedConfig) },
        }, ...(otherStoredConfig ? [{
          id: otherStepId, step_id: "other_feed", display_name: "Other feed",
          step_kind: "tabular_feed",
          override: { tabular_feed: structuredClone(otherStoredConfig) },
        }] : [])], groups: [],
    } as never));
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    mockedFetchWorkflowTransformActions.mockResolvedValue({
      actions: [{
        action_id: "map.values", category: "map", display_name: "Map Values",
        value_scope: "scalar", input_type: "json", output_type: "json",
        params_schema: {
          type: "object",
          properties: { mapping: { type: "object" }, keep_unmapped: { type: "boolean" } },
          required: ["mapping"], additionalProperties: false,
        },
        param_sources: {}, pure: true, deterministic: true,
        io_allowed: false, transform_safe: true,
        backends: { scalar: true, polars: true },
      }],
    } as never);
    fetchSpy.mockImplementation(async (input, init) => {
      if (input === "/api/workflows/weekly_report/changes" && init?.method === "POST") {
        if (typeof init.body !== "string") throw new Error("Expected a JSON change set");
        const changeSet = objectOrEmpty(JSON.parse(init.body));
        const [operation] = changeSet.operations as Array<Record<string, unknown>>;
        if (operation.op !== "update_step_config") throw new Error("Expected one step config change");
        const payload = objectOrEmpty(operation.patch);
        writes.push(structuredClone(payload));
        const config = structuredClone(objectOrEmpty(objectOrEmpty(payload.override).tabular_feed));
        if (operation.step_id === "other_feed") otherStoredConfig = config;
        else storedConfig = config;
        revision += 1;
        return new Response(JSON.stringify({ workflow_id: "weekly_report", revision, valid: true, issues: [], workflow: {} }), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      if (!fallbackFetch) throw new Error("Missing ordinary page API fixture");
      return fallbackFetch(input, init);
    });
    return {
      storedConfig: () => structuredClone(storedConfig),
      otherStoredConfig: () => structuredClone(otherStoredConfig),
      writes,
    };
  }

  it.each(["create_dataset", "merge_rows"])("edits %s rows as a state selector and reads the saved selector back", async (operation) => {
    const user = userEvent.setup();
    const boundary = prepareTabularRoundtrip({
      operation,
      ...(operation === "merge_rows" ? { input: { dataset_handle: "state.existing_dataset" }, key_column: "sku" } : {}),
      rows: "state.source_rows", result_target: "state.feed_dataset",
    });
    await user.click(await renderTabularStepButton());
    expect(screen.getByRole("textbox", { name: "Rows" })).toHaveValue("state.source_rows");
    fireEvent.change(screen.getByRole("textbox", { name: "Rows" }), {
      target: { value: "state.revised_rows" },
    });
    expect(screen.getByRole("textbox", { name: "Rows" })).toHaveValue("state.revised_rows");
    const readsBeforeSave = mockedFetchWorkflowDetail.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.writes[0]).toMatchObject({
      override: { tabular_feed: { rows: "state.revised_rows", result_target: "state.feed_dataset" } },
    });
    await waitFor(() => expect(mockedFetchWorkflowDetail.mock.calls.length).toBeGreaterThan(readsBeforeSave));
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Feed rows/i }));
    expect(screen.getByRole("textbox", { name: "Rows" })).toHaveValue("state.revised_rows");
    expect(boundary.storedConfig().rows).toBe("state.revised_rows");
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("preserves a stored rows selector when only another field is edited", async () => {
    const user = userEvent.setup();
    const boundary = prepareTabularRoundtrip({
      operation: "create_dataset", rows: "state.source_rows", result_target: "state.feed_dataset",
    });
    await user.click(await renderTabularStepButton());
    fireEvent.change(screen.getByRole("textbox", { name: "Result target" }), {
      target: { value: "state.changed_dataset" },
    });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.storedConfig()).toMatchObject({
      rows: "state.source_rows", result_target: "state.changed_dataset",
    });
    expect(boundary.writes).toHaveLength(1);
  });

  it.each([
    { operation: "create_dataset", invalid: "[{", label: "Rows", original: { rows: [{ sku: "A1" }] }, corrected: '[{"sku":"B2"}]', expected: { rows: [{ sku: "B2" }] } },
    { operation: "create_dataset", invalid: '{"sku":"B2"}', label: "Rows", original: { rows: [{ sku: "A1" }] }, corrected: '[{"sku":"B2"}]', expected: { rows: [{ sku: "B2" }] } },
    { operation: "create_dataset", invalid: "42", label: "Rows", original: { rows: [{ sku: "A1" }] }, corrected: '[{"sku":"B2"}]', expected: { rows: [{ sku: "B2" }] } },
    { operation: "validate", invalid: '{"required_columns":', label: "Validation checks", original: { input: { dataset_handle: "state.existing_dataset" }, checks: { required_columns: ["sku"] } }, corrected: '{"required_columns":["sku","price"]}', expected: { checks: { required_columns: ["sku", "price"] } } },
    { operation: "transform", invalid: '{"A":', label: "mapping", original: { input: { dataset_handle: "state.existing_dataset" }, actions: [{ action: "map.values", target: "status", params: { mapping: { A: "Active" }, keep_unmapped: true } }] }, corrected: '{"A":"Approved"}', expected: { actions: [{ action: "map.values", target: "status", params: { mapping: { A: "Approved" }, keep_unmapped: true } }] } },
  ])("keeps invalid $operation $label text without saving a substitute: $invalid", async ({ operation, invalid, label, original, corrected, expected }) => {
    const user = userEvent.setup();
    const initialConfig = { operation, result_target: "state.feed_dataset", ...original };
    const boundary = prepareTabularRoundtrip(initialConfig);
    await user.click(await renderTabularStepButton());
    const field = await screen.findByRole("textbox", { name: label });
    fireEvent.change(field, { target: { value: invalid } });
    expect(field).toHaveValue(invalid);
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    expect(boundary.writes).toHaveLength(0);
    expect(boundary.storedConfig()).toEqual(initialConfig);
    expect(field).toHaveValue(invalid);
    fireEvent.change(field, { target: { value: corrected } });
    const readsBeforeSave = mockedFetchWorkflowDetail.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.writes[0]).toMatchObject({ override: { tabular_feed: expected } });
    await waitFor(() => expect(mockedFetchWorkflowDetail.mock.calls.length).toBeGreaterThan(readsBeforeSave));
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Feed rows/i }));
    expect(JSON.parse((await screen.findByRole("textbox", { name: label }) as HTMLTextAreaElement).value)).toEqual(JSON.parse(corrected));
    expect(boundary.storedConfig()).toMatchObject(expected);
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("persists the displayed empty dataset rows default without requiring an edit to Rows", async () => {
    const user = userEvent.setup();
    const boundary = prepareTabularRoundtrip({
      operation: "create_dataset", result_target: "state.feed_dataset",
    });
    await user.click(await renderTabularStepButton());
    expect(screen.getByRole("textbox", { name: "Rows" })).toHaveValue("[]");
    // Saving another visible field must persist the empty Rows value the author sees.
    fireEvent.change(screen.getByRole("textbox", { name: "Result target" }), {
      target: { value: "state.baseline" },
    });
    const readsBeforeSave = mockedFetchWorkflowDetail.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.writes[0]).toMatchObject({
      override: { tabular_feed: { operation: "create_dataset", rows: [], result_target: "state.baseline" } },
    });
    expect(boundary.storedConfig()).toHaveProperty("rows", []);
    await waitFor(() => expect(mockedFetchWorkflowDetail.mock.calls.length).toBeGreaterThan(readsBeforeSave));
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Feed rows/i }));
    expect(screen.getByRole("textbox", { name: "Rows" })).toHaveValue("[]");
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("keeps an intentionally empty rows array distinct from invalid JSON", async () => {
    const user = userEvent.setup();
    const boundary = prepareTabularRoundtrip({
      operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.feed_dataset",
    });
    await user.click(await renderTabularStepButton());
    fireEvent.change(screen.getByRole("textbox", { name: "Rows" }), { target: { value: "[]" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.storedConfig().rows).toEqual([]);
  });

  it("removes an invalid mapping action without blocking the remaining valid action", async () => {
    const user = userEvent.setup();
    const boundary = prepareTabularRoundtrip({
      operation: "transform", input: { dataset_handle: "state.existing_dataset" },
      result_target: "state.feed_dataset",
      actions: [
        { action: "map.values", target: "status", params: { mapping: { A: "Active" }, keep_unmapped: true } },
        { action: "map.values", target: "region", params: { mapping: { EU: "Europe" }, keep_unmapped: false } },
      ],
    });
    await user.click(await renderTabularStepButton());
    const mappings = await screen.findAllByRole("textbox", { name: "mapping" });
    expect(mappings).toHaveLength(2);
    fireEvent.change(mappings[0], { target: { value: '{"A":' } });
    expect(mappings[0]).toHaveValue('{"A":');
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    expect(boundary.writes).toHaveLength(0);
    const removals = screen.getAllByRole("button", { name: "Remove" });
    expect(removals).toHaveLength(2);
    await user.click(removals[0]);
    expect(screen.getAllByRole("textbox", { name: "mapping" })).toHaveLength(1);
    expect(JSON.parse((screen.getByRole("textbox", { name: "mapping" }) as HTMLTextAreaElement).value)).toEqual({ EU: "Europe" });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.storedConfig().actions).toEqual([
      { action: "map.values", target: "region", params: { mapping: { EU: "Europe" }, keep_unmapped: false } },
    ]);
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Feed rows/i }));
    expect(await screen.findAllByRole("textbox", { name: "mapping" })).toHaveLength(1);
    expect(JSON.parse((screen.getByRole("textbox", { name: "mapping" }) as HTMLTextAreaElement).value)).toEqual({ EU: "Europe" });
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("accepts a valid Advanced replacement after an invalid rows draft", async () => {
    const user = userEvent.setup();
    const boundary = prepareTabularRoundtrip({
      operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.feed_dataset",
    });
    await user.click(await renderTabularStepButton());
    fireEvent.change(screen.getByRole("textbox", { name: "Rows" }), { target: { value: "[{" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    expect(boundary.writes).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Advanced mode" }));
    await user.click(screen.getByRole("tab", { name: "Advanced" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Discard invalid input?" })).getByRole("button", { name: "Discard input" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Override JSON" }), {
      target: { value: JSON.stringify({ tabular_feed: {
        operation: "create_dataset", rows: [{ sku: "B2" }], result_target: "state.replaced_dataset",
      } }) },
    });
    await user.click(screen.getByRole("tab", { name: "Smart setup" }));
    expect(JSON.parse((screen.getByRole("textbox", { name: "Rows" }) as HTMLTextAreaElement).value)).toEqual([{ sku: "B2" }]);
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.storedConfig()).toMatchObject({
      operation: "create_dataset", rows: [{ sku: "B2" }], result_target: "state.replaced_dataset",
    });
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Feed rows/i }));
    expect(JSON.parse((screen.getByRole("textbox", { name: "Rows" }) as HTMLTextAreaElement).value)).toEqual([{ sku: "B2" }]);
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it.each(["A1", "B2"])("does not carry a rows error into another step whose source contains %s", async (otherSku) => {
    const user = userEvent.setup();
    const boundary = prepareTabularRoundtrip(
      { operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.feed_dataset" },
      { operation: "create_dataset", rows: [{ sku: otherSku }], result_target: "state.other_dataset" },
    );
    await user.click(await renderTabularStepButton());
    fireEvent.change(screen.getByRole("textbox", { name: "Rows" }), { target: { value: "[{" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    expect(boundary.writes).toHaveLength(0);
    // Select directly from the real canvas, without closing the first editor.
    await user.click(screen.getByRole("button", { name: /Other feed/i }));
    await user.click(within(await screen.findByRole("dialog", { name: "Discard unsaved parameters?" })).getByRole("button", { name: "Discard parameters" }));
    expect(JSON.parse((screen.getByRole("textbox", { name: "Rows" }) as HTMLTextAreaElement).value)).toEqual([{ sku: otherSku }]);
    fireEvent.change(screen.getByRole("textbox", { name: "Result target" }), {
      target: { value: "state.other_saved" },
    });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.writes[0]).toMatchObject({
      override: { tabular_feed: { rows: [{ sku: otherSku }], result_target: "state.other_saved" } },
    });
    expect(boundary.storedConfig()).toEqual({
      operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.feed_dataset",
    });
    expect(boundary.otherStoredConfig()).toMatchObject({ rows: [{ sku: otherSku }], result_target: "state.other_saved" });
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("creates a new write step with the visible CSV default", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report", display_name: "Weekly Report", enabled: true,
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({ validation: { valid: true, errors: [], warnings: [] } } as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "+ Add Step" }, { timeout: 5_000 }));
    fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "tabular_feed" } });
    chooseSetupOption(screen.getByRole("combobox", { name: "Operation" }), "write");
    fireEvent.change(screen.getByLabelText("Dataset handle"), { target: { value: "state.feed_dataset" } });
    expect(screen.getByRole("combobox", { name: "Output format" })).toHaveTextContent("csv");
    fireEvent.click(screen.getByRole("button", { name: "Add step to changes" }));
    fireEvent.click(await screen.findByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(savedStepBodies().length).toBeGreaterThan(0));
    expect((savedStepBodies()[0].override as Record<string, unknown>).tabular_feed).toMatchObject({ operation: "write", format: "csv", input: { dataset_handle: "state.feed_dataset" } });
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("shows a failed step save as an alert, and a saved one as a plain note", async () => {
    const stepId = "11111111-1111-1111-1111-111111111052";
    const detail = {
      revision: 1,
      workflow_id: "weekly_report", display_name: "Weekly Report", enabled: true,
      steps: [{
        id: stepId, step_id: "feed", display_name: "Feed export", step_kind: "tabular_feed",
        override: { tabular_feed: { operation: "read", input: { artifact_handle: "state.feed_artifact" }, result_target: "state.export" } },
      }], groups: [],
    };
    mockedFetchWorkflowDetail.mockResolvedValue(detail as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({ validation: { valid: true, errors: [], warnings: [] } } as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Feed export/i }, { timeout: 5_000 }));
    const pageFetch = fetchSpy.getMockImplementation();
    let failNextSave = true;
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/changes") && init?.method === "POST") {
        if (failNextSave) {
          failNextSave = false;
          return new Response(
            JSON.stringify({ detail: "new row violates check constraint" }),
            { status: 500, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({ workflow_id: "weekly_report", revision: 2, valid: true, issues: [], workflow: {} }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return pageFetch!(input, init);
    });
    editStepLabel();
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    await screen.findByText(/violates check constraint/);
    const failure = screen.getAllByRole("alert").find((element) =>
      element.textContent?.includes("violates check constraint"));
    expect(failure).toHaveTextContent("Not saved");

    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(screen.queryAllByRole("alert").some((element) => element.textContent?.includes("Step parameters saved."))).toBe(false);
    expect(screen.queryByText(/violates check constraint/)).not.toBeInTheDocument();
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it.each(["read", "write"])("saves and reopens the visible CSV default from a %s draft without touching the format selector", async (operation) => {
    const stepId = "11111111-1111-1111-1111-111111111051";
    const detail = {
      revision: 1,
      workflow_id: "weekly_report", display_name: "Weekly Report", enabled: true,
      steps: [{
        id: stepId, step_id: "feed", display_name: "Feed export", step_kind: "tabular_feed",
        override: { tabular_feed: { operation, input: operation === "read"
          ? { artifact_handle: "state.feed_artifact" }
          : { dataset_handle: "state.feed_dataset" }, result_target: "state.export" } },
      }], groups: [],
    };
    mockedFetchWorkflowDetail.mockResolvedValue(detail as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({ validation: { valid: true, errors: [], warnings: [] } } as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Feed export/i }, { timeout: 5_000 }));
    if (operation === "read") {
      chooseSetupOption(screen.getByRole("combobox", { name: "Operation" }), "write");
      fireEvent.change(screen.getByLabelText("Dataset handle"), { target: { value: "state.feed_dataset" } });
    }
    expect(screen.getByRole("combobox", { name: "Output format" })).toHaveTextContent("csv");
    fireEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
    fireEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    expect(JSON.parse((screen.getByLabelText("Override JSON") as HTMLTextAreaElement).value).tabular_feed.format).toBe("csv");
    fireEvent.click(screen.getByRole("tab", { name: "Smart setup" }));
    const pageFetch = fetchSpy.getMockImplementation();
    fetchSpy.mockImplementation(async (input, init) => {
      if (!(String(input).endsWith("/changes") && init?.method === "POST")) return pageFetch!(input, init);
      const saved = { ...detail, revision: 2, steps: [{ ...detail.steps[0], override: { tabular_feed: { operation: "write", format: "csv", input: { dataset_handle: "state.feed_dataset" }, result_target: "state.export" } } }], groups: [] };
      mockedFetchWorkflowDetail.mockResolvedValue(saved as never);
      return new Response(JSON.stringify({ workflow_id: "weekly_report", revision: 2, valid: true, issues: [], workflow: {} }), { status: 200, headers: { "content-type": "application/json" } });
    });
    editStepLabel();
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(savedStepBodies().length).toBeGreaterThan(0));
    const saveCall = fetchSpy.mock.calls.find(([url, options]) => String(url).endsWith("/changes") && options?.method === "POST");
    const payload = stepBodyFromCall(saveCall);
    expect(payload.override.tabular_feed).toMatchObject({ operation: "write", format: "csv", input: { dataset_handle: "state.feed_dataset" } });
    await screen.findByText("Parameters saved");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: /Feed export/i }));
    expect(screen.getByRole("combobox", { name: "Output format" })).toHaveTextContent("csv");
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("saves a manual TSV choice after saving and reopening the same CSV step", async () => {
    const stepId = "11111111-1111-1111-1111-111111111051";
    const detail = {
      revision: 1,
      workflow_id: "weekly_report", display_name: "Weekly Report", enabled: true,
      steps: [{
        id: stepId, step_id: "feed", display_name: "Feed export", step_kind: "tabular_feed",
        override: { tabular_feed: { operation: "write", format: "csv", input: { dataset_handle: "state.feed_dataset" }, result_target: "state.export" } },
      }], groups: [],
    };
    mockedFetchWorkflowDetail.mockResolvedValue(detail as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({ validation: { valid: true, errors: [], warnings: [] } } as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Feed export/i }, { timeout: 5_000 }));
    expect(screen.getByRole("combobox", { name: "Output format" })).toHaveTextContent("csv");
    const pageFetch = fetchSpy.getMockImplementation();
    fetchSpy.mockImplementation(async (input, init) => {
      if (!(String(input).endsWith("/changes") && init?.method === "POST")) return pageFetch!(input, init);
      mockedFetchWorkflowDetail.mockResolvedValue({
        ...detail,
        revision: 2,
        steps: [{ ...detail.steps[0] }], groups: [],
      } as never);
      return new Response(JSON.stringify({ workflow_id: "weekly_report", revision: 2, valid: true, issues: [], workflow: {} }), { status: 200, headers: { "content-type": "application/json" } });
    });
    const detailFetchesBeforeSave = mockedFetchWorkflowDetail.mock.calls.length;
    editStepLabel();
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(fetchSpy.mock.calls.filter(([url, options]) => String(url).endsWith("/changes") && options?.method === "POST")).toHaveLength(1));
    const csvSaveCall = fetchSpy.mock.calls.find(([url, options]) => String(url).endsWith("/changes") && options?.method === "POST");
    expect(stepBodyFromCall(csvSaveCall).override.tabular_feed).toMatchObject({
      operation: "write", format: "csv", input: { dataset_handle: "state.feed_dataset" },
    });
    await screen.findByText("Parameters saved");
    expect(mockedFetchWorkflowDetail.mock.calls.length).toBeGreaterThan(detailFetchesBeforeSave);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: /Feed export/i }));
    expect(screen.getByRole("combobox", { name: "Output format" })).toHaveTextContent("csv");
    chooseSetupOption(screen.getByRole("combobox", { name: "Output format" }), "tsv");
    chooseSetupOption(screen.getByRole("combobox", { name: "Operation" }), "read");
    chooseSetupOption(screen.getByRole("combobox", { name: "Operation" }), "write");
    fireEvent.change(screen.getByLabelText("Dataset handle"), { target: { value: "state.feed_dataset" } });
    expect(screen.getByRole("combobox", { name: "Output format" })).toHaveTextContent("tsv");
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(fetchSpy.mock.calls.filter(([url, options]) => String(url).endsWith("/changes") && options?.method === "POST")).toHaveLength(2));
    const tsvSaveCall = fetchSpy.mock.calls.filter(([url, options]) => String(url).endsWith("/changes") && options?.method === "POST").at(-1);
    expect(stepBodyFromCall(tsvSaveCall).override.tabular_feed).toMatchObject({
      operation: "write", format: "tsv", input: { dataset_handle: "state.feed_dataset" },
    });
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("saves read to transform without the hidden artifact input", async () => {
    const stepId = "11111111-1111-1111-1111-111111111050";
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
          id: stepId,
          step_id: "read_feed",
          display_name: "Read feed",
          step_kind: "tabular_feed",
          override: {
            tabular_feed: {
              operation: "read",
              input: { artifact_handle: "state.feed_artifact" },
              result_target: "state.feed_dataset",
            },
          },
        }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Read feed/i }));
    chooseSetupOption(screen.getByRole("combobox", { name: "Operation" }), "transform");
    expect(screen.queryByLabelText("Artifact handle")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Dataset handle"), {
      target: { value: "state.previous_dataset.dataset_handle" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add operation" }));
    fireEvent.change(screen.getByLabelText("Filter column"), {
      target: { value: "status" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Value" }), {
      target: { value: "active" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    await waitFor(() => {
      expect(savedStepBodies().length).toBeGreaterThan(0);
    });
    const saveCall = fetchSpy.mock.calls.find(([url, options]) => String(url).endsWith("/changes") && options?.method === "POST");
    const payload = stepBodyFromCall(saveCall);
    expect(payload.override.tabular_feed).toMatchObject({
      operation: "transform",
      result_target: "state.feed_dataset",
      relational: [{ op: "filter", when: { column: "status", equals: "active" } }],
    });
    expect(payload.override.tabular_feed.input).toEqual({ dataset_handle: "state.previous_dataset.dataset_handle" });
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("marks a generic OAuth connection ineligible when an operation scope is absent", () => {
    expect(extensionConnectionEligibility({
      connection_type: "resource_account",
      oauth_lifecycle_status: "connected",
      oauth_granted_scopes: ["resource.read"],
    }, {
      connectionTypeIds: ["resource_account"],
      requiredScopes: ["resource.read", "resource.write"],
    })).toMatchObject({
      allowed: false,
      applicable: true,
      optionSuffix: "Additional access required",
    });
  });

  it("renders typed Smart Setup fields for every supported step kind", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111010",
              step_id: "step_1",
              display_name: "Step one",
              step_kind: "agent",
              agent_id: "writer",
              override: {},
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    // A saved step keeps its kind; every kind is chosen when a step is added.
    fireEvent.click(
      await screen.findByRole("button", { name: "+ Add Step" }, { timeout: 5_000 }),
    );
    expect(
      await screen.findByRole("heading", { name: "Step editor" }),
    ).toBeInTheDocument();

    const stepKindSelect = screen.getByRole("combobox", { name: "Step kind" });
    expect(
      Array.from((stepKindSelect as HTMLSelectElement).options).map(
        (option) => option.value,
      ),
    ).not.toContain("transform_data");
    expect(
      Array.from((stepKindSelect as HTMLSelectElement).options).find(
        (option) => option.value === "connector",
      )?.text,
    ).toBe("System tools");
    // Collect, Break and Continue live only in a sequence For each body; a root step never offers them.
    for (const loopOnly of ["collect", "break", "continue", "for_each"]) {
      expect(
        Array.from((stepKindSelect as HTMLSelectElement).options).map((option) => option.value),
      ).not.toContain(loopOnly);
    }
    const checks: Array<[string, RegExp[]]> = [
      ["agent", [/Agent$/]],
      ["if", [/Selector$/, /Operator$/]],
      ["switch", [/Selector$/, /^Cases$/]],
      ["approval", [/^Approval request$/, /^Message$/, /Outputs shown to the reviewer$/]],
      ["wait", [/^Wait duration$/, /Fixed duration$/, /Duration, milliseconds$/]],
      [
        "connector",
        // No action chosen yet: nothing to map, so no Parameters or Output mapping (item 6).
        [/Tool$/, /^Action$/],
      ],
      [
        "http_request",
        [
          /Method$/,
          /URL$/,
          /Response target$/,
          /Headers$/,
          /Query parameters$/,
          /Body$/,
        ],
      ],
      ["invoke_workflow", [/Workflow$/, /Input mapping$/, /Output mapping$/]],
      ["set_variable", [/Assignments$/]],
      [
        "batch",
        [
          /Source path$/,
          /Result target$/,
          /Batch size$/,
          /Emit partial final batch$/,
        ],
      ],
      [
        "transform",
        [/Result target$/, /Mode$/, /Output$/, /Fields$/, /Sample JSON$/, /Preview$/],
      ],
    ];

    for (const [stepKind, labels] of checks) {
      fireEvent.change(stepKindSelect, { target: { value: stepKind } });
      for (const label of labels) {
        // HTTP map labels share their text with the Add row button; assert
        // the actual DS field label. Other kinds retain their existing lookup.
        expect(await screen.findByText(label, { selector: stepKind === "http_request" ? ".fs-field-label" : ":not(option)" })).toBeInTheDocument();
      }
      if (stepKind === "connector") {
        expect(screen.queryByText(/Parameters$/, { selector: ":not(option)" })).not.toBeInTheDocument();
        expect(screen.queryByText(/Output mapping$/, { selector: ":not(option)" })).not.toBeInTheDocument();
        // A new action step with no connection rows and no resolved install: the host owns
        // connection_binding, so the author never sees a raw key/value editor for it.
        expect(screen.queryByText(/Connection binding/)).not.toBeInTheDocument();
        expect(screen.queryByRole("button", { name: "Add Connection binding" })).not.toBeInTheDocument();
        expect(screen.queryByTestId("connector-imported-binding")).not.toBeInTheDocument();
      }
    }
  }, 15_000);

  it("edits transform mappings from the action catalog and runs preview", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111041",
              step_id: "normalize_customer",
              display_name: "Normalize customer",
              step_kind: "transform",
              override: {
                transform: {
                  input: { source: "state.inputs.customer" },
                  mode: "object",
                  result_target: "state.normalized_customer",
                  output_mode: "mapped_only",
                  fields: [
                    {
                      from: "name",
                      to: "full_name",
                      type: "text",
                      actions: [{ action: "text.trim", params: {} }],
                    },
                  ],
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Normalize customer/i }));
    expect(await screen.findByText("Fields")).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: "text / Append" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Type" }), {
      target: { value: "number" },
    });
    expect(screen.getByRole("option", { name: "text / Trim" })).toBeInTheDocument();

    const actionSelect = screen.getByRole("combobox", { name: "Action" });
    fireEvent.change(actionSelect, { target: { value: "text.append" } });
    expect(await screen.findByText("append_value")).toBeInTheDocument();

    const sourceModeSelect = screen.getByRole("combobox", { name: "append_value" });
    fireEvent.change(sourceModeSelect, { target: { value: "selector" } });
    const selectorParam = screen.getByLabelText("append_value source");
    expect(selectorParam).toHaveAttribute("placeholder", "customer.last_name");
    fireEvent.change(selectorParam, { target: { value: "last_name" } });

    fireEvent.click(screen.getByRole("button", { name: "Run preview" }));

    await waitFor(() => {
      expect(mockedPreviewWorkflowTransform).toHaveBeenCalledWith(
        "weekly_report",
        expect.objectContaining({
          result_target: "state.normalized_customer",
        }),
        expect.objectContaining({ name: " Ada " }),
      );
    });
    expect((await screen.findAllByText(/Ada Lovelace/)).length).toBeGreaterThan(0);
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("keeps transform field focus while typing multi-character output paths", async () => {
    const user = userEvent.setup();
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111044",
              step_id: "normalize_customer",
              display_name: "Normalize customer",
              step_kind: "transform",
              override: {
                transform: {
                  input: { source: "state.inputs.customer" },
                  mode: "object",
                  result_target: "state.normalized_customer",
                  output_mode: "mapped_only",
                  fields: [
                    {
                      from: "name",
                      to: "x",
                      type: "text",
                    },
                  ],
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Normalize customer/i }));
    const outputPath = await screen.findByDisplayValue("x");

    await user.clear(outputPath);
    await user.type(outputPath, "customer_full_name");

    expect(screen.getByDisplayValue("customer_full_name")).toHaveFocus();
  });

  it("adds transform actions from the previous pipeline output type", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111042",
              step_id: "normalize_customer",
              display_name: "Normalize customer",
              step_kind: "transform",
              override: {
                transform: {
                  input: { source: "state.inputs.customer" },
                  mode: "object",
                  result_target: "state.normalized_customer",
                  output_mode: "mapped_only",
                  fields: [
                    {
                      from: "name",
                      to: "name_length",
                      type: "number",
                      actions: [{ action: "text.trim", params: {} }],
                    },
                  ],
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Normalize customer/i }));
    expect(await screen.findByText("Fields")).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: "text / Append" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add action" }));

    await waitFor(() => {
      expect(screen.getAllByRole("combobox", { name: "Action" })).toHaveLength(2);
    });
    expect(screen.getAllByRole("combobox", { name: "Action" })[1]).toHaveValue("text.append");
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("defaults the first transform action from the declared field type", async () => {
    mockedFetchWorkflowTransformActions.mockResolvedValue({
      actions: [
        {
          action_id: "array.length",
          category: "array",
          display_name: "Length",
          value_scope: "array",
          input_type: "array",
          output_type: "number",
          params_schema: { type: "object", properties: {}, additionalProperties: false },
          param_sources: {},
          pure: true,
          deterministic: true,
          io_allowed: false,
          transform_safe: true,
        },
        {
          action_id: "text.trim",
          category: "text",
          display_name: "Trim",
          value_scope: "scalar",
          input_type: "text",
          output_type: "text",
          params_schema: { type: "object", properties: {}, additionalProperties: false },
          param_sources: {},
          pure: true,
          deterministic: true,
          io_allowed: false,
          transform_safe: true,
        },
      ],
    } as never);
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111045",
              step_id: "normalize_customer",
              display_name: "Normalize customer",
              step_kind: "transform",
              override: {
                transform: {
                  input: { source: "state.inputs.customer" },
                  mode: "object",
                  result_target: "state.normalized_customer",
                  output_mode: "mapped_only",
                  fields: [{ from: "name", to: "full_name", type: "text" }],
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Normalize customer/i }));
    await waitFor(() => {
      expect(mockedFetchWorkflowTransformActions).toHaveBeenCalled();
    });
    const addAction = screen.getByRole("button", { name: "Add action" });
    await waitFor(() => {
      expect(addAction).toBeEnabled();
    });
    fireEvent.click(addAction);

    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Action" })).toHaveValue("text.trim");
    });
  });

  it("filters transform actions strictly after a json pipeline output", async () => {
    mockedFetchWorkflowTransformActions.mockResolvedValue({
      actions: [
        {
          action_id: "json.get",
          category: "json",
          display_name: "Get Path",
          value_scope: "json",
          input_type: "json",
          output_type: "json",
          params_schema: { type: "object", properties: { path: { type: "string" } } },
          param_sources: {},
          pure: true,
          deterministic: true,
          io_allowed: false,
          transform_safe: true,
        },
        {
          action_id: "json.stringify",
          category: "json",
          display_name: "Stringify",
          value_scope: "json",
          input_type: "json",
          output_type: "text",
          params_schema: { type: "object", properties: {}, additionalProperties: false },
          param_sources: {},
          pure: true,
          deterministic: true,
          io_allowed: false,
          transform_safe: true,
        },
        {
          action_id: "text.trim",
          category: "text",
          display_name: "Trim",
          value_scope: "scalar",
          input_type: "text",
          output_type: "text",
          params_schema: { type: "object", properties: {}, additionalProperties: false },
          param_sources: {},
          pure: true,
          deterministic: true,
          io_allowed: false,
          transform_safe: true,
        },
        {
          action_id: "array.compact",
          category: "array",
          display_name: "Compact",
          value_scope: "array",
          input_type: "array",
          output_type: "array",
          params_schema: { type: "object", properties: {}, additionalProperties: false },
          param_sources: {},
          pure: true,
          deterministic: true,
          io_allowed: false,
          transform_safe: true,
        },
      ],
    } as never);
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111043",
              step_id: "normalize_customer",
              display_name: "Normalize customer",
              step_kind: "transform",
              override: {
                transform: {
                  input: { source: "state.inputs.customer" },
                  mode: "object",
                  result_target: "state.normalized_customer",
                  output_mode: "mapped_only",
                  fields: [
                    {
                      from: "meta",
                      to: "external",
                      type: "json",
                      actions: [
                        { action: "json.get", params: { path: "external-id" } },
                        { action: "", params: {} },
                      ],
                    },
                  ],
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Normalize customer/i }));
    expect(await screen.findAllByRole("option", { name: "json / Stringify" })).toHaveLength(2);

    const [, secondActionSelect] = screen.getAllByRole("combobox", { name: "Action" });
    const secondOptions = within(secondActionSelect).getAllByRole("option").map((option) => option.textContent);

    expect(secondOptions).toContain("json / Stringify");
    expect(secondOptions).not.toContain("text / Trim");
    expect(secondOptions).not.toContain("array / Compact");
  });

  it("moves transform wildcard mappings from actions to actions_each", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111042",
              step_id: "extract_comment_names",
              display_name: "Extract comment names",
              step_kind: "transform",
              override: {
                transform: {
                  input: { source: "state.inputs.ticket" },
                  mode: "object",
                  result_target: "state.comment_names",
                  output_mode: "mapped_only",
                  fields: [
                    {
                      from: "title",
                      to: "names",
                      type: "array",
                      item_type: "text",
                      actions: [{ action: "text.trim", params: {} }],
                    },
                  ],
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Extract comment names/i }));
    fireEvent.change(screen.getAllByPlaceholderText("customer.email")[0], {
      target: { value: "comments[*].user.username" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Run preview" }));

    await waitFor(() => {
      const config = mockedPreviewWorkflowTransform.mock.calls.at(-1)?.[1] as
        | Record<string, unknown>
        | undefined;
      const fields = config?.fields as Array<Record<string, unknown>>;
      expect(fields[0].actions).toBeUndefined();
      expect(fields[0].actions_each).toEqual([{ action: "text.trim", params: {} }]);
    });
  });

  it("stores a list row filter only with a selector and shows validation messages", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111043",
              step_id: "prepare_variants",
              display_name: "Prepare variants",
              step_kind: "transform",
              override: {
                transform: {
                  input: { source: "state.current_item" },
                  mode: "object",
                  result_target: "state.new_variants",
                  output_mode: "mapped_only",
                  fields: [{ from: "SKU", to: "sku", type: "text", actions: [] }],
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    mockedPreviewWorkflowTransform.mockResolvedValue({
      ok: false,
      errors: [
        {
          code: "workflow.transform_input_source_missing",
          message: "transform input.source 'state.current_item' is not produced by earlier steps",
          path: "workflow.steps.prepare_variants.transform.input.source",
          severity: "error",
        },
      ],
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Prepare variants/i }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Mode" }), {
      target: { value: "list" },
    });
    expect(screen.getByRole("combobox", { name: "Operator" })).toBeDisabled();

    const rowSelector = screen.getByPlaceholderText("status");
    fireEvent.change(rowSelector, { target: { value: "status" } });
    fireEvent.change(rowSelector, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Run preview" }));

    await waitFor(() => {
      const config = mockedPreviewWorkflowTransform.mock.calls.at(-1)?.[1] as
        | Record<string, unknown>
        | undefined;
      expect(config?.mode).toBe("list");
      expect(config).not.toHaveProperty("row_filter");
    });
    expect(
      await screen.findByText(
        /is not produced by earlier steps · workflow\.steps\.prepare_variants\.transform\.input\.source/,
      ),
    ).toBeInTheDocument();

    fireEvent.change(rowSelector, { target: { value: "status" } });
    fireEvent.click(screen.getByRole("button", { name: "Run preview" }));

    await waitFor(() => {
      const config = mockedPreviewWorkflowTransform.mock.calls.at(-1)?.[1] as
        | Record<string, unknown>
        | undefined;
      expect(config?.row_filter).toEqual({ operator: "equals", selector: "status" });
    });
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("shows a list-mode preview output, which is an array", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111044",
              step_id: "prepare_variants",
              display_name: "Prepare variants",
              step_kind: "transform",
              override: {
                transform: {
                  input: { source: "state.current_item" },
                  mode: "list",
                  result_target: "state.new_variants",
                  output_mode: "mapped_only",
                  fields: [{ from: "SKU", to: "sku", type: "text", actions: [] }],
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    mockedPreviewWorkflowTransform.mockResolvedValue({
      ok: true,
      errors: [],
      output: [{ sku: "WINE-001" }],
      state_patch: { new_variants: [{ sku: "WINE-001" }] },
      preview: true,
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Prepare variants/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Run preview" }));

    // Once in Output and once in State patch; an array output used to render empty.
    await waitFor(() => {
      expect(screen.getAllByText(/"sku": "WINE-001"/)).toHaveLength(2);
    });
  });

  it("moves a step into a parallel lane explicitly, dropping its old connections", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 4,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: false,
      steps: [
        { step_id: "write_launch_copy", display_name: "Write launch copy", step_kind: "agent", agent_id: "writer", override: {} },
        { step_id: "review_launch_risks", display_name: "Review launch risks", step_kind: "agent", agent_id: "reviewer", group_id: "fanout", lane_id: "risk", override: {} },
      ],
      groups: [{
        group_id: "fanout",
        display_name: "Fan out",
        body: { kind: "lanes", lanes: [{ lane_id: "copy" }, { lane_id: "risk", entry: { kind: "step", id: "review_launch_risks" } }] },
      }],
      settings: {
        execution_graph: {
          schema_version: 1,
          entry: { kind: "step", id: "write_launch_copy" },
          edges: [{ source: { kind: "step", id: "write_launch_copy" }, outcome: { name: "success" }, target: { kind: "group", id: "fanout" } }],
        },
      },
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Write launch copy/i }));
    expect(await screen.findByRole("heading", { name: "Step editor" })).toBeInTheDocument();
    const structure = screen.getByTestId("step-structure-setup");
    expect(structure).toHaveTextContent("Now in the workflow root.");
    // The entry is set from the step's menus, not from the editor.
    expect(structure).not.toHaveTextContent("The workflow starts with this step.");
    // Lane membership is no longer a free-text field on the step.
    expect(screen.queryByLabelText("Parallel lane id")).not.toBeInTheDocument();

    fireEvent.click(within(structure).getByRole("combobox", { name: "Move step to" }));
    fireEvent.click(screen.getByRole("option", { name: "Fan out · copy" }));
    // The choice asks beside the control and names, by step name, the connections the move removes.
    const confirm = within(structure).getByRole("group", { name: "Move it to Fan out · copy?" });
    expect(within(confirm).getByText(/^Write launch copy · .+ → Fan out$/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Move this step?" })).not.toBeInTheDocument();
    fireEvent.click(within(confirm).getByRole("button", { name: "Move step" }));

    await waitFor(() => {
      expect(screen.getByTestId("step-structure-setup")).toHaveTextContent("Now in Fan out · copy.");
    });
    fireEvent.click(within(screen.getByTestId("workflow-change-set-bar")).getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    // Lane copy had no entry yet: the moved step starts it.
    expect(postedChangeSets()[0]).toEqual({
      expected_revision: 4,
      operations: [
        { op: "move_step", step_id: "write_launch_copy", group_id: "fanout", for_each_id: null, lane_id: "copy" },
        { op: "set_entry", target: { kind: "step", id: "write_launch_copy" }, group_id: "fanout", lane_id: "copy" },
      ],
    });
  });

  it("hydrates connector smart setup from persisted mapping fields", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111011",
              step_id: "create_issue",
              display_name: "Create issue",
              step_kind: "connector",
              connector_id: "github",
              override: { connector: { function_name: "create_issue" } },
              input_mapping_jsonb: { title: "state.issue_title" },
              output_mapping_jsonb: { issue_url: "state.issue_url" },
              connection_binding_jsonb: { connection_id: "conn_1" },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /Create issue/i }),
    );
    expect(
      await screen.findByRole("heading", { name: "Step editor" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Action source" })).toHaveTextContent("GitHub"));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Action" })).toHaveTextContent("Create issue"));
    // The action contract is reference material: closed under Action details, no source chip.
    expect(await screen.findByText("Action details")).toBeVisible();
    expect(screen.queryByText("Tool registry")).not.toBeInTheDocument();
    expect(await screen.findByText("Action inputs")).not.toBeVisible();
    expect(await screen.findByText("Action returns")).toBeInTheDocument();
    expect(await screen.findByDisplayValue("title")).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Parameter source 1" }),
    ).toHaveValue("state");
    expect(
      await screen.findByDisplayValue("state.issue_title"),
    ).toBeInTheDocument();
    expect(await screen.findByDisplayValue("issue_url")).toBeInTheDocument();
    expect(
      await screen.findByDisplayValue("state.issue_url"),
    ).toBeInTheDocument();
    // AC17-R65: the Connection select owns the binding. The repeater used to render the same
    // connection_id/conn_1 pair a second time as editable free text, which is what the owner
    // hit: the field deciding step validity was an unlabelled box keyed by a guessed name.
    expect(screen.queryByDisplayValue("connection_id")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Connection" })).toHaveTextContent("Primary GitHub"));
  });

  it("shows action contracts and preserves literal mappings on save", async () => {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(
          JSON.stringify({
            connectors: [
              {
                connector_id: "connector.sample_content_tools",
                display_name: "Demo Content Tools",
                description: "Demo deterministic content helpers",
                source: "registry_tool",
                tool_id: "sample_content_tools",
                tool_url: "/registry/tools/sample_content_tools",
                operations: [
                  {
                    operation_id: "demo_compose_content_brief",
                    display_name: "Compose content brief",
                    description: "Compose a compact content brief.",
                    parameters: [
                      { name: "topic", type: "string", required: true },
                      { name: "tone", type: "string" },
                    ],
                    returns: {
                      name: "content_brief",
                      type: "json",
                      required: true,
                    },
                  },
                ],
                connections: [],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "sample_content_campaign_router",
      display_name: "Demo Content Campaign Router",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111031",
              step_id: "compose_brief",
              display_name: "Compose content brief",
              step_kind: "connector",
              connector_id: "connector.sample_content_tools",
              override: {
                connector: {
                  connector_id: "connector.sample_content_tools",
                  operation_id: "demo_compose_content_brief",
                },
              },
              input_mapping_jsonb: {
                topic: "literal:Flow Steward demo workflow extension",
                tone: "literal:clear",
              },
              output_mapping_jsonb: { content_brief: "state.content_brief" },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /Compose content brief/i }),
    );
    await waitFor(() => expect(
      screen.getByRole("combobox", { name: "Tool" }),
    ).toHaveTextContent("Demo Content Tools"));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Action" })).toHaveTextContent("Compose content brief"));
    expect(await screen.findByText("Open tool")).toHaveAttribute(
      "href",
      "/registry/tools/sample_content_tools",
    );
    expect(
      screen.getByRole("combobox", { name: "Parameter source 1" }),
    ).toHaveValue("literal");
    expect(
      screen.getByRole("textbox", { name: "Parameter value 1" }),
    ).toHaveValue("Flow Steward demo workflow extension");
    expect(
      screen.getByRole("combobox", { name: "Parameter source 2" }),
    ).toHaveValue("literal");
    expect(
      screen.getByRole("textbox", { name: "Parameter value 2" }),
    ).toHaveValue("clear");
    expect(screen.queryByDisplayValue("literal:clear")).not.toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Returned field 1" }),
    ).toHaveValue("content_brief");
    expect(
      screen.getByRole("textbox", { name: "Output target 1" }),
    ).toHaveValue("state.content_brief");
    expect(
      screen.queryByRole("combobox", { name: "Connection" }),
    ).not.toBeInTheDocument();

    editStepLabel();
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    await waitFor(() => expect(savedStepBodies().length).toBeGreaterThan(0));
    const saveCall = savedStepCall();
    expect(
      stepBodyFromCall(saveCall).override,
    ).toMatchObject({
      connector: {
        operation_id: "demo_compose_content_brief",
        input_mapping: {
          topic: "literal:Flow Steward demo workflow extension",
          tone: "literal:clear",
        },
        output_mapping: { content_brief: "state.content_brief" },
      },
    });
  });

  it("groups MCP actions under their connector and persists the selected projected tool", async () => {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(
          JSON.stringify({
            connectors: [
              {
                connector_id: "mcp_drive",
                display_name: "Drive",
                description: "Cloud file MCP connector",
                source: "mcp_connector",
                operations: [
                  {
                    operation_id: "drive.create_file",
                    display_name: "create_file",
                    runtime_connector_id: "connector.drive.create_file",
                    parameters: [],
                    returns: { name: "result", type: "json" },
                  },
                  {
                    operation_id: "drive.search_files",
                    display_name: "search_files",
                    runtime_connector_id: "connector.drive.search_files",
                    parameters: [],
                    returns: { name: "result", type: "json" },
                  },
                ],
                connections: [],
              },
              {
                connector_id: "extension.example.drive",
                display_name: "Drive",
                source: "extension",
                operations: [],
                connections: [],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "mcp_drive_audit",
      display_name: "MCP Drive Audit",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111099",
              step_id: "create_file",
              display_name: "Create file",
              step_kind: "connector",
              connector_id: "connector.drive.create_file",
              override: {
                connector: {
                  operation_id: "drive.create_file",
                },
              },
              input_mapping_jsonb: {},
              output_mapping_jsonb: { result: "state.result" },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Create file/i }));
    const toolSelect = await screen.findByRole("combobox", { name: "Tool" });
    await waitFor(() => expect(toolSelect).toHaveTextContent("Drive"));
    expect(
      setupOptions(toolSelect).getAllByRole("option", { name: "Drive" }),
    ).toHaveLength(1);
    expect(
      setupOptions(toolSelect).queryByRole("option", { name: "Drive / create_file" }),
    ).not.toBeInTheDocument();

    const actionSelect = screen.getByRole("combobox", { name: "Action" });
    await waitFor(() => expect(actionSelect).toHaveTextContent("create_file"));
    expect(
      setupOptions(actionSelect).getByRole("option", { name: "create_file" }),
    ).toBeInTheDocument();
    expect(
      setupOptions(actionSelect).getByRole("option", { name: "search_files" }),
    ).toBeInTheDocument();

    chooseSetupOption(actionSelect, "search_files");
    await waitFor(() => expect(toolSelect).toHaveTextContent("Drive"));
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    await waitFor(() => expect(savedStepBodies().length).toBeGreaterThan(0));
    const saveCall = savedStepCall();
    const body = stepBodyFromCall(saveCall);
    expect(body.connector_id).toBe("connector.drive.search_files");
    expect(body.override.connector.operation_id).toBe("drive.search_files");
  });

  it("saves standard tool actions with the connector step payload shape", async () => {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(
          JSON.stringify({
            connectors: [
              {
                connector_id: "connector.std.numbers",
                display_name: "Numbers",
                description: "Deterministic numeric Standard Tool Actions.",
                source: "standard_tool_action",
                category: "standard_tool_action",
                tool_id: "std.numbers",
                tool_url: "/registry/tools/std.numbers",
                operations: [
                  {
                    operation_id: "round",
                    display_name: "Round",
                    description: "Round a numeric value.",
                    source: "standard_tool_action",
                    category: "standard_tool_action",
                    parameters: [
                      { name: "value", type: "number", required: true },
                      { name: "decimals", type: "integer" },
                    ],
                    returns: {
                      name: "result",
                      type: "number",
                      required: true,
                    },
                  },
                ],
                connections: [],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "demo_math",
      display_name: "Demo Math",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111041",
              step_id: "round_price",
              display_name: "Round price",
              step_kind: "connector",
              connector_id: "connector.std.numbers",
              override: {
                connector: {
                  connector_id: "connector.std.numbers",
                  operation_id: "round",
                  input_mapping: {
                    value: "state.price",
                    decimals: "literal:2",
                  },
                  output_mapping: { result: "state.rounded_price" },
                },
              },
              input_mapping_jsonb: {
                value: "state.price",
                decimals: "literal:2",
              },
              output_mapping_jsonb: { result: "state.rounded_price" },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Round price/i }));
    // The Tool select names the source; no separate source chip repeats it.
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Tool" })).toHaveTextContent("Numbers"));
    expect(screen.queryByText("Tool Action", { exact: true })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Connection" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Action" })).toHaveTextContent("Round"));

    editStepLabel();
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    await waitFor(() => expect(savedStepBodies().length).toBeGreaterThan(0));
    const saveCall = savedStepCall();
    const body = stepBodyFromCall(saveCall);
    // A config patch never re-sends the step's kind.
    expect(body).not.toHaveProperty("step_kind");
    expect(body.connector_id).toBe("connector.std.numbers");
    expect(body.input_mapping_jsonb).toEqual({
      value: "state.price",
      decimals: "literal:2",
    });
    expect(body.output_mapping_jsonb).toEqual({
      result: "state.rounded_price",
    });
    expect(body.override).toMatchObject({
      connector: {
        operation_id: "round",
        input_mapping: {
          value: "state.price",
          decimals: "literal:2",
        },
        output_mapping: { result: "state.rounded_price" },
      },
    });
  });

  it("adds removes and disables duplicate connector mapping rows", async () => {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(
          JSON.stringify({
            connectors: [
              {
                connector_id: "connector.sample_content_tools",
                display_name: "Demo Content Tools",
                source: "registry_tool",
                tool_id: "sample_content_tools",
                tool_url: "/registry/tools/sample_content_tools",
                operations: [
                  {
                    operation_id: "demo_compose_content_brief",
                    display_name: "Compose content brief",
                    parameters: [
                      { name: "topic", type: "string", required: true },
                      { name: "tone", type: "string" },
                      { name: "channel", type: "string" },
                    ],
                    returns: [
                      { name: "content_brief", type: "json", required: true },
                      { name: "total", type: "integer" },
                    ],
                  },
                ],
                connections: [
                  { connection_id: "conn_1", display_name: "Primary" },
                ],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "sample_content_campaign_router",
      display_name: "Demo Content Campaign Router",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111031",
              step_id: "compose_brief",
              display_name: "Compose content brief",
              step_kind: "connector",
              connector_id: "connector.sample_content_tools",
              override: {
                connector: {
                  connector_id: "connector.sample_content_tools",
                  operation_id: "demo_compose_content_brief",
                },
              },
              input_mapping_jsonb: {
                topic: "literal:Flow Steward demo workflow extension",
                tone: "literal:clear",
              },
              output_mapping_jsonb: { content_brief: "state.content_brief" },
              // A binding carrying more than connection_id came from an imported definition:
              // it is shown read-only with a Clear action, never as a key/value editor.
              connection_binding_jsonb: { connection_id: "conn_1", tenant_hint: "eu" },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /Compose content brief/i }),
    );
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Add parameter mapping" }),
      ).toBeEnabled();
    });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Connection" })).toHaveTextContent("Primary"));
    const parameterOne = (await screen.findByRole("combobox", {
      name: "Parameter 1",
    })) as HTMLSelectElement;
    const disabledTone = Array.from(parameterOne.options).find(
      (option) => option.value === "tone",
    );
    expect(disabledTone).toBeDisabled();
    const sourceOne = screen.getByRole("combobox", {
      name: "Parameter source 1",
    }) as HTMLSelectElement;
    expect(Array.from(sourceOne.options).map((option) => option.text)).toEqual([
      "State path",
      "Literal value",
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Add parameter mapping" }));
    expect(screen.getByRole("combobox", { name: "Parameter 3" })).toHaveValue(
      "channel",
    );
    expect(screen.getByRole("textbox", { name: "Parameter value 3" })).toHaveValue(
      "state.channel",
    );
    expect(screen.getByRole("button", { name: "Add parameter mapping" })).toBeDisabled();

    fireEvent.click(
      screen.getByRole("button", { name: "Remove parameter mapping 2" }),
    );
    expect(screen.getAllByRole("combobox", { name: /Parameter \d+/ })).toHaveLength(2);
    expect(
      screen.getByRole("button", { name: "Add parameter mapping" }),
    ).toBeEnabled();

    fireEvent.change(screen.getByRole("combobox", { name: "Parameter 2" }), {
      target: { value: "" },
    });
    expect(screen.getAllByRole("combobox", { name: /Parameter \d+/ })).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Add output mapping" }));
    expect(screen.getByRole("combobox", { name: "Returned field 2" })).toHaveValue(
      "total",
    );
    expect(screen.getByRole("textbox", { name: "Output target 2" })).toHaveValue(
      "state.total",
    );
    expect(screen.getByRole("button", { name: "Add output mapping" })).toBeDisabled();

    // The imported custom binding is read-only: its extra key is listed, nothing is editable.
    const imported = screen.getByTestId("connector-imported-binding");
    expect(imported).toHaveTextContent("came from an imported definition");
    expect(imported).toHaveTextContent("tenant_hint: eu");
    expect(within(imported).queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add Connection binding" })).not.toBeInTheDocument();
    // Clear drops the imported keys and keeps the connection the Connection select owns.
    fireEvent.click(within(imported).getByRole("button", { name: "Clear connection settings" }));
    await waitFor(() => expect(screen.queryByTestId("connector-imported-binding")).not.toBeInTheDocument());
    expect(screen.getByRole("combobox", { name: "Connection" })).toHaveTextContent("Primary");
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  function mockIfAndSwitchWorkflow() {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/available-variables")) {
        return new Response(
          JSON.stringify({
            state: [{ name: "task_summary", type: "text" }],
            dotted_path: [{ name: "project_config.stack.language", type: "text" }],
            prior_outputs: [
              { name: "city_profile", step_id: "analysis", group_id: null, type: "json" },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          required_inputs: [],
          optional_inputs: [],
          custom_inputs: [],
          current_expected_inputs: [],
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      );
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "sample_weather_city_brief",
      display_name: "Demo Weather",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111021",
              step_id: "check_city_input",
              display_name: "Check city input",
              step_kind: "if",
              override: {
                if: {
                  condition: "state.inputs.city",
                },
              },
            },
            {
              id: "11111111-1111-1111-1111-111111111022",
              step_id: "route_by_city",
              display_name: "Route by city",
              step_kind: "switch",
              override: {
                switch: {
                  selector: "state.inputs.city",
                  cases: [{ case_id: "berlin", when: "Berlin" }],
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
  }

  it("edits if and switch conditions while their outcomes stay graph connections", async () => {
    mockIfAndSwitchWorkflow();

    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /Check city input/i }),
    );
    expect(
      await screen.findByRole("heading", { name: "Step editor" }),
    ).toBeInTheDocument();
    const ifSelectorInput = screen.getByDisplayValue("state.inputs.city");
    fireEvent.change(ifSelectorInput, {
      target: { value: "state.needs_upscale" },
    });
    expect(ifSelectorInput).toHaveValue("state.needs_upscale");
    expect(screen.getByRole("combobox", { name: "Operator" })).toHaveTextContent("truthy");
    // Targets are edges now: each outcome is a named port, not a target field.
    expect(screen.getByRole("combobox", { name: "Next step for True" })).toHaveTextContent("Not connected");
    expect(screen.getByRole("combobox", { name: "Next step for False" })).toHaveTextContent("Not connected");

    fireEvent.click(
      await screen.findByRole("button", { name: /Route by city/i }),
    );
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Discard unsaved parameters?" })).getByRole("button", { name: "Discard parameters" }));
    const switchSelectorInput = await screen.findByDisplayValue("state.inputs.city");
    fireEvent.focus(switchSelectorInput);
    await waitFor(() => {
      expect(switchSelectorInput).toHaveAttribute(
        "aria-controls",
        "workflow-variable-suggestions-root-switch-selector",
      );
    });
    // The saved selector filters suggestions to its own token. Clearing the
    // search exposes other available workflow paths without changing the wire.
    fireEvent.change(switchSelectorInput, { target: { value: "" } });
    expect(
      await screen.findByTestId("workflow-variable-suggestions-root-switch-selector"),
    ).toHaveTextContent("state.project_config.stack.language");
    fireEvent.change(switchSelectorInput, { target: { value: "state.inputs.city" } });
    expect(await screen.findByDisplayValue("Berlin")).toBeInTheDocument();
    expect(screen.getByText("case_id: berlin")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Next step for Berlin" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Next step for Default" })).toBeInTheDocument();

    // Renaming a case's match value keeps its port, so it saves directly.
    fireEvent.change(screen.getByRole("textbox", { name: "Case value 1" }), {
      target: { value: "Vilnius" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    await waitFor(() => expect(savedStepBodies().length).toBeGreaterThan(0));
    expect(stepBodyFromCall(savedStepCall()).override).toEqual({
      switch: {
        selector: "state.inputs.city",
        cases: [{ case_id: "berlin", when: "Vilnius" }],
      },
    });
  }, SMART_SETUP_JOURNEY_TIMEOUT);


  it("keeps the If selector's variable suggestions, and its focus, while typing", async () => {
    // PR #61 replaced the If selector's variable combobox with a plain input keyed by its
    // own value: the suggestions went, and every keystroke remounted the input. The
    // canvas must still not see the typing (#61's reason for stopPropagation).
    mockIfAndSwitchWorkflow();
    const canvasKeys = vi.fn();
    document.addEventListener("keydown", canvasKeys);
    const user = userEvent.setup();
    try {
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: /Check city input/i }));
      const selector = await screen.findByRole("combobox", { name: "Selector" });

      await user.click(selector);
      expect(selector).toHaveAttribute(
        "aria-controls",
        "workflow-variable-suggestions-root-if-selector",
      );
      expect(
        await screen.findByTestId("workflow-variable-suggestions-root-if-selector"),
      ).toHaveTextContent("Previous steps");

      await user.type(selector, ".x");

      expect(selector).toHaveFocus();
      expect(selector).toHaveValue("state.inputs.city.x");
      expect(canvasKeys).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener("keydown", canvasKeys);
    }
  });
  it("uses extension form schema select options for literal connector inputs", async () => {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(
          JSON.stringify({
            connectors: [
              {
                connector_id: "connector.acme_docs",
                extension_install_id: "pi_acme_docs",
                display_name: "Acme Docs",
                operations: [
                  {
                    operation_id: "convert_document",
                    parameters: [{ name: "table_mode", type: "string" }],
                    returns: [
                      {
                        name: "markdown_artifact_handle",
                        type: "value",
                      },
                      {
                        name: "acme_docs_json_artifact_handle",
                        type: "value",
                      },
                    ],
                  },
                ],
                connections: [],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/meta/extension-form-schema")) {
        return new Response(
          JSON.stringify({
            sections: [
              {
                fields: [
                  {
                    name: "table_mode",
                    widget_type: "select",
                    options: [
                      { label: "Fast", value: "fast" },
                      { label: "Accurate", value: "accurate" },
                    ],
                  },
                ],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/meta/extension-install-contract")) {
        return new Response(
          JSON.stringify({
            install_id: "pi_acme_docs",
            extension_id: "acme.doc-parser",
            operations: [
              {
                operation_id: "convert_document",
                parameters: [{ name: "table_mode", type: "string" }],
                returns: [
                  { name: "markdown_artifact_handle", type: "value" },
                  { name: "acme_docs_json_artifact_handle", type: "value" },
                ],
              },
            ],
            connections: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/meta/extension-form-schema")) {
        return new Response(JSON.stringify({ sections: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "acme_docs_workflow",
      display_name: "Acme Docs workflow",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111099",
              step_id: "convert_document",
              display_name: "Convert document",
              step_kind: "connector",
              connector_id: "connector.acme_docs",
              override: {
                connector: {
                  connector_id: "connector.acme_docs",
                  operation_id: "convert_document",
                },
              },
              input_mapping_jsonb: { table_mode: "literal:fast" },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Convert document/i }));

    // Catalog resolution enables the schema request, which replaces the literal
    // textbox with this select. Allow both requests to settle under parallel load.
    const valueSelect = await screen.findByRole("combobox", {
      name: "Parameter value 1",
    }, { timeout: 5_000 });
    expect(valueSelect).toHaveValue("fast");
    expect(within(valueSelect).getByRole("option", { name: "Accurate" })).toHaveValue(
      "accurate",
    );
    const addOutput = screen.getByRole("button", { name: "Add output mapping" });
    expect(addOutput).toBeEnabled();
    fireEvent.click(addOutput);
    expect(screen.getByRole("combobox", { name: "Returned field 1" })).toHaveValue(
      "markdown_artifact_handle",
    );
    expect(
      screen.getByRole("combobox", { name: "Returned field 1" }),
    ).toHaveTextContent("acme_docs_json_artifact_handle");
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/workflows/meta/extension-form-schema?install_id=pi_acme_docs&operation_id=convert_document&locale=en",
      expect.objectContaining({ method: "GET" }),
    );
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("binds connector operations and connections to the persisted extension install", async () => {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(
          JSON.stringify({
            connectors: [
              {
                connector_id: "extension.acme.doc-parser",
                connector_install_id: "install_current",
                extension_install_id: "install_current",
                display_name: "Acme Docs",
                operations: [
                  {
                    operation_id: "convert_current",
                    display_name: "Convert with current release",
                    returns: [{ name: "current_artifact", type: "value" }],
                  },
                ],
                connections: [
                  {
                    connection_id: "connection_current",
                    display_name: "Current connection",
                  },
                ],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/meta/extension-install-contract")) {
        return new Response(
          JSON.stringify({
            install_id: "install_old",
            extension_id: "acme.doc-parser",
            operations: [
              {
                operation_id: "convert_legacy",
                display_name: "Convert with legacy release",
                returns: [{ name: "legacy_artifact", type: "value" }],
              },
            ],
            connections: [
              {
                connection_id: "connection_old",
                display_name: "Legacy connection",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/meta/extension-form-schema")) {
        return new Response(JSON.stringify({ sections: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "acme_docs_legacy_workflow",
      display_name: "Legacy Acme Docs workflow",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111098",
              step_id: "convert_document",
              display_name: "Convert legacy document",
              step_kind: "connector",
              connector_id: "extension.acme.doc-parser",
              override: {
                connector: {
                  connector_id: "extension.acme.doc-parser",
                  connector_install_id: "install_old",
                  operation_id: "convert_legacy",
                  connection_binding: { connection_id: "connection_old" },
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: /Convert legacy document/i }),
    );

    const actionSelect = await screen.findByRole("combobox", { name: "Action" });
    await waitFor(() => expect(actionSelect).toHaveTextContent("Convert with legacy release"));
    expect(
      await setupOptions(actionSelect).findByRole("option", {
        name: "Convert with legacy release",
      }),
    ).toBeInTheDocument();
    expect(
      setupOptions(actionSelect).queryByRole("option", {
        name: "Convert with current release",
      }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText("legacy_artifact").length).toBeGreaterThan(0);

    const connectionSelect = screen.getByRole("combobox", { name: "Connection" });
    await waitFor(() => expect(connectionSelect).toHaveTextContent("Legacy connection"));
    expect(
      setupOptions(connectionSelect).getByRole("option", { name: "Legacy connection" }),
    ).toBeInTheDocument();
    expect(
      setupOptions(connectionSelect).queryByRole("option", { name: "Current connection" }),
    ).not.toBeInTheDocument();
  });

  it("labels required extension connection choices with their connected accounts", async () => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (url.endsWith("/changes") && init?.method === "POST") {
        // The server stores the patch; the next read returns the saved step.
        const [operation] = JSON.parse(String(init.body)).operations as Array<{ patch: Record<string, unknown> }>;
        const current = (await mockedFetchWorkflowDetail.getMockImplementation()?.("mail_multi_account_workflow")) as unknown as {
          steps: Array<Record<string, unknown>>;
        };
        mockedFetchWorkflowDetail.mockResolvedValue({
          ...current,
          revision: 2,
          steps: [{ ...current.steps[0], ...operation.patch }],
        } as never);
        return new Response(JSON.stringify({ workflow_id: "mail_multi_account_workflow", revision: 2, valid: true, issues: [], workflow: {} }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.includes("/meta/connector-catalog")) {
        return new Response(JSON.stringify({ connectors: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.includes("/meta/extension-install-contract")) {
        return new Response(
          JSON.stringify({
            install_id: "install-mail",
            extension_id: "partner.mail",
            install_state: "active",
            operations: [{
              operation_id: "search_messages",
              display_name: "Search mailbox",
              connection_type_ids: ["mail_account"],
              connection_label: "mail account",
              required_oauth_scopes: ["mail.read"],
              connection_required: true,
            }],
            connections: [
              {
                connection_id: "fixture-invoices",
                connection_type: "mail_account",
                display_name: "Mail account",
                connected_email: "invoices@example.test",
                oauth_lifecycle_status: "connected",
                oauth_granted_scopes: ["mail.read"],
                runtime_status: "ready",
              },
              {
                connection_id: "fixture-records",
                connection_type: "mail_account",
                display_name: "Mail account",
                connected_email: "stock@example.test",
                oauth_lifecycle_status: "connected",
                oauth_granted_scopes: ["mail.read"],
                runtime_status: "degraded",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ sections: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "mail_multi_account_workflow",
      display_name: "Mail multi-account workflow",
      enabled: true,
      steps: [{
          id: "11111111-1111-1111-1111-111111111095",
          step_id: "search_invoices",
          display_name: "Search invoice mailbox",
          step_kind: "connector",
          connector_id: "extension.partner.mail",
          override: {
            connector: {
              connector_id: "extension.partner.mail",
              connector_install_id: "install-mail",
              operation_id: "search_messages",
              connection_binding: { connection_id: "fixture-removed" },
            },
          },
        }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: /Search invoice mailbox/i }),
    );

    const connectionSelect = await screen.findByRole("combobox", { name: "Connection" });
    expect(
      await setupOptions(connectionSelect).findByRole("option", {
        name: "Mail account (invoices@example.test)",
      }),
    ).toBeEnabled();
    expect(
      await screen.findByText("The saved mail account was removed. Choose another one for this action."),
    ).toBeInTheDocument();
    expect(setupOptions(connectionSelect).getByRole("option", { name: "Choose connection" })).toBeEnabled();
    expect(
      setupOptions(connectionSelect).getByRole("option", {
        name: "Mail account (stock@example.test)",
      }),
    ).toBeEnabled();
    expect(setupOptions(connectionSelect).queryByText("Auto-select connection")).not.toBeInTheDocument();

    chooseSetupOption(connectionSelect, "Choose connection");
    // A required connection not chosen yet is said at the field, not in an alert.
    expect(connectionSelect).toHaveAttribute("aria-invalid", "true");
    expect(connectionSelect).toHaveAccessibleDescription(/^Choose a .+ connection for this action\.$/);
    chooseSetupOption(connectionSelect, "Mail account (stock@example.test)");
    expect(connectionSelect).not.toHaveAttribute("aria-invalid");
    const saveButton = screen.getByRole("button", { name: "Save parameters" });
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);

    await waitFor(() => {
      const saveCall = fetchSpy.mock.calls.find(
        ([url, options]) =>
          stringFromUnknownValue(url).endsWith("/changes")
          && objectOrEmpty(options).method === "POST",
      );
      expect(saveCall).toBeDefined();
      const body = stepBodyFromCall(saveCall);
      expect(body.connector_id).toBe("extension.partner.mail");
      expect(body.connector_install_id).toBe("install-mail");
      expect(body.override.connector).not.toHaveProperty("connector_id");
      expect(body.override.connector).not.toHaveProperty("connector_install_id");
      expect(body.override.connector.connection_binding.connection_id).toBe("fixture-records");
    });

    fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
    expect(
      await screen.findByRole("button", { name: "Run step test run" }),
    ).toBeEnabled();
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it.each([
    {
      operationId: "search_messages",
      operationLabel: "Search mailbox messages",
      parameterName: "query",
      service: "mail",
      widgetType: "text",
    },
    {
      operationId: "sync_storage_object",
      operationLabel: "Sync storage object",
      parameterName: "object_id",
      service: "storage",
      widgetType: "text",
    },
  ])(
    "marks and blocks an absent required $service input before Save or Test",
    async ({ operationId, operationLabel, parameterName, service, widgetType }) => {
      mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
      fetchSpy.mockImplementation(async (input) => {
        const url = stringFromUnknownValue(input);
        if (url.includes("/meta/connector-catalog")) {
          return new Response(JSON.stringify({ connectors: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.includes("/meta/extension-install-contract")) {
          return new Response(JSON.stringify({
            install_id: "install-resource",
            extension_id: "partner.resource",
            install_state: "active",
            operations: [{
              operation_id: operationId,
              display_name: operationLabel,
              connection_type_ids: ["resource_account"],
              connection_label: "resource account",
              connection_required: true,
            }],
            connections: [{
              connection_id: "resource-primary",
              connection_type: "resource_account",
              display_name: "Stock account",
              connected_email: "stock@example.test",
              oauth_lifecycle_status: "connected",
              runtime_status: "ready",
              oauth_granted_scopes: ["resource.read"],
            }],
          }), { status: 200, headers: { "content-type": "application/json" } });
        }
        if (url.includes("/meta/extension-form-schema")) {
          return new Response(JSON.stringify({
            sections: [{
              fields: [{
                name: parameterName,
                label: `${parameterName} (required)`,
                required: true,
                widget_type: widgetType,
                resource_query_id: widgetType === "resource_picker"
                  ? "resource_files"
                  : undefined,
              }],
            }],
          }), { status: 200, headers: { "content-type": "application/json" } });
        }
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      });
      mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
        workflow_id: `required_${service}_workflow`,
        display_name: `Required ${service} workflow`,
        enabled: true,
        steps: [{
            id: "11111111-1111-1111-1111-111111111091",
            step_id: `required_${service}`,
            display_name: operationLabel,
            step_kind: "connector",
            connector_id: "extension.partner.resource",
            connector_install_id: "install-resource",
            input_mapping_jsonb: {},
            override: {
              connector: {
                extension_id: "partner.resource",
                operation_id: operationId,
                connection_binding: { connection_id: "resource-primary" },
              },
            },
          }], groups: [],
      } as never);
      mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
      mockedFetchWorkflowValidation.mockResolvedValue({
        validation: { valid: true, errors: [], warnings: [] },
      } as never);

      renderPage();
      fireEvent.click(await screen.findByRole("button", {
        name: new RegExp(operationLabel, "i"),
      }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        `Required action inputs are missing: ${parameterName}.`,
      );
      const requiredInput = screen.getByRole("textbox", {
        name: "Parameter value 1",
      });
      expect(requiredInput).toBeRequired();
      expect(requiredInput).toHaveAttribute("aria-invalid", "true");
      editStepLabel(); // With a change waiting, only readiness decides whether Save is live.
      expect(screen.getByRole("button", { name: "Save parameters" })).toBeDisabled();

      fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
      expect(await screen.findByRole("button", {
        name: "Run step test run",
      })).toBeDisabled();
    },
  );

  it.each([
    {
      lifecycleStatus: "pending",
      runtimeStatus: "unavailable",
      guidance:
        "Reconnect this account from the extension Setup page.",
    },
    {
      lifecycleStatus: "disconnected",
      runtimeStatus: "disconnected",
      guidance:
        "Reconnect this account from the extension Setup page.",
    },
    {
      lifecycleStatus: "reconnect_required",
      runtimeStatus: "unavailable",
      guidance:
        "Reconnect this account from the extension Setup page.",
    },
    {
      lifecycleStatus: "connected",
      runtimeStatus: "unavailable",
      guidance:
        "Resolve this connection in the extension Setup page before retrying.",
    },
  ])(
    "preserves a saved generic OAuth binding but blocks Save and Test Run while it is $lifecycleStatus/$runtimeStatus",
    async ({ lifecycleStatus, runtimeStatus, guidance }) => {
      mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
      fetchSpy.mockImplementation(async (input) => {
        const url = stringFromUnknownValue(input);
        if (url.includes("/meta/connector-catalog")) {
          return new Response(JSON.stringify({ connectors: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.includes("/meta/extension-install-contract")) {
          return new Response(
            JSON.stringify({
              install_id: "install-mail",
              extension_id: "partner.mail",
              install_state: "active",
              operations: [{
                operation_id: "search_messages",
                display_name: "Search mailbox",
                connection_type_ids: ["mail_account"],
                connection_label: "mail account",
                required_oauth_scopes: ["mail.read"],
                connection_required: true,
              }],
              connections: [{
                connection_id: "fixture-invoices",
                connection_type: "mail_account",
                display_name: "Mail account",
                connected_email: "invoices@example.test",
                oauth_lifecycle_status: lifecycleStatus,
                oauth_granted_scopes: ["mail.read"],
                runtime_status: runtimeStatus,
              }],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      });
      mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
        workflow_id: "mail_lifecycle_workflow",
        display_name: "Mail lifecycle workflow",
        enabled: true,
        steps: [{
            id: "11111111-1111-1111-1111-111111111094",
            step_id: "search_invoices",
            display_name: "Search saved mailbox",
            step_kind: "connector",
            connector_id: "extension.partner.mail",
            override: {
              connector: {
                connector_id: "extension.partner.mail",
                connector_install_id: "install-mail",
                operation_id: "search_messages",
                connection_binding: { connection_id: "fixture-invoices" },
              },
            },
          }], groups: [],
      } as never);
      mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
      mockedFetchWorkflowValidation.mockResolvedValue({
        validation: { valid: true, errors: [], warnings: [] },
      } as never);

      renderPage();
      fireEvent.click(
        await screen.findByRole("button", { name: /Search saved mailbox/i }),
      );

      const connectionSelect = await screen.findByRole("combobox", {
        name: "Connection",
      });
      await waitFor(() => expect(connectionSelect).toHaveTextContent("Mail account (invoices@example.test)"));
      expect(
        await setupOptions(connectionSelect).findByRole("option", {
          name: /Mail account \(invoices@example\.test\) —/,
        }),
      ).toBeInTheDocument();
      expect(await screen.findByText(guidance)).toBeInTheDocument();
      editStepLabel(); // With a change waiting, only readiness decides whether Save is live.
      expect(screen.getByRole("button", { name: "Save parameters" })).toBeDisabled();

      fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
      expect(
        await screen.findByRole("button", { name: "Run step test run" }),
      ).toBeDisabled();
    },
  );

  it.each([
    {
      operationId: "download_storage_object",
      operationLabel: "Download storage object",
      requiredScope: "storage.read",
    },
    {
      operationId: "read_calendar_event",
      operationLabel: "Read calendar event",
      requiredScope: "calendar.read",
    },
  ])(
    "keeps a $operationLabel workflow binding when its exact OAuth scope is granted",
    async ({ operationId, operationLabel, requiredScope }) => {
      mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
      fetchSpy.mockImplementation(async (input) => {
        const url = stringFromUnknownValue(input);
        if (url.includes("/meta/connector-catalog")) {
          return new Response(JSON.stringify({ connectors: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.includes("/meta/extension-install-contract")) {
          return new Response(
            JSON.stringify({
              install_id: "install-resource",
              extension_id: "partner.resource",
              install_state: "active",
              operations: [{
                operation_id: operationId,
                display_name: operationLabel,
                connection_type_ids: ["resource_account"],
                connection_label: "resource account",
                required_oauth_scopes: [requiredScope],
                connection_required: true,
              }],
              connections: [{
                connection_id: "resource-primary",
                connection_type: "resource_account",
                display_name: "Stock account",
                connected_email: "stock@example.test",
                oauth_lifecycle_status: "connected",
                oauth_granted_scopes: [requiredScope],
                runtime_status: "ready",
              }],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      });
      mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
        workflow_id: "resource_scope_workflow",
        display_name: "Resource scope workflow",
        enabled: true,
        steps: [{
            id: "11111111-1111-1111-1111-111111111093",
            step_id: "resource_scope_step",
            display_name: operationLabel,
            step_kind: "connector",
            connector_id: "extension.partner.resource",
            connector_install_id: "install-resource",
            override: {
              connector: {
                extension_id: "partner.resource",
                operation_id: operationId,
                connection_binding: { connection_id: "resource-primary" },
              },
            },
          }], groups: [],
      } as never);
      mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
      mockedFetchWorkflowValidation.mockResolvedValue({
        validation: { valid: true, errors: [], warnings: [] },
      } as never);

      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: new RegExp(operationLabel, "i") }));

      const connectionSelect = await screen.findByRole("combobox", {
        name: "Connection",
      });
      await waitFor(() => expect(connectionSelect).toHaveTextContent("Stock account (stock@example.test)"));
      expect(
        await setupOptions(connectionSelect).findByRole("option", {
          name: "Stock account (stock@example.test)",
        }),
      ).toBeEnabled();
      expect(screen.queryByText(/Reconnect this account and approve/i)).not.toBeInTheDocument();
      // Runnable as saved: an unsaved edit would hold the step test run back on its own.
      fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
      expect(await screen.findByRole("button", { name: "Run step test run" })).toBeEnabled();
      fireEvent.click(screen.getByRole("tab", { name: "Smart setup" }));
      editStepLabel(); // With a change waiting, only readiness decides whether Save is live.
      expect(screen.getByRole("button", { name: "Save parameters" })).toBeEnabled();
    },
  );

  it("offers the only compatible connection when the saved one was removed", async () => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(JSON.stringify({ connectors: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.includes("/meta/extension-install-contract")) {
        return new Response(
          JSON.stringify({
            install_id: "install-mail",
            extension_id: "partner.mail",
            install_state: "active",
            operations: [{
              operation_id: "search_messages",
              display_name: "Search mailbox",
              connection_type_ids: ["mail_account"],
              connection_label: "mail account",
              required_oauth_scopes: ["mail.read"],
              connection_required: true,
            }],
            connections: [
              {
                connection_id: "fixture-invoices",
                connection_type: "mail_account",
                display_name: "Mail account",
                connected_email: "invoices@example.test",
                oauth_lifecycle_status: "connected",
                oauth_granted_scopes: ["mail.read"],
                runtime_status: "ready",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ sections: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "mail_multi_account_workflow",
      display_name: "Mail multi-account workflow",
      enabled: true,
      steps: [{
          id: "11111111-1111-1111-1111-111111111095",
          step_id: "search_invoices",
          display_name: "Search invoice mailbox",
          step_kind: "connector",
          connector_id: "extension.partner.mail",
          override: {
            connector: {
              connector_id: "extension.partner.mail",
              connector_install_id: "install-mail",
              operation_id: "search_messages",
              connection_binding: { connection_id: "fixture-removed" },
            },
          },
        }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: /Search invoice mailbox/i }),
    );

    const connectionSelect = await screen.findByRole("combobox", { name: "Connection" });
    expect(
      await screen.findByText("The saved mail account was removed. Choose another one for this action."),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Use Mail account (invoices@example.test)" }),
    );

    await waitFor(() => expect(connectionSelect).toHaveTextContent("Mail account (invoices@example.test)"));
    expect(
      screen.queryByText("The saved mail account was removed. Choose another one for this action."),
    ).not.toBeInTheDocument();
  });

  it("allows a connected generic OAuth account with a non-blocking runtime status to remain selectable and runnable", async () => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(JSON.stringify({ connectors: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.includes("/meta/extension-install-contract")) {
        return new Response(JSON.stringify({
          install_id: "install-storage",
          extension_id: "partner.storage",
          install_state: "active",
          operations: [{
            operation_id: "search_storage_files",
            display_name: "Search storage files",
            connection_type_ids: ["storage_account"],
            connection_label: "storage account",
            required_oauth_scopes: ["storage.read"],
            connection_required: true,
          }],
          connections: [{
            connection_id: "storage-primary",
            connection_type: "storage_account",
            display_name: "Stock account",
            connected_email: "stock@example.test",
            oauth_lifecycle_status: "connected",
            oauth_granted_scopes: ["storage.read"],
            runtime_status: "degraded",
          }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "storage_runtime_workflow",
      display_name: "Storage runtime workflow",
      enabled: true,
      steps: [{
          id: "11111111-1111-1111-1111-111111111092",
          step_id: "search_storage",
          display_name: "Search storage files",
          step_kind: "connector",
          connector_id: "extension.partner.storage",
          connector_install_id: "install-storage",
          override: {
            connector: {
              extension_id: "partner.storage",
              operation_id: "search_storage_files",
              connection_binding: { connection_id: "storage-primary" },
            },
          },
        }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Search storage files/i }));

    const option = await setupOptions(
      screen.getByRole("combobox", { name: "Connection" }),
    ).findByRole("option", { name: "Stock account (stock@example.test)" });
    expect(option).toBeEnabled();
    // Runnable as saved: an unsaved edit would hold the step test run back on its own.
    fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
    expect(await screen.findByRole("button", { name: "Run step test run" })).toBeEnabled();
    fireEvent.click(screen.getByRole("tab", { name: "Smart setup" }));
    editStepLabel(); // With a change waiting, only readiness decides whether Save is live.
    expect(screen.getByRole("button", { name: "Save parameters" })).toBeEnabled();
  });

  it("applies a generic external resource picker selection through the extension bridge", async () => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    const pickerOpen = vi.fn().mockResolvedValue([{ resource_id: "resource-42" }]);
    (globalThis as typeof globalThis & { GenericResourcePicker?: { open: typeof pickerOpen } }).GenericResourcePicker = {
      open: pickerOpen,
    };
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(JSON.stringify({ connectors: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.includes("/meta/extension-install-contract")) {
        return new Response(JSON.stringify({
          install_id: "install-resource",
          extension_id: "partner.resource",
          release_id: "release-resource",
          descriptor_sha256: "a".repeat(64),
          install_state: "active",
          operations: [{
            operation_id: "download_resource",
            display_name: "Download resource",
            connection_type_ids: ["resource_account"],
            connection_label: "resource account",
            required_oauth_scopes: ["resource.read"],
            connection_required: true,
          }],
          connections: [{
            connection_id: "resource-primary",
            connection_type: "resource_account",
            display_name: "Stock account",
            connected_email: "stock@example.test",
            oauth_lifecycle_status: "connected",
            oauth_granted_scopes: ["resource.read"],
            runtime_status: "ready",
          }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/meta/extension-form-schema")) {
        return new Response(JSON.stringify({
          sections: [{
            fields: [{
              name: "file_id",
              label: "Resource (required)",
              widget_type: "resource_picker",
              required: true,
              external_resource_picker: {
                allowed_script_origins: ["https://picker.example.test"],
                allowed_connect_origins: ["https://api.example.test"],
                allowed_frame_origins: ["https://frame.example.test"],
                bootstrap_action_id: "picker_bootstrap",
                bridge_global: "GenericResourcePicker",
                button_label: "Choose resource",
                error_message: "Resource selection could not be applied.",
                scripts: [
                  { src: "https://picker.example.test/sdk.js" },
                  { asset_ref: "assets/picker-bridge.js" },
                ],
                selection: {
                  maximum: 1,
                  fields: [{ source: "resource_id", target: "file_id", required: true, max_length: 200 }],
                },
                target_component_id: "resource_picker",
              },
            }],
          }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/extensions/partner.resource/action/picker_bootstrap")) {
        return new Response(JSON.stringify({
          action: { status: "succeeded", response: { ok: true, result: { audience: "project_alpha" } } },
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "resource_picker_workflow",
      display_name: "Resource picker workflow",
      enabled: true,
      steps: [{
          id: "11111111-1111-1111-1111-111111111093",
          step_id: "download_stock",
          display_name: "Download resource",
          step_kind: "connector",
          connector_id: "extension.partner.resource",
          connector_install_id: "install-resource",
          input_mapping_jsonb: { file_id: "state.upstream_resource_id" },
          override: {
            connector: {
              extension_id: "partner.resource",
              operation_id: "download_resource",
              connection_binding: { connection_id: "resource-primary" },
            },
          },
        }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Download resource/i }));
    const pickerButton = await screen.findByRole("button", {
      name: "Choose resource",
    });
    const sourceSelect = screen.getByRole("combobox", { name: "Parameter source 1" });
    const valueInput = screen.getByRole("textbox", { name: "Parameter value 1" });
    expect(sourceSelect).toHaveValue("state");
    expect(valueInput).toHaveValue("state.upstream_resource_id");

    fireEvent.click(pickerButton);
    const scripts = await waitFor(() => {
      // eslint-disable-next-line testing-library/no-node-access -- the browser bridge owns the external script element.
      const elements = Array.from(document.head.querySelectorAll<HTMLScriptElement>("script"))
        .filter((element) => element.src.includes("picker.example.test/sdk.js")
          || element.src.includes("assets/picker-bridge.js"));
      expect(elements).toHaveLength(2);
      return elements;
    });
    scripts.forEach((script) => script.dispatchEvent(new Event("load")));
    await waitFor(() => expect(sourceSelect).toHaveValue("literal"));
    expect(screen.getByRole("textbox", { name: "Parameter value 1" })).toHaveValue(
      "resource-42",
    );
    expect(pickerOpen).toHaveBeenCalledWith(
      expect.objectContaining({
        bootstrap: { audience: "project_alpha" },
      }),
    );
    await waitFor(() => expect(pickerButton).toHaveFocus());
  });

  it.each([
    {
      installState: "removed",
      responseStatus: 404,
      expectedMessage:
        "This extension was removed—reinstall it or replace this saved step before it can run.",
    },
    {
      installState: "disabled",
      responseStatus: 200,
      expectedMessage:
        "This extension is disabled—ask an admin to enable it before this saved step can run.",
    },
    {
      installState: "unavailable",
      responseStatus: 500,
      expectedMessage:
        "Could not verify this extension right now; retry or contact an admin.",
    },
    {
      installState: "forbidden",
      responseStatus: 403,
      expectedMessage:
        "Could not verify this extension right now; retry or contact an admin.",
    },
  ])("preserves but blocks a saved step when its extension is $installState", async ({
    installState,
    responseStatus,
    expectedMessage,
  }) => {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(
          JSON.stringify({
            connectors: [
              {
                connector_id: "extension.acme.doc-parser",
                connector_install_id: "install_current",
                display_name: "Acme Docs",
                operations: [{ operation_id: "convert_current" }],
                connections: [{ connection_id: "connection_current" }],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/meta/extension-install-contract")) {
        if (installState === "disabled") {
          return new Response(
            JSON.stringify({
              install_id: "install_old",
              extension_id: "acme.doc-parser",
              install_state: "disabled",
              operations: [{ operation_id: "convert_removed" }],
              connections: [{ connection_id: "connection_removed" }],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({ detail: "The installed extension release is unavailable" }),
          { status: responseStatus, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "stale_acme_docs_workflow",
      display_name: "Stale Acme Docs workflow",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111099",
              step_id: "convert_document",
              display_name: "Convert stale document",
              step_kind: "connector",
              connector_id: "extension.acme.doc-parser",
              override: {
                connector: {
                  connector_id: "extension.acme.doc-parser",
                  connector_install_id: "install_old",
                  operation_id: "convert_removed",
                  connection_binding: { connection_id: "connection_removed" },
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: {
        valid: false,
        errors: [{
          code: installState === "disabled"
            ? "workflow.extension_disabled"
            : "workflow.extension_removed",
          message: expectedMessage,
          path: "workflow.steps.convert_document.connector_install_id",
        }],
        warnings: [],
      },
    } as never);

    renderPage({ embedded: false });
    expect(
      await screen.findByText("Validation: 1 errors / 0 warnings"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Validation: valid")).not.toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: /Convert stale document/i }),
    );

    const actionSelect = await screen.findByRole("combobox", { name: "Action" });
    const connectionSelect = screen.getByRole("combobox", { name: "Connection" });
    expect((await screen.findAllByText(expectedMessage)).length).toBeGreaterThan(0);
    if (installState !== "disabled") {
      expect(
        setupOptions(actionSelect).queryByRole("option", { name: "convert_removed" }),
      ).not.toBeInTheDocument();
      expect(
        setupOptions(connectionSelect).queryByRole("option", { name: "connection_removed" }),
      ).not.toBeInTheDocument();
    } else {
      expect(setupOptions(actionSelect).getByRole("option", { name: "Unavailable action" })).toHaveAttribute("aria-selected", "true");
      expect(actionSelect).not.toHaveTextContent("convert_removed");
      expect(setupOptions(connectionSelect).getByRole("option", { name: "connection_removed" })).toBeInTheDocument();
    }
    expect(
      screen.queryByText("The saved connection was removed. Choose another one for this action."),
    ).not.toBeInTheDocument();

    // A live Save that is still refused: the block, not a clean form, keeps it from writing.
    editStepLabel();
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() =>
      expect(
        fetchSpy.mock.calls.some(
          ([url, options]) =>
            stringFromUnknownValue(url).endsWith("/changes") &&
            objectOrEmpty(options).method === "POST",
        ),
      ).toBe(false),
    );

    fireEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
    fireEvent.click(await screen.findByRole("tab", { name: "Advanced" }));
    const override = JSON.parse((screen.getByLabelText("Override JSON") as HTMLTextAreaElement).value);
    expect(override.connector.connector_id).toBe("extension.acme.doc-parser");
    expect(override.connector.connector_install_id).toBe("install_old");
    expect(override.connector.operation_id).toBe("convert_removed");
    expect(override.connector.connection_binding.connection_id).toBe("connection_removed");
  });

  it("does not show install or connection errors while the saved install contract is loading", async () => {
    let resolveContract: ((response: Response) => void) | undefined;
    const pendingContract = new Promise<Response>((resolve) => {
      resolveContract = resolve;
    });
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(
          JSON.stringify({
            connectors: [{
              connector_id: "extension.acme.doc-parser",
              connector_install_id: "install_current",
              operations: [{ operation_id: "convert_current" }],
              connections: [],
            }],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/meta/extension-install-contract")) {
        return pendingContract;
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "loading_extension_workflow",
      display_name: "Loading extension workflow",
      enabled: true,
      steps: [{
          id: "11111111-1111-1111-1111-111111111096",
          step_id: "convert_document",
          display_name: "Convert loading document",
          step_kind: "connector",
          connector_id: "extension.acme.doc-parser",
          override: {
            connector: {
              connector_id: "extension.acme.doc-parser",
              connector_install_id: "install_old",
              operation_id: "convert_legacy",
              connection_binding: { connection_id: "connection_old" },
            },
          },
        }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: /Convert loading document/i }),
    );
    await screen.findByRole("combobox", { name: "Action" });

    expect(screen.queryByText(/Could not verify this extension/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Saved connection is no longer available/)).not.toBeInTheDocument();

    await act(async () => {
      resolveContract?.(
        new Response(
          JSON.stringify({
            install_id: "install_old",
            extension_id: "acme.doc-parser",
            install_state: "active",
            operations: [{ operation_id: "convert_legacy" }],
            connections: [{ connection_id: "connection_old" }],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
      await pendingContract;
    });

    await waitFor(() => {
      expect(screen.queryByText(/Could not verify this extension/)).not.toBeInTheDocument();
      expect(screen.queryByText(/Saved connection is no longer available/)).not.toBeInTheDocument();
    });
  });

  it("saves an extension action whose install contract has no connections", async () => {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(
          JSON.stringify({
            connectors: [
              {
                connector_id: "extension.acme.showcase",
                connector_install_id: "install_showcase",
                display_name: "Acme Showcase",
                operations: [
                  {
                    operation_id: "demo_compose_content_brief",
                    connection_required: false,
                  },
                ],
                connections: [{ connection_id: "unrelated_connection" }],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/meta/extension-install-contract")) {
        return new Response(
          JSON.stringify({
            extension_id: "acme.showcase",
            install_id: "install_showcase",
            operations: [
              {
                operation_id: "demo_compose_content_brief",
                connection_required: false,
              },
            ],
            connections: [
              {
                connection_id: "unrelated_connection",
                connection_type: "other_service",
                runtime_status: "unavailable",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "connectionless_extension_workflow",
      display_name: "Connectionless extension workflow",
      enabled: true,
      steps: [{
              id: "11111111-1111-1111-1111-111111111098",
              step_id: "compose_brief",
              display_name: "Compose extension brief",
              step_kind: "connector",
              connector_id: "extension.acme.showcase",
              override: {
                connector: {
                  connector_id: "extension.acme.showcase",
                  connector_install_id: "install_showcase",
                  operation_id: "demo_compose_content_brief",
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: /Compose extension brief/i }),
    );
    expect(
      screen.queryByRole("combobox", { name: "Connection" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Action" })).toHaveTextContent("Unavailable action"));
    await waitFor(() => {
      expect(
        screen.queryByText(
          "This extension was removed—reinstall it or replace this saved step before it can run.",
        ),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByText(
          "The saved connection was removed. Choose another one for this action.",
        ),
      ).not.toBeInTheDocument();
    });

    editStepLabel();
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    await waitFor(() => {
      expect(savedStepBodies().length).toBeGreaterThan(0);
      expect(stepBodyFromCall(savedStepCall())).toMatchObject({
        connector_id: "extension.acme.showcase",
        connector_install_id: "install_showcase",
        override: { connector: { operation_id: "demo_compose_content_brief" } },
      });
    });
  });

  function mockConnectionRequiredStep(
    projectId?: string,
    connection?: Record<string, unknown>,
    bindConnection = true,
    requiredScopes: string[] = [],
  ) {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/connector-catalog")) {
        return new Response(
          JSON.stringify({
            connectors: [
              {
                connector_id: "extension.partner.resource",
                connector_install_id: "install_required",
                display_name: "Required provider",
                operations: [{ operation_id: "publish", connection_required: true }],
                connections: [],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/meta/extension-install-contract")) {
        return new Response(
          JSON.stringify({
            extension_id: "partner.resource",
            install_id: "install_required",
            install_state: "active",
            operations: [
              {
                operation_id: "publish",
                connection_type_ids: ["resource_account"],
                connection_label: "resource account",
                connection_required: true,
                required_oauth_scopes: requiredScopes,
              },
            ],
            connections: connection ? [connection] : [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValue({ revision: 1,
      workflow_id: "required_connection_workflow",
      display_name: "Required connection workflow",
      enabled: true,
      ...(projectId ? { project_id: projectId } : {}),
      steps: [{
              id: "11111111-1111-1111-1111-111111111097",
              step_id: "publish_record",
              display_name: "Publish required record",
              step_kind: "connector",
              connector_id: "extension.partner.resource",
              ...(connection && bindConnection ? {
                connector_install_id: "install_required",
                connection_binding_jsonb: { connection_id: connection.connection_id },
              } : {}),
              override: {
                connector: {
                  connector_id: "extension.partner.resource",
                  connector_install_id: "install_required",
                  operation_id: "publish",
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
  }

  async function expectStepIsNotSaved() {
    editStepLabel();
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() =>
      expect(
        fetchSpy.mock.calls.some(
          ([url, options]) =>
            stringFromUnknownValue(url).endsWith("/changes") &&
            objectOrEmpty(options).method === "POST",
        ),
      ).toBe(false),
    );
  }

  it("tells an unlinked workflow to link a project rather than blaming a lost connection", async () => {
    // Connections are project-scoped, so an unlinked workflow can never list one.
    // Reporting that as a saved connection having disappeared sent users looking
    // for something that had never existed.
    mockConnectionRequiredStep();

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: /Publish required record/i }),
    );

    expect(
      await screen.findByText(
        "Link this workflow to a project to choose a resource account for this action.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no longer available/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Connection" })).not.toBeInTheDocument();
    await expectStepIsNotSaved();
  });

  it("blocks a connection-required extension action with no compatible connection", async () => {
    mockConnectionRequiredStep("project_alpha");

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: /Publish required record/i }),
    );

    expect(
      await screen.findByText(
        "Connect a compatible resource account before using this action.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no longer available/i)).not.toBeInTheDocument();
    // One Connection control: nothing to pick yet, so it offers adding one in the extension.
    const opened = vi.spyOn(window, "open").mockReturnValue(null);
    onTestFinished(() => opened.mockRestore());
    const connection = screen.getByRole("combobox", { name: "Connection" });
    expect(connection).toHaveTextContent("Choose connection");
    chooseSetupOption(connection, "Add new connection…");
    expect(opened).toHaveBeenCalledExactlyOnceWith("/extensions/partner.resource?project_id=project_alpha#installed", "_blank", "noopener,noreferrer");
    expect(connection).toHaveTextContent("Choose connection");
    expect(screen.queryByRole("link", { name: /Set up connection/ })).not.toBeInTheDocument();
    const actionSelect = screen.getByRole("combobox", { name: "Action" });
    expect(actionSelect).toHaveTextContent("Unnamed action");
    expect(setupOptions(actionSelect).getByRole("option", { name: "Unnamed action" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("button", { name: "Add Connection binding" })).not.toBeInTheDocument();
    await expectStepIsNotSaved();
  });

  it("keeps refresh available without repair guidance for a healthy bound connection", async () => {
    mockConnectionRequiredStep("project_alpha", {
      connection_id: "connected_account",
      extension_id: "partner.resource",
      extension_install_id: "install_required",
      connection_type: "resource_account",
      display_name: "Authorized resource account",
      enabled: true,
      oauth_lifecycle_status: "connected",
      oauth_requested_scopes: [],
      oauth_granted_scopes: [],
      health_status: "healthy",
      runtime_status: "ready",
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Publish required record/i }));

    const connections = await screen.findByRole("combobox", { name: "Connection" });
    await setupOptions(connections).findByRole("option", { name: "Authorized resource account" });
    await waitFor(() => expect(connections).toHaveTextContent("Authorized resource account"));
    expect(screen.getByRole("button", { name: "Refresh connections" })).toBeEnabled();
    expect(screen.queryByText(/Create or repair it on the extension's connection page/i))
      .not.toBeInTheDocument();
  });

  it("keeps a connected account unavailable until it grants the selected action's required scopes", async () => {
    mockConnectionRequiredStep("project_alpha", {
      connection_id: "connected_account",
      connection_type: "resource_account",
      display_name: "Authorized resource account",
      enabled: true,
      oauth_lifecycle_status: "connected",
      oauth_granted_scopes: ["scope:read"],
      health_status: "healthy",
      runtime_status: "ready",
    }, true, ["scope:write"]);

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Publish required record/i }));

    const connectionSelect = await screen.findByRole("combobox", { name: "Connection" });
    expect(await setupOptions(connectionSelect).findByRole("option", { name: "Authorized resource account — Additional access required" })).toBeDisabled();
    expect(setupOptions(screen.getByRole("combobox", { name: "Action" })).getByRole("option", { name: "Unnamed action" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Reconnect this account and approve the access required by this action.")).toBeInTheDocument();
    await expectStepIsNotSaved();
  });

  it("offers an existing compatible connection without asking to create or repair it", async () => {
    mockConnectionRequiredStep("project_alpha", {
      connection_id: "connected_account",
      connection_type: "resource_account",
      display_name: "Authorized resource account",
      enabled: true,
      oauth_lifecycle_status: "connected",
      oauth_granted_scopes: [],
      health_status: "healthy",
      runtime_status: "ready",
    }, false);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Publish required record/i }));
    const editor = within(await screen.findByRole("dialog", { name: "Step editor" }));
    expect(await editor.findByRole("button", { name: "Use Authorized resource account" })).toBeEnabled();
    const actionSelect = editor.getByRole("combobox", { name: "Action" });
    expect(setupOptions(actionSelect).getByRole("option", { name: "Unnamed action" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText(/Create or repair it on the extension's connection page/i)).not.toBeInTheDocument();
    fireEvent.click(editor.getByRole("button", { name: "Use Authorized resource account" }));
    await waitFor(() => expect(editor.getByRole("combobox", { name: "Connection" })).toHaveTextContent("Authorized resource account"));
  });

  it("refreshes connections after extension setup without losing the action draft", async () => {
    mockConnectionRequiredStep("project_alpha");
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Publish required record/i }));
    await screen.findByRole("combobox", { name: "Connection" });

    const previousFetch = fetchSpy.getMockImplementation()!;
    fetchSpy.mockImplementation(async (input, init) => {
      if (stringFromUnknownValue(input).includes("/meta/extension-install-contract")) {
        return new Response(JSON.stringify({
          extension_id: "partner.resource",
          install_id: "install_required",
          install_state: "active",
          operations: [{ operation_id: "publish", connection_type_ids: ["resource_account"], connection_required: true }],
          connections: [{ connection_id: "connected_account", connection_type: "resource_account", display_name: "New resource account" }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return previousFetch(input, init);
    });
    fireEvent.click(screen.getByRole("button", { name: "Refresh connections" }));
    const connections = await screen.findByRole("combobox", { name: "Connection" });
    chooseSetupOption(connections, "New resource account");
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Action" })).toHaveTextContent("Unnamed action"));
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => {
      const saved = fetchSpy.mock.calls.find(([url, options]) =>
        stringFromUnknownValue(url).endsWith("/changes") && options?.method === "POST");
      const payload = stepBodyFromCall(saved);
      expect(payload.override.connector).toMatchObject({ operation_id: "publish", connection_binding: { connection_id: "connected_account" } });
    });
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it.each([
    { operation: "read", config: { operation: "read", input: { artifact_handle: "state.feed_artifact" }, options: { delimiter: ";" }, result_target: "state.feed_dataset" } },
    { operation: "create_dataset", config: { operation: "create_dataset", rows: "state.source_rows", result_target: "state.feed_dataset" } },
  ])("marks the existing $operation Result target as required without changing its draft contract", async ({ operation, config }) => {
    const boundary = prepareTabularRoundtrip(config);
    fireEvent.click(await renderTabularStepButton());
    const resultTarget = screen.getByLabelText("Result target");
    expect(resultTarget).toHaveAccessibleName("Result target");
    expect(resultTarget).toBeRequired();
    expect(resultTarget).toHaveValue("state.feed_dataset");
    const resultLabel = screen.getByText("Result target");
    const requiredMark = within(resultLabel).getByText("*");
    expect(requiredMark).toBeVisible();
    expect(requiredMark).toHaveAttribute("aria-hidden", "true");
    if (operation === "read") {
      expect(screen.getByRole("textbox", { name: "Sheet" })).not.toBeRequired();
      expect(screen.getByRole("textbox", { name: "Delimiter" })).not.toBeRequired();
    }
    fireEvent.change(resultTarget, { target: { value: "state.updated_dataset" } });
    expect(resultTarget).toHaveValue("state.updated_dataset");
    fireEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
    fireEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    const override = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Override JSON" });
    expect(JSON.parse(override.value)).toEqual({ tabular_feed: { ...config, result_target: "state.updated_dataset" } });
    expect(boundary.writes).toEqual([]);
  });
});


function prepareBusinessStepRoundtrip(kind: "set_variable" | "approval", initial: Record<string, unknown>) {
  let stored = structuredClone(initial);
  let revision = 1;
  const writes: Array<Record<string, unknown>> = [];
  const fallbackFetch = fetchSpy.getMockImplementation();
  mockedFetchWorkflowDetail.mockImplementation(async () => ({
    workflow_id: "weekly_report", display_name: "Weekly Report", enabled: true, revision,
    steps: [{ id: "11111111-1111-1111-1111-111111111025", step_id: "business_step",
      display_name: "Business step", step_kind: kind, override: { [kind]: structuredClone(stored) } }],
    groups: [],
  } as never));
  mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
  mockedFetchWorkflowValidation.mockResolvedValue({ validation: { valid: true, errors: [], warnings: [] } } as never);
  fetchSpy.mockImplementation(async (input, init) => {
    if (input === "/api/workflows/weekly_report/changes" && init?.method === "POST") {
      const changeSet = objectOrEmpty(JSON.parse(String(init.body)));
      const [operation] = changeSet.operations as Array<Record<string, unknown>>;
      if (operation.op !== "update_step_config" || operation.step_id !== "business_step")
        throw new Error("Expected the business step config patch");
      const patch = objectOrEmpty(operation.patch);
      writes.push(structuredClone(patch));
      stored = structuredClone(objectOrEmpty(objectOrEmpty(patch.override)[kind]));
      revision += 1;
      return new Response(JSON.stringify({ workflow_id: "weekly_report", revision, valid: true, issues: [], workflow: {} }), {
        status: 200, headers: { "content-type": "application/json" },
      });
    }
    if (!fallbackFetch) throw new Error("Missing ordinary page API fixture");
    return fallbackFetch(input, init);
  });
  return { stored: () => structuredClone(stored), writes };
}

describe("business values save and reopen through the workflow page", () => {
  it("saves an exact Set source object and retains untouched falsy and unknown assignments", async () => {
    const boundary = prepareBusinessStepRoundtrip("set_variable", {
      assignments: { "state.customer_copy": "old", "state.keep_false": false,
        "state.keep_zero": 0, "state.keep_null": null, "state.keep_object": { source: "other", extra: 1 } },
      output_types: { "state.customer_copy": "string" }, opaque: { keep: true },
    });
    mockedFetchWorkflowContract.mockResolvedValue({
      workflow_id: "weekly_report", enabled: true, display_name: "Weekly Report",
      interface: { enabled: true, input_mode: "json_schema", expected_inputs: [],
        input_schema: { type: "object", properties: { customer: { type: "object", title: "Customer",
          properties: { name: { type: "string", title: "Name" } } } } }, expected_outputs: [] },
      definition_issues: [],
    } as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Business step/i }));
    fireEvent.change(screen.getByRole("combobox", { name: "Assignment 1 value mode" }), { target: { value: "source" } });
    const source = await screen.findByRole("combobox", { name: "Assignment 1 previous value" });
    fireEvent.click(source);
    const panel = screen.getByTestId(String(source.getAttribute("aria-controls")));
    fireEvent.mouseDown(await within(panel).findByRole("option", { name: /Customer.*Name/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.stored()).toEqual({
      assignments: { "state.customer_copy": { source: "state.inputs.customer.name" },
        "state.keep_false": false, "state.keep_zero": 0, "state.keep_null": null,
        "state.keep_object": { source: "other", extra: 1 } },
      output_types: { "state.customer_copy": "string" }, opaque: { keep: true },
    });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(await screen.findByRole("button", { name: /Business step/i }));
    expect(await screen.findByRole("combobox", { name: "Assignment 1 previous value" })).toHaveValue("Customer → Name");
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("removes a legacy Set value through Save and does not restore it on reopen", async () => {
    const boundary = prepareBusinessStepRoundtrip("set_variable", {
      target: "state.legacy", value: null, opaque: { keep: true },
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Business step/i }));
    expect(await screen.findByRole("textbox", { name: "Assignment 1 target" })).toHaveValue("Legacy");
    fireEvent.click(screen.getByRole("button", { name: "Remove assignment" }));
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.stored()).toEqual({ assignments: {}, opaque: { keep: true } });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(await screen.findByRole("button", { name: /Business step/i }));
    expect(await screen.findByRole("textbox", { name: "Assignment 1 target" })).toHaveValue("");
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("blocks Save while a single retained Set destination is cleared", async () => {
    const boundary = prepareBusinessStepRoundtrip("set_variable", {
      assignments: { "state.keep": "hello" }, opaque: { keep: true },
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Business step/i }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Assignment 1 target" }),
      { target: { value: "   " } });
    expect(screen.getByRole("alert")).toHaveTextContent("Give each assignment a target.");
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    expect(boundary.writes).toHaveLength(0);
    expect(boundary.stored()).toEqual({ assignments: { "state.keep": "hello" }, opaque: { keep: true } });
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("retains untouched whitespace Approval outputs through Save and reopen", async () => {
    const boundary = prepareBusinessStepRoundtrip("approval", {
      message: "Review", show_outputs: [" ", "state.keep"], opaque: { keep: true },
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Business step/i }));
    fireEvent.click(screen.getByRole("button", { name: "Add reviewer output" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Reviewer output 3" }),
      { target: { value: "state.inputs.customer.name" } });
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.stored()).toEqual({ message: "Review",
      show_outputs: [" ", "state.keep", "state.inputs.customer.name"], opaque: { keep: true } });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(await screen.findByRole("button", { name: /Business step/i }));
    expect(await screen.findByRole("textbox", { name: "Reviewer output 1" })).toHaveValue(" ");
  }, SMART_SETUP_JOURNEY_TIMEOUT);

  it("saves and reopens ordered Approval outputs without rebuilding retained custom paths", async () => {
    const boundary = prepareBusinessStepRoundtrip("approval", {
      message: "Review", show_outputs: ["state.custom.legacy", "state.outputs.text", "state.custom.legacy"],
      opaque: { keep: true },
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Business step/i }));
    fireEvent.click(screen.getByRole("button", { name: "Add reviewer output" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Reviewer output 4" }),
      { target: { value: "state.inputs.customer.name" } });
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.stored()).toEqual({ message: "Review",
      show_outputs: ["state.custom.legacy", "state.outputs.text", "state.custom.legacy", "state.inputs.customer.name"],
      opaque: { keep: true } });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(await screen.findByRole("button", { name: /Business step/i }));
    expect(await screen.findByRole("textbox", { name: "Reviewer output 4" })).toHaveValue("state.inputs.customer.name");
  }, SMART_SETUP_JOURNEY_TIMEOUT);
});
