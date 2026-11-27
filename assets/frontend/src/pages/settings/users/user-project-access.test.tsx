import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { useAuth } from "@/lib/auth/useAuth";
import { SettingsUsersPage } from "@/pages/settings/users";
import { fetchAccountsCollection, fetchUsersCollection } from "@/pages/settings/shared/api";
import { permissionsForRoles } from "@/test/role-permissions";

vi.mock("@/lib/auth/useAuth", () => ({ useAuth: vi.fn() }));
vi.mock("@/pages/settings/shared/api", () => ({
  cancelAccountInvitation: vi.fn(),
  fetchSentAccountInvitations: vi.fn(async () => []),
  SENT_INVITATIONS_QUERY_KEY: ["settings", "users", "invitations"],
  createUser: vi.fn(),
  deleteUser: vi.fn(),
  fetchAccountsCollection: vi.fn(),
  fetchUsersCollection: vi.fn(),
  forceResetUser: vi.fn(),
  setTemporaryUserPassword: vi.fn(),
  unlockUser: vi.fn(),
  updateUser: vi.fn(),
  updateUserAuthenticator: vi.fn(),
}));

const BASE = "/api/modules/acme.access/scoped-roles";

const maya = { id: "u1", email: "maya@example.com", username: "maya@example.com", display_name: "Maya Chen", roles: ["operator"], is_active: true, must_change_password: false, account_id: "acc-1", account_ids: ["acc-1"] };
const ivan = { id: "u2", email: "ivan@example.com", username: "ivan@example.com", display_name: "Ivan Petrov", roles: ["viewer"], is_active: true, must_change_password: false, account_id: "acc-1", account_ids: ["acc-1"] };

function authValue(flags: Record<string, boolean>, endpoints: Record<string, string>): AuthContextValue {
  return {
    error: null,
    isAuthenticated: true,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    status: "authenticated",
    session: {
      authenticated: true, local_auth_enabled: true, bootstrap_required: false, external_auth_enabled: false, auth_source: "local", roles: ["admin"],
      user: { id: "admin-self", email: "admin@example.com", username: "admin@example.com", display_name: "Admin", roles: ["admin"], permissions: permissionsForRoles(["admin"]), account_id: "acc-1", account_ids: ["acc-1"], account_memberships: [] },
      permissions: { can_read: true, can_mutate: true },
      account_memberships: [], i18n: { locale: "en", fallback_locale: "en", supported_locales: [], messages: {} },
      feature_flags: flags, feature_endpoints: endpoints,
      environment: { edition: "community", mode: "production" }, settings_pages: [], registered_pages: [], shell_navigation: {},
    } as unknown as AuthContextValue["session"],
  };
}

type Call = { url: string; method: string; body: unknown };

const CANDIDATES = {
  workspaces: [{ workspace_id: "w1", name: "Accounting" }, { workspace_id: "w2", name: "Marketing" }],
  projects: [
    { project_id: "p1", workspace_id: "w1", name: "Ledger" },
    { project_id: "p2", workspace_id: "w1", name: "Payroll" },
    { project_id: "p3", workspace_id: "w2", name: "Campaigns" },
  ],
  workflows: [
    { workflow_id: "wf-invoice", project_id: "p1", name: "Invoice" },
    { workflow_id: "wf-report", project_id: "p3", name: "Report" },
    { workflow_id: "wf-post", project_id: "p3", name: "Post" },
  ],
};

