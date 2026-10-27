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

// Existing full-page journeys use the same bounded per-case async allowance.
const MAPPING_JOURNEY_TIMEOUT = 15_000;

const acceptingParameters = [
  { name: "query", type: "string", required: true },
  { name: "filter", type: "json", required: false },
  { name: "note", type: "string", required: false },
  { name: "zero", type: "integer", required: false },
  { name: "flag", type: "boolean", required: false },
  { name: "encoded_null", type: "json", required: false },
  { name: "renamed_filter", type: "json", required: false },
  { name: "renamed_note", type: "string", required: false },
];
// Same accepting operation/connection as the backend contract controls. The
// catalog is a projected fixture; neither it nor these API doubles call a provider.
const operation = {
  operation_id: "send_message", display_name: "Map optional values", parameters: acceptingParameters,
  connection_type_ids: ["mail_account"], required_oauth_scopes: ["mail.send"],
  returns: [{ name: "summary", type: "string", required: false }],
};
const connection = {
  connection_id: "mail-1", display_name: "Mapping account", connection_type: "mail_account",
  enabled: true, runtime_status: "ready", health_status: "ok", oauth_granted_scopes: ["mail.send"],
  connection_config: { oauth: { lifecycle_status: "connected" } },
};
const response = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });

function installMappingPage(kind: "connector" | "invoke_workflow", config: Record<string, unknown>, { enabled = true, rawInput, optionalQuery = false, childContract }: { enabled?: boolean; rawInput?: Record<string, unknown>; optionalQuery?: boolean; childContract?: Record<string, unknown> } = {}) {
  const selectedOperation = { ...operation, parameters: acceptingParameters.map((parameter) => optionalQuery && parameter.name === "query" ? { ...parameter, required: false } : parameter) };
  let revision = 7;
  let stored: Record<string, unknown> = {
    id: "11111111-1111-1111-1111-111111111093", step_id: "mapping_step", display_name: "Mapping step", step_kind: kind,
    override: { [kind]: structuredClone(config) },
    ...(kind === "connector" ? { connector_id: "extension.fixture.mail", connector_install_id: "install-mail", input_mapping_jsonb: structuredClone(rawInput || {}), output_mapping_jsonb: { summary: "state.summary" }, connection_binding_jsonb: { connection_id: "mail-1" } } : {}),
  };
  const writes: Array<Record<string, unknown>> = [];
  const ordinaryFetch = fetchSpy.getMockImplementation();
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  mockedFetchWorkflowDetail.mockImplementation(async () => workflowDetailFixture({ enabled, revision, steps: [structuredClone(stored)] }));
  const canvas = () => canvasGraphFixture({ enabled, entry: { kind: "step", id: "mapping_step" }, steps: [{ id: "mapping_step", step_id: "mapping_step", kind, display_name: stored.display_name, config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] }] });
  mockedFetchWorkflowCanvasGraph.mockImplementation(async () => canvas());
  fetchSpy.mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.includes("/meta/connector-catalog")) return response({ connectors: [{ connector_id: "extension.fixture.mail", connector_install_id: "install-mail", extension_install_id: "install-mail", display_name: "Mapping fixture", operations: [selectedOperation], connections: [connection] }] });
    if (url.includes("/meta/extension-install-contract")) return response({ install_id: "install-mail", install_state: "active", extension_id: "fixture.mail", operations: [selectedOperation], connections: [connection] });
    if (url.includes("/meta/extension-form-schema")) return response({ sections: [] });
    if (url.includes("/meta/callable-workflows")) return response({ workflows: [{ workflow_id: "mapping_child", display_name: "Mapping child", enabled: true, callable: true, trigger_channels: ["parent_workflow"], input_mode: "fields", expected_inputs: [{ name: "request", type: "string", required: true }, { name: "city", type: "string", required: false }], expected_outputs: [{ name: "summary", type: "string", required: false }], returned_files: [], ...childContract }] });
    if (url === "/api/workflows/weekly_report/changes" && init?.method === "POST") {
      if (typeof init.body !== "string") throw new Error("Expected actual JSON change set");
      const request = objectOrEmpty(JSON.parse(init.body));
      const operations = request.operations as Array<Record<string, unknown>>;
      if (request.expected_revision !== revision || operations.length !== 1 || operations[0].op !== "update_step_config" || operations[0].step_id !== "mapping_step") throw new Error("Expected one revision-guarded mapping patch");
      writes.push(structuredClone(request));
      stored = { ...stored, ...structuredClone(objectOrEmpty(operations[0].patch)) };
      revision += 1;
      return response({ workflow_id: "weekly_report", revision, applied: true, valid: enabled, issues: enabled ? [] : [{ code: "invoke.input_mapping_value_required", path: "workflow.steps.mapping_step.override.invoke_workflow.input_mapping.request", message: "Map request." }], workflow: {} });
    }
    if (!ordinaryFetch) throw new Error("Missing ordinary API fixture");
    return ordinaryFetch(input, init);
  });
  return {
    kind, writes, row: () => structuredClone(stored),
    holdFreshRead() {
      stored.display_name = "Server returned mapping step";
      const override = objectOrEmpty(stored.override);
      stored.override = { ...override, [kind]: { ...objectOrEmpty(override[kind]), output_mapping: { summary: "state.returned_summary" } } };
      if (kind === "connector") stored.output_mapping_jsonb = { summary: "state.returned_summary" };
      revision += 1;
      let release: () => void = () => undefined;
      const gate = new Promise<void>((resolve) => { release = resolve; });
      const completed: Array<Record<string, unknown>> = [];
      let started = 0;
      mockedFetchWorkflowDetail.mockImplementation(async () => {
        started += 1;
        await gate;
        completed.push({ revision, row: structuredClone(stored) });
        return workflowDetailFixture({ enabled, revision, steps: [structuredClone(stored)] });
      });
      return { release, completed, started: () => started, revision: () => revision };
    },
  };
}

