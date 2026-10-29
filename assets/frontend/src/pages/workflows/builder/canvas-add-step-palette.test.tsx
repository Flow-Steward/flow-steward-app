import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CanvasAddStepPalette } from "./canvas-add-step-palette";

// Deliberately small unit contract fixture, not a captured production catalog.
// Harness metadata provenance is recorded separately and cannot use these rows.
const catalog = { connectors: [{ connector_id: "unit-mail", connector_install_id: "unit-install", extension_id: "unit.mail", display_name: "Unit mail", operations: [{ operation_id: "send", runtime_connector_id: "unit-runtime-mail", display_name: "Send mail", parameters: [{ name: "subject", default: "Hello" }], input_mapping: { priority: "normal" } }, { operation_id: "disabled", display_name: "Disabled action", enabled: false }] }, { connector_id: "disabled-source", display_name: "Disabled source", enabled: false, operations: [{ operation_id: "send", display_name: "Disabled source action" }] }] };
function renderPalette(overrides: Partial<Parameters<typeof CanvasAddStepPalette>[0]> = {}, connectorCatalog: unknown = catalog) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } });
  queryClient.setQueryData(["workflows", "meta", "connector-catalog", "unit-project", "en"], connectorCatalog);
  queryClient.setQueryData(["workflows", "meta", "agent-catalog"], [{ agent_id: "unit-writer", display_name: "Unit writer" }]);
  const onChoose = vi.fn();
  const onDismiss = vi.fn();
  const view = render(<QueryClientProvider client={queryClient}><CanvasAddStepPalette clientPoint={{ x: 640, y: 400 }} projectId="unit-project" sequenceForEach={false} allowGroup allowForEach inserting={false} onChoose={onChoose} onDismiss={onDismiss} {...overrides} /></QueryClientProvider>);
  return { ...view, onChoose, onDismiss, queryClient };
}

// Human clarification: this is a step-type picker, not a catalog item picker.
it("offers only the six action step types and never concrete agents or catalog operations", () => {
  renderPalette();
  const actions = within(screen.getByRole("group", { name: "Actions" }));
  expect(actions.getAllByRole("option").map((option) => option.getAttribute("aria-label"))).toEqual([
    "Agent", "System tools", "Extension tools", "MCP tools", "HTTP request", "Invoke workflow",
  ]);
  expect(screen.queryByRole("option", { name: /Action step/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /Unit writer|Send mail|Disabled action/ })).not.toBeInTheDocument();
});

it.each([
  ["System tools", "connector"], ["MCP tools", "mcp_tools"], ["Extension tools", "extension_tools"],
])("keeps %s as an unconfigured canonical connector choice", (label, toolCategory) => {
  const view = renderPalette();
  fireEvent.click(screen.getByRole("option", { name: label }));
  expect(view.onChoose).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
    catalogKey: `tool-category:${toolCategory}`, stepKind: "connector", displayName: label,
    toolCategory, family: "Actions",
    override: { connector: { operation_id: "", input_mapping: {}, connection_binding: {} } },
  }));
  const choice = view.onChoose.mock.calls[0][0];
  expect(choice).not.toHaveProperty("agentId");
  expect(choice.override.connector).not.toHaveProperty("connector_id");
  expect(choice.override.connector).not.toHaveProperty("connector_install_id");
});

it("lists For each in Flow, a loop, and Groups as a plain Group first, then Parallel lanes", () => {
  renderPalette();
  const flow = within(screen.getByRole("group", { name: "Flow" }));
  expect(flow.getAllByRole("option").map((option) => option.getAttribute("aria-label"))).toEqual(["If", "Switch", "Approval", "Wait", "For each", "Inline loop", "Batch"]);
  const groups = within(screen.getByRole("group", { name: "Groups" }));
  expect(groups.getAllByRole("option").map((option) => option.getAttribute("aria-label"))).toEqual(["Group", "Parallel lanes"]);
  const data = within(screen.getByRole("group", { name: "Data" }));
  expect(data.getAllByRole("option").map((option) => option.getAttribute("aria-label")).slice(0, 2)).toEqual(["Set variable", "Transform"]);
});

