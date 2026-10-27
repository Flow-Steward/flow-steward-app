import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { objectOrEmpty } from "./detail-helpers";
import { canvasGraphFixture, fetchSpy, mockedFetchWorkflowCanvasGraph, mockedFetchWorkflowContract, mockedFetchWorkflowDetail, mockedFetchWorkflowValidation, renderPage, workflowDetailFixture } from "./index.test-helpers";

// jsdom has no hit testing; the tested canvas clicks are directly on nodes.
const hitTestDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
beforeAll(() => Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [] }));
afterAll(() => {
  if (hitTestDescriptor) Object.defineProperty(document, "elementsFromPoint", hitTestDescriptor);
  else Reflect.deleteProperty(document, "elementsFromPoint");
});

/** Contract rows show one summary line each; the row opens to edit. */
function openContractRow(drawer: HTMLElement, name: string) {
  const summary = within(drawer).getByRole("button", { name: new RegExp(`^${name}`) });
  if (summary.getAttribute("aria-expanded") !== "true") fireEvent.click(summary);
}

it("opens one existing interface editor from Start and both successful stops, preserving the shared draft", async () => {
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps: [{ step_id: "done", step_kind: "stop_success", display_name: "Done" }, { step_id: "other", step_kind: "stop_success", display_name: "Other success" }] }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ entry: { kind: "step", id: "done" }, steps: [{ id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] }, { id: "other", step_id: "other", kind: "stop_success", display_name: "Other success", config_summary: "", ports: [] }], edges: [] }));
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
  expect(await screen.findByRole("heading", { name: "Workflow interface" })).toBeInTheDocument();
  let drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  // The interface is Inputs and Outputs; returned files are outputs, ticked on their step.
  expect(within(drawer).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Inputs", "Outputs"]);
  expect(within(drawer).queryByRole("tab", { name: /Artifacts|External Changes/ })).not.toBeInTheDocument();
  expect(within(drawer).getByRole("tab", { name: "Inputs" })).toHaveAttribute("aria-selected", "true");
  expect(within(drawer).queryByRole("button", { name: /Show|Hide/ })).not.toBeInTheDocument();
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("group", { name: "Open step Done" }));
  drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  await waitFor(() => expect(within(drawer).getByRole("tab", { name: "Outputs" })).toHaveAttribute("aria-selected", "true"));
  // Add output adds the row open.
  fireEvent.click(within(drawer).getByRole("button", { name: "Add output" }));
  fireEvent.change(within(drawer).getByLabelText("Name"), { target: { value: "shared_result" } });
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("group", { name: "Open step Other success" }));
  drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  await waitFor(() => expect(within(drawer).getByRole("tab", { name: "Outputs" })).toHaveAttribute("aria-selected", "true"));
  openContractRow(drawer, "shared_result");
  expect(within(drawer).getByLabelText("Name")).toHaveValue("shared_result");
  fireEvent.click(within(drawer).getByRole("button", { name: "Save workflow settings" }));
  await waitFor(() => {
    const saves = fetchSpy.mock.calls.filter(([url, init]) => String(url) === "/api/workflows/weekly_report" && init?.method === "PATCH");
    expect(saves).toHaveLength(1);
    expect(JSON.parse(String(saves[0][1]?.body)).settings.callable.expected_outputs).toEqual(expect.arrayContaining([expect.objectContaining({ name: "shared_result" })]));
  });
  expect(fetchSpy.mock.calls.filter(([url, init]) => String(url).includes("/steps/") && init?.method === "PUT")).toHaveLength(0);
}, 10_000);

