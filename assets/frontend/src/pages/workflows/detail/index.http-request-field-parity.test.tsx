import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, onTestFinished } from "vitest";
import * as domBridge from "./detail-dom-bridge";
import { useEffect } from "react";
import {
  canvasGraphFixture, fetchSpy, mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail, mockedFetchWorkflowValidation, mockedFindWorkflowLinkedProjectId,
  postedChangeSets, renderPage, workflowDetailFixture,
} from "./index.test-helpers";

// Capture the real hook before any test installs an observer spy.
const unobservedDomBridge = domBridge.useWorkflowDetailDomBridge;
let restoreBridgeObserver: (() => void) | undefined;
const retireBridgeObserver = () => { restoreBridgeObserver?.(); };
beforeEach(retireBridgeObserver);
afterEach(retireBridgeObserver);

// Each case renders the whole workflow page: 1-2 s alone, past the 5 s default under the full suite.
vi.setConfig({ testTimeout: 15_000 });

const config = {
  method: " post ", url: "https://example.test/items/{{ state.inputs.id }}",
  response_target: "state.response", expected_statuses: [200, 201], timeout_seconds: 30,
  headers: { Flag: "false", Code: "001", Null: "null", Other: "before" },
  query_params: { reference: "001" }, body_template: { source: "state.inputs.payload" },
  response_mode: "artifact", max_response_bytes: 4096,
  artifact_filename: "report.json", auth_mode: "none", auth_ref: "dormant-ref",
};
// JSONB orders object keys by length then bytes; these fixture keys are ASCII.
// Normalize both reads, never values or the order of array entries.
function jsonbRead<T>(value: T): T {
  if (Array.isArray(value)) return value.map(jsonbRead) as T;
  if (value === null || typeof value !== "object") return value;
  const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) =>
    left.length - right.length || (left < right ? -1 : left > right ? 1 : 0));
  return Object.fromEntries(entries.map(([key, item]) => [key, jsonbRead(item)])) as T;
}
function install(initial: Record<string, unknown> = config, enabled = false, orderedRead = false) {
  let stored = structuredClone(initial);
  let revision = 7;
  let held: ((response: Response) => void) | undefined;
  let heldCandidate: Record<string, unknown> | undefined;
  const reads: Array<{ revision: number; config: Record<string, unknown> }> = [];
  const ordinary = fetchSpy.getMockImplementation();
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  mockedFetchWorkflowDetail.mockImplementation(async () => {
    reads.push({ revision, config: structuredClone(stored) });
    return workflowDetailFixture({ enabled, revision, steps: [
      { step_id: "http_a", display_name: "HTTP A", step_kind: "http_request", override: { http_request: orderedRead ? jsonbRead(stored) : structuredClone(stored) } },
      { step_id: "done", display_name: "Done", step_kind: "stop_success" },
    ] });
  });
  mockedFetchWorkflowCanvasGraph.mockImplementation(async () => canvasGraphFixture({ enabled,
    entry: { kind: "step", id: "http_a" }, steps: [
      { id: "http_a", step_id: "http_a", kind: "http_request", display_name: "HTTP A", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", ports: [] },
    ],
  }));
  let hold = false;
  fetchSpy.mockImplementation(async (input, init) => {
    if (input === "/api/workflows/weekly_report/changes" && init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      if (body.operations.length !== 1 || body.operations[0].op !== "update_step_config") throw new Error("Unexpected operation");
      if (hold) { heldCandidate = structuredClone(body.operations[0].patch.override.http_request); return new Promise<Response>((resolve) => { held = resolve; }); }
      const candidate = structuredClone(body.operations[0].patch.override.http_request);
      const issues = [
        ...(!candidate.url ? [{ code: "workflow.step_http_request_url_missing", path: "workflow.steps.http_a.override.http_request.url", message: "URL rejected" }] : []),
        ...(!Array.isArray(candidate.expected_statuses) || !candidate.expected_statuses.length || !candidate.expected_statuses.every((code: unknown) => typeof code === "number" && Number.isInteger(code) && code >= 100 && code <= 599) ? [{ code: "workflow.step_http_request_expected_statuses_invalid", path: "workflow.steps.http_a.override.http_request.expected_statuses", message: "Statuses are incomplete" }] : []),
      ];
      if (enabled && issues.length) return new Response(JSON.stringify({ detail: { issues } }), { status: 400, headers: { "content-type": "application/json" } });
      stored = candidate;
      revision += 1;
      return new Response(JSON.stringify({ revision, applied: true, valid: issues.length === 0, issues, workflow: {} }), { status: 200, headers: { "content-type": "application/json" } });
    }
    if (!ordinary) throw new Error("Missing ordinary transport");
    return ordinary(input, init);
  });
  return { reads, stored: () => structuredClone(stored), replaceRead: (next: Record<string, unknown>) => { stored = structuredClone(next); revision += 1; },
    hold: () => { hold = true; }, accept: (issues: Array<{ code: string; path: string; message: string }> = []) => {
      if (!held || !heldCandidate) throw new Error("No held request");
      stored = structuredClone(heldCandidate); revision += 1;
      held(new Response(JSON.stringify({ revision, applied: true, valid: issues.length === 0, issues, workflow: {} }), { status: 200, headers: { "content-type": "application/json" } }));
    }, reject: () => {
      if (!held) throw new Error("No held request");
      held(new Response(JSON.stringify({ detail: { issues: [{ code: "workflow.step_http_request_url_missing", path: "workflow.steps.http_a.override.http_request.url", message: "URL rejected" }] } }), { status: 400, headers: { "content-type": "application/json" } }));
    } };
}
const touchedConfig = { ...config, headers: { ...config.headers, Other: "touched" } };
/** Save rests until something changes: give the editor one change the test does not watch. */
function touchHttpDraft() {
  const editor = within(screen.getByRole("dialog", { name: "Step editor" }));
  const header = editor.queryByLabelText("Headers value 4");
  if (header) { fireEvent.change(header, { target: { value: "touched" } }); return; }
  const raw = editor.getByRole("textbox", { name: "Override JSON" });
  const next = JSON.parse((raw as HTMLTextAreaElement).value);
  next.http_request.headers = { ...next.http_request.headers, Other: "touched" };
  fireEvent.change(raw, { target: { value: JSON.stringify(next) } });
}
async function open(view = renderPage()) {
  await userEvent.click(await screen.findByRole("button", { name: /HTTP A/i }));
  const editor = within(await screen.findByRole("dialog", { name: "Step editor" }));
  await editor.findByRole("button", { name: "Save parameters" });
  return view;
}

it("keeps typed HTTP siblings when another header changes through the real page", async () => {
  // Mutation caught: old shared map reducer reparses every displayed sibling.
  const boundary = install();
  await open();
  fireEvent.change(screen.getByLabelText("Headers value 4"), { target: { value: "after" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await screen.findByText("Parameters saved");
  expect(boundary.stored()).toEqual({ ...config, headers: { Flag: "false", Code: "001", Null: "null", Other: "after" } });
  expect(postedChangeSets()).toHaveLength(1);
});

it("retains a partial CSV and blocks hidden-last-valid Save, but saves an intentional empty typed draft", async () => {
  const boundary = install();
  await open();
  const statuses = screen.getByRole("textbox", { name: "Expected statuses" });
  fireEvent.change(statuses, { target: { value: "201," } });
  expect(statuses).toHaveValue("201,");
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  expect(statuses).toHaveFocus();
  expect(postedChangeSets()).toEqual([]);
  fireEvent.change(statuses, { target: { value: "" } });
  fireEvent.change(screen.getByLabelText("URL"), { target: { value: "" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  // The button says the save landed; the accepted issue stays on the field (and the card).
  await screen.findByText("Parameters saved");
  expect(screen.getByLabelText("URL")).toHaveAccessibleDescription("URL rejected");
  expect(boundary.stored()).toMatchObject({ expected_statuses: [], url: "" });
  expect(screen.getByLabelText("URL")).not.toHaveFocus();
});

it("asks before the actual kind select can discard an invalid HTTP draft", async () => {
  install(); await open();
  const statuses = screen.getByRole("textbox", { name: "Expected statuses" });
  fireEvent.change(statuses, { target: { value: "201," } });
  fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "stop_success" } });
  const dialog = await screen.findByRole("dialog", { name: "Discard invalid input?" });
  await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("http_request");
  expect(statuses).toHaveValue("201,");
  fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "stop_success" } });
  await userEvent.click(within(await screen.findByRole("dialog", { name: "Discard invalid input?" })).getByRole("button", { name: "Discard input" }));
  expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("stop_success");
});

it("loads all typed HTTP values from a fresh mounted reader instead of retained editor draft", async () => {
  const boundary = install(); const view = await open();
  fireEvent.change(screen.getByLabelText("Headers value 4"), { target: { value: "saved" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await screen.findByText("Parameters saved");
  view.unmount();
  const fromRead = { ...config, url: "https://example.test/fresh", headers: { Flag: "false", Code: "001", Null: "null", Other: "saved" } };
  boundary.replaceRead(fromRead);
  const readCount = boundary.reads.length;
  await open();
  await waitFor(() => expect(boundary.reads.length).toBeGreaterThan(readCount));
  expect(boundary.reads.at(-1)?.config).toEqual(fromRead);
  expect(boundary.reads.at(-1)?.revision).toBe(9);
  expect(screen.getByLabelText("URL")).toHaveValue("https://example.test/fresh");
  touchHttpDraft();
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(2));
  expect(boundary.stored()).toEqual({ ...fromRead, headers: { ...fromRead.headers, Other: "touched" } });
  expect(postedChangeSets()[1]).toMatchObject({ expected_revision: 9 });
});

it("refuses a held error after a URL value ABA through actual controls", async () => {
  const boundary = install(); boundary.hold(); await open();
  touchHttpDraft();
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
  const url = screen.getByLabelText("URL");
  fireEvent.change(url, { target: { value: "https://example.test/new" } });
  fireEvent.change(url, { target: { value: config.url } });
  await act(async () => boundary.reject());
  expect(url).not.toHaveAccessibleDescription("URL rejected");
  expect(url).not.toHaveFocus();
});

it("associates a current server issue with the actual URL control", async () => {
  const boundary = install(); boundary.hold(); await open();
  touchHttpDraft();
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
  await act(async () => boundary.reject());
  const url = screen.getByLabelText("URL");
  await waitFor(() => expect(url).toHaveAccessibleDescription("URL rejected"));
  expect(url).toHaveFocus();
});


it("lets the backend refuse enabled incomplete typed config instead of inventing a client gate", async () => {
  const boundary = install(config, true); await open();
  fireEvent.change(screen.getByLabelText("URL"), { target: { value: "" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
  expect(boundary.stored()).toEqual(config);
  await waitFor(() => expect(screen.getByLabelText("URL")).toHaveAccessibleDescription("URL rejected"));
});

it("refuses an old field publication after kind A to B to A through the actual select", async () => {
  const boundary = install(); boundary.hold(); await open();
  touchHttpDraft();
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
  fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "stop_success" } });
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("stop_success"));
  fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "http_request" } });
  await screen.findByLabelText("URL");
  await act(async () => boundary.reject());
  expect(screen.getByLabelText("URL")).not.toHaveAccessibleDescription("URL rejected");
});