it("says what each kind does in one line and shows its title and keys", () => {
  renderPalette({ title: "Add after Classify request · Next" });
  expect(screen.getByText("Add after Classify request · Next")).toBeVisible();
  expect(screen.getByRole("option", { name: "Agent" })).toHaveAccessibleDescription("Ask an agent to read, decide or write.");
  expect(screen.getByRole("option", { name: "Parallel lanes" })).toHaveAccessibleDescription("A group whose lanes run side by side.");
  expect(screen.getByRole("option", { name: "Group" })).toHaveAccessibleDescription("Steps that belong together. Can become a child workflow.");
  expect(screen.getByRole("option", { name: "For each" })).toHaveAccessibleDescription("Run the steps inside once for each item.");
  expect(screen.getByText("Esc close")).toBeInTheDocument();
});

it("ends with Canvas › Note, which search finds by its name", () => {
  const view = renderPalette();
  fireEvent.change(screen.getByRole("combobox", { name: "Search steps" }), { target: { value: "note" } });
  const canvas = within(screen.getByRole("group", { name: "Canvas" }));
  fireEvent.click(canvas.getByRole("option", { name: "Note" }));
  expect(view.onChoose).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ nodeKind: "note", catalogKey: "canvas:note" }));
});

it("creates a plain Group, Parallel lanes, For each and an Inline loop through the container choice", () => {
  const view = renderPalette();
  fireEvent.click(screen.getByRole("option", { name: "Group" }));
  expect(view.onChoose).toHaveBeenLastCalledWith(expect.objectContaining({ nodeKind: "group", catalogKey: "container:group", containerDefaults: { containerKind: "group", bodyKind: "sequence" } }));
  fireEvent.click(screen.getByRole("option", { name: "For each" }));
  expect(view.onChoose).toHaveBeenLastCalledWith(expect.objectContaining({ nodeKind: "for_each", family: "Flow", containerDefaults: { containerKind: "for_each" } }));
  fireEvent.click(screen.getByRole("option", { name: "Parallel lanes" }));
  expect(view.onChoose).toHaveBeenLastCalledWith(expect.objectContaining({ nodeKind: "group", containerDefaults: { containerKind: "group", bodyKind: "lanes" } }));
  fireEvent.click(screen.getByRole("option", { name: "Inline loop" }));
  expect(view.onChoose).toHaveBeenLastCalledWith(expect.objectContaining({ nodeKind: "for_each", presentation: "inline" }));
});

it("does not turn search into individual-agent or child-tool discovery", () => {
  renderPalette();
  fireEvent.change(screen.getByRole("combobox", { name: "Search steps" }), { target: { value: "Unit writer" } });
  expect(screen.queryAllByRole("option")).toEqual([]);
  expect(screen.getByRole("status")).toHaveTextContent("No matching steps");
});

it("focuses search and organizes the scoped choices into the published families", () => {
  renderPalette();
  expect(screen.getByRole("combobox", { name: "Search steps" })).toHaveFocus();
  const list = screen.getByRole("listbox", { name: "Add step" });
  for (const family of ["Actions", "Data", "Flow", "Finish", "Groups", "Canvas"]) expect(within(list).getByRole("group", { name: family })).toBeInTheDocument();
});

it("offers tool step types before a catalog has loaded without inventing an installation", () => {
  const view = renderPalette({}, null);
  fireEvent.click(screen.getByRole("option", { name: "Extension tools" }));
  expect(view.onChoose).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ stepKind: "connector", toolCategory: "extension_tools", displayName: "Extension tools" }));
  expect(view.onChoose.mock.calls[0][0].override.connector).toEqual({ operation_id: "", input_mapping: {}, connection_binding: {} });
  expect(screen.queryByText("Loading catalog…")).not.toBeInTheDocument();
});

it("uses arrows and Enter for the chosen result and closes queried Escape once", () => {
  const view = renderPalette();
  const search = screen.getByRole("combobox", { name: "Search steps" });
  fireEvent.change(search, { target: { value: "Agent" } });
  fireEvent.keyDown(search, { key: "ArrowDown", code: "ArrowDown" });
  expect(screen.getByRole("option", { name: "Agent" })).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(search, { key: "Enter", code: "Enter" });
  expect(view.onChoose).toHaveBeenCalledWith(expect.objectContaining({ catalogKey: "builtin:agent", stepKind: "agent", displayName: "Agent" }));
  fireEvent.keyDown(search, { key: "Escape", code: "Escape" });
  expect(view.onDismiss).toHaveBeenCalledTimes(1);
});

