import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router";

import { fetchExtensions } from "@/pages/extensions/api";
import { fetchProjectsList } from "@/pages/projects/list/api";
import {
  createWorkflowFromCatalogItem,
  fetchInstallationVersion,
  fetchWorkflowCatalog,
  preflightWorkflowCatalogItem,
  type WorkflowCatalogIndex,
  type WorkflowCatalogItem,
  type WorkflowCatalogPreflight,
} from "@/pages/workflows/catalog/api";
import { type CatalogTab, WorkflowCatalogDialog } from "@/pages/workflows/catalog/catalog-dialog";
import { fetchWorkflowImportOptions } from "@/pages/workflows/import/api";
import { fetchWorkflows } from "@/pages/workflows/shared/api";

// These page journeys take about 6 s when the quality gate shares the machine,
// over vitest's 5 s default. Per-test timeouts set below still take precedence.
vi.setConfig({ testTimeout: 10_000 });

vi.mock("@/pages/workflows/catalog/api", () => ({
  fetchWorkflowCatalog: vi.fn(),
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

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflows: vi.fn(),
}));

const mockedFetchCatalog = vi.mocked(fetchWorkflowCatalog);
const mockedVersion = vi.mocked(fetchInstallationVersion);
const mockedPreflight = vi.mocked(preflightWorkflowCatalogItem);
const mockedCreate = vi.mocked(createWorkflowFromCatalogItem);
const mockedImportOptions = vi.mocked(fetchWorkflowImportOptions);
const mockedExtensions = vi.mocked(fetchExtensions);

const CATEGORIES = ["productivity", "sales", "support", "engineering", "finance"];
const INTEGRATIONS = ["gmail", "slack", "acme_shop", "acme_cms", "postgres", "webhook"];
const TRIGGERS = ["manual", "schedule", "webhook", "event"];

/** Mirrors `tests/fixtures/catalogs/synthetic_catalogs.py` so both sides scale-test alike. */
function syntheticItem(index: number): WorkflowCatalogItem {
  const category = CATEGORIES[index % CATEGORIES.length];
  const integration = INTEGRATIONS[index % INTEGRATIONS.length];
  const trigger = TRIGGERS[index % TRIGGERS.length];
  const itemId = `synthetic_template_${String(index).padStart(4, "0")}`;
  return {
    item_id: itemId,
    version: `1.${index % 12}.${index % 5}`,
    name: `Synthetic template ${String(index).padStart(4, "0")}`,
    summary: `A ${category} template driven by ${integration}.`,
    description: `Synthetic catalog entry ${index}.`,
    categories: [category],
    tags: [category, integration, trigger],
    integrations: [integration],
    trigger_types: [trigger],
    setup_requirements: [
      { kind: "extension", label: `${integration} extension`, ref: `synthetic_${integration}`, required: true },
    ],
    workflow_yaml_url: `https://catalog.example.test/${itemId}.yaml`,
    workflow_yaml_bytes: 1024 + index,
    workflow_yaml_sha256: "a".repeat(64),
    publication_status: "published",
    verification_status: "verified",
    bundle_schema: "platform.workflow_bundle.v2",
    importable: true,
  };
}

function catalogIndex(count: number, overrides: Partial<WorkflowCatalogIndex> = {}) {
  return {
    kind: "workflow_template_catalog",
    schema_version: "workflow_template_catalog/v1",
    catalog_revision: "r1",
    published_at: "2026-09-18T00:00:00Z",
    availability: {
      kind: "workflow_template_catalog_availability",
      configured: true,
      available: true,
      item_count: count,
      stale: false,
    },
    items: Array.from({ length: count }, (_value, index) => syntheticItem(index)),
    ...overrides,
  } as WorkflowCatalogIndex;
}

function preflight(overrides: Partial<WorkflowCatalogPreflight> = {}) {
  return {
    schema_version: "1",
    kind: "workflow_import_preflight",
    ok: true,
    can_apply: true,
    summary: { workflow_count: 1, reference_count: 0, dependency_count: 0, blocking_count: 0 },
    workflows: [{ workflow_id: "synthetic_template_0000", display_name: "Synthetic template 0000" }],
    dependencies: [],
    required_resolution_actions: [],
    catalog_item: {
      item_id: "synthetic_template_0000",
      version: "1.0.0",
      name: "Synthetic template 0000",
      setup_requirements: [],
    },
    catalog_extension_resolutions: [],
    ...overrides,
  } as WorkflowCatalogPreflight;
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.search}</output>;
}

type DialogProps = Partial<Parameters<typeof WorkflowCatalogDialog>[0]> & { url?: string; basename?: string };

function openDialog(props: DialogProps = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const handlers = {
    onClose: props.onClose ?? vi.fn(),
    onOpenBuilder: props.onOpenBuilder ?? vi.fn(),
    onViewWorkflow: props.onViewWorkflow ?? vi.fn(),
    onTabChange: props.onTabChange ?? vi.fn(),
  };
  function Harness() {
    return (
      <WorkflowCatalogDialog
        configured={props.configured ?? true}
        onClose={handlers.onClose}
        onOpenBuilder={handlers.onOpenBuilder}
        onTabChange={handlers.onTabChange}
        onViewWorkflow={handlers.onViewWorkflow}
        open={props.open ?? true}
        projectId={props.projectId}
        readOnly={props.readOnly}
        tab={(props.tab ?? "template") as CatalogTab}
      />
    );
  }
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter basename={props.basename} initialEntries={[props.url ?? "/registry/workflows?create=template"]}>
        <Harness />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return handlers;
}

function escape() {
  fireEvent.keyDown(document, { key: "Escape" });
}

async function openTemplate(name: RegExp) {
  // The card's name starts with the template's; "Use template: …" beside it does not.
  fireEvent.click(await screen.findByRole("button", { name: new RegExp(`^${name.source}`, name.flags) }));
  return screen.findByRole("heading", { level: 3, name });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedFetchCatalog.mockResolvedValue(catalogIndex(3));
  mockedVersion.mockResolvedValue("0.4.0");
  mockedExtensions.mockResolvedValue([{ extension_id: "synthetic_gmail" }]);
  vi.mocked(fetchProjectsList).mockResolvedValue({ projects: [], page: 1, per_page: 200, total: 0, total_pages: 1 });
  vi.mocked(fetchWorkflows).mockResolvedValue([]);
  mockedPreflight.mockResolvedValue(preflight());
  mockedImportOptions.mockResolvedValue({
    kind: "workflow_import_dependency_options",
    options: {
      child_workflow: [{ value: "stock_import_supplier_a_2", label: "Stock import: supplier A" }],
    },
  });
  mockedCreate.mockResolvedValue({
    status: "imported",
    workflow_id: "synthetic_template_0000",
    imported_workflows: [],
    preflight: preflight(),
    catalog_item: { item_id: "synthetic_template_0000", version: "1.0.0" },
    catalog_extension_resolutions: [],
  });
});