it.each([
  { label: "omitted", body: {}, expected: "" },
  { label: "null", body: { body_template: null }, expected: null },
  { label: "empty", body: { body_template: "" }, expected: "" },
])("qualifies the actual editable body $label provenance without inventing absence", async ({ body, expected }) => {
  const initial: Record<string, unknown> = { ...config };
  delete initial.body_template;
  Object.assign(initial, body);
  const boundary = install(initial); await open();
  fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Body provenance" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await screen.findByText("Parameters saved");
  expect(boundary.stored()).toEqual({ ...initial, body_template: expected });
});

it("stages an incomplete new HTTP config through the actual local authoring action", async () => {
  install(); renderPage({ surface: "builder" });
  await userEvent.click(await screen.findByRole("button", { name: "Add step" }));
  const palette = await screen.findByRole("listbox", { name: "Add step" });
  await userEvent.click(within(palette).getByRole("option", { name: "HTTP request" }));
  const add = await screen.findByRole("button", { name: /Add step to changes|Update new step/ });
  expect(screen.getByLabelText("URL")).toHaveValue("");
  await userEvent.click(add);
  await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied"));
  expect(postedChangeSets()).toEqual([]);
}, 15_000);


it("keeps a newer partial CSV through the actual held Save ACK and changed-config refetch", async () => {
  const boundary = install(); boundary.hold(); await open();
  fireEvent.change(screen.getByLabelText("URL"), { target: { value: "https://example.test/submitted" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
  const statuses = screen.getByRole("textbox", { name: "Expected statuses" });
  fireEvent.change(statuses, { target: { value: "201," } });
  const before = boundary.reads.length;
  await act(async () => boundary.accept());
  await waitFor(() => expect(boundary.reads.length).toBeGreaterThan(before));
  expect(boundary.reads.at(-1)?.config.url).toBe("https://example.test/submitted");
  expect(statuses).toHaveValue("201,");
  expect(statuses).toHaveAttribute("aria-invalid", "true");
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  expect(postedChangeSets()).toHaveLength(1);
});


it.each([" post ", "pOsT", "{{ state.inputs.method }}", { source: "state.inputs.method", retained: true }])("preserves the backend method dialect on an unrelated Save: %j", async (method) => {
  const initial = { ...config, method };
  const boundary = install(initial); await open();
  fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Method retained" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await screen.findByText("Parameters saved");
  expect(boundary.stored()).toEqual(initial);
});

it("retains represented out-of-range statuses in a disabled draft without sending rounded CSV", async () => {
  const boundary = install(); await open();
  const statuses = screen.getByRole("textbox", { name: "Expected statuses" });
  fireEvent.change(statuses, { target: { value: "600, 99" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await screen.findByText("Parameters saved");
  expect(boundary.stored()).toMatchObject({ expected_statuses: [600,99] });
  fireEvent.change(statuses, { target: { value: "9007199254740993" } });
  expect(statuses).toHaveValue("9007199254740993");
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  expect(postedChangeSets()).toHaveLength(1);
  expect(statuses).toHaveFocus();
});


it("fences an Advanced override ABA through the real textarea", async () => {
  const boundary = install(); boundary.hold(); await open();
  touchHttpDraft();
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
  await userEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
  await userEvent.click(screen.getByRole("button", { name: /HTTP A/i }));
  await userEvent.click(screen.getByRole("tab", { name: "Advanced" }));
  const raw = screen.getByRole("textbox", { name: "Override JSON" });
  const original = (raw as HTMLTextAreaElement).value;
  const changed = JSON.parse(original); changed.http_request.url = "https://example.test/new";
  fireEvent.change(raw, { target: { value: JSON.stringify(changed) } });
  fireEvent.change(raw, { target: { value: original } });
  await act(async () => boundary.reject());
  expect(raw).toHaveValue(original);
  expect(raw).not.toHaveFocus();
});


it.each([false, true])("keeps a newer Workflow settings Save busy when an older HTTP Save settles (retired HTTP token: %s)", async (retireHttp) => {
  const boundary = install();
  boundary.hold();
  let releaseWorkflow: ((response: Response) => void) | undefined;
  let workflowWrites = 0;
  const transport = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation((input, init) => {
    if (input === "/api/workflows/weekly_report" && init?.method === "PATCH") {
      workflowWrites += 1;
      return new Promise<Response>((resolve) => { releaseWorkflow = resolve; });
    }
    return transport(input, init);
  });
  let view: ReturnType<typeof renderPage> | undefined;
  let httpStarted = false;
  let httpSettled = false;
  let workflowSettled = false;
  try {
    view = renderPage({ embedded: false });
    await userEvent.click(await screen.findByRole("button", { name: /HTTP A/i }));
    await screen.findByRole("button", { name: "Save parameters" });
    touchHttpDraft();
    await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    httpStarted = true;
    if (retireHttp) {
      fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "stop_success" } });
      await waitFor(() => expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("stop_success"));
    }
    // Existing public header action; no fake drawer/controller or bypass setter.
    await userEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Workflow settings" }));
    await screen.findByRole("heading", { name: "Workflow settings" });
    // Save workflow settings rests until a setting changes.
    fireEvent.change(within(screen.getByRole("dialog", { name: "Workflow settings" })).getByRole("textbox", { name: "Display name" }), { target: { value: "Weekly report, renamed" } });
    await userEvent.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(workflowWrites).toBe(1));
    const saving = screen.getByRole("button", { name: "Saving workflow settings…" });
    expect(saving).toBeDisabled();
    const readsBeforeAck = boundary.reads.length;
    await act(async () => { boundary.accept(); httpSettled = true; });
    if (!retireHttp) await waitFor(() => expect(boundary.reads.length).toBeGreaterThan(readsBeforeAck));
    // The old response has been released through act/render flush; the independent PATCH is still held.
    expect(screen.getByRole("button", { name: "Saving workflow settings…" })).toBe(saving);
    expect(saving).toBeDisabled();
    await userEvent.click(saving);
    expect(workflowWrites).toBe(1);
    expect(postedChangeSets()).toHaveLength(1);
    await act(async () => {
      if (!releaseWorkflow) throw new Error("Workflow PATCH was not observed");
      // Its expected revision is now stale after the HTTP commit: a realistic CAS refusal.
      releaseWorkflow(new Response(JSON.stringify({ detail: "Workflow revision changed" }), { status: 409, headers: { "content-type": "application/json" } }));
      workflowSettled = true;
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save workflow settings" })).toBeEnabled());
    expect(workflowWrites).toBe(1);
  } finally {
    await act(async () => {
      if (httpStarted && !httpSettled) boundary.reject();
      if (releaseWorkflow && !workflowSettled) releaseWorkflow(new Response(JSON.stringify({ detail: "Fixture cleanup" }), { status: 409, headers: { "content-type": "application/json" } }));
    });
    view?.unmount();
  }
});

it("releases its own Step busy slot after its HTTP editor token has retired", async () => {
  const boundary = install(); boundary.hold();
  let view: ReturnType<typeof renderPage> | undefined;
  let started = false;
  let settled = false;
  try {
    view = renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /HTTP A/i }));
    await screen.findByRole("button", { name: "Save parameters" });
    touchHttpDraft();
    await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    started = true;
    fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "stop_success" } });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("stop_success"));
    fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "http_request" } });
    await screen.findByLabelText("URL");
    expect(screen.getByRole("button", { name: "Saving parameters…" })).toBeDisabled();
    await act(async () => { boundary.accept(); settled = true; });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save parameters" })).toBeEnabled());
    expect(postedChangeSets()).toHaveLength(1);
  } finally {
    await act(async () => { if (started && !settled) boundary.reject(); });
    view?.unmount();
  }
});