async function openMappingPage() {
  const view = renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Mapping step/i }));
  await screen.findByRole("button", { name: "Save parameters" });
  return view;
}
async function readFreshMapping(view: ReturnType<typeof renderPage>, boundary: ReturnType<typeof installMappingPage>) {
  view.unmount();
  const read = boundary.holdFreshRead();
  let next: ReturnType<typeof renderPage> | undefined;
  try {
    next = renderPage(); // A new QueryClient/provider/router/page owner, not Close/reopen.
    await waitFor(() => expect(read.started()).toBeGreaterThan(0));
    expect(read.completed).toEqual([]);
    expect(await screen.findByText("Preparing the workflow")).toBeVisible(); // the page loader paints after its 400 ms delay
    expect(screen.queryByDisplayValue("state.returned_summary")).not.toBeInTheDocument();
    read.release();
    await waitFor(() => expect(screen.queryByText("Preparing the workflow")).not.toBeInTheDocument());
    fireEvent.click(await screen.findByRole("button", { name: /Server returned mapping step/i }));
    expect(await screen.findByRole("textbox", { name: "Step label" })).toHaveValue("Server returned mapping step");
    fireEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
    fireEvent.click(await screen.findByRole("tab", { name: "Advanced" }));
    const raw = await screen.findByRole("textbox", { name: "Override JSON" });
    const saved = JSON.parse((raw as HTMLTextAreaElement).value) as Record<string, unknown>;
    const returnedRow = boundary.row();
    const persistedConfig = objectOrEmpty(returnedRow.override)[boundary.kind];
    if (boundary.kind === "connector") {
      const editableConfig = { ...objectOrEmpty(saved.connector) };
      expect(editableConfig.connector_id).toBe(returnedRow.connector_id);
      expect(editableConfig.connector_install_id).toBe(returnedRow.connector_install_id);
      delete editableConfig.connector_id;
      delete editableConfig.connector_install_id;
      expect(editableConfig).toEqual(persistedConfig);
    } else {
      expect(saved[boundary.kind]).toEqual(persistedConfig);
    }
    expect(objectOrEmpty(saved[boundary.kind]).output_mapping).toEqual({ summary: "state.returned_summary" });
    expect(read.completed).toContainEqual({ revision: read.revision(), row: boundary.row() });
    return saved;
  } finally {
    read.release();
    next?.unmount();
  }
}

function connectorConfig(input: Record<string, unknown>) {
  return { operation_id: "send_message", input_mapping: input, output_mapping: { summary: "state.summary" }, connection_binding: { connection_id: "mail-1" } };
}
function currentPatch(boundary: ReturnType<typeof installMappingPage>) {
  const operations = boundary.writes.at(-1)?.operations as Array<Record<string, unknown>>;
  return objectOrEmpty(operations[0].patch);
}

