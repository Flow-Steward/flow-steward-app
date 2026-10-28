/**
 * The ways into a new workflow from the list page: Create workflow opens the
 * template catalog with starting from scratch one tab away, Import YAML keeps
 * its own page, an operator only browses templates, and an installation
 * without a catalog gets today's create form directly.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { useCanReadWorkflowRegistry, useIsAdmin } from "@/lib/auth/use-is-admin";
import { fetchJson } from "@/lib/http/client";
import { fetchExtensions } from "@/pages/extensions/api";
import { fetchProjectsList } from "@/pages/projects/list/api";
import {
  createWorkflowFromCatalogItem,
  fetchInstallationVersion,
  fetchWorkflowCatalog,
  fetchWorkflowCatalogAvailability,
  preflightWorkflowCatalogItem,
} from "@/pages/workflows/catalog/api";
import { fetchWorkflowImportOptions } from "@/pages/workflows/import/api";
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

vi.mock("@/pages/workflows/catalog/api", () => ({
  fetchWorkflowCatalog: vi.fn(),
  fetchWorkflowCatalogAvailability: vi.fn(),
  fetchInstallationVersion: vi.fn(),
  preflightWorkflowCatalogItem: vi.fn(),
  createWorkflowFromCatalogItem: vi.fn(),
}));

vi.mock("@/pages/workflows/import/api", () => ({
  fetchWorkflowImportOptions: vi.fn(),
}));

vi.mock("@/pages/extensions/api", () => ({
  fetchExtensions: vi.fn(),
}));

vi.mock("@/pages/projects/list/api", () => ({
  fetchProjectsList: vi.fn(),
}));

const mockedFetchCatalog = vi.mocked(fetchWorkflowCatalog);
const mockedAvailability = vi.mocked(fetchWorkflowCatalogAvailability);
const mockedPreflight = vi.mocked(preflightWorkflowCatalogItem);
const mockedCreate = vi.mocked(createWorkflowFromCatalogItem);
const mockedFetchJson = vi.mocked(fetchJson);

function installCreateHttpFixture(createdWorkflowId = "custom_workflow_1") {
  mockedFetchJson.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method || "GET";
    const membership = /^\/api\/projects\/([^/]+)\/workflows$/.exec(url.pathname);
    if (method === "GET" && membership) {
      return { project_id: decodeURIComponent(membership[1]), workflow_ids: [], operator_default_workflow_id: "", project_workflows: [] } as never;
    }
    if (method === "POST" && url.pathname === "/api/workflows") return { workflow_id: createdWorkflowId } as never;
    throw new Error(`Unexpected fixture request: ${method} ${url.pathname}`);
  });
}

function workflowCreateRequests() {
  return mockedFetchJson.mock.calls.filter(([input, init]) => String(input) === "/api/workflows" && init?.method === "POST");
}


const TEMPLATE = {
  item_id: "tpl",
  version: "1.0.0",
  name: "Template",
  summary: "One template.",
  description: "",
  categories: [],
  tags: [],
  integrations: [],
  trigger_types: [],
  setup_requirements: [],
  workflow_yaml_url: "https://catalog.example.test/tpl.yaml",
  workflow_yaml_bytes: 10,
  workflow_yaml_sha256: "a".repeat(64),
  publication_status: "published",
  verification_status: "verified",
};

function availability(configured: boolean) {
  return {
    kind: "workflow_template_catalog_availability" as const,
    configured,
    available: configured,
    item_count: configured ? 1 : 0,
    stale: false,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

function renderPage(initialEntry = "/registry/workflows") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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

/** The page header, once the page knows whether a catalog exists. */
async function header() {
  await waitFor(() => expect(mockedAvailability).toHaveBeenCalled());
  // Let the availability answer land before a button is read or pressed.
  await act(async () => {
    await new Promise((resolve) => globalThis.setTimeout(resolve, 0));
  });
  return screen.findByRole("banner");
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedFetchJson.mockReset();
  installCreateHttpFixture();
  vi.mocked(useIsAdmin).mockReturnValue(true);
  vi.mocked(useCanReadWorkflowRegistry).mockReturnValue(true);
  vi.mocked(fetchWorkflows).mockResolvedValue([]);
  vi.mocked(fetchInstallationVersion).mockResolvedValue("0.4.0");
  vi.mocked(fetchExtensions).mockResolvedValue([]);
  vi.mocked(fetchProjectsList).mockResolvedValue({ projects: [{ id: "proj_7", name: "Selected project" }], page: 1, per_page: 200, total: 1, total_pages: 1 });
  vi.mocked(fetchWorkflowImportOptions).mockResolvedValue({ kind: "workflow_import_dependency_options", options: {} });
  mockedAvailability.mockResolvedValue(availability(true));
  mockedFetchCatalog.mockResolvedValue({
    kind: "workflow_template_catalog",
    schema_version: "workflow_template_catalog/v1",
    catalog_revision: "r1",
    published_at: "2026-09-18T00:00:00Z",
    availability: availability(true),
    items: [TEMPLATE],
  });
});