test("the catalog loads once and opens on the search", async () => {
  openDialog();

  expect(await screen.findByText("Synthetic template 0000")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "3 templates" })).toBeInTheDocument();
  expect(mockedFetchCatalog).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(screen.getByLabelText("Search templates and integrations")).toHaveFocus());
});

test("a card names its integrations, what it includes and at most one badge", async () => {
  const index = catalogIndex(2, { integration_labels: { gmail: "Google Mail" } });
  index.items[0].includes = [
    { item_id: "fetch_messages", name: "Fetch messages" },
    { item_id: "file_attachments", name: "File attachments" },
  ];
  mockedFetchCatalog.mockResolvedValue(index);
  openDialog();

  // The card is an article named by the template; its name is the one button inside.
  const first = await screen.findByRole("article", { name: "Synthetic template 0000" });
  expect(within(first).getByText("Google Mail")).toBeInTheDocument();
  expect(within(first).getByText("v1.0.0")).toBeInTheDocument();
  expect(within(first).getByText("Includes 2 workflows")).toBeInTheDocument();
  // gmail is installed; slack is not.
  expect(within(first).queryByText("Needs extension")).not.toBeInTheDocument();
  const second = screen.getByRole("article", { name: "Synthetic template 0001" });
  expect(await within(second).findByText("Needs extension")).toBeInTheDocument();
  expect(within(second).queryByText(/Includes/)).not.toBeInTheDocument();
});

