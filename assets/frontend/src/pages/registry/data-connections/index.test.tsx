import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

import {
  createDataConnection,
  deleteDataConnection,
  fetchDataConnections,
  fetchDatasetConnectors,
  introspectDataConnection,
  testDataConnection,
  toggleDataConnection,
  type DataConnectionRecord,
} from "@/pages/registry/datasets/api";
import { RegistryDataConnectionsPage } from "@/pages/registry/data-connections";
import {
  connectorOptionsFromCatalog,
  schemaFields,
} from "@/pages/registry/data-connections/data-connection-helpers";
import { fetchWorkspaces } from "@/pages/registry/workspaces/api";
import { HttpError } from "@/lib/http/errors";
import { useHasCapability } from "@/lib/auth/capabilities";

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));

vi.mock("@/pages/registry/datasets/api", () => ({
  createDataConnection: vi.fn(),
  deleteDataConnection: vi.fn(),
  fetchDataConnections: vi.fn(),
  fetchDatasetConnectors: vi.fn(),
  introspectDataConnection: vi.fn(),
  testDataConnection: vi.fn(),
  toggleDataConnection: vi.fn(),
}));

vi.mock("@/pages/registry/workspaces/api", () => ({
  fetchWorkspaces: vi.fn(),
}));

const mockedFetchDataConnections = vi.mocked(fetchDataConnections);
const mockedFetchDatasetConnectors = vi.mocked(fetchDatasetConnectors);
const mockedFetchWorkspaces = vi.mocked(fetchWorkspaces);
const mockedTestDataConnection = vi.mocked(testDataConnection);
const mockedIntrospectDataConnection = vi.mocked(introspectDataConnection);
const mockedCreateDataConnection = vi.mocked(createDataConnection);
const mockedDeleteDataConnection = vi.mocked(deleteDataConnection);
const mockedToggleDataConnection = vi.mocked(toggleDataConnection);
const mockedUseHasCapability = vi.mocked(useHasCapability);

const mariadbConnector = {
  connector_id: "extension.database",
  display_name: "Database",
  connector_catalog: [
    {
      connector_type_id: "mariadb",
      display_name: "MariaDB",
      source_kind: "mariadb",
      connection_schema: {
        type: "object",
        required: ["host", "port", "database", "user"],
        properties: {
          host: { type: "string", title: "Host" },
          port: { type: "integer", title: "Port" },
          database: { type: "string", title: "Database" },
          user: { type: "string", title: "User" },
          query_params: { type: "object", title: "Query parameters" },
        },
      },
      secret_schema: {
        type: "object",
        required: ["password", "certificate"],
        properties: {
          password: { type: "string", title: "Password" },
          certificate: { type: "string", title: "Certificate" },
        },
      },
    },
  ],
};

const postgresqlConnector = {
  connector_id: "extension.database",
  display_name: "Database",
  connector_catalog: [
    {
      connector_type_id: "postgresql",
      display_name: "PostgreSQL",
      source_kind: "postgresql",
      connection_schema: {
        type: "object",
        required: ["host", "port", "database", "user"],
        properties: {
          host: { type: "string", title: "Host" },
          port: { type: "integer", title: "Port" },
          database: { type: "string", title: "Database" },
          user: { type: "string", title: "User" },
          query_params: { type: "object", title: "Query parameters" },
        },
      },
      secret_schema: {
        type: "object",
        required: ["password", "certificate"],
        properties: {
          password: { type: "string", title: "Password" },
          certificate: { type: "string", title: "Certificate" },
        },
      },
    },
  ],
};

it("derives connector variants and fields from a generic manifest catalog", () => {
  expect(
    connectorOptionsFromCatalog({
      connector_id: "acme.analytics",
      display_name: "Acme Analytics",
      connector_catalog: [
        {
          connector_type_id: "acme_warehouse",
          display_name: "Acme Warehouse",
          source_kind: "warehouse",
          preset_config: { mode: "warehouse-v2" },
          connection_schema: {
            type: "object",
            required: ["endpoint"],
            properties: {
              endpoint: { type: "string", title: "Warehouse endpoint" },
            },
          },
        },
      ],
    }),
  ).toEqual([
    expect.objectContaining({
      connector_id: "acme.analytics",
      local_connector_id: "acme_warehouse",
      label: "Acme Warehouse",
      preset_config: { mode: "warehouse-v2" },
      connection_schema: expect.objectContaining({
        required: ["endpoint"],
      }),
    }),
  ]);
});