function stubModule(): Call[] {
  const calls: Call[] = [];
  let grants: Array<Record<string, string>> = [
    { user_id: "u1", scope: "workspace", id: "w1", name: "Accounting", workspace_id: "w1", workspace_name: "Accounting", role: "viewer" },
    { user_id: "u1", scope: "project", id: "p2", name: "Payroll", project_id: "p2", workspace_id: "w1", role: "operator" },
    { user_id: "u1", scope: "project", id: "p3", name: "Campaigns", project_id: "p3", workspace_id: "w2", role: "viewer" },
    { user_id: "u1", scope: "workflow", id: "wf-invoice", name: "Invoice", project_id: "p1", workspace_id: "w1", role: "operator" },
  ];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) as unknown : undefined;
    calls.push({ url, method, body });
    if (url === `${BASE}/candidates`) return Response.json(CANDIDATES);
    if (method === "PUT") {
      const next = (body as { grants: Array<Record<string, string>> }).grants;
      grants = next.map((grant) => ({ user_id: "u2", ...grant }));
      return Response.json({ ok: true });
    }
    if (method === "DELETE") return Response.json({ ok: true });
    const userId = new URL(url, "https://example.test").searchParams.get("user_id");
    return Response.json({ grants: userId ? grants.filter((grant) => grant.user_id === userId) : grants });
  }));
  return calls;
}

function renderPage(auth: AuthContextValue) {
  vi.mocked(useAuth).mockReturnValue(auth);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <AuthContext.Provider value={auth}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/settings/users"]}><SettingsUsersPage /></MemoryRouter>
      </QueryClientProvider>
    </AuthContext.Provider>,
  );
}

async function openEdit(name: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: `Actions for ${name}` }));
  await user.click(screen.getByRole("menuitem", { name: "Edit access" }));
  return { user, dialog: screen.getByRole("dialog", { name: `Edit access for ${name}` }) };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchAccountsCollection).mockResolvedValue([{ id: "acc-1", name: "Flow Steward" }] as never);
  vi.mocked(fetchUsersCollection).mockResolvedValue({ users: [maya, ivan], total: 2, page: 1, per_page: 5, total_pages: 1 } as never);
});

afterEach(() => { vi.unstubAllGlobals(); });

it("shows neither the column nor the dialog section, and asks nothing, without the feature", async () => {
  const calls = stubModule();
  renderPage(authValue({}, { scoped_roles: BASE }));
  await screen.findByText("Maya Chen");
  expect(screen.queryByRole("columnheader", { name: "Project access" })).not.toBeInTheDocument();
  const { dialog } = await openEdit("Maya Chen");
  expect(within(dialog).queryByTestId("user-project-access")).not.toBeInTheDocument();
  expect(calls).toEqual([]);
});

it("hides project access when the flag is on but no endpoint is named", async () => {
  const calls = stubModule();
  renderPage(authValue({ scoped_roles: true }, {}));
  await screen.findByText("Maya Chen");
  expect(screen.queryByRole("columnheader", { name: "Project access" })).not.toBeInTheDocument();
  expect(calls).toEqual([]);
});

it("shows the places each person reaches with the feature", async () => {
  stubModule();
  renderPage(authValue({ scoped_roles: true }, { scoped_roles: BASE }));
  await screen.findByRole("columnheader", { name: "Project access" });
  await waitFor(() => expect(screen.getAllByTestId("user-project-access-cell").map((cell) => cell.textContent)).toEqual([
    "Accounting (workspace), 2 projects, 1 workflow",
    "All projects",
  ]));
});

it("shows the tree with the stored roles and what each place inherits", async () => {
  stubModule();
  renderPage(authValue({ scoped_roles: true }, { scoped_roles: BASE }));
  const { user, dialog } = await openEdit("Maya Chen");
  const section = within(dialog).getByTestId("user-project-access");
  await waitFor(() => expect(within(section).getByRole("switch", { name: "Limit to chosen places" })).toBeChecked());

  await user.click(within(section).getByRole("button", { name: /Accounting/ }));
  expect(within(section).getByRole("combobox", { name: "Role in Accounting" })).toHaveTextContent("Viewer");
  // Ledger has no grant of its own: it follows the workspace.
  expect(within(section).getByRole("combobox", { name: "Role in Ledger" })).toHaveTextContent("As above (Viewer)");
  expect(within(section).getByRole("combobox", { name: "Role in Payroll" })).toHaveTextContent("Operator");
  expect(within(section).getByRole("combobox", { name: "Role in Invoice" })).toHaveTextContent("Operator");

  await user.click(within(section).getByRole("button", { name: /Marketing/ }));
  expect(within(section).getByRole("combobox", { name: "Role in Marketing" })).toHaveTextContent("No access");
  expect(within(section).getByRole("combobox", { name: "Role in Report" })).toHaveTextContent("As above (Viewer)");
});

