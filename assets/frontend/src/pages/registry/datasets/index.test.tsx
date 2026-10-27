import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

import {
  createDataset,
  deleteDataset,
  fetchDataConnections,
  fetchDatasetConnectors,
  fetchDatasets,
  introspectDataConnection,
} from "@/pages/registry/datasets/api";
import { RegistryDatasetsPage } from "@/pages/registry/datasets";
import { fetchWorkspaceProjects, fetchWorkspaces } from "@/pages/registry/workspaces/api";
import { useHasCapability } from "@/lib/auth/capabilities";

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));

vi.mock("@/pages/registry/datasets/api", () => ({
  createDataset: vi.fn(),
  deleteDataset: vi.fn(),
  fetchDataConnections: vi.fn(),
  fetchDatasetConnectors: vi.fn(),
  fetchDatasets: vi.fn(),
  introspectDataConnection: vi.fn(),
  toggleDataset: vi.fn(),
}));

vi.mock("@/pages/registry/workspaces/api", () => ({
  fetchWorkspaceProjects: vi.fn(),
  fetchWorkspaces: vi.fn(),
}));

const mockedCreateDataset = vi.mocked(createDataset);
const mockedDeleteDataset = vi.mocked(deleteDataset);
const mockedFetchDataConnections = vi.mocked(fetchDataConnections);
const mockedFetchDatasetConnectors = vi.mocked(fetchDatasetConnectors);
const mockedFetchDatasets = vi.mocked(fetchDatasets);
const mockedIntrospectDataConnection = vi.mocked(introspectDataConnection);
const mockedFetchWorkspaceProjects = vi.mocked(fetchWorkspaceProjects);
const mockedFetchWorkspaces = vi.mocked(fetchWorkspaces);
const mockedUseHasCapability = vi.mocked(useHasCapability);

/** Two rows that differ in every dimension the toolbar can filter on. */
const DATASET_ROWS = [
  {
    dataset_id: "orders_daily",
    display_name: "Orders daily",
    description: "",
    workspace_id: "ws-1",
    availability_mode: "specific_projects_only",
    project_ids: [],
    connector_id: "extension.install_1.warehouse_sql",
    connector_kind: "database",
    runtime_kind: "local_extension",
    query_language: "sql",
    read_only: true,
    enabled: true,
    tags: ["orders"],
  },
  {
    dataset_id: "events_raw",
    display_name: "Events raw",
    description: "",
    workspace_id: "ws-1",
    availability_mode: "specific_projects_only",
    project_ids: [],
    connector_id: "extension.install_1.warehouse_sql",
    connector_kind: "database",
    runtime_kind: "local_extension",
    query_language: "graphql",
    read_only: false,
    enabled: false,
    tags: [],
  },
];

function renderPage(locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  const messages: Record<string, string> = locale === "lt" ? { "react.registry.none": "Nėra" } : {};
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <QueryClientProvider client={queryClient}>
      <RegistryDatasetsPage />
    </QueryClientProvider></I18nContext.Provider>,
  );
}

