import { act, fireEvent, queryHelpers, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider } from "react-router";

import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { hasPendingRequests } from "@/lib/http/request-activity";
import { WorkflowsOverviewPage } from "@/pages/workflows/overview";

const workflowId = "lazy-settings-fixture";
const projectId = "project-settings-fixture";
const rootPath = `/registry/workflows/${workflowId}`;
const contractPath = `/api/workflows/${workflowId}/contract`;

function auth(role: "admin" | "operator"): AuthContextValue {
  return {
    error: null,
    isAuthenticated: true,
    logout: async () => undefined,
    refresh: async () => undefined,
    switchAccount: async () => undefined,
    status: "authenticated",
    session: {
      authenticated: true,
      local_auth_enabled: true,
      bootstrap_required: false,
      external_auth_enabled: false,
      roles: [role],
      permissions: {},
      account_memberships: [],
      user: {
        id: "settings-fixture-user", email: "settings-fixture@example.invalid",
        username: "settings-fixture", display_name: "Settings fixture",
        roles: [role], account_id: "settings-fixture-account",
        account_ids: ["settings-fixture-account"], account_memberships: [],
        timezone: "UTC", locale: "en",
      },
      i18n: { locale: "en", fallback_locale: "en", supported_locales: [], messages: {} },
      feature_flags: {}, environment: { edition: "community" },
      settings_pages: [], registered_pages: [], shell_navigation: {},
    },
  };
}

type FixtureOptions = {
  callable?: Record<string, unknown>;
  normalized?: Record<string, unknown>;
  synthesizeEmptySchema?: boolean;
  protected?: boolean;
  installed?: boolean;
  patchError?: string;
  contractError?: boolean;
  failSecretOnce?: boolean;
  failFileOnce?: boolean;
  steps?: unknown[];
};

function fixture(options: FixtureOptions = {}) {
  const requested: Array<{ method: string; path: string; body?: unknown; cache?: RequestCache }> = [];
  const unexpected: string[] = [];
  const callable: Record<string, unknown> = {
    enabled: true, trigger_channels: ["schedule"], input_mode: "fields",
    strict_inputs: true, expected_inputs: [], input_schema: {},
    output_mode: "none", expected_outputs: [],
    ...options.callable,
  };
  const portability = {
    workflow_id: workflowId, extension_owned: Boolean(options.installed), protected: options.protected || false,
    full_export_allowed: true, reference_export_allowed: false,
    clone_allowed: true, structure_read_allowed: true,
    structure_change_allowed: !options.protected && !options.installed, step_test_run_allowed: !options.protected,
  };
  const detail = {
    workflow_id: workflowId, display_name: "Shipping status workflow",
    description: "Owned Settings fixture", enabled: true, revision: 7,
    source_install_id: options.installed ? "fixture-install" : undefined,
    settings: { retained_setting: { keep: true }, callable }, portability, steps: options.steps || [], groups: [], for_each: [],
  };
  const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
    status, headers: { "content-type": "application/json" },
  });
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
    const path = url.pathname;
    const method = init?.method || (input instanceof Request ? input.method : "GET");
    const body = typeof init?.body === "string" ? JSON.parse(init.body) as Record<string, unknown> : init?.body;
    requested.push({ method, path, body, cache: init?.cache || (input instanceof Request ? input.cache : undefined) });
    if (method === "PATCH" && path === `/api/workflows/${workflowId}`) {
      if (options.patchError) return json({ detail: options.patchError }, 409);
      const patch = body as Record<string, unknown>;
      expect(patch.expected_revision).toBe(detail.revision);
      Object.assign(detail, patch, { revision: detail.revision + 1 });
      return json(detail);
    }
    if (method === "PUT" && /\/inputs\/.*\/(secret|sample-file)$/.test(path)) {
      const secret = path.endsWith("/secret");
      if (secret && options.failSecretOnce) { options.failSecretOnce = false; return json({ detail: "Secret store unavailable" }, 503); }
      if (!secret && options.failFileOnce) { options.failFileOnce = false; return json({ detail: "File store unavailable" }, 503); }
      const expected = body instanceof FormData ? Number(body.get("expected_revision")) : (body as Record<string, unknown>).expected_revision;
      expect(expected).toBe(detail.revision);
      detail.revision += 1;
      return json({ revision: detail.revision, default_configured: true });
    }
    if (method === "GET") {
      if (path === `/api/workflows/${workflowId}`) return json(detail);
      if (path === "/api/workflows/second-settings-fixture") return json({ ...detail, workflow_id: "second-settings-fixture", display_name: "Second shipping workflow", portability: { ...portability, workflow_id: "second-settings-fixture" } });
      if (path === `/api/workflows/${workflowId}/contract-validation` || path === "/api/workflows/second-settings-fixture/contract-validation") {
        return json({ workflow_id: workflowId, validation: { valid: true, errors: [], warnings: [] } });
      }
      if (path === `/api/workflows/${workflowId}/stats` || path === "/api/workflows/second-settings-fixture/stats") {
        return json({
          workflow_id: workflowId, total_runs: 0, completed_count: 0,
          failed_count: 0, active_count: 0, cancelled_count: 0,
          success_rate: null, total_cost_usd: 0, avg_cost_per_run_usd: 0,
          jobs_with_cost: 0, last_run_at: null, avg_duration_seconds: null,
        });
      }
      if (path === "/api/workflows/second-settings-fixture/contract") {
        return json({ ...detail, workflow_id: "second-settings-fixture", interface: detail.settings.callable, definition_issues: [] });
      }
      if (path === contractPath) {
        if (options.contractError) return json({ detail: "Contract unavailable" }, 503);
        const stored = options.normalized || detail.settings.callable;
        const publicInterface = options.synthesizeEmptySchema && stored.input_mode === "json_schema" && !Object.keys((stored.input_schema || {}) as Record<string, unknown>).length
          ? { ...stored, input_schema: { type: "object", additionalProperties: false } }
          : stored;
        return json({ ...detail, interface: publicInterface, definition_issues: [] });
      }
      if (path === `/api/workflows/${workflowId}/export-policy`) return json(portability);
      if (path === `/api/workflows/${workflowId}/triggers`) return json({ incoming_webhooks: [], provider_triggers: [], catalog: [] });
      if (path === "/api/v2/projects") {
        expect(url.searchParams.get("page")).toBe("1");
        expect(url.searchParams.get("per_page")).toBe("200");
        return json({ projects: [{ id: projectId, name: "Shipping project" }], total_pages: 1 });
      }
      if (path === `/api/projects/${projectId}/workflows`) return json({ workflow_ids: [workflowId] });
      if (path === `/api/projects/${projectId}/scheduled-jobs`) return json({ jobs: [] });
      if (path === `/api/projects/${projectId}/workflow-catalog`) return json({ workflows: [{ workflow_id: workflowId, display_name: "Shipping status workflow" }] });
      if (path === `/api/workflows/${workflowId}/reactions`) return json({ reactions: [], sources: [] });
      if (path === `/api/projects/${projectId}/providers`) return json({ project_providers: [] });
      if (path === `/api/workflows/${workflowId}/history`) return json([]);
    }
    unexpected.push(`${method} ${path}`);
    // In particular, never provide DAG/canvas or broad project-catalog fixtures
    // that let a full Builder initialization quietly pass this harness.
    return json({ detail: `Unexpected Settings fixture request ${method} ${path}` }, 404);
  });
  return { requested, unexpected, detail };
}