test("facets narrow with OR inside and AND between, and live in the URL", async () => {
  mockedFetchCatalog.mockResolvedValue(catalogIndex(12, { integration_labels: { slack: "Slack Workspace" } }));
  openDialog();

  await screen.findByRole("heading", { name: "12 templates" });
  const integration = screen.getByRole("group", { name: "Integration" });
  fireEvent.click(within(integration).getByRole("checkbox", { name: /Slack Workspace/ }));
  expect(await screen.findByRole("heading", { name: "2 templates" })).toBeInTheDocument();
  fireEvent.click(within(integration).getByRole("checkbox", { name: /gmail/ }));
  expect(await screen.findByRole("heading", { name: "4 templates" })).toBeInTheDocument();
  expect(screen.getByTestId("location")).toHaveTextContent("tpl_integration=slack%2Cgmail");

  // Slack sits on items 1 and 7, gmail on 0 and 6; schedule is every 4th from 1.
  const trigger = screen.getByRole("group", { name: "Trigger" });
  fireEvent.click(within(trigger).getByRole("checkbox", { name: /Schedule/ }));
  expect(await screen.findByRole("heading", { name: "1 template" })).toBeInTheDocument();

  expect(screen.getByText("Integration · Slack Workspace, gmail")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
  expect(await screen.findByRole("heading", { name: "12 templates" })).toBeInTheDocument();
});

test("the URL restores search, filters and view", async () => {
  mockedFetchCatalog.mockResolvedValue(catalogIndex(12));
  openDialog({ url: "/registry/workflows?create=template&tpl_trigger=webhook&tpl_view=list" });

  expect(await screen.findByRole("heading", { name: "3 templates" })).toBeInTheDocument();
  expect(screen.getByRole("table", { name: "Templates" })).toBeInTheDocument();
  const view = screen.getByRole("group", { name: "View" });
  expect(within(view).getByRole("button", { name: "Table" })).toHaveAttribute("aria-pressed", "true");
  expect(within(view).getByRole("button", { name: "Cards" })).toHaveAttribute("aria-pressed", "false");
});

test("search matches integration names", async () => {
  mockedFetchCatalog.mockResolvedValue(catalogIndex(12, { integration_labels: { slack: "Slack Workspace" } }));
  openDialog();

  await screen.findByRole("heading", { name: "12 templates" });
  fireEvent.change(screen.getByLabelText("Search templates and integrations"), {
    target: { value: "slack workspace" },
  });
  expect(await screen.findByRole("heading", { name: "2 templates" })).toBeInTheDocument();
});

test("one template has nothing to sort, and keeps the filter rail", async () => {
  mockedFetchCatalog.mockResolvedValue(catalogIndex(1));
  openDialog();

  await screen.findByRole("heading", { name: "1 template" });
  expect(screen.queryByRole("combobox", { name: "Sort" })).not.toBeInTheDocument();
  expect(screen.getByRole("navigation", { name: "Template filters" })).toBeInTheDocument();
});

test("1,000 templates page at 24, without another request", async () => {
  mockedFetchCatalog.mockResolvedValue(catalogIndex(1000));
  openDialog();

  await screen.findByRole("heading", { name: "1000 templates" });
  expect(within(screen.getByRole("list", { name: "Templates" })).getAllByRole("listitem")).toHaveLength(24);
  const pager = screen.getByRole("navigation", { name: "Template pages" });
  // The design's pager: the range and the arrows, no page numbers.
  expect(within(pager).getByText("1–24 of 1000")).toBeInTheDocument();
  expect(within(pager).queryByRole("button", { name: "Page 1" })).not.toBeInTheDocument();
  expect(within(pager).getByRole("button", { name: "Previous page" })).toBeDisabled();
  fireEvent.click(within(pager).getByRole("button", { name: "Next page" }));
  expect(await screen.findByText("Synthetic template 0024")).toBeInTheDocument();
  expect(screen.getByTestId("location")).toHaveTextContent("tpl_page=2");
  expect(within(pager).getByText("25–48 of 1000")).toBeInTheDocument();
  expect(mockedFetchCatalog).toHaveBeenCalledTimes(1);
}, 30_000);

test("the table pages by its own Rows per page, 25 or 50", async () => {
  mockedFetchCatalog.mockResolvedValue(catalogIndex(60));
  openDialog({ url: "/registry/workflows?create=template&tpl_view=list" });

  await screen.findByRole("heading", { name: "60 templates" });
  const table = screen.getByRole("table", { name: "Templates" });
  expect(within(table).getAllByRole("row")).toHaveLength(26);
  const pager = screen.getByRole("navigation", { name: "Template pages" });
  expect(within(pager).getByText("1–25 of 60")).toBeInTheDocument();
  fireEvent.click(within(pager).getByRole("combobox", { name: "Rows per page" }));
  fireEvent.click(await screen.findByRole("option", { name: "50" }));
  expect(await within(pager).findByText("1–50 of 60")).toBeInTheDocument();
  expect(within(screen.getByRole("table", { name: "Templates" })).getAllByRole("row")).toHaveLength(51);
}, 30_000);

test("browse → details → setup → result, and Escape steps back", async () => {
  const user = userEvent.setup();
  const handlers = openDialog();

  await openTemplate(/Synthetic template 0000/);
  expect(screen.getByTestId("location")).toHaveTextContent("tpl=synthetic_template_0000");
  expect(mockedPreflight).not.toHaveBeenCalled();
  escape();
  expect(await screen.findByRole("heading", { name: "3 templates" })).toBeInTheDocument();
  expect(handlers.onClose).not.toHaveBeenCalled();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  expect(await screen.findByRole("heading", { name: "Review" })).toBeInTheDocument();
  expect(mockedPreflight).toHaveBeenCalledWith("synthetic_template_0000", {
    projectId: undefined,
    targetWorkflowId: undefined,
  });
  expect(screen.getByText(/Creates 1 disabled draft/)).toBeInTheDocument();
  expect(screen.getByText(/Nothing runs until you enable it/)).toBeInTheDocument();

  escape();
  expect(await screen.findByRole("button", { name: "Use template" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const create = await screen.findByRole("button", { name: "Create draft" });
  await waitFor(() => expect(create).toBeEnabled());
  // Await the async create lifecycle, including the dialog's busy-state effect,
  // before exercising the document-level Escape handler on the result screen.
  await user.click(create);

  expect(await screen.findByRole("heading", { name: "Draft created" })).toBeInTheDocument();
  expect(screen.getByRole("dialog")).toHaveAttribute("aria-busy", "false");
  expect(mockedCreate).toHaveBeenCalledWith("synthetic_template_0000", {
    projectId: undefined,
    targetWorkflowId: undefined,
    resolutions: [],
  });
  fireEvent.click(screen.getByRole("button", { name: "Open in builder" }));
  expect(handlers.onOpenBuilder).toHaveBeenCalledWith("synthetic_template_0000", "");
  fireEvent.click(screen.getByRole("button", { name: "View workflow" }));
  expect(handlers.onViewWorkflow).toHaveBeenCalledWith("synthetic_template_0000");

  // The result is a first screen again: Escape closes.
  escape();
  expect(handlers.onClose).toHaveBeenCalledTimes(1);
});

test("names the optional Project selector from its visible label and preserves project selection", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchProjectsList).mockResolvedValue({
    projects: [{ id: "project-platform", name: "Platform" }],
    page: 1, per_page: 200, total: 1, total_pages: 1,
  });
  openDialog();
  await openTemplate(/Synthetic template 0000/);
  await user.click(screen.getByRole("button", { name: "Use template" }));
  await screen.findByRole("heading", { name: "Review" });

  const project = screen.getByRole("combobox", { name: "Project (optional)" });
  expect(project).toHaveTextContent("No project");
  await user.click(project);
  await user.click(await screen.findByRole("option", { name: "Platform" }));
  expect(project).toHaveTextContent("Platform");
  await waitFor(() => expect(mockedPreflight).toHaveBeenLastCalledWith("synthetic_template_0000", {
    projectId: "project-platform", targetWorkflowId: "synthetic_template_0000",
  }));
});

test("a simple catalog draft can recheck after an edit is restored or only whitespace changes", async () => {
  openDialog();
  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const create = await screen.findByRole("button", { name: "Create draft" });
  await waitFor(() => expect(create).toBeEnabled());
  const id = screen.getByLabelText("Workflow ID");
  const original = (id as HTMLInputElement).value;
  fireEvent.change(id, { target: { value: "temporary_id" } });
  fireEvent.change(id, { target: { value: original } });
  expect(create).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.change(id, { target: { value: `${original} ` } });
  expect(create).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(create).toBeEnabled());
  expect(mockedPreflight).toHaveBeenCalledTimes(3);
});

test("a late Create success cannot replace a newer catalog draft", async () => {
  let acceptCreate!: (value: Awaited<ReturnType<typeof createWorkflowFromCatalogItem>>) => void;
  mockedCreate.mockReturnValueOnce(new Promise((resolve) => { acceptCreate = resolve; }));
  openDialog();
  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const create = await screen.findByRole("button", { name: "Create draft" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);
  await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
  const id = screen.getByLabelText("Workflow ID");
  fireEvent.change(id, { target: { value: "newer_draft" } });
  await act(async () => { acceptCreate({
    status: "imported", workflow_id: "synthetic_template_0000", imported_workflows: [],
    preflight: preflight(), catalog_item: { item_id: "synthetic_template_0000", version: "1.0.0" },
    catalog_extension_resolutions: [],
  }); });
  expect(screen.queryByRole("heading", { name: "Draft created" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("Workflow ID")).toHaveValue("newer_draft");
});

test("Check again during Create cannot hide a successful simple draft", async () => {
  let acceptCreate!: (value: Awaited<ReturnType<typeof createWorkflowFromCatalogItem>>) => void;
  mockedCreate.mockReturnValueOnce(new Promise((resolve) => { acceptCreate = resolve; }));
  openDialog();
  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const create = await screen.findByRole("button", { name: "Create draft" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);
  await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
  const check = screen.getByRole("button", { name: "Check again" });
  const disabledDuringCreate = check.hasAttribute("disabled");
  fireEvent.click(check);
  await act(async () => { acceptCreate({
    status: "imported", workflow_id: "synthetic_template_0000", imported_workflows: [],
    preflight: preflight(), catalog_item: { item_id: "synthetic_template_0000", version: "1.0.0" },
    catalog_extension_resolutions: [],
  }); });
  expect(await screen.findByRole("heading", { name: "Draft created" })).toBeInTheDocument();
  expect(disabledDuringCreate).toBe(true);
  expect(mockedPreflight).toHaveBeenCalledTimes(1);
});

test("a child-decision recheck stays unavailable during Create but current refusal still recovers", async () => {
  mockedPreflight
    .mockResolvedValueOnce(preflight({
      can_apply: false, workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [childConflict("fetch_messages")],
    }))
    .mockResolvedValue(preflight({
      workflows: THREE_WORKFLOWS.slice(0, 2),
      dependencies: [{ ...childConflict("fetch_messages"), status: "resolved", blocking: false }],
    }));
  let rejectCreate!: (error: Error) => void;
  mockedCreate.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectCreate = reject; }));
  openDialog();
  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  await screen.findByLabelText("New workflow id for fetch_messages");
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  const create = await screen.findByRole("button", { name: "Create 2 drafts" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);
  await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
  const check = screen.getByRole("button", { name: "Check again" });
  expect(check).toBeDisabled();
  fireEvent.click(check);
  expect(mockedPreflight).toHaveBeenCalledTimes(2);
  await act(async () => { rejectCreate(new Error("Current Create refused")); });
  expect(await screen.findByText("Current Create refused")).toBeInTheDocument();
  expect(mockedPreflight).toHaveBeenCalledTimes(3);
});

test("a late Create rejection cannot recheck the old project over a newer choice", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchProjectsList).mockResolvedValue({
    projects: [{ id: "project-platform", name: "Platform" }],
    page: 1, per_page: 200, total: 1, total_pages: 1,
  });
  let rejectCreate!: (error: Error) => void;
  mockedCreate.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectCreate = reject; }));
  openDialog();
  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const create = await screen.findByRole("button", { name: "Create draft" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);
  await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
  const project = screen.getByRole("combobox", { name: "Project (optional)" });
  await user.click(project);
  await user.click(await screen.findByRole("option", { name: "Platform" }));
  await waitFor(() => expect(mockedPreflight).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(mockedImportOptions).toHaveBeenLastCalledWith({ projectId: "project-platform" }));
  await act(async () => { rejectCreate(new Error("Old Create refused")); });
  expect(mockedPreflight).toHaveBeenCalledTimes(2);
  expect(mockedImportOptions).toHaveBeenLastCalledWith({ projectId: "project-platform" });
  expect(project).toHaveTextContent("Platform");
  expect(screen.queryByText("Old Create refused")).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Draft created" })).not.toBeInTheDocument();
});