test("an administrator gets Import YAML and Create workflow, and no From catalog", async () => {
  renderPage();

  const banner = await header();
  expect(within(banner).getByRole("button", { name: "Create workflow" })).toBeInTheDocument();
  expect(within(banner).getByRole("button", { name: "Import YAML" })).toBeInTheDocument();
  expect(within(banner).queryByRole("button", { name: "From catalog" })).not.toBeInTheDocument();
});

test("Create workflow opens the start-from-scratch form, with Display name first", async () => {
  installCreateHttpFixture("custom_workflow_1");
  renderPage("/registry/workflows?project_id=proj_7");

  await header();
  expect(mockedFetchCatalog).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole("button", { name: "Create workflow" }));

  const dialog = await screen.findByRole("dialog", { name: "Create workflow" });
  expect(within(dialog).queryByRole("tab")).not.toBeInTheDocument();
  await waitFor(() => expect(within(dialog).getByLabelText(/^Display name/)).toHaveFocus());
  fireEvent.change(within(dialog).getByLabelText(/^Display name/), { target: { value: "My workflow" } });
  fireEvent.change(within(dialog).getByLabelText(/^Workflow ID/), { target: { value: "custom_workflow" } });
  expect(within(dialog).getByLabelText("Description")).toHaveClass("fs-textarea");
  fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));

  await waitFor(() =>
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/registry/workflows/custom_workflow_1/builder?project_id=proj_7",
    ),
  );
  expect(workflowCreateRequests()).toHaveLength(1);
  const body = JSON.parse(String(workflowCreateRequests()[0]?.[1]?.body)) as Record<string, unknown>;
  expect(body.project_id).toBe("proj_7");
  expect(body.display_name).toBe("My workflow");
});

test("the Catalog tab browses templates in the page, its search spanning the results", async () => {
  renderPage();

  await header();
  fireEvent.click(await screen.findByRole("tab", { name: "Catalog" }));

  expect(screen.getByRole("tab", { name: "Catalog" })).toHaveAttribute("aria-selected", "true");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(await screen.findByLabelText("Search templates and integrations")).toBeInTheDocument();
  await waitFor(() => expect(mockedFetchCatalog).toHaveBeenCalledTimes(1));
  expect(screen.getByTestId("location")).toHaveTextContent("tab=catalog");
});

test("without a configured catalog there is no Catalog tab", async () => {
  mockedAvailability.mockResolvedValue(availability(false));
  renderPage();

  await header();
  expect(screen.queryByRole("tab", { name: "Catalog" })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "Create workflow" }));
  const dialog = await screen.findByRole("dialog", { name: "Create workflow" });
  expect(within(dialog).getByLabelText(/^Workflow ID/)).toBeInTheDocument();
  expect(mockedFetchCatalog).not.toHaveBeenCalled();
});

