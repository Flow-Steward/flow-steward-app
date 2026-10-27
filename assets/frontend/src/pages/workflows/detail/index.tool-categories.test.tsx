import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fetchSpy, mockedFetchWorkflowDag, mockedFetchWorkflowDetail, mockedFetchWorkflowValidation, renderPage } from "./index.test-helpers";

const connectors = [
  { connector_id: "extension.fixture.zebra", extension_id: "fixture.zebra", display_name: "Zebra", source: "extension", operations: [] },
  { connector_id: "mcp_shop", display_name: "Shop", source: "mcp_connector", catalog_item_id: "shop", operations: [{ operation_id: "list_products", display_name: "List products", runtime_connector_id: "connector.shop.list_products" }] },
  { connector_id: "extension.fixture.mail", extension_id: "fixture.mail", display_name: "Mail", source: "extension", operations: [{ operation_id: "send", display_name: "Send email" }, { operation_id: "draft", display_name: "Create draft" }] },
  { connector_id: "connector.std.text", display_name: "Text", source: "standard_tool_action", operations: [] },
];

function mockCatalog() {
  fetchSpy.mockImplementation(async (input) => new Response(JSON.stringify(String(input).includes("/meta/connector-catalog") ? { connectors } : { sections: [] }), { status: 200, headers: { "content-type": "application/json" } }));
  mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "tools", revision: 1, display_name: "Tools", steps: [], groups: [] } as never);
  mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
  mockedFetchWorkflowValidation.mockResolvedValue({ validation: { valid: true, errors: [], warnings: [] } } as never);
}

it("separates tool categories, sorts sources, and searches sources and actions", async () => {
  mockCatalog();
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: "+ Add Step" }));
  const kind = await screen.findByRole("combobox", { name: /step kind/i });
  expect(within(kind).queryByRole("option", { name: "Action step" })).not.toBeInTheDocument();
  fireEvent.change(kind, { target: { value: "extension_tools" } });
  await user.click(await screen.findByRole("combobox", { name: "Action source" }));
  const sources = await screen.findByRole("listbox", { name: "Action source options" });
  expect(within(sources).getAllByRole("option").map((option) => option.textContent)).toEqual(["Choose action source", "Mail", "Zebra"]);
  await user.type(screen.getByRole("searchbox", { name: "Action source Search" }), "MAIL");
  expect(within(sources).queryByRole("option", { name: "Zebra" })).not.toBeInTheDocument();
  await user.click(within(sources).getByRole("option", { name: "Mail" }));
  await user.click(screen.getByRole("combobox", { name: "Action" }));
  await user.type(screen.getByRole("searchbox", { name: "Action Search" }), "draft");
  const actions = screen.getByRole("listbox", { name: "Action options" });
  expect(within(actions).queryByRole("option", { name: "Send email" })).not.toBeInTheDocument();
  await user.click(within(actions).getByRole("option", { name: "Create draft" }));
  fireEvent.change(kind, { target: { value: "mcp_tools" } });
  expect(screen.getByRole("combobox", { name: "Action" })).toHaveTextContent("Choose action");
  await user.click(screen.getByRole("combobox", { name: "Tool" }));
  const mcpSources = screen.getByRole("listbox", { name: "Tool options" });
  expect(within(mcpSources).queryByRole("option", { name: "Mail" })).not.toBeInTheDocument();
  const shop = within(mcpSources).getByRole("option", { name: "Shop" });
  expect(within(shop).getByRole("presentation")).toHaveAttribute("src", "/api/mcp/catalog/shop/icon");
  await user.click(shop);
  fireEvent.change(kind, { target: { value: "connector" } });
  await user.click(screen.getByRole("combobox", { name: "Tool" }));
  const systemSources = screen.getByRole("listbox", { name: "Tool options" });
  expect(within(systemSources).getByRole("option", { name: "Text" })).toBeInTheDocument();
  expect(within(systemSources).queryByRole("option", { name: "Shop" })).not.toBeInTheDocument();
}, 15_000);

