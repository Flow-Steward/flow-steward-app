import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { useHasCapability } from "@/lib/auth/capabilities";
import { RegistryDataConnectionsPage } from "@/pages/registry/data-connections";
import { valueForSchemaField } from "./connection-page-helpers";

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));

describe("Connection numeric field contract", () => {
  it("rejects fractional integers instead of sending a backend-invalid payload", () => {
    expect(() => valueForSchemaField("5432.5", {
      label: "Port", type: "integer", required: true, minimum: 1, maximum: 65535,
    })).toThrow();
  });
  it("preserves whole integers and decimal number fields", () => {
    expect(valueForSchemaField("5432", { label: "Port", type: "integer", required: true })).toBe(5432);
    expect(valueForSchemaField("1.5", { label: "Ratio", type: "number", required: true })).toBe(1.5);
  });
});

const schemaSnapshot = { resources: [{ resource_id: "orders", columns: [{ name: "id" }] }] };
const connector = {
  connector_id: "extension.database", local_connector_id: "sql", display_name: "Database",
  connector_kind: "extension", runtime_kind: "local_extension", query_language: "sql",
  connection_schema: {
    type: "object", additionalProperties: true, required: ["port"],
    properties: { port: { type: "integer", title: "Port", minimum: 1, maximum: 65535 } },
  },
  secret_schema: { type: "object", properties: {} },
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><RegistryDataConnectionsPage /></QueryClientProvider>);
  return { dispose: () => { view.unmount(); client.clear(); } };
}

function response(payload: unknown) {
  return new Response(JSON.stringify(payload), { status: 200, headers: { "content-type": "application/json" } });
}

function httpStore(
  connectors: Record<string, unknown>[] = [connector],
  configPatch: Record<string, unknown> = {},
) {
  let saved = {
    connection_id: "warehouse-1", workspace_id: "ws-1", display_name: "Warehouse",
    description: "", environment: "dev", enabled: true, tags: [], health_status: "healthy",
    source_kind: "extension", connector_ref_jsonb: { connector_id: "extension.database", local_connector_id: "sql" },
    connection_config_jsonb: { port: 5432, vendor_options: { mode: "strict", retries: [1, 2] }, ...configPatch },
    secret_refs_jsonb: {},
    metadata_jsonb: {
      dataset_connector: { connector_id: "extension.database", local_connector_id: "sql", display_name: "Database" },
      last_schema_snapshot: structuredClone(schemaSnapshot), owner: "ops",
    },
  };
  const writes: Record<string, unknown>[] = [];
  let gets = 0;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === "/api/datasets/data-connections" && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      writes.push(structuredClone(body));
      saved = structuredClone({ ...saved, ...body }) as typeof saved;
      return response(saved);
    }
    if (url.pathname === "/api/datasets/data-connections") {
      gets += 1;
      return response([structuredClone(saved)]);
    }
    if (url.pathname === "/api/datasets/connectors") return response(connectors);
    if (url.pathname === "/api/workspaces") return response([{ id: "ws-1", display_name: "Demo workspace" }]);
    throw new Error(`Unexpected HTTP operation ${init?.method || "GET"} ${url.pathname}`);
  });
  return { writes, read: () => structuredClone(saved), getCount: () => gets };
}

