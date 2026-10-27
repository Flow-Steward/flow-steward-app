import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router";

import {
  createDatasetQueryTemplate,
  deleteDatasetQueryTemplate,
  fetchDataConnections,
  fetchDataset,
  fetchDatasetConnectors,
  fetchDatasetProjectCatalog,
  fetchDatasetQueryTemplates,
  introspectDataConnection,
  previewDatasetQueryTemplate,
  updateDataset,
  validateDatasetQueryTemplate,
} from "@/pages/registry/datasets/api";
import { RegistryDatasetsDetailPage } from "@/pages/registry/datasets/detail";

vi.mock("@/pages/registry/datasets/api", () => ({
  createDatasetQueryTemplate: vi.fn(),
  deleteDatasetQueryTemplate: vi.fn(),
  fetchDataConnections: vi.fn(),
  fetchDataset: vi.fn(),
  fetchDatasetConnectors: vi.fn(),
  fetchDatasetProjectCatalog: vi.fn(),
  fetchDatasetQueryTemplates: vi.fn(),
  introspectDataConnection: vi.fn(),
  previewDatasetQueryTemplate: vi.fn(),
  toggleDataset: vi.fn(),
  toggleDatasetQueryTemplate: vi.fn(),
  updateDataset: vi.fn(),
  updateDatasetProjects: vi.fn(),
  updateDatasetQueryTemplate: vi.fn(),
  validateDatasetQueryTemplate: vi.fn(),
}));

const mockedCreateDatasetQueryTemplate = vi.mocked(createDatasetQueryTemplate);
const mockedDeleteDatasetQueryTemplate = vi.mocked(deleteDatasetQueryTemplate);
const mockedFetchDataConnections = vi.mocked(fetchDataConnections);
const mockedFetchDataset = vi.mocked(fetchDataset);
const mockedFetchDatasetConnectors = vi.mocked(fetchDatasetConnectors);
const mockedFetchDatasetProjectCatalog = vi.mocked(fetchDatasetProjectCatalog);
const mockedFetchDatasetQueryTemplates = vi.mocked(fetchDatasetQueryTemplates);
const mockedIntrospectDataConnection = vi.mocked(introspectDataConnection);
const mockedPreviewDatasetQueryTemplate = vi.mocked(
  previewDatasetQueryTemplate,
);
const mockedUpdateDataset = vi.mocked(updateDataset);
const mockedValidateDatasetQueryTemplate = vi.mocked(
  validateDatasetQueryTemplate,
);

/** The active section lives in the URL, so the test has to be able to read it. */
function LocationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <span data-testid="location">{location.pathname + location.search}</span>
      <button onClick={() => ignoreAsyncResult(navigate("/registry/datasets/another-dataset"))} type="button">
        Open another dataset
      </button>
    </>
  );
}

function renderPage(entry = "/registry/datasets/events") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <LocationProbe />
        <Routes>
          <Route
            element={<RegistryDatasetsDetailPage />}
            path="/registry/datasets/:datasetId"
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The five inline row buttons became one row-action menu (spec decision). */
async function openRowMenu(queryName: string) {
  await userEvent.click(screen.getByRole("button", { name: `Actions for ${queryName}` }));
}

async function clickRowAction(queryName: string, action: string) {
  await openRowMenu(queryName);
  await userEvent.click(await screen.findByRole("menuitem", { name: action }));
}