it("does not restore a saved extension after entering MCP Tools through another kind", async () => {
  mockCatalog();
  mockedFetchWorkflowDetail.mockResolvedValue({
    workflow_id: "tools", revision: 1, display_name: "Tools", groups: [],
    steps: [{ id: "saved-mail", step_id: "saved_mail", display_name: "Saved mail", step_kind: "connector", connector_id: "extension.fixture.mail", override: { connector: { extension_id: "fixture.mail", operation_id: "send" } } }],
  } as never);
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /Saved mail/i }));
  const kind = screen.getByRole("combobox", { name: "Step kind" });
  expect(kind).toHaveValue("extension_tools");
  fireEvent.change(kind, { target: { value: "agent" } });
  fireEvent.change(kind, { target: { value: "mcp_tools" } });
  const source = await screen.findByRole("combobox", { name: "Tool" });
  expect(source).toHaveTextContent("Choose tool");
  await user.click(source);
  const sources = screen.getByRole("listbox", { name: "Tool options" });
  expect(await within(sources).findByRole("option", { name: "Shop" })).toBeInTheDocument();
  expect(within(sources).queryByRole("option", { name: "Mail" })).not.toBeInTheDocument();
}, 15_000);

/* Item 10: every System tools action was "Unavailable action #1/#2". The registry row of
   a built-in tool lost its operations' names, so each was named by its id, which the
   editor never shows. The catalog now names them; "Unavailable action" stays for an
   action the catalog no longer has. */
const systemTools = [
  {
    connector_id: "connector.std.json", display_name: "JSON", source: "standard_tool_action", tool_id: "std.json", tool_url: "/registry/tools/std.json",
    operations: [
      { operation_id: "stringify", display_name: "Stringify", description: "Serialize a JSON object to a deterministic string.", runtime_connector_id: "connector.std.json", parameters: [{ name: "value", type: "json", required: true }], returns: { name: "result", type: "string", required: true } },
      { operation_id: "pick", display_name: "Pick", runtime_connector_id: "connector.std.json", parameters: [{ name: "value", type: "json", required: true }, { name: "path", type: "string", required: true }], returns: { name: "result", type: "json", required: true } },
      // Listed but nameless (its name is its id): it exists, so it is not "unavailable".
      { operation_id: "keys", display_name: "keys", runtime_connector_id: "connector.std.json" },
    ],
  },
];

function mockSystemTools(steps: unknown[] = []) {
  fetchSpy.mockImplementation(async (input) => new Response(JSON.stringify(String(input).includes("/meta/connector-catalog") ? { connectors: systemTools } : { sections: [] }), { status: 200, headers: { "content-type": "application/json" } }));
  mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "tools", revision: 1, display_name: "Tools", steps, groups: [] } as never);
  mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
  mockedFetchWorkflowValidation.mockResolvedValue({ validation: { valid: true, errors: [], warnings: [] } } as never);
}

function optionNames(listbox: HTMLElement): string[] {
  return within(listbox).getAllByRole("option").map((option) => option.textContent || "");
}

it("names a system tool's actions by the catalog, never 'Unavailable action'", async () => {
  mockSystemTools();
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: "+ Add Step" }));
  fireEvent.change(await screen.findByRole("combobox", { name: /step kind/i }), { target: { value: "connector" } });
  await user.click(await screen.findByRole("combobox", { name: "Tool" }));
  await user.click(within(screen.getByRole("listbox", { name: "Tool options" })).getByRole("option", { name: "JSON" }));
  await user.click(screen.getByRole("combobox", { name: "Action" }));

  const actions = optionNames(screen.getByRole("listbox", { name: "Action options" }));
  expect(actions).toEqual(["Choose action", "Stringify", "Pick", "Unnamed action"]);
  expect(actions.join(" ")).not.toMatch(/Unavailable/);
}, 15_000);

it("says 'Unavailable action' only for a saved action the tool no longer has", async () => {
  mockSystemTools([{
    id: "saved-json", step_id: "saved_json", display_name: "Saved JSON", step_kind: "connector", connector_id: "connector.std.json",
    override: { connector: { operation_id: "flatten" } },
  }]);
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /Saved JSON/i }));

  const action = await screen.findByRole("combobox", { name: "Action" });
  expect(action).toHaveTextContent("Unavailable action");
  await user.click(action);
  expect(optionNames(screen.getByRole("listbox", { name: "Action options" }))).toEqual([
    "Choose action", "Stringify", "Pick", "Unnamed action", "Unavailable action",
  ]);
  // The chip that said "Tool Action" is gone; the tool link sits in the closed Action details.
  expect(screen.queryByText("Tool Action")).not.toBeInTheDocument();
  expect(screen.getByText("Action details", { selector: "summary" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open tool", hidden: true })).toHaveAttribute("href", "/registry/tools/std.json");
}, 15_000);