test("leaving the Catalog tab leaves no template parameters in the list URL", async () => {
  renderPage("/registry/workflows?enabled=1&tab=catalog&tpl_category=email&tpl_q=supplier&tpl_view=list");

  fireEvent.click(await screen.findByRole("tab", { name: "Workflows" }));

  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/registry/workflows?enabled=1"));
  expect(screen.getByTestId("location")).not.toHaveTextContent("tpl");
});

test("import YAML still navigates to the import page", async () => {
  renderPage();

  await header();
  fireEvent.click(await screen.findByRole("button", { name: "Import YAML" }));

  await waitFor(() =>
    expect(screen.getByTestId("location")).toHaveTextContent("/registry/workflows/import"),
  );
  expect(workflowCreateRequests()).toEqual([]);
});

test("an operator browses templates read-only", async () => {
  vi.mocked(useIsAdmin).mockReturnValue(false);
  renderPage();

  const banner = await header();
  expect(within(banner).queryByRole("button", { name: "Create workflow" })).not.toBeInTheDocument();
  expect(within(banner).queryByRole("button", { name: "Import YAML" })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("tab", { name: "Catalog" }));

  // No Use template for someone who cannot create.
  expect(screen.queryByRole("button", { name: /^Use template/ })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: /^Template/ }));
  // The action-rights rule: the template's page has no Use template either, only why.
  expect(await screen.findByText("Only administrators can create workflows.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Use template" })).not.toBeInTheDocument();
});

test("an operator without a catalog is offered no templates", async () => {
  vi.mocked(useIsAdmin).mockReturnValue(false);
  mockedAvailability.mockResolvedValue(availability(false));
  renderPage();

  await header();
  expect(screen.queryByRole("tab", { name: "Catalog" })).not.toBeInTheDocument();
});

test("Use template from the card sets it up, and the result offers the builder with the project", async () => {
  mockedPreflight.mockResolvedValue({
    schema_version: "1",
    kind: "workflow_import_preflight",
    ok: true,
    can_apply: true,
    summary: { workflow_count: 1, reference_count: 0, dependency_count: 0, blocking_count: 0 },
    workflows: [{ workflow_id: "tpl" }],
    dependencies: [],
    required_resolution_actions: [],
    catalog_item: { item_id: "tpl", version: "1.0.0", name: "Template", setup_requirements: [] },
    catalog_extension_resolutions: [],
  });
  mockedCreate.mockResolvedValue({
    status: "imported",
    workflow_id: "tpl_wf",
    imported_workflows: [],
    preflight: {} as never,
    catalog_item: { item_id: "tpl", version: "1.0.0" },
    catalog_extension_resolutions: [],
  });

  renderPage("/registry/workflows?project_id=proj_7");

  await header();
  fireEvent.click(await screen.findByRole("tab", { name: "Catalog" }));
  // Use template straight from the card goes to setting it up.
  fireEvent.click(await screen.findByRole("button", { name: "Use template: Template" }));
  const create = await screen.findByRole("button", { name: "Create draft" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);

  expect(await screen.findByRole("heading", { name: "Draft created" })).toBeInTheDocument();
  expect(screen.getByTestId("location")).toHaveTextContent("/registry/workflows?");
  fireEvent.click(screen.getByRole("button", { name: "Open in builder" }));

  await waitFor(() =>
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/registry/workflows/tpl_wf/builder?project_id=proj_7",
    ),
  );
});

test("the Catalog tab closes an open template and returns to the results", async () => {
  renderPage("/registry/workflows?tab=catalog");

  await header();
  fireEvent.click(await screen.findByRole("button", { name: /^Template/ }));
  expect(await screen.findByRole("button", { name: "Use template" })).toBeInTheDocument();

  fireEvent.click(screen.getByRole("tab", { name: "Catalog" }));

  expect(await screen.findByRole("button", { name: /^Template/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Use template" })).not.toBeInTheDocument();
  expect(screen.getByTestId("location")).not.toHaveTextContent("tpl=");
});