it("limits a person to a workspace, a project and a workflow and saves through the module", async () => {
  const calls = stubModule();
  renderPage(authValue({ scoped_roles: true }, { scoped_roles: BASE }));
  const { user, dialog } = await openEdit("Ivan Petrov");
  const section = within(dialog).getByTestId("user-project-access");
  await waitFor(() => expect(within(section).getByRole("switch", { name: "Limit to chosen places" })).toBeEnabled());
  const limit = within(section).getByRole("switch", { name: "Limit to chosen places" });
  expect(limit).not.toBeChecked();
  expect(within(section).getByRole("button", { name: "Save project access" })).toBeDisabled();

  await user.click(limit);
  await user.click(within(section).getByRole("button", { name: /Accounting/ }));
  await user.click(within(section).getByRole("combobox", { name: "Role in Accounting" }));
  await user.click(await screen.findByRole("option", { name: "Viewer" }));
  await user.click(within(section).getByRole("combobox", { name: "Role in Payroll" }));
  await user.click(await screen.findByRole("option", { name: "Operator" }));
  await user.click(within(section).getByRole("button", { name: /Marketing/ }));
  // Without a grant above, a workflow starts with no access.
  expect(within(section).getByRole("combobox", { name: "Role in Post" })).toHaveTextContent("No access");
  await user.click(within(section).getByRole("combobox", { name: "Role in Post" }));
  await user.click(await screen.findByRole("option", { name: "Viewer" }));
  await user.click(within(section).getByRole("button", { name: "Save project access" }));

  await waitFor(() => expect(calls.find((call) => call.method === "PUT")).toEqual({
    url: `${BASE}/users/u2/grants`,
    method: "PUT",
    body: {
      grants: [
        { scope: "workspace", id: "w1", role: "viewer" },
        { scope: "project", id: "p2", role: "operator" },
        { scope: "workflow", id: "wf-post", project_id: "p3", role: "viewer" },
      ],
    },
  }));
  await within(section).findByText("Project access saved.");
});

it("removes a place's own grant when it goes back to what it inherits", async () => {
  const calls = stubModule();
  renderPage(authValue({ scoped_roles: true }, { scoped_roles: BASE }));
  const { user, dialog } = await openEdit("Maya Chen");
  const section = within(dialog).getByTestId("user-project-access");
  await waitFor(() => expect(within(section).getByRole("switch", { name: "Limit to chosen places" })).toBeChecked());
  await user.click(within(section).getByRole("button", { name: /Accounting/ }));
  await user.click(within(section).getByRole("combobox", { name: "Role in Payroll" }));
  await user.click(await screen.findByRole("option", { name: "As above (Viewer)" }));
  await user.click(within(section).getByRole("button", { name: "Save project access" }));

  await waitFor(() => expect(calls.find((call) => call.method === "PUT")?.body).toEqual({
    grants: [
      { scope: "workspace", id: "w1", role: "viewer" },
      { scope: "project", id: "p3", role: "viewer" },
      { scope: "workflow", id: "wf-invoice", project_id: "p1", role: "operator" },
    ],
  }));
});

it("lifts a limit when the switch is turned off", async () => {
  const calls = stubModule();
  renderPage(authValue({ scoped_roles: true }, { scoped_roles: BASE }));
  const { user, dialog } = await openEdit("Maya Chen");
  const section = within(dialog).getByTestId("user-project-access");
  await waitFor(() => expect(within(section).getByRole("switch", { name: "Limit to chosen places" })).toBeChecked());
  await user.click(within(section).getByRole("switch", { name: "Limit to chosen places" }));
  await user.click(within(section).getByRole("button", { name: "Save project access" }));
  await waitFor(() => expect(calls.find((call) => call.method === "DELETE")?.url).toBe(`${BASE}/users/u1/grants`));
});