it("keeps successful Stop Edit separate from its global Outputs body action", async () => {
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps: [{ step_id: "done", step_kind: "stop_success", display_name: "Done" }] }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ entry: { kind: "step", id: "done" }, steps: [{ id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] }], edges: [] }));
  renderPage({ surface: "builder" });
  const stop = await screen.findByRole("group", { name: "Open step Done" });
  fireEvent.click(stop);
  let drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  expect(await within(drawer).findByRole("tab", { name: "Outputs" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  // Right-click › Open is the step itself; the card opens Workflow interface › Outputs.
  fireEvent.contextMenu(stop);
  fireEvent.click(screen.getByRole("menuitem", { name: "Open" }));
  drawer = screen.getByRole("dialog", { name: "Step editor" });
  expect(await within(drawer).findByRole("combobox", { name: "Step kind" })).toHaveValue("stop_success");
  expect(within(drawer).getByRole("textbox", { name: "Step label" })).toHaveValue("Done");
  expect(within(drawer).queryByRole("tab", { name: "Outputs" })).not.toBeInTheDocument();
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  fireEvent.click(stop);
  drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  expect(await within(drawer).findByRole("tab", { name: "Outputs" })).toHaveAttribute("aria-selected", "true");
}, 10_000);


it.each([
  ["Inputs", "expected_inputs", "audit_data", "object", "Field description"],
  ["Outputs", "expected_outputs", "audit_result", "object", "Field description"],
] as const)("requires the existing %s row name and deletes it only through its Remove action", async (tab, collection, originalName, type, descriptionLabel) => {
  const input = { name: "audit_data", type: "object", required: true, description: "Original input", default: { region: "eu" }, default_configured: true };
  const output = { name: "audit_result", type: "object", required: true, description: "Original output" };
  const contract = { enabled: false, input_mode: "fields", output_mode: "variables", expected_inputs: [input], expected_outputs: [output] };
  mockedFetchWorkflowContract.mockResolvedValue({ workflow_id: "weekly_report", display_name: "Weekly Report", enabled: false, interface: contract, definition_issues: [] } as never);
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ enabled: false, revision: 5, settings: { callable: contract } }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ enabled: false }));
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
  let drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  fireEvent.click(within(drawer).getByRole("tab", { name: tab }));
  openContractRow(drawer, originalName);
  let name = within(drawer).getByRole("textbox", { name: "Name" });
  expect(name).toHaveValue(originalName);
  expect(name).toBeRequired();
  const nameLabel = within(drawer).getByText("Name", { selector: ".fs-required-label" });
  expect(nameLabel).toBeVisible();
  expect(nameLabel).toHaveTextContent(/^Name\*$/);
  const description = within(drawer).getByRole("textbox", { name: descriptionLabel });
  const originalDescription = (description as HTMLInputElement).value;
  fireEvent.change(name, { target: { value: "" } });
  fireEvent.blur(name);
  fireEvent.click(within(drawer).getByRole("button", { name: "Save workflow settings" }));
  await waitFor(() => {
    expect(name).toHaveAttribute("aria-invalid", "true");
  })
  expect(name).toHaveAccessibleDescription(/name.*required/i);;
  const saves = () => fetchSpy.mock.calls.filter(([url, init]) => String(url) === "/api/workflows/weekly_report" && init?.method === "PATCH");
  expect(saves()).toHaveLength(0);
  expect(screen.queryByText("Workflow settings saved.")).not.toBeInTheDocument();
  expect(description).toHaveValue(originalDescription);
  expect(within(drawer).getByRole("combobox", { name: "Type" })).toHaveValue(type);
  expect(within(drawer).getByRole("checkbox", { name: "Required" })).toBeChecked();
  if (tab === "Inputs") expect(JSON.parse((within(drawer).getByRole("textbox", { name: "Default value (JSON)" }) as HTMLTextAreaElement).value)).toEqual({ region: "eu" });
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Start — workflow inputs" }));
  drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  fireEvent.click(within(drawer).getByRole("tab", { name: tab }));
  // A row whose name was rejected stays open across the reopen.
  name = within(drawer).getByRole("textbox", { name: "Name" });
  expect(name).toHaveValue("");
  expect(within(drawer).getByRole("textbox", { name: descriptionLabel })).toHaveValue(originalDescription);
  fireEvent.change(name, { target: { value: originalName } });
  fireEvent.click(within(drawer).getByRole("button", { name: "Save workflow settings" }));
  await waitFor(() => expect(saves()).toHaveLength(1));
  const repaired = JSON.parse(String(saves()[0][1]?.body));
  expect(repaired.enabled).toBe(false);
  expect(repaired.settings.callable[collection]).toEqual([expect.objectContaining({ name: originalName, type, required: true, description: originalDescription })]);
  expect(repaired.settings.callable.expected_inputs[0].default).toEqual({ region: "eu" });
  await screen.findByText("Workflow settings saved.");
  openContractRow(drawer, originalName);
  fireEvent.click(within(drawer).getByRole("button", { name: `Remove ${tab === "Inputs" ? "input" : "output"}` }));
  fireEvent.click(within(drawer).getByRole("button", { name: "Save workflow settings" }));
  await waitFor(() => expect(saves()).toHaveLength(2));
  const removed = JSON.parse(String(saves()[1][1]?.body));
  expect(removed.enabled).toBe(false);
  expect(removed.settings.callable[collection]).toEqual([]);
  for (const other of ["expected_inputs", "expected_outputs"]) {
    if (other !== collection) expect(removed.settings.callable[other]).toEqual(repaired.settings.callable[other]);
  }
}, 10_000);