describe("RegistryDatasetsDetailPage saved queries", () => {
  beforeEach(() => {
    mockedCreateDatasetQueryTemplate.mockReset();
    mockedDeleteDatasetQueryTemplate.mockReset();
    mockedFetchDataConnections.mockReset();
    mockedFetchDataset.mockReset();
    mockedFetchDatasetConnectors.mockReset();
    mockedFetchDatasetProjectCatalog.mockReset();
    mockedFetchDatasetQueryTemplates.mockReset();
    mockedIntrospectDataConnection.mockReset();
    mockedPreviewDatasetQueryTemplate.mockReset();
    mockedUpdateDataset.mockReset();
    mockedValidateDatasetQueryTemplate.mockReset();

    mockedFetchDataset.mockResolvedValue({
      dataset_id: "events",
      workspace_id: "workspace-1",
      display_name: "Events",
      availability_mode: "all_workspace_projects",
      connector_kind: "extension",
      runtime_kind: "local_extension",
      query_language: "sql",
      read_only: true,
      enabled: true,
    });
    mockedFetchDatasetConnectors.mockResolvedValue([]);
    mockedFetchDataConnections.mockResolvedValue([]);
    mockedFetchDatasetProjectCatalog.mockResolvedValue({
      dataset_id: "events",
      workspace_id: "workspace-1",
      availability_mode: "all_workspace_projects",
      project_ids: [],
      explicit_project_ids: [],
      available_projects: [],
    });
    mockedFetchDatasetQueryTemplates.mockResolvedValue([]);
    mockedValidateDatasetQueryTemplate.mockResolvedValue({ ok: true });
    mockedUpdateDataset.mockResolvedValue({});
    mockedDeleteDatasetQueryTemplate.mockResolvedValue({});
  });

  it("does not run a saved query before dataset connection metadata has loaded", async () => {
    projectScopedQuery();
    mockedFetchDataset.mockReturnValue(new Promise(() => {}));
    renderPage("/registry/datasets/events?tab=saved-queries");
    await screen.findByText("Project query");
    await clickRowAction("Project query", "Preview");
    const dialog = within(screen.getByRole("dialog", { name: "Project query" }));
    const run = dialog.getByRole("button", { name: "Run preview" });
    expect(run).toBeDisabled();
    await userEvent.click(run);
    expect(mockedPreviewDatasetQueryTemplate).not.toHaveBeenCalled();
    expect(mockedValidateDatasetQueryTemplate).not.toHaveBeenCalled();
  });

  function projectScopedQuery() {
    mockedFetchDataset.mockResolvedValue({
      dataset_id: "events", workspace_id: "", display_name: "Events",
      availability_mode: "specific_projects_only", connector_kind: "extension",
      runtime_kind: "local_extension", query_language: "sql", read_only: true, enabled: true,
      metadata_jsonb: { connection_resolution: "project_scope" },
    });
    mockedFetchDatasetProjectCatalog.mockResolvedValue({
      dataset_id: "events", workspace_id: "", availability_mode: "specific_projects_only",
      project_ids: ["project-alpha"], explicit_project_ids: ["project-alpha"],
      available_projects: [{ id: "project-alpha", name: "Alpha project" }, { id: "project-unassigned", name: "Unassigned project" }],
    });
    mockedFetchDatasetQueryTemplates.mockResolvedValue([{
      query_template_id: "project-query", dataset_id: "events", display_name: "Project query", query_language: "sql",
      query_definition_jsonb: { sql: "SELECT 1" }, params_schema_jsonb: {}, output_schema_jsonb: {}, enabled: true, version: 1,
    }]);
    mockedPreviewDatasetQueryTemplate.mockResolvedValue({ ok: true, result: { rows: [], row_count: 0, output_schema: {} } });
  }

  it.each([
    ["Preview", "Run preview"], ["Validate", "Run validation"],
  ])("requires an explicit assigned project for project-scoped %s even when only one is eligible", async (action, command) => {
    projectScopedQuery();
    const user = userEvent.setup();
    renderPage("/registry/datasets/events?tab=saved-queries");
    await screen.findByText("Project query");
    await clickRowAction("Project query", action);
    const dialog = within(screen.getByRole("dialog", { name: "Project query" }));
    const project = dialog.getByRole("combobox", { name: "Project" });
    expect(project).toHaveTextContent("Select a project");
    const run = dialog.getByRole("button", { name: command });
    expect(run).toBeDisabled();
    await user.click(run);
    expect(mockedPreviewDatasetQueryTemplate).not.toHaveBeenCalled();
    expect(mockedValidateDatasetQueryTemplate).not.toHaveBeenCalled();
    await user.click(project);
    expect(screen.queryByRole("option", { name: "Unassigned project" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("option", { name: "Alpha project" }));
    expect(run).toBeEnabled();
    await user.click(run);
    await waitFor(() => {
      if (action === "Preview") expect(mockedPreviewDatasetQueryTemplate).toHaveBeenCalledWith("project-query", {}, 100, "project-alpha");
      else expect(mockedValidateDatasetQueryTemplate).toHaveBeenCalledWith("project-query", {}, "project-alpha");
    });
  });

  it("starts each project-scoped query dialog without reusing a previous project choice", async () => {
    projectScopedQuery();
    const user = userEvent.setup();
    renderPage("/registry/datasets/events?tab=saved-queries");
    await screen.findByText("Project query");
    await clickRowAction("Project query", "Preview");
    let dialog = within(screen.getByRole("dialog", { name: "Project query" }));
    await user.click(dialog.getByRole("combobox", { name: "Project" }));
    await user.click(await screen.findByRole("option", { name: "Alpha project" }));
    expect(dialog.getByRole("button", { name: "Run preview" })).toBeEnabled();
    await user.click(dialog.getByRole("button", { name: "Cancel" }));
    await clickRowAction("Project query", "Validate");
    dialog = within(screen.getByRole("dialog", { name: "Project query" }));
    expect(dialog.getByRole("combobox", { name: "Project" })).toHaveTextContent("Select a project");
    expect(dialog.getByRole("button", { name: "Run validation" })).toBeDisabled();
    expect(mockedPreviewDatasetQueryTemplate).not.toHaveBeenCalled();
    expect(mockedValidateDatasetQueryTemplate).not.toHaveBeenCalled();
  });

  it.each(["pending", "failed", "unassigned"])("does not run a project-scoped query when the assigned project catalog is %s", async (state) => {
    projectScopedQuery();
    if (state === "pending") mockedFetchDatasetProjectCatalog.mockReturnValue(new Promise(() => {}));
    else if (state === "failed") mockedFetchDatasetProjectCatalog.mockRejectedValue(new Error("Catalog unavailable"));
    else mockedFetchDatasetProjectCatalog.mockResolvedValue({ dataset_id: "events", workspace_id: "", availability_mode: "specific_projects_only", project_ids: [], available_projects: [{ id: "project-alpha", name: "Alpha project" }] });
    renderPage("/registry/datasets/events?tab=saved-queries");
    await screen.findByText("Project query");
    await clickRowAction("Project query", "Preview");
    const dialog = within(screen.getByRole("dialog", { name: "Project query" }));
    expect(dialog.getByRole("combobox", { name: "Project" })).toBeDisabled();
    expect(dialog.getByRole("button", { name: "Run preview" })).toBeDisabled();
    if (state === "failed") expect(dialog.getByText("Failed to load projects.")).toBeInTheDocument();
    if (state === "unassigned") {
      expect(dialog.getByText("Link this dataset to a project from the project's Datasets page before running the query.")).toBeInTheDocument();
      expect(dialog.getByRole("link", { name: "Projects" })).toHaveAttribute("href", "/projects");
    }
    expect(mockedPreviewDatasetQueryTemplate).not.toHaveBeenCalled();
    expect(mockedValidateDatasetQueryTemplate).not.toHaveBeenCalled();
  });

  it("does not allow previewing a disabled saved query", async () => {
    mockedFetchDatasetQueryTemplates.mockResolvedValue([
      {
        query_template_id: "query-disabled",
        dataset_id: "events",
        display_name: "Disabled query",
        query_language: "sql",
        query_definition_jsonb: { sql: "SELECT 1" },
        params_schema_jsonb: {},
        output_schema_jsonb: {},
        enabled: false,
        version: 1,
      },
    ]);

    renderPage();
    await userEvent.click(await screen.findByRole("tab", { name: "Saved queries" }));

    await openRowMenu("Disabled query");
    expect(await screen.findByRole("menuitem", { name: "Preview" })).toBeDisabled();
    expect(screen.getByRole("menuitem", { name: "Validate" })).toBeDisabled();
  });

  it("requires an explicit rebind instead of adopting the only connection", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([
      {
        connector_id: "connector.sql",
        display_name: "SQL",
        connector_kind: "extension",
        runtime_kind: "local_extension",
        query_language: "sql",
      },
    ]);
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "connection-a",
        display_name: "Connection A",
        workspace_id: "workspace-1",
        source_kind: "extension",
        environment: "dev",
        enabled: true,
        health_status: "healthy",
        query_capable: true,
        linked_dataset_count: 0,
        connector_ref_jsonb: { connector_id: "connector.sql" },
      },
    ]);

    renderPage();

    const connectionSelect = await screen.findByLabelText("Data Connection");
    await waitFor(() => expect(connectionSelect).toHaveValue(""));
    expect(
      screen.getByText("This dataset needs to be rebound to a query-capable data connection."),
    ).toBeInTheDocument();
    expect(mockedUpdateDataset).not.toHaveBeenCalled();
  });

  it("requires confirmation before deleting a saved query", async () => {
    const confirmSpy = vi.spyOn(globalThis, "confirm").mockReturnValue(false);
    mockedFetchDatasetQueryTemplates.mockResolvedValue([
      {
        query_template_id: "query-delete",
        dataset_id: "events",
        display_name: "Delete me",
        query_language: "sql",
        query_definition_jsonb: { sql: "SELECT 1" },
        params_schema_jsonb: {},
        output_schema_jsonb: {},
        enabled: true,
        version: 1,
      },
    ]);

    renderPage();
    await userEvent.click(await screen.findByRole("tab", { name: "Saved queries" }));
    await clickRowAction("Delete me", "Delete");

    // The browser confirm is gone: a real alertdialog asks instead, and
    // cancelling it leaves the mutation uncalled.
    const confirmDialog = await screen.findByRole("alertdialog");
    expect(confirmDialog).toHaveAccessibleName('Delete saved query "Delete me"');
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(mockedDeleteDatasetQueryTemplate).not.toHaveBeenCalled();

    await userEvent.click(within(confirmDialog).getByRole("button", { name: "Cancel" }));
    expect(mockedDeleteDatasetQueryTemplate).not.toHaveBeenCalled();

    await clickRowAction("Delete me", "Delete");
    await userEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete" }),
    );
    await waitFor(() => expect(mockedDeleteDatasetQueryTemplate).toHaveBeenCalled());
    expect(mockedDeleteDatasetQueryTemplate.mock.calls[0]?.[0]).toBe("query-delete");
    confirmSpy.mockRestore();
  });

  it("shows the connector SQL diagnostic instead of a generic preview error", async () => {
    mockedFetchDatasetQueryTemplates.mockResolvedValue([
      {
        query_template_id: "query-invalid",
        dataset_id: "events",
        display_name: "Invalid SQL",
        query_language: "sql",
        query_definition_jsonb: { sql: "SELECT FROM" },
        params_schema_jsonb: {},
        output_schema_jsonb: {},
        enabled: true,
        version: 1,
      },
    ]);
    mockedPreviewDatasetQueryTemplate.mockResolvedValue({
      ok: false,
      errors: [
        { code: "invalid_sql", message: 'syntax error at or near "FROM"' },
      ],
    });

    renderPage();
    await userEvent.click(await screen.findByRole("tab", { name: "Saved queries" }));
    await clickRowAction("Invalid SQL", "Preview");
    await userEvent.click(
      within(screen.getByRole("dialog", { name: "Invalid SQL" })).getByRole(
        "button",
        { name: "Run preview" },
      ),
    );

    expect(
      (await screen.findAllByText('syntax error at or near "FROM"')).length,
    ).toBeGreaterThan(0);
  });

  it("cannot save a selected resource from the previous data connection", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchDataset.mockResolvedValue({
      dataset_id: "events",
      workspace_id: "workspace-1",
      display_name: "Events",
      availability_mode: "all_workspace_projects",
      connector_kind: "extension",
      connector_id: "connector.sql",
      runtime_kind: "local_extension",
      query_language: "sql",
      read_only: true,
      enabled: true,
      metadata_jsonb: { connection_id: "connection-a" },
      schema_snapshot_jsonb: {
        resources: [
          {
            resource_id: "schema_a.events",
            columns: [{ name: "id", type: "integer" }],
          },
        ],
      },
      resource_scope_jsonb: {
        resources: [{ resource_id: "schema_a.events", columns: ["id"] }],
      },
    });
    mockedFetchDatasetConnectors.mockResolvedValue([
      {
        connector_id: "connector.sql",
        display_name: "SQL",
        connector_kind: "extension",
        runtime_kind: "local_extension",
        query_language: "sql",
      },
    ]);
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "connection-a",
        display_name: "Connection A",
        workspace_id: "workspace-1",
        source_kind: "extension",
        environment: "dev",
        enabled: true,
        health_status: "healthy",
        query_capable: true,
        linked_dataset_count: 1,
        connector_ref_jsonb: { connector_id: "connector.sql" },
        metadata_jsonb: {
          last_schema_snapshot: {
            resources: [
              {
                resource_id: "schema_a.events",
                columns: [{ name: "id", type: "integer" }],
              },
            ],
          },
        },
      },
      {
        connection_id: "connection-b",
        display_name: "Connection B",
        workspace_id: "workspace-1",
        source_kind: "extension",
        environment: "dev",
        enabled: true,
        health_status: "healthy",
        query_capable: true,
        linked_dataset_count: 0,
        connector_ref_jsonb: { connector_id: "connector.sql" },
        metadata_jsonb: {
          last_schema_snapshot: {
            resources: [
              {
                resource_id: "schema_b.events",
                columns: [{ name: "id", type: "integer" }],
              },
            ],
          },
        },
      },
    ]);

    renderPage();

    // Kept from main: a60c0815a deleted the Dataset Connector select, and
    // nothing in the transfer may bring it back.
    expect(screen.queryByLabelText("Dataset Connector")).not.toBeInTheDocument();
    const connectionSelect = await screen.findByRole("combobox", { name: "Data Connection" });
    await waitFor(() => expect(connectionSelect).toHaveTextContent("Connection A"));
    await screen.findByText("schema_a.events");
    expect(
      screen.getByRole("checkbox", { name: /schema_a\.events/i }),
    ).toBeChecked();

    await user.click(connectionSelect);
    await user.click(await screen.findByRole("option", { name: /Connection B/ }));

    expect(screen.queryByText("schema_a.events")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save dataset" })).toBeDisabled();
    await screen.findByText("schema_b.events");
    await user.click(
      screen.getByRole("checkbox", { name: /schema_b\.events/i }),
    );
    await user.click(screen.getByRole("button", { name: "Save dataset" }));

    await waitFor(() => expect(mockedUpdateDataset).toHaveBeenCalledTimes(1));
    expect(mockedUpdateDataset).toHaveBeenCalledWith(
      "events",
      expect.objectContaining({
        connection_id: "connection-b",
        resource_scope_jsonb: {
          version: 1,
          resources: [{ resource_id: "schema_b.events", columns: null }],
        },
      }),
    );
    expect(mockedUpdateDataset.mock.calls[0]?.[1]).not.toEqual(
      expect.objectContaining({
        connector_id: expect.anything(),
      }),
    );
    expect(mockedUpdateDataset.mock.calls[0]?.[1]).not.toEqual(
      expect.objectContaining({
        resource_scope_jsonb: expect.objectContaining({
          resources: expect.arrayContaining([
            expect.objectContaining({ resource_id: "schema_a.events" }),
          ]),
        }),
      }),
    );
  });

  it("preserves the saved schema and current dataset draft when introspection returns an application refusal", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchDataset.mockResolvedValue({
      dataset_id: "events",
      workspace_id: "workspace-1",
      display_name: "Events",
      description: "Existing dataset notes",
      availability_mode: "all_workspace_projects",
      connector_kind: "extension",
      connector_id: "connector.sql",
      runtime_kind: "local_extension",
      query_language: "sql",
      read_only: true,
      enabled: true,
      metadata_jsonb: { connection_id: "connection-a" },
      schema_snapshot_jsonb: {
        resources: [{ resource_id: "customer_records", columns: [{ name: "record_id", type: "integer" }] }],
      },
      resource_scope_jsonb: {
        resources: [{ resource_id: "customer_records", columns: ["record_id"] }],
      },
    });
    mockedFetchDatasetConnectors.mockResolvedValue([
      { connector_id: "connector.sql", display_name: "SQL", connector_kind: "database", runtime_kind: "local_extension", query_language: "sql" },
    ] as never);
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "connection-a",
        display_name: "Connection A",
        workspace_id: "workspace-1",
        source_kind: "database",
        environment: "dev",
        enabled: true,
        health_status: "healthy",
        query_capable: true,
        linked_dataset_count: 1,
        connector_ref_jsonb: { connector_id: "connector.sql" },
        metadata_jsonb: {},
      },
    ] as never);
    mockedIntrospectDataConnection.mockResolvedValue({
      ok: false,
      result: {},
      errors: [{ code: "connection_unavailable", message: "The selected extension connection is unavailable." }],
    } as never);

    renderPage();

    const selectedResource = await screen.findByRole("checkbox", { name: /customer_records/i });
    await waitFor(() => expect(selectedResource).toBeChecked());
    const name = screen.getByLabelText("Display Name");
    await user.clear(name);
    await user.type(name, "Current dataset draft");
    const connectionSelect = screen.getByRole("combobox", { name: "Data Connection" });
    await waitFor(() => expect(connectionSelect).toHaveTextContent("Connection A"));
    await user.click(screen.getByRole("button", { name: "Introspect schema" }));

    const refusal = await screen.findByRole("alert");
    expect(refusal).toHaveTextContent("The selected extension connection is unavailable.");
    expect(screen.getByRole("checkbox", { name: /customer_records/i })).toBeChecked();
    expect(screen.getByLabelText("Display Name")).toHaveValue("Current dataset draft");
    expect(screen.getByRole("combobox", { name: "Data Connection" })).toHaveTextContent("Connection A");
    expect(screen.queryByText("Schema refreshed, but no resources were returned.")).not.toBeInTheDocument();
    expect(mockedUpdateDataset).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Open another dataset" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/registry/datasets/another-dataset"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("saves a query, refreshes its list, and explains parameter and output schemas", async () => {
    const user = userEvent.setup();
    mockedCreateDatasetQueryTemplate.mockResolvedValue({
      query_template_id: "query-1",
    });

    renderPage();
    await user.click(
      await screen.findByRole("tab", { name: "Saved queries" }),
    );
    await user.click(screen.getByRole("button", { name: "Add query" }));

    const dialog = screen.getByRole("dialog", { name: "Add query" });
    expect(
      within(dialog).getByText("Use :name in SQL for a named parameter."),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("heading", { name: "Input parameters" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Select the dataset columns returned by this query. Their types come from the connection introspection.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText("SQL query")).toHaveAttribute(
      "placeholder",
      expect.stringContaining(":from_date"),
    );
    await user.type(
      within(dialog).getByLabelText("Query name"),
      "Recent events",
    );
    await user.type(
      within(dialog).getByLabelText("SQL query"),
      "SELECT * FROM events",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Save query" }),
    );

    await waitFor(() =>
      expect(mockedCreateDatasetQueryTemplate).toHaveBeenCalledWith(
        expect.objectContaining({
          dataset_id: "events",
          display_name: "Recent events",
          query_definition_jsonb: { sql: "SELECT * FROM events" },
          params_schema_jsonb: {},
          output_schema_jsonb: {},
        }),
      ),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Add query" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Saved query created.")).toBeInTheDocument();
    expect(mockedFetchDatasetQueryTemplates).toHaveBeenCalledTimes(2);
  });

  it("shows a save error in the query form", async () => {
    const user = userEvent.setup();
    mockedCreateDatasetQueryTemplate.mockRejectedValue(
      new Error("Query name is already in use."),
    );

    renderPage();
    await user.click(
      await screen.findByRole("tab", { name: "Saved queries" }),
    );
    await user.click(screen.getByRole("button", { name: "Add query" }));

    const dialog = screen.getByRole("dialog", { name: "Add query" });
    await user.type(
      within(dialog).getByLabelText("Query name"),
      "Recent events",
    );
    await user.type(
      within(dialog).getByLabelText("SQL query"),
      "SELECT * FROM events",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Save query" }),
    );

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Query name is already in use.",
    );
  });

  it("sends preview parameter values from the preview dialog", async () => {
    const user = userEvent.setup();
    mockedFetchDatasetQueryTemplates.mockResolvedValue([
      {
        query_template_id: "query-events-by-name",
        dataset_id: "events",
        display_name: "Events by name",
        query_language: "sql",
        query_definition_jsonb: {
          sql: "SELECT * FROM event WHERE event = :event",
        },
        params_schema_jsonb: {
          type: "object",
          properties: { event: { type: "string" } },
          required: ["event"],
        },
        output_schema_jsonb: {},
        enabled: true,
        version: 1,
      },
    ]);
    mockedPreviewDatasetQueryTemplate.mockResolvedValue({
      ok: true,
      result: { rows: [], row_count: 0, output_schema: {} },
    });

    renderPage();
    await user.click(
      await screen.findByRole("tab", { name: "Saved queries" }),
    );
    await clickRowAction("Events by name", "Preview");

    const dialog = screen.getByRole("dialog", { name: "Events by name" });
    const paramsInput = within(dialog).getByLabelText("Preview parameters");
    expect(paramsInput).toHaveValue('{\n  "event": "example"\n}');
    await user.clear(paramsInput);
    await user.click(paramsInput);
    await user.paste('{"event":"purchase"}');
    await user.click(
      within(dialog).getByRole("button", { name: "Run preview" }),
    );

    await waitFor(() =>
      expect(mockedPreviewDatasetQueryTemplate).toHaveBeenCalledWith(
        "query-events-by-name",
        { event: "purchase" },
      ),
    );
  });

  it("selects all output columns by default and lets users clear or restore the selection", async () => {
    const user = userEvent.setup();
    mockedCreateDatasetQueryTemplate.mockResolvedValue({
      query_template_id: "query-typed",
    });
    mockedFetchDataset.mockResolvedValue({
      dataset_id: "events",
      workspace_id: "workspace-1",
      display_name: "Events",
      availability_mode: "all_workspace_projects",
      connector_kind: "extension",
      runtime_kind: "local_extension",
      query_language: "sql",
      read_only: true,
      enabled: true,
      schema_snapshot_jsonb: {
        resources: [
          {
            resource_id: "event",
            columns: [
              { name: "event", value_type: "string" },
              { name: "eventStart", value_type: "datetime" },
            ],
          },
        ],
      },
    });

    renderPage();
    await user.click(
      await screen.findByRole("tab", { name: "Saved queries" }),
    );
    await user.click(screen.getByRole("button", { name: "Add query" }));

    const dialog = screen.getByRole("dialog", { name: "Add query" });
    await user.type(
      within(dialog).getByLabelText("Query name"),
      "Events by type",
    );
    await user.type(
      within(dialog).getByLabelText("SQL query"),
      "SELECT eventStart FROM event WHERE event = :event",
    );
    await user.click(within(dialog).getByRole("combobox", { name: "Parameter type for event" }));
    await user.click(await screen.findByRole("option", { name: "Whole number" }));
    const eventOutput = await within(dialog).findByLabelText("event.event");
    const eventStartOutput = within(dialog).getByLabelText("event.eventStart");
    expect(eventOutput).toBeChecked();
    expect(eventStartOutput).toBeChecked();
    await user.click(
      within(dialog).getByRole("button", { name: "Clear selection" }),
    );
    expect(eventOutput).not.toBeChecked();
    expect(eventStartOutput).not.toBeChecked();
    await user.click(
      within(dialog).getByRole("button", { name: "Select all" }),
    );
    expect(eventOutput).toBeChecked();
    expect(eventStartOutput).toBeChecked();
    await user.click(
      within(dialog).getByRole("button", { name: "Save query" }),
    );

    await waitFor(() =>
      expect(mockedCreateDatasetQueryTemplate).toHaveBeenCalledWith(
        expect.objectContaining({
          params_schema_jsonb: {
            type: "object",
            properties: { event: { type: "integer" } },
            required: ["event"],
          },
          output_schema_jsonb: {
            type: "object",
            properties: {
              event: { type: "string" },
              eventStart: { type: "string", format: "date-time" },
            },
          },
        }),
      ),
    );
  });

  it("collects sample parameters before validating a parameterized saved query", async () => {
    const user = userEvent.setup();
    mockedFetchDatasetQueryTemplates.mockResolvedValue([
      {
        query_template_id: "events-disabled",
        dataset_id: "events",
        display_name: "Events by disabled state",
        query_language: "sql",
        query_definition_jsonb: {
          sql: "SELECT * FROM events WHERE disabled = :disabled",
        },
        params_schema_jsonb: {
          type: "object",
          properties: { disabled: { type: "boolean" } },
          required: ["disabled"],
        },
        output_schema_jsonb: {},
        enabled: true,
        version: 1,
      },
    ]);

    renderPage();
    await user.click(
      await screen.findByRole("tab", { name: "Saved queries" }),
    );
    await clickRowAction("Events by disabled state", "Validate");

    const dialog = await screen.findByRole("dialog", {
      name: "Events by disabled state",
    });
    fireEvent.change(within(dialog).getByLabelText("Validation parameters"), {
      target: { value: '{"disabled":false}' },
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Run validation" }),
    );

    await waitFor(() => {
      expect(mockedValidateDatasetQueryTemplate).toHaveBeenCalledWith(
        "events-disabled",
        { disabled: false },
      );
    });
  });
  it("takes the active section from the URL and writes it back", async () => {
    const user = userEvent.setup();
    mockedFetchDatasetQueryTemplates.mockResolvedValue([]);
    renderPage("/registry/datasets/events?tab=saved-queries");

    // Deep-linked: the saved-queries tab is selected on first render.
    expect(await screen.findByRole("tab", { name: "Saved queries" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);

    await user.click(screen.getByRole("tab", { name: "Dataset scope" }));
    // The default section writes no query parameter at all.
    expect(screen.getByTestId("location")).toHaveTextContent("/registry/datasets/events");
    expect(screen.getByTestId("location")).not.toHaveTextContent("tab=");

    await user.click(screen.getByRole("tab", { name: "Saved queries" }));
    expect(screen.getByTestId("location")).toHaveTextContent("?tab=saved-queries");
  });

  it("moves between tabs with the arrow keys", async () => {
    const user = userEvent.setup();
    mockedFetchDatasetQueryTemplates.mockResolvedValue([]);
    renderPage();

    const datasetTab = await screen.findByRole("tab", { name: "Dataset scope" });
    datasetTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Saved queries" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Dataset scope" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("explains an empty saved-queries list", async () => {
    mockedFetchDatasetQueryTemplates.mockResolvedValue([]);
    renderPage("/registry/datasets/events?tab=saved-queries");

    expect(await screen.findByText("No saved queries yet")).toBeInTheDocument();
    expect(
      screen.getByText("Add one to reference this dataset from a dataset_query workflow step."),
    ).toBeInTheDocument();
  });

  it("keeps Workspace owner readOnly rather than disabled", async () => {
    renderPage();

    // `disabled` would drop the value out of the accessibility tree and out of
    // the form; `readOnly` keeps both.
    const owner = await screen.findByDisplayValue("workspace-1");
    expect(owner).toHaveAttribute("readonly");
    expect(owner).toBeEnabled();
  });

  it("closes each dialog on Escape", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchDatasetQueryTemplates.mockResolvedValue([
      {
        query_template_id: "query-escape",
        dataset_id: "events",
        display_name: "Escape me",
        query_language: "sql",
        query_definition_jsonb: { sql: "SELECT 1" },
        params_schema_jsonb: {},
        output_schema_jsonb: {},
        enabled: true,
        version: 1,
      },
    ]);
    renderPage("/registry/datasets/events?tab=saved-queries");

    await user.click(await screen.findByRole("button", { name: "Add query" }));
    expect(await screen.findByRole("dialog", { name: "Add query" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await clickRowAction("Escape me", "Preview");
    expect(await screen.findByRole("dialog", { name: "Escape me" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await clickRowAction("Escape me", "Delete");
    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("shows package configuration and query SQL read-only while ordinary queries remain editable", async () => {
    projectScopedQuery();
    const dataset = { dataset_id: "events", workspace_id: "", display_name: "Package orders", availability_mode: "specific_projects_only" as const, connector_kind: "extension", runtime_kind: "local_extension", query_language: "sql", read_only: true, enabled: true, source_install_id: "install", source_release_id: "release", metadata_jsonb: { connection_resolution: "project_scope" as const }, schema_snapshot_jsonb: { resources: [{ resource_id: "orders", columns: [{ name: "order_id" }] }] }, resource_scope_jsonb: { resources: [{ resource_id: "orders", columns: ["order_id"] }] } };
    mockedFetchDataset.mockResolvedValue(dataset);
    const query = { query_template_id: "package-query", dataset_id: "events", display_name: "Package query", query_language: "sql", query_definition_jsonb: { sql: "SELECT order_id FROM orders" }, enabled: true, version: 1, source_install_id: "install", source_release_id: "release" };
    mockedFetchDatasetQueryTemplates.mockResolvedValue([query, { ...query, query_template_id: "user-query", display_name: "User query", source_install_id: undefined, source_release_id: undefined }]);
    renderPage();
    await screen.findByRole("heading", { name: "Package orders" });
    expect.soft(screen.queryByText(/provided by an installed extension/i)).toBeInTheDocument();
    expect.soft(screen.queryByText(/no permanent workspace/i)).toBeInTheDocument();
    expect.soft(screen.queryByText(/Data Connections/)).toBeInTheDocument();
    expect.soft(screen.queryByRole("link", { name: "Projects" })).toHaveAttribute("href", "/projects");
    for (const name of ["Save dataset", "Save project links", "Introspect schema", "Disable"]) {
      expect.soft(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect.soft(screen.queryByRole("combobox", { name: "Data Connection" })).not.toBeInTheDocument();
    expect.soft(screen.queryByDisplayValue("Unresolved")).not.toBeInTheDocument();
    expect.soft(screen.queryByText(/needs to be rebound/)).not.toBeInTheDocument();
    const resource = screen.queryByRole("region", { name: "Package schema" });
    expect.soft(resource).toHaveTextContent("order_id");
    await userEvent.click(screen.getByRole("tab", { name: "Saved queries" }));
    await screen.findByText("Package query");
    await openRowMenu("Package query");
    for (const name of ["Edit", "Disable", "Delete"]) expect.soft(screen.queryByRole("menuitem", { name })).not.toBeInTheDocument();
    expect.soft(screen.queryByRole("menuitem", { name: "View" })).toBeInTheDocument();
    expect.soft(screen.queryByRole("menuitem", { name: "Preview" })).toBeEnabled();
    expect.soft(screen.queryByRole("menuitem", { name: "Validate" })).toBeEnabled();
    const inspect = screen.queryByRole("menuitem", { name: "View" }) || screen.getByRole("menuitem", { name: "Edit" });
    await userEvent.click(inspect);
    const dialog = within(screen.getByRole("dialog"));
    expect.soft(dialog.getByLabelText("SQL query")).toHaveAttribute("readonly");
    expect.soft(dialog.getByLabelText("SQL query")).toHaveValue("SELECT order_id FROM orders");
    expect.soft(dialog.queryByRole("button", { name: "Save query" })).not.toBeInTheDocument();
    await userEvent.click(dialog.getByRole("button", { name: "Cancel" }));
    await clickRowAction("User query", "Edit");
    const editable = within(screen.getByRole("dialog", { name: "Edit query" }));
    expect.soft(editable.getByLabelText("SQL query")).not.toHaveAttribute("readonly");
    expect.soft(editable.getByRole("button", { name: "Save query" })).toBeEnabled();
    expect.soft(mockedUpdateDataset).not.toHaveBeenCalled();
    expect.soft(mockedDeleteDatasetQueryTemplate).not.toHaveBeenCalled();
  });

});