it("preserves required and default schema contracts for the connection form", () => {
  expect(
    schemaFields({
      type: "object",
      required: ["endpoint"],
      properties: {
        endpoint: { type: "string", default: "https://api.acme.test" },
      },
    }),
  ).toEqual([
    expect.objectContaining({
      key: "endpoint",
      required: true,
      default_value: "https://api.acme.test",
    }),
  ]);
});

function renderPage(locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  const messages: Record<string, string> = locale === "lt" ? { "react.registry.required_field_additional_settings_hint": " Jis yra papildomuose nustatymuose." } : {};
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <QueryClientProvider client={queryClient}>
      <RegistryDataConnectionsPage />
    </QueryClientProvider></I18nContext.Provider>,
  );
}

/**
 * `DsSelect` renders a combobox trigger and a listbox, not a native `<select>`,
 * so a value is chosen by opening the menu and clicking the option. The menu
 * lives in the viewport layer (so a dialog footer cannot clip it), so it is
 * found by its own name rather than inside the dialog.
 */
async function chooseOption(scope: HTMLElement, name: string, optionLabel: string) {
  fireEvent.click(within(scope).getByRole("combobox", { name }));
  const menu = await screen.findByRole("listbox", { name: `${name} options` });
  fireEvent.click(within(menu).getByRole("option", { name: optionLabel }));
}

/** Row actions live behind the published ellipsis menu. */
async function openRowMenu(connectionName: string) {
  fireEvent.click(
    await screen.findByRole("button", { name: `Actions for ${connectionName}` }),
  );
}

async function clickRowAction(connectionName: string, action: string) {
  await openRowMenu(connectionName);
  fireEvent.click(await screen.findByRole("menuitem", { name: action }));
}