// HTTP outcome is persistence plus readiness, never a new universal Save gate.
it("clears the accepted-save warning only after a clean revision is saved", async () => {
  const boundary = install(); await open();
  fireEvent.change(screen.getByLabelText("URL"), { target: { value: "" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await screen.findByText("Parameters saved");
  expect(boundary.stored()).toMatchObject({ url: "" });
  // Persisted invalid drafts may be closed: warning is not an unsaved/error state.
  await userEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog", { name: /discard/i })).not.toBeInTheDocument();
  await userEvent.click(await screen.findByRole("button", { name: /HTTP A/i }));
  fireEvent.change(screen.getByLabelText("URL"), { target: { value: "https://example.test/repaired" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await screen.findByText("Parameters saved");
  expect(boundary.stored()).toMatchObject({ url: "https://example.test/repaired" });
  expect(postedChangeSets()).toHaveLength(2);
});

it("does not replace a newer HTTP edit with a held accepted-save warning", async () => {
  const boundary = install(); boundary.hold(); await open();
  fireEvent.change(screen.getByLabelText("URL"), { target: { value: "" } });
  await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
  fireEvent.change(screen.getByLabelText("URL"), { target: { value: "https://example.test/newer" } });
  await act(async () => boundary.accept([{ code: "workflow.step_http_request_url_missing", path: "workflow.steps.http_a.override.http_request.url", message: "URL rejected" }]));
  expect(screen.getByLabelText("URL")).toHaveValue("https://example.test/newer");
  expect(screen.getByLabelText("URL")).not.toHaveAccessibleDescription("URL rejected");
  expect(screen.queryByText("Parameters saved")).not.toBeInTheDocument();
  // The newer edit still waits: Save is live again.
  expect(screen.getByRole("button", { name: "Save parameters" })).toBeEnabled();
});



type CommonHttpControl = "Step label" | "Optional step";
async function openCommonHttpControl(control: CommonHttpControl, view = renderPage()) {
  await open(view);
  if (control === "Optional step") {
    await userEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
    await userEvent.click(screen.getByRole("button", { name: /HTTP A/i }));
    const editor = within(await screen.findByRole("dialog", { name: "Step editor" }));
    await userEvent.click(editor.getByRole("tab", { name: "Advanced" }));
  }
  return view;
}
function editCommonHttpControl(control: CommonHttpControl, restore = false) {
  const editor = within(screen.getByRole("dialog", { name: "Step editor" }));
  if (control === "Step label") fireEvent.change(editor.getByRole("textbox", { name: control }), { target: { value: restore ? "HTTP A" : "Newer HTTP label" } });
  else fireEvent.click(editor.getByRole("checkbox", { name: control }));
}
function expectCommonHttpControl(control: CommonHttpControl, restored = false) {
  const editor = within(screen.getByRole("dialog", { name: "Step editor" }));
  if (control === "Step label") expect(editor.getByRole("textbox", { name: control })).toHaveValue(restored ? "HTTP A" : "Newer HTTP label");
  else if (restored) expect(editor.getByRole("checkbox", { name: control })).not.toBeChecked();
  else expect(editor.getByRole("checkbox", { name: control })).toBeChecked();
}

it.each([
  { control: "Step label", aba: false }, { control: "Optional step", aba: false },
  { control: "Step label", aba: true }, { control: "Optional step", aba: true },
] as const)("protects newer $control intent through a no-op revision readback held before Save completion (ABA: $aba)", async ({ control, aba }) => {
  const boundary = install(config, false, true); boundary.hold();
  const ordinaryValidation = mockedFetchWorkflowValidation.getMockImplementation();
  if (!ordinaryValidation) throw new Error("Missing ordinary validation query");
  let releaseValidation: () => void = () => undefined;
  const validationGate = new Promise<void>((resolve) => { releaseValidation = resolve; });
  let holdValidation = false;
  let heldValidationReads = 0;
  mockedFetchWorkflowValidation.mockImplementation(async (...args) => {
    if (holdValidation) { heldValidationReads += 1; await validationGate; }
    return ordinaryValidation(...args);
  });
  // Own the mounted tree before the first async opening step can time out.
  let view: ReturnType<typeof renderPage> | undefined;
  // Observe the real consumer; retain every actual bridge effect and setter.
  const observed: Array<{ revision: unknown; saving: unknown; workflowId: unknown }> = [];
  const bridgeObserver = vi.spyOn(domBridge, "useWorkflowDetailDomBridge").mockImplementation(function useObservedBridge(ctx) {
    const result = unobservedDomBridge(ctx);
    const revision = ctx.stepServerRevision;
    const saving = ctx.savingWorkflowSurface;
    const workflowId = ctx.stepWorkflowId;
    useEffect(() => { observed.push({ revision, saving, workflowId }); }, [revision, saving, workflowId]);
    return result;
  });
  let restored = false;
  const restoreObserver = () => {
    if (restored) return;
    restored = true;
    const mounted = view;
    view = undefined;
    // The observer adds a hook: unmount before restoring the original hook shape.
    try { mounted?.unmount(); } finally {
      bridgeObserver.mockRestore();
      if (restoreBridgeObserver === restoreObserver) restoreBridgeObserver = undefined;
    }
  };
  restoreBridgeObserver = restoreObserver;
  onTestFinished(restoreObserver);
  let started = false;
  let accepted = false;
  try {
    view = renderPage();
    await openCommonHttpControl(control, view);
    const editor = within(await screen.findByRole("dialog", { name: "Step editor" }));
    const originalRead = await mockedFetchWorkflowDetail.mock.results.at(-1)?.value;
    expect(originalRead?.revision).toBe(7);
    touchHttpDraft();
    await userEvent.click(editor.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    started = true;
    editCommonHttpControl(control);
    if (aba) editCommonHttpControl(control, true);
    expectCommonHttpControl(control, aba);
    holdValidation = true;
    await act(async () => { boundary.accept(); accepted = true; });
    await waitFor(() => expect(heldValidationReads).toBeGreaterThan(0));
    await waitFor(() => expect(observed).toContainEqual({ revision: 8, saving: "step", workflowId: "weekly_report" }));
    const acknowledgedRead = await mockedFetchWorkflowDetail.mock.results.at(-1)?.value;
    expect(acknowledgedRead?.revision).toBe(8);
    // The readback is exactly what was sent: nothing in it may replace the newer edit.
    expect(acknowledgedRead?.steps?.[0]?.override?.http_request).toEqual(touchedConfig);
    expect(boundary.reads.at(-1)).toEqual({ revision: 8, config: touchedConfig });
    expect(editor.getByRole("button", { name: "Saving parameters…" })).toBeDisabled();
    expectCommonHttpControl(control, aba);
    await act(async () => releaseValidation());
    await waitFor(() => expect(editor.queryByRole("button", { name: "Saving parameters…" })).not.toBeInTheDocument());
    expectCommonHttpControl(control, aba);
    expect(screen.queryByText("Parameters saved")).not.toBeInTheDocument();
    if (!aba) expect(editor.getByRole("button", { name: "Save parameters" })).toBeEnabled();
    expect(postedChangeSets()).toHaveLength(1);
  } finally {
    restoreObserver();
    await act(async () => { releaseValidation(); if (started && !accepted) boundary.reject(); });
  }
}, 15_000);

it.each(["Step label", "Optional step"] as const)("retains a current URL server refusal after a newer %s edit", async (control) => {
  const boundary = install(); boundary.hold();
  const view = await openCommonHttpControl(control);
  const editor = within(await screen.findByRole("dialog", { name: "Step editor" }));
  let started = false;
  let settled = false;
  try {
    touchHttpDraft();
    await userEvent.click(editor.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    started = true;
    editCommonHttpControl(control);
    await act(async () => { boundary.reject(); settled = true; });
    expectCommonHttpControl(control);
    if (control === "Optional step") await userEvent.click(editor.getByRole("tab", { name: "Smart setup" }));
    await waitFor(() => expect(editor.getByLabelText("URL")).toHaveAccessibleDescription("URL rejected"));
    expect(screen.queryByText("Parameters saved")).not.toBeInTheDocument();
    expect(postedChangeSets()).toHaveLength(1);
  } finally {
    await act(async () => { if (started && !settled) boundary.reject(); });
    view.unmount();
  }
}, 15_000);