it.each([false, true])("offers direct sequence For each controls only when eligibility is %s", (sequenceForEach) => {
  renderPalette({ sequenceForEach, allowGroup: false, allowForEach: false });
  for (const label of ["Collect", "Break", "Continue"]) {
    if (sequenceForEach) expect(screen.getByRole("option", { name: label })).toBeInTheDocument();
    else expect(screen.queryByRole("option", { name: label })).not.toBeInTheDocument();
  }
  expect(screen.queryByRole("group", { name: "Groups" })).not.toBeInTheDocument();
});

it("keeps kinds with more than one outcome listed on a connection, disabled, with the reason", () => {
  const view = renderPalette({ inserting: true, sequenceForEach: true });
  for (const label of ["If", "Switch", "Approval", "Break", "Continue", "Stop · fail"]) {
    const option = screen.getByRole("option", { name: label });
    expect(option).toHaveAttribute("aria-disabled", "true");
    expect(option).toHaveAccessibleDescription("Only a step with one Next outcome can go on a connection.");
  }
  for (const label of ["For each", "Group", "Parallel lanes"]) expect(screen.getByRole("option", { name: label })).toHaveAccessibleDescription("A group has its own outcomes, so it cannot go on a connection.");
  fireEvent.click(screen.getByRole("option", { name: "If" }));
  expect(view.onChoose).not.toHaveBeenCalled();
  expect(screen.getByRole("option", { name: "Wait" })).not.toHaveAttribute("aria-disabled");
  expect(screen.queryByRole("group", { name: "Canvas" })).not.toBeInTheDocument();
  // Arrows skip the refused kinds.
  const search = screen.getByRole("combobox", { name: "Search steps" });
  fireEvent.change(search, { target: { value: "i" } });
  fireEvent.keyDown(search, { key: "Enter", code: "Enter" });
  expect(view.onChoose).toHaveBeenCalledTimes(1);
  expect(view.onChoose.mock.calls[0][0].stepKind).not.toBe("if");
});

it("sends a genuine discriminated container choice through the same guarded command", () => {
  const view = renderPalette();
  fireEvent.click(screen.getByRole("option", { name: "For each" }));
  expect(view.onChoose).toHaveBeenCalledWith(expect.objectContaining({ nodeKind: "for_each", catalogKey: "container:for_each", displayName: "For each" }));
  expect(view.onChoose.mock.calls[0][0]).not.toHaveProperty("stepKind");
});

it("keeps a busy choice read-only and shows its real refusal without a second delivery", () => {
  const view = renderPalette({ busy: true, error: "Apply the current hook first." });
  const search = screen.getByRole("combobox", { name: "Search steps" });
  expect(search).toHaveAttribute("readonly");
  fireEvent.keyDown(search, { key: "Enter", code: "Enter" });
  expect(view.onChoose).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("Apply the current hook first.");
});


// DEVHOOK03: presentation eligibility is supplied by the detail owner; it is not
// a request field or server authority. The ordinary Invoke option stays separate.
it.each([false, true])("offers a distinct Hook declaration choice when author eligibility is %s", (allowHookDeclaration) => {
  const eligibility = { allowHookDeclaration, inserting: false };
  const view = renderPalette(eligibility);
  expect(screen.getByRole("option", { name: "Invoke workflow" })).toBeInTheDocument();
  const hook = screen.queryByRole("option", { name: "Hook" });
  if (!allowHookDeclaration) { expect(hook).not.toBeInTheDocument(); return; }
  expect(hook).toBeInTheDocument();
  fireEvent.click(hook!);
  const choice = view.onChoose.mock.calls[0][0];
  expect(choice).toMatchObject({ catalogKey: "hook:declaration", stepKind: "invoke_workflow", family: "Groups", override: { invoke_workflow: { hook: { contract_version: 1, input_schema: { type: "object", properties: {} }, input_mapping: {}, stage_contract: { summary: "" } } } } });
  expect(choice).not.toHaveProperty("nodeKind", "group");
  for (const key of ["workflow_id", "input_mapping", "output_mapping", "artifact_mapping"]) expect(choice.override.invoke_workflow).not.toHaveProperty(key);
});


