import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { objectOrEmpty } from "./detail-helpers";
import {
  canvasGraphFixture,
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail,
  mockedFindWorkflowLinkedProjectId,
  postedChangeSets,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

// Only the API boundary is replaced. Its readback records the actual patch;
// this proves page state/payload behavior, not real backend validation/storage.
function installNumericPage(kind: string, initialConfig: Record<string, unknown>) {
  let storedConfig = structuredClone(initialConfig);
  let revision = 7;
  const writes: Array<Record<string, unknown>> = [];
  const ordinaryFetch = fetchSpy.getMockImplementation();
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  mockedFetchWorkflowDetail.mockImplementation(async () => workflowDetailFixture({
    revision,
    steps: [{
      id: "11111111-1111-1111-1111-111111111099",
      step_id: "numeric_step",
      display_name: "Numeric step",
      step_kind: kind,
      override: { [kind]: structuredClone(storedConfig) },
    }],
  }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "step", id: "numeric_step" },
    steps: [{
      id: "numeric_step", step_id: "numeric_step", kind,
      display_name: "Numeric step", config_summary: "",
      ports: [{ outcome: { name: "success" }, label: "Next" }],
    }],
  }));
  fetchSpy.mockImplementation(async (input, init) => {
    if (input === "/api/workflows/weekly_report/changes" && init?.method === "POST") {
      if (typeof init.body !== "string") throw new Error("Expected JSON change set");
      const request = objectOrEmpty(JSON.parse(init.body));
      const operations = request.operations as Array<Record<string, unknown>>;
      if (operations.length !== 1 || operations[0].op !== "update_step_config") {
        throw new Error("Expected one actual step config patch");
      }
      const patch = objectOrEmpty(operations[0].patch);
      writes.push(structuredClone(request));
      storedConfig = structuredClone(objectOrEmpty(objectOrEmpty(patch.override)[kind]));
      revision += 1;
      return new Response(JSON.stringify({ workflow_id: "weekly_report", revision, valid: true, issues: [], workflow: {} }), {
        status: 200, headers: { "content-type": "application/json" },
      });
    }
    if (!ordinaryFetch) throw new Error("Missing ordinary page API fixture");
    return ordinaryFetch(input, init);
  });
  return { writes, storedConfig: () => structuredClone(storedConfig) };
}

async function openNumericPage() {
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Numeric step/i }));
  const editor = within(await screen.findByRole("dialog", { name: "Step editor" }));
  await editor.findByRole("button", { name: "Save parameters" });
  return editor;
}

function fieldError(input: HTMLElement) {
  expect(input).toHaveAttribute("aria-invalid", "true");
  const descriptionId = input.getAttribute("aria-describedby");
  expect(descriptionId).toBeTruthy();
  const message = screen.getByRole("alert");
  expect(message).toHaveAttribute("id", descriptionId);
  expect(message).toBeVisible();
  expect(message).toHaveAttribute("role", "alert");
}

