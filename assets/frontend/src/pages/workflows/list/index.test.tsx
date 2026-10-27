import type * as HttpClient from "@/lib/http/client";
import type * as WorkflowApi from "@/pages/workflows/shared/api";
import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { useCanReadWorkflowRegistry, useIsAdmin } from "@/lib/auth/use-is-admin";
import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { WorkflowsListPage } from "@/pages/workflows/list";
import { fetchWorkflows } from "@/pages/workflows/shared/api";

vi.mock("@/lib/auth/use-is-admin", () => ({
  useCanReadWorkflowRegistry: vi.fn(),
  useIsAdmin: vi.fn(),
}));

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflows: vi.fn(),
}));

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

// No template catalog here, so Create workflow opens the plain form these
// tests are about; the catalog paths are covered in create-paths.test.tsx.
vi.mock("@/pages/workflows/catalog/api", () => ({
  fetchWorkflowCatalogAvailability: vi.fn(async () => ({ configured: false })),
  fetchWorkflowCatalog: vi.fn(),
  fetchInstallationVersion: vi.fn(),
  preflightWorkflowCatalogItem: vi.fn(),
  createWorkflowFromCatalogItem: vi.fn(),
}));

const mockedUseIsAdmin = vi.mocked(useIsAdmin);
const mockedUseCanReadWorkflowRegistry = vi.mocked(useCanReadWorkflowRegistry);
const mockedFetchWorkflows = vi.mocked(fetchWorkflows);
const mockedFetchJson = vi.mocked(fetchJson);

/**
 * Finds one element by a property of itself rather than by walking the tree:
 * `closest` and `querySelector` are DOM traversal, which the Testing Library
 * rules reject, and rightly — a test that walks upward is asserting a shape it
 * did not name.
 */
function elementBy(predicate: (element: Element) => boolean) {
  return screen.getByText((_, element) => Boolean(element && predicate(element)));
}

function renderPage(initialEntry = "/registry/workflows") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<WorkflowsListPage />} path="/registry/workflows" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="location">{`${location.pathname}${location.search}`}</output>
  );
}