// Supersedes the five concrete catalog-row palette cases after the direct
// human clarification. Metadata and falsy defaults belong to the later setup.
it.each([
  { name: "named extension", connectors: [{ connector_id: "extension.mail", extension_id: "mail", connector_install_id: "install-mail", display_name: "Merchant tools", operations: [{ operation_id: "send", display_name: "Send receipt", parameters: [{ name: "count", default: 0 }, { name: "enabled", default: false }] }] }] },
  { name: "missing metadata", connectors: [{ connector_id: "extension.opaque", extension_id: "opaque", operations: [{ operation_id: "opaque-action" }] }] },
  { name: "identity-valued metadata", connectors: [{ connector_id: "extension.opaque", extension_id: "opaque", display_name: "opaque", operations: [{ operation_id: "opaque-action", display_name: "opaque-action" }] }] },
  { name: "duplicate action labels", connectors: ["a", "z"].map((id) => ({ connector_id: `extension.${id}`, extension_id: id, display_name: "Merchant tools", operations: [{ operation_id: "send", display_name: "Send receipt" }] })) },
  { name: "registry tools", connectors: [{ connector_id: "connector.opaque", tool_id: "opaque", display_name: "opaque", source: "registry_tool", operations: [{ operation_id: "check_contact", display_name: "Check contact", runtime_connector_id: "connector.opaque", parameters: [{ name: "count", default: 0 }, { name: "enabled", default: false }] }] }] },
])("keeps $name behind the three tool step types instead of enumerating operations", (connectorCatalog) => {
  const view = renderPalette({}, connectorCatalog);
  const actions = within(screen.getByRole("group", { name: "Actions" }));
  expect(actions.getAllByRole("option").map((option) => option.getAttribute("aria-label"))).toEqual([
    "Agent", "System tools", "Extension tools", "MCP tools", "HTTP request", "Invoke workflow",
  ]);
  expect(screen.queryByRole("option", { name: /Send receipt|Check contact|opaque|Unit writer/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("option", { name: "Extension tools" }));
  expect(view.onChoose).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
    catalogKey: "tool-category:extension_tools", stepKind: "connector", toolCategory: "extension_tools", displayName: "Extension tools",
    override: { connector: { operation_id: "", input_mapping: {}, connection_binding: {} } },
  }));
});

it("searches names before descriptions: Wait finds Wait, and base64 still finds its kind", () => {
  renderPalette();
  const search = screen.getByRole("combobox", { name: "Search steps" });
  fireEvent.change(search, { target: { value: "Wait" } });
  expect(screen.getAllByRole("option").map((option) => option.getAttribute("aria-label"))).toEqual(["Wait"]);
  fireEvent.change(search, { target: { value: "into a file" } });
  expect(screen.getAllByRole("option").map((option) => option.getAttribute("aria-label"))).toEqual(["File from base64"]);
});

it("disables every lane-forbidden palette choice and preserves all accepted lane steps", () => {
  const onDragStart = vi.fn();
  const laneScope = { laneId: "lane_a", sequenceForEach: false, allowGroup: false, allowForEach: false, onDragStart } as unknown as Partial<Parameters<typeof CanvasAddStepPalette>[0]>;
  const view = renderPalette(laneScope);
  const forbidden = ["If", "Switch", "Approval", "Wait", "Stop · success", "Stop · fail", "For each", "Inline loop"];
  for (const label of forbidden) {
    const option = screen.getByRole("option", { name: label });
    expect(option).toHaveAttribute("aria-disabled", "true");
    expect(option).toHaveAccessibleDescription("This step cannot be added inside a parallel lane.");
    fireEvent.pointerDown(option, { pointerId: 1, button: 0 });
    fireEvent.click(option);
  }
  expect(view.onChoose).not.toHaveBeenCalled();
  expect(onDragStart).not.toHaveBeenCalled();

  for (const label of ["Agent", "System tools", "Invoke workflow", "HTTP request", "DataSet", "Transform", "Tabular feed", "File from base64", "Template", "Set variable", "Batch"]) {
    expect(screen.getByRole("option", { name: label })).not.toHaveAttribute("aria-disabled", "true");
  }
  for (const label of ["Collect", "Break", "Continue"]) expect(screen.queryByRole("option", { name: label })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Group" })).not.toBeInTheDocument();
});

it.each(["Approval", "For each", "Inline loop"])("does not keyboard-select the lane-forbidden %s choice", (label) => {
  const laneScope = { laneId: "lane_a" } as unknown as Partial<Parameters<typeof CanvasAddStepPalette>[0]>;
  const view = renderPalette(laneScope);
  const search = screen.getByRole("combobox", { name: "Search steps" });
  fireEvent.change(search, { target: { value: label } });
  fireEvent.keyDown(search, { key: "ArrowDown", code: "ArrowDown" });
  fireEvent.keyDown(search, { key: "Enter", code: "Enter" });

  expect(view.onChoose).not.toHaveBeenCalled();
});
