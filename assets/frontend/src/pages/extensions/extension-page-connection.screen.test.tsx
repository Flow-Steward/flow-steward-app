import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { ExtensionsHubPage } from "@/pages/extensions";
import {
  executeExtensionAction,
  executeExtensionQuery,
  fetchExtension,
  fetchExtensionArchiveLifecycleAvailability,
  fetchExtensionCallbacks,
  fetchExtensionCompiledManifest,
  fetchExtensionExternalEffects,
  fetchExtensionPreview,
  fetchExtensions,
} from "@/pages/extensions/api";
import { fetchExtensionProjectState, fetchProjectConnectionsList } from "@/pages/projects/connections/api";
import { fetchProjectsList } from "@/pages/projects/list/api";

vi.mock("@/pages/extensions/api", () => ({
  executeExtensionAction: vi.fn(),
  executeExtensionQuery: vi.fn(),
  fetchExtension: vi.fn(),
  fetchExtensionArchiveLifecycleAvailability: vi.fn(),
  fetchExtensionCallbacks: vi.fn(),
  fetchExtensionCompiledManifest: vi.fn(),
  fetchExtensionExternalEffects: vi.fn(),
  fetchExtensionPreview: vi.fn(),
  fetchExtensions: vi.fn(),
  normalizeExtensionRecord: vi.fn((value: unknown) => value),
}));
vi.mock("@/pages/projects/list/api", () => ({ fetchProjectsList: vi.fn() }));
vi.mock("@/pages/projects/connections/api", () => ({
  fetchExtensionProjectState: vi.fn(),
  fetchProjectConnectionsList: vi.fn(),
}));

const EXTENSION_ID = "example.connector";
const mockedExecuteExtensionAction = vi.mocked(executeExtensionAction);
const mockedExecuteExtensionQuery = vi.mocked(executeExtensionQuery);
const mockedFetchProjectConnectionsList = vi.mocked(fetchProjectConnectionsList);

const manifest = {
  account_scope: { scope_type: "project" },
  connection_types: [{ connection_type_id: "service_profile", display_name: "Service profile" }],
  query_definitions: [
    { query_id: "runs.list", connection: { connection_type_ids: ["service_profile"] } },
    { query_id: "about" },
  ],
  ui_manifest: {
    pages: [
      {
        page_id: "runs",
        title: "Runs",
        connection: { connection_type_ids: ["service_profile"] },
        components: [
          {
            component_id: "runs_grid",
            type: "grid",
            title: "Runs",
            query_id: "runs.list",
            columns: [{ key: "run_id", label: "Run" }],
            row_actions: [{ label: "Stop", action_id: "runs.stop" }],
          },
          { component_id: "about", type: "markdown", query_id: "about" },
        ],
      },
      {
        page_id: "profiles",
        title: "Profiles",
        components: [{
          component_id: "profile_form",
          type: "connection_form",
          data: { connection_type: "service_profile" },
          fields: [{ path: "display_name", label: "Name", widget: "text" }],
        }],
      },
    ],
  },
};

const connection = (id: string, name: string) => ({
  connection_id: id,
  display_name: name,
  connection_type: "service_profile",
  extension_install_id: "install-1",
  enabled: true,
});

function auth(role: "admin" | "operator" | "viewer"): AuthContextValue {
  return {
    error: null,
    isAuthenticated: true,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    session: { authenticated: true, roles: [role], user: { roles: [role] } } as AuthContextValue["session"],
    status: "authenticated",
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.search}</div>;
}