describe("RegistryDataConnectionsPage", () => {
  beforeEach(() => {
    localStorage.clear();
    mockedUseHasCapability.mockReturnValue(true);
    mockedFetchDataConnections.mockReset();
    mockedFetchDatasetConnectors.mockReset();
    mockedFetchWorkspaces.mockReset();
    mockedTestDataConnection.mockReset();
    mockedIntrospectDataConnection.mockReset();
    mockedCreateDataConnection.mockReset();
    mockedDeleteDataConnection.mockReset();
    mockedToggleDataConnection.mockReset();

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
        projects_count: 1,
        jobs_count: 0,
      },
    ]);
    mockedFetchDatasetConnectors.mockResolvedValue([]);
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "flow-steward-app-db-00000000-000",
        display_name: "Flow Steward App Database",
        workspace_id: "ws-1",
        workspace_display_name: "Default workspace",
        source_kind: "postgres",
        environment: "dev",
        enabled: true,
        health_status: "unknown",
        linked_dataset_count: 0,
        tags: ["warehouse"],
        connector_ref_jsonb: {},
        connection_config_jsonb: {},
        secret_refs_jsonb: {},
        metadata_jsonb: {},
      },
    ]);
    mockedDeleteDataConnection.mockResolvedValue(undefined as never);
  });

  it("stacks the page on the published section rhythm", async () => {
    // `fs-page` is not a class the design system defines, so the header and the
    // table card rendered flush at 0px. `.fs-panel-stack` is the published
    // grid carrying --fs-dashboard-section-gap.
    const { container } = renderPage();
    await screen.findByRole("table", { name: "Connection registry" });

    // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access -- a layout wrapper has no role to query by
    expect(container.querySelector("section")).toHaveClass("fs-panel-stack");
  });

  it("keeps operator-readable connections free of mutation controls", async () => {
    mockedUseHasCapability.mockReturnValue(false);
    renderPage();

    expect(
      screen.queryByRole("button", { name: "Add connection" }),
    ).not.toBeInTheDocument();
    // The connection is listed; only the action column is gone, which is how
    // the published table says "read only" — an empty column is not rendered.
    expect(
      await screen.findByText("Flow Steward App Database"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "Actions for Flow Steward App Database",
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Edit" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete" }),
    ).not.toBeInTheDocument();
  });

  it("keeps All workspaces aggregate unscoped when only one workspace option is visible", async () => {
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "connection-ws-1",
        display_name: "Workspace one database",
        workspace_id: "ws-1",
      },
      {
        connection_id: "connection-ws-2",
        display_name: "Workspace two database",
        workspace_id: "ws-2",
      },
    ] as never);

    renderPage();

    expect(
      await screen.findByText("Workspace one database"),
    ).toBeInTheDocument();
    expect(screen.getByText("Workspace two database")).toBeInTheDocument();
    // No workspace filter is applied, so the published filter bar shows no
    // active chip for it and the query stays unscoped.
    expect(
      screen.queryByRole("button", { name: /^Workspace:/ }),
    ).not.toBeInTheDocument();
    expect(mockedFetchDataConnections).toHaveBeenCalledWith({
      workspaceId: undefined,
    });
  });

  it("does not reuse the app-shell workspace selection as the registry filter", async () => {
    localStorage.setItem("platform:selected-workspace-id", "ws-1");
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "connection-ws-1",
        display_name: "Workspace one database",
        workspace_id: "ws-1",
      },
      {
        connection_id: "connection-ws-2",
        display_name: "Workspace two database",
        workspace_id: "ws-2",
      },
    ] as never);

    renderPage();

    expect(
      await screen.findByText("Workspace one database"),
    ).toBeInTheDocument();
    expect(screen.getByText("Workspace two database")).toBeInTheDocument();
    // No workspace filter is applied, so the published filter bar shows no
    // active chip for it and the query stays unscoped.
    expect(
      screen.queryByRole("button", { name: /^Workspace:/ }),
    ).not.toBeInTheDocument();
    expect(mockedFetchDataConnections).toHaveBeenCalledWith({
      workspaceId: undefined,
    });
  });

  it("shows MariaDB primary fields and hides runtime IDs", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([mariadbConnector] as never);
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Workspace", "Default workspace");
    await chooseOption(dialog, "Connector", "MariaDB");

    expect(within(dialog).getByLabelText(/^Host/)).toBeVisible();
    expect(within(dialog).getByLabelText(/^Port/)).toBeVisible();
    expect(within(dialog).getByLabelText(/^Database/)).toBeVisible();
    expect(within(dialog).getByLabelText(/^User/)).toBeVisible();
    expect(
      within(dialog).getByLabelText(/^Password/),
    ).toBeVisible();
    expect(
      within(dialog).queryByLabelText(/^Query parameters/),
    ).not.toBeVisible();
    expect(
      within(dialog).queryByText(/connector id:/i),
    ).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/local id:/i)).not.toBeInTheDocument();
  });

  it("shows PostgreSQL primary fields through the driver-neutral grouping", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([
      postgresqlConnector,
    ] as never);
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Connector", "PostgreSQL");

    expect(within(dialog).getByLabelText(/^Host/)).toBeVisible();
    expect(within(dialog).getByLabelText(/^Port/)).toBeVisible();
    expect(within(dialog).getByLabelText(/^Database/)).toBeVisible();
    expect(within(dialog).getByLabelText(/^User/)).toBeVisible();
    expect(
      within(dialog).getByLabelText(/^Password/),
    ).toBeVisible();
    expect(
      within(dialog).queryByLabelText(/^Query parameters/),
    ).not.toBeVisible();
  });

  it("localizes the advanced required-field location while preserving the connector-owned field name", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([mariadbConnector] as never);
    renderPage("lt");
    fireEvent.click(await screen.findByRole("button", { name: "Add connection" }));
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Workspace", "Default workspace");
    await chooseOption(dialog, "Connector", "MariaDB");
    for (const [label, value] of Object.entries({
      Host: "mariadb.test",
      Port: "3306",
      Database: "analytics",
      User: "reporter",
      Password: "fixture-secret", // pragma: allowlist secret - non-authenticating form-test value
    })) {
      fireEvent.change(within(dialog).getByLabelText(new RegExp(`^${label}`)), { target: { value } });
    }
    expect(await within(dialog).findByText("Certificate is required. Jis yra papildomuose nustatymuose.")).toBeInTheDocument();
    expect(within(dialog).queryByText(/It is under Additional settings\./)).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Create connection" })).toBeDisabled();
    expect(mockedCreateDataConnection).not.toHaveBeenCalled();
  });

  it("opens additional settings for a missing required advanced field", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([mariadbConnector] as never);
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Workspace", "Default workspace");
    await chooseOption(dialog, "Connector", "MariaDB");
    for (const [label, value] of Object.entries({
      "Host": "mariadb.test",
      "Port": "3306",
      "Database": "analytics",
      "User": "reporter",
      "Password": "secret", // pragma: allowlist secret - a form value in a test
    })) {
      fireEvent.change(within(dialog).getByLabelText(new RegExp(`^${label}`)), {
        target: { value },
      });
    }

    // RS5-014: Create is now disabled while a required field is empty, so the
    // click cannot be what explains the gap. The form says so instead, and
    // names where the field lives - a dead control with no reason is its own
    // defect.
    expect(
      within(dialog).getByRole("button", { name: "Create connection" }),
    ).toBeDisabled();

    expect(
      await screen.findAllByText(/Certificate is required\./),
    ).not.toHaveLength(0);
    expect(
      screen.getByText(/It is under Additional settings\./),
    ).toBeInTheDocument();
    // The section is not force-opened on every keystroke; the operator is told
    // where to look and opens it. Once open, the field is there and marked.
    fireEvent.click(within(dialog).getByText("Additional settings"));
    expect(
      within(dialog).getByLabelText(/^Certificate/),
    ).toBeVisible();
    expect(mockedCreateDataConnection).not.toHaveBeenCalled();
  });

  it("edits an object-valued schema field in a textarea, not a single line", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([mariadbConnector] as never);
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Connector", "MariaDB");
    fireEvent.click(within(dialog).getByText("Additional settings"));

    // `query_params` is `type: "object"`; its value is JSON, so it needs the
    // multi-line control the form has always given it.
    expect(within(dialog).getByLabelText(/^Query parameters/).tagName).toBe(
      "TEXTAREA",
    );
  });

  it("states on the closed row how many advanced values are set", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([mariadbConnector] as never);
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Connector", "MariaDB");

    // query_params and certificate are the advanced pair for this connector.
    const summary = within(dialog).getByText("Additional settings");
    expect(summary).toHaveTextContent("0 of 2 set");

    fireEvent.click(summary);
    fireEvent.change(within(dialog).getByLabelText(/^Query parameters/), {
      target: { value: '{"ssl":"true"}' },
    });

    expect(summary).toHaveTextContent("1 of 2 set");
  });

  it("clusters advanced fields under the published legends", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([mariadbConnector] as never);
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Connector", "MariaDB");
    fireEvent.click(within(dialog).getByText("Additional settings"));

    expect(within(dialog).getByRole("group", { name: "Transport" })).toBeInTheDocument();
    expect(
      within(dialog).getByRole("group", { name: "Other settings" }),
    ).toBeInTheDocument();
  });

  it("keeps the stored-secret condition in the helper, not the placeholder", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([mariadbConnector] as never);
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "mariadb-connection",
        display_name: "MariaDB reporting",
        workspace_id: "ws-1",
        environment: "dev",
        enabled: true,
        health_status: "unknown",
        linked_dataset_count: 0,
        tags: [],
        connector_ref_jsonb: { connector_id: "extension.database", local_connector_id: "mariadb" },
        connection_config_jsonb: {},
        secret_refs_jsonb: { refs: { password: "ref-1" } }, // pragma: allowlist secret
        metadata_jsonb: {},
      },
    ] as never);
    renderPage();

    await clickRowAction("MariaDB reporting", "Edit");
    const dialog = screen.getByRole("dialog");
    const password = await within(dialog).findByLabelText(/^Password/);

    // A placeholder disappears the moment someone types; the helper survives.
    expect(password).not.toHaveAttribute("placeholder");
    // Both secret fields carry it — password here, certificate under Additional
    // settings — so the assertion is on the set, not on a single node.
    expect(
      within(dialog).getAllByText(/Leave blank to keep the existing stored secret\./),
    ).not.toHaveLength(0);
  });

  it("renders a branch-narrowed field as a select, not free text", async () => {
    // Canon: "A value from a fixed set is a Select. An enum rendered as a text
    // input is a defect: the operator cannot know the valid strings." Toolbox
    // declares such sets per source, inside the schema's allOf branches.
    mockedFetchDatasetConnectors.mockResolvedValue([
      {
        connector_id: "extension.database",
        display_name: "Database",
        connector_catalog: [
          {
            connector_type_id: "spanner",
            display_name: "Spanner",
            source_kind: "spanner",
            connection_schema: {
              type: "object",
              properties: {
                dialect: { type: "string", title: "SQL dialect" },
                instance: { type: "string", title: "Instance" },
              },
              allOf: [
                {
                  if: { properties: { dialect: { enum: ["googlesql", "postgresql"] } } },
                  then: {},
                },
                {
                  if: {},
                  then: {
                    properties: { dialect: { enum: ["googlesql", "postgresql"] } },
                  },
                },
              ],
            },
          },
        ],
      },
    ] as never);
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Connector", "Spanner");

    const dialect = within(dialog).getByRole("combobox", { name: "SQL dialect" });
    fireEvent.click(dialect);
    const dialects = await screen.findByRole("listbox", { name: "SQL dialect options" });
    expect(within(dialects).getByRole("option", { name: "googlesql" })).toBeInTheDocument();
    expect(within(dialects).getByRole("option", { name: "postgresql" })).toBeInTheDocument();

    // A field no branch narrows stays a plain input.
    expect(within(dialog).getByLabelText(/^Instance/).tagName).toBe("INPUT");
  });

  it("preserves a required advanced secret reference when editing a non-secret field", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([mariadbConnector] as never);
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "mariadb-connection",
        display_name: "MariaDB reporting",
        workspace_id: "ws-1",
        workspace_display_name: "Default workspace",
        source_kind: "mariadb",
        environment: "dev",
        enabled: true,
        health_status: "unknown",
        linked_dataset_count: 0,
        tags: [],
        connector_ref_jsonb: {
          connector_id: "extension.database",
          local_connector_id: "mariadb",
        },
        connection_config_jsonb: {
          host: "mariadb.test",
          port: 3306,
          database: "analytics",
          user: "reporter",
        },
        secret_refs_jsonb: {
          refs: {
            password: "stored-password-ref", // pragma: allowlist secret
            certificate: "stored-certificate-ref",
          },
        },
        metadata_jsonb: {},
      },
    ]);
    mockedCreateDataConnection.mockResolvedValue({
      connection_id: "mariadb-connection",
      workspace_id: "ws-1",
    } as never);

    renderPage();

    await waitFor(
      () => expect(screen.getByRole("table", { name: "Connection registry" })).toHaveAttribute("aria-busy", "false"),
      { timeout: 5_000 },
    );
    await clickRowAction("MariaDB reporting", "Edit");
    const dialog = screen.getByRole("dialog");
    await waitFor(() => {
      expect(
        within(dialog).getByRole("combobox", { name: "Connector" }),
      ).toHaveTextContent("MariaDB");
    });
    fireEvent.change(within(dialog).getByLabelText(/^Name/), {
      target: { value: "MariaDB reporting renamed" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save connection" }),
    );

    await waitFor(() => expect(mockedCreateDataConnection).toHaveBeenCalled());
    expect(
      mockedCreateDataConnection.mock.calls[0][0].secret_refs_jsonb,
    ).toEqual({});
  });

  function prepareMetadataConnection(enabled: boolean): DataConnectionRecord {
    mockedFetchDatasetConnectors.mockResolvedValue([{
      connector_id: "fixture.metadata",
      local_connector_id: "metadata",
      display_name: "Metadata fixture",
      connection_schema: { type: "object", properties: {} },
      secret_schema: { type: "object", properties: {} },
    }] as never);
    const connection: DataConnectionRecord = {
      connection_id: "metadata-connection",
      display_name: "Metadata connection",
      description: "Original description",
      workspace_id: "ws-1",
      source_kind: "fixture",
      environment: "dev",
      enabled,
      health_status: "unknown",
      linked_dataset_count: 0,
      tags: [],
      connector_ref_jsonb: {
        connector_id: "fixture.metadata",
        local_connector_id: "metadata",
      },
      connection_config_jsonb: {},
      secret_refs_jsonb: {},
      metadata_jsonb: {},
    };
    mockedFetchDataConnections.mockResolvedValue([connection]);
    return connection;
  }

  function expectNoOperationalActions() {
    expect(mockedToggleDataConnection).not.toHaveBeenCalled();
    expect(mockedTestDataConnection).not.toHaveBeenCalled();
    expect(mockedIntrospectDataConnection).not.toHaveBeenCalled();
  }

  it.each([false, true])("preserves enabled=%s through metadata save and original-description restore", async (enabled) => {
    let responseRow = prepareMetadataConnection(enabled);
    const otherRow: DataConnectionRecord = {
      ...responseRow,
      connection_id: "different-connection",
      display_name: "Different connection",
      enabled: !enabled,
    };
    // This models only API responses/refetch, not real persistence or query capability.
    mockedFetchDataConnections.mockImplementation(async () => [{ ...otherRow }, { ...responseRow }]);
    mockedCreateDataConnection.mockImplementation(async (payload) => {
      responseRow = {
        ...responseRow,
        ...payload,
        connection_id: payload.connection_id ?? responseRow.connection_id,
      };
      return { ...responseRow };
    });
    renderPage();

    await clickRowAction("Metadata connection", "Edit");
    let dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText("Description")).toHaveValue("Original description");
    fireEvent.change(within(dialog).getByLabelText("Description"), {
      target: { value: "Edited description" },
    });
    const fetchCount = mockedFetchDataConnections.mock.calls.length;
    fireEvent.click(within(dialog).getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(mockedCreateDataConnection).toHaveBeenCalledTimes(1));
    expect(mockedCreateDataConnection.mock.calls[0][0]).toEqual(expect.objectContaining({
      connection_id: "metadata-connection",
      workspace_id: "ws-1",
      description: "Edited description",
      enabled,
      secret_refs_jsonb: {},
    }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(mockedFetchDataConnections.mock.calls.length).toBeGreaterThan(fetchCount));

    await clickRowAction("Metadata connection", "Edit");
    dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText("Description")).toHaveValue("Edited description");
    fireEvent.change(within(dialog).getByLabelText("Description"), {
      target: { value: "Original description" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(mockedCreateDataConnection).toHaveBeenCalledTimes(2));
    expect(mockedCreateDataConnection.mock.calls[1][0]).toEqual(expect.objectContaining({
      connection_id: "metadata-connection",
      description: "Original description",
      enabled,
      secret_refs_jsonb: {},
    }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expectNoOperationalActions();
  });

  it("keeps the create enabled default after closing a disabled connection editor", async () => {
    const connection = prepareMetadataConnection(false);
    mockedCreateDataConnection.mockResolvedValue({ ...connection, connection_id: "created-connection", enabled: true });
    renderPage();
    await clickRowAction("Metadata connection", "Edit");
    fireEvent.click(screen.getByRole("button", { name: "Close connection editor" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mockedCreateDataConnection).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Workspace", "Default workspace");
    await chooseOption(dialog, "Connector", "Metadata fixture");
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: "New metadata connection" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create connection" }));

    await waitFor(() => expect(mockedCreateDataConnection).toHaveBeenCalledTimes(1));
    expect(mockedCreateDataConnection.mock.calls[0][0]).toEqual(expect.objectContaining({
      connection_id: undefined,
      display_name: "New metadata connection",
      workspace_id: "ws-1",
      enabled: true,
    }));
    expectNoOperationalActions();
  });

  it("cancels a disabled connection metadata edit without saving or operational actions", async () => {
    prepareMetadataConnection(false);
    renderPage();
    await clickRowAction("Metadata connection", "Edit");
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Description"), { target: { value: "Discarded description" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await clickRowAction("Metadata connection", "Edit");
    expect(within(screen.getByRole("dialog")).getByLabelText("Description")).toHaveValue("Original description");
    expect(mockedCreateDataConnection).not.toHaveBeenCalled();
    expectNoOperationalActions();
  });

  it("shows unwrapped connector errors instead of raw JSON", async () => {
    mockedTestDataConnection.mockResolvedValue({
      ok: false,
      result: {},
      errors: [
        {
          code: "connector_runtime_failed",
          message:
            '{"error":"connection failed: password authentication failed"}',
        },
      ],
    });

    renderPage();

    await clickRowAction("Flow Steward App Database", "Test");

    expect(
      await screen.findByText(
        "connection failed: password authentication failed",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText(/"error"/)).not.toBeInTheDocument();
    });
  });

  it("shows a neutral message for a connection-test 5xx without details", async () => {
    mockedTestDataConnection.mockRejectedValue(
      new HttpError({
        status: 500,
        url: "/api/datasets/data-connections/test/test",
        payload: {},
      }),
    );

    renderPage();

    await clickRowAction("Flow Steward App Database", "Test");

    expect(
      await screen.findByText(
        "Unable to test the data connection. Please try again or contact an administrator.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps Test and Introspect pending states on the invoked connection only", async () => {
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "first-connection",
        display_name: "First connection",
        workspace_id: "ws-1",
        workspace_display_name: "Default workspace",
        source_kind: "postgres",
        environment: "dev",
        enabled: true,
        health_status: "unknown",
        linked_dataset_count: 0,
        tags: [],
        connector_ref_jsonb: {},
        connection_config_jsonb: {},
        secret_refs_jsonb: {},
        metadata_jsonb: {},
      },
      {
        connection_id: "second-connection",
        display_name: "Second connection",
        workspace_id: "ws-1",
        workspace_display_name: "Default workspace",
        source_kind: "postgres",
        environment: "dev",
        enabled: true,
        health_status: "unknown",
        linked_dataset_count: 0,
        tags: [],
        connector_ref_jsonb: {},
        connection_config_jsonb: {},
        secret_refs_jsonb: {},
        metadata_jsonb: {},
      },
    ]);
    mockedTestDataConnection.mockImplementation(() => new Promise(() => {}));
    mockedIntrospectDataConnection.mockImplementation(
      () => new Promise(() => {}),
    );

    renderPage();

    await clickRowAction("First connection", "Test");
    await openRowMenu("First connection");
    await waitFor(() =>
      expect(screen.getByRole("menuitem", { name: "Test" })).toBeDisabled(),
    );
    await openRowMenu("Second connection");
    expect(screen.getByRole("menuitem", { name: "Test" })).toBeEnabled();

    await clickRowAction("First connection", "Introspect");
    await openRowMenu("First connection");
    await waitFor(() =>
      expect(
        screen.getByRole("menuitem", { name: "Introspect" }),
      ).toBeDisabled(),
    );
    await openRowMenu("Second connection");
    expect(screen.getByRole("menuitem", { name: "Introspect" })).toBeEnabled();
  });

  it("keeps actions reachable and opens editing from the connection name", async () => {
    renderPage();

    expect(
      await screen.findByRole("table", { name: "Connection registry" }),
    ).toHaveClass("fs-table");

    await openRowMenu("Flow Steward App Database");
    for (const action of ["Edit", "Test", "Introspect", "Disable", "Delete"]) {
      expect(screen.getByRole("menuitem", { name: action })).toBeInTheDocument();
    }
    fireEvent.keyDown(document, { key: "Escape" });

    fireEvent.click(
      await screen.findByRole("button", { name: "Flow Steward App Database" }),
    );

    const dialog = await screen.findByRole("dialog", {
      name: "Edit data connection",
    });
    expect(
      within(dialog).getByText(
        'Editing "Flow Steward App Database". Stored secrets remain unchanged unless replaced.',
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^Name/)).toHaveValue(
      "Flow Steward App Database",
    );
  });

  it("blocks create when the port is outside the valid range", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([
      {
        connector_id: "extension.install_1.warehouse_sql",
        local_connector_id: "warehouse_sql",
        display_name: "Warehouse SQL",
        connection_schema: {
          type: "object",
          properties: {
            port: {
              type: "integer",
              title: "Port",
              minimum: 1,
              maximum: 65535,
            },
          },
        },
      },
    ] as never);

    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Workspace", "Default workspace");
    await chooseOption(dialog, "Connector", "Warehouse SQL");
    await waitFor(() => {
      expect(
        within(dialog).getByRole("button", { name: "Create connection" }),
      ).toBeEnabled();
    });
    fireEvent.change(within(dialog).getByLabelText(/^Port/), {
      target: { value: "70000" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Create connection" }),
    );

    expect(
      await screen.findAllByText("Port must be at most 65535."),
    ).not.toHaveLength(0);
    expect(mockedCreateDataConnection).not.toHaveBeenCalled();
  });

  it("accepts a valid port when creating a connection", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([
      {
        connector_id: "extension.install_1.warehouse_sql",
        local_connector_id: "warehouse_sql",
        display_name: "Warehouse SQL",
        connection_schema: {
          type: "object",
          properties: {
            port: { type: "integer", title: "Port" },
          },
        },
      },
    ] as never);
    mockedCreateDataConnection.mockResolvedValue({
      connection_id: "warehouse",
      display_name: "Warehouse SQL connection",
    } as never);

    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Workspace", "Default workspace");
    await chooseOption(dialog, "Connector", "Warehouse SQL");
    fireEvent.change(within(dialog).getByLabelText(/^Port/), {
      target: { value: "5432" },
    });
    await waitFor(() => {
      expect(
        within(dialog).getByRole("button", { name: "Create connection" }),
      ).toBeEnabled();
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Create connection" }),
    );

    await waitFor(() => {
      expect(mockedCreateDataConnection).toHaveBeenCalled();
    });
    const [payload] = mockedCreateDataConnection.mock.calls[0];
    expect(payload.connection_config_jsonb).toMatchObject({ port: 5432 });
  });

  it("selects the saved Toolbox prebuilt when editing a data connection", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([
      {
        connector_id: "extension.install_1.toolbox_database",
        local_connector_id: "toolbox_database",
        display_name: "Acme database source",
        connector_catalog: [
          {
            connector_type_id: "toolbox_alloydb_admin",
            display_name: "AlloyDB Admin",
            source_kind: "alloydb-admin",
            preset_config: { prebuilt: "alloydb-admin" },
          },
          {
            connector_type_id: "toolbox_postgres",
            display_name: "PostgreSQL",
            source_kind: "postgres",
            preset_config: { prebuilt: "postgres" },
          },
        ],
        connection_schema: {
          type: "object",
          properties: {
            prebuilt: { type: "string" },
            project: { type: "string", title: "Cloud project" },
            region: { type: "string", title: "Region" },
            instance: { type: "string", title: "Instance" },
          },
        },
        secret_schema: {
          type: "object",
          required: ["password"],
          properties: {
            password: { type: "string", title: "Password" },
          },
        },
      },
    ] as never);
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "flow-steward-app-db-00000000-000",
        display_name: "Flow Steward App Database",
        workspace_id: "ws-1",
        workspace_display_name: "Default workspace",
        source_kind: "extension",
        environment: "dev",
        enabled: true,
        health_status: "healthy",
        linked_dataset_count: 0,
        tags: ["warehouse"],
        connector_ref_jsonb: {
          connector_id: "extension.install_1.toolbox_database",
          local_connector_id: "toolbox_database",
        },
        connection_config_jsonb: {
          prebuilt: "postgres",
          host: "pg-fs",
          port: 5432,
          user: "flow_steward",
          database: "flow_steward",
        },
        secret_refs_jsonb: { refs: { password: "stored-password-ref" } }, // pragma: allowlist secret
        metadata_jsonb: {},
      },
    ]);

    renderPage();

    await clickRowAction("Flow Steward App Database", "Edit");
    const dialog = screen.getByRole("dialog");
    await waitFor(() => {
      expect(
        within(dialog).getByRole("combobox", { name: "Connector" }),
      ).toHaveTextContent("PostgreSQL");
    });
    expect(within(dialog).getAllByText("PostgreSQL").length).toBeGreaterThan(0);
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save connection" }),
    );
    await waitFor(() => expect(mockedCreateDataConnection).toHaveBeenCalled());
    expect(
      mockedCreateDataConnection.mock.calls[0][0].secret_refs_jsonb,
    ).toEqual({});
  });

  it("persists source-specific Toolbox configuration fields", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([
      {
        connector_id: "extension.install_1.toolbox_database",
        local_connector_id: "toolbox_database",
        display_name: "Acme database source",
        connector_catalog: [
          {
            connector_type_id: "toolbox_cloud_sql_postgres",
            display_name: "Cloud SQL for PostgreSQL",
            source_kind: "cloud-sql-postgres",
            preset_config: { prebuilt: "cloud-sql-postgres" },
          },
        ],
        connection_schema: {
          type: "object",
          properties: {
            prebuilt: { type: "string" },
            project: { type: "string", title: "Cloud project" },
            region: { type: "string", title: "Region" },
            instance: { type: "string", title: "Instance" },
          },
        },
      },
    ] as never);

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Workspace", "Default workspace");
    await chooseOption(dialog, "Connector", "Cloud SQL for PostgreSQL");
    fireEvent.change(within(dialog).getByLabelText(/^Cloud project/), {
      target: { value: "analytics-project" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Region/), {
      target: { value: "europe-west1" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Instance/), {
      target: { value: "reporting-db" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Create connection" }),
    );

    await waitFor(() => expect(mockedCreateDataConnection).toHaveBeenCalled());
    expect(
      mockedCreateDataConnection.mock.calls[0][0].connection_config_jsonb,
    ).toMatchObject({
      prebuilt: "cloud-sql-postgres",
      project: "analytics-project",
      region: "europe-west1",
      instance: "reporting-db",
    });
  });

  it("renders connection fields from a third-party connector schema", async () => {
    mockedFetchDatasetConnectors.mockResolvedValue([
      {
        connector_id: "acme.analytics",
        local_connector_id: "analytics",
        display_name: "Acme Analytics",
        connection_schema: {
          type: "object",
          properties: {
            endpoint: { type: "string", title: "Endpoint" },
            tenant: { type: "string", title: "Tenant" },
          },
        },
      },
    ] as never);

    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "Add connection" }),
    );
    const dialog = screen.getByRole("dialog");
    await chooseOption(dialog, "Connector", "Acme Analytics");

    expect(within(dialog).getByLabelText(/^Endpoint/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^Tenant/)).toBeInTheDocument();
  });

  it("uses an in-app dialog instead of native confirm before deleting", async () => {
    const confirmSpy = vi.spyOn(globalThis, "confirm");

    renderPage();

    await clickRowAction("Flow Steward App Database", "Delete");

    expect(confirmSpy).not.toHaveBeenCalled();
    // A confirm dialog is an alertdialog in the published component.
    const dialog = screen.getByRole("alertdialog");
    expect(
      within(dialog).getByText(/Flow Steward App Database/),
    ).toBeInTheDocument();

    fireEvent.click(
      within(dialog).getByRole("button", { name: "Delete connection" }),
    );

    await waitFor(() => {
      expect(mockedDeleteDataConnection.mock.calls[0]?.[0]).toBe(
        "flow-steward-app-db-00000000-000",
      );
    });
    confirmSpy.mockRestore();
  });
});