describe("RegistryDatasetsPage", () => {
  beforeEach(() => {
    mockedUseHasCapability.mockReturnValue(true);
    mockedCreateDataset.mockReset();
    mockedDeleteDataset.mockReset();
    mockedFetchDataConnections.mockReset();
    mockedFetchDatasetConnectors.mockReset();
    mockedFetchDatasets.mockReset();
    mockedIntrospectDataConnection.mockReset();
    mockedFetchWorkspaceProjects.mockReset();
    mockedFetchWorkspaces.mockReset();

    mockedFetchDatasets.mockResolvedValue([]);
    mockedFetchDatasetConnectors.mockResolvedValue([
      {
        connector_id: "extension.install_1.warehouse_sql",
        local_connector_id: "warehouse_sql",
        display_name: "Warehouse SQL",
        connector_kind: "database",
        runtime_kind: "local_extension",
        query_language: "sql",
      },
    ]);
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "warehouse",
        display_name: "Warehouse",
        workspace_id: "ws-1",
        workspace_display_name: "Default workspace",
        source_kind: "postgres",
        environment: "dev",
        enabled: true,
        health_status: "healthy",
        query_capable: true,
        linked_dataset_count: 0,
        tags: [],
        connector_ref_jsonb: {
          connector_id: "extension.install_1.warehouse_sql",
          local_connector_id: "warehouse_sql",
        },
        connection_config_jsonb: {},
        secret_refs_jsonb: {},
        metadata_jsonb: {
          last_schema_snapshot: {
            resources: [{ resource_id: "orders", columns: [{ name: "id" }] }],
          },
        },
      },
    ]);
    mockedFetchWorkspaces.mockResolvedValue([
      {
        id: "ws-1",
        tenant_id: "tenant-1",
        account_id: "account-1",
        workspace_key: "default",
        display_name: "Default workspace",
        description: "",
        status: "active",
        metadata_jsonb: {},
        projects_count: 0,
        jobs_count: 0,
      },
    ]);
    mockedFetchWorkspaceProjects.mockResolvedValue([]);
  });

  it("keeps an operator dataset catalog read-only", async () => {
    mockedUseHasCapability.mockReturnValue(false);
    renderPage();

    expect(screen.queryByRole("button", { name: "New Dataset" })).not.toBeInTheDocument();
  });

  it("shows dataset create validation near the submit button", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Display Name"), "Orders");
    await user.click(within(dialog).getByRole("button", { name: "Create dataset" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Select at least one resource before creating a dataset.");
    expect(mockedCreateDataset).not.toHaveBeenCalled();
  });

  it("keeps the create draft and selected resources when introspection returns an application refusal", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedIntrospectDataConnection
      .mockResolvedValueOnce({
        ok: true,
        result: {
          schema_snapshot: {
            resources: [
              { resource_id: "customer_records", columns: [{ name: "record_id" }] },
              { resource_id: "purchase_records", columns: [{ name: "record_id" }] },
            ],
          },
        },
      } as never)
      .mockResolvedValueOnce({
        ok: false,
        result: {},
        errors: [{ code: "connection_unavailable", message: "The selected extension connection is unavailable." }],
      } as never);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    const dialog = await screen.findByRole("dialog", { name: "Create dataset" });
    const connection = within(dialog).getByRole("combobox", { name: "Data Connection" });
    await user.click(within(dialog).getByRole("combobox", { name: "Workspace owner" }));
    await user.click(await screen.findByRole("option", { name: "Default workspace" }));
    await user.click(connection);
    await user.click(await screen.findByRole("option", { name: /Warehouse \(healthy\)/ }));
    const name = within(dialog).getByLabelText("Display Name");
    await user.type(name, "Quarterly reporting");
    expect(within(dialog).getByLabelText("Display Name")).toHaveValue("Quarterly reporting");
    expect(connection).toHaveTextContent("Warehouse (healthy)");

    await user.click(within(dialog).getByRole("button", { name: "Introspect schema" }));
    await screen.findByText("Schema loaded. Select the resources you want to expose in this dataset.");
    expect(within(dialog).getByLabelText("Display Name")).toHaveValue("Quarterly reporting");
    const selectedResource = await within(dialog).findByRole("checkbox", { name: /customer_records/i });
    await user.click(selectedResource);
    expect(selectedResource).toBeChecked();
    expect(within(dialog).getByText("purchase_records")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Introspect schema" }));

    const refusal = await within(dialog).findByRole("alert");
    expect(refusal).toHaveTextContent("The selected extension connection is unavailable.");
    expect(screen.getByRole("dialog", { name: "Create dataset" })).toBe(dialog);
    expect(within(dialog).getByLabelText("Display Name")).toHaveValue("Quarterly reporting");
    expect(within(dialog).getByRole("combobox", { name: "Data Connection" })).toHaveTextContent("Warehouse (healthy)");
    expect(within(dialog).getByRole("checkbox", { name: /customer_records/i })).toBeChecked();
    expect(within(dialog).getByText("purchase_records")).toBeInTheDocument();
    expect(screen.queryByText("Schema loaded, but no resources were returned.")).not.toBeInTheDocument();
    expect(mockedCreateDataset).not.toHaveBeenCalled();
  });

  it("clears a refusal from connection A after selecting connection B", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "warehouse-a",
        display_name: "Warehouse A",
        workspace_id: "ws-1",
        workspace_display_name: "Default workspace",
        source_kind: "postgres",
        environment: "dev",
        enabled: true,
        health_status: "healthy",
        query_capable: true,
        linked_dataset_count: 0,
        tags: [],
        connector_ref_jsonb: { connector_id: "extension.install_1.warehouse_sql" },
        connection_config_jsonb: {},
        secret_refs_jsonb: {},
        metadata_jsonb: {},
      },
      {
        connection_id: "warehouse-b",
        display_name: "Warehouse B",
        workspace_id: "ws-1",
        workspace_display_name: "Default workspace",
        source_kind: "postgres",
        environment: "dev",
        enabled: true,
        health_status: "healthy",
        query_capable: true,
        linked_dataset_count: 0,
        tags: [],
        connector_ref_jsonb: { connector_id: "extension.install_1.warehouse_sql" },
        connection_config_jsonb: {},
        secret_refs_jsonb: {},
        metadata_jsonb: {},
      },
    ] as never);
    mockedIntrospectDataConnection.mockResolvedValue({
      ok: false,
      result: {},
      errors: [{ code: "connection_unavailable", message: "The selected extension connection is unavailable." }],
    } as never);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    const dialog = await screen.findByRole("dialog", { name: "Create dataset" });
    await user.click(within(dialog).getByRole("combobox", { name: "Workspace owner" }));
    await user.click(await screen.findByRole("option", { name: "Default workspace" }));
    const connection = within(dialog).getByRole("combobox", { name: "Data Connection" });
    await user.click(connection);
    await user.click(await screen.findByRole("option", { name: /Warehouse A \(healthy\)/ }));
    await user.click(within(dialog).getByRole("button", { name: "Introspect schema" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The selected extension connection is unavailable.");
    await user.click(within(dialog).getByRole("combobox", { name: "Data Connection" }));
    await user.click(await screen.findByRole("option", { name: /Warehouse B \(healthy\)/ }));

    expect(within(dialog).getByRole("combobox", { name: "Data Connection" })).toHaveTextContent("Warehouse B (healthy)");
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("ignores a deferred refusal from connection A after switching to connection B", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "warehouse-a",
        display_name: "Warehouse A",
        workspace_id: "ws-1",
        workspace_display_name: "Default workspace",
        source_kind: "postgres",
        environment: "dev",
        enabled: true,
        health_status: "healthy",
        query_capable: true,
        linked_dataset_count: 0,
        tags: [],
        connector_ref_jsonb: { connector_id: "extension.install_1.warehouse_sql" },
        connection_config_jsonb: {},
        secret_refs_jsonb: {},
        metadata_jsonb: {},
      },
      {
        connection_id: "warehouse-b",
        display_name: "Warehouse B",
        workspace_id: "ws-1",
        workspace_display_name: "Default workspace",
        source_kind: "postgres",
        environment: "dev",
        enabled: true,
        health_status: "healthy",
        query_capable: true,
        linked_dataset_count: 0,
        tags: [],
        connector_ref_jsonb: { connector_id: "extension.install_1.warehouse_sql" },
        connection_config_jsonb: {},
        secret_refs_jsonb: {},
        metadata_jsonb: {},
      },
    ] as never);
    let finishIntrospection!: (result: unknown) => void;
    mockedIntrospectDataConnection.mockImplementationOnce(
      () => new Promise((resolve) => { finishIntrospection = resolve; }) as never,
    );
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    const dialog = await screen.findByRole("dialog", { name: "Create dataset" });
    await user.click(within(dialog).getByRole("combobox", { name: "Workspace owner" }));
    await user.click(await screen.findByRole("option", { name: "Default workspace" }));
    await user.click(within(dialog).getByRole("combobox", { name: "Data Connection" }));
    await user.click(await screen.findByRole("option", { name: /Warehouse A \(healthy\)/ }));
    await user.click(within(dialog).getByRole("button", { name: "Introspect schema" }));
    await user.click(within(dialog).getByRole("combobox", { name: "Data Connection" }));
    await user.click(await screen.findByRole("option", { name: /Warehouse B \(healthy\)/ }));

    expect(within(dialog).getByRole("button", { name: "Loading schema..." })).toBeDisabled();
    finishIntrospection({
      ok: false,
      result: {},
      errors: [{ code: "connection_unavailable", message: "The selected extension connection is unavailable." }],
    });

    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Introspect schema" })).toBeEnabled());
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("combobox", { name: "Data Connection" })).toHaveTextContent("Warehouse B (healthy)");
  });

  it("ignores a deferred rejection from a closed Create session after reopening the same connection", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    let rejectIntrospection!: (error: Error) => void;
    mockedIntrospectDataConnection.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectIntrospection = reject; }) as never,
    );
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    let dialog = await screen.findByRole("dialog", { name: "Create dataset" });
    expect(within(dialog).getByRole("combobox", { name: "Data Connection" })).toHaveTextContent("Warehouse (healthy)");
    await user.type(within(dialog).getByLabelText("Display Name"), "First session draft");
    await user.click(within(dialog).getByRole("button", { name: "Introspect schema" }));
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    dialog = await screen.findByRole("dialog", { name: "Create dataset" });
    await user.type(within(dialog).getByLabelText("Display Name"), "Second session draft");
    expect(within(dialog).getByLabelText("Display Name")).toHaveValue("Second session draft");

    expect(within(dialog).getByRole("button", { name: "Loading schema..." })).toBeDisabled();
    rejectIntrospection(new Error("Old session transport failure."));

    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Introspect schema" })).toBeEnabled());
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText("Display Name")).toHaveValue("Second session draft");
  });

  it("shows a successful empty schema as an empty result rather than an introspection failure", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedIntrospectDataConnection.mockResolvedValue({
      ok: true,
      result: { schema_snapshot: { resources: [] } },
    } as never);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    const dialog = await screen.findByRole("dialog", { name: "Create dataset" });
    await user.click(within(dialog).getByRole("combobox", { name: "Workspace owner" }));
    await user.click(await screen.findByRole("option", { name: "Default workspace" }));
    const connection = within(dialog).getByRole("combobox", { name: "Data Connection" });
    await user.click(connection);
    await user.click(await screen.findByRole("option", { name: /Warehouse \(healthy\)/ }));
    await user.click(within(dialog).getByRole("button", { name: "Introspect schema" }));

    expect(await screen.findByText("Schema loaded, but no resources were returned.")).toBeInTheDocument();
    expect(within(dialog).getByText("No schema loaded yet")).toBeInTheDocument();
    expect(screen.queryByText("Failed to introspect data connection.")).not.toBeInTheDocument();
    expect(mockedCreateDataset).not.toHaveBeenCalled();
  });

  it("shows a generic error alert when the introspection request rejects", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedIntrospectDataConnection.mockRejectedValue(new Error("Extension runtime diagnostic."));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    const dialog = await screen.findByRole("dialog", { name: "Create dataset" });
    await user.click(within(dialog).getByRole("combobox", { name: "Workspace owner" }));
    await user.click(await screen.findByRole("option", { name: "Default workspace" }));
    const connection = within(dialog).getByRole("combobox", { name: "Data Connection" });
    await user.click(connection);
    await user.click(await screen.findByRole("option", { name: /Warehouse \(healthy\)/ }));
    expect(within(dialog).getByRole("checkbox", { name: /orders/i })).not.toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "Introspect schema" }));

    const error = await within(dialog).findByRole("alert");
    expect(error).toHaveTextContent("Failed to introspect data connection.");
    expect(error).not.toHaveTextContent("Extension runtime diagnostic.");
    expect(within(dialog).getByRole("checkbox", { name: /orders/i })).not.toBeChecked();
    expect(within(dialog).queryByText("No schema loaded yet")).not.toBeInTheDocument();
    expect(mockedCreateDataset).not.toHaveBeenCalled();
  });

  it("does not expose unclassified introspection response diagnostics", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedIntrospectDataConnection.mockResolvedValue({
      ok: false,
      result: {},
      errors: [{ code: "connector_runtime_failed", message: "Runtime diagnostic output." }],
    } as never);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    const dialog = await screen.findByRole("dialog", { name: "Create dataset" });
    await user.click(within(dialog).getByRole("combobox", { name: "Workspace owner" }));
    await user.click(await screen.findByRole("option", { name: "Default workspace" }));
    const connection = within(dialog).getByRole("combobox", { name: "Data Connection" });
    await user.click(connection);
    await user.click(await screen.findByRole("option", { name: /Warehouse \(healthy\)/ }));
    expect(within(dialog).getByRole("checkbox", { name: /orders/i })).not.toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "Introspect schema" }));

    const error = await within(dialog).findByRole("alert");
    expect(error).toHaveTextContent("Failed to introspect data connection.");
    expect(error).not.toHaveTextContent("Runtime diagnostic output.");
    expect(within(dialog).getByRole("checkbox", { name: /orders/i })).not.toBeChecked();
    expect(within(dialog).queryByText("No schema loaded yet")).not.toBeInTheDocument();
    expect(mockedCreateDataset).not.toHaveBeenCalled();
  });

  it("blocks create when dataset display name is blank", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Create dataset" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Dataset display name is required.");
    expect(mockedCreateDataset).not.toHaveBeenCalled();
  });

  it("localizes an empty tag list without translating owner-authored dataset tags", async () => {
    mockedFetchDatasets.mockResolvedValue(DATASET_ROWS as never);
    renderPage("lt");
    await screen.findByRole("link", { name: "Events raw" });
    const table = screen.getByRole("table", { name: "Datasets catalog" });
    expect(within(table).getByRole("cell", { name: "Nėra" })).toBeInTheDocument();
    expect(within(table).queryByRole("cell", { name: "none" })).not.toBeInTheDocument();
    expect(within(table).getByText("orders")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Events raw" })).toHaveAttribute("href", "/registry/datasets/events_raw");
  });

  it("submits only the selected data connection as dataset binding authority", async () => {
    const user = userEvent.setup();
    mockedCreateDataset.mockResolvedValue({ dataset_id: "orders" });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByLabelText("Dataset Connector")).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Display Name"), "Orders");
    // DsCheckbox hides its native input (`position: absolute; opacity: 0;
    // pointer-events: none`) and paints `.fs-checkbox-mark` instead, so the
    // input cannot receive a pointer event. Its label is what a real user
    // clicks, and what forwards the toggle - so the query goes for the label's
    // own text rather than climbing to it from the input.
    expect(within(dialog).getByRole("checkbox", { name: /orders/i })).not.toBeChecked();
    await user.click(within(dialog).getByText("orders"));
    expect(within(dialog).getByRole("checkbox", { name: /orders/i })).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "Create dataset" }));

    expect(mockedCreateDataset).toHaveBeenCalledTimes(1);
    const payload = mockedCreateDataset.mock.calls[0]?.[0];
    expect(payload).toEqual(expect.objectContaining({ connection_id: "warehouse" }));
    expect(payload).not.toEqual(expect.objectContaining({ connector_id: expect.anything() }));
    expect(payload).not.toEqual(expect.objectContaining({ connector_kind: expect.anything() }));
    expect(payload).not.toEqual(expect.objectContaining({ runtime_kind: expect.anything() }));
    expect(payload).not.toEqual(expect.objectContaining({ query_language: expect.anything() }));
  });

  it("explains an empty catalog with the grid config's own copy", async () => {
    renderPage();

    expect(await screen.findByText("No datasets yet")).toBeInTheDocument();
    expect(screen.getByText("Create one using the button above.")).toBeInTheDocument();
  });

  it("narrows the visible rows by search and keeps the toolbar when nothing matches", async () => {
    const user = userEvent.setup();
    mockedFetchDatasets.mockResolvedValue(DATASET_ROWS as never);
    renderPage();

    expect(await screen.findByText("Orders daily")).toBeInTheDocument();
    expect(screen.getByText("Events raw")).toBeInTheDocument();

    const searchField = screen.getByRole("searchbox", { name: "Search datasets" });
    await user.type(searchField, "orders");

    await waitFor(() => expect(screen.queryByText("Events raw")).not.toBeInTheDocument());
    expect(screen.getByText("Orders daily")).toBeInTheDocument();

    await user.clear(searchField);
    await user.type(searchField, "nothing matches this");

    // Canon: keep the filter controls visible and explain the empty result
    // inside the table.
    expect(await screen.findByText("No datasets match these filters")).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search datasets" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add filter" })).toBeInTheDocument();
  });

  it("narrows the visible rows by the status filter and restores them when cleared", async () => {
    const user = userEvent.setup();
    mockedFetchDatasets.mockResolvedValue(DATASET_ROWS as never);
    renderPage();

    await screen.findByText("Orders daily");

    await user.click(screen.getByRole("button", { name: "Add filter" }));
    await user.click(await screen.findByRole("menuitem", { name: "Status" }));
    await user.click(await screen.findByRole("menuitem", { name: "Disabled" }));

    await waitFor(() => expect(screen.queryByText("Orders daily")).not.toBeInTheDocument());
    expect(screen.getByText("Events raw")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear all" }));
    expect(await screen.findByText("Orders daily")).toBeInTheDocument();
  });

  it("closes the create dialog on Escape", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    expect(await screen.findByRole("dialog", { name: "Create dataset" })).toBeInTheDocument();

    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("asks for delete confirmation in a dialog rather than the browser", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(globalThis, "confirm");
    mockedFetchDatasets.mockResolvedValue(DATASET_ROWS as never);
    renderPage();

    await screen.findByText("Orders daily");
    await user.click(screen.getByRole("button", { name: "Actions for orders_daily" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    const confirmDialog = await screen.findByRole("alertdialog");
    expect(confirmDialog).toHaveAccessibleName("Delete orders_daily");
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(mockedDeleteDataset).not.toHaveBeenCalled();

    await user.click(within(confirmDialog).getByRole("button", { name: "Delete" }));
    // react-query hands the mutation context in as a second argument, so the
    // dataset id is read positionally rather than through toHaveBeenCalledWith.
    await waitFor(() => expect(mockedDeleteDataset).toHaveBeenCalled());
    expect(mockedDeleteDataset.mock.calls[0]?.[0]).toBe("orders_daily");
    confirmSpy.mockRestore();
  });

  it("explains recoverable deletion before moving a dataset out of the catalog", async () => {
    const user = userEvent.setup();
    mockedFetchDatasets
      .mockResolvedValueOnce(DATASET_ROWS as never)
      .mockResolvedValue([DATASET_ROWS[1]] as never);
    mockedDeleteDataset.mockResolvedValue({ status: "deleted", dataset_id: "orders_daily" });
    renderPage();

    await screen.findByText("Orders daily");
    await user.click(screen.getByRole("button", { name: "Actions for orders_daily" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    const dialog = await screen.findByRole("alertdialog", { name: "Delete orders_daily" });
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/move.*orders_daily.*Trash/i);
    expect(dialog).toHaveTextContent(/restore.*Trash/i);
    expect(dialog).not.toHaveTextContent(/permanent/i);
    expect(mockedDeleteDataset).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.getByText("Orders daily")).toBeInTheDocument();
    expect(mockedDeleteDataset).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Actions for orders_daily" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete" }),
    );

    expect(await screen.findByText("Dataset moved to Trash.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Orders daily")).not.toBeInTheDocument());
    expect(screen.getByText("Events raw")).toBeInTheDocument();
    expect(mockedDeleteDataset.mock.calls).toHaveLength(1);
    expect(mockedDeleteDataset.mock.calls[0]?.[0]).toBe("orders_daily");
  });

  it("offers inspection only for package datasets and preserves ordinary read-only-data edits", async () => {
    const ordinary = { ...DATASET_ROWS[0], availability_mode: "specific_projects_only" as const, dataset_id: "extension.user.orders", workspace_id: "", metadata_jsonb: { connection_resolution: "project_scope" as const } };
    const packaged = { ...ordinary, dataset_id: "extension.install.orders", display_name: "Package orders", source_install_id: "install", source_release_id: "release" };
    mockedFetchDatasets.mockResolvedValue([packaged, ordinary]);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Actions for extension.install.orders" }));
    expect.soft(screen.queryByRole("menuitem", { name: "View" })).toBeInTheDocument();
    for (const name of ["Edit", "Disable", "Delete"]) expect.soft(screen.queryByRole("menuitem", { name })).not.toBeInTheDocument();
    expect.soft(screen.getByRole("link", { name: "Package orders" })).toHaveAttribute("href", "/registry/datasets/extension.install.orders");
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "Actions for extension.user.orders" }));
    for (const name of ["Edit", "Disable", "Delete"]) expect.soft(screen.getByRole("menuitem", { name })).toBeEnabled();
    expect.soft(mockedDeleteDataset).not.toHaveBeenCalled();
  });

});