it("lists returned files in Outputs from the steps and never writes them into the contract", async () => {
  const returnedFile = {
    name: "invoice_pdf", type: "file", required: false,
    source: { step_id: "render", step_name: "Render invoice", result: "file", target: "state.invoice_pdf" },
  };
  // A workflow saved before the setting existed keeps its old declaration until it is linked.
  const legacy = [{ name: "invoice_pdf", type: "file" }];
  const stored = { enabled: false, trigger_channels: [], input_mode: "fields", strict_inputs: true, expected_inputs: [], expected_outputs: [], expected_artifacts: legacy };
  const publicContract = { ...stored, output_mode: "artifacts", returned_files: [returnedFile] };
  Reflect.deleteProperty(publicContract, "expected_artifacts");
  mockedFetchWorkflowContract.mockResolvedValue({ workflow_id: "weekly_report", display_name: "Weekly Report", enabled: false, interface: publicContract, definition_issues: [] } as never);
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ enabled: false, revision: 5, settings: { callable: stored } }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ enabled: false }));
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
  const drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  fireEvent.click(within(drawer).getByRole("tab", { name: "Outputs" }));
  expect(within(drawer).queryByRole("combobox", { name: "Output mode" })).not.toBeInTheDocument();
  const block = await within(drawer).findByTestId("workflow-contract-returned-files");
  expect(block).toHaveTextContent("invoice_pdf");
  expect(block).toHaveTextContent("Render invoice → file");
  expect(within(block).queryByRole("button", { name: /Add/ })).not.toBeInTheDocument();
  fireEvent.click(within(drawer).getByRole("button", { name: "Add output" }));
  fireEvent.change(within(drawer).getByLabelText("Name"), { target: { value: "total" } });
  fireEvent.click(within(drawer).getByRole("button", { name: "Save workflow settings" }));
  await waitFor(() => expect(fetchSpy.mock.calls.filter(([url, init]) => String(url) === "/api/workflows/weekly_report" && init?.method === "PATCH")).toHaveLength(1));
  const save = fetchSpy.mock.calls.find(([url, init]) => String(url) === "/api/workflows/weekly_report" && init?.method === "PATCH");
  const callable = objectOrEmpty(objectOrEmpty(JSON.parse(String(save?.[1]?.body)).settings).callable);
  // Nothing the interface shows as a returned file is written back; the old declaration is left as it was.
  expect(callable).not.toHaveProperty("returned_files");
  expect(callable).not.toHaveProperty("side_effects");
  expect(callable).not.toHaveProperty("output_mode");
  expect(callable.expected_artifacts).toEqual(legacy);
  expect(callable.expected_outputs).toEqual([expect.objectContaining({ name: "total" })]);
}, 10_000);