function renderJourney(path: string, role: "admin" | "operator" = "admin", cachedDetail?: unknown) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  if (cachedDetail) client.setQueryData(["workflows", "detail", workflowId], cachedDetail);
  const router = createMemoryRouter([
    { path: "/registry/workflows/:workflowId", element: <WorkflowsOverviewPage /> },
    // A navigation trap, not a mock of Settings or Builder implementation.
    { path: "*", element: <h1>Unexpected workflow route</h1> },
  ], { initialEntries: [path] });
  const view = render(
    <AuthContext.Provider value={auth(role)}>
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
        <RequestLoadingIndicator />
      </QueryClientProvider>
    </AuthContext.Provider>,
  );
  return { router, client, dispose: () => { view.unmount(); router.dispose(); client.clear(); } };
}

function expectNoGraphReads(requested: Array<{ path: string }>) {
  expect(requested.filter(({ path }) => /\/(dag|canvas-graph)$/.test(path))).toEqual([]);
}

afterEach(() => { vi.restoreAllMocks(); });

it("opens real Settings from Overview without initializing the Builder or losing the explicit project", async () => {
  const state = fixture();
  const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}`);
  try {
    await screen.findByRole("heading", { name: "Shipping status workflow" });
    await waitFor(() => expect(hasPendingRequests()).toBe(false));
    expectNoGraphReads(state.requested);
    expect(state.requested.map(({ path }) => path)).not.toContain(contractPath);
    expect(state.requested.map(({ path }) => path)).not.toContain(`/api/workflows/${workflowId}/export-policy`);
    expect(screen.queryByRole("textbox", { name: "Display name" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Manage schedules" })).not.toBeInTheDocument();
    expect(state.unexpected).toEqual([]);

    await user.click(screen.getByRole("button", { name: "Workflow actions" }));
    const settings = within(await screen.findByRole("menu", { name: "Workflow actions" }))
      .getByRole("menuitem", { name: "Workflow settings" });
    const href = settings.getAttribute("href");
    expect(href).not.toBeNull();
    const destination = new URL(href || "", "http://localhost");
    expect(destination.pathname).toBe(rootPath);
    expect(destination.searchParams.get("project_id")).toBe(projectId);
    expect(destination.searchParams.get("tab")).toBe("smart-setup");

    await user.click(settings);
    await screen.findByRole("textbox", { name: "Display name" });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Display name" }))
      .toHaveValue("Shipping status workflow"));
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("Owned Settings fixture");
    expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("enabled");
    expect(screen.getByRole("switch", { name: "Enable workflow interface" })).toBeChecked();
    // With nothing to save, Save workflow settings rests.
    expect(screen.getByRole("button", { name: "Save workflow settings" })).toBeDisabled();
    // Save sits alone in the footer: Manage schedules is the Schedules tab now.
    expect(screen.queryByRole("button", { name: "Manage schedules" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Workflow settings" })).not.toBeInTheDocument();
    expect(view.router.state.location.pathname).toBe(rootPath);
    expect(new URLSearchParams(view.router.state.location.search).get("project_id")).toBe(projectId);
    expect(screen.queryByRole("heading", { name: "Unexpected workflow route" })).not.toBeInTheDocument();
    expectNoGraphReads(state.requested);
    expect(state.requested.map(({ path }) => path)).toContain(contractPath);
    expect(state.requested.filter(({ path }) => path === contractPath).every(({ cache }) => cache === "no-store")).toBe(true);
    expect(state.requested.every(({ method }) => method === "GET")).toBe(true);
    expect(state.unexpected).toEqual([]);
    await waitFor(() => expect(hasPendingRequests()).toBe(false));
  } finally { view.dispose(); }
});

it("lets an admin inspect a licensed installed contract through Settings without source edits", async () => {
  const state = fixture({ installed: true, callable: { expected_inputs: [{ name: "customer", type: "string", required: true }] } });
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    const name = await screen.findByRole("textbox", { name: "Display name" });
    expect(name).toHaveValue("Shipping status workflow");
    expect(name).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Description" })).toBeDisabled();
    expect(screen.getByText("customer")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save workflow settings" })).not.toBeInTheDocument();
    expect(state.requested.filter(({ method }) => method !== "GET")).toEqual([]);
    expectNoGraphReads(state.requested);
  } finally { view.dispose(); }
});

it("does not initialize author Settings for an operator's direct Overview tab", async () => {
  const state = fixture();
  const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`, "operator");
  try {
    await screen.findByRole("heading", { name: "Shipping status workflow" });
    await waitFor(() => expect(hasPendingRequests()).toBe(false));
    await user.click(screen.getByRole("button", { name: "Workflow actions" }));
    expect(within(await screen.findByRole("menu", { name: "Workflow actions" }))
      .queryByRole("menuitem", { name: "Workflow settings" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Display name" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Smart setup" })).not.toBeInTheDocument();
    expect(state.requested.map(({ path }) => path)).not.toContain(contractPath);
    expectNoGraphReads(state.requested);
    expect(state.requested.every(({ method }) => method === "GET")).toBe(true);
    expect(view.router.state.location.pathname).toBe(rootPath);
    expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

async function openSettings() {
  await screen.findByRole("textbox", { name: "Display name" });
  await waitFor(() => expect(hasPendingRequests()).toBe(false));
}

function inputControl<T extends HTMLInputElement | HTMLTextAreaElement>(name: string): T {
  const node = queryHelpers.queryByAttribute("name", document.body, name) as T | null;
  expect(node).not.toBeNull();
  return node!;
}

it("saves the same normalized callable contract, typed defaults, metadata and revision without graph requests", async () => {
  const state = fixture({ normalized: {
    enabled: false, trigger_channels: [], input_mode: "fields", strict_inputs: false,
    expected_inputs: [
      { name: "flag", type: "boolean", default: false, default_configured: true },
      { name: "count", type: "number", default: 0, default_configured: true },
      { name: "label", type: "string", default: "", default_configured: true },
      { name: "generic", type: "object", default: {}, default_configured: true },
    ], output_mode: "variables_and_artifacts", expected_outputs: [{ name: "actual", type: "object" }],
    returned_files: [{ name: "export", type: "file", required: false, source: { step_id: "write_export", step_name: "Write export", result: "file", target: "state.export" } }],
  } });
  const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await openSettings();
    expect(screen.getByLabelText("Enable workflow interface")).not.toBeChecked();
    expect(state.requested.filter(({ path }) => path === contractPath).map(({ cache }) => cache)).toEqual(["no-store"]);
    expect(screen.getByLabelText("Schedule")).not.toBeChecked();
    await user.clear(screen.getByRole("textbox", { name: "Description" }));
    await user.type(screen.getByRole("textbox", { name: "Description" }), "Updated shipping contract");
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await screen.findByText("Workflow settings saved.");
    const contractReads = state.requested.filter(({ path }) => path === contractPath);
    expect(contractReads.length).toBeGreaterThanOrEqual(2);
    expect(contractReads.every(({ cache }) => cache === "no-store")).toBe(true);
    const patch = state.requested.find(({ method }) => method === "PATCH")?.body as { settings: { callable: Record<string, unknown> } };
    expect(patch).toMatchObject({ expected_revision: 7, description: "Updated shipping contract", settings: { retained_setting: { keep: true }, callable: { enabled: false, trigger_channels: [], strict_inputs: false } } });
    expect(patch.settings.callable.expected_inputs).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: "flag", default: false }), expect.objectContaining({ name: "count", default: 0 }),
      expect.objectContaining({ name: "label", default: "" }), expect.objectContaining({ name: "generic", default: {} }),
    ]));
    expect(patch.settings.callable.expected_outputs).toEqual([expect.objectContaining({ name: "actual", type: "object" })]);
    // Returned files belong to their steps: shown in Outputs, never written into the contract.
    expect(patch.settings.callable).not.toHaveProperty("returned_files");
    expect(patch.settings.callable).not.toHaveProperty("expected_artifacts");
    expect(patch.settings.callable).not.toHaveProperty("side_effects");
    expectNoGraphReads(state.requested); expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it.each(["failSecretOnce", "failFileOnce"] as const)("retains rejected %s actions while applying each successful secret/file once with sequential revisions", async (failure) => {
  const state = fixture({ [failure]: true, callable: { expected_inputs: [{ name: "token", type: "secret" }, { name: "document", type: "file" }] } });
  const user = userEvent.setup(); const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await openSettings();
    // Each contract row opens from its summary line, one at a time.
    await user.click(await screen.findByRole("button", { name: /^token/ }));
    await waitFor(() => expect(inputControl("workflow_interface_expected_inputs_0_default")).toHaveAttribute("type", "password"));
    await user.type(inputControl("workflow_interface_expected_inputs_0_default"), "test-secret-only");
    await user.click(screen.getByRole("button", { name: "Set" }));
    await user.click(screen.getByRole("button", { name: /^document/ }));
    await user.upload(inputControl("workflow_interface_expected_inputs_1_default"), new File(["sample"], "sample.txt", { type: "text/plain" }));
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await screen.findByText(/Workflow settings saved. Not applied:/);
    expect(state.requested.filter(({ method }) => method === "PUT").map(({ path }) => path.split("/").at(-1))).toEqual(["secret", "sample-file"]);
    const firstPatch = state.requested.find(({ method }) => method === "PATCH");
    expect(JSON.stringify(firstPatch?.body)).not.toContain("test-secret-only");
    expect(state.detail.revision).toBe(9);
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await screen.findByText("Workflow settings saved.");
    const writes = state.requested.filter(({ method }) => method !== "GET");
    expect(writes.map(({ method, path }) => method === "PATCH" ? "PATCH" : path.split("/").at(-1))).toEqual(["PATCH", "secret", "sample-file", "PATCH", failure === "failSecretOnce" ? "secret" : "sample-file"]);
    expect(state.detail.revision).toBe(11);
    expectNoGraphReads(state.requested); expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it("keeps dirty drafts through detail refetch and a change of tab, blocks pathname leave and beforeunload, then confirms discard", async () => {
  const state = fixture(); const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup&retained=yes`);
  try {
    await openSettings(); const field = screen.getByRole("textbox", { name: "Display name" });
    await user.clear(field); await user.type(field, "Unsaved shipping name");
    state.detail.display_name = "Server refresh name";
    await act(async () => { await view.client.invalidateQueries({ queryKey: ["workflows", "detail", workflowId] }); });
    expect(field).toHaveValue("Unsaved shipping name");
    const unload = new Event("beforeunload", { cancelable: true }); globalThis.dispatchEvent(unload); expect(unload.defaultPrevented).toBe(true);
    // Another tab hides the form; its draft stays.
    await user.click(screen.getByRole("link", { name: "Overview" }));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Display name" })).not.toBeInTheDocument());
    expect(new URLSearchParams(view.router.state.location.search).get("project_id")).toBe(projectId);
    expect(new URLSearchParams(view.router.state.location.search).get("retained")).toBeNull();
    expect(new URLSearchParams(view.router.state.location.search).get("tab")).toBeNull();
    await user.click(screen.getByRole("link", { name: "Smart setup" }));
    expect(await screen.findByRole("textbox", { name: "Display name" })).toHaveValue("Unsaved shipping name");
    await act(async () => { await view.router.navigate("/elsewhere"); });
    const dialog = await screen.findByRole("dialog", { name: "Unsaved changes" });
    await user.click(within(dialog).getByRole("button", { name: "Stay" }));
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("Unsaved shipping name");
    await act(async () => { await view.router.navigate("/elsewhere"); });
    await user.click(within(await screen.findByRole("dialog", { name: "Unsaved changes" })).getByRole("button", { name: "Leave and discard" }));
    await screen.findByRole("heading", { name: "Unexpected workflow route" });
    expectNoGraphReads(state.requested);
  } finally { view.dispose(); }
});

it.each(["platform_workflow_requires_clone", "Workflow revision conflict"])("shows %s without clearing the draft or claiming success", async (patchError) => {
  const state = fixture({ patchError }); const user = userEvent.setup(); const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await openSettings(); await user.type(screen.getByRole("textbox", { name: "Description" }), " local draft");
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await screen.findByText(patchError === "platform_workflow_requires_clone" ? /This platform workflow is read-only/ : patchError);
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("Owned Settings fixture local draft");
    expect(screen.queryByText("Workflow settings saved.")).not.toBeInTheDocument();
    expect(state.requested.filter(({ method }) => method === "PUT")).toEqual([]);
    expectNoGraphReads(state.requested);
  } finally { view.dispose(); }
});

it("keeps the ordinary metadata surface for a protected workflow", async () => {
  const state = fixture({ protected: true }); const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`, "admin", state.detail);
  try {
    await openSettings(); expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("Shipping status workflow");
    expectNoGraphReads(state.requested); expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it("uses the real Webhook, Schedules, Automation and History leaves only while selected", async () => {
  // Webhook and Schedules are tabs only while their channels are on (R2-16).
  const state = fixture({ callable: { trigger_channels: ["schedule", "webhook"] } }); const user = userEvent.setup(); const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await openSettings(); expect(state.requested.some(({ path }) => path.endsWith("/triggers"))).toBe(false);
    // Smart setup carries the whole contract; there is no separate Interface leaf to open.
    const contract = within(screen.getByTestId("workflow-settings-contract"));
    for (const block of ["Expected inputs", "Expected outputs", "Returned files"]) {
      expect(contract.getByRole("region", { name: block })).toBeInTheDocument();
    }
    for (const removed of ["Expected artifacts", "External changes"]) {
      expect(contract.queryByRole("region", { name: removed })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: "Open interface" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Webhook" }));
    await waitFor(() => expect(state.requested.some(({ path }) => path.endsWith("/triggers"))).toBe(true));
    await user.click(screen.getByRole("link", { name: "Schedules" }));
    await screen.findByRole("button", { name: "New schedule" });
    // The project page has no schedules tab any more, so nothing links out to one.
    expect(screen.queryByRole("link", { name: "Open project schedules" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Automation" }));
    await screen.findByRole("button", { name: /Save rules/ });
    await user.click(screen.getByRole("link", { name: "History" }));
    await waitFor(() => expect(state.requested.some(({ path }) => path === `/api/workflows/${workflowId}/history`)).toBe(true));
    expectNoGraphReads(state.requested); expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it("shows a failed normalized contract without initializing empty editable defaults", async () => {
  const state = fixture({ contractError: true }); const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await screen.findByText("Contract unavailable");
    expect(screen.queryByRole("textbox", { name: "Display name" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save workflow settings" })).not.toBeInTheDocument();
    expect(state.requested.every(({ method }) => method === "GET")).toBe(true);
    expectNoGraphReads(state.requested); expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it("opens the real Automation leaf for ?tab=automation without initializing other tabs", async () => {
  const state = fixture(); const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=automation`);
  try {
    await screen.findByRole("button", { name: "Save rules" });
    expect(state.requested.some(({ path }) => path.endsWith("/reactions"))).toBe(true);
    expect(state.requested.some(({ path }) => /\/(triggers|scheduled-jobs)$/.test(path))).toBe(false);
    expect(screen.getByRole("link", { name: "Automation" })).toHaveAttribute("aria-current", "page");
    expectNoGraphReads(state.requested); expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it("releases workflow A's Settings controller before visiting B without a settings tab", async () => {
  const state = fixture(); const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await openSettings();
    await act(async () => { await view.router.navigate(`/registry/workflows/second-settings-fixture?project_id=${projectId}`); });
    await screen.findByRole("heading", { name: "Second shipping workflow" });
    await waitFor(() => expect(hasPendingRequests()).toBe(false));
    expect(screen.queryByRole("textbox", { name: "Display name" })).not.toBeInTheDocument();
    expect(state.requested.some(({ path }) => path === "/api/workflows/second-settings-fixture/contract")).toBe(false);
    const originalContractReads = state.requested.filter(({ path }) => path === contractPath).length;
    await act(async () => { await view.router.navigate(`${rootPath}?project_id=${projectId}`); });
    await screen.findByRole("heading", { name: "Shipping status workflow" });
    expect(screen.queryByRole("textbox", { name: "Display name" })).not.toBeInTheDocument();
    expect(state.requested.filter(({ path }) => path === contractPath)).toHaveLength(originalContractReads);
    expectNoGraphReads(state.requested); expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it("focuses the existing unnamed Interface field on refused Save without sending a write", async () => {
  const state = fixture({ callable: { expected_inputs: [{ name: "source_rows", type: "array", required: true }] } });
  const user = userEvent.setup(); const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await openSettings();
    await user.click(await screen.findByRole("button", { name: /^source_rows/ }));
    await screen.findByDisplayValue("source_rows");
    const name = screen.getByRole("textbox", { name: "Name" });
    expect(name).toBe(screen.getByDisplayValue("source_rows"));
    await user.clear(name);
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => {
      expect(name).toHaveFocus();
    });
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(name).toHaveAccessibleDescription(/name.*required/i);
    expect(name).toHaveValue("");
    expect(state.requested.filter(({ method }) => method !== "GET")).toEqual([]);
    expectNoGraphReads(state.requested); expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

function pageTabNames() {
  return within(screen.getByRole("navigation", { name: "Workflow views" })).getAllByRole("link").map((link) => link.textContent);
}

const CHANNEL_TABS = [
  { name: "no channel", callable: { enabled: true, trigger_channels: ["ui"] }, tabs: ["Overview", "Projects", "Smart setup", "Automation", "History"] },
  { name: "webhook", callable: { enabled: true, trigger_channels: ["webhook"] }, tabs: ["Overview", "Projects", "Smart setup", "Webhook", "Automation", "History"] },
  { name: "schedule", callable: { enabled: true, trigger_channels: ["schedule"] }, tabs: ["Overview", "Projects", "Smart setup", "Schedules", "Automation", "History"] },
  { name: "both", callable: { enabled: true, trigger_channels: ["schedule", "webhook"] }, tabs: ["Overview", "Projects", "Smart setup", "Webhook", "Schedules", "Automation", "History"] },
  { name: "both with the interface off", callable: { enabled: false, trigger_channels: ["schedule", "webhook"] }, tabs: ["Overview", "Projects", "Smart setup", "Automation", "History"] },
];

it.each(CHANNEL_TABS)("lists the workflow page tabs in order for $name", async ({ callable, tabs }) => {
  const state = fixture({ callable }); const view = renderJourney(`${rootPath}?project_id=${projectId}`);
  try {
    await screen.findByRole("heading", { name: "Shipping status workflow" });
    expect(pageTabNames()).toEqual(tabs);
    expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
    expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it("puts Hooks after Projects only when the workflow declares hooks", async () => {
  const hookStep = { step_id: "before_finish", step_kind: "invoke_workflow", override: { invoke_workflow: { hook: { hook_id: "before_finish", display_name: "Before finish" } } } };
  fixture({ steps: [hookStep], callable: { enabled: true, trigger_channels: ["schedule"] } });
  const view = renderJourney(`${rootPath}?project_id=${projectId}`);
  try {
    await screen.findByRole("heading", { name: "Shipping status workflow" });
    expect(pageTabNames()).toEqual(["Overview", "Projects", "Hooks", "Smart setup", "Schedules", "Automation", "History"]);
  } finally { view.dispose(); }
});

it("shows only Overview and Projects to an operator", async () => {
  fixture({ callable: { enabled: true, trigger_channels: ["schedule", "webhook"] } });
  const view = renderJourney(`${rootPath}?project_id=${projectId}`, "operator");
  try {
    await screen.findByRole("heading", { name: "Shipping status workflow" });
    expect(pageTabNames()).toEqual(["Overview", "Projects"]);
  } finally { view.dispose(); }
});

it.each([
  ["smart-setup", "Smart setup"], ["webhook", "Webhook"], ["schedules", "Schedules"], ["automation", "Automation"], ["history", "History"],
])("selects the %s tab from ?tab=", async (key, label) => {
  const state = fixture({ callable: { enabled: true, trigger_channels: ["schedule", "webhook"] } }); const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=${key}`);
  try {
    await screen.findByRole("heading", { name: "Shipping status workflow" });
    expect(screen.getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Overview" })).not.toHaveAttribute("aria-current");
    // The overview's own cards stay out of a settings tab.
    expect(screen.queryByRole("heading", { name: "Run statistics" })).not.toBeInTheDocument();
    await waitFor(() => expect(hasPendingRequests()).toBe(false));
    expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it("shows a channel's tab at once when it is ticked on Smart setup, and hides it when unticked", async () => {
  const state = fixture({ callable: { enabled: true, trigger_channels: ["ui"] } }); const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await openSettings();
    expect(pageTabNames()).toEqual(["Overview", "Projects", "Smart setup", "Automation", "History"]);
    await user.click(screen.getByLabelText("Schedule"));
    expect(pageTabNames()).toEqual(["Overview", "Projects", "Smart setup", "Schedules", "Automation", "History"]);
    await user.click(screen.getByLabelText("Incoming Webhook"));
    expect(pageTabNames()).toEqual(["Overview", "Projects", "Smart setup", "Webhook", "Schedules", "Automation", "History"]);
    await user.click(screen.getByLabelText("Schedule"));
    expect(pageTabNames()).toEqual(["Overview", "Projects", "Smart setup", "Webhook", "Automation", "History"]);
    // Nothing was saved to show them.
    expect(state.requested.filter(({ method }) => method === "PATCH")).toEqual([]);
  } finally { view.dispose(); }
});

it("goes back to Smart setup when the channel of the open tab is turned off", async () => {
  const state = fixture({ callable: { enabled: true, trigger_channels: ["schedule"] } });
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=schedules`);
  try {
    await screen.findByRole("button", { name: "New schedule" });
    expect(screen.getByRole("link", { name: "Schedules" })).toHaveAttribute("aria-current", "page");
    state.detail.settings.callable = { ...state.detail.settings.callable, trigger_channels: [] } as typeof state.detail.settings.callable;
    await act(async () => { await view.client.invalidateQueries({ queryKey: ["workflows", "detail", workflowId] }); });
    await waitFor(() => expect(new URLSearchParams(view.router.state.location.search).get("tab")).toBe("smart-setup"));
    await waitFor(() => expect(screen.queryByRole("link", { name: "Schedules" })).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("link", { name: "Smart setup" })).toHaveAttribute("aria-current", "page"));
  } finally { view.dispose(); }
});

it("leaves a deep link to a channel tab alone while the channel is off", async () => {
  fixture({ callable: { enabled: true, trigger_channels: ["ui"] } });
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=schedules`);
  try {
    await screen.findByRole("heading", { name: "Shipping status workflow" });
    await waitFor(() => expect(hasPendingRequests()).toBe(false));
    expect(new URLSearchParams(view.router.state.location.search).get("tab")).toBe("schedules");
    expect(screen.queryByRole("link", { name: "Schedules" })).not.toBeInTheDocument();
  } finally { view.dispose(); }
});

it("ignores a ?tab= value it does not offer and keeps the Overview", async () => {
  fixture(); const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=hooks`);
  try {
    await screen.findByRole("heading", { name: "Run statistics" });
    expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
  } finally { view.dispose(); }
});

it("keeps an inactive declaration visible through saved Settings reload until explicitly enabled", async () => {
  const state = fixture({ callable: { enabled: false, trigger_channels: [], expected_outputs: [{ name: "summary", type: "string", required: true }] } });
  const user = userEvent.setup();
  let view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    expect(await screen.findByText("Workflow interface is off")).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Description" }), " updated");
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(state.requested.filter(({ method }) => method === "PATCH")).toHaveLength(1));
    expect(state.detail.settings.callable.enabled).toBe(false);
    expect(state.detail.settings.callable.trigger_channels).toEqual([]);
    await waitFor(() => expect(screen.getByRole("button", { name: "Saved" })).toBeDisabled());
    view.dispose();
    view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
    expect(await screen.findByText("Workflow interface is off")).toBeVisible();
    const activation = screen.getByRole("switch", { name: "Enable workflow interface" });
    expect(activation).not.toBeChecked();
    await user.click(activation);
    expect(screen.queryByText("Workflow interface is off")).not.toBeInTheDocument();
    expect(screen.getByText("Choose at least one trigger channel before running a Test Run.")).toBeVisible();
    await user.click(screen.getByRole("checkbox", { name: "UI" }));
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(state.detail.settings.callable.enabled).toBe(true));
    expect(state.detail.settings.callable.trigger_channels).toEqual(["ui"]);
    expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it.each([
  { enabled: false, input_mode: "fields", expected_inputs: [], expected_outputs: [], input_schema: { type: "object", properties: {}, additionalProperties: false } },
  { enabled: true, expected_outputs: [{ name: "summary", type: "string", required: true }] },
])("does not call empty fields or enabled Settings contracts inactive: %j", async (normalized) => {
  fixture({ callable: { enabled: normalized.enabled, expected_outputs: normalized.expected_outputs }, normalized });
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await screen.findByRole("switch", { name: "Enable workflow interface" });
    expect(screen.queryByText("Workflow interface is off")).not.toBeInTheDocument();
  } finally { view.dispose(); }
});

it.each([
  { callable: { enabled: false, input_mode: "json_schema", input_schema: {} }, normalized: { enabled: false, input_mode: "json_schema", input_schema: { type: "object", additionalProperties: false } }, warned: false },
  { callable: { enabled: false, input_mode: "json_schema", input_schema: { type: "object" } }, normalized: { enabled: false, input_mode: "json_schema", input_schema: { type: "object" } }, warned: true },
  { callable: { enabled: false, expected_inputs: [{ name: "request", type: "string", required: true }] }, normalized: undefined, warned: true },
])("warns for an authored schema or input, never the synthesized empty schema: %j", async ({ callable, normalized, warned }) => {
  fixture({ callable, normalized });
  const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await screen.findByRole("switch", { name: "Enable workflow interface" });
    if (warned) expect(await screen.findByText("Workflow interface is off")).toBeVisible();
    else expect(screen.queryByText("Workflow interface is off")).not.toBeInTheDocument();
    // Changing an unrelated field must not turn a synthesized schema into an authored declaration.
    await user.type(screen.getByRole("textbox", { name: "Description" }), " changed");
    expect(Boolean(screen.queryByText("Workflow interface is off"))).toBe(warned);
  } finally { view.dispose(); }
});


it.each([
  { label: "unchanged empty schema", stored: {}, edit: undefined, warned: false },
  { label: "authored common default", stored: { type: "object", additionalProperties: false }, edit: undefined, warned: true },
  { label: "deliberately entered common default", stored: {}, edit: { type: "object", additionalProperties: false }, warned: true },
  { label: "deliberately entered fields", stored: {}, edit: { type: "object", properties: { request: { type: "string" } }, required: ["request"] }, warned: true },
])("preserves $label through a description-only Save and full remount", async ({ stored, edit, warned }) => {
  const state = fixture({ synthesizeEmptySchema: true, callable: {
    enabled: false, trigger_channels: ["ui"], input_mode: "json_schema", input_schema: stored,
    expected_inputs: [], expected_outputs: [],
  } });
  const user = userEvent.setup();
  let view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await screen.findByRole("textbox", { name: "Input JSON schema" });
    if (edit) fireEvent.change(screen.getByRole("textbox", { name: "Input JSON schema" }), { target: { value: JSON.stringify(edit) } });
    await user.type(screen.getAllByRole("textbox", { name: "Description" })[0], " changed");
    if (warned) expect(await screen.findByText("Workflow interface is off")).toBeVisible();
    else expect(screen.queryByText("Workflow interface is off")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(state.detail.revision).toBe(8));
    const patches = state.requested.filter(({ method }) => method === "PATCH");
    expect(patches).toHaveLength(1);
    expect(patches[0].body).toMatchObject({ expected_revision: 7, settings: { callable: {
      enabled: false, trigger_channels: ["ui"], input_mode: "json_schema", input_schema: edit || stored,
    } } });
    const saved = patches[0].body as { settings: { callable: { input_schema: unknown } } };
    expect(saved.settings.callable.input_schema).toEqual(edit || stored);
    expect(state.detail.settings.callable.input_schema).toEqual(edit || stored);
    expect(state.detail.settings.callable.enabled).toBe(false);
    expect(state.detail.settings.callable.trigger_channels).toEqual(["ui"]);
    await screen.findByRole("button", { name: "Saved" });
    view.dispose();
    view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
    const schema = await screen.findByRole("textbox", { name: "Input JSON schema" });
    expect(JSON.parse((schema as HTMLTextAreaElement).value)).toEqual(edit || stored);
    expect(screen.getByRole("switch", { name: "Enable workflow interface" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "UI" })).toBeChecked();
    if (warned) expect(await screen.findByText("Workflow interface is off")).toBeVisible();
    else expect(screen.queryByText("Workflow interface is off")).not.toBeInTheDocument();
    expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});


it("preserves dormant fields-mode schema and authored row metadata on an unrelated save", async () => {
  const dormant = { $defs: { customer: { type: "object", properties: { name: { type: "string" } } } }, note: null };
  const input = { name: "customer", type: "object", required: false, x_author_note: { keep: [0, false, null] } };
  const state = fixture({ callable: { input_mode: "fields", expected_inputs: [input], input_schema: dormant } });
  const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    await screen.findByRole("textbox", { name: "Description" });
    await user.type(screen.getByRole("textbox", { name: "Description" }), " changed");
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(state.requested.filter(({ method }) => method === "PATCH")).toHaveLength(1));
    const patch = state.requested.find(({ method }) => method === "PATCH")?.body as { settings: { callable: Record<string, unknown> } };
    expect(patch.settings.callable.input_mode).toBe("fields");
    expect(patch.settings.callable.input_schema).toEqual(dormant);
    expect(patch.settings.callable.expected_inputs).toEqual([input]);
    expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it("retains imported schema-mode rows and unsupported rules through description-only save", async () => {
  const schema = { type: "object", $defs: { Name: { type: "string", default: null } },
    properties: { customer: { $ref: "#/$defs/Name" } }, required: ["customer"], additionalProperties: false };
  const retained = [{ name: "legacy", type: "string", x_author_note: { keep: 0 } }];
  const state = fixture({ callable: { input_mode: "json_schema", input_schema: schema, expected_inputs: retained } });
  const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    expect(await screen.findByText(/This schema uses advanced rules/)).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Description" }), " changed");
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(state.requested.filter(({ method }) => method === "PATCH")).toHaveLength(1));
    const patch = state.requested.find(({ method }) => method === "PATCH")?.body as { settings: { callable: Record<string, unknown> } };
    expect(patch.settings.callable.input_schema).toEqual(schema);
    expect(patch.settings.callable.expected_inputs).toEqual(retained);
    expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});


it.each([
  { label: "unrepresented root required name", schema: { type: "object", properties: { customer: { type: "string" } }, required: ["other"], additionalProperties: true } },
  { label: "unrepresented object member required name", schema: { type: "object", properties: { customer: { type: "object", properties: { name: { type: "string" } }, required: ["other"], additionalProperties: true } } } },
  { label: "nonstring root annotation", schema: { type: "object", title: { translated: "Inputs" }, properties: { customer: { type: "string" } } } },
  { label: "nonstring member annotation", schema: { type: "object", properties: { customer: { type: "object", properties: { name: { type: "string", description: { translated: "Name" } } } } } } },
])("retains $label through unrelated Settings save", async ({ schema }) => {
  const state = fixture({ callable: { input_mode: "json_schema", input_schema: schema, expected_inputs: [] } });
  const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    expect(await screen.findByText(/This schema uses advanced rules/)).toBeVisible();
    expect(screen.queryByRole("group", { name: "Structured input 1" })).not.toBeInTheDocument();
    await user.type(screen.getAllByRole("textbox", { name: "Description" })[0], " changed");
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(state.requested.filter(({ method }) => method === "PATCH")).toHaveLength(1));
    const patch = state.requested.find(({ method }) => method === "PATCH")?.body as { settings: { callable: Record<string, unknown> } };
    expect(patch.settings.callable.input_schema).toEqual(schema);
    expect(state.detail.settings.callable.input_schema).toEqual(schema);
    expect(state.unexpected).toEqual([]);
  } finally { view.dispose(); }
});

it("keeps a structured input mounted across unrelated Settings edits", async () => {
  fixture({ callable: { input_mode: "json_schema", input_schema: { type: "object", properties: { customer: { type: "string", title: "Customer" } }, required: ["customer"] }, expected_inputs: [] } });
  const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    const row = await screen.findByRole("group", { name: "Structured input 1" });
    await user.type(screen.getAllByRole("textbox", { name: "Description" })[0], " changed");
    expect(screen.getByRole("group", { name: "Structured input 1" })).toBe(row);
  } finally { view.dispose(); }
});


it("clears a local invalid visual row when a different workflow has identical schema text", async () => {
  const schema = { type: "object", properties: { customer: { type: "string", title: "Customer" } }, required: ["customer"] };
  fixture({ callable: { input_mode: "json_schema", input_schema: schema, expected_inputs: [] } });
  const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    const row = await screen.findByRole("group", { name: "Structured input 1" });
    fireEvent.change(within(row).getByRole("textbox", { name: "Name" }), { target: { value: "" } });
    expect(within(row).getByRole("textbox", { name: "Name" })).toHaveValue("");
    expect(screen.getByRole("alert", { name: "" })).toHaveTextContent("Give each structured input a unique name.");
    await act(async () => { await view.router.navigate(`/registry/workflows/second-settings-fixture?project_id=${projectId}&tab=smart-setup`); });
    const discard = await screen.findByRole("dialog", { name: "Unsaved changes" });
    await user.click(within(discard).getByRole("button", { name: "Leave and discard" }));
    await screen.findByRole("heading", { name: "Second shipping workflow" });
    await waitFor(() => expect(within(screen.getByRole("group", { name: "Structured input 1" }))
      .getByRole("textbox", { name: "Name" })).toHaveValue("customer"));
    expect(screen.queryByText("Give each structured input a unique name.")).not.toBeInTheDocument();
  } finally { view.dispose(); }
});


it("retains a literal dotted JSON Schema property in Advanced through an unrelated save", async () => {
  const schema = { type: "object", properties: { "a.b": { type: "string", title: "Literal dotted key" } }, required: ["a.b"] };
  const state = fixture({ callable: { input_mode: "json_schema", input_schema: schema, expected_inputs: [] } });
  const user = userEvent.setup();
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    expect(await screen.findByText(/This schema uses advanced rules/)).toBeVisible();
    await user.type(screen.getAllByRole("textbox", { name: "Description" })[0], " changed");
    await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(state.requested.filter(({ method }) => method === "PATCH")).toHaveLength(1));
    expect(state.detail.settings.callable.input_schema).toEqual(schema);
  } finally { view.dispose(); }
});

it("retains untouched structured schema property presence on a neighboring visual edit", async () => {
  const schema = { type: "object", required: [], properties: {
    untouched: { type: "object", title: "", description: "", required: [], additionalProperties: false },
    with_member: { type: "object", properties: { name: { type: "string", title: "", description: "" } }, required: [] },
    edited: { type: "string", title: "Before" },
  } };
  const state = fixture({ callable: { input_mode: "json_schema", input_schema: schema, expected_inputs: [] } });
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    const row = await screen.findByRole("group", { name: "Structured input 3" });
    fireEvent.change(within(row).getByRole("textbox", { name: "Label" }), { target: { value: "After" } });
    fireEvent.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(state.requested.filter(({ method }) => method === "PATCH")).toHaveLength(1));
    expect(state.detail.settings.callable.input_schema).toEqual({
      ...schema, properties: { ...schema.properties, edited: { type: "string", title: "After" } },
    });
  } finally { view.dispose(); }
});

it.each(["root", "member"])("keeps a literal __proto__ %s key when its visual label changes", async (kind) => {
  const schema = kind === "root"
    ? JSON.parse('{"type":"object","properties":{"__proto__":{"type":"string","title":"Odd"}}}')
    : JSON.parse('{"type":"object","properties":{"customer":{"type":"object","properties":{"__proto__":{"type":"string","title":"Odd"}}}}}');
  const state = fixture({ callable: { input_mode: "json_schema", input_schema: schema, expected_inputs: [] } });
  const view = renderJourney(`${rootPath}?project_id=${projectId}&tab=smart-setup`);
  try {
    const row = await screen.findByRole("group", { name: kind === "root" ? "Structured input 1" : "Member 1" });
    fireEvent.change(within(row).getByRole("textbox", { name: "Label" }), { target: { value: "Odd revised" } });
    fireEvent.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(state.requested.filter(({ method }) => method === "PATCH")).toHaveLength(1));
    const saved = state.detail.settings.callable.input_schema as { properties: Record<string, unknown> };
    const properties = kind === "root" ? saved.properties : (saved.properties.customer as { properties: Record<string, unknown> }).properties;
    expect(Object.hasOwn(properties, "__proto__")).toBe(true);
    expect(properties["__proto__"]).toMatchObject({ type: "string", title: "Odd revised" });
  } finally { view.dispose(); }
});