describe("actual page mapping preservation and fresh-owner readback", () => {
  it("saves a sibling edit without losing optional null/blank or literal controls", async () => {
    const initial = { query: "state.query", filter: null, note: "", zero: 0, flag: false, encoded_null: "literal:null" };
    const boundary = installMappingPage("connector", connectorConfig(initial), { rawInput: initial });
    const view = await openMappingPage();
    try {
      fireEvent.change(await screen.findByLabelText("Parameter value 1"), { target: { value: "state.changed" } });
      fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
      await screen.findByText("Parameters saved");
      const expected = { ...initial, query: "state.changed", zero: "literal:0", flag: "literal:false" };
      expect(boundary.writes).toHaveLength(1);
      expect(currentPatch(boundary).input_mapping_jsonb).toEqual(expected);
      expect(objectOrEmpty(objectOrEmpty(currentPatch(boundary).override).connector).input_mapping).toEqual(expected);
      expect(JSON.stringify(currentPatch(boundary))).not.toContain("retainedValue");
      const fresh = await readFreshMapping(view, boundary);
      expect(objectOrEmpty(fresh.connector).input_mapping).toEqual(expected);
      expect(postedChangeSets()).toHaveLength(1);
    } finally { view.unmount(); }
  }, MAPPING_JOURNEY_TIMEOUT);

  it("keeps the exact raw mapping on an unrelated Step-label Save", async () => {
    const initial = { query: "state.query", filter: null, note: "", zero: 0, flag: false, encoded_null: "literal:null" };
    const boundary = installMappingPage("connector", connectorConfig(initial), { rawInput: initial });
    const view = await openMappingPage();
    try {
      await screen.findByLabelText("Parameter value 1");
      fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Unrelated connector label" } });
      fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
      await screen.findByText("Parameters saved");
      expect(currentPatch(boundary).input_mapping_jsonb).toEqual(initial);
      expect(objectOrEmpty(objectOrEmpty(currentPatch(boundary).override).connector).input_mapping).toEqual(initial);
      expect(postedChangeSets()).toHaveLength(1);
    } finally { view.unmount(); }
  }, MAPPING_JOURNEY_TIMEOUT);

  it("hydrates an imported alias, saves an edit, then clears without reviving it", async () => {
    const boundary = installMappingPage("connector", { operation_id: "send_message", input_mapping_jsonb: { query: "literal:old" }, connection_binding: { connection_id: "mail-1" } }, { optionalQuery: true });
    const view = await openMappingPage();
    try {
      // This operation permits every input to be absent; a fully empty map is valid.
      expect(await screen.findByLabelText("Parameter value 1")).toHaveValue("old");
      fireEvent.change(screen.getByLabelText("Parameter value 1"), { target: { value: "new" } });
      fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
      await screen.findByText("Parameters saved");
      expect(currentPatch(boundary).input_mapping_jsonb).toEqual({ query: "literal:new" });
      expect(objectOrEmpty(objectOrEmpty(currentPatch(boundary).override).connector)).not.toHaveProperty("input_mapping_jsonb");
      fireEvent.click(screen.getByRole("button", { name: "Remove parameter mapping 1" }));
      fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
      await waitFor(() => expect(boundary.writes).toHaveLength(2));
      await screen.findByText("Parameters saved");
      expect(currentPatch(boundary).input_mapping_jsonb).toEqual({});
      const fresh = await readFreshMapping(view, boundary);
      expect(objectOrEmpty(fresh.connector).input_mapping).toEqual({});
      expect(objectOrEmpty(fresh.connector)).not.toHaveProperty("input_mapping_jsonb");
      expect(postedChangeSets()).toHaveLength(2);
    } finally { view.unmount(); }
  }, MAPPING_JOURNEY_TIMEOUT);

  it.each(["state.city", "literal:"])("clears an optional declared input from %s as omission and reads it back", async (city) => {
    const user = userEvent.setup();
    const boundary = installMappingPage("invoke_workflow", { workflow_id: "mapping_child", input_mapping: { request: "state.request", city }, output_mapping: { summary: "state.summary" }, artifact_mapping: {} });
    const view = await openMappingPage();
    try {
      const input = await screen.findByLabelText("Input mapping city");
      expect(input).toHaveValue(city);
      await user.clear(input);
      fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
      await screen.findByText("Parameters saved");
      expect(objectOrEmpty(objectOrEmpty(currentPatch(boundary).override).invoke_workflow)).toEqual({ workflow_id: "mapping_child", input_mapping: { request: "state.request" }, output_mapping: { summary: "state.summary" }, artifact_mapping: {} });
      const fresh = await readFreshMapping(view, boundary);
      expect(objectOrEmpty(fresh.invoke_workflow).input_mapping).toEqual({ request: "state.request" });
      expect(postedChangeSets()).toHaveLength(1);
    } finally { view.unmount(); }
  }, MAPPING_JOURNEY_TIMEOUT);

  it.each(["literal:", "literal:null", "literal:0", "literal:false", " "])("keeps intentional optional selector %j through an unrelated Save", async (city) => {
    const boundary = installMappingPage("invoke_workflow", { workflow_id: "mapping_child", input_mapping: { request: "state.request", city }, output_mapping: {}, artifact_mapping: {} });
    const view = await openMappingPage();
    try {
      expect(await screen.findByLabelText("Input mapping city")).toHaveValue(city);
      fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Unrelated label" } });
      fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
      await screen.findByText("Parameters saved");
      expect(objectOrEmpty(objectOrEmpty(objectOrEmpty(currentPatch(boundary).override).invoke_workflow).input_mapping)).toEqual({ request: "state.request", city });
    } finally { view.unmount(); }
  }, MAPPING_JOURNEY_TIMEOUT);

  it("keeps required Clear as a disabled saved draft with server readiness issues", async () => {
    const user = userEvent.setup();
    const boundary = installMappingPage("invoke_workflow", { workflow_id: "mapping_child", input_mapping: { request: "state.request" }, output_mapping: {}, artifact_mapping: {} }, { enabled: false });
    const view = await openMappingPage();
    try {
      await user.clear(await screen.findByLabelText(/^Input mapping request\b/));
      fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
      await screen.findByText("Parameters saved");
      expect(boundary.writes).toHaveLength(1);
      expect(objectOrEmpty(objectOrEmpty(objectOrEmpty(currentPatch(boundary).override).invoke_workflow).input_mapping)).toEqual({ request: "" });
      expect(postedChangeSets()).toHaveLength(1);
    } finally { view.unmount(); }
  }, MAPPING_JOURNEY_TIMEOUT);
});