test("Escape on the first screen closes the dialog", async () => {
  const handlers = openDialog();
  await screen.findByRole("heading", { name: "3 templates" });
  escape();
  expect(handlers.onClose).toHaveBeenCalledTimes(1);
});

test("an operator browses read-only", async () => {
  openDialog({ readOnly: true });

  expect(await screen.findByRole("heading", { name: "Workflow templates" })).toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "From scratch" })).not.toBeInTheDocument();
  // The action-rights rule: no Use template on the cards, the table or the page.
  const card = await screen.findByRole("article", { name: "Synthetic template 0000" });
  expect(screen.queryByRole("button", { name: /^Use template/ })).not.toBeInTheDocument();
  // A card says nothing about rights: no button and no text in its place.
  expect(card).not.toHaveTextContent(/administrators|Use template/i);
  await openTemplate(/Synthetic template 0000/);
  expect(screen.queryByRole("button", { name: "Use template" })).not.toBeInTheDocument();
  expect(screen.getByText("Only administrators can create workflows.")).toBeInTheDocument();
});

test("an operator's table has no Use template column; Status takes its room", async () => {
  openDialog({ readOnly: true, url: "/registry/workflows?create=template&tpl_view=list" });

  const table = await screen.findByRole("table", { name: "Templates" });
  const headers = within(table).getAllByRole("columnheader").map((header) => header.textContent || header.getAttribute("aria-label"));
  expect(headers).toEqual(["Template", "Status", "Integrations", "Trigger"]);
});

test("an administrator's table ends in Use template", async () => {
  openDialog({ url: "/registry/workflows?create=template&tpl_view=list" });

  const table = await screen.findByRole("table", { name: "Templates" });
  expect(within(table).getAllByRole("columnheader").map((header) => header.textContent || header.getAttribute("aria-label"))).toEqual([
    "Template",
    "Status",
    "Integrations",
    "Trigger",
    "Use template",
  ]);
  expect(within(table).getByRole("button", { name: "Use template: Synthetic template 0000" })).toBeEnabled();
});

test("a template for a newer Flow Steward cannot be used, and says why", async () => {
  const index = catalogIndex(2);
  index.items[0].compatible_flow_steward = { min: "0.9.0" };
  mockedFetchCatalog.mockResolvedValue(index);
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  expect(screen.getByText("Flow Steward 0.9.0 or later")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", { name: "Use template" })).toBeDisabled());
  expect(
    screen.getByText("This template needs Flow Steward 0.9.0 or later; this installation runs 0.4.0."),
  ).toBeInTheDocument();
});

test("a template Core cannot import is unavailable in its card and cannot be used", async () => {
  const index = catalogIndex(3);
  index.items[0] = { ...index.items[0], bundle_schema: "platform.workflow_bundle.v1", importable: false };
  index.items[1] = { ...index.items[1], bundle_schema: "", importable: false };
  mockedFetchCatalog.mockResolvedValue(index);
  openDialog();

  await screen.findByRole("heading", { name: "3 templates" });
  const v1Card = screen.getByRole("article", { name: "Synthetic template 0000" });
  expect(within(v1Card).getByText("Unavailable")).toBeInTheDocument();
  expect(
    within(v1Card).getByText("This template is in a workflow format this installation cannot import (platform.workflow_bundle.v1)."),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Use template: Synthetic template 0000" })).toBeDisabled();

  const unknownCard = screen.getByRole("article", { name: "Synthetic template 0001" });
  expect(
    within(unknownCard).getByText("This template does not say which workflow format it uses, so this installation cannot import it."),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Use template: Synthetic template 0001" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Use template: Synthetic template 0002" })).toBeEnabled();

  await openTemplate(/Synthetic template 0000/);
  await waitFor(() => expect(screen.getByRole("button", { name: "Use template" })).toBeDisabled());
  expect(mockedPreflight).not.toHaveBeenCalled();
});