it("keeps the disabled Interface warning after saving and reloading, with an explicit settings route", async () => {
  let contract = { enabled: false, trigger_channels: [] as string[], input_mode: "fields", strict_inputs: true, expected_inputs: [], expected_outputs: [{ name: "summary", type: "string", required: true }] };
  let revision = 5;
  const fallback = fetchSpy.getMockImplementation();
  mockedFetchWorkflowContract.mockImplementation(async () => ({ workflow_id: "weekly_report", interface: contract, definition_issues: [] }) as never);
  mockedFetchWorkflowDetail.mockImplementation(async () => workflowDetailFixture({ revision, settings: { callable: contract } }));
  fetchSpy.mockImplementation(async (input, init) => {
    if (String(input) === "/api/workflows/weekly_report" && init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      contract = body.settings.callable;
      revision += 1;
      return new Response(JSON.stringify({ revision }));
    }
    return fallback ? fallback(input, init) : new Response("{}");
  });
  let view = renderPage({ surface: "builder" });
  try {
    fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
    let drawer = await screen.findByRole("dialog", { name: "Workflow interface" });
    expect(await within(drawer).findByText("Workflow interface is off")).toBeVisible();
    expect(within(drawer).getByText(/required-output checks are inactive/)).toHaveTextContent(/Input forms may still validate declared inputs/);
    fireEvent.click(within(drawer).getByRole("tab", { name: "Outputs" }));
    openContractRow(drawer, "summary");
    fireEvent.change(within(drawer).getByRole("textbox", { name: "Field description" }), { target: { value: "The final summary" } });
    fireEvent.click(within(drawer).getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(contract.expected_outputs[0]).toHaveProperty("description", "The final summary"));
    expect(contract.enabled).toBe(false);
    expect(contract.trigger_channels).toEqual([]);
    await screen.findByRole("button", { name: "Saved" });
    view.unmount(); view.router.dispose();
    view = renderPage({ surface: "builder" });
    fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
    drawer = await screen.findByRole("dialog", { name: "Workflow interface" });
    expect(await within(drawer).findByText("Workflow interface is off")).toBeVisible();
    const settings = within(drawer).getByRole("button", { name: "Open workflow settings" });
    expect(settings).toBeEnabled();
    fireEvent.click(settings);
    expect(await screen.findByRole("switch", { name: "Enable workflow interface" })).not.toBeChecked();
    expect(contract.enabled).toBe(false);
    expect(fetchSpy.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(1);
  } finally { view.unmount(); view.router.dispose(); }
}, 10_000);

it("routes the structured inactive Diagnostics warning to the existing activation controls", async () => {
  const message = "Workflow interface is off. Any declared outputs are not returned and required-output checks are inactive until it is enabled. Input forms may still validate declared inputs.";
  const warning = { code: "workflow.callable.inactive", path: "workflow.callable.enabled", severity: "warning", message };
  const contract = { enabled: false, expected_outputs: [{ name: "summary", type: "string", required: true }] };
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ settings: { callable: contract } }));
  mockedFetchWorkflowContract.mockResolvedValue({ workflow_id: "weekly_report", interface: contract, definition_issues: [] } as never);
  mockedFetchWorkflowValidation.mockResolvedValue({ validation: { valid: true, errors: [], warnings: [warning] } } as never);
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ validation_issues: [{ ...warning, field_path: warning.path, title: message, scope: "workflow", scope_id: "workflow", suggested_action: "Open workflow settings and update the workflow contract.", issues: [{ path: warning.path, message }] }] }));
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: /^Diagnostics/ }));
  const diagnostics = await screen.findByRole("region", { name: "Validation issues" });
  fireEvent.click(within(diagnostics).getByRole("button", { name: /Workflow interface is off/ }));
  const settings = await screen.findByRole("dialog", { name: "Workflow settings" });
  expect(await within(settings).findByText("Workflow interface is off")).toBeVisible();
  expect(within(settings).getByRole("switch", { name: "Enable workflow interface" })).not.toBeChecked();
  expect(fetchSpy.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
});

it("explains the concrete output source per row and preserves guidance when reopened", async () => {
  const contract = { enabled: true, trigger_channels: ["ui"], input_mode: "fields", output_mode: "variables", expected_inputs: [{ name: "request", type: "string" }], expected_outputs: [{ name: "summary", type: "string" }] };
  mockedFetchWorkflowContract.mockResolvedValue({ workflow_id: "weekly_report", interface: contract, definition_issues: [] } as never);
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ settings: { callable: contract } }));
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
  let drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  expect(within(drawer).queryByText(/state\.outputs\./)).not.toBeInTheDocument();
  fireEvent.click(within(drawer).getByRole("tab", { name: "Outputs" }));
  openContractRow(drawer, "summary");
  expect(within(drawer).getByText(/state\.outputs\.summary/)).toBeVisible();
  expect(within(drawer).getByText(/Agent.*declares.*output/i)).toBeVisible();
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Start — workflow inputs" }));
  drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  fireEvent.click(within(drawer).getByRole("tab", { name: "Outputs" }));
  openContractRow(drawer, "summary");
  expect(within(drawer).getByText(/state\.outputs\.summary/)).toBeVisible();
});