describe("numeric fields through the actual workflow page", () => {
  it.each([
    { kind: "dataset_query", config: { query_template_id: "query-1", params: {}, row_limit: 100, result_target: "state.rows" }, label: "Row limit", key: "row_limit" },
    { kind: "http_request", config: { url: "https://example.test", timeout_seconds: 30, max_response_bytes: 4096 }, label: "Max response bytes", key: "max_response_bytes" },
  ])("refuses keyboard-entered incomplete $label, retains it on reopen, and saves an intentional blank", async ({ kind, config, label, key }) => {
    const user = userEvent.setup();
    const boundary = installNumericPage(kind, config);
    await openNumericPage();
    const input = screen.getByLabelText(label);
    await user.clear(input);
    await user.type(input, "1e");
    expect(input).toHaveValue("1e");
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => fieldError(input));
    expect(input).toHaveFocus();
    expect(boundary.writes).toEqual([]);
    expect(postedChangeSets()).toEqual([]);
    expect(boundary.storedConfig()).toEqual(config);
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Numeric step/i }));
    const reopened = await screen.findByLabelText(label);
    expect(reopened).toHaveValue("1e");
    fieldError(reopened);
    await user.clear(reopened);
    expect(reopened).toHaveValue("");
    expect(reopened).not.toHaveAttribute("aria-invalid", "true");
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.writes[0].expected_revision).toBe(7);
    expect(boundary.storedConfig()[key]).toBe("");
  });

  it.each([
    { kind: "dataset_query", config: { query_template_id: "query-1", params: {}, row_limit: "７", result_target: "state.rows" }, key: "row_limit", value: "７" },
    { kind: "http_request", config: { url: "https://example.test", timeout_seconds: "١.٥" }, key: "timeout_seconds", value: "١.٥" },
  ])("preserves backend-valid Unicode $kind numeric strings through rename and Advanced Save", async ({ kind, config, key, value }) => {
    const user = userEvent.setup();
    const boundary = installNumericPage(kind, config);
    await openNumericPage();
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Renamed Unicode step" } });
    await user.click(screen.getByRole("button", { name: "Advanced mode" }));
    await user.click(screen.getByRole("tab", { name: "Advanced" }));
    const raw = screen.getByRole("textbox", { name: "Override JSON" });
    expect((raw as HTMLTextAreaElement).value).toContain(JSON.stringify(value));
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.writes[0].expected_revision).toBe(7);
    expect(boundary.storedConfig()[key]).toBe(value);
    expect(typeof boundary.storedConfig()[key]).toBe("string");
  });

  it("refuses an empty Wait Save, focuses its error, retains the draft on reopen and saves intentional zero", async () => {
    const user = userEvent.setup();
    const boundary = installNumericPage("wait", { duration_ms: 15000 });
    await openNumericPage();
    const duration = screen.getByRole("spinbutton", { name: "Duration, milliseconds" });
    fireEvent.change(duration, { target: { value: "" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => fieldError(duration));
    expect(duration).toHaveValue(null);
    expect(duration).toHaveFocus();
    expect(boundary.writes).toEqual([]);
    expect(postedChangeSets()).toEqual([]);
    expect(boundary.storedConfig()).toEqual({ duration_ms: 15000 });

    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Numeric step/i }));
    const reopened = await screen.findByRole("spinbutton", { name: "Duration, milliseconds" });
    expect(reopened).toHaveValue(null);
    fieldError(reopened);
    fireEvent.change(reopened, { target: { value: "0" } });
    expect(reopened).not.toHaveAttribute("aria-invalid", "true");
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.writes[0].expected_revision).toBe(7);
    expect(boundary.storedConfig()).toEqual({ duration_ms: 0 });
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Numeric step/i }));
    expect(await screen.findByRole("spinbutton", { name: "Duration, milliseconds" })).toHaveValue(0);
  });

  it.each([
    { kind: "dataset_query", config: { query_template_id: "query-1", params: {}, row_limit: 100, result_target: "state.rows" }, label: "Row limit", invalid: "0", valid: "7", key: "row_limit", expected: 7 },
    { kind: "http_request", config: { url: "https://example.test", timeout_seconds: 30 }, label: "Timeout seconds", invalid: "0", valid: "0.01", key: "timeout_seconds", expected: 0.01 },
    { kind: "http_request", config: { url: "https://example.test", timeout_seconds: 30, max_response_bytes: 4096 }, label: "Max response bytes", invalid: "1.5", valid: "8192", key: "max_response_bytes", expected: 8192 },
  ])("blocks invalid $kind $label before saving the corrected exact value", async ({ kind, config, label, invalid, valid, key, expected }) => {
    const user = userEvent.setup();
    const boundary = installNumericPage(kind, config);
    await openNumericPage();
    const input = screen.getByRole(label === "Timeout seconds" ? "spinbutton" : "textbox", { name: label });
    fireEvent.change(input, { target: { value: invalid } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => fieldError(input));
    expect(input).toHaveValue(label === "Timeout seconds" ? Number(invalid) : invalid);
    expect(input).toHaveFocus();
    expect(boundary.writes).toEqual([]);
    fireEvent.change(input, { target: { value: valid } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.writes[0].expected_revision).toBe(7);
    expect(boundary.storedConfig()[key]).toBe(expected);
  });

  it("keeps optional blank byte limits in the payload rather than asserting a runtime default", async () => {
    const user = userEvent.setup();
    const boundary = installNumericPage("http_request", { url: "https://example.test", timeout_seconds: 30, max_response_bytes: 4096 });
    await openNumericPage();
    const input = screen.getByRole("textbox", { name: "Max response bytes" });
    fireEvent.change(input, { target: { value: "" } });
    expect(input).toHaveValue("");
    expect(input).not.toBeRequired();
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.storedConfig().max_response_bytes).toBe("");
    // No claim about the backend's effective byte limit is made by this fixture.
  });

  it("retains the existing HTTP missing-timeout backfill while accepting a smaller positive fraction", async () => {
    const user = userEvent.setup();
    const boundary = installNumericPage("http_request", { url: "https://example.test" });
    await openNumericPage();
    const input = screen.getByRole("spinbutton", { name: "Timeout seconds" });
    expect(input).toHaveValue(30);
    fireEvent.change(input, { target: { value: "0.01" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.storedConfig().timeout_seconds).toBe(0.01);
  });

  it.each([
    { kind: "wait", config: { duration_ms: 0 }, invalid: { duration_ms: -1 } },
    { kind: "dataset_query", config: { query_template_id: "query-1", params: {}, row_limit: 100, result_target: "state.rows" }, invalid: { query_template_id: "query-1", params: {}, row_limit: "1e", result_target: "state.rows" } },
    { kind: "http_request", config: { url: "https://example.test", timeout_seconds: 30, max_response_bytes: 4096 }, invalid: { url: "https://example.test", timeout_seconds: 30, max_response_bytes: "1e" } },
  ])("blocks invalid $kind raw numeric input from Advanced and focuses the visible raw editor", async ({ kind, config, invalid }) => {
    const user = userEvent.setup();
    const boundary = installNumericPage(kind, config);
    await openNumericPage();
    await user.click(screen.getByRole("button", { name: "Advanced mode" }));
    await user.click(screen.getByRole("tab", { name: "Advanced" }));
    const raw = screen.getByRole("textbox", { name: "Override JSON" });
    const invalidJson = JSON.stringify({ [kind]: invalid });
    fireEvent.change(raw, { target: { value: invalidJson } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    expect(boundary.writes).toEqual([]);
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(raw).toHaveFocus();
    expect(raw).toHaveValue(invalidJson);
  });


  it.each([
    { kind: "http_request", config: { url: "https://example.test", timeout_seconds: "30" }, label: "Timeout seconds", key: "timeout_seconds", value: "30" },
    { kind: "http_request", config: { url: "https://example.test", timeout_seconds: "0.01" }, label: "Timeout seconds", key: "timeout_seconds", value: "0.01" },
    { kind: "http_request", config: { url: "https://example.test", timeout_seconds: "12.5" }, label: "Timeout seconds", key: "timeout_seconds", value: "12.5" },
    { kind: "dataset_query", config: { query_template_id: "query-1", params: {}, row_limit: "7", result_target: "state.rows" }, label: "Row limit", key: "row_limit", value: "7" },
  ])("preserves loaded $kind numeric string $value through an unrelated rename", async ({ kind, config, label, key, value }) => {
    const user = userEvent.setup();
    const boundary = installNumericPage(kind, config);
    await openNumericPage();
    const input = screen.getByRole(kind === "dataset_query" ? "textbox" : "spinbutton", { name: label });
    expect(input).toHaveValue(kind === "dataset_query" ? value : Number(value));
    expect(input).not.toHaveAttribute("aria-invalid", "true");
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Renamed numeric step" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.writes[0].expected_revision).toBe(7);
    expect(boundary.storedConfig()[key]).toBe(value);
    expect(typeof boundary.storedConfig()[key]).toBe("string");
  });

  it.each([0, { source: "state.inputs.duration" }])("does not rewrite mapped Wait dialect %s through an unrelated edit", async (mapped) => {
    const user = userEvent.setup();
    const boundary = installNumericPage("wait", { input_mapping: { duration_ms: mapped } });
    await openNumericPage();
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Renamed Wait" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.storedConfig()).toEqual({ input_mapping: { duration_ms: mapped } });
    expect(boundary.writes[0].expected_revision).toBe(7);
  });
});


// H1/H3: real page/editor/Save and canonical readback; transport storage is a
// fixture, so these cases do not establish backend execution or persistence.
function preservedHttpConfig(extra: Record<string, unknown>) {
  return {
    method: "POST",
    url: "https://example.test/items/{{ state.inputs.invoice_id }}",
    headers: { "X-Flag": "false", "X-Trace": { source: "state.inputs.trace" } },
    query_params: { reference: "001" },
    body_template: { source: "state.inputs.payload" },
    response_target: "state.api_response",
    expected_statuses: [200, 201],
    timeout_seconds: 30,
    response_mode: "artifact",
    artifact_filename: "report-{{ state.inputs.invoice_id }}.json",
    auth_mode: "none",
    auth_ref: "retained-reference",
    ...extra,
  };
}

function chooseHttpResponseMode(value: "inline" | "artifact", control: HTMLElement = screen.getByRole("combobox", { name: "Response mode" })) {
  if (control instanceof HTMLSelectElement) {
    fireEvent.change(control, { target: { value } });
    return;
  }
  if (control.getAttribute("aria-expanded") !== "true") {
    fireEvent.mouseDown(control);
    fireEvent.click(control);
  }
  const options = screen.getByRole("listbox", { name: "Response mode options" });
  fireEvent.click(within(options).getByRole("option", { name: value }));
}

describe("HTTP response-mode preservation through the workflow page", () => {
  it.each([
    { label: "omitted", config: {}, expected: 100 * 1024 * 1024, visible: String(100 * 1024 * 1024) },
    { label: "null", config: { max_response_bytes: null }, expected: null, visible: "" },
    { label: "empty", config: { max_response_bytes: "" }, expected: "", visible: "" },
    { label: "custom", config: { max_response_bytes: 12 * 1024 * 1024 }, expected: 12 * 1024 * 1024, visible: String(12 * 1024 * 1024) },
  ])("saves and reopens an artifact $label limit after an unrelated label edit", async ({ config, expected, visible }) => {
    const user = userEvent.setup();
    const initial = preservedHttpConfig(config);
    const boundary = installNumericPage("http_request", initial);
    await openNumericPage();
    expect(screen.getByRole("textbox", { name: "Max response bytes" })).toHaveValue(visible);
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Preserved HTTP step" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.writes[0].expected_revision).toBe(7);
    expect(boundary.writes[0].operations).toEqual([expect.objectContaining({
      op: "update_step_config", step_id: "numeric_step",
      patch: expect.objectContaining({ display_name: "Preserved HTTP step" }),
    })]);
    expect(boundary.storedConfig()).toEqual({ ...initial, max_response_bytes: expected });
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Numeric step/i }));
    expect(await screen.findByRole("textbox", { name: "Max response bytes" })).toHaveValue(visible);
    expect(screen.getByRole("textbox", { name: "Artifact filename" })).toHaveValue(initial.artifact_filename);
  });

  it("retains a dormant filename and explicit limit through inline Save, reopen and artifact Save", async () => {
    const user = userEvent.setup();
    const initial = preservedHttpConfig({ max_response_bytes: 4096 });
    const boundary = installNumericPage("http_request", initial);
    const editor = await openNumericPage();
    chooseHttpResponseMode("inline", editor.getByRole("combobox", { name: "Response mode" }));
    expect(editor.getByRole("textbox", { name: "Max response bytes" })).toHaveValue("4096");
    expect(editor.queryByRole("textbox", { name: "Artifact filename" })).not.toBeInTheDocument();
    await user.click(editor.getByRole("button", { name: "Save parameters" }));
    await editor.findByText("Parameters saved");
    expect(boundary.storedConfig()).toEqual({ ...initial, response_mode: "inline" });
    expect(boundary.writes).toHaveLength(1);
    await user.click(editor.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Numeric step/i }));
    const reopenedEditor = within(await screen.findByRole("dialog", { name: "Step editor" }));
    await reopenedEditor.findByRole("textbox", { name: "Max response bytes" });
    chooseHttpResponseMode("artifact", reopenedEditor.getByRole("combobox", { name: "Response mode" }));
    expect(reopenedEditor.getByRole("textbox", { name: "Artifact filename" })).toHaveValue(initial.artifact_filename);
    expect(reopenedEditor.getByRole("textbox", { name: "Max response bytes" })).toHaveValue("4096");
    await user.click(reopenedEditor.getByRole("button", { name: "Save parameters" }));
    await reopenedEditor.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(2);
    expect(boundary.writes[1].expected_revision).toBe(8);
    expect(boundary.storedConfig()).toEqual(initial);
  }, 15_000);

  it("keeps an over-inline limit visible, links and focuses its error without a write, then restores artifact mode", async () => {
    const user = userEvent.setup();
    const customLimit = 12 * 1024 * 1024;
    const initial = preservedHttpConfig({ max_response_bytes: customLimit });
    const boundary = installNumericPage("http_request", initial);
    await openNumericPage();
    chooseHttpResponseMode("inline");
    const limit = screen.getByRole("textbox", { name: "Max response bytes" });
    expect(limit).toHaveValue(String(customLimit));
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => fieldError(limit));
    expect(limit).toHaveFocus();
    expect(boundary.writes).toEqual([]);
    expect(postedChangeSets()).toEqual([]);
    expect(boundary.storedConfig()).toEqual(initial);
    chooseHttpResponseMode("artifact");
    expect(limit).toHaveValue(String(customLimit));
    expect(limit).not.toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("textbox", { name: "Artifact filename" })).toHaveValue(initial.artifact_filename);
    // Back to the saved mode Save rests; an unrelated label edit sends the config as it is.
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Renamed limit step" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.storedConfig()).toEqual(initial);
  });

  it("retains the explicit inline five-MiB limit on artifact selection and saves an intentional clear", async () => {
    const user = userEvent.setup();
    const initial = preservedHttpConfig({ response_mode: "inline", max_response_bytes: 5 * 1024 * 1024 });
    const boundary = installNumericPage("http_request", initial);
    await openNumericPage();
    chooseHttpResponseMode("artifact");
    const limit = screen.getByRole("textbox", { name: "Max response bytes" });
    expect(limit).toHaveValue(String(5 * 1024 * 1024));
    await user.clear(limit);
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(boundary.writes).toHaveLength(1);
    expect(boundary.storedConfig()).toEqual({ ...initial, response_mode: "artifact", max_response_bytes: "" });
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: /Numeric step/i }));
    expect(await screen.findByRole("textbox", { name: "Max response bytes" })).toHaveValue("");
  }, 15_000);
});