test("the included workflows are listed and counted in what is created", async () => {
  const index = catalogIndex(2);
  index.items[0].includes = [
    { item_id: "fetch_messages", name: "Fetch messages" },
    { item_id: "file_attachments", name: "File attachments" },
  ];
  mockedFetchCatalog.mockResolvedValue(index);
  mockedPreflight.mockResolvedValue(
    preflight({
      workflows: [
        { workflow_id: "synthetic_template_0000", display_name: "Synthetic template 0000" },
        { workflow_id: "fetch_messages", display_name: "Fetch messages" },
        { workflow_id: "file_attachments", display_name: "File attachments" },
      ],
    }),
  );
  mockedCreate.mockResolvedValue({
    status: "imported",
    workflow_id: "synthetic_template_0000",
    imported_workflows: [
      { workflow_id: "synthetic_template_0000", source_workflow_id: "synthetic_template_0000", enabled: false, import_state: "draft" },
      { workflow_id: "fetch_messages", source_workflow_id: "fetch_messages", enabled: false, import_state: "draft" },
      { workflow_id: "file_attachments", source_workflow_id: "file_attachments", enabled: false, import_state: "draft" },
    ],
    preflight: preflight(),
    catalog_item: { item_id: "synthetic_template_0000", version: "1.0.0" },
    catalog_extension_resolutions: [],
  });
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  expect(screen.getByRole("heading", { name: "Includes" })).toBeInTheDocument();
  expect(screen.getByText("Fetch messages")).toBeInTheDocument();
  expect(screen.getByText("File attachments")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  expect(await screen.findByText(/Creates 3 disabled drafts/)).toBeInTheDocument();
  const create = screen.getByRole("button", { name: "Create 3 drafts" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);

  expect(await screen.findByRole("heading", { name: "3 drafts created" })).toBeInTheDocument();
  expect(screen.getByText(/Enable the included workflows first/)).toBeInTheDocument();
});

test("a carried child whose id is taken can use the existing workflow", async () => {
  mockedPreflight.mockImplementation(async (_itemId, body = {}) =>
    preflight({
      can_apply: Boolean(body.resolutions?.some((choice) => choice.action === "use_existing")),
      workflows: [
        { workflow_id: "synthetic_template_0000", display_name: "Synthetic template 0000" },
        { workflow_id: "fetch_messages", display_name: "Fetch messages" },
      ],
      dependencies: [
        {
          dependency_key: "workflow:fetch_messages",
          dependency_type: "workflow",
          ref: "fetch_messages",
          label: "fetch_messages",
          status: body.resolutions?.some((choice) => choice.action === "use_existing") ? "resolved" : "conflict",
          blocking: !body.resolutions?.some((choice) => choice.action === "use_existing"),
          resolution_actions: ["rename_workflow", "use_existing", "abort"],
          message: "Workflow id already exists. Use the existing workflow or choose a new id.",
          metadata: { code: "workflow_id_conflict", bundled_child: true },
        },
      ],
    }),
  );
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const action = await screen.findByLabelText("Resolution for fetch_messages");
  expect(within(action).getByRole("option", { name: "Use existing" })).toBeInTheDocument();
  // A taken child id starts with a free one suggested, as the root's does.
  expect(screen.getByLabelText("New workflow id for fetch_messages")).toHaveValue("fetch_messages_2");

  fireEvent.change(action, { target: { value: "use_existing" } });
  expect(await screen.findByText("Uses the workflow already here as fetch_messages.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create draft" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  // The reused child is not created again.
  const create = await screen.findByRole("button", { name: "Create draft" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);

  await waitFor(() =>
    expect(mockedCreate).toHaveBeenCalledWith("synthetic_template_0000", {
      projectId: undefined,
      targetWorkflowId: "synthetic_template_0000",
      resolutions: [
        {
          action: "use_existing",
          dependency_key: "workflow:fetch_messages",
          dependency_type: "workflow",
          ref: "fetch_messages",
        },
      ],
    }),
  );
});

test("a taken workflow id offers a free one and checks it", async () => {
  mockedPreflight.mockImplementation(async (_itemId, body = {}) =>
    body.targetWorkflowId === "synthetic_template_0000_2"
      ? preflight()
      : preflight({
          can_apply: false,
          dependencies: [
            {
              dependency_key: "workflow:synthetic_template_0000",
              dependency_type: "workflow",
              ref: "synthetic_template_0000",
              status: "conflict",
              blocking: true,
              resolution_actions: ["rename_workflow", "abort"],
              metadata: { code: "workflow_id_conflict" },
            },
          ],
        }),
  );
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  expect(await screen.findByText("synthetic_template_0000 already exists.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create draft" })).toBeDisabled();
  // The root's own conflict is answered by the field, not by a second row.
  expect(screen.queryByLabelText("New workflow id for synthetic_template_0000")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Use synthetic_template_0000_2" }));
  await waitFor(
    () =>
      expect(mockedPreflight).toHaveBeenLastCalledWith("synthetic_template_0000", {
        projectId: undefined,
        targetWorkflowId: "synthetic_template_0000_2",
      }),
    { timeout: 2000 },
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "Create draft" })).toBeEnabled());
});

test("a missing extension links to it in Marketplace and can be checked again", async () => {
  mockedPreflight.mockResolvedValue(
    preflight({
      can_apply: false,
      catalog_extension_resolutions: [
        { extension_id: "synthetic_gmail", outcome: "missing", message: "No active install of this extension." },
      ],
    }),
  );
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const link = await screen.findByRole("link", { name: /Open in Catalog/ });
  expect(link).toHaveAttribute("href", "/extensions?find=synthetic_gmail#marketplace");
  expect(link).toHaveAttribute("target", "_blank");
  expect(screen.getByText("No active install of this extension.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create draft" })).toBeDisabled();

  mockedPreflight.mockResolvedValue(preflight());
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(mockedPreflight).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByRole("button", { name: "Create draft" })).toBeEnabled());
});

/* A parent template names its child by the id it had on the author's machine.
   If the child was imported under another id, the catalog must offer the same
   "map to existing" choice the manual YAML import does, and send it on create. */
test("a missing child workflow is mapped here exactly as in the manual import", async () => {
  mockedPreflight.mockResolvedValueOnce(
    preflight({
      can_apply: false,
      dependencies: [
        {
          dependency_key: "child_workflow:stock_import_supplier_a",
          dependency_type: "child_workflow",
          ref: "stock_import_supplier_a",
          label: "stock_import_supplier_a",
          status: "requires_mapping",
          blocking: true,
          required: true,
          resolution_actions: ["map_existing", "abort"],
        },
      ],
    }),
  ).mockResolvedValueOnce(preflight());
  openDialog({ projectId: "proj_1" });

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const picker = await screen.findByLabelText("Existing workflow for stock_import_supplier_a");
  expect(mockedImportOptions).toHaveBeenCalledWith({ projectId: "proj_1" });
  const create = screen.getByRole("button", { name: "Create draft" });
  expect(create).toBeDisabled();

  fireEvent.focus(picker);
  fireEvent.click(screen.getByRole("option", { name: /Stock import: supplier A/ }));
  expect(create).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);

  await waitFor(() =>
    expect(mockedCreate).toHaveBeenCalledWith("synthetic_template_0000", {
      projectId: "proj_1",
      targetWorkflowId: "synthetic_template_0000",
      resolutions: [
        {
          action: "map_existing",
          dependency_key: "child_workflow:stock_import_supplier_a",
          dependency_type: "child_workflow",
          ref: "stock_import_supplier_a",
          target_ref: "stock_import_supplier_a_2",
        },
      ],
    }),
  );
});

/* The set-up opens with No project, so the project is chosen after the first check.
   A connection mapped afterwards resolves in that project: on 0.9.4 the mapping
   went without project_id and the server refused the re-check. */
test("a connection mapped after the project is chosen resolves in that project", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchProjectsList).mockResolvedValue({
    projects: [{ id: "project-platform", name: "Platform" }],
    page: 1, per_page: 200, total: 1, total_pages: 1,
  });
  mockedImportOptions.mockResolvedValue({
    kind: "workflow_import_dependency_options",
    options: { project_connection: [{ value: "conn_local", label: "Local Docling" }] },
  });
  const needsConnection = preflight({
    can_apply: false,
    dependencies: [
      {
        dependency_key: "project_connection:conn_template",
        dependency_type: "project_connection",
        ref: "conn_template",
        status: "requires_project_connection",
        blocking: true,
        required: true,
        resolution_actions: ["map_existing", "create_connection_placeholder", "abort"],
      },
    ],
  });
  mockedPreflight
    .mockResolvedValueOnce(needsConnection)
    .mockResolvedValueOnce(needsConnection)
    .mockResolvedValue(preflight());
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  await user.click(screen.getByRole("button", { name: "Use template" }));
  const project = await screen.findByRole("combobox", { name: "Project (optional)" });
  await user.click(project);
  await user.click(await screen.findByRole("option", { name: "Platform" }));
  await waitFor(() => expect(mockedPreflight).toHaveBeenCalledTimes(2));

  const picker = await screen.findByLabelText("Existing project connection for conn_template");
  fireEvent.focus(picker);
  fireEvent.click(await screen.findByRole("option", { name: /Local Docling/ }));
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));

  const mapping = {
    action: "map_existing",
    dependency_key: "project_connection:conn_template",
    dependency_type: "project_connection",
    ref: "conn_template",
    target_ref: "conn_local",
    project_id: "project-platform",
  };
  await waitFor(() =>
    expect(mockedPreflight).toHaveBeenLastCalledWith("synthetic_template_0000", {
      projectId: "project-platform",
      targetWorkflowId: "synthetic_template_0000",
      resolutions: [mapping],
    }),
  );
  const create = screen.getByRole("button", { name: "Create draft" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);
  await waitFor(() =>
    expect(mockedCreate).toHaveBeenCalledWith("synthetic_template_0000", {
      projectId: "project-platform",
      targetWorkflowId: "synthetic_template_0000",
      resolutions: [mapping],
    }),
  );
});