it("keeps untouched structured schema property presence through Interface Save and reopen", async () => {
  const schema = { type: "object", required: [], properties: {
    untouched: { type: "object", title: "", description: "", required: [], additionalProperties: false },
    with_member: { type: "object", properties: { name: { type: "string", title: "", description: "" } }, required: [] },
    edited: { type: "string", title: "Before" },
  } };
  let contract = { enabled: true, trigger_channels: ["ui"], input_mode: "json_schema",
    expected_inputs: [] as unknown[], input_schema: schema as Record<string, unknown>, expected_outputs: [] as unknown[] };
  let revision = 5;
  const fallback = fetchSpy.getMockImplementation();
  mockedFetchWorkflowContract.mockImplementation(async () => ({
    workflow_id: "weekly_report", display_name: "Weekly Report", enabled: true, interface: contract, definition_issues: [],
  }) as never);
  mockedFetchWorkflowDetail.mockImplementation(async () => workflowDetailFixture({
    enabled: true, revision, settings: { callable: contract },
  }));
  fetchSpy.mockImplementation(async (input, init) => {
    if (String(input) === "/api/workflows/weekly_report" && init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      contract = body.settings.callable;
      revision += 1;
      return new Response(JSON.stringify({ revision }), { status: 200, headers: { "content-type": "application/json" } });
    }
    return fallback ? fallback(input, init) : new Response("{}");
  });
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
  let drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  const edited = within(drawer).getByRole("group", { name: "Structured input 3" });
  fireEvent.change(within(edited).getByRole("textbox", { name: "Label" }), { target: { value: "After" } });
  fireEvent.click(within(drawer).getByRole("button", { name: "Save workflow settings" }));
  await waitFor(() => expect(contract.input_schema).toEqual({
    ...schema, properties: { ...schema.properties, edited: { type: "string", title: "After" } },
  }));
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Start — workflow inputs" }));
  drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  expect(within(within(drawer).getByRole("group", { name: "Structured input 3" })).getByRole("textbox", { name: "Label" })).toHaveValue("After");
});