function renderPageWithLocation(initialEntry = "/registry/workflows") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            element={
              <>
                <WorkflowsListPage />
                <LocationProbe />
              </>
            }
            path="/registry/workflows"
          />
          <Route element={<LocationProbe />} path="*" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("WorkflowsListPage", () => {
  it("keeps the actual catalog search focused and loaded rows visible during its20s HTTP refresh", async () => {
    const actualApi = await vi.importActual<typeof WorkflowApi>("@/pages/workflows/shared/api");
    const actualHttp = await vi.importActual<typeof HttpClient>("@/lib/http/client");
    const previousHttp = mockedFetchJson.getMockImplementation();
    mockedFetchJson.mockImplementation(actualHttp.fetchJson);
    mockedFetchWorkflows.mockImplementation(actualApi.fetchWorkflows);
    const rows = [{ workflow_id: "nightly_reconciliation", display_name: "Nightly reconciliation", enabled: true }];
    let pending: ((response: Response) => void) | undefined;
    let calls = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      expect(url.pathname).toBe("/api/workflows");
      expect(url.searchParams.get("search")).toBe("nightly");
      calls += 1;
      if (calls > 1) return new Promise<Response>((resolve) => { pending = resolve; });
      return new Response(JSON.stringify(rows), { headers: { "content-type": "application/json" } });
    });
    vi.useFakeTimers();
    const view = renderPage("/registry/workflows?search=nightly");
    const { unmount: unmountIndicator } = render(<RequestLoadingIndicator />);
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      const field = screen.getByRole("searchbox", { name: "Search workflows" });
      field.focus();
      const row = screen.getByRole("link", { name: "Nightly reconciliation" });
      await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(pending).toBeDefined();
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      expect(screen.getByRole("searchbox", { name: "Search workflows" })).toBe(field);
      expect(field).toHaveValue("nightly"); expect(field).toHaveFocus();
      expect(screen.getByRole("link", { name: "Nightly reconciliation" })).toBe(row);
      await act(async () => { pending?.(new Response(JSON.stringify([...rows, { workflow_id: "nightly_extra", display_name: "Nightly extra", enabled: true }]), { headers: { "content-type": "application/json" } })); await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByRole("link", { name: "Nightly extra" })).toBeInTheDocument();
      expect(field).toHaveValue("nightly"); expect(field).toHaveFocus();
    } finally {
      view.unmount(); unmountIndicator(); pending?.(new Response("[]"));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      vi.clearAllTimers(); vi.useRealTimers(); fetchMock.mockRestore();
      mockedFetchJson.mockReset(); if (previousHttp) mockedFetchJson.mockImplementation(previousHttp);
    }
  });

  beforeEach(() => {
    mockedUseIsAdmin.mockReset();
    mockedUseCanReadWorkflowRegistry.mockReset();
    mockedFetchWorkflows.mockReset();
    mockedUseIsAdmin.mockReturnValue(true);
    mockedUseCanReadWorkflowRegistry.mockReturnValue(true);
    mockedFetchWorkflows.mockResolvedValue([
      {
        description: "Runs the nightly reconciliation.",
        display_name: "Nightly reconciliation",
        enabled: true,
        portability: {
          clone_allowed: true,
          full_export_allowed: true,
          structure_change_allowed: true,
        },
        workflow_id: "nightly_reconciliation",
      },
    ] as never);
  });

  // Two renders, not one: the id has to hold in both states, and rendering the
  // page twice into one document would put two nodes under the same id.
  it("carries the catalog id when the catalog is empty", async () => {
    // The UI lane waits on this id before it asserts anything. On the empty-state
    // block alone that wait could only ever pass while the catalog was empty.
    mockedFetchWorkflows.mockResolvedValue([] as never);
    renderPage();

    await screen.findByText("No workflows found");
    const card = elementBy((element) => element.id === "workflows-catalog-grid");
    expect(within(card).getByText("No workflows found")).toBeInTheDocument();
    const table = within(card).getByRole("table", { name: "Workflows catalog" });
    expect(within(table).queryByRole("columnheader", { name: "ID" })).not.toBeInTheDocument();
    expect(within(table).getAllByRole("columnheader")).toHaveLength(6);
    expect(within(table).queryByRole("cell")).not.toBeInTheDocument();
  });

  it("offers View workflow on a protected extension workflow's row", async () => {
    mockedFetchWorkflows.mockResolvedValue([
      {
        display_name: "Hook sample",
        enabled: true,
        portability: {
          clone_allowed: false,
          extension_owned: true,
          full_export_allowed: false,
          protected: true,
          reference_export_allowed: true,
          structure_change_allowed: false,
        },
        workflow_id: "extension.inst.hook_sample",
      },
    ] as never);
    renderPageWithLocation();

    // B15: the row menu had no way into the builder, which is where a protected
    // workflow is read and where its hooks are connected.
    fireEvent.click(await screen.findByRole("button", { name: "Actions for extension.inst.hook_sample" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).queryByRole("menuitem", { name: "Builder" })).not.toBeInTheDocument();
    fireEvent.click(within(menu).getByRole("menuitem", { name: "View workflow" }));

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/registry/workflows/extension.inst.hook_sample/builder",
    );
  });

  it("names the providing extension on the Source badge, never its install id", async () => {
    mockedFetchWorkflows.mockResolvedValue([
      {
        display_name: "Hook sample",
        enabled: true,
        portability: {
          extension_owned: true,
          protected: true,
          source_extension_id: "acme.hook-sample",
          source_extension_name: "Hook Sample",
          source_extension_version: "1.0.1",
          source_install_id: "4f054cc7-e9c4-44f4-9e44-6d90fc58f255",
          structure_change_allowed: false,
        },
        workflow_id: "extension.inst.hook_sample",
      },
    ] as never);
    renderPage();

    // B13: the row's source detail was the install UUID.
    expect(await screen.findByTitle("Provided by extension: Hook Sample 1.0.1")).toBeInTheDocument();
    expect(screen.queryByTitle(/4f054cc7/)).not.toBeInTheDocument();
  });

  it("offers Builder, not View workflow, on an ordinary workflow's row", async () => {
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Actions for nightly_reconciliation" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: "Builder" })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "View workflow" })).not.toBeInTheDocument();
  });

  it("carries the catalog id when the catalog has rows", async () => {
    renderPage();

    expect(await screen.findByText("Nightly reconciliation")).toBeInTheDocument();
    const card = elementBy((element) => element.id === "workflows-catalog-grid");
    expect(within(card).getByText("Nightly reconciliation")).toBeInTheDocument();
    const table = within(card).getByRole("table", { name: "Workflows catalog" });
    expect(within(table).queryByRole("columnheader", { name: "ID" })).not.toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Nightly reconciliation" })).toHaveAttribute("href", "/registry/workflows/nightly_reconciliation");
    expect(within(table).getByRole("button", { name: "Actions for nightly_reconciliation" })).toBeInTheDocument();
  });

  it("lists an extension workflow that cannot be offered as Unavailable, with its reason", async () => {
    const reason =
      "Needs the extension acme.required-warehouse, which this account does not have active. Install it from Extensions > Catalog, or turn it on if it is installed.";
    mockedFetchWorkflows.mockResolvedValue([
      {
        availability_status: "unavailable",
        availability_reason_text: reason,
        description: "Runs a saved dataset query.",
        display_name: "Demo: Dataset Open Orders",
        enabled: false,
        portability: {},
        source_install_id: "install-a",
        workflow_id: "extension.install-a.demo_dataset_open_orders",
      },
    ] as never);
    renderPage();

    const table = await screen.findByRole("table", { name: "Workflows catalog" });
    expect(await within(table).findByText("Demo: Dataset Open Orders")).toBeInTheDocument();
    expect(within(table).getByText("unavailable")).toBeInTheDocument();
    expect(within(table).getByText(reason)).toBeInTheDocument();
    expect(within(table).queryByText("Runs a saved dataset query.")).not.toBeInTheDocument();
  });

  it("names a missing extension in one short line instead of the backend sentence", async () => {
    const reason =
      "Needs the extension acme.data-connector-postgres, which this account does not have active. Install it from Extensions > Catalog, or turn it on if it is installed.";
    mockedFetchWorkflows.mockResolvedValue([
      {
        availability_status: "unavailable",
        availability_reason_text: reason,
        missing_extension_details: [
          {
            extension_id: "acme.data-connector-postgres",
            display_name: "PostgreSQL (Google MCP Toolbox)",
            state: "not_installed",
          },
        ],
        description: "Runs a saved dataset query.",
        display_name: "Demo: Dataset Open Orders",
        enabled: false,
        portability: {},
        source_install_id: "install-a",
        workflow_id: "extension.install-a.demo_dataset_open_orders",
      },
    ] as never);
    renderPage();

    const table = await screen.findByRole("table", { name: "Workflows catalog" });
    expect(await within(table).findByText("Can't run yet: needs PostgreSQL (Google MCP Toolbox).")).toBeInTheDocument();
    expect(within(table).queryByText(reason)).not.toBeInTheDocument();
    expect(within(table).queryByText(/flowsteward\.data-connector/)).not.toBeInTheDocument();
  });

  it("opens the create dialog without offering an invalid enabled state", async () => {
    renderPage();
    expect(await screen.findByText("Nightly reconciliation")).toBeInTheDocument();

    // The header owns the create action, as it does on Projects.
    const header = elementBy((element) => element.classList.contains("fs-page-header"));
    const create = within(header).getByRole("button", { name: "Create workflow" });

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(create);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText(/^Workflow ID/)).toHaveClass("fs-input");
    expect(within(dialog).getByLabelText(/^Display name/)).toHaveClass("fs-input");
    expect(within(dialog).getByLabelText("Description")).toHaveClass("fs-textarea");
    expect(within(dialog).queryByRole("checkbox", { name: "Enabled" })).not.toBeInTheDocument();
  });

  it("reports an invalid workflow id under the field, not as a server alert", async () => {
    renderPage();
    expect(await screen.findByText("Nightly reconciliation")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create workflow" }));

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Display name/), { target: { value: "My workflow" } });
    fireEvent.change(within(dialog).getByLabelText(/^Workflow ID/), {
      target: { value: "9_bad_start" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));

    const field = within(dialog).getByLabelText(/^Workflow ID/);
    await waitFor(() => {
      expect(field).toHaveAttribute("aria-invalid", "true");
    });
    // A rule about one input belongs under that input; an alert is for what the
    // server said.
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps the search and enabled-only controls on the published roles", async () => {
    renderPage("/registry/workflows?search=nightly&enabled=1");
    expect(await screen.findByText("Nightly reconciliation")).toBeInTheDocument();

    const searchContainer = elementBy((element) => element.classList.contains("fs-search"));
    const search = within(searchContainer).getByLabelText("Search workflows");
    expect(search).toHaveAttribute("type", "search");
    expect(search).toHaveAttribute("id", "search");
    expect(search).toHaveValue("nightly");

    // The status narrowing is an applied filter chip, not a checkbox parked next
    // to a heading, and it is readable straight from the URL.
    expect(screen.getByText("Status · Enabled only")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove Status filter" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add filter" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear all" })).toBeInTheDocument();
  });

  it("drops the enabled parameter when the filter is cleared", async () => {
    renderPage("/registry/workflows?enabled=1");
    expect(await screen.findByText("Nightly reconciliation")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove Status filter" }));

    await waitFor(() => {
      expect(screen.queryByText("Status · Enabled only")).not.toBeInTheDocument();
    });
    // The query follows the URL, so the unfiltered catalog is fetched again.
    await waitFor(() => {
      expect(mockedFetchWorkflows).toHaveBeenLastCalledWith(
        expect.objectContaining({ enabledOnly: false }),
        { presentation: "background" },
      );
    });
  });

  it("tells a viewer the catalog is restricted instead of firing the request", async () => {
    mockedUseCanReadWorkflowRegistry.mockReturnValue(false);
    mockedUseIsAdmin.mockReturnValue(false);
    renderPage();

    expect(
      await screen.findByText(/The workflow registry catalog is available to administrators/),
    ).toBeInTheDocument();
    expect(mockedFetchWorkflows).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Create workflow" })).not.toBeInTheDocument();
  });
});