// RB1: renderPage allocates a new QueryClient per call. Unmounting the first
// owner and delaying the second owner's real detail-query result prevents the
// preserved local step draft from serving as the readback witness.
describe("HTTP preservation with a fresh mounted readback owner", () => {
  it.each([
    { label: "omitted", config: {}, expected: 100 * 1024 * 1024, visible: String(100 * 1024 * 1024) },
    { label: "null", config: { max_response_bytes: null }, expected: null, visible: "" },
    { label: "empty", config: { max_response_bytes: "" }, expected: "", visible: "" },
    { label: "custom", config: { max_response_bytes: 12 * 1024 * 1024 }, expected: 12 * 1024 * 1024, visible: String(12 * 1024 * 1024) },
  ])("reads the $label limit and distinguishing server fields after a new mount", async ({ config, expected, visible }) => {
    const user = userEvent.setup();
    const initial = preservedHttpConfig(config);
    const boundary = installNumericPage("http_request", initial);
    let view: ReturnType<typeof renderPage> | undefined;
    let releaseRead: () => void = () => undefined;
    const readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
    let readStarted = 0;
    const completedReads: Array<{ revision: number; config: Record<string, unknown> }> = [];
    try {
      view = renderPage();
      fireEvent.click(await screen.findByRole("button", { name: /Numeric step/i }));
      const editor = within(await screen.findByRole("dialog", { name: "Step editor" }));
      await editor.findByRole("button", { name: "Save parameters" });
      chooseHttpResponseMode("inline", editor.getByRole("combobox", { name: "Response mode" }));
      expect(editor.getByRole("textbox", { name: "Max response bytes" })).toHaveValue(visible);
      expect(editor.queryByRole("textbox", { name: "Artifact filename" })).not.toBeInTheDocument();
      chooseHttpResponseMode("artifact", editor.getByRole("combobox", { name: "Response mode" }));
      expect(editor.getByRole("textbox", { name: "Artifact filename" })).toHaveValue(initial.artifact_filename);
      fireEvent.change(editor.getByRole("textbox", { name: "Step label" }), { target: { value: "Local label before readback" } });
      await user.click(editor.getByRole("button", { name: "Save parameters" }));
      await editor.findByText("Parameters saved");
      expect(boundary.writes).toHaveLength(1);
      expect(boundary.writes[0].expected_revision).toBe(7);
      expect(boundary.storedConfig()).toEqual({ ...initial, max_response_bytes: expected });

      view.unmount();
      view = undefined;
      // Model a later server-owned snapshot, not an invented backend
      // normalization rule. Its filename/target/label differ from our draft.
      const returnedConfig = {
        ...boundary.storedConfig(),
        artifact_filename: "server-returned-{{ state.inputs.invoice_id }}.json",
        response_target: "state.server_returned_response",
      };
      mockedFetchWorkflowDetail.mockImplementation(async () => {
        readStarted += 1;
        await readGate;
        completedReads.push({ revision: 9, config: structuredClone(returnedConfig) });
        return workflowDetailFixture({
          revision: 9,
          steps: [{
            id: "11111111-1111-1111-1111-111111111099",
            step_id: "numeric_step",
            display_name: "Server observed HTTP step",
            step_kind: "http_request",
            override: { http_request: structuredClone(returnedConfig) },
          }],
        });
      });
      mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
        entry: { kind: "step", id: "numeric_step" },
        steps: [{
          id: "numeric_step", step_id: "numeric_step", kind: "http_request",
          display_name: "Server observed HTTP step", config_summary: "",
          ports: [{ outcome: { name: "success" }, label: "Next" }],
        }],
      }));
      view = renderPage();
      await waitFor(() => expect(readStarted).toBeGreaterThan(0));
      expect(completedReads).toEqual([]);
      expect(await screen.findByText("Preparing the workflow")).toBeVisible(); // the page loader paints after its 400 ms delay
      expect(screen.queryByDisplayValue(returnedConfig.artifact_filename)).not.toBeInTheDocument();
      expect(screen.queryByDisplayValue("Local label before readback")).not.toBeInTheDocument();
      releaseRead();
      await waitFor(() => expect(screen.queryByText("Preparing the workflow")).not.toBeInTheDocument());
      fireEvent.click(await screen.findByRole("button", { name: /Server observed HTTP step/i }));
      const readbackEditor = within(await screen.findByRole("dialog", { name: "Step editor" }));
      expect(await readbackEditor.findByRole("textbox", { name: "Max response bytes" })).toHaveValue(visible);
      expect(readbackEditor.getByRole("textbox", { name: "Step label" })).toHaveValue("Server observed HTTP step");
      expect(readbackEditor.getByRole("textbox", { name: "Artifact filename" })).toHaveValue(returnedConfig.artifact_filename);
      expect(readbackEditor.getByRole("textbox", { name: "Response target" })).toHaveValue(returnedConfig.response_target);
      await waitFor(() => expect(completedReads).toContainEqual({ revision: 9, config: returnedConfig }));
      await user.click(screen.getByRole("button", { name: "Advanced mode" }));
      await user.click(readbackEditor.getByRole("tab", { name: "Advanced" }));
      const raw = readbackEditor.getByRole("textbox", { name: "Override JSON" });
      expect(JSON.parse((raw as HTMLTextAreaElement).value).http_request).toEqual(returnedConfig);
      expect(boundary.writes).toHaveLength(1);
      expect(postedChangeSets()).toHaveLength(1);
    } finally {
      releaseRead();
      view?.unmount();
    }
  }, 15_000);
});