function renderPage(search: string, role: "admin" | "operator" | "viewer" = "operator") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <AuthContext.Provider value={auth(role)}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/extensions/${EXTENSION_ID}${search}`]}>
          <Routes>
            <Route element={<><ExtensionsHubPage /><LocationProbe /></>} path="/extensions/:extensionId" />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </AuthContext.Provider>,
  );
}

function runsQueryCalls() {
  return mockedExecuteExtensionQuery.mock.calls.filter(([, queryId]) => queryId === "runs.list");
}

beforeEach(() => {
  vi.clearAllMocks();
  const extension = {
    extension_id: EXTENSION_ID,
    extension_install_id: "install-1",
    display_name: "Example connector",
    version: "1.0.0",
    state: "active",
    active: true,
  };
  vi.mocked(fetchExtensions).mockResolvedValue([extension] as never);
  vi.mocked(fetchExtension).mockResolvedValue(extension as never);
  vi.mocked(fetchExtensionCompiledManifest).mockResolvedValue(manifest as never);
  vi.mocked(fetchExtensionArchiveLifecycleAvailability).mockResolvedValue({
    available: true, reason_code: "available", message: "", remediation: { install_update: [], uninstall: [] },
  } as never);
  vi.mocked(fetchExtensionCallbacks).mockResolvedValue([]);
  vi.mocked(fetchExtensionExternalEffects).mockResolvedValue({ effects: [], truncated: false } as never);
  vi.mocked(fetchExtensionPreview).mockResolvedValue({} as never);
  vi.mocked(fetchExtensionProjectState).mockResolvedValue([]);
  vi.mocked(fetchProjectsList).mockResolvedValue({
    projects: [{ id: "project-1", name: "Project One" }], page: 1, per_page: 200, total: 1, total_pages: 1,
  } as never);
  mockedExecuteExtensionQuery.mockResolvedValue({ query: { status: "succeeded", response: { ok: true, result: { items: [{ run_id: "r-1" }] } } } } as never);
  mockedExecuteExtensionAction.mockResolvedValue({ action: { status: "succeeded", response: { ok: true, result: {} } } } as never);
});

describe("an extension page that reads through a connection", () => {
  it("uses the only connection and sends it with the connection query only", async () => {
    mockedFetchProjectConnectionsList.mockResolvedValue([connection("conn-1", "Production")]);

    renderPage("?project_id=project-1&extension_page=runs");

    await waitFor(() => expect(runsQueryCalls()).toHaveLength(1));
    expect(runsQueryCalls()[0][2]).toEqual(expect.objectContaining({
      context: { project_id: "project-1" },
      target: { connection_id: "conn-1" },
    }));
    const about = mockedExecuteExtensionQuery.mock.calls.find(([, queryId]) => queryId === "about");
    expect(about?.[2]).not.toHaveProperty("target");
    expect(screen.getByTestId("extension-page-connection")).toHaveTextContent("Production");
  });

  it("runs nothing until the person picks one of several connections, and keeps it in the URL", async () => {
    const user = userEvent.setup();
    mockedFetchProjectConnectionsList.mockResolvedValue([
      connection("conn-1", "Production"),
      connection("conn-2", "Staging"),
    ]);

    renderPage("?project_id=project-1&extension_page=runs");

    await screen.findByText("Choose the connection this page reads its data through.");
    expect(mockedExecuteExtensionQuery).not.toHaveBeenCalled();

    await user.click(screen.getByRole("combobox", { name: "Connection" }));
    await user.click(await screen.findByRole("option", { name: "Staging" }));

    await waitFor(() => expect(runsQueryCalls()).toHaveLength(1));
    expect(runsQueryCalls()[0][2]).toEqual(expect.objectContaining({ target: { connection_id: "conn-2" } }));
    expect(screen.getByTestId("location-probe")).toHaveTextContent("extension_connection=conn-2");
  });

  it("restores the connection named in the URL", async () => {
    mockedFetchProjectConnectionsList.mockResolvedValue([
      connection("conn-1", "Production"),
      connection("conn-2", "Staging"),
    ]);

    renderPage("?project_id=project-1&extension_page=runs&extension_connection=conn-2");

    await waitFor(() => expect(runsQueryCalls()).toHaveLength(1));
    expect(runsQueryCalls()[0][2]).toEqual(expect.objectContaining({ target: { connection_id: "conn-2" } }));
  });

  it("says when the project has no connection of the page type and links to its setup page", async () => {
    const user = userEvent.setup();
    mockedFetchProjectConnectionsList.mockResolvedValue([
      { ...connection("conn-9", "Other install"), extension_install_id: "install-2" },
    ]);

    renderPage("?project_id=project-1&extension_page=runs");

    await screen.findByText("No Service profile connection in this project.");
    expect(mockedExecuteExtensionQuery).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Open connections" }));
    await waitFor(() => expect(screen.getByTestId("location-probe")).toHaveTextContent("extension_page=profiles"));
  });

  it("does not load the page data for a viewer", async () => {
    mockedFetchProjectConnectionsList.mockResolvedValue([connection("conn-1", "Production")]);

    renderPage("?project_id=project-1&extension_page=runs", "viewer");

    await screen.findByText("Only an operator or administrator can load this page's data.");
    expect(mockedExecuteExtensionQuery).not.toHaveBeenCalled();
  });

  it("sends the page connection with a row action, after the row's own fields", async () => {
    const user = userEvent.setup();
    mockedFetchProjectConnectionsList.mockResolvedValue([connection("conn-1", "Production")]);
    mockedExecuteExtensionQuery.mockImplementation(async (_extension, queryId) => (
      queryId === "runs.list"
        ? { query: { status: "succeeded", response: { ok: true, result: { items: [{ run_id: "r-1", connection_id: "conn-evil" }] } } } }
        : {}
    ) as never);

    renderPage("?project_id=project-1&extension_page=runs");

    await user.click(await screen.findByRole("button", { name: "Stop" }));

    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalled());
    const [, actionId, payload] = mockedExecuteExtensionAction.mock.calls[0];
    expect(actionId).toBe("runs.stop");
    expect(payload).toEqual(expect.objectContaining({
      target: expect.objectContaining({ run_id: "r-1", connection_id: "conn-1" }),
    }));
  });
});

describe("page refresh", () => {
  it("reloads the connection page's queries when the person presses Refresh", async () => {
    const user = userEvent.setup();
    mockedFetchProjectConnectionsList.mockResolvedValue([connection("conn-1", "Production")]);

    renderPage("?project_id=project-1&extension_page=runs");

    await waitFor(() => expect(runsQueryCalls()).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(runsQueryCalls()).toHaveLength(2));
    expect(runsQueryCalls()[1][2]).toEqual(expect.objectContaining({ target: { connection_id: "conn-1" } }));
  });
});

describe("URL page parameters", () => {
  it("merges p.<name> keys into every query of the page and drops them on another page", async () => {
    const user = userEvent.setup();
    mockedFetchProjectConnectionsList.mockResolvedValue([connection("conn-1", "Production")]);

    renderPage("?project_id=project-1&extension_page=runs&p.run_id=42&p.Bad=1");

    await waitFor(() => expect(runsQueryCalls()).toHaveLength(1));
    expect(runsQueryCalls()[0][2]).toEqual(expect.objectContaining({ params: { run_id: "42" } }));
    const about = mockedExecuteExtensionQuery.mock.calls.find(([, queryId]) => queryId === "about");
    expect(about?.[2]).toEqual(expect.objectContaining({ params: { run_id: "42" } }));

    await user.click(screen.getByRole("tab", { name: "Profiles" }));
    await waitFor(() => expect(screen.getByTestId("location-probe")).toHaveTextContent("extension_page=profiles"));
    expect(screen.getByTestId("location-probe")).not.toHaveTextContent("p.run_id");
  });
});