// Direct Invoke has no Hook alternative in this branch. These journeys use the
// real DsSelect, variable input, page owner and existing revision-guarded save.
describe("direct Invoke requiredness", () => {
  it("marks the child selector required and keeps choosing/saving its actual workflow reference", async () => {
    const user = userEvent.setup();
    const boundary = installMappingPage("invoke_workflow", { workflow_id: "", input_mapping: {}, output_mapping: {}, artifact_mapping: {} }, { enabled: false });
    const view = await openMappingPage();
    try {
      const selector = screen.getByRole("combobox", { name: "Workflow" });
      const selectorRequired = selector.getAttribute("aria-required");
      expect(selectorRequired).toBe("true");
      const caption = screen.getByText("Workflow", { selector: ".fs-required-label" });
      expect(within(caption).getByText("*")).toBeVisible();
      await user.click(selector);
      await user.click(await screen.findByRole("option", { name: "Mapping child" }));
      fireEvent.change(await screen.findByLabelText(/^Input mapping request\b/), { target: { value: "state.request" } });
      fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
      await screen.findByText("Parameters saved");
      expect(boundary.writes).toHaveLength(1);
      expect(boundary.writes[0].expected_revision).toBe(7);
      expect(objectOrEmpty(objectOrEmpty(currentPatch(boundary).override).invoke_workflow)).toEqual({
        workflow_id: "mapping_child", input_mapping: { request: "state.request" }, output_mapping: {}, artifact_mapping: {},
      });
    } finally { view.unmount(); }
  }, MAPPING_JOURNEY_TIMEOUT);

  it("exposes the required published child input in its name/state while leaving the optional input optional", async () => {
    const boundary = installMappingPage("invoke_workflow", { workflow_id: "mapping_child", input_mapping: { request: "state.request", city: "state.city" }, output_mapping: {}, artifact_mapping: {} });
    const view = await openMappingPage();
    try {
      const required = await screen.findByLabelText(/^Input mapping request\b/);
      const optional = screen.getByLabelText("Input mapping city");
      expect(required).toBeRequired();
      expect(required).toHaveAccessibleName(/Input mapping request.*required/i);
      expect(required).toHaveValue("state.request");
      expect(optional).not.toBeRequired();
      expect(optional).toHaveAccessibleName("Input mapping city");
      expect(optional).toHaveValue("state.city");
      expect(boundary.writes).toHaveLength(0);
    } finally { view.unmount(); }
  }, MAPPING_JOURNEY_TIMEOUT);

  it.each(["none", "variables", "artifacts"])("does not derive inactive mapping requiredness from row.required when child output mode is %s", async (outputMode) => {
    const boundary = installMappingPage("invoke_workflow", { workflow_id: "mapping_child", input_mapping: { request: "state.request" }, output_mapping: {}, artifact_mapping: {} }, {
      childContract: { output_mode: outputMode, expected_outputs: [{ name: "summary", type: "string", required: true }], returned_files: [{ name: "report", type: "file", required: true }] },
    });
    const view = await openMappingPage();
    try {
      const output = await screen.findByLabelText(/^Output mapping summary\b/);
      const artifact = screen.getByLabelText(/^Returned files mapping report\b/);
      // Backend predicates require variable outputs only for variables/both,
      // artifacts only for artifacts/both. Active counterparts are not changed
      // by the two input-requiredness findings in this packet.
      if (outputMode !== "variables") {
        expect(output).not.toBeRequired();
        expect(output).toHaveAccessibleName("Output mapping summary");
      }
      if (outputMode !== "artifacts") {
        expect(artifact).not.toBeRequired();
        expect(artifact).toHaveAccessibleName("Returned files mapping report");
      }
      expect(boundary.writes).toHaveLength(0);
    } finally { view.unmount(); }
  }, MAPPING_JOURNEY_TIMEOUT);
});
