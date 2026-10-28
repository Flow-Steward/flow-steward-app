import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router";
import { useCanReadWorkflowRegistry, useIsAdmin } from "@/lib/auth/use-is-admin";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { persistWorkspaceSelection, WORKSPACE_STORAGE_KEY } from "@/lib/utils/workspace-selection";
import { WorkflowsListPage } from "@/pages/workflows/list";

vi.mock("@/lib/auth/use-is-admin", () => ({ useCanReadWorkflowRegistry: vi.fn(), useIsAdmin: vi.fn() }));
vi.mock("@/pages/workflows/catalog/api", () => ({
  fetchWorkflowCatalogAvailability: vi.fn(async () => ({ configured: false })),
  fetchWorkflowCatalog: vi.fn(), fetchInstallationVersion: vi.fn(),
  preflightWorkflowCatalogItem: vi.fn(), createWorkflowFromCatalogItem: vi.fn(),
}));
const alpha = { id: "project-alpha", name: "Alpha project" };
const beta = { id: "project-beta", name: "Beta project" };
const rows = [
  ...Array.from({ length: 28 }, (_, index) => ({ workflow_id: `other-${index}`, display_name: `Other ${index}`, enabled: true })),
  { workflow_id: "alpha-only", display_name: "Alpha workflow", enabled: true, project_id: alpha.id },
  { workflow_id: "beta-only", display_name: "Beta workflow", enabled: true, project_id: beta.id },
  // Ownership differs from membership: Shared must appear in both projects.
  { workflow_id: "shared", display_name: "Shared workflow", enabled: true, project_id: alpha.id },
  { workflow_id: "extension.install-7.example", display_name: "Installed workflow", enabled: true },
  { workflow_id: "unassigned", display_name: "Unassigned workflow", enabled: true },
  { workflow_id: "inactive-association", display_name: "Inactive association workflow", enabled: true },
  { workflow_id: "shared-copy", display_name: "Unlinked workflow sharing a prefix", enabled: true },
  { workflow_id: "disabled-alpha", display_name: "Disabled Alpha workflow", enabled: false },
];
const members: Record<string, string[]> = {
  [alpha.id]: ["alpha-only", "shared", "extension.install-7.example", "disabled-alpha"],
  [beta.id]: ["beta-only", "shared"],
};
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
function bindings(projectId: string) {
  return { project_id: projectId, workflow_ids: members[projectId], operator_default_workflow_id: "shared",
    project_workflows: [...members[projectId].map((workflow_id) => ({ project_id: projectId, workflow_id, enabled: true })),
      ...(projectId === alpha.id ? [{ project_id: projectId, workflow_id: "inactive-association", enabled: false }] : [])] };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function installHttp() {
  const requests: URL[] = [];
  const state = {
    workflows: async (url: URL): Promise<Response> => {
      const search = url.searchParams.get("search")?.toLowerCase() || "";
      return json(rows.filter((row) => (!search || row.display_name.toLowerCase().includes(search)) && (url.searchParams.get("enabled_only") !== "true" || row.enabled)));
    },
    projects: async (url: URL): Promise<Response> => json({ projects: [alpha, beta], page: Number(url.searchParams.get("page")), per_page: 200, total: 2, total_pages: 1 }),
    membership: async (id: string): Promise<Response> => json(bindings(id)),
  };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost"); requests.push(url);
    if (init?.method && init.method !== "GET") throw new Error(`Unexpected write: ${init.method} ${url.pathname}`);
    if (url.pathname === "/api/workflows") return state.workflows(url);
    if (url.pathname === "/api/v2/projects") return state.projects(url);
    const match = /^\/api\/projects\/([^/]+)\/workflows$/.exec(url.pathname);
    if (match) return state.membership(decodeURIComponent(match[1]));
    throw new Error(`Unexpected request: ${url.pathname}`);
  });
  return { requests, state };
}
function renderPage(initialEntry = "/registry/workflows") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([{ path: "/registry/workflows", element: <WorkflowsListPage /> }], { initialEntries: [initialEntry] });
  return { ...render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>), router, client };
}
function table() { return within(screen.getByRole("table", { name: "Workflows catalog" })); }
async function chooseProject(label: string) {
  fireEvent.click(await screen.findByRole("combobox", { name: "Project" }));
  fireEvent.click(await screen.findByRole("option", { name: label }));
}
beforeEach(() => {
  vi.mocked(useCanReadWorkflowRegistry).mockReturnValue(true); vi.mocked(useIsAdmin).mockReturnValue(true);
  localStorage.setItem(WORKSPACE_STORAGE_KEY, "workspace-primary");
});
afterEach(() => { vi.restoreAllMocks(); });