/* A step's connection belongs to the extension the step calls. The picker offered
   every connection of the project, so an email step could be pointed at Docling. */
test("a connection slot offers only connections of its step's extension", async () => {
  mockedImportOptions.mockResolvedValue({
    kind: "workflow_import_dependency_options",
    options: {
      project_connection: [
        { value: "conn_docs", label: "Local Docling", metadata: { extension_id: "flowsteward.docling" } },
        { value: "conn_mail", label: "Mailpit", metadata: { extension_id: "flowsteward.outgoing-email" } },
      ],
    },
  });
  mockedPreflight.mockResolvedValue(
    preflight({
      can_apply: false,
      dependencies: [
        {
          dependency_key: "project_connection:conn_template",
          dependency_type: "project_connection",
          ref: "conn_template",
          status: "requires_project_connection",
          blocking: true,
          required: true,
          resolution_actions: ["map_existing", "create_connection_placeholder", "abort"],
          metadata: { extension_id: "flowsteward.outgoing-email" },
        },
      ],
    }),
  );
  openDialog({ projectId: "proj_1" });

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const picker = await screen.findByLabelText("Existing project connection for conn_template");
  fireEvent.focus(picker);
  expect(await screen.findByRole("option", { name: /Mailpit/ })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /Local Docling/ })).not.toBeInTheDocument();

  // A placeholder is created for the same extension without typing it.
  fireEvent.change(screen.getByLabelText("Resolution for conn_template"), {
    target: { value: "create_connection_placeholder" },
  });
  expect(screen.getByLabelText("Extension id for conn_template")).toHaveValue("flowsteward.outgoing-email");
});

test("a template that cannot be created here says so plainly", async () => {
  mockedPreflight.mockResolvedValue(
    preflight({
      can_apply: false,
      topology_issues: [
        { code: "unreachable_step", message: "The Report step can never run.", blocking: true },
      ],
    }),
  );
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  expect(await screen.findByText("This template can't be created on this installation")).toBeInTheDocument();
  expect(screen.getByText("The Report step can never run.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create draft" })).toBeDisabled();
});