it("refreshes the applied input picker after Interface Save on the same mounted page", async () => {
  let contract = { enabled: true, trigger_channels: ["ui"], input_mode: "fields",
    expected_inputs: [] as unknown[], input_schema: {} as Record<string, unknown>, expected_outputs: [] as unknown[] };
  let revision = 5;
  const step = { step_id: "use_customer", display_name: "Use customer", step_kind: "transform",
    override: { transform: { input: { source: "state.inputs.audit" }, result_target: "state.result" } } };
  const fallback = fetchSpy.getMockImplementation();
  mockedFetchWorkflowContract.mockImplementation(async () => ({
    workflow_id: "weekly_report", display_name: "Weekly Report", enabled: true, interface: contract, definition_issues: [],
  }) as never);
  mockedFetchWorkflowDetail.mockImplementation(async () => workflowDetailFixture({
    enabled: true, revision, steps: [step], settings: { callable: contract },
  }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ entry: { kind: "step", id: "use_customer" },
    steps: [{ id: "use_customer", step_id: "use_customer", kind: "transform", display_name: "Use customer", config_summary: "", ports: [] }], edges: [] }));
  fetchSpy.mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.includes("/available-variables")) return new Response(JSON.stringify({
      state: [], prior_outputs: [], loop: [], position: "before",
    }), { status: 200, headers: { "content-type": "application/json" } });
    if (url === "/api/workflows/weekly_report" && init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      contract = body.settings.callable;
      revision += 1;
      return new Response(JSON.stringify({ revision }), { status: 200, headers: { "content-type": "application/json" } });
    }
    return fallback ? fallback(input, init) : new Response("{}");
  });
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("group", { name: "Open step Use customer" }));
  let source = await screen.findByRole("textbox", { name: "Transform source" });
  fireEvent.click(source);
  expect(source).not.toHaveAttribute("aria-controls");
  expect(screen.queryByRole("option", { name: /Customer.*Name/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
  const drawer = screen.getByRole("dialog", { name: "Workflow interface" });
  fireEvent.change(within(drawer).getByRole("combobox", { name: "Input mode" }), { target: { value: "json_schema" } });
  fireEvent.click(within(drawer).getByRole("button", { name: "Add structured input" }));
  const customer = within(drawer).getByRole("group", { name: "Structured input 1" });
  fireEvent.change(within(customer).getByRole("textbox", { name: "Name" }), { target: { value: "customer" } });
  fireEvent.change(within(customer).getByRole("textbox", { name: "Label" }), { target: { value: "Customer" } });
  fireEvent.change(within(customer).getByRole("combobox", { name: "Type" }), { target: { value: "object" } });
  fireEvent.click(within(customer).getByRole("button", { name: "Add member" }));
  const member = within(customer).getByRole("group", { name: "Member 1" });
  fireEvent.change(within(member).getByRole("textbox", { name: "Name" }), { target: { value: "name" } });
  fireEvent.change(within(member).getByRole("textbox", { name: "Label" }), { target: { value: "Name" } });
  fireEvent.click(within(drawer).getByRole("button", { name: "Save workflow settings" }));
  await waitFor(() => expect(contract.input_schema).toEqual({ type: "object", properties: {
    customer: { type: "object", title: "Customer", properties: { name: { type: "string", title: "Name" } } },
  } }));
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  fireEvent.click(await screen.findByRole("group", { name: "Open step Use customer" }));
  source = await screen.findByRole("combobox", { name: "Transform source" });
  fireEvent.click(source);
  const panel = screen.getByTestId(String(source.getAttribute("aria-controls")));
  expect(await within(panel).findByRole("option", { name: /Customer.*Name/ })).toBeVisible();
  expect(mockedFetchWorkflowContract.mock.calls.length).toBeGreaterThan(1);
}, 15_000);

it("authors and reopens a labeled Customer Name member through the ordinary Interface form", async () => {
  let contract = { enabled: true, trigger_channels: ["ui"], input_mode: "fields",
    expected_inputs: [] as unknown[], input_schema: {} as Record<string, unknown>, expected_outputs: [] as unknown[] };
  let revision = 5;
  const fallback = fetchSpy.getMockImplementation();
  mockedFetchWorkflowContract.mockImplementation(async () => ({
    workflow_id: "weekly_report", display_name: "Weekly Report", enabled: true, interface: contract, definition_issues: [],
  }) as never);
  mockedFetchWorkflowDetail.mockImplementation(async () => workflowDetailFixture({
    enabled: true, revision, settings: { callable: contract },
  }));
  fetchSpy.mockImplementation(async (input, init) => {
    if (String(input) === "/api/workflows/weekly_report" && init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      contract = body.settings.callable;
      revision += 1;
      return new Response(JSON.stringify({ revision }), { status: 200, headers: { "content-type": "application/json" } });
    }
    return fallback ? fallback(input, init) : new Response("{}");
  });
  let view = renderPage({ surface: "builder" });
  try {
    fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
    let drawer = screen.getByRole("dialog", { name: "Workflow interface" });
    const mode = within(drawer).getByRole("combobox", { name: "Input mode" });
    expect(within(mode).getByRole("option", { name: "Structured inputs" })).toBeInTheDocument();
    fireEvent.change(mode, { target: { value: "json_schema" } });
    fireEvent.click(within(drawer).getByRole("button", { name: "Add structured input" }));
    const customer = within(drawer).getByRole("group", { name: "Structured input 1" });
    fireEvent.change(within(customer).getByRole("textbox", { name: "Name" }), { target: { value: "customer" } });
    fireEvent.change(within(customer).getByRole("textbox", { name: "Label" }), { target: { value: "Customer" } });
    fireEvent.change(within(customer).getByRole("combobox", { name: "Type" }), { target: { value: "object" } });
    fireEvent.click(within(customer).getByRole("checkbox", { name: "Required" }));
    fireEvent.click(within(customer).getByRole("button", { name: "Add member" }));
    const member = within(customer).getByRole("group", { name: "Member 1" });
    fireEvent.change(within(member).getByRole("textbox", { name: "Name" }), { target: { value: "name" } });
    fireEvent.change(within(member).getByRole("textbox", { name: "Label" }), { target: { value: "Name" } });
    fireEvent.click(within(member).getByRole("checkbox", { name: "Required" }));
    fireEvent.click(within(drawer).getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(contract.input_schema).toEqual({
      type: "object", properties: { customer: { type: "object", title: "Customer",
        properties: { name: { type: "string", title: "Name" } }, required: ["name"] } },
      required: ["customer"],
    }));
    expect(contract.expected_inputs).toEqual([]);
    view.unmount(); view.router.dispose();
    view = renderPage({ surface: "builder" });
    fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
    drawer = screen.getByRole("dialog", { name: "Workflow interface" });
    const reopenedCustomer = within(drawer).getByRole("group", { name: "Structured input 1" });
    expect(within(reopenedCustomer).getAllByRole("textbox", { name: "Name" })[0]).toHaveValue("customer");
    expect(within(reopenedCustomer).getAllByRole("textbox", { name: "Label" })[0]).toHaveValue("Customer");
    const reopenedName = within(reopenedCustomer).getByRole("group", { name: "Member 1" });
    expect(within(reopenedName).getByRole("textbox", { name: "Name" })).toHaveValue("name");
    expect(within(reopenedName).getByRole("textbox", { name: "Label" })).toHaveValue("Name");
  } finally { view.unmount(); view.router.dispose(); }
});