// First RED reaches existing rendering: ?project_id=Alpha currently also shows Beta.
it("filters the entire catalog by exact Alpha membership, including shared and installed IDs", async () => {
  const { requests } = installHttp(); renderPage(`/registry/workflows?project_id=${alpha.id}`);
  await table().findByRole("link", { name: "Alpha workflow" });
  expect(table().queryByRole("link", { name: "Beta workflow" })).not.toBeInTheDocument();
  expect(table().queryByRole("link", { name: "Unassigned workflow" })).not.toBeInTheDocument();
  expect(table().queryByRole("link", { name: "Other 0" })).not.toBeInTheDocument();
  expect(table().queryByRole("link", { name: "Inactive association workflow" })).not.toBeInTheDocument();
  expect(table().queryByRole("link", { name: "Unlinked workflow sharing a prefix" })).not.toBeInTheDocument();
  expect(table().getByRole("link", { name: "Shared workflow" })).toBeInTheDocument();
  expect(table().getByRole("link", { name: "Installed workflow" })).toBeInTheDocument();
  expect(table().getByRole("link", { name: "Disabled Alpha workflow" })).toBeInTheDocument();
  expect(requests.some((url) => url.pathname === `/api/projects/${alpha.id}/workflows`)).toBe(true);
});
it("defaults to All projects and restores unassigned workflows after an explicit reset", async () => {
  const { requests } = installHttp(); const view = renderPage();
  await table().findByRole("link", { name: "Unassigned workflow" });
  expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("All projects");
  expect(requests.filter((url) => /^\/api\/projects\//.test(url.pathname))).toEqual([]);
  await chooseProject("Beta project"); await table().findByRole("link", { name: "Beta workflow" });
  expect(table().queryByRole("link", { name: "Alpha workflow" })).not.toBeInTheDocument();
  expect(table().getByRole("link", { name: "Shared workflow" })).toBeInTheDocument();
  expect(new URLSearchParams(view.router.state.location.search).get("project_id")).toBe(beta.id);
  await chooseProject("All projects"); await table().findByRole("link", { name: "Unassigned workflow" });
  expect(new URLSearchParams(view.router.state.location.search).has("project_id")).toBe(false);
});
it("preserves search, enabled status and view preferences while resetting page on a project change", async () => {
  const { requests } = installHttp();
  const view = renderPage("/registry/workflows?search=workflow&enabled=1&page=7&view=grid&per_page=50&from=overview");
  await table().findByRole("link", { name: "Unassigned workflow" });
  await chooseProject("Beta project"); await table().findByRole("link", { name: "Beta workflow" });
  const params = new URLSearchParams(view.router.state.location.search);
  expect(params.get("search")).toBe("workflow"); expect(params.get("enabled")).toBe("1");
  expect(params.get("view")).toBe("grid"); expect(params.get("per_page")).toBe("50"); expect(params.get("from")).toBe("overview");
  expect([null, "1"]).toContain(params.get("page"));
  expect(screen.getByRole("searchbox", { name: "Search workflows" })).toHaveValue("workflow");
  expect(requests.filter((url) => url.pathname === "/api/workflows").every((url) => url.searchParams.get("search") === "workflow" && url.searchParams.get("enabled_only") === "true")).toBe(true);
  fireEvent.change(screen.getByRole("searchbox", { name: "Search workflows" }), { target: { value: "Shared" } });
  await waitFor(() => expect(table().queryByRole("link", { name: "Beta workflow" })).not.toBeInTheDocument());
  expect(await table().findByRole("link", { name: "Shared workflow" })).toBeInTheDocument();
  expect(table().queryByRole("link", { name: "Beta workflow" })).not.toBeInTheDocument();
  expect(new URLSearchParams(view.router.state.location.search).get("project_id")).toBe(beta.id);
});
it("restores project and search state on a fresh URL load", async () => {
  installHttp(); const view = renderPage(`/registry/workflows?project_id=${beta.id}&search=Shared&view=grid`);
  await table().findByRole("link", { name: "Shared workflow" });
  expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("Beta project"); view.unmount();
  renderPage(`${view.router.state.location.pathname}${view.router.state.location.search}`);
  await table().findByRole("link", { name: "Shared workflow" });
  expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("Beta project");
  expect(screen.getByRole("searchbox", { name: "Search workflows" })).toHaveValue("Shared");
});
it("withdraws old and unfiltered rows while the next project's membership loads", async () => {
  const { state } = installHttp(); const pending = deferred<Response>();
  state.membership = async (id) => id === beta.id ? pending.promise : json(bindings(id));
  renderPage(`/registry/workflows?project_id=${alpha.id}`); await table().findByRole("link", { name: "Alpha workflow" });
  await chooseProject("Beta project");
  expect(table().queryByRole("link")).not.toBeInTheDocument();
  await act(async () => { pending.resolve(json(bindings(beta.id))); });
  await table().findByRole("link", { name: "Beta workflow" });
  expect(table().queryByRole("link", { name: "Alpha workflow" })).not.toBeInTheDocument();
});
it.each([403, 503])("keeps failed membership scoped for HTTP %s until explicit reset", async (status) => {
  const { state } = installHttp(); state.membership = async () => json({ detail: "Project workflows unavailable" }, status);
  const view = renderPage(`/registry/workflows?project_id=${alpha.id}`);
  expect(await screen.findByRole("alert")).toHaveTextContent(/project/i);
  expect(table().queryByRole("link")).not.toBeInTheDocument();
  expect(new URLSearchParams(view.router.state.location.search).get("project_id")).toBe(alpha.id);
  await chooseProject("All projects"); await table().findByRole("link", { name: "Unassigned workflow" });
});
it("never substitutes a real project for an unknown URL project", async () => {
  const { requests } = installHttp(); const view = renderPage("/registry/workflows?project_id=missing-project");
  await screen.findByRole("alert"); expect(table().queryByRole("link")).not.toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("Project unavailable");
  expect(requests.filter((url) => /^\/api\/projects\//.test(url.pathname))).toEqual([]);
  expect(new URLSearchParams(view.router.state.location.search).get("project_id")).toBe("missing-project");
  await chooseProject("All projects"); await table().findByRole("link", { name: "Unassigned workflow" });
});
it("withholds scoped rows while the authorized project list loads", async () => {
  const { requests, state } = installHttp(); const pending = deferred<Response>(); state.projects = async () => pending.promise;
  renderPage(`/registry/workflows?project_id=${alpha.id}`);
  await waitFor(() => expect(requests.some((url) => url.pathname === "/api/workflows")).toBe(true));
  expect(screen.getByRole("combobox", { name: "Project" })).toBeDisabled(); expect(table().queryByRole("link")).not.toBeInTheDocument();
  await act(async () => { pending.resolve(json({ projects: [alpha, beta], page: 1, per_page: 200, total: 2, total_pages: 1 })); });
  await table().findByRole("link", { name: "Alpha workflow" });
  expect(table().queryByRole("link", { name: "Unassigned workflow" })).not.toBeInTheDocument();
});
it("reports project-list failure without fetching unvalidated membership or exposing unfiltered rows", async () => {
  const { requests, state } = installHttp(); state.projects = async () => json({ detail: "Projects unavailable" }, 503);
  const view = renderPage(`/registry/workflows?project_id=${alpha.id}`); await screen.findByRole("alert");
  expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("Project unavailable");
  expect(screen.getByRole("combobox", { name: "Project" })).toBeEnabled();
  expect(table().queryByRole("link")).not.toBeInTheDocument();
  expect(requests.filter((url) => /^\/api\/projects\//.test(url.pathname))).toEqual([]);
  expect(new URLSearchParams(view.router.state.location.search).get("project_id")).toBe(alpha.id);
});
it("finds authorized projects beyond the first options page in the current workspace", async () => {
  const { requests, state } = installHttp();
  state.projects = async (url) => { const page = Number(url.searchParams.get("page")); return json({ projects: page === 1 ? [alpha] : [beta], page, per_page: 200, total: 201, total_pages: 2 }); };
  renderPage(`/registry/workflows?project_id=${beta.id}`); await table().findByRole("link", { name: "Beta workflow" });
  expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("Beta project");
  expect(requests.filter((url) => url.pathname === "/api/v2/projects").map((url) => [url.searchParams.get("page"), url.searchParams.get("workspace_id")])).toEqual([["1", "workspace-primary"], ["2", "workspace-primary"]]);
});
it("refreshes workspace-scoped choices without substituting the next workspace's first project", async () => {
  const { requests, state } = installHttp();
  state.projects = async (url) => json({ projects: url.searchParams.get("workspace_id") === "workspace-secondary" ? [beta] : [alpha], page: 1, per_page: 200, total: 1, total_pages: 1 });
  const view = renderPage(`/registry/workflows?project_id=${alpha.id}&search=workflow&view=grid`);
  await table().findByRole("link", { name: "Alpha workflow" }); await act(async () => { persistWorkspaceSelection("workspace-secondary"); });
  await waitFor(() => expect(requests.some((url) => url.pathname === "/api/v2/projects" && url.searchParams.get("workspace_id") === "workspace-secondary")).toBe(true));
  fireEvent.click(screen.getByRole("combobox", { name: "Project" })); expect(await screen.findByRole("option", { name: "Beta project" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Alpha project" })).not.toBeInTheDocument();
  expect(new URLSearchParams(view.router.state.location.search).get("project_id")).not.toBe(beta.id);
  expect(requests.filter((url) => url.pathname === `/api/projects/${beta.id}/workflows`)).toEqual([]);
  expect(new URLSearchParams(view.router.state.location.search).get("search")).toBe("workflow"); expect(new URLSearchParams(view.router.state.location.search).get("view")).toBe("grid");
});
it("lets operators filter but keeps viewers from making project or registry requests", async () => {
  const { requests } = installHttp(); vi.mocked(useIsAdmin).mockReturnValue(false);
  const view = renderPage(`/registry/workflows?project_id=${beta.id}`); await table().findByRole("link", { name: "Beta workflow" });
  expect(table().queryByRole("link", { name: "Alpha workflow" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Create workflow" })).not.toBeInTheDocument(); view.unmount(); requests.length = 0;
  vi.mocked(useCanReadWorkflowRegistry).mockReturnValue(false); renderPage(`/registry/workflows?project_id=${beta.id}`);
  await screen.findByText(/The workflow registry catalog is available to administrators/); expect(requests).toEqual([]);
  expect(screen.queryByRole("combobox", { name: "Project" })).not.toBeInTheDocument();
});
it("rejects a membership response that belongs to a different project", async () => {
  const { state } = installHttp(); state.membership = async () => json(bindings(beta.id));
  const view = renderPage(`/registry/workflows?project_id=${alpha.id}`); await screen.findByRole("alert");
  expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("Alpha project");
  expect(screen.getByRole("combobox", { name: "Project" })).toBeEnabled();
  expect(table().queryByRole("link")).not.toBeInTheDocument();
  expect(new URLSearchParams(view.router.state.location.search).get("project_id")).toBe(alpha.id);
});


function accountAuth(accountId: string): AuthContextValue {
  return {
    error: null, isAuthenticated: true, status: "authenticated",
    logout: async () => undefined, refresh: async () => undefined, switchAccount: async () => undefined,
    session: {
      authenticated: true, local_auth_enabled: true, bootstrap_required: false, external_auth_enabled: false,
      roles: ["admin"], permissions: {}, account_memberships: [],
      user: { id: "filter-user", email: "filter@example.test", username: "filter-user", display_name: "Filter user",
        roles: ["admin"], account_id: accountId, account_ids: [accountId], account_memberships: [] },
      i18n: { locale: "en", fallback_locale: "en", supported_locales: [], messages: {} },
      feature_flags: {}, environment: { edition: "community", mode: "development" },
      settings_pages: [], registered_pages: [], shell_navigation: {},
    },
  };
}

it("withdraws the previous account's rows, project options and cached membership on the same QueryClient", async () => {
  const { state } = installHttp();
  let accountId = "account-alpha";
  const pendingProjects = deferred<Response>();
  const pendingWorkflows = deferred<Response>();
  const pendingMembership = deferred<Response>();
  const originalProjects = state.projects;
  const originalWorkflows = state.workflows;
  state.projects = async (url) => accountId === "account-beta" ? pendingProjects.promise : originalProjects(url);
  state.workflows = async (url) => accountId === "account-beta" ? pendingWorkflows.promise : originalWorkflows(url);
  state.membership = async (id) => accountId === "account-beta" ? pendingMembership.promise : json(bindings(id));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 60_000 } } });
  const router = createMemoryRouter([{ path: "/registry/workflows", element: <WorkflowsListPage /> }], {
    initialEntries: [`/registry/workflows?project_id=${beta.id}&search=workflow&view=grid`],
  });
  const tree = () => <AuthContext.Provider value={accountAuth(accountId)}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></AuthContext.Provider>;
  const view = render(tree());
  // Prime Beta membership in the first account, then leave Alpha selected.
  await table().findByRole("link", { name: "Beta workflow" });
  await chooseProject("Alpha project");
  await table().findByRole("link", { name: "Alpha workflow" });
  accountId = "account-beta";
  view.rerender(tree());
  expect(table().queryByRole("link")).not.toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Project" })).toBeDisabled();
  await act(async () => { pendingProjects.resolve(json({ projects: [beta], page: 1, per_page: 200, total: 1, total_pages: 1 })); });
  await screen.findByRole("alert");
  expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("Project unavailable");
  expect(new URLSearchParams(router.state.location.search).get("project_id")).toBe(alpha.id);
  fireEvent.click(screen.getByRole("combobox", { name: "Project" }));
  expect(await screen.findByRole("option", { name: "Beta project" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Alpha project" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("option", { name: "All projects" }));
  // Resetting to All must not expose the previous account's cached global list.
  expect(table().queryByRole("link")).not.toBeInTheDocument();
  await act(async () => { pendingWorkflows.resolve(json([{ workflow_id: "beta-only", display_name: "Beta workflow", enabled: true }])); });
  await table().findByRole("link", { name: "Beta workflow" });
  await chooseProject("Beta project");
  // Nor may the first account's still-fresh Beta membership authorize these rows.
  expect(table().queryByRole("link")).not.toBeInTheDocument();
  await act(async () => { pendingMembership.resolve(json({ project_id: beta.id, workflow_ids: ["beta-only"], operator_default_workflow_id: "beta-only", project_workflows: [{ project_id: beta.id, workflow_id: "beta-only", enabled: true }] })); });
  await table().findByRole("link", { name: "Beta workflow" });
  expect(table().queryByRole("link", { name: "Alpha workflow" })).not.toBeInTheDocument();
  expect(table().queryByRole("link", { name: "Shared workflow" })).not.toBeInTheDocument();
  expect(new URLSearchParams(router.state.location.search).get("search")).toBe("workflow");
  expect(new URLSearchParams(router.state.location.search).get("view")).toBe("grid");
});


it("withdraws cached membership rows after a same-key background failure and recovers only after explicit Retry", async () => {
  const { requests, state } = installHttp();
  const view = renderPage(`/registry/workflows?project_id=${alpha.id}&search=workflow&view=grid`);
  await table().findByRole("link", { name: "Alpha workflow" });
  expect(table().getByRole("link", { name: "Shared workflow" })).toBeInTheDocument();
  const membershipQuery = view.client.getQueryCache().find({ queryKey: ["workflows", "project-filter", "membership"], exact: false });
  expect(membershipQuery).toBeDefined();
  const membershipKey = membershipQuery!.queryKey;
  const cachedMembership = view.client.getQueryData(membershipKey);
  expect(cachedMembership).toContain("alpha-only");

  state.membership = async () => json({ detail: "Project membership could not be refreshed" }, 503);
  await act(async () => { await view.client.refetchQueries({ queryKey: membershipKey, exact: true }); });

  expect(await screen.findByRole("alert")).toHaveTextContent("Project membership could not be refreshed");
  // QueryClient retains successful data on a failed background refresh; that
  // cached authorization must no longer be enough to render any workflow row.
  expect(view.client.getQueryData(membershipKey)).toEqual(cachedMembership);
  expect(table().queryByRole("link")).not.toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("Alpha project");
  expect(new URLSearchParams(view.router.state.location.search).get("project_id")).toBe(alpha.id);

  const retry = deferred<Response>();
  state.membership = async () => retry.promise;
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(requests.filter((url) => url.pathname === `/api/projects/${alpha.id}/workflows`)).toHaveLength(3));
  expect(table().queryByRole("link")).not.toBeInTheDocument();
  await act(async () => { retry.resolve(json(bindings(alpha.id))); });

  await table().findByRole("link", { name: "Alpha workflow" });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(table().queryByRole("link", { name: "Beta workflow" })).not.toBeInTheDocument();
  expect(table().queryByRole("link", { name: "Unassigned workflow" })).not.toBeInTheDocument();
  const params = new URLSearchParams(view.router.state.location.search);
  expect(params.get("project_id")).toBe(alpha.id);
  expect(params.get("search")).toBe("workflow");
  expect(params.get("view")).toBe("grid");
});


function WorkflowNavigationDestination() {
  const location = useLocation();
  return <output data-testid="workflow-navigation-destination">{location.pathname}{location.search}</output>;
}

function renderNavigableList(initialEntry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([
    { path: "/registry/workflows", element: <WorkflowsListPage /> },
    { path: "*", element: <WorkflowNavigationDestination /> },
  ], { initialEntries: [initialEntry] });
  return { ...render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>), router };
}

it.each([
  ["Beta project", "row detail", false, "/registry/workflows/shared?project_id=project-beta"],
  ["Beta project", "Builder", true, "/registry/workflows/shared/builder?project_id=project-beta"],
  ["Beta project", "View workflow", false, "/registry/workflows/shared/builder?project_id=project-beta"],
  ["All projects", "row detail", false, "/registry/workflows/shared"],
  ["All projects", "Builder", true, "/registry/workflows/shared/builder"],
  ["All projects", "View workflow", false, "/registry/workflows/shared/builder"],
] as const)("carries %s through the shared workflow anchor and %s navigation, then restores the filtered list on Back", async (projectLabel, action, canEdit, destination) => {
  const { state } = installHttp();
  state.workflows = async () => json([{ workflow_id: "shared", display_name: "Shared workflow", enabled: true,
    project_id: alpha.id, portability: { structure_change_allowed: canEdit, clone_allowed: true, full_export_allowed: true } }]);
  const view = renderNavigableList(`/registry/workflows?project_id=${alpha.id}&search=Shared&enabled=1&view=grid&from=overview`);
  await table().findByRole("link", { name: "Shared workflow" });
  // Change context on the same shared row: cached columns/callbacks must not
  // keep Alpha after the list's explicit choice becomes Beta or All projects.
  await chooseProject(projectLabel);
  const expectedProject = projectLabel === "Beta project" ? beta.id : null;
  await waitFor(() => expect(new URLSearchParams(view.router.state.location.search).get("project_id")).toBe(expectedProject));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent(projectLabel));
  await table().findByRole("link", { name: "Shared workflow" });
  const expectedHref = projectLabel === "Beta project"
    ? "/registry/workflows/shared?project_id=project-beta"
    : "/registry/workflows/shared";
  expect(table().getByRole("link", { name: "Shared workflow" })).toHaveAttribute("href", expectedHref);
  if (action === "row detail") {
    fireEvent.click(table().getByRole("cell", { name: "Shared workflow" }));
  } else {
    fireEvent.click(screen.getByRole("button", { name: "Actions for shared" }));
    fireEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: action }));
  }
  expect(await screen.findByTestId("workflow-navigation-destination")).toHaveTextContent(destination);
  expect(`${view.router.state.location.pathname}${view.router.state.location.search}`).toBe(destination);
  await act(async () => { await view.router.navigate(-1); });
  await table().findByRole("link", { name: "Shared workflow" });
  const params = new URLSearchParams(view.router.state.location.search);
  expect(params.get("project_id")).toBe(expectedProject);
  expect(params.get("search")).toBe("Shared");
  expect(params.get("enabled")).toBe("1");
  expect(params.get("view")).toBe("grid");
  expect(params.get("from")).toBe("overview");
  expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent(projectLabel);
  expect(screen.getByRole("searchbox", { name: "Search workflows" })).toHaveValue("Shared");
  expect(table().getByRole("link", { name: "Shared workflow" })).toHaveAttribute("href", expectedHref);
});

it("keeps the configured app base exactly once on the project-scoped shared workflow anchor", async () => {
  installHttp();
  vi.stubEnv("BASE_URL", "/app/");
  try {
    renderPage(`/registry/workflows?project_id=${beta.id}`);
    const link = await table().findByRole("link", { name: "Shared workflow" });
    expect(link).toHaveAttribute("href", "/app/registry/workflows/shared?project_id=project-beta");
  } finally {
    vi.unstubAllEnvs();
  }
});