test("a failed check and a failed create are shown and recoverable", async () => {
  mockedPreflight.mockRejectedValueOnce(new Error("The template file couldn't be downloaded."));
  mockedCreate.mockRejectedValueOnce(new Error("Workflow storage is unavailable."));
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  expect(await screen.findByText("The template file couldn't be downloaded.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));

  const create = await screen.findByRole("button", { name: "Create draft" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);
  expect(await screen.findByText("Workflow storage is unavailable.")).toBeInTheDocument();
  expect(screen.getByText("The workflow was not created")).toBeInTheDocument();
});

test("an unavailable catalog says so and offers the other way in", async () => {
  mockedFetchCatalog.mockRejectedValue(new Error("Workflow template catalog is unavailable."));
  const handlers = openDialog();

  expect(await screen.findByText("The template catalog is unavailable right now")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Start from scratch" }));
  expect(handlers.onTabChange).toHaveBeenCalledWith("scratch");
});

test("an unconfigured catalog is not requested", async () => {
  openDialog({ configured: false });

  expect(await screen.findByText("No template catalog is configured")).toBeInTheDocument();
  expect(mockedFetchCatalog).not.toHaveBeenCalled();
});

test("a stale catalog is served with a warning rather than hidden", async () => {
  mockedFetchCatalog.mockResolvedValue(
    catalogIndex(2, {
      availability: {
        kind: "workflow_template_catalog_availability",
        configured: true,
        available: true,
        item_count: 2,
        stale: true,
        last_refresh_error: "host is down",
      },
    }),
  );
  openDialog();

  expect(await screen.findByText(/Showing the catalog as of/)).toBeInTheDocument();
  expect(screen.getByText("Synthetic template 0000")).toBeInTheDocument();
});

test("no results offers to remove the last filter and to suggest a template", async () => {
  openDialog({ url: "/registry/workflows?create=template&tpl_q=nothing+like+this" });

  expect(await screen.findByText("No templates match")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Suggest one on GitHub/ })).toHaveAttribute(
    "href",
    "https://github.com/Flow-Steward/workflows",
  );
  fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
  expect(await screen.findByRole("heading", { name: "3 templates" })).toBeInTheDocument();
});

test("on a phone the list is rows and the facets are one Add filter control", async () => {
  const matchMedia = vi.fn().mockReturnValue({
    matches: true,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  });
  vi.stubGlobal("matchMedia", matchMedia);
  try {
    mockedFetchCatalog.mockResolvedValue(catalogIndex(12));
    openDialog();

    await screen.findByRole("heading", { name: "12 templates" });
    expect(screen.queryByRole("navigation", { name: "Template filters" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "List view" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Trigger" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Webhook (3)" }));
    expect(await screen.findByRole("heading", { name: "3 templates" })).toBeInTheDocument();
    expect(screen.getByText("Trigger · Webhook")).toBeInTheDocument();
  } finally {
    vi.unstubAllGlobals();
  }
});

test("Marketplace is linked under the app's base path", async () => {
  mockedPreflight.mockResolvedValue(
    preflight({
      can_apply: false,
      catalog_extension_resolutions: [
        { extension_id: "synthetic_gmail", outcome: "missing", message: "" },
      ],
    }),
  );
  openDialog({ basename: "/app", url: "/app/registry/workflows?create=template" });

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  expect(await screen.findByRole("link", { name: /Open in Catalog/ })).toHaveAttribute(
    "href",
    "/app/extensions?find=synthetic_gmail#marketplace",
  );
}, 15_000);

test("a missing extension is one card by its name, not also a raw dependency row", async () => {
  mockedPreflight.mockResolvedValue(
    preflight({
      can_apply: false,
      dependencies: [
        {
          dependency_key: "extension:5b1f0c9e-install",
          dependency_type: "extension",
          ref: "5b1f0c9e-install",
          status: "requires_extension_install",
          blocking: true,
          resolution_actions: ["install_extension_required", "abort"],
          metadata: { extension_id: "synthetic_gmail" },
        },
      ],
      catalog_extension_resolutions: [
        { extension_id: "synthetic_gmail", outcome: "missing", message: "No active install of this extension." },
      ],
    }),
  );
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  // Named as the template's setup requirement names it.
  expect(await screen.findByText("gmail extension")).toBeInTheDocument();
  expect(screen.queryByText("requires_extension_install")).not.toBeInTheDocument();
  expect(screen.queryByText(/5b1f0c9e-install/)).not.toBeInTheDocument();
  expect(screen.getByText("Resolve 1 item above.")).toBeInTheDocument();
}, 15_000);

test("a blocked template shows the preflight's own reasons", async () => {
  mockedPreflight.mockResolvedValue(
    preflight({
      can_apply: false,
      dependencies: [
        {
          dependency_key: "workflow:workflows[0]",
          dependency_type: "workflow",
          ref: "workflows[0]",
          status: "blocked",
          blocking: true,
          message: "Workflow definition is invalid.",
          metadata: { message: "Unknown invoke_workflow config key 'wait'" },
        },
      ],
      topology_issues: [
        { code: "terminal_path", message: "Workflow must define exactly one unambiguous terminal path", blocking: false },
      ],
    }),
  );
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  expect(await screen.findByText("Unknown invoke_workflow config key 'wait'")).toBeInTheDocument();
  expect(screen.getByText("Workflow must define exactly one unambiguous terminal path")).toBeInTheDocument();
  expect(screen.queryByText("Workflow definition is invalid.")).not.toBeInTheDocument();
});

test("two quick ticks keep both values, and two quick Escapes step back and close", async () => {
  mockedFetchCatalog.mockResolvedValue(catalogIndex(12));
  const handlers = openDialog();

  await screen.findByRole("heading", { name: "12 templates" });
  const category = screen.getByRole("group", { name: "Category" });
  const productivity = within(category).getByRole("checkbox", { name: /productivity/ });
  const sales = within(category).getByRole("checkbox", { name: /sales/ });
  // Both clicks land before React renders again, as two quick clicks in a
  // browser can: the second must not start from the first one's snapshot.
  act(() => {
    productivity.click();
    sales.click();
  });
  await waitFor(() =>
    expect(screen.getByTestId("location")).toHaveTextContent("tpl_category=productivity%2Csales"),
  );

  await openTemplate(/Synthetic template 0000/);
  act(() => {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  expect(handlers.onClose).toHaveBeenCalledTimes(1);
});

test("Create another returns to the templates with the filters kept", async () => {
  openDialog({ url: "/registry/workflows?create=template&tpl_trigger=manual" });

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const create = await screen.findByRole("button", { name: "Create draft" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);
  fireEvent.click(await screen.findByRole("button", { name: "Create another" }));

  // Of the three templates only the first is manual.
  expect(await screen.findByRole("heading", { name: "1 template" })).toBeInTheDocument();
  expect(screen.getByTestId("location")).toHaveTextContent("tpl_trigger=manual");
  expect(screen.getByTestId("location")).not.toHaveTextContent("tpl=");
}, 15_000);

function childConflict(ref: string) {
  return {
    dependency_key: `workflow:${ref}`,
    dependency_type: "workflow",
    ref,
    label: ref,
    status: "conflict" as const,
    blocking: true,
    resolution_actions: ["rename_workflow", "use_existing", "abort"],
    metadata: { code: "workflow_id_conflict", bundled_child: true },
  };
}

const ROOT_CONFLICT = {
  dependency_key: "workflow:synthetic_template_0000",
  dependency_type: "workflow",
  ref: "synthetic_template_0000",
  status: "conflict" as const,
  blocking: true,
  resolution_actions: ["rename_workflow", "abort"],
  metadata: { code: "workflow_id_conflict" },
};

const THREE_WORKFLOWS = [
  { workflow_id: "synthetic_template_0000", display_name: "Synthetic template 0000" },
  { workflow_id: "fetch_messages", display_name: "Fetch messages" },
  { workflow_id: "file_attachments", display_name: "File attachments" },
];

test("suggested ids are free here and do not collide with each other", async () => {
  vi.mocked(fetchWorkflows).mockResolvedValue([
    { workflow_id: "synthetic_template_0000" },
    { workflow_id: "synthetic_template_0000_2" },
    { workflow_id: "fetch_messages" },
    { workflow_id: "fetch_messages_2" },
    { workflow_id: "file_attachments" },
  ]);
  mockedPreflight.mockResolvedValue(
    preflight({
      can_apply: false,
      workflows: THREE_WORKFLOWS,
      dependencies: [ROOT_CONFLICT, childConflict("fetch_messages"), childConflict("file_attachments")],
    }),
  );
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  expect(await screen.findByRole("button", { name: "Use synthetic_template_0000_3" })).toBeInTheDocument();
  expect(screen.getByLabelText("New workflow id for fetch_messages")).toHaveValue("fetch_messages_3");
  expect(screen.getByLabelText("New workflow id for file_attachments")).toHaveValue("file_attachments_2");
}, 15_000);

test("a taken child id is marked on its row and keeps Create off", async () => {
  vi.mocked(fetchWorkflows).mockResolvedValue([{ workflow_id: "fetch_messages" }, { workflow_id: "taken_id" }]);
  mockedPreflight.mockResolvedValueOnce(
    preflight({
      can_apply: false,
      workflows: THREE_WORKFLOWS.slice(0, 2),
      dependencies: [childConflict("fetch_messages")],
    }),
  ).mockResolvedValueOnce(preflight({ workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [{ ...childConflict("fetch_messages"), status: "resolved", blocking: false }] }));
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const target = await screen.findByLabelText("New workflow id for fetch_messages");
  expect(screen.getByRole("button", { name: "Create 2 drafts" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Create 2 drafts" })).toBeEnabled());

  fireEvent.change(target, { target: { value: "taken_id" } });
  expect(await screen.findByText("taken_id already exists.")).toBeInTheDocument();
  expect(target).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByRole("button", { name: "Create 2 drafts" })).toBeDisabled();
  expect(screen.getByText("Choose a free id for fetch_messages.")).toBeInTheDocument();
});

test("when the server refuses, the rows it still refuses are named", async () => {
  mockedPreflight
    .mockResolvedValueOnce(
      preflight({ can_apply: false, workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [childConflict("fetch_messages")] }),
    )
    .mockResolvedValueOnce(
      preflight({ workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [{ ...childConflict("fetch_messages"), status: "resolved", blocking: false }] }),
    )
    .mockResolvedValue(
      preflight({ can_apply: false, workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [childConflict("fetch_messages")] }),
    );
  mockedCreate.mockRejectedValueOnce(new Error("Workflow import has unresolved blocking dependencies."));
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const create = await screen.findByRole("button", { name: "Create 2 drafts" });
  expect(create).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);

  expect(
    await screen.findByText(
      "Workflow import has unresolved blocking dependencies. Still to resolve: fetch_messages.",
    ),
  ).toBeInTheDocument();
});

test("editing a child rename invalidates an in-flight check and keeps the row after server omission", async () => {
  let acceptOlder!: (value: WorkflowCatalogPreflight) => void;
  const older = new Promise<WorkflowCatalogPreflight>((resolve) => { acceptOlder = resolve; });
  mockedPreflight
    .mockResolvedValueOnce(preflight({
      can_apply: false, workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [{ ...childConflict("fetch_messages"), message: "Original conflict: choose a different ID." }],
    }))
    .mockReturnValueOnce(older)
    .mockResolvedValueOnce(preflight({ workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [] }));
  openDialog();
  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  const target = await screen.findByLabelText("New workflow id for fetch_messages");
  expect(target).toHaveValue("fetch_messages_2");
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(mockedPreflight).toHaveBeenCalledTimes(2));
  fireEvent.change(target, { target: { value: "fetch_messages_3" } });
  expect(screen.getByRole("button", { name: "Create 2 drafts" })).toBeDisabled();
  await act(async () => { acceptOlder(preflight({ workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [] })); });
  expect(screen.getByRole("button", { name: "Create 2 drafts" })).toBeDisabled();
  expect(screen.getByLabelText("New workflow id for fetch_messages")).toHaveValue("fetch_messages_3");
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(mockedPreflight).toHaveBeenCalledTimes(3));
  expect(mockedPreflight).toHaveBeenLastCalledWith("synthetic_template_0000", {
    projectId: undefined,
    targetWorkflowId: "synthetic_template_0000",
    resolutions: [{
      action: "rename_workflow", dependency_key: "workflow:fetch_messages",
      dependency_type: "workflow", ref: "fetch_messages", target_workflow_id: "fetch_messages_3",
    }],
  });
  await waitFor(() => expect(screen.getByRole("button", { name: "Create 2 drafts" })).toBeEnabled());
  expect(screen.getByLabelText("New workflow id for fetch_messages")).toHaveValue("fetch_messages_3");
  expect(screen.getByText("resolved")).toBeInTheDocument();
  expect(screen.queryByText("Original conflict: choose a different ID.")).not.toBeInTheDocument();
});

test("a child rename stays visible after the server accepts it", async () => {
  mockedPreflight
    .mockResolvedValueOnce(
      preflight({ can_apply: false, workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [{ ...childConflict("fetch_messages"), message: "Original conflict: choose a different ID." }] }),
    )
    .mockResolvedValue(
      preflight({
        workflows: THREE_WORKFLOWS.slice(0, 2),
        dependencies: [{ ...childConflict("fetch_messages"), status: "resolved", blocking: false, message: "Resolved after check." }],
      }),
    );
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  await screen.findByLabelText("New workflow id for fetch_messages");
  expect(screen.getByRole("button", { name: "Create 2 drafts" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));

  await waitFor(() => expect(mockedPreflight).toHaveBeenCalledTimes(2));
  expect(mockedPreflight).toHaveBeenLastCalledWith("synthetic_template_0000", {
    projectId: undefined,
    targetWorkflowId: "synthetic_template_0000",
    resolutions: [
      {
        action: "rename_workflow",
        dependency_key: "workflow:fetch_messages",
        dependency_type: "workflow",
        ref: "fetch_messages",
        target_workflow_id: "fetch_messages_2",
      },
    ],
  });
  expect(await screen.findByText(/Creates fetch_messages as fetch_messages_2/)).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", { name: "Create 2 drafts" })).toBeEnabled());
  expect(screen.getByText("resolved")).toBeInTheDocument();
  expect(screen.getByText("Resolved after check.")).toBeInTheDocument();
  expect(screen.queryByText("Original conflict: choose a different ID.")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("New workflow id for fetch_messages"), { target: { value: "fetch_messages_3" } });
  expect(screen.getByRole("button", { name: "Create 2 drafts" })).toBeDisabled();
  expect(screen.getByText("conflict")).toBeInTheDocument();
  expect(screen.getByText("Original conflict: choose a different ID.")).toBeInTheDocument();
});

test("the summary keeps an accepted rename and a reused child while another row has an error", async () => {
  vi.mocked(fetchWorkflows).mockResolvedValue([
    { workflow_id: "fetch_messages" },
    { workflow_id: "file_attachments" },
    { workflow_id: "taken_id" },
  ]);
  const FOUR_WORKFLOWS = [...THREE_WORKFLOWS, { workflow_id: "send_reply", display_name: "Send reply" }];
  mockedPreflight
    .mockResolvedValueOnce(
      preflight({
        can_apply: false,
        workflows: FOUR_WORKFLOWS,
        dependencies: [childConflict("fetch_messages"), childConflict("file_attachments"), childConflict("send_reply")],
      }),
    )
    // The server keeps an accepted rename's row, now resolved, and a reused
    // child's row; the other child's target is still taken.
    .mockResolvedValue(
      preflight({
        can_apply: false,
        workflows: FOUR_WORKFLOWS,
        dependencies: [
          { ...childConflict("fetch_messages"), status: "resolved", blocking: false },
          childConflict("file_attachments"),
          { ...childConflict("send_reply"), status: "resolved", blocking: false },
        ],
      }),
    );
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  fireEvent.change(await screen.findByLabelText("Resolution for send_reply"), { target: { value: "use_existing" } });
  fireEvent.change(screen.getByLabelText("New workflow id for file_attachments"), { target: { value: "taken_id" } });
  expect(await screen.findByText("taken_id already exists.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(mockedPreflight).toHaveBeenCalledTimes(2));

  const summary = await screen.findByText(/Nothing runs until you enable it/);
  await waitFor(() => expect(summary).toHaveTextContent("Creates fetch_messages as fetch_messages_2"));
  expect(summary).toHaveTextContent("Uses the existing send_reply");
  expect(screen.getByText("taken_id already exists.")).toBeInTheDocument();
});

test("reused children that are disabled still have to be enabled first", async () => {
  vi.mocked(fetchWorkflows).mockResolvedValue([{ workflow_id: "fetch_messages", enabled: false }]);
  mockedPreflight.mockResolvedValueOnce(
    preflight({ can_apply: false, workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [childConflict("fetch_messages")] }),
  ).mockResolvedValueOnce(preflight({ workflows: THREE_WORKFLOWS.slice(0, 2), dependencies: [{ ...childConflict("fetch_messages"), status: "resolved", blocking: false }] }));
  mockedCreate.mockResolvedValue({
    status: "imported",
    workflow_id: "synthetic_template_0000",
    imported_workflows: [
      { workflow_id: "synthetic_template_0000", source_workflow_id: "synthetic_template_0000", enabled: false, import_state: "draft" },
    ],
    preflight: preflight(),
    catalog_item: { item_id: "synthetic_template_0000", version: "1.0.0" },
    catalog_extension_resolutions: [],
  });
  openDialog();

  await openTemplate(/Synthetic template 0000/);
  fireEvent.click(screen.getByRole("button", { name: "Use template" }));
  fireEvent.change(await screen.findByLabelText("Resolution for fetch_messages"), {
    target: { value: "use_existing" },
  });
  const create = await screen.findByRole("button", { name: "Create draft" });
  expect(create).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.click(create);

  expect(await screen.findByRole("heading", { name: "Draft created" })).toBeInTheDocument();
  expect(screen.getByText(/Enable the included workflow first \(fetch_messages\)/)).toBeInTheDocument();
});