async function editConnection(name: string) {
  await userEvent.click(await screen.findByRole("button", { name: `Actions for ${name}` }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Edit" }));
  return screen.findByRole("dialog", { name: "Edit data connection" });
}

describe("Connection page lossless HTTP save and fresh readback", () => {
  beforeEach(() => { vi.mocked(useHasCapability).mockReturnValue(true); });
  afterEach(() => { vi.restoreAllMocks(); });

  const optionalConnector = {
    ...connector,
    connection_schema: {
      ...connector.connection_schema,
      properties: {
        ...connector.connection_schema.properties,
        optional_note: { type: ["string", "null"], title: "Optional note" },
        empty_label: { type: "string", title: "Empty label" },
      },
    },
  };

  it("keeps schema-legal known null and empty string across unrelated edits and fresh GET", async () => {
    const store = httpStore([optionalConnector], { optional_note: null, empty_label: "" });
    const before = store.read();
    const { dispose: disposeFirst } = renderPage();
    let dialog = await editConnection("Warehouse");
    fireEvent.change(within(dialog).getByRole("textbox", { name: /^Name/ }), { target: { value: "Nullable warehouse" } });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(store.writes).toHaveLength(1));
    expect(store.writes[0]?.connection_config_jsonb).toEqual(before.connection_config_jsonb);
    expect(store.read().connection_config_jsonb).toHaveProperty("optional_note", null);
    expect(store.read().connection_config_jsonb).toHaveProperty("empty_label", "");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const previousGets = store.getCount();
    disposeFirst();

    const { dispose: disposeSecond } = renderPage();
    dialog = await editConnection("Nullable warehouse");
    expect(store.getCount()).toBeGreaterThan(previousGets);
    expect(within(dialog).getByLabelText(/^Optional note/)).toHaveValue("");
    expect(within(dialog).getByLabelText(/^Empty label/)).toHaveValue("");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(store.writes).toHaveLength(2));
    expect(store.writes[1]?.connection_config_jsonb).toEqual(before.connection_config_jsonb);
    expect(store.read().metadata_jsonb).toEqual(before.metadata_jsonb);
    disposeSecond();
  });

  it.each([
    ["optional_note", "Optional note"],
    ["empty_label", "Empty label"],
  ])("honors explicit clear of %s even when the final display equals its original blank projection", async (key, label) => {
    const store = httpStore([optionalConnector], { optional_note: null, empty_label: "" });
    const expected = { ...store.read().connection_config_jsonb } as Record<string, unknown>;
    delete expected[key];
    const { dispose: disposeFirst } = renderPage();
    let dialog = await editConnection("Warehouse");
    await userEvent.click(within(dialog).getByText(/Additional settings/));
    const input = within(dialog).getByLabelText(new RegExp(`^${label}`));
    fireEvent.change(input, { target: { value: "temporary" } });
    fireEvent.change(input, { target: { value: "" } });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(store.writes).toHaveLength(1));
    expect(store.writes[0]?.connection_config_jsonb).toEqual(expected);
    expect(store.read().connection_config_jsonb).not.toHaveProperty(key);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const previousGets = store.getCount();
    disposeFirst();

    const { dispose: disposeSecond } = renderPage();
    dialog = await editConnection("Warehouse");
    expect(store.getCount()).toBeGreaterThan(previousGets);
    await userEvent.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(store.writes).toHaveLength(2));
    expect(store.writes[1]?.connection_config_jsonb).toEqual(expected);
    disposeSecond();
  });

  it("keeps allowed unknown config and the last schema snapshot during an unrelated edit", async () => {
    const store = httpStore();
    const before = store.read();
    const { dispose: disposeFirst } = renderPage();
    let dialog = await editConnection("Warehouse");
    fireEvent.change(within(dialog).getByRole("textbox", { name: /^Name/ }), { target: { value: "Renamed warehouse" } });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(store.writes).toHaveLength(1));
    expect(store.writes[0]?.connection_config_jsonb).toEqual(before.connection_config_jsonb);
    expect(store.writes[0]?.metadata_jsonb).toEqual(before.metadata_jsonb);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const previousGets = store.getCount();
    disposeFirst();

    const { dispose: disposeSecond } = renderPage();
    dialog = await editConnection("Renamed warehouse");
    expect(store.getCount()).toBeGreaterThan(previousGets);
    await userEvent.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(store.writes).toHaveLength(2));
    expect(store.writes[1]?.connection_config_jsonb).toEqual(before.connection_config_jsonb);
    expect(store.read().metadata_jsonb.last_schema_snapshot).toEqual(schemaSnapshot);
    expect(store.read().metadata_jsonb.owner).toBe("ops");
    disposeSecond();
  });

  it("allows clearing a known optional value without retaining it from the preservation base", async () => {
    const store = httpStore([{ ...connector, connection_schema: { ...connector.connection_schema, required: [] } }]);
    const { dispose } = renderPage();
    const dialog = await editConnection("Warehouse");
    const additional = within(dialog).queryByRole("button", { name: /Additional settings/i });
    if (additional) await userEvent.click(additional);
    fireEvent.change(within(dialog).getByLabelText(/^Port/), { target: { value: "" } });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(store.writes).toHaveLength(1));
    expect(store.writes[0]?.connection_config_jsonb).toEqual({ vendor_options: { mode: "strict", retries: [1, 2] } });
    expect(store.read().connection_config_jsonb).not.toHaveProperty("port");
    dispose();
  });

  it("does not transfer connector-specific unknown values or schema snapshots to another connector", async () => {
    const other = { ...connector, connector_id: "extension.other", local_connector_id: "other", display_name: "Other database" };
    const store = httpStore([optionalConnector, other], { optional_note: null, empty_label: "" });
    const { dispose } = renderPage();
    const dialog = await editConnection("Warehouse");
    await userEvent.click(within(dialog).getByRole("combobox", { name: "Connector" }));
    await userEvent.click(await screen.findByRole("option", { name: "Other database" }));
    fireEvent.change(within(dialog).getByLabelText(/^Port/), { target: { value: "6432" } });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(store.writes).toHaveLength(1));
    expect(store.writes[0]?.connector_id).toBe("extension.other");
    expect(store.writes[0]?.connection_config_jsonb).toEqual({ port: 6432 });
    expect(store.writes[0]?.metadata_jsonb).toEqual({ dataset_connector: { connector_id: "extension.other", local_connector_id: "other", display_name: "Other database" } });
    dispose();
  });

  it("blocks a fractional integer at submit even when browser validity is bypassed", async () => {
    const store = httpStore();
    const { dispose: disposeFirst } = renderPage();
    const dialog = await editConnection("Warehouse");
    fireEvent.change(within(dialog).getByLabelText(/^Port/), { target: { value: "5432.5" } });
    // Submit the named control to exercise its real ancestor form without
    // allowing browser numeric validity to suppress the application handler.
    fireEvent.submit(within(dialog).getByLabelText(/^Port/));
    await waitFor(() => expect(screen.getAllByRole("alert", { hidden: true }).length).toBeGreaterThan(0));
    expect(store.writes).toHaveLength(0);
    expect(store.read().connection_config_jsonb.port).toBe(5432);
    disposeFirst();
  });
});