/**
 * The four cases below are RBAC and parity, not markup: which roles are offered
 * an authoring control, what a refused catalog read shows, and where a created
 * workflow lands. They were asserted before the design-system transfer and are
 * asserted here for the transferred page, because none of them is about the
 * design system and all four are the kind of thing a restyle silently drops.
 */
describe("WorkflowsListPage RBAC and create parity", () => {
  beforeEach(() => {
    mockedUseIsAdmin.mockReset();
    mockedUseCanReadWorkflowRegistry.mockReset();
    mockedFetchWorkflows.mockReset();
    mockedFetchJson.mockReset();
    mockedUseIsAdmin.mockReturnValue(true);
    mockedUseCanReadWorkflowRegistry.mockReturnValue(true);
    mockedFetchWorkflows.mockResolvedValue([] as never);
    mockedFetchJson.mockResolvedValue({} as never);
  });

  it("requires a display name, then a workflow id, before posting", async () => {
    renderPage();
    await screen.findByText("No workflows found");
    fireEvent.click(screen.getByRole("button", { name: "Create workflow" }));

    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await within(dialog).findByText("Display name is required.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^Display name/)).toHaveAttribute("aria-invalid", "true");

    // A name with no Latin letters makes no ID, so the ID is asked for.
    fireEvent.change(within(dialog).getByLabelText(/^Display name/), { target: { value: "Заказы" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await within(dialog).findByText("Workflow ID is required.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^Workflow ID/)).toHaveAttribute("aria-invalid", "true");
    expect(mockedFetchJson).not.toHaveBeenCalled();
  });

  it("makes the workflow id from the display name until the id is typed", async () => {
    renderPage();
    await screen.findByText("No workflows found");
    fireEvent.click(screen.getByRole("button", { name: "Create workflow" }));

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Display name/), { target: { value: "Order Sync (EU)" } });
    expect(within(dialog).getByLabelText(/^Workflow ID/)).toHaveValue("order_sync_eu");
    fireEvent.change(within(dialog).getByLabelText(/^Workflow ID/), { target: { value: "orders" } });
    fireEvent.change(within(dialog).getByLabelText(/^Display name/), { target: { value: "Order Sync" } });
    expect(within(dialog).getByLabelText(/^Workflow ID/)).toHaveValue("orders");
  });

  it("opens the builder for the workflow id the create response returns", async () => {
    // Not the id that was typed: the backend may hand back a different one, and
    // the page has to follow the response.
    mockedFetchJson.mockResolvedValue({ workflow_id: "custom_workflow_1" } as never);
    renderPageWithLocation();
    await screen.findByText("No workflows found");
    fireEvent.click(screen.getByRole("button", { name: "Create workflow" }));

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Display name/), { target: { value: "My workflow" } });
    fireEvent.change(within(dialog).getByLabelText(/^Workflow ID/), {
      target: { value: "custom_workflow" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/registry/workflows/custom_workflow_1/builder",
      );
    });
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/workflows", expect.objectContaining({ method: "POST" }));
  });

  it("creates and opens a workflow in the selected project context", async () => {
    mockedFetchJson.mockResolvedValue({ workflow_id: "project_workflow" } as never);
    renderPageWithLocation("/registry/workflows?project_id=project-123");
    await screen.findByText("No workflows found");
    fireEvent.click(screen.getByRole("button", { name: "Create workflow" }));

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Display name/), { target: { value: "My workflow" } });
    fireEvent.change(within(dialog).getByLabelText(/^Workflow ID/), {
      target: { value: "project_workflow" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/registry/workflows/project_workflow/builder?project_id=project-123",
      );
    });
    const request = vi.mocked(mockedFetchJson).mock.calls[0]?.[1];
    const body = JSON.parse(String(request?.body)) as Record<string, unknown>;
    expect(body.project_id).toBe("project-123");
  });

  it("lets the backend create a new workflow disabled by default", async () => {
    renderPage();
    await screen.findByText("No workflows found");
    fireEvent.click(screen.getByRole("button", { name: "Create workflow" }));

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Display name/), { target: { value: "My workflow" } });
    fireEvent.change(within(dialog).getByLabelText(/^Workflow ID/), {
      target: { value: "disabled_draft" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalled());
    const request = vi.mocked(mockedFetchJson).mock.calls[0]?.[1];
    const body = JSON.parse(String(request?.body)) as Record<string, unknown>;
    expect(body).not.toHaveProperty("enabled");
  });

  it("offers an operator no authoring control", async () => {
    mockedUseIsAdmin.mockReturnValue(false);
    renderPage();

    await waitFor(() => expect(mockedFetchWorkflows).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Create workflow" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Import YAML" })).not.toBeInTheDocument();
  });

  it("shows the restricted message when an operator's catalog read is refused", async () => {
    // Operators may read the catalog, so the query fires. A 403 from the backend
    // is still the restricted message, not a raw error and not an empty catalog.
    mockedUseIsAdmin.mockReturnValue(false);
    mockedFetchWorkflows.mockRejectedValue(
      new HttpError({
        payload: { error: "Operator role required" },
        status: 403,
        url: "/api/workflows",
      }),
    );
    renderPage();

    expect(
      await screen.findByText(/The workflow registry catalog is available to administrators/),
    ).toBeInTheDocument();
    expect(screen.queryByText("No workflows found")).not.toBeInTheDocument();
  });

  // IMPORT-001: the button navigated to the import page without the project the
  // operator was looking at, and that page resolves project connections against
  // it. A deep link carried the project; ordinary navigation dropped it.
  it("carries the project into the import page", async () => {
    renderPageWithLocation("/registry/workflows?project_id=project-123");
    await screen.findByText("No workflows found");

    fireEvent.click(screen.getByRole("button", { name: "Import YAML" }));

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/registry/workflows/import?project_id=project-123",
      );
    });
  });

  it("opens the import page without a project when there was none to carry", async () => {
    renderPageWithLocation();
    await screen.findByText("No workflows found");

    fireEvent.click(screen.getByRole("button", { name: "Import YAML" }));

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/registry/workflows/import");
    });
  });
});
