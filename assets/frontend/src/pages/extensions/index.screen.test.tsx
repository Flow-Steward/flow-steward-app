import type { ReactNode } from "react";

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router";

import { HttpError } from "@/lib/http/errors";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { ExtensionsHubPage } from "@/pages/extensions";
import {
  checkExtensionHealth,
  confirmExtensionUninstall,
  createExtensionCallbackRegistration,
  executeExtensionAction,
  executeExtensionQuery,
  fetchExtension,
  fetchExtensionExternalEffects,
  resolveExtensionExternalEffect,
  fetchExtensionArchiveLifecycleAvailability,
  fetchExtensionUninstallPlan,
  fetchExtensionCallbacks,
  fetchExtensionCompiledManifest,
  fetchExtensionPreview,
  fetchExtensions,
  fetchLlmProviderSources,
  fetchLlmProviderStatus,
  installExtensionFromFile,
  refreshLlmProviderModels,
  refreshExtensionCallbackStatus,
  saveLlmProviderCredentials,
  setExtensionEnabled,
  setLlmProviderModelEnabled,
} from "@/pages/extensions/api";
import {
  createProjectConnection,
  deleteExtensionProjectState,
  deleteProjectConnection,
  fetchExtensionProjectState,
  fetchProjectConnectionsList,
  putExtensionProjectState,
  saveProjectConnectionSecrets,
  startProjectConnectionOAuth,
  updateProjectConnection,
} from "@/pages/projects/connections/api";
import { fetchProjectsList, type ProjectsListResponse } from "@/pages/projects/list/api";
import type * as ProjectsListApi from "@/pages/projects/list/api";
import { RegistryWorkspacesPage } from "@/pages/registry/workspaces";
import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import { hasPendingRequests } from "@/lib/http/request-activity";
import { persistWorkspaceSelection, WORKSPACE_STORAGE_KEY } from "@/lib/utils/workspace-selection";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import {
  fetchExtensionOAuthConfigurations,
  fetchExtensionOAuthSetupStatus,
} from "@/pages/extensions/extension-oauth-api";
import catalogBrowseStyles from "@/components/shared/catalog-browse.css?raw";
import extensionStyles from "@/pages/extensions/extensions.css?raw";

// These page journeys take about 6 s when the quality gate shares the machine,
// over vitest's 5 s default. Per-test timeouts set below still take precedence.
vi.setConfig({ testTimeout: 10_000 });

vi.mock("@/pages/extensions/api", () => ({
  checkExtensionHealth: vi.fn(),
  confirmExtensionUninstall: vi.fn(),
  createExtensionCallbackRegistration: vi.fn(),
  executeExtensionAction: vi.fn(),
  executeExtensionQuery: vi.fn(),
  fetchExtension: vi.fn(),
  fetchExtensionExternalEffects: vi.fn(),
  resolveExtensionExternalEffect: vi.fn(),
  fetchExtensionArchiveLifecycleAvailability: vi.fn(),
  fetchExtensionUninstallPlan: vi.fn(),
  fetchExtensionCallbacks: vi.fn(),
  fetchExtensionCompiledManifest: vi.fn(),
  fetchExtensionPreview: vi.fn(),
  fetchExtensions: vi.fn(),
  fetchLlmProviderSources: vi.fn(),
  fetchLlmProviderStatus: vi.fn(),
  installExtensionFromFile: vi.fn(),
  normalizeExtensionRecord: vi.fn((value: unknown) => value),
  refreshExtensionCallbackStatus: vi.fn(),
  refreshLlmProviderModels: vi.fn(),
  saveLlmProviderCredentials: vi.fn(),
  setExtensionEnabled: vi.fn(),
  setLlmProviderModelEnabled: vi.fn(),
}));

vi.mock("@/lib/auth/use-is-admin", () => ({ useIsAdmin: vi.fn() }));

vi.mock("@/pages/extensions/extension-oauth-api", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    fetchExtensionOAuthConfigurations: vi.fn(),
    fetchExtensionOAuthSetupStatus: vi.fn(),
  };
});

vi.mock("@/pages/projects/list/api", () => ({
  fetchProjectsList: vi.fn(),
}));

vi.mock("@/pages/projects/connections/api", () => ({
  createProjectConnection: vi.fn(),
  deleteExtensionProjectState: vi.fn(),
  deleteProjectConnection: vi.fn(),
  fetchExtensionProjectState: vi.fn(),
  fetchProjectConnectionsList: vi.fn(),
  putExtensionProjectState: vi.fn(),
  saveProjectConnectionSecrets: vi.fn(),
  startProjectConnectionOAuth: vi.fn(),
  updateProjectConnection: vi.fn(),
}));

const mockedCheckExtensionHealth = vi.mocked(checkExtensionHealth);
const mockedConfirmExtensionUninstall = vi.mocked(confirmExtensionUninstall);
const mockedCreateExtensionCallbackRegistration = vi.mocked(createExtensionCallbackRegistration);
const mockedCreateProjectConnection = vi.mocked(createProjectConnection);
const mockedDeleteExtensionProjectState = vi.mocked(deleteExtensionProjectState);
const mockedDeleteProjectConnection = vi.mocked(deleteProjectConnection);
const mockedExecuteExtensionAction = vi.mocked(executeExtensionAction);
const mockedExecuteExtensionQuery = vi.mocked(executeExtensionQuery);
const mockedFetchExtensionProjectState = vi.mocked(fetchExtensionProjectState);
const mockedFetchExtensionOAuthConfigurations = vi.mocked(fetchExtensionOAuthConfigurations);
const mockedFetchExtensionOAuthSetupStatus = vi.mocked(fetchExtensionOAuthSetupStatus);
const mockedFetchExtension = vi.mocked(fetchExtension);
const mockedFetchExtensionExternalEffects = vi.mocked(fetchExtensionExternalEffects);
const mockedResolveExtensionExternalEffect = vi.mocked(resolveExtensionExternalEffect);
const mockedFetchExtensionArchiveLifecycleAvailability = vi.mocked(
  fetchExtensionArchiveLifecycleAvailability,
);
const mockedFetchExtensionUninstallPlan = vi.mocked(fetchExtensionUninstallPlan);
const mockedFetchExtensionCallbacks = vi.mocked(fetchExtensionCallbacks);
const mockedFetchExtensionCompiledManifest = vi.mocked(fetchExtensionCompiledManifest);
const mockedFetchExtensionPreview = vi.mocked(fetchExtensionPreview);
const mockedFetchExtensions = vi.mocked(fetchExtensions);
const mockedFetchLlmProviderStatus = vi.mocked(fetchLlmProviderStatus);
const mockedFetchLlmProviderSources = vi.mocked(fetchLlmProviderSources);
const mockedFetchProjectConnectionsList = vi.mocked(fetchProjectConnectionsList);
const mockedFetchProjectsList = vi.mocked(fetchProjectsList);
const mockedInstallExtensionFromFile = vi.mocked(installExtensionFromFile);
const mockedPutExtensionProjectState = vi.mocked(putExtensionProjectState);
const mockedRefreshExtensionCallbackStatus = vi.mocked(refreshExtensionCallbackStatus);
const mockedSaveProjectConnectionSecrets = vi.mocked(saveProjectConnectionSecrets);
const mockedStartProjectConnectionOAuth = vi.mocked(startProjectConnectionOAuth);
const mockedSetExtensionEnabled = vi.mocked(setExtensionEnabled);
const mockedRefreshLlmProviderModels = vi.mocked(refreshLlmProviderModels);
const mockedSaveLlmProviderCredentials = vi.mocked(saveLlmProviderCredentials);
const mockedSetLlmProviderModelEnabled = vi.mocked(setLlmProviderModelEnabled);
const mockedUpdateProjectConnection = vi.mocked(updateProjectConnection);
const mockedUseIsAdmin = vi.mocked(useIsAdmin);

function imapConnectionFormFixture() {
  return {
    component_id: "imap_mailbox_connection",
    type: "connection_form",
    title: "IMAP mailbox connection",
    data: {
      connection_kind: "extension",
      connection_type: "imap_mailbox",
      connection_type_field: "provider_preset",
      default_connection_type: "manual",
      config_fields: ["provider_preset", "imap_host", "imap_port", "tls_mode", "username"],
      secret_fields: ["password"],
      required_secret_fields: ["password"],
      tags: ["imap", "email", "mailbox"],
      test_action: "test_connection",
      connection_types: {
        manual: {
          connection_type: "imap_mailbox",
          defaults: { imap_host: "", imap_port: 993, tls_mode: "ssl" },
        },
        yahoo: {
          connection_type: "imap_mailbox",
          defaults: { imap_host: "imap.mail.yahoo.com", imap_port: 993, tls_mode: "ssl" },
        },
        fastmail: {
          connection_type: "imap_mailbox",
          defaults: { imap_host: "imap.fastmail.com", imap_port: 993, tls_mode: "ssl" },
        },
        zoho_personal: {
          connection_type: "imap_mailbox",
          defaults: { imap_host: "imap.zoho.com", imap_port: 993, tls_mode: "ssl" },
        },
        zoho_pro: {
          connection_type: "imap_mailbox",
          defaults: { imap_host: "imappro.zoho.com", imap_port: 993, tls_mode: "ssl" },
        },
      },
    },
    defaults: {
      display_name: "Acme Mailbox",
      provider_preset: "manual",
      imap_port: 993,
      tls_mode: "ssl",
    },
    fields: [
      { path: "display_name", label: "Display name", widget: "text" },
      {
        path: "provider_preset",
        label: "Provider preset",
        widget: "select",
        required: true,
        options: [
          { value: "manual", label: "Manual setup" },
          { value: "yahoo", label: "Yahoo Mail" },
          { value: "fastmail", label: "Fastmail" },
          { value: "zoho_personal", label: "Zoho Mail personal" },
          { value: "zoho_pro", label: "Zoho Mail business" },
        ],
      },
      { path: "imap_host", label: "IMAP host", widget: "text", required: true },
      { path: "imap_port", label: "IMAP port", widget: "integer", required: true },
      {
        path: "tls_mode",
        label: "TLS mode",
        widget: "select",
        required: true,
        options: [
          { value: "ssl", label: "SSL/TLS" },
          { value: "starttls", label: "STARTTLS" },
        ],
      },
      { path: "username", label: "Username", widget: "text", required: true },
      { path: "password", label: "Password", widget: "secret", required: true },
    ],
  };
}

function authValue(role: "admin" | "operator" | "viewer"): AuthContextValue {
  return {
    error: null,
    isAuthenticated: true,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    session: {
      authenticated: true,
      roles: [role],
      user: { roles: [role] },
    } as AuthContextValue["session"],
    status: "authenticated",
  };
}

function renderPage(
  initialEntry = "/extensions",
  role: "admin" | "operator" | "viewer" = "admin",
  configureQueryClient?: (queryClient: QueryClient) => void,
  recoverySwitches = false,
  extraContent?: ReactNode,
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  configureQueryClient?.(queryClient);
  return {
    ...render(
    <AuthContext.Provider value={authValue(role)}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[initialEntry]}>
          {recoverySwitches ? <RecoveryScopeSwitcher /> : null}
          <Routes>
            <Route
              path="/extensions"
              element={<><ExtensionsHubPage /><LocationProbe /></>}
            />
            <Route
              path="/extensions/:extensionId"
              element={<><ExtensionsHubPage /><LocationProbe /></>}
            />
          </Routes>
          {extraContent}
        </MemoryRouter>
      </QueryClientProvider>
    </AuthContext.Provider>,
    ),
    queryClient,
  };
}

function RecoveryScopeSwitcher() {
  const navigate = useNavigate();
  return <>
    <button type="button" onClick={() => void navigate("/extensions/smoke.guardrail.enforcement?project_id=project-2")}>Switch recovery project</button>
    <button type="button" onClick={() => void navigate("/extensions/extension.beta?project_id=project-1")}>Switch recovery install</button>
  </>;
}

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location-probe">
      {`${location.pathname}${location.search}${location.hash}`}
    </div>
  );
}

function ExtensionRouteSwitcher() {
  const navigate = useNavigate();
  return (
    <button onClick={() => void navigate("/extensions/extension.beta")} type="button">
      Switch to beta
    </button>
  );
}

function ExtensionPageParamRemover() {
  const navigate = useNavigate();
  return (
    <button onClick={() => void navigate("/extensions/extension.alpha")} type="button">
      Remove extension page
    </button>
  );
}

function renderCachedExtensionTransition(queryClient: QueryClient) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/extensions/extension.alpha"]}>
        <ExtensionRouteSwitcher />
        <Routes>
          <Route path="/extensions/:extensionId" element={<ExtensionsHubPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}


function deferredWorkspaceResponse<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

type TestUser = ReturnType<typeof userEvent.setup>;

/**
 * The status filter is the canon's Add filter chooser. "All" is the filter
 * removed, not a third value, so it clears the applied Status chip.
 */
const PROJECT_NAMES: Record<string, string> = { "project-1": "Project One", "project-2": "Project Two" };

/** Project scope is the design-system Select: open it, then pick the project by name. */
async function chooseProject(user: TestUser, projectId: string) {
  await user.click(await screen.findByRole("combobox", { name: "Project scope" }));
  await user.click(await screen.findByRole("option", { name: PROJECT_NAMES[projectId] ?? projectId }));
}

const PRESET_LABELS: Record<string, string> = {
  fastmail: "Fastmail",
  yahoo: "Yahoo Mail",
  zoho_personal: "Zoho Mail personal",
  zoho_pro: "Zoho Mail business",
};

/** Manifest selects are DsFormSelect: a combobox trigger and a listbox of options. */
async function chooseManifestOption(user: TestUser, fieldLabel: string, optionLabel: string) {
  await user.click(await screen.findByRole("combobox", { name: fieldLabel }));
  await user.click(await screen.findByRole("option", { name: optionLabel }));
}

/** What the trigger shows is the option the hidden input submits. */
function expectManifestSelection(fieldLabel: string, optionLabel: string) {
  expect(screen.getByRole("combobox", { name: fieldLabel })).toHaveTextContent(optionLabel);
}

async function chooseStatus(user: TestUser, label: "Active" | "Disabled" | "All") {
  if (label === "All") {
    await user.click(await screen.findByRole("button", { name: "Remove Status filter" }));
    return;
  }
  await user.click(await screen.findByRole("button", { name: "Add filter" }));
  await user.click(await screen.findByRole("menuitem", { name: "Status" }));
  await user.click(await screen.findByRole("menuitem", { name: label }));
}

describe("ExtensionsHubPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    globalThis.localStorage.removeItem("platform-last-project-id");
    mockedCheckExtensionHealth.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      status: "healthy",
      latency_ms: 42,
    });
    mockedConfirmExtensionUninstall.mockResolvedValue({});
    mockedCreateExtensionCallbackRegistration.mockResolvedValue({} as never);
    mockedExecuteExtensionAction.mockResolvedValue({});
    mockedFetchExtensionExternalEffects.mockResolvedValue({ effects: [], truncated: false });
    mockedResolveExtensionExternalEffect.mockReset();
    mockedExecuteExtensionQuery.mockResolvedValue({});
    mockedCreateProjectConnection.mockResolvedValue({ connection_id: "conn_created" });
    mockedDeleteExtensionProjectState.mockResolvedValue({});
    mockedDeleteProjectConnection.mockResolvedValue({});
    mockedFetchExtensionCallbacks.mockResolvedValue([]);
    mockedFetchExtensionCompiledManifest.mockResolvedValue({});
    mockedFetchExtensionPreview.mockResolvedValue({});
    mockedFetchExtensionProjectState.mockResolvedValue([]);
    mockedFetchExtensionOAuthConfigurations.mockResolvedValue([{
      configuration_id: "config-ready",
      display_name: "Ready application",
      status: "ready_for_authorization",
      fields: [],
      revision: 1,
      authorization_revision: 1,
    }]);
    mockedFetchExtensionOAuthSetupStatus.mockResolvedValue({
      callback_registration_ready: true,
    });
    mockedFetchProjectConnectionsList.mockResolvedValue([]);
    mockedFetchExtensions.mockResolvedValue([
      {
        extension_id: "smoke.guardrail.enforcement",
        display_name: "Smoke Guardrail Enforcement",
        version: "1.0.1",
        state: "active",
        active: true,
        vendor_name: "Flow Steward",
        category: "security-automation",
        description: "Enforces extension runtime guardrails.",
      },
    ]);
    mockedFetchLlmProviderSources.mockResolvedValue([]);
    mockedFetchLlmProviderStatus.mockResolvedValue({ install_id: "install-acme-llm", saved_secrets: [], last_check: null });
    mockedFetchExtension.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      display_name: "Smoke Guardrail Enforcement",
      version: "1.0.1",
      state: "active",
      active: true,
      vendor_name: "Flow Steward",
      category: "security-automation",
      description: "Enforces extension runtime guardrails.",
    });
    mockedFetchExtensionArchiveLifecycleAvailability.mockResolvedValue({
      available: true,
      reason_code: "available",
      message: "",
      remediation: { install_update: [], uninstall: [] },
    });
    mockedFetchExtensionUninstallPlan.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      install_id: "pi_smoke",
      release_id: "pr_smoke",
      revision: "revision-1",
      extension_name: "Smoke Guardrail Enforcement",
      extension_version: "1.0.0",
      remove: [
        { kind: "workflow", id: "pi_smoke", label: "Workflow", display_name: "Guardrail check", count: 2 },
      ],
      affected: [
        {
          kind: "workflow",
          id: "customer_retained_workflow",
          label: "Workflow",
          display_name: "Customer retained workflow",
          reason: "Missing required extension: smoke.guardrail.enforcement",
          explanation:
            "Needs Smoke Guardrail Enforcement; it stops working until that extension is installed again.",
        },
      ],
    });
    mockedFetchProjectsList.mockResolvedValue({
      projects: [],
      page: 1,
      per_page: 200,
      total: 0,
      total_pages: 0,
    });
    mockedInstallExtensionFromFile.mockResolvedValue({
      extension: {
        extension_id: "smoke.guardrail.enforcement",
        display_name: "Smoke Guardrail Enforcement",
        version: "1.0.1",
      },
    });
    mockedPutExtensionProjectState.mockResolvedValue({});
    mockedRefreshExtensionCallbackStatus.mockResolvedValue({});
    mockedRefreshLlmProviderModels.mockResolvedValue({} as never);
    mockedSaveProjectConnectionSecrets.mockResolvedValue({});
    mockedStartProjectConnectionOAuth.mockResolvedValue({
      authorize_url: "",
      connection_id: "conn_created",
    });
    mockedSaveLlmProviderCredentials.mockResolvedValue({} as never);
    mockedSetExtensionEnabled.mockResolvedValue({});
    mockedSetLlmProviderModelEnabled.mockResolvedValue({} as never);
    mockedUpdateProjectConnection.mockResolvedValue({});
    mockedUseIsAdmin.mockReturnValue(false);
  });

  const effectId = "60efa34c-2af9-4ea2-9c4d-e3b937c21fc7";
  const otherEffectId = "bb6f99a0-ce4f-4f17-ae98-3d3f862fc2f4";
  const unknownEffect = {
    effect_id: effectId, observed_version: 1, operation_id: "test_connection",
    status: "timeout_unknown", decision_allowed: true, retry_supported: false,
    retry_requires_original_request: true as const, resolution_ref: null,
    retry_unavailable_reason: null,
  };
  const receipt = {
    resolution_ref: { command_id: "resolution-1", effect_id: effectId, fenced_version: 2, approval_id: "" as const },
    action: "reject" as const, observed_version: 1, fenced_version: 2,
  };
  const pendingEffect = { ...unknownEffect, observed_version: 2, decision_allowed: false,
    retry_supported: true, resolution_ref: receipt.resolution_ref };

  function configureRecovery() {
    mockedFetchExtension.mockImplementation(async (extensionId) => ({
      extension_id: extensionId, install_id: extensionId === "extension.beta" ? "pi_beta" : "pi_smoke",
      display_name: "Effect fixture", state: "active", active: true,
    }));
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }, { id: "project-2", name: "Project Two" }],
      page: 1, per_page: 200, total: 2, total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" }, manifest_version: 2,
      action_definitions: [{ action_id: "test_connection", parameters: [
        { name: "connection_ref", value_type: "connection_ref", required: true },
        { name: "imap_host", type: "string", required: false },
      ] }],
      ui_manifest: { pages: [{ page_id: "configuration", title: "Configuration", components: [imapConnectionFormFixture()] }] },
    });
    mockedFetchProjectConnectionsList.mockResolvedValue([{
      connection_id: "conn_original", project_id: "project-1", extension_install_id: "pi_smoke",
      extension_id: "smoke.guardrail.enforcement", connection_type: "imap_mailbox",
      display_name: "Original connection", enabled: true,
      connection_config: { provider_preset: "manual", imap_host: "original.example.test", imap_port: 993, tls_mode: "ssl", username: "mailbox" },
    }]);
    mockedFetchExtensionExternalEffects.mockResolvedValue({ effects: [unknownEffect], truncated: false });
    mockedResolveExtensionExternalEffect.mockResolvedValue(receipt);
  }

  it("refreshes Project scope from an empty workspace using background transport without disturbing an unrelated draft", async () => {
    const user = userEvent.setup();
    localStorage.setItem(WORKSPACE_STORAGE_KEY, "ws-other");
    const actualApi = await vi.importActual<typeof ProjectsListApi>("@/pages/projects/list/api");
    mockedFetchProjectsList.mockImplementation(actualApi.fetchProjectsList);
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: { pages: [{ page_id: "configuration", title: "Configuration", project_scoped: true, components: [] }] },
    });
    const pending = deferredWorkspaceResponse<Response>();
    const requestedScopes: Array<string | null> = [];
    const json = (payload: unknown) => new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/workspaces") return json({ workspaces: [] });
      if (url.pathname !== "/api/v2/projects") throw new Error(`Unexpected request ${url.pathname}`);
      const workspace = url.searchParams.get("workspace_id");
      requestedScopes.push(workspace);
      if (workspace === "ws-default") return pending.promise;
      return json({ projects: [], page: 1, per_page: 200, total: 0, total_pages: 0 });
    }));
    const view = renderPage("/extensions/smoke.guardrail.enforcement", "admin", undefined, false,
      <><RegistryWorkspacesPage /><RequestLoadingIndicator /></>);
    try {
      await waitFor(() => expect(requestedScopes).toEqual(["ws-other"]));
      await user.click(await screen.findByRole("combobox", { name: "Project scope" }));
      expect(await screen.findByText("No options available")).toBeVisible();
      await user.keyboard("{Escape}");
      await user.click(screen.getByRole("button", { name: "New workspace" }));
      const draft = screen.getByRole("textbox", { name: "Workspace name" });
      await user.type(draft, "Unrelated workspace draft");
      await waitFor(() => expect(hasPendingRequests()).toBe(false));
      await act(async () => { persistWorkspaceSelection("ws-default"); });
      await waitFor(() => expect(requestedScopes).toEqual(["ws-other", "ws-default"]));
      expect(hasPendingRequests()).toBe(false);
      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      expect(screen.getByRole("textbox", { name: "Workspace name" })).toBe(draft);
      expect(draft).toHaveValue("Unrelated workspace draft");
      expect(draft).toHaveFocus();
      await user.keyboard(" continues");
      await act(async () => { pending.resolve(json({
        projects: [{ id: "project-default", name: "Project in Default" }], page: 1, per_page: 200, total: 1, total_pages: 1,
      })); });
      expect(draft).toHaveValue("Unrelated workspace draft continues");
      expect(draft).toHaveFocus();
      await user.click(screen.getByRole("button", { name: "Cancel" }));
      await user.click(screen.getByRole("combobox", { name: "Project scope" }));
      expect(await screen.findByRole("option", { name: "Project in Default" })).toBeVisible();
      expect(screen.queryByText("No options available")).not.toBeInTheDocument();
    } finally {
      view.unmount();
      pending.resolve(json({ projects: [], page: 1, per_page: 200, total: 0, total_pages: 0 }));
      view.queryClient.clear();
      vi.unstubAllGlobals();
      localStorage.removeItem(WORKSPACE_STORAGE_KEY);
    }
  });

  it.each(["success", "failure"] as const)("ignores a late old-workspace %s and clears its selected and remembered project", async (oldOutcome) => {
    const user = userEvent.setup();
    configureRecovery();
    localStorage.setItem(WORKSPACE_STORAGE_KEY, "ws-other");
    localStorage.setItem("platform-last-project-id", "project-1");
    const older = deferredWorkspaceResponse<ProjectsListResponse>();
    const newer = deferredWorkspaceResponse<ProjectsListResponse>();
    const requestedScopes: string[] = [];
    mockedFetchProjectsList.mockImplementation((params) => {
      const workspace = params.workspaceId ?? localStorage.getItem(WORKSPACE_STORAGE_KEY) ?? "";
      requestedScopes.push(workspace);
      return workspace === "ws-default" ? newer.promise : older.promise;
    });
    const view = renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=configuration&keep=draft#overview");
    try {
      await screen.findByLabelText("IMAP host");
      await waitFor(() => expect(requestedScopes).toEqual(["ws-other"]));
      await act(async () => { persistWorkspaceSelection("ws-default"); });
      await waitFor(() => expect(requestedScopes).toEqual(["ws-other", "ws-default"]));
      await waitFor(() => expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/extensions/smoke.guardrail.enforcement?extension_page=configuration&keep=draft#overview",
      ));
      expect(screen.queryByLabelText("IMAP host")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Use last project" })).not.toBeInTheDocument();
      await act(async () => { newer.resolve({
        projects: [{ id: "project-default", name: "Project in Default" }], page: 1, per_page: 200, total: 1, total_pages: 1,
      }); });
      await user.click(screen.getByRole("combobox", { name: "Project scope" }));
      expect(await screen.findByRole("option", { name: "Project in Default" })).toBeVisible();
      await act(async () => {
        if (oldOutcome === "failure") older.reject(new Error("Old workspace unavailable"));
        else older.resolve({ projects: [{ id: "project-1", name: "Project from Other" }], page: 1, per_page: 200, total: 1, total_pages: 1 });
      });
      expect(screen.getByRole("option", { name: "Project in Default" })).toBeVisible();
      expect(screen.queryByRole("option", { name: /Project from Other|Project project-1/ })).not.toBeInTheDocument();
      expect(screen.queryByText("Old workspace unavailable")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Use last project" })).not.toBeInTheDocument();
      expect(screen.getByRole("combobox", { name: "Project scope" })).toHaveTextContent("Select a project");
    } finally {
      view.unmount();
      older.resolve({ projects: [], page: 1, per_page: 200, total: 0, total_pages: 0 });
      newer.resolve({ projects: [], page: 1, per_page: 200, total: 0, total_pages: 0 });
      view.queryClient.clear();
      localStorage.removeItem(WORKSPACE_STORAGE_KEY);
    }
  });

  it("preserves the selected project and actual connection draft on a same-workspace event", async () => {
    configureRecovery();
    localStorage.setItem(WORKSPACE_STORAGE_KEY, "ws-other");
    const user = userEvent.setup();
    const view = renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=configuration#overview");
    try {
      const draft = await screen.findByLabelText("IMAP host");
      await user.clear(draft);
      await user.type(draft, "unsaved.mail.example.test");
      const requests = mockedFetchProjectsList.mock.calls.length;
      await act(async () => { persistWorkspaceSelection("ws-other"); });
      expect(screen.getByLabelText("IMAP host")).toBe(draft);
      expect(draft).toHaveValue("unsaved.mail.example.test");
      expect(draft).toHaveFocus();
      expect(mockedFetchProjectsList).toHaveBeenCalledTimes(requests);
      expect(screen.getByTestId("location-probe")).toHaveTextContent("?project_id=project-1&extension_page=configuration#overview");
      expect(screen.getByRole("combobox", { name: "Project scope" })).toHaveTextContent("Project One");
    } finally {
      view.unmount();
      view.queryClient.clear();
      localStorage.removeItem(WORKSPACE_STORAGE_KEY);
    }
  });

  it("restores the existing result card from scoped host projection after reload without sending an action", async () => {
    configureRecovery();
    renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1");
    expect(await screen.findByRole("button", { name: "Operation completed — continue" })).toBeVisible();
    expect(mockedFetchExtensionExternalEffects).toHaveBeenCalledWith("project-1", "pi_smoke");
    const card = screen.getByRole("region", { name: "Test Connection result" });
    expect(card).toBeVisible();
    expect(within(card).getByText("unknown", { exact: true })).toBeVisible();
    expect(within(card).queryByText("failed", { exact: true })).not.toBeInTheDocument();
    expect(within(card).getByText("The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.")).toBeVisible();
    expect(screen.getByText(new RegExp(effectId))).not.toBeVisible();
    expect(screen.getByText("The original request for this effect is unavailable. Retry is unavailable.")).toBeVisible();
    expect(mockedExecuteExtensionAction).not.toHaveBeenCalled();
    expect(mockedResolveExtensionExternalEffect).not.toHaveBeenCalled();
  });

  it("keeps a restored viewer card read-only even with contradictory server flags", async () => {
    configureRecovery();
    renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1", "viewer");
    expect(await screen.findByText(new RegExp(effectId))).toBeInTheDocument();
    expect(screen.getByText("You can view this effect. An operator must record the decision.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Operation completed — continue" })).not.toBeInTheDocument();
    expect(mockedExecuteExtensionAction).not.toHaveBeenCalled();
  });

  it("records a trimmed no-effect decision after reload but cannot invent its missing original request", async () => {
    configureRecovery();
    let decided = false;
    mockedFetchExtensionExternalEffects.mockImplementation(async () => ({ effects: [decided ? pendingEffect : unknownEffect], truncated: false }));
    mockedResolveExtensionExternalEffect.mockImplementation(async () => { decided = true; return receipt; });
    const user = userEvent.setup();
    renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1", "operator");
    await user.type(await screen.findByRole("textbox", { name: "Comment" }), "  Verified no provider record  ");
    await user.click(screen.getByRole("button", { name: "Operation not completed — retry" }));
    await waitFor(() => expect(mockedResolveExtensionExternalEffect).toHaveBeenCalledExactlyOnceWith(
      "project-1", "pi_smoke", effectId,
      { observed_version: 1, action: "reject", comment: "Verified no provider record" },
    ));
    expect(await screen.findByRole("button", { name: "Retry original action" })).toBeDisabled();
    expect(mockedExecuteExtensionAction).not.toHaveBeenCalled();
  });

  it.each([409, 403])("never retries a rejected decision HTTP %s and refreshes the scoped state on stale version", async (status) => {
    configureRecovery();
    let attempted = false;
    const active = { ...pendingEffect, status: "running", observed_version: 3, retry_supported: false, retry_unavailable_reason: "retry_in_progress" as const };
    mockedFetchExtensionExternalEffects.mockImplementation(async () => ({ effects: [attempted && status === 409 ? active : unknownEffect], truncated: false }));
    mockedResolveExtensionExternalEffect.mockImplementation(async () => {
      attempted = true;
      throw new HttpError({ status, url: "/resolution", payload: { error: status === 409 ? "external_effect_resolution_stale" : "Access denied" } });
    });
    const user = userEvent.setup();
    renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1", "operator");
    await user.type(await screen.findByRole("textbox", { name: "Comment" }), "Checked");
    await user.click(screen.getByRole("button", { name: "Operation not completed — retry" }));
    if (status === 409) {
      expect(await screen.findByText("The authorized retry is already in progress.")).toBeVisible();
      expect(mockedFetchExtensionExternalEffects).toHaveBeenCalledTimes(2);
      expect(screen.getByRole("button", { name: "Retry original action" })).toBeDisabled();
    } else {
      expect(await screen.findByText("You do not have permission to resolve this effect.")).toBeVisible();
    }
    expect(mockedExecuteExtensionAction).not.toHaveBeenCalled();
  });

  it.each(["project", "install"])("ignores a late recovery GET after switching the selected %s", async (selection) => {
    configureRecovery();
    let resolveOld!: (value: { effects: typeof unknownEffect[]; truncated: boolean }) => void;
    const oldResponse = new Promise<{ effects: typeof unknownEffect[]; truncated: boolean }>((resolve) => { resolveOld = resolve; });
    mockedFetchExtensionExternalEffects.mockImplementation((projectId, installId) => (
      projectId === "project-1" && installId === "pi_smoke" ? oldResponse : Promise.resolve({ effects: [{ ...unknownEffect, effect_id: otherEffectId }], truncated: false })
    ));
    const user = userEvent.setup();
    const { queryClient } = renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1", "operator", undefined, true);
    await waitFor(() => expect(mockedFetchExtensionExternalEffects).toHaveBeenCalledWith("project-1", "pi_smoke"));
    await user.click(screen.getByRole("button", { name: `Switch recovery ${selection}` }));
    expect(await screen.findByText(new RegExp(otherEffectId))).toBeInTheDocument();
    resolveOld({ effects: [unknownEffect], truncated: false });
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(screen.queryByText(new RegExp(effectId))).not.toBeInTheDocument();
    expect(mockedResolveExtensionExternalEffect).not.toHaveBeenCalled();
    expect(mockedExecuteExtensionAction).not.toHaveBeenCalled();
  });

  it.each(["project", "install"])("does not retry a late no-effect decision after switching the selected %s", async (selection) => {
    configureRecovery();
    let effects: Array<typeof unknownEffect | typeof pendingEffect> = [];
    let completeDecision!: (value: typeof receipt) => void;
    mockedFetchExtensionExternalEffects.mockImplementation(async (projectId, installId) => ({
      effects: projectId === "project-1" && installId === "pi_smoke" ? effects : [{ ...unknownEffect, effect_id: otherEffectId }], truncated: false,
    }));
    mockedResolveExtensionExternalEffect.mockImplementation(() => new Promise((resolve) => { completeDecision = resolve; }));
    mockedExecuteExtensionAction.mockImplementationOnce(async () => {
      effects = [unknownEffect];
      throw new HttpError({ status: 502, url: "/action/test_connection", payload: {
        action: { status: "failed", detail: "provider_timeout", response: {
          ok: false, error_code: "provider_timeout",
          external_effect: { effect_id: effectId, status: "timeout_unknown", claim_version: 1, operation_id: "test_connection" },
        }, errors: [] },
      } });
    });
    const user = userEvent.setup();
    const { queryClient } = renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1", "operator", undefined, true);
    await screen.findByText("Original connection");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Test" }));
    await user.type(await screen.findByRole("textbox", { name: "Comment" }), "Checked the original effect");
    await user.click(screen.getByRole("button", { name: "Operation not completed — retry" }));
    await waitFor(() => expect(mockedResolveExtensionExternalEffect).toHaveBeenCalledOnce());
    await user.click(screen.getByRole("button", { name: `Switch recovery ${selection}` }));
    expect(await screen.findByText(new RegExp(otherEffectId))).toBeInTheDocument();
    completeDecision(receipt);
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(screen.queryByText(new RegExp(effectId))).not.toBeInTheDocument();
    expect(mockedExecuteExtensionAction).toHaveBeenCalledOnce();
  });

  it.each(["exact", "missing", "different", "business-result"])("associates the retained request with the %s effect ID before exactly one retry", async (association) => {
    configureRecovery();
    let effects: Array<typeof unknownEffect | typeof pendingEffect> = [];
    mockedFetchExtensionExternalEffects.mockImplementation(async () => ({ effects, truncated: false }));
    mockedResolveExtensionExternalEffect.mockImplementation(async () => { effects = [pendingEffect]; return receipt; });
    mockedExecuteExtensionAction.mockImplementationOnce(async () => {
      effects = [unknownEffect];
      throw new HttpError({ status: 502, url: "/action/test_connection", payload: {
        action: { status: "failed", detail: "provider_timeout", response: {
          ok: false, error_code: "provider_timeout",
          ...(association === "business-result"
            ? { result: { external_effect: { effect_id: effectId, status: "timeout_unknown", claim_version: 1, operation_id: "test_connection" } } }
            : association === "missing" ? {} : { external_effect: { effect_id: association === "exact" ? effectId : otherEffectId, status: "timeout_unknown", claim_version: 1, operation_id: "test_connection" } }),
        } },
      } });
    }).mockImplementation(async () => { effects = []; return { action: { status: "succeeded", response: { ok: true } } }; });
    const user = userEvent.setup();
    renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1", "operator");
    await screen.findByText("Original connection");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Test" }));
    const originalBody = JSON.parse(JSON.stringify(mockedExecuteExtensionAction.mock.calls[0]?.[2])) as Record<string, unknown>;
    const originalKey = mockedExecuteExtensionAction.mock.calls[0]?.[3];
    expect(originalBody).toEqual(expect.objectContaining({ target: { connection_id: "conn_original", project_id: "" } }));
    await user.clear(screen.getByLabelText("IMAP host"));
    await user.type(screen.getByLabelText("IMAP host"), "changed-after-ambiguity.example.test");
    await user.type(await screen.findByRole("textbox", { name: "Comment" }), "  Verified provider  ");
    await user.click(screen.getByRole("button", { name: "Operation not completed — retry" }));
    await waitFor(() => expect(mockedResolveExtensionExternalEffect).toHaveBeenCalledOnce());
    if (association === "exact") {
      await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2));
      expect(mockedExecuteExtensionAction.mock.calls[1]).toEqual([
        "smoke.guardrail.enforcement", "test_connection",
        { ...originalBody, resolution_ref: receipt.resolution_ref }, originalKey,
      ]);
      expect(mockedExecuteExtensionAction.mock.calls[0]?.[2]).not.toHaveProperty("resolution_ref");
    } else {
      expect(await screen.findByRole("button", { name: "Retry original action" })).toBeDisabled();
      expect(screen.getByText("The original request for this effect is unavailable. Retry is unavailable.")).toBeVisible();
      expect(mockedExecuteExtensionAction).toHaveBeenCalledOnce();
    }
  });

  it.each([false, true])("synchronizes successful exact recovery feedback while preserving older cards and newer field errors (%s)", async (newerFieldError) => {
    configureRecovery();
    const ambiguity = "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.";
    const olderEffect = { ...unknownEffect, effect_id: otherEffectId };
    let effects: Array<typeof unknownEffect | typeof pendingEffect> = [olderEffect];
    let finishRetry!: (result: Record<string, unknown>) => void;
    const retryResult = new Promise<Record<string, unknown>>((resolve) => { finishRetry = resolve; });
    mockedFetchExtensionExternalEffects.mockImplementation(async () => ({ effects, truncated: false }));
    mockedResolveExtensionExternalEffect.mockImplementation(async () => { effects = [pendingEffect, olderEffect]; return receipt; });
    mockedExecuteExtensionAction.mockImplementationOnce(async () => {
      effects = [unknownEffect, olderEffect];
      throw new HttpError({ status: 502, url: "/action/test_connection", payload: {
        action: { status: "failed", response: { ok: false, error_code: "provider_timeout",
          external_effect: { effect_id: effectId, status: "timeout_unknown", claim_version: 1, operation_id: "test_connection" },
        } },
      } });
    }).mockImplementation(() => retryResult);
    const user = userEvent.setup();
    renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1", "operator");
    await screen.findByText("Original connection");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Test" }));
    await waitFor(() => expect(screen.getAllByRole("alert").some((alert) => within(alert).queryByText(ambiguity))).toBe(true));
    const failedCard = await screen.findByRole("region", { name: "Test result" });
    await waitFor(() => expect(screen.getAllByRole("alert").filter((alert) => within(alert).queryByText(ambiguity))).toHaveLength(3));
    await user.type(within(failedCard).getByRole("textbox", { name: "Comment" }), "Provider confirmed no effect");
    await user.click(within(failedCard).getByRole("button", { name: "Operation not completed — retry" }));
    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2));
    const originalBody = mockedExecuteExtensionAction.mock.calls[0]?.[2];
    const originalKey = mockedExecuteExtensionAction.mock.calls[0]?.[3];
    expect(mockedExecuteExtensionAction.mock.calls[1]).toEqual([
      "smoke.guardrail.enforcement", "test_connection", { ...originalBody, resolution_ref: receipt.resolution_ref }, originalKey,
    ]);
    if (newerFieldError) {
      await user.clear(screen.getByLabelText("IMAP host"));
      await user.click(screen.getByRole("button", { name: "Save connection" }));
      expect(await screen.findByText("IMAP host is required.")).toBeVisible();
    }
    effects = [olderEffect];
    finishRetry({ action: { status: "succeeded", response: { ok: true } } });
    await waitFor(() => expect(within(screen.getByRole("region", { name: "Test result" })).getByText("succeeded", { exact: true })).toBeVisible());
    await waitFor(() => expect(screen.queryAllByRole("alert").filter((alert) => within(alert).queryByText(ambiguity))).toHaveLength(1));
    const olderCard = screen.getByRole("region", { name: "Test Connection result" });
    expect(within(within(olderCard).getByRole("alert")).getByText(ambiguity)).toBeVisible();
    if (newerFieldError) {
      expect(screen.getByLabelText("IMAP host")).toHaveAttribute("aria-invalid", "true");
      expect(screen.getByText("IMAP host is required.")).toBeVisible();
    }
    expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2);
    expect(mockedUpdateProjectConnection).not.toHaveBeenCalled();
  });

  it("shows a generic OAuth success notice and removes callback parameters", async () => {
    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&oauth=success#overview",
    );

    expect(await screen.findByText("External account connected successfully.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/extensions/smoke.guardrail.enforcement?project_id=project-1#overview",
      );
    });
    await userEvent.setup().click(screen.getByRole("button", { name: "Dismiss authorization notice" }));
    expect(screen.queryByText("External account connected successfully.")).not.toBeInTheDocument();
  });

  it("loads exact extension detail without fetching the admin catalog", async () => {
    renderPage("/extensions/smoke.guardrail.enforcement", "viewer");

    expect(await screen.findByText("Smoke Guardrail Enforcement")).toBeInTheDocument();
    expect(mockedFetchExtension).toHaveBeenCalledWith("smoke.guardrail.enforcement");
    expect(mockedFetchExtensions).not.toHaveBeenCalled();
    expect(mockedFetchExtensionPreview).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Back to dashboard" })).toHaveAttribute(
      "href",
      "/dashboard",
    );
    expect(screen.queryByRole("link", { name: "Back to extensions" })).not.toBeInTheDocument();
  });

  it.each(["operator", "viewer"] as const)(
    "limits privileged detail resources by capability for a %s",
    async (role) => {
      mockedFetchExtensionCompiledManifest.mockResolvedValue({
        account_scope: { scope_type: "project" },
        ui_manifest: {
          pages: [{
            page_id: "connections",
            title: "Connections",
            project_scoped: true,
            components: [{
              component_id: "callback_setup",
              type: "callback_setup",
              title: "Incoming updates",
              callback_id: "updates",
            }],
          }],
        },
      });

      renderPage(
        "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=connections",
        role,
      );

      expect(await screen.findByText("Smoke Guardrail Enforcement")).toBeInTheDocument();
      expect(mockedFetchExtensionPreview).not.toHaveBeenCalled();
      if (role === "operator") {
        expect(mockedFetchExtensionCallbacks).toHaveBeenCalledWith(
          "smoke.guardrail.enforcement",
          "project-1",
        );
        expect(screen.getByRole("button", { name: "Create and Configure" })).toBeVisible();
        expect(screen.getByRole("button", { name: "Refresh Status" })).toBeVisible();
      } else {
        expect(mockedFetchExtensionCallbacks).not.toHaveBeenCalled();
        expect(screen.getByText("Callback setup is managed by project managers.")).toBeVisible();
        expect(screen.queryByText("not registered")).not.toBeInTheDocument();
        expect(screen.queryByRole("button", { name: "Create and Configure" })).not.toBeInTheDocument();
        expect(screen.queryByRole("button", { name: "Refresh Status" })).not.toBeInTheDocument();
      }
      expect(screen.getByRole("link", { name: "Back to project" })).toHaveAttribute(
        "href",
        "/projects/project-1",
      );
    },
  );

  it("names a callback connection only after exact scoped resolution and refetch", async () => {
    mockedFetchExtension.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      install_id: "pi_smoke",
      display_name: "Smoke Guardrail Enforcement",
      version: "1.0.1",
      state: "active",
      active: true,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [{
          page_id: "connections",
          title: "Connections",
          project_scoped: true,
          components: [],
        }],
      },
    });
    mockedFetchProjectConnectionsList.mockResolvedValue([{
      connection_id: "connection:a-b",
      extension_install_id: "pi_smoke",
      display_name: "Primary mailbox",
    }]);

    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=connections&oauth=success&oauth_connection_id=connection%3Aa-b#overview",
    );

    expect(await screen.findByText("Primary mailbox connected successfully.")).toBeInTheDocument();
    await waitFor(() => expect(mockedFetchProjectConnectionsList).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("location-probe")).toHaveTextContent(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=connections#overview",
    );
  });

  it("gates OAuth connections until the route inventory retry succeeds", async () => {
    let rejectConnections: ((reason?: unknown) => void) | undefined;
    mockedFetchExtension.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      install_id: "pi_smoke",
      display_name: "Smoke Guardrail Enforcement",
      version: "1.0.1",
      state: "active",
      active: true,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      oauth_providers: [{
        provider_id: "example",
        display_name: "Example Identity",
        setup_page_id: "oauth_setup",
        credential_fields: [{
          field_id: "client_id",
          value_type: "text",
          required: true,
          secret: false,
          label: "Application ID",
          help_text: "Public application identifier.",
        }],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
      connection_types: [{
        connection_type_id: "example_mail",
        auth: {
          type: "oauth2",
          provider_id: "example",
          scopes: { required: ["mail.read"] },
        },
      }],
      ui_manifest: {
        pages: [{
          page_id: "connections",
          title: "Connections",
          project_scoped: true,
          components: [{
            component_id: "mail_connection",
            type: "connection_form",
            data: {
              connection_type: "example_mail",
              oauth_intent: "connect",
              oauth_provider_id: "example",
            },
          }],
        }],
      },
    });
    mockedFetchExtensionOAuthConfigurations.mockResolvedValue([{
      configuration_id: "config-a",
      display_name: "Primary application",
      status: "ready_for_authorization",
      fields: [],
      revision: 1,
      authorization_revision: 1,
    }]);
    mockedFetchProjectConnectionsList
      .mockImplementationOnce(() => new Promise((_resolve, reject) => {
        rejectConnections = reject;
      }))
      .mockResolvedValueOnce([{
        connection_id: "connection-a",
        extension_install_id: "pi_smoke",
        display_name: "Primary mailbox",
        oauth_provider_id: "example",
        oauth_configuration_id: "config-a",
        connection_type: "example_mail",
        connection_config: {
          oauth: { lifecycle_status: "connected", authorization_intent_id: "connect" },
        },
      }]);

    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=connections",
      "operator",
    );

    await waitFor(() => expect(mockedFetchExtensionOAuthConfigurations).toHaveBeenCalledWith({
      projectId: "project-1",
      installId: "pi_smoke",
      providerId: "example",
    }));
    expect(await screen.findByRole("status")).toHaveTextContent("Loading connections");
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
    rejectConnections?.(new Error("private transport detail"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Connections could not be loaded. Try again.",
    );
    expect(screen.queryByText(/private transport detail/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: "Retry loading connections" }));
    expect(await screen.findByLabelText("Connection Primary mailbox")).toBeVisible();
  });

  it("keeps fresh OAuth service pages locked when credentials are ready but callback setup is not", async () => {
    mockedFetchExtension.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      install_id: "pi_smoke",
      display_name: "Smoke Guardrail Enforcement",
      state: "active",
      active: true,
    });
    mockedFetchExtensionOAuthConfigurations.mockResolvedValue([{
      configuration_id: "config-a",
      display_name: "Primary application",
      status: "ready_for_authorization",
      fields: [],
      revision: 1,
      authorization_revision: 1,
    }]);
    mockedFetchExtensionOAuthSetupStatus.mockResolvedValue({
      callback_registration_ready: false,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      oauth_providers: [{
        provider_id: "example",
        display_name: "Example Identity",
        setup_page_id: "oauth_setup",
        credential_fields: [{
          field_id: "client_id",
          value_type: "text",
          required: true,
          secret: false,
          label: "Application ID",
          help_text: "Public application identifier.",
        }],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
      connection_types: [{
        connection_type_id: "example_mail",
        auth: { type: "oauth2", provider_id: "example", scopes: { required: ["mail.read"] } },
      }],
      ui_manifest: { pages: [{
        page_id: "oauth_setup",
        title: "Setup",
        project_scoped: true,
        components: [],
      }, {
        page_id: "connections",
        title: "Connections",
        project_scoped: true,
        components: [{
          component_id: "mail_connection",
          type: "connection_form",
          data: {
            connection_type: "example_mail",
            oauth_intent: "connect",
            oauth_provider_id: "example",
          },
        }],
      }] },
    });

    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=connections",
      "operator",
    );

    expect(await screen.findByRole("tab", { name: "Connections" })).toBeDisabled();
    expect(screen.getByText("Finish provider setup")).toBeVisible();
    expect(screen.queryByRole("form", { name: "Connect an account" })).not.toBeInTheDocument();
    await waitFor(() => expect(mockedFetchExtensionOAuthSetupStatus).toHaveBeenCalledWith({
      projectId: "project-1",
      installId: "pi_smoke",
      providerId: "example",
    }));
  });

  it("names and focuses an exact preserved reconnect without contradicting its lifecycle", async () => {
    mockedFetchExtension.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      install_id: "pi_smoke",
      display_name: "Smoke Guardrail Enforcement",
      version: "1.0.1",
      state: "active",
      active: true,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      oauth_providers: [{
        provider_id: "example",
        display_name: "Example Identity",
        setup_page_id: "oauth_setup",
        credential_fields: [{
          field_id: "client_id",
          value_type: "text",
          required: true,
          secret: false,
          label: "Application ID",
          help_text: "Public application identifier.",
        }],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
      connection_types: [{
        connection_type_id: "example_mail",
        auth: {
          type: "oauth2",
          provider_id: "example",
          scopes: { required: ["mail.read"] },
        },
      }],
      ui_manifest: {
        pages: [{
          page_id: "connections",
          title: "Connections",
          project_scoped: true,
          components: [{
            component_id: "mail_connection",
            type: "connection_form",
            data: {
              connection_type: "example_mail",
              oauth_intent: "connect",
              oauth_provider_id: "example",
            },
          }],
        }],
      },
    });
    mockedFetchProjectConnectionsList.mockResolvedValue([
      {
        connection_id: "connection-other",
        extension_install_id: "pi_smoke",
        display_name: "Other mailbox",
        oauth_provider_id: "example",
        oauth_configuration_id: "config-a",
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: "connected" } },
      },
      {
        connection_id: "connection:a-b",
        extension_install_id: "pi_smoke",
        display_name: "Primary mailbox",
        oauth_provider_id: "example",
        oauth_configuration_id: "config-a",
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: "connected" } },
      },
    ]);

    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1"
      + "&extension_page=connections&oauth=error&reason=authorization_required"
      + "&oauth_connection_id=connection%3Aa-b&oauth_connection_preserved=1",
    );

    expect(await screen.findByText(
      "Primary mailbox reauthorization did not complete. The existing connection and its previous authorization were not replaced and remain in their prior state.",
    )).toBeInTheDocument();
    expect(screen.queryByText(/Reconnect/u)).not.toBeInTheDocument();
    await waitFor(() => expect(mockedFetchProjectConnectionsList).toHaveBeenCalledTimes(2));
    const focused = screen.getByLabelText("Connection Primary mailbox");
    await waitFor(() => expect(focused).toHaveFocus());
    expect(focused).toHaveClass("is-focused");
    expect(screen.getByLabelText("Connection Other mailbox")).not.toHaveClass("is-focused");
    expect(screen.getByTestId("location-probe")).toHaveTextContent(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=connections",
    );
  });

  it("does not claim an initial-connect failure preserved a connection", async () => {
    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1"
      + "&oauth=error&reason=authorization_required&oauth_connection_preserved=1",
    );

    expect(await screen.findByText(
      "Authorization must be completed again. Reconnect the account and approve the declared access.",
    )).toBeInTheDocument();
    expect(screen.queryByText(/existing connection and its previous authorization/u)).not.toBeInTheDocument();
  });

  it.each([
    {
      label: "missing connection",
      rows: [{
        connection_id: "connection-other",
        extension_install_id: "pi_smoke",
        display_name: "Other mailbox",
        oauth_provider_id: "example",
        connection_type: "example_mail",
      }],
    },
    {
      label: "connection from another install",
      rows: [
        {
          connection_id: "connection-other",
          extension_install_id: "pi_smoke",
          display_name: "Other mailbox",
          oauth_provider_id: "example",
          connection_type: "example_mail",
        },
        {
          connection_id: "connection:a-b",
          extension_install_id: "pi_foreign",
          display_name: "Foreign mailbox",
          oauth_provider_id: "example",
          connection_type: "example_mail",
        },
      ],
    },
  ])("does not trust a preserved reconnect marker for a $label", async ({ rows }) => {
    mockedFetchExtension.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      install_id: "pi_smoke",
      display_name: "Smoke Guardrail Enforcement",
      version: "1.0.1",
      state: "active",
      active: true,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      oauth_providers: [{
        provider_id: "example",
        display_name: "Example Identity",
        setup_page_id: "oauth_setup",
        credential_fields: [{
          field_id: "client_id",
          value_type: "text",
          required: true,
          secret: false,
          label: "Application ID",
          help_text: "Public application identifier.",
        }],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
      connection_types: [{
        connection_type_id: "example_mail",
        auth: {
          type: "oauth2",
          provider_id: "example",
          scopes: { required: ["mail.read"] },
        },
      }],
      ui_manifest: {
        pages: [{
          page_id: "connections",
          title: "Connections",
          project_scoped: true,
          components: [{
            component_id: "mail_connection",
            type: "connection_form",
            data: {
              connection_type: "example_mail",
              oauth_intent: "connect",
              oauth_provider_id: "example",
            },
          }],
        }],
      },
    });
    mockedFetchProjectConnectionsList.mockResolvedValue(rows);

    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1"
      + "&extension_page=connections&oauth=error&reason=authorization_required"
      + "&oauth_connection_id=connection%3Aa-b&oauth_connection_preserved=1",
    );

    expect(await screen.findByText(
      "Authorization must be completed again. Reconnect the account and approve the declared access.",
    )).toBeInTheDocument();
    expect(screen.queryByText(/existing connection and its previous authorization/u)).not.toBeInTheDocument();
    await waitFor(() => expect(mockedFetchProjectConnectionsList).toHaveBeenCalledTimes(2));
    const otherConnection = await screen.findByLabelText("Connection Other mailbox");
    expect(otherConnection).not.toHaveFocus();
    expect(otherConnection).not.toHaveClass("is-focused");
  });

  it("keeps the selected extension page and anchor after a generic OAuth cancellation", async () => {
    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=configuration&oauth=error&reason=authorization_cancelled#connections",
    );

    expect(await screen.findByText("Authorization was cancelled. You can try connecting the account again.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=configuration#connections",
      );
    });
  });

  it("clears a callback notice when the user changes extension page", async () => {
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          { page_id: "connections", title: "Connections", project_scoped: true, components: [] },
          { page_id: "other", title: "Other", project_scoped: true, components: [] },
        ],
      },
    });
    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=connections&oauth=error&reason=authorization_cancelled",
    );
    expect(await screen.findByText("Authorization was cancelled. You can try connecting the account again.")).toBeInTheDocument();

    await userEvent.setup().click(await screen.findByRole("tab", { name: "Other" }));

    await waitFor(() => expect(screen.queryByText("Authorization was cancelled. You can try connecting the account again.")).not.toBeInTheDocument());
  });

  it("clears a callback notice when the user changes project", async () => {
    mockedFetchProjectsList.mockResolvedValue({
      projects: [
        { id: "project-1", name: "Project One" },
        { id: "project-2", name: "Project Two" },
      ],
      page: 1,
      per_page: 200,
      total: 2,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [{
          page_id: "connections",
          title: "Connections",
          project_scoped: true,
          components: [],
        }],
      },
    });
    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=connections&oauth=error&reason=authorization_cancelled",
    );
    expect(await screen.findByText("Authorization was cancelled. You can try connecting the account again.")).toBeInTheDocument();

    await chooseProject(userEvent.setup(), "project-2");

    await waitFor(() => expect(screen.queryByText("Authorization was cancelled. You can try connecting the account again.")).not.toBeInTheDocument());
  });

  it("does not expose provider-specific configuration text for a generic OAuth callback", async () => {
    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&oauth=error&reason=provider_configuration_error#overview",
    );

    expect(await screen.findByText("The extension OAuth configuration needs attention. Open the extension Setup page.")).toBeInTheDocument();
    expect(screen.queryByText("provider_configuration_error")).not.toBeInTheDocument();
  });

  it("renders a static manifest page without requiring a project selection", async () => {
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      ui_manifest: {
        pages: [{
          page_id: "about",
          title: "About",
          project_scoped: false,
          components: [{ component_id: "copy", type: "markdown", content: "Neutral extension documentation." }],
        }],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement?extension_page=about");

    expect(await screen.findByText("Neutral extension documentation.")).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Project scope" })).not.toBeInTheDocument();
    expect(mockedFetchProjectConnectionsList).not.toHaveBeenCalled();
  });

  it("renders a generic extension connection form after a project is selected", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }], page: 1, per_page: 200, total: 1, total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: { pages: [{
        page_id: "configuration",
        title: "Configuration",
        components: [imapConnectionFormFixture()],
      }] },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    await chooseProject(user, "project-1");

    expect(await screen.findByText("IMAP mailbox connection")).toBeVisible();
    expect(screen.getByLabelText("IMAP host")).toBeVisible();
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("creates a generic extension connection with its manifest-declared type", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }], page: 1, per_page: 200, total: 1, total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: { pages: [{
        page_id: "configuration",
        title: "Configuration",
        components: [imapConnectionFormFixture()],
      }] },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    await chooseProject(user, "project-1");
    await user.type(await screen.findByLabelText("IMAP host"), "imap.example.test");
    await user.type(screen.getByLabelText("Username"), "mailbox-user");
    await user.type(screen.getByLabelText("Password"), "secret-value");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() => expect(mockedCreateProjectConnection).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({
        connection_kind: "extension",
        connection_type: "imap_mailbox",
        extension_id: "smoke.guardrail.enforcement",
      }),
    ));
  });

  it("submits the current DOM secret when browser autofill leaves controlled state stale", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }], page: 1, per_page: 200, total: 1, total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      manifest_version: 2,
      action_definitions: [{
        action_id: "save_secrets",
        parameters: [{ name: "bot_token", type: "string", required: false }],
      }],
      ui_manifest: { pages: [{
        page_id: "configuration",
        title: "Configuration",
        components: [{
          component_id: "secrets_form",
          type: "form",
          title: "Secrets",
          ui_schema: {
            fields: [{ field_id: "bot_token", path: "bot_token", widget: "secret", label: "Bot Token" }],
          },
          data: { load_query: "get_secrets", save_action: "save_secrets" },
        }],
      }] },
    });
    mockedExecuteExtensionQuery
      .mockResolvedValueOnce({
        values: { bot_token: "" },
        summary: { bot_token_configured: false },
      })
      .mockResolvedValueOnce({
        values: { bot_token: "" },
        summary: { bot_token_configured: true },
      });
    mockedExecuteExtensionAction.mockResolvedValue({
      action: {
        status: "succeeded",
        response: { ok: true, reload: ["secrets_form"] },
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    await chooseProject(user, "project-1");
    const tokenInput = await screen.findByLabelText("Bot Token") as HTMLInputElement;
    await waitFor(() => expect(mockedExecuteExtensionQuery).toHaveBeenCalled());

    const nativeValueSetter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    nativeValueSetter?.call(tokenInput, "browser-filled-value");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(mockedExecuteExtensionAction).toHaveBeenCalledWith(
        "smoke.guardrail.enforcement",
        "save_secrets",
        expect.objectContaining({ input: { bot_token: "browser-filled-value" } }),
        expect.any(String),
      );
    });
    await waitFor(() => expect(mockedExecuteExtensionQuery).toHaveBeenCalledTimes(2));
  });

  it("submits an explicitly cleared optional text action input and reloads persisted values", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }], page: 1, per_page: 200, total: 1, total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      manifest_version: 2,
      action_definitions: [{
        action_id: "save_config",
        parameters: [
          { name: "disable_web_page_preview", type: "boolean", required: false },
          {
            name: "message_prefix",
            value_type: "text",
            required: false,
            schema: { type: ["string", "null"] },
          },
        ],
      }],
      ui_manifest: { pages: [{
        page_id: "configuration",
        title: "Configuration",
        components: [{
          component_id: "config_form",
          type: "form",
          title: "Message defaults",
          ui_schema: {
            fields: [
              {
                path: "disable_web_page_preview",
                widget: "boolean",
                label: "Disable Link Previews",
              },
              { path: "message_prefix", widget: "text", label: "Message Prefix" },
            ],
            actions: { save_label: "Save defaults" },
          },
          data: { load_query: "get_config", save_action: "save_config" },
        }],
      }] },
    });
    mockedExecuteExtensionQuery
      .mockResolvedValueOnce({
        values: { disable_web_page_preview: false, message_prefix: "[Release audit]" },
      })
      .mockResolvedValueOnce({
        values: { disable_web_page_preview: false, message_prefix: "" },
      });
    mockedExecuteExtensionAction.mockResolvedValue({
      action: {
        status: "succeeded",
        response: { ok: true, reload: ["config_form"] },
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    await chooseProject(user, "project-1");
    const prefix = await screen.findByLabelText("Message Prefix");
    expect(prefix).toHaveValue("[Release audit]");
    await user.clear(prefix);
    await user.click(screen.getByRole("button", { name: "Save defaults" }));

    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledWith(
      "smoke.guardrail.enforcement",
      "save_config",
      expect.objectContaining({
        input: { disable_web_page_preview: false, message_prefix: "" },
      }),
      expect.any(String),
    ));
    await waitFor(() => expect(mockedExecuteExtensionQuery).toHaveBeenCalledTimes(2));
    expect(prefix).toHaveValue("");
  });

  it("hydrates delayed repeater rows once and preserves user edits across a later refetch", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      manifest_version: 2,
      action_definitions: [
        {
          action_id: "save_records",
          parameters: [{ name: "records", type: "array", required: true }],
        },
      ],
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "records_form",
                type: "form",
                title: "Records",
                ui_schema: {
                  fields: [
                    {
                      field_id: "records",
                      path: "records",
                      widget: "repeater",
                      label: "Records",
                      props: {
                        fields: [
                          {
                            path: "record_id",
                            label: "Record ID",
                            widget: "text",
                            required: true,
                            props: { immutable_on_edit: true },
                          },
                          {
                            path: "display_name",
                            label: "Display Name",
                            widget: "text",
                            required: true,
                          },
                        ],
                      },
                    },
                  ],
                  actions: { save_label: "Save records" },
                },
                data: { load_query: "list_records", save_action: "save_records" },
              },
            ],
          },
        ],
      },
    });
    const savedRow = {
      record_id: "saved-record",
      display_name: "Saved name",
      _meta: {
        existing: true,
        immutable_fields: ["record_id"],
        original_record_id: "saved-record",
      },
    };
    mockedExecuteExtensionQuery
      .mockImplementationOnce(async () => {
        await new Promise((resolve) => globalThis.setTimeout(resolve, 50));
        return { values: { records: [savedRow] }, total: 1 } as never;
      })
      .mockResolvedValue({ values: { records: [savedRow] }, total: 1 } as never);
    mockedExecuteExtensionAction.mockResolvedValue({
      action: {
        status: "succeeded",
        response: { ok: true, reload: ["records_form"] },
      },
    });

    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&extension_page=configuration",
    );

    expect(await screen.findByText("No rows yet.")).toBeInTheDocument();
    const row = await screen.findByTestId("manifest-repeater-row");
    expect(row).toHaveTextContent("Saved name");
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("textbox", { name: /Record ID/ })).toBeDisabled();
    const displayName = screen.getByRole("textbox", { name: "Display Name" });
    await user.clear(displayName);
    await user.type(displayName, "Locally-edited-name");
    await user.click(screen.getByRole("button", { name: "Save row" }));
    await user.click(screen.getByRole("button", { name: "Save records" }));

    await waitFor(() => expect(mockedExecuteExtensionQuery).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("manifest-repeater-row")).toHaveTextContent(
      "Locally-edited-name",
    );
    expect(mockedExecuteExtensionAction).toHaveBeenCalledWith(
      "smoke.guardrail.enforcement",
      "save_records",
      expect.objectContaining({
        input: {
          records: [
            expect.objectContaining({
              record_id: "saved-record",
              display_name: "Locally-edited-name",
            }),
          ],
        },
      }),
      expect.any(String),
    );
  });

  it.each([
    ["pattern", { type: "string", pattern: ".+" }],
    ["enum", { type: "string", enum: ["configured"] }],
  ])("omits an explicitly blank action input when its %s schema rejects empty", async (_name, schema) => {
    const user = userEvent.setup();
    mockedExecuteExtensionQuery.mockResolvedValue({
      values: { restricted_value: "configured" },
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      manifest_version: 2,
      action_definitions: [{
        action_id: "save_config",
        parameters: [{
          name: "restricted_value",
          type: "string",
          required: false,
          schema,
        }],
      }],
      ui_manifest: { pages: [{
        page_id: "configuration",
        title: "Configuration",
        components: [{
          component_id: "config_form",
          type: "form",
          title: "Configuration",
          ui_schema: {
            fields: [{ path: "restricted_value", widget: "text", label: "Restricted value" }],
          },
          data: { load_query: "get_config", save_action: "save_config" },
        }],
      }] },
    });
    mockedExecuteExtensionAction.mockResolvedValue({
      action: { status: "succeeded", response: { ok: true } },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    const input = await screen.findByLabelText("Restricted value");
    await waitFor(() => expect(input).toHaveValue("configured"));
    await user.clear(input);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledWith(
      "smoke.guardrail.enforcement",
      "save_config",
      expect.objectContaining({ input: {} }),
      expect.any(String),
    ));
  });

  it("omits a blank configured secret action input while reloading its persisted status", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }], page: 1, per_page: 200, total: 1, total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      manifest_version: 2,
      action_definitions: [{
        action_id: "save_secrets",
        parameters: [{ name: "bot_token", type: "string", required: false }],
      }],
      ui_manifest: { pages: [{
        page_id: "configuration",
        title: "Configuration",
        components: [{
          component_id: "secrets_form",
          type: "form",
          title: "Secrets",
          ui_schema: {
            fields: [{ path: "bot_token", widget: "secret", label: "Bot Token" }],
          },
          data: { load_query: "get_secrets", save_action: "save_secrets" },
        }],
      }] },
    });
    mockedExecuteExtensionQuery
      .mockResolvedValueOnce({
        values: { bot_token: "" },
        summary: { bot_token_configured: true },
      })
      .mockResolvedValueOnce({
        values: { bot_token: "" },
        summary: { bot_token_configured: true },
      });
    mockedExecuteExtensionAction.mockResolvedValue({
      action: {
        status: "succeeded",
        response: { ok: true, reload: ["secrets_form"] },
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    await chooseProject(user, "project-1");
    expect(await screen.findByLabelText("Bot Token")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledWith(
      "smoke.guardrail.enforcement",
      "save_secrets",
      expect.objectContaining({ input: {} }),
      expect.any(String),
    ));
    await waitFor(() => expect(mockedExecuteExtensionQuery).toHaveBeenCalledTimes(2));
    expect(screen.getByText("bot token configured:")).toBeVisible();
    expect(screen.getByText("Yes")).toBeVisible();
  });

  it("rejects an explicitly blank required text action input locally", async () => {
    const user = userEvent.setup();
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      manifest_version: 2,
      action_definitions: [{
        action_id: "save_config",
        parameters: [{ name: "required_label", type: "string", required: true }],
      }],
      ui_manifest: { pages: [{
        page_id: "configuration",
        title: "Configuration",
        components: [{
          component_id: "config_form",
          type: "form",
          title: "Configuration",
          ui_schema: {
            fields: [{
              path: "required_label",
              widget: "text",
              label: "Required label",
              required: true,
            }],
          },
          data: { save_action: "save_config" },
        }],
      }] },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    await user.click(await screen.findByRole("button", { name: "Save" }));

    expect(await screen.findByText("Required Label is required.")).toBeVisible();
    expect(mockedExecuteExtensionAction).not.toHaveBeenCalled();
  });

  it("materializes manifest action inputs using declared parameter types", async () => {
    const user = userEvent.setup();
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      manifest_version: 2,
      action_definitions: [{
        action_id: "save_inbound_settings",
        parameters: [
          { name: "inbound_mode", type: "string" },
          { name: "polling_interval_seconds", type: "integer" },
          { name: "backoff_factor", type: "number" },
          { name: "polling_enabled", type: "boolean" },
        ],
      }],
      ui_manifest: { pages: [{
        page_id: "inbound",
        title: "Inbound",
        components: [{
          component_id: "inbound_settings",
          type: "form",
          title: "Inbound settings",
          values: {
            inbound_mode: "auto",
            polling_interval_seconds: "60",
            backoff_factor: "1.5",
            polling_enabled: "true",
          },
          ui_schema: {
            fields: [
              {
                path: "inbound_mode",
                label: "Inbound mode",
                widget: "select",
                options: [{ value: "auto", label: "Auto" }],
              },
              {
                path: "polling_interval_seconds",
                label: "Polling interval",
                widget: "select",
                options: [{ value: "60", label: "60 seconds" }],
              },
              {
                path: "backoff_factor",
                label: "Backoff factor",
                widget: "select",
                options: [{ value: "1.5", label: "1.5" }],
              },
              {
                path: "polling_enabled",
                label: "Polling enabled",
                widget: "select",
                options: [{ value: "true", label: "Enabled" }],
              },
            ],
          },
          data: { save_action: "save_inbound_settings" },
        }],
      }] },
    });
    mockedExecuteExtensionAction.mockResolvedValue({
      action: { status: "succeeded", response: { ok: true } },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    await chooseManifestOption(user, "Inbound mode", "Auto");
    await chooseManifestOption(user, "Polling interval", "60 seconds");
    await chooseManifestOption(user, "Backoff factor", "1.5");
    await chooseManifestOption(user, "Polling enabled", "Enabled");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledWith(
      "smoke.guardrail.enforcement",
      "save_inbound_settings",
      expect.objectContaining({
        input: {
          inbound_mode: "auto",
          polling_interval_seconds: 60,
          backoff_factor: 1.5,
          polling_enabled: true,
        },
      }),
      expect.any(String),
    ));
  });

  it.each([
    ["integer", "not-an-integer"],
    ["number", "not-a-number"],
    ["boolean", "not-a-boolean"],
  ] as const)("does not submit an invalid %s manifest action input", async (type, value) => {
    const user = userEvent.setup();
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      manifest_version: 2,
      action_definitions: [{
        action_id: "save_inbound_settings",
        parameters: [{ name: "polling_interval_seconds", type }],
      }],
      ui_manifest: { pages: [{
        page_id: "inbound",
        title: "Inbound",
        components: [{
          component_id: "inbound_settings",
          type: "form",
          title: "Inbound settings",
          values: { polling_interval_seconds: value },
          ui_schema: {
            fields: [{
              path: "polling_interval_seconds",
              label: "Polling interval",
              widget: "select",
              options: [{ value, label: "Invalid interval" }],
            }],
          },
          data: { save_action: "save_inbound_settings" },
        }],
      }] },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    await user.click(await screen.findByRole("button", { name: "Save" }));

    expect(await screen.findByText(`Polling Interval Seconds must be a valid ${type}.`))
      .toBeVisible();
    expect(mockedExecuteExtensionAction).not.toHaveBeenCalled();
  });

  it("translates a provider secret validation code into actionable copy", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }], page: 1, per_page: 200, total: 1, total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      manifest_version: 2,
      action_definitions: [{ action_id: "save_secrets", parameters: [{ name: "bot_token" }] }],
      ui_manifest: { pages: [{
        page_id: "configuration",
        title: "Configuration",
        components: [{
          component_id: "secrets_form",
          type: "form",
          title: "Secrets",
          ui_schema: {
            fields: [{ field_id: "bot_token", path: "bot_token", widget: "secret", label: "Bot Token" }],
          },
          data: { save_action: "save_secrets" },
        }],
      }] },
    });
    mockedExecuteExtensionAction.mockRejectedValueOnce(new HttpError({
      status: 400,
      url: "/api/extensions/smoke.guardrail.enforcement/action/save_secrets",
      payload: {
        message: "Extension action failed",
        action: {
          status: "failed",
          detail: "provider_secret_value_required",
          response: { ok: false, error: "provider_secret_value_required" },
        },
      },
    }));

    renderPage("/extensions/smoke.guardrail.enforcement");
    await chooseProject(user, "project-1");
    await user.click(await screen.findByRole("button", { name: "Save" }));

    const friendlyMessages = await screen.findAllByText("Enter a secret value before saving.");
    friendlyMessages.forEach((message) => expect(message).toBeVisible());
    expect(screen.queryByText("provider_secret_value_required")).not.toBeInTheDocument();
  });

  it("uses manifest-declared OAuth guidance instead of a core provider settings route", async () => {
    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&oauth=error&reason=provider_configuration_error#configuration",
    );

    expect(await screen.findByText("The extension OAuth configuration needs attention. Open the extension Setup page.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /settings/i })).not.toBeInTheDocument();
  });

  it("keeps an OAuth callback failure safe when the extension page is static", async () => {
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      ui_manifest: { pages: [{
        page_id: "help",
        title: "Help",
        project_scoped: false,
          components: [{
            component_id: "copy",
            anchor_id: "retry",
            type: "markdown",
            content: "Connection help.",
          }],
      }] },
    });
    renderPage(
      "/extensions/smoke.guardrail.enforcement?extension_page=help&oauth=error&reason=missing_required_scope#retry",
    );

    expect(await screen.findByText("Required access was not granted. Reconnect and approve the permissions declared by the extension.")).toBeInTheDocument();
    const helpTarget = await screen.findByRole("region", { name: "retry" });
    await waitFor(() => expect(helpTarget).toHaveFocus());
  });

  it("does not render an undeclared provider name from an OAuth callback error", async () => {
    renderPage(
      "/extensions/smoke.guardrail.enforcement?oauth=error&reason=OAuthTokenEndpointError#configuration",
    );

    expect(await screen.findByText("Authorization failed. Try connecting the account again.")).toBeInTheDocument();
    expect(screen.queryByText("OAuthTokenEndpointError")).not.toBeInTheDocument();
  });

  it.each([
    [
      "authorization_cancelled",
      "Authorization was cancelled. You can try connecting the account again.",
    ],
    [
      "duplicate_connection",
      "This external account is already connected to this project. Choose another account or reconnect the existing connection.",
    ],
    [
      "missing_required_scope",
      "Required access was not granted. Reconnect and approve the permissions declared by the extension.",
    ],
    [
      "mailbox_unusable",
      "Authorization failed. Try connecting the account again.",
    ],
    [
      "provider_configuration_error",
      "The extension OAuth configuration needs attention. Open the extension Setup page.",
    ],
  ])("shows safe OAuth guidance for %s and preserves project/hash", async (reason, guidance) => {
    renderPage(
      `/extensions/smoke.guardrail.enforcement?project_id=project-1&oauth=error&reason=${reason}#overview`,
    );

    expect(await screen.findByText(guidance)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/extensions/smoke.guardrail.enforcement?project_id=project-1#overview",
      );
    });
  });

  it("does not disclose an unrecognised OAuth callback reason", async () => {
    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&oauth=error&reason=OAuthTokenEndpointError#overview",
    );

    expect(await screen.findByText("Authorization failed. Try connecting the account again.")).toBeInTheDocument();
    expect(screen.queryByText(/OAuthTokenEndpointError/)).not.toBeInTheDocument();
  });

  it("names the installs the Active filter is hiding instead of advertising an archive", async () => {
    // AC17-R66: a fresh install arrives with its extensions already installed and every one
    // disabled, and this view opens on Active. The owner saw "0 VISIBLE", an empty list, and
    // a banner telling him to install an archive with a CLI command his Compact deployment
    // refuses -- while the two extensions he wanted were sitting right there, disabled.
    mockedFetchExtensions.mockResolvedValue([
      {
        extension_id: "acme.documents",
        display_name: "Acme Documents",
        version: "1.0.0",
        state: "disabled",
        active: false,
      },
      {
        extension_id: "acme.storefront",
        display_name: "Acme Shop",
        version: "1.0.0",
        state: "disabled",
        active: false,
      },
    ]);

    renderPage();

    expect(await screen.findByText("Nothing enabled yet")).toBeInTheDocument();
    expect(
      await screen.findByText(/2 extensions are installed but disabled/),
    ).toBeInTheDocument();
    // it must not send someone hunting for a download they already have
    expect(screen.queryByText(/install an extension from a .zip/i)).not.toBeInTheDocument();

    // and one click reaches them, rather than "choose Disabled or All" prose
    fireEvent.click(await screen.findByTestId("extensions-show-disabled"));

    expect(await screen.findByText("Acme Documents")).toBeInTheDocument();
    expect(await screen.findByText("Acme Shop")).toBeInTheDocument();
  });

  it("keeps the generic empty state when there is genuinely nothing installed", async () => {
    mockedFetchExtensions.mockResolvedValue([]);

    renderPage();

    expect(await screen.findByText("No active extensions")).toBeInTheDocument();
    expect(screen.queryByTestId("extensions-show-disabled")).not.toBeInTheDocument();
  });

  it("keeps OAuth callback guidance independent of the extension catalog record", async () => {
    mockedFetchExtensions.mockResolvedValue([
      {
        extension_id: "partner.calendar",
        display_name: "Partner Calendar",
        version: "1.0.0",
        state: "active",
        active: true,
      },
    ]);
    mockedFetchExtension.mockResolvedValue({
      extension_id: "partner.calendar",
      display_name: "Partner Calendar",
      version: "1.0.0",
      state: "active",
      active: true,
    });

    renderPage("/extensions/partner.calendar?oauth=error&reason=duplicate_connection#connections");

    expect(await screen.findByText("This external account is already connected to this project. Choose another account or reconnect the existing connection.")).toBeInTheDocument();
  });

  it("renders nested health payloads from the backend contract", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("Smoke Guardrail Enforcement");
    await user.click(screen.getByRole("button", { name: "Extension runtime health" }));

    await waitFor(() => {
      expect(mockedCheckExtensionHealth).toHaveBeenCalledWith("smoke.guardrail.enforcement");
      expect(screen.getByText("Healthy")).toBeInTheDocument();
    });
  });

  it("renders installed extensions as a catalog table with publisher metadata", async () => {
    renderPage();

    expect(await screen.findByRole("columnheader", { name: "Extension" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Publisher" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Category" })).toBeInTheDocument();
    // The table keeps its header while loading, so wait for the row itself.
    expect(await screen.findByText("Enforces extension runtime guardrails.")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Security Automation" })).toBeInTheDocument();
    expect(screen.queryByText("Official")).not.toBeInTheDocument();
  });

  it("keeps a floor under the Extension name column so a medium screen scrolls the table instead", async () => {
    renderPage();

    expect(await screen.findByText("Enforces extension runtime guardrails.")).toBeInTheDocument();
    // The name cell is the last child of the logo lockup, inside the installed table.
    const table = screen.getByRole("table", { name: "Installed extensions" });
    expect(table).toHaveClass("fs-table");
    expect(table.innerHTML).toMatch(/<div class="fs-catalog-icon-lockup">.*?<div class="fs-extensions-name-cell">.*?<\/div><\/div><\/td>/);
    // The lockup zeroes its last child's min-width with two classes of specificity, so a floor
    // on the bare cell class loses (the name read "H…" at about 1024px). The floor needs more.
    expect(catalogBrowseStyles).toMatch(/\.fs-catalog-icon-lockup > :last-child\s*\{[^}]*min-width:\s*0;/);
    expect(extensionStyles).not.toMatch(/(^|\n)\.fs-extensions-name-cell\s*\{[^}]*min-width/);
    expect(extensionStyles).toMatch(
      /\.fs-table \.fs-catalog-icon-lockup > \.fs-extensions-name-cell\s*\{[^}]*min-width:\s*calc\(var\(--fs-space-400\) \* 6\);/,
    );
  });

  it("filters installed extensions by display name and extension id", async () => {
    const user = userEvent.setup();
    mockedFetchExtensions.mockResolvedValueOnce([
      {
        extension_id: "security.guardrails",
        display_name: "Guardrail Enforcement",
        version: "1.0.0",
        state: "active",
        active: true,
        category: "security-automation",
      },
      {
        extension_id: "messaging.chat-relay",
        display_name: "Chat Relay Notifications",
        version: "1.0.0",
        state: "active",
        active: true,
        category: "messaging-notifications",
      },
    ]);

    renderPage();

    await screen.findByText("Guardrail Enforcement");
    await user.type(screen.getByRole("searchbox", { name: "Search extensions" }), "chat relay");

    expect(screen.getByText("Chat Relay Notifications")).toBeInTheDocument();
    expect(screen.queryByText("Guardrail Enforcement")).not.toBeInTheDocument();

    await user.clear(screen.getByRole("searchbox", { name: "Search extensions" }));
    await user.type(screen.getByRole("searchbox", { name: "Search extensions" }), "security.guardrails");

    expect(screen.getByText("Guardrail Enforcement")).toBeInTheDocument();
    expect(screen.queryByText("Chat Relay Notifications")).not.toBeInTheDocument();
  });

  it("filters extensions by category and shows a specific no-match state", async () => {
    const user = userEvent.setup();
    mockedFetchExtensions.mockResolvedValueOnce([
      {
        extension_id: "security.guardrails",
        display_name: "Guardrail Enforcement",
        version: "1.0.0",
        state: "active",
        active: true,
        category: "security-automation",
      },
      {
        extension_id: "messaging.chat-relay",
        display_name: "Chat Relay Notifications",
        version: "1.0.0",
        state: "active",
        active: true,
        category: "messaging-notifications",
      },
      {
        extension_id: "utility.sample",
        display_name: "Sample Utility",
        version: "1.0.0",
        state: "active",
        active: true,
      },
    ]);

    renderPage();

    await screen.findByText("Guardrail Enforcement");
    await user.click(screen.getByRole("button", { name: "Add filter" }));
    await user.click(await screen.findByRole("menuitem", { name: "Category" }));
    const categoryMenu = await screen.findByRole("menu", { name: "Choose a filter" });
    expect(
      within(categoryMenu)
        .getAllByRole("menuitem")
        .map((option) => option.textContent)
        .filter((label) => label !== "All fields"),
    ).toEqual(["Messaging Notifications", "Security Automation", "Uncategorized"]);

    await user.click(within(categoryMenu).getByRole("menuitem", { name: "Messaging Notifications" }));
    expect(screen.getByText("Chat Relay Notifications")).toBeInTheDocument();
    expect(screen.queryByText("Guardrail Enforcement")).not.toBeInTheDocument();
    expect(screen.queryByText("Sample Utility")).not.toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Search extensions" }), "guardrail");
    expect(screen.getByText("No extensions match the current filters.")).toBeInTheDocument();
  });

  it("shows only Healthy for a successful health result", async () => {
    const user = userEvent.setup();
    mockedCheckExtensionHealth.mockResolvedValueOnce({
      extension_id: "smoke.guardrail.enforcement",
      status: "healthy",
      latency_ms: null,
    });

    renderPage();

    await screen.findByText("Smoke Guardrail Enforcement");
    await user.click(screen.getByRole("button", { name: "Extension runtime health" }));

    await waitFor(() => {
      expect(screen.getByText("Healthy")).toBeInTheDocument();
    });
    expect(screen.queryByText(/Health:/u)).not.toBeInTheDocument();
    expect(screen.queryByText(/Reason:/u)).not.toBeInTheDocument();
    expect(screen.queryByText(/Checked:/u)).not.toBeInTheDocument();
  });

  it("hides successful health diagnostics from the ordinary status", async () => {
    const user = userEvent.setup();
    mockedCheckExtensionHealth.mockResolvedValueOnce({
      extension_id: "smoke.guardrail.enforcement",
      status: "healthy",
      latency_ms: null,
      reason: "All runtime checks passed",
      checked_at: "2026-07-30T20:30:00Z",
    });

    renderPage();

    await screen.findByText("Smoke Guardrail Enforcement");
    await user.click(screen.getByRole("button", { name: "Extension runtime health" }));

    expect(await screen.findByText("Healthy")).toBeInTheDocument();
    expect(screen.queryByText(/All runtime checks passed/u)).not.toBeInTheDocument();
    expect(screen.queryByText(/2026-07-30T20:30:00Z/u)).not.toBeInTheDocument();
  });

  it("shows Unhealthy with a short reason and hides raw diagnostics", async () => {
    const user = userEvent.setup();
    mockedCheckExtensionHealth.mockResolvedValueOnce({
      extension_id: "smoke.guardrail.enforcement",
      status: "unhealthy",
      latency_ms: 900,
      reason: "Runtime probe failed",
      checked_at: "2026-07-30T20:30:00Z",
      detail: { secret: "raw diagnostic" }, // pragma: allowlist secret - redaction test sentinel
    });

    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");
    await user.click(screen.getByRole("button", { name: "Extension runtime health" }));

    expect(await screen.findByText("Unhealthy — Runtime probe failed")).toBeInTheDocument();
    expect(screen.queryByText(/900ms/u)).not.toBeInTheDocument();
    expect(screen.queryByText(/2026-07-30T20:30:00Z/u)).not.toBeInTheDocument();
    expect(screen.queryByText(/raw diagnostic/u)).not.toBeInTheDocument();
  });

  it("shows an update success message with from/to versions", async () => {
    mockedInstallExtensionFromFile.mockResolvedValueOnce({
      extension: {
        extension_id: "fs_simple_probe",
        display_name: "FS Simple Probe",
        version: "1.0.1",
        lifecycle_operation: "updated",
        from_version: "1.0.0",
        to_version: "1.0.1",
      },
    });

    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");

    fireEvent.change(screen.getByLabelText("Extension package file"), {
      target: {
        files: [new File(["zip"], "fs_simple_probe-1.0.1.zip", { type: "application/zip" })],
      },
    });

    await waitFor(() => {
      expect(mockedInstallExtensionFromFile).toHaveBeenCalledTimes(1);
      expect(screen.getByText("Updated FS Simple Probe from 1.0.0 to 1.0.1.")).toBeInTheDocument();
    });
  });

  it("fails closed for archive install and uninstall when the managed root is read-only", async () => {
    mockedFetchExtensionArchiveLifecycleAvailability.mockResolvedValueOnce({
      available: false,
      reason_code: "managed_root_read_only",
      message: "Extension archive lifecycle is unavailable in this deployment.",
      remediation: {
        install_update: ["./flow-steward archive-install-from-capability"],
        uninstall: ["./flow-steward archive-uninstall-from-capability"],
      },
    });

    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");

    expect(screen.getByRole("button", { name: "Install extension from file" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Uninstall" })).toBeDisabled();
    expect(
      screen.getByText("./flow-steward archive-install-from-capability"),
    ).toBeInTheDocument();
    expect(screen.getByText("./flow-steward archive-uninstall-from-capability"))
      .toBeInTheDocument();
    fireEvent.drop(screen.getByTestId("extension-install-dropzone"), {
      dataTransfer: { files: [new File(["zip"], "blocked.zip", { type: "application/zip" })] },
    });
    expect(mockedInstallExtensionFromFile).not.toHaveBeenCalled();
    expect(mockedFetchExtensionUninstallPlan).not.toHaveBeenCalled();
  });

  it("offers no uninstall bypass for any extension when the archive root is read-only", async () => {
    mockedFetchExtensionArchiveLifecycleAvailability.mockResolvedValueOnce({
      available: false,
      reason_code: "managed_root_read_only",
      message: "Extension archive lifecycle is unavailable in this deployment.",
      remediation: {
        install_update: ["./flow-steward extensions install --archive <path>"],
        uninstall: ["./flow-steward extensions uninstall <extension-id>"],
      },
    });
    // A record still claiming the retired "system" origin earns nothing: every
    // extension uninstalls the same way.
    mockedFetchExtensions.mockResolvedValueOnce([{
      extension_id: "acme.mail",
      display_name: "Acme Mail",
      state: "active",
      active: true,
      origin: "system",
    }]);

    renderPage();
    await screen.findByText("Acme Mail");

    expect(screen.getByRole("button", { name: "Uninstall" })).toBeDisabled();
    expect(mockedFetchExtensionUninstallPlan).not.toHaveBeenCalled();
  });

  it("fails closed with a distinct message when capability verification fails", async () => {
    mockedFetchExtensionArchiveLifecycleAvailability.mockRejectedValueOnce(
      new Error("network failed"),
    );

    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");

    expect(screen.getByRole("button", { name: "Install extension from file" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Uninstall" })).toBeDisabled();
    expect(
      screen.getByText("Could not verify whether extension archive changes are available."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/extensions install --archive/u)).not.toBeInTheDocument();
  });

  it("fails closed when a cached positive capability refetch fails", async () => {
    mockedFetchExtensionArchiveLifecycleAvailability.mockRejectedValueOnce(
      new Error("network failed"),
    );

    renderPage("/extensions", "admin", (queryClient) => {
      queryClient.setQueryData(["extensions", "archive-lifecycle-capability"], {
        available: true,
        reason_code: "available",
        message: "",
        remediation: { install_update: [], uninstall: [] },
      });
    });
    await screen.findByText("Smoke Guardrail Enforcement");
    await screen.findByText(
      "Could not verify whether extension archive changes are available.",
    );

    expect(screen.getByRole("button", { name: "Install extension from file" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Uninstall" })).toBeDisabled();
  });

  it("keeps archive controls disabled while capability verification is loading", async () => {
    mockedFetchExtensionArchiveLifecycleAvailability.mockImplementationOnce(
      () => new Promise(() => undefined),
    );

    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");

    expect(screen.getByRole("button", { name: "Install extension from file" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Uninstall" })).toBeDisabled();
    expect(
      screen.getByText("Checking extension archive lifecycle availability…"),
    ).toBeInTheDocument();
  });

  it("shows file install failures from Marketplace and allows a retry", async () => {
    mockedInstallExtensionFromFile.mockRejectedValueOnce(new HttpError({
      status: 400,
      url: "/api/extensions/install",
      payload: { detail: "Extension archive is missing extension.yaml." },
    }));

    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");

    await userEvent.setup().click(screen.getByRole("tab", { name: "Catalog" }));

    const fileInput = screen.getByLabelText("Extension package file");
    fireEvent.change(fileInput, {
      target: {
        files: [new File(["invalid"], "broken-extension.zip", { type: "application/zip" })],
      },
    });

    expect(
      await screen.findByText("Extension archive is missing extension.yaml."),
    ).toBeVisible();
    expect(
      within(screen.getByRole("tabpanel", { name: "Installed" }))
        .queryByTestId("extension-install-notice"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Installing broken-extension.zip...")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install extension from file" })).toBeEnabled();

    mockedInstallExtensionFromFile.mockResolvedValueOnce({
      extension: {
        extension_id: "retry.extension",
        display_name: "Retry Extension",
        version: "1.0.0",
      },
    });
    fireEvent.change(fileInput, {
      target: {
        files: [new File(["zip"], "retry-extension.zip", { type: "application/zip" })],
      },
    });

    expect(await screen.findByText("Installed Retry Extension 1.0.0.")).toBeVisible();
    expect(mockedInstallExtensionFromFile).toHaveBeenCalledTimes(2);
  });

  it("frames a refused install as a failure and a completed one as a status", async () => {
    // One slot carries progress, success and failure. A refusal is printed
    // exactly as the server phrased it — nothing here reads it — but it has to
    // announce itself as a failure, or it is a grey line the operator scrolls
    // past after waiting for a large upload.
    mockedInstallExtensionFromFile.mockRejectedValueOnce(new HttpError({
      status: 400,
      url: "/api/extensions/install",
      payload: { detail: "No compatible wheel for python requirement 'numpy==2.4.2'." },
    }));

    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");
    const fileInput = screen.getByLabelText("Extension package file");

    fireEvent.change(fileInput, {
      target: {
        files: [new File(["zip"], "image-processing.zip", { type: "application/zip" })],
      },
    });

    await screen.findByText("No compatible wheel for python requirement 'numpy==2.4.2'.");
    const failure = screen.getByTestId("extension-install-notice");
    expect(failure).toHaveAttribute("data-install-notice", "error");
    expect(within(failure).getByRole("alert")).toHaveTextContent(
      "No compatible wheel for python requirement 'numpy==2.4.2'.",
    );

    // The next install reuses the same slot and must not inherit that framing.
    mockedInstallExtensionFromFile.mockResolvedValueOnce({
      extension: {
        extension_id: "fs_simple_probe",
        display_name: "FS Simple Probe",
        version: "1.0.1",
      },
    });
    fireEvent.change(fileInput, {
      target: {
        files: [new File(["zip"], "fs_simple_probe-1.0.1.zip", { type: "application/zip" })],
      },
    });

    await screen.findByText("Installed FS Simple Probe 1.0.1.");
    const success = screen.getByTestId("extension-install-notice");
    expect(success).toHaveAttribute("data-install-notice", "status");
    expect(within(success).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("requires explicit browser authority confirmation and retries the same file", async () => {
    const user = userEvent.setup();
    const firstHash = "a".repeat(64);
    const staleHash = "b".repeat(64);
    const approvalError = (impactHash: string) => new HttpError({
      status: 412,
      url: "/api/extensions/install",
      payload: {
        error: "browser_authority_approval_required",
        extension_id: "acme.browser-picker",
        impact_hash: impactHash,
        remote_script_urls: ["https://sdk.example.test/picker.js"],
        remote_origins: ["https://sdk.example.test"],
        local_asset_refs: ["assets/picker.js"],
      },
    });
    mockedInstallExtensionFromFile
      .mockRejectedValueOnce(approvalError(firstHash))
      .mockRejectedValueOnce(approvalError(staleHash))
      .mockResolvedValueOnce({
        extension: {
          extension_id: "acme.browser-picker",
          display_name: "Browser Picker",
          version: "1.0.0",
        },
      });

    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");
    const file = new File(["same-zip"], "browser-picker.zip", { type: "application/zip" });
    fireEvent.change(screen.getByLabelText("Extension package file"), {
      target: { files: [file] },
    });

    expect(await screen.findByRole("heading", { name: "Approve external browser code" })).toBeInTheDocument();
    expect(screen.getByText("acme.browser-picker")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm and continue" }));
    expect(await screen.findByText(/changed while approval was pending/u)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm and continue" }));

    expect(await screen.findByText("Installed Browser Picker 1.0.0.")).toBeInTheDocument();
    expect(mockedInstallExtensionFromFile).toHaveBeenNthCalledWith(1, file);
    expect(mockedInstallExtensionFromFile).toHaveBeenNthCalledWith(2, file, firstHash);
    expect(mockedInstallExtensionFromFile).toHaveBeenNthCalledWith(3, file, staleHash);
  });

  it("cancels browser authority approval without retrying or leaving install busy", async () => {
    const user = userEvent.setup();
    mockedInstallExtensionFromFile.mockRejectedValueOnce(new HttpError({
      status: 412,
      url: "/api/extensions/install",
      payload: {
        error: "browser_authority_approval_required",
        extension_id: "acme.browser-picker",
        impact_hash: "c".repeat(64),
      },
    }));

    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");
    fireEvent.change(screen.getByLabelText("Extension package file"), {
      target: { files: [new File(["zip"], "browser-picker.zip")] },
    });
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("heading", { name: "Approve external browser code" })).not.toBeInTheDocument();
    expect(mockedInstallExtensionFromFile).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Installation cancelled.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install extension from file" })).toBeEnabled();
  });

  it("rejects non-zip drag-and-drop uploads with a clear message", async () => {
    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");

    fireEvent.drop(screen.getByTestId("extension-install-dropzone"), {
      dataTransfer: {
        files: [new File(["nope"], "not-an-extension.txt", { type: "text/plain" })],
      },
    });

    expect(
      await screen.findByText("Install extension from file accepts only .zip archives."),
    ).toBeInTheDocument();
  });

  it("keeps broken extensions out of the default view but shows them in All", async () => {
    const user = userEvent.setup();
    mockedFetchExtensions.mockResolvedValueOnce([
      {
        extension_id: "active.extension",
        display_name: "Active Extension",
        version: "1.0.0",
        state: "active",
        active: true,
      },
      {
        extension_id: "broken.extension",
        display_name: "Broken Extension",
        version: "1.0.0",
        state: "disabled",
        active: false,
        broken: true,
        broken_reason: "Extension bundle is missing.",
      },
    ]);

    renderPage();

    expect(await screen.findByText("Active Extension")).toBeInTheDocument();
    expect(screen.queryByText("Broken Extension")).not.toBeInTheDocument();

    await chooseStatus(user, "All");

    expect(await screen.findByText("Broken Extension")).toBeInTheDocument();
    expect(screen.getByText("Extension bundle is missing.")).toBeInTheDocument();
  });

  it("keeps Active empty when installed extensions exist but none are active", async () => {
    const user = userEvent.setup();
    mockedFetchExtensions.mockResolvedValueOnce([
      {
        extension_id: "disabled.chat-relay",
        display_name: "Chat Relay Notifications",
        version: "1.0.0",
        state: "disabled",
        active: false,
      },
      {
        extension_id: "disabled.sample_tasks",
        display_name: "Sample Tasks Tasks",
        version: "1.0.0",
        state: "disabled",
        active: false,
      },
    ]);

    renderPage();

    // AC17-R66 changed only the wording here: it now counts the installs the filter is
    // hiding rather than telling the operator to "enable an extension" while showing none.
    // The filter mechanics this test exists for are unchanged and still asserted below.
    expect(await screen.findByText("Nothing enabled yet")).toBeInTheDocument();
    expect(
      screen.getByText(/2 extensions are installed but disabled/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Chat Relay Notifications")).not.toBeInTheDocument();
    expect(screen.queryByText("Sample Tasks Tasks")).not.toBeInTheDocument();

    await chooseStatus(user, "Disabled");

    expect(await screen.findByText("Chat Relay Notifications")).toBeInTheDocument();
    expect(screen.getByText("Sample Tasks Tasks")).toBeInTheDocument();

    await chooseStatus(user, "Active");

    expect(await screen.findByText("Nothing enabled yet")).toBeInTheDocument();
    expect(screen.queryByText("Chat Relay Notifications")).not.toBeInTheDocument();

    await chooseStatus(user, "All");

    expect(await screen.findByText("Chat Relay Notifications")).toBeInTheDocument();
    expect(screen.getByText("Sample Tasks Tasks")).toBeInTheDocument();
  });

  it("uses detail API state for direct extension URLs without loading the catalog", async () => {
    const user = userEvent.setup();
    mockedFetchExtension.mockResolvedValueOnce({
      extension_id: "audit.extension",
      display_name: "Audit Extension",
      version: "1.0.0",
      state: "active",
      active: true,
    });

    renderPage("/extensions/audit.extension");

    expect(await screen.findAllByText("Audit Extension")).not.toHaveLength(0);
    expect(mockedFetchExtension).toHaveBeenCalledWith("audit.extension");
    expect(mockedFetchExtensions).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Disable" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Extension runtime health" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Uninstall" })).toBeEnabled();
    expect(screen.queryByText("Extension was not found.")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Extension runtime health" }));

    await waitFor(() => {
      expect(mockedCheckExtensionHealth).toHaveBeenCalledWith("audit.extension");
      expect(screen.getByText("Healthy")).toBeInTheDocument();
    });
  });

  it.each([false, undefined])("propagates real install availability (%s) through LLM Enable, Disable and reload", async (initialActive) => {
    const user = userEvent.setup();
    let extension = {
      extension_id: "acme.llm", install_id: "install-acme-llm", display_name: "Acme LLM Provider",
      version: "1.0.0", state: "disabled", active: initialActive, runtime_mode: "subprocess",
    };
    mockedFetchExtension.mockImplementation(async () => extension);
    mockedSetExtensionEnabled.mockImplementation(async (_id, active) => {
      extension = { ...extension, active, state: active ? "active" : "disabled" };
      return {};
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({ runtime: { extension_contract_v2: {
      llm_provider: { schema_version: "llm_provider_extension_v1", required_provider_secrets: ["api_key"] },
    } } });
    const view = renderPage("/extensions/acme.llm");
    await screen.findByRole("button", { name: "Discover models" });
    expect(screen.getByRole("button", { name: "Discover models" })).toBeDisabled();
    await user.type(screen.getByLabelText("API key"), "unsaved-value"); // pragma: allowlist secret - test sentinel
    await user.click(screen.getByRole("button", { name: "Enable" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Discover models" })).toBeEnabled());
    expect(screen.getByLabelText("API key")).toHaveValue("unsaved-value");
    await user.click(screen.getByRole("button", { name: "Discover models" }));
    await waitFor(() => expect(mockedRefreshLlmProviderModels).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Discover models" })).toBeDisabled());
    expect(screen.getByLabelText("API key")).toHaveValue("unsaved-value");
    view.unmount();
    renderPage("/extensions/acme.llm");
    await screen.findByText("Enable this extension before checking the connection.");
    expect(screen.getByRole("button", { name: "Discover models" })).toBeDisabled();
    expect(mockedRefreshLlmProviderModels).toHaveBeenCalledTimes(1);
    expect(mockedSetLlmProviderModelEnabled).not.toHaveBeenCalled();
  });

  it("keeps an unconfigured remote LLM extension disabled while exposing configuration", async () => {
    const user = userEvent.setup();
    const extension = {
      extension_id: "acme.router-llm",
      install_id: "install-acme-llm",
      display_name: "Acme LLM Provider",
      version: "1.0.0",
      state: "disabled",
      active: false,
      runtime_mode: "remote_https",
      remote_base_url: "",
    };
    mockedFetchExtensions.mockResolvedValue([extension]);
    mockedFetchExtension.mockResolvedValue(extension);
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      runtime: {
        extension_contract_v2: {
          llm_provider: {
            schema_version: "llm_provider_extension_v1",
            required_provider_secrets: ["api_key"],
          },
        },
      },
    });

    renderPage("/extensions/acme.router-llm");

    expect(await screen.findByLabelText("Runtime endpoint URL")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable" })).toBeDisabled();
    expect(
      screen.getByText("Save a public HTTPS runtime endpoint below before enabling this extension."),
    ).toBeInTheDocument();
    expect(mockedSetExtensionEnabled).not.toHaveBeenCalled();
    mockedSaveLlmProviderCredentials.mockImplementationOnce(async () => {
      mockedFetchExtension.mockResolvedValue({ ...extension, remote_base_url: "https://runtime.example.com" });
      return {} as never;
    });
    await user.type(screen.getByLabelText("Runtime endpoint URL"), "https://runtime.example.com");
    await user.type(screen.getByLabelText("API key"), "bootstrap-value"); // pragma: allowlist secret - test sentinel
    await user.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Enable" })).toBeEnabled());
    await waitFor(() => expect(screen.getByText(/Settings saved without checking the connection/)).toBeVisible());
    expect(mockedSaveLlmProviderCredentials).toHaveBeenCalledExactlyOnceWith("install-acme-llm", {
      secrets: { api_key: "bootstrap-value" }, // pragma: allowlist secret - test sentinel
      connection_config: {}, remote_base_url: "https://runtime.example.com",
    });
    expect(mockedRefreshLlmProviderModels).not.toHaveBeenCalled();
    expect(mockedSetLlmProviderModelEnabled).not.toHaveBeenCalled();
  });

  it("asks an in-process LLM extension for no runtime endpoint", async () => {
    const extension = {
      extension_id: "acme.llm",
      install_id: "install-acme-llm",
      display_name: "Acme LLM Provider",
      version: "1.1.0",
      state: "disabled",
      active: false,
      runtime_mode: "subprocess",
      remote_base_url: "",
    };
    mockedFetchExtensions.mockResolvedValue([extension]);
    mockedFetchExtension.mockResolvedValue(extension);
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      runtime: {
        extension_contract_v2: {
          llm_provider: {
            schema_version: "llm_provider_extension_v1",
            runtime_mode: "subprocess",
            required_provider_secrets: ["api_key"],
          },
        },
      },
    });

    renderPage("/extensions/acme.llm");

    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    expect(screen.queryByLabelText("Runtime endpoint URL")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable" })).toBeEnabled();
    expect(
      screen.queryByText("Save a public HTTPS runtime endpoint below before enabling this extension."),
    ).not.toBeInTheDocument();
  });

  it("blocks the grid enable toggle for a remote extension without a runtime endpoint", async () => {
    const user = userEvent.setup();
    mockedFetchExtensions.mockResolvedValueOnce([
      {
        extension_id: "acme.local-llm",
        display_name: "Acme Local LLM",
        version: "1.0.0",
        state: "disabled",
        active: false,
        runtime_mode: "remote_https",
        remote_base_url: "",
      },
    ]);

    renderPage();

    await chooseStatus(user, "Disabled");
    await user.click(await screen.findByRole("button", { name: "Enable" }));

    expect(
      await screen.findByText(
        "Save a public HTTPS runtime endpoint on the extension page before enabling.",
      ),
    ).toBeInTheDocument();
    expect(mockedSetExtensionEnabled).not.toHaveBeenCalled();
  });

  it("shows the backend error when extension activation is rejected", async () => {
    const user = userEvent.setup();
    mockedFetchExtensions.mockResolvedValueOnce([
      {
        extension_id: "configured.remote",
        display_name: "Configured Remote",
        version: "1.0.0",
        state: "disabled",
        active: false,
        runtime_mode: "remote_https",
        remote_base_url: "https://runtime.example.com",
      },
    ]);
    mockedSetExtensionEnabled.mockRejectedValueOnce(
      new HttpError({
        status: 400,
        url: "/api/extensions/configured.remote/enable",
        payload: { detail: "remote_base_url is required before activation" },
      }),
    );

    renderPage();

    await chooseStatus(user, "Disabled");
    await user.click(await screen.findByRole("button", { name: "Enable" }));

    expect(
      await screen.findByText("remote_base_url is required before activation"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Updating active state...")).not.toBeInTheDocument();
  });

  it("invalidates every projected artifact catalog after lifecycle changes", async () => {
    const user = userEvent.setup();
    mockedFetchExtensions.mockResolvedValueOnce([
      {
        extension_id: "projected.catalogs",
        display_name: "Projected Catalogs",
        version: "1.0.0",
        state: "disabled",
        active: false,
        runtime_mode: "local",
      },
    ]);
    const { queryClient } = renderPage();
    const affectedCatalogs = [
      ["workflows"],
      ["registry", "agents"],
      ["registry", "skills"],
      ["registry", "tools"],
      ["registry", "datasets"],
      ["dataset-query-templates"],
    ] as const;
    for (const queryKey of affectedCatalogs) {
      queryClient.setQueryData(queryKey, []);
    }

    await chooseStatus(user, "Disabled");
    await user.click(await screen.findByRole("button", { name: "Enable" }));

    await waitFor(() => {
      for (const queryKey of affectedCatalogs) {
        expect(queryClient.getQueryState(queryKey)?.isInvalidated).toBe(true);
      }
    });
  });

  it("shows a rejected activation error on the extension detail page", async () => {
    const user = userEvent.setup();
    const extension = {
      extension_id: "audit.lifecycle-fixture",
      display_name: "AUDIT Lifecycle Fixture",
      version: "1.0.3",
      state: "disabled",
      active: false,
      runtime_mode: "subprocess",
    };
    mockedFetchExtension.mockResolvedValueOnce(extension);
    mockedSetExtensionEnabled.mockRejectedValueOnce(
      new HttpError({
        status: 400,
        url: "/api/extensions/audit.lifecycle-fixture/enable",
        payload: {
          detail:
            "Managed extension release artifact is not available; reinstall or repair the extension before enabling.",
        },
      }),
    );

    renderPage("/extensions/audit.lifecycle-fixture");

    await user.click(await screen.findByRole("button", { name: "Enable" }));

    expect(
      await screen.findByText(
        "Managed extension release artifact is not available; reinstall or repair the extension before enabling.",
      ),
    ).toBeInTheDocument();
  });

  it("previews uninstall impact and requires explicit revision confirmation", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderPage();
    queryClient.setQueryData(["model-selection", "options"], { provider_options: [] });
    await screen.findByText("Smoke Guardrail Enforcement");

    await user.click(screen.getByRole("button", { name: "Uninstall" }));

    const firstDialog = await screen.findByRole("dialog");
    expect(screen.getByText("Will be removed")).toBeInTheDocument();
    expect(
      within(firstDialog).getAllByText("Workflow", { selector: "span" }).length,
    ).toBeGreaterThan(0);
    expect(within(firstDialog).getByText("Guardrail check")).toBeInTheDocument();
    expect(screen.getByText("Will remain but stop working")).toBeInTheDocument();
    expect(within(firstDialog).getByText("Customer retained workflow")).toBeInTheDocument();
    // Row ids are storage keys, not something the person confirming recognises.
    expect(within(firstDialog).queryByText(/customer_retained_workflow|pi_smoke/)).not.toBeInTheDocument();
    expect(within(firstDialog).getByText(/^Needs Smoke Guardrail Enforcement;/)).toBeInTheDocument();
    expect(within(firstDialog).queryByText(/Missing required extension/)).not.toBeInTheDocument();
    const confirmButton = within(firstDialog).getByRole("button", { name: "Uninstall extension" });
    expect(confirmButton).toBeDisabled();

    await user.click(within(firstDialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockedConfirmExtensionUninstall).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Uninstall" }));
    const secondDialog = await screen.findByRole("dialog");
    await user.click(within(secondDialog).getByText("I understand this can't be undone."));
    await user.click(within(secondDialog).getByRole("button", { name: "Uninstall extension" }));

    await waitFor(() => {
      expect(mockedConfirmExtensionUninstall).toHaveBeenCalledWith(
        "smoke.guardrail.enforcement",
        "revision-1",
      );
      expect(queryClient.getQueryState(["model-selection", "options"])?.isInvalidated).toBe(true);
    });
  });

  it("blocks an open uninstall confirmation when capability verification later fails", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");

    await user.click(screen.getByRole("button", { name: "Uninstall" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByText("I understand this can't be undone."));
    expect(within(dialog).getByRole("button", { name: "Uninstall extension" })).toBeEnabled();

    mockedFetchExtensionArchiveLifecycleAvailability.mockRejectedValueOnce(
      new Error("network failed"),
    );
    await queryClient.refetchQueries({
      queryKey: ["extensions", "archive-lifecycle-capability"],
    });

    expect(
      await screen.findByText("Could not verify whether extension archive changes are available."),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Uninstall extension" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Uninstall extension" }));
    expect(mockedConfirmExtensionUninstall).not.toHaveBeenCalled();
  });

  it("says an uninstall was refused instead of staying on Uninstalling", async () => {
    // Seen in the demo: the dialog came back with no explanation and the row
    // kept saying "Uninstalling extension..." for good.
    const user = userEvent.setup();
    mockedConfirmExtensionUninstall.mockRejectedValueOnce(
      new HttpError({
        payload: { detail: "Installed extension active release path is invalid" },
        status: 400,
        url: "/api/extensions/smoke.guardrail.enforcement/uninstall",
      }),
    );
    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");

    await user.click(screen.getByRole("button", { name: "Uninstall" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByText("I understand this can't be undone."));
    await user.click(within(dialog).getByRole("button", { name: "Uninstall extension" }));

    expect(
      await within(dialog).findByRole("alert"),
    ).toHaveTextContent("Installed extension active release path is invalid");
    expect(screen.queryByText("Uninstalling extension...")).not.toBeInTheDocument();
    expect(await screen.findByText("Uninstall failed.")).toBeInTheDocument();
  });

  it("shows an install whose files are gone as such, and uninstalls it by its real id", async () => {
    // The demo listed "acme_file_sync" (a folder name) with "—" and
    // "Uncategorized"; its Uninstall asked for a plan under the folder name.
    const user = userEvent.setup();
    mockedFetchExtensions.mockResolvedValue([
      {
        extension_id: "acme.file-sync",
        display_name: "acme.file-sync",
        version: "1.0.0",
        state: "disabled",
        active: false,
        broken: true,
        broken_reason: "Files missing. Uninstall to clean up.",
        files_missing: true,
      },
    ]);
    renderPage();
    // Disabled installs sit behind the Active filter.
    fireEvent.click(await screen.findByTestId("extensions-show-disabled"));

    expect(await screen.findByText("Files missing. Uninstall to clean up.")).toBeInTheDocument();
    expect(screen.getByText("1.0.0")).toBeInTheDocument();
    expect(screen.queryByText("Uncategorized")).not.toBeInTheDocument();
    expect(screen.queryByText("—")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Uninstall" }));
    expect(mockedFetchExtensionUninstallPlan).toHaveBeenCalledWith("acme.file-sync");
  });

  it("leaves the extension's own page for the Installed list after uninstalling it", async () => {
    // Seen in the demo: the page stayed on the uninstalled extension's card,
    // old version and Uninstall button included. The list reloads without the
    // extension before the "is this its page" check ran.
    const user = userEvent.setup();
    renderPage("/extensions/smoke.guardrail.enforcement");
    const uninstall = await screen.findByRole("button", { name: "Uninstall" });
    mockedFetchExtensions.mockResolvedValue([]);

    await user.click(uninstall);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByText("I understand this can't be undone."));
    await user.click(within(dialog).getByRole("button", { name: "Uninstall extension" }));

    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent("/extensions#installed"),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("says why the uninstall plan did not open", async () => {
    // Seen in the demo: rows built from a folder name asked for a plan by an id
    // no install has; the 404 was swallowed and the click did nothing.
    const user = userEvent.setup();
    mockedFetchExtensionUninstallPlan.mockRejectedValueOnce(
      new HttpError({
        payload: { detail: "Extension 'smoke.guardrail.enforcement' not found" },
        status: 404,
        url: "/api/extensions/smoke.guardrail.enforcement/uninstall-plan",
      }),
    );
    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");

    await user.click(screen.getByRole("button", { name: "Uninstall" }));

    expect(
      await screen.findByText(
        "Couldn't load the uninstall plan: Extension 'smoke.guardrail.enforcement' not found",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the refreshed plan when the uninstall impact changed", async () => {
    const user = userEvent.setup();
    mockedConfirmExtensionUninstall.mockRejectedValueOnce(
      new HttpError({
        payload: {
          detail: "Extension uninstall impact changed; refresh the plan before confirming",
          stale_plan: true,
          current_plan: {
            extension_id: "smoke.guardrail.enforcement",
            install_id: "pi_smoke",
            release_id: "pr_smoke",
            revision: "revision-2",
            remove: [
              { kind: "workflow", id: "new_wf", label: "Workflow", display_name: "Added meanwhile", count: 1 },
            ],
            affected: [],
          },
        },
        status: 409,
        url: "/api/extensions/smoke.guardrail.enforcement/uninstall",
      }),
    );
    renderPage();
    await screen.findByText("Smoke Guardrail Enforcement");

    await user.click(screen.getByRole("button", { name: "Uninstall" }));
    const first = await screen.findByRole("dialog");
    await user.click(within(first).getByText("I understand this can't be undone."));
    await user.click(within(first).getByRole("button", { name: "Uninstall extension" }));

    const refreshed = await screen.findByRole("dialog");
    expect(await within(refreshed).findByText("Added meanwhile")).toBeInTheDocument();
    expect(within(refreshed).getByRole("alert")).toHaveTextContent(
      "The plan changed; review it and confirm again.",
    );
    // The new plan needs its own acknowledgement.
    expect(within(refreshed).getByRole("checkbox")).not.toBeChecked();
    expect(screen.queryByText("Uninstalling extension...")).not.toBeInTheDocument();

    await user.click(within(refreshed).getByText("I understand this can't be undone."));
    await user.click(within(refreshed).getByRole("button", { name: "Uninstall extension" }));
    await waitFor(() =>
      expect(mockedConfirmExtensionUninstall).toHaveBeenLastCalledWith(
        "smoke.guardrail.enforcement",
        "revision-2",
      ),
    );
  });

  it("shows each installed extension's logo in its table row, the glyph when it has none", async () => {
    mockedFetchExtensions.mockResolvedValue([
      {
        extension_id: "smoke.guardrail.enforcement",
        display_name: "Smoke Guardrail Enforcement",
        version: "1.0.1",
        state: "active",
        active: true,
        has_icon: true,
        icon_url: "/api/extensions/smoke.guardrail.enforcement/icon?v=1",
      },
      {
        extension_id: "acme.no-logo",
        display_name: "No Logo",
        version: "1.0.0",
        state: "active",
        active: true,
        has_icon: false,
        icon_url: "",
      },
    ]);
    renderPage();

    await screen.findByText("Smoke Guardrail Enforcement");
    const [, withLogo, withoutLogo] = within(screen.getByRole("table", { name: "Installed extensions" })).getAllByRole("row");
    expect(within(withLogo).getByTestId("catalog-icon")).toHaveClass("fs-catalog-icon-row");
    expect(within(withLogo).getByRole("presentation")).toHaveAttribute(
      "src",
      "/api/extensions/smoke.guardrail.enforcement/icon?v=1",
    );
    expect(within(withoutLogo).queryByRole("presentation")).not.toBeInTheDocument();
    expect(within(withoutLogo).getByTestId("catalog-icon")).toHaveAttribute("data-catalog-icon", "fallback");
    expect(within(withoutLogo).getByTestId("catalog-icon")).toContainHTML("lucide-package");
  });

  it("puts the extension's logo before its name on its own page", async () => {
    mockedFetchExtension.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      display_name: "Smoke Guardrail Enforcement",
      version: "1.0.1",
      state: "active",
      active: true,
      has_icon: true,
      icon_url: "/api/extensions/smoke.guardrail.enforcement/icon?v=1",
    });
    renderPage("/extensions/smoke.guardrail.enforcement");

    expect(await screen.findByRole("heading", { level: 1, name: "Smoke Guardrail Enforcement" })).toBeInTheDocument();
    const [pageTile] = screen.getAllByTestId("catalog-icon").filter((tile) => tile.className.includes("fs-catalog-icon-page"));
    expect(within(pageTile).getByRole("presentation")).toHaveAttribute(
      "src",
      "/api/extensions/smoke.guardrail.enforcement/icon?v=1",
    );
  });

  it("keeps every row action reachable in the design-system actions column", async () => {
    renderPage();

    await screen.findByText("Smoke Guardrail Enforcement");

    expect(screen.getByRole("columnheader", { name: "Actions" })).toBeInTheDocument();
    const [, firstRow] = within(screen.getByRole("table", { name: "Installed extensions" })).getAllByRole("row");
    for (const name of ["Open", "Disable", "Extension runtime health", "Uninstall"]) {
      expect(within(firstRow).getByRole(name === "Open" ? "link" : "button", { name })).toBeInTheDocument();
    }
    const openLink = within(firstRow).getByRole("link", { name: "Open" });
    fireEvent.click(within(firstRow).getByRole("button", { name: "Extension runtime health" }));
    await waitFor(() => expect(mockedCheckExtensionHealth).toHaveBeenCalled());
    await waitFor(() => expect(within(firstRow).getByRole("button", { name: "Extension runtime health" })).toBeEnabled());
    expect(within(firstRow).getByRole("link", { name: "Open" })).toBe(openLink);
  });

  it("renders static extension pages without project scope", async () => {
    const user = userEvent.setup();
    globalThis.localStorage.setItem("platform-last-project-id", "project-1");
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          {
            page_id: "about",
            title: "About",
            components: [
              {
                component_id: "about_copy",
                type: "markdown",
                title: "Extension About",
                body: "Static About copy.",
              },
            ],
          },
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "configuration_form",
                type: "form",
                title: "Project Configuration",
                fields: [],
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1");

    expect(await screen.findByText("Static About copy.")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Project scope" })).not.toBeInTheDocument();
    expect(screen.queryByText("Select a project to continue.")).not.toBeInTheDocument();
    expect(mockedFetchProjectsList).not.toHaveBeenCalled();
    expect(mockedFetchProjectConnectionsList).not.toHaveBeenCalled();
    expect(mockedFetchExtensionProjectState).not.toHaveBeenCalled();
    expect(mockedFetchExtensionCallbacks).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedFetchExtensionPreview).toHaveBeenCalledWith(
        "smoke.guardrail.enforcement",
        undefined,
      );
    });

    await user.click(screen.getByRole("tab", { name: "Configuration" }));

    expect(await screen.findByRole("combobox", { name: "Project scope" })).toBeInTheDocument();
    expect(screen.getByText("Project Configuration")).toBeInTheDocument();
    await waitFor(() => {
      expect(mockedFetchProjectConnectionsList).toHaveBeenCalledWith("project-1");
      expect(mockedFetchExtensionPreview).toHaveBeenCalledWith(
        "smoke.guardrail.enforcement",
        "project-1",
      );
    });

    await user.click(screen.getByRole("tab", { name: "About" }));
    expect(await screen.findByText("Static About copy.")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Project scope" })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(mockedFetchExtensionPreview).toHaveBeenLastCalledWith(
        "smoke.guardrail.enforcement",
        undefined,
      );
    });
  });

  it("keeps project scope on an explicitly scoped Markdown OAuth setup page", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      oauth_providers: [{
        provider_id: "example",
        display_name: "Example Identity",
        setup_page_id: "oauth_setup",
        credential_fields: [
          {
            field_id: "client_id",
            value_type: "text",
            required: true,
            secret: false,
            label: "Application ID",
            help_text: "Public application identifier.",
          },
          {
            field_id: "client_secret",
            value_type: "text",
            required: true,
            secret: true,
            label: "Application secret",
            help_text: "Private application credential.",
          },
        ],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
      connection_types: [],
      ui_manifest: {
        pages: [
          {
            page_id: "mail",
            title: "Mail",
            components: [{
              component_id: "mail_copy",
              type: "markdown",
              content: "Default mail page.",
            }],
          },
          {
            page_id: "oauth_setup",
            title: "OAuth Setup",
            project_scoped: true,
            components: [{
              component_id: "setup_copy",
              type: "markdown",
              content: "Provider setup page.",
            }],
          },
        ],
      },
    });

    renderPage(
      "/extensions/smoke.guardrail.enforcement?project_id=project-1&oauth_provider_id=example",
    );

    expect(await screen.findByText("Provider setup page.")).toBeVisible();
    expect(screen.queryByText("Default mail page.")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "OAuth Setup" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Project scope" })).toHaveTextContent("Project One");
    });

    await user.click(screen.getByRole("tab", { name: "Mail" }));
    expect(await screen.findByText("Default mail page.")).toBeVisible();
    expect(screen.getByTestId("location-probe")).not.toHaveTextContent("oauth_provider_id");
  });

  it("fails an unknown OAuth provider hint closed to the neutral default page", async () => {
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      oauth_providers: [{
        provider_id: "example",
        setup_page_id: "oauth_setup",
      }],
      ui_manifest: {
        pages: [
          {
            page_id: "overview",
            title: "Overview",
            components: [{
              component_id: "overview_copy",
              type: "markdown",
              content: "Neutral extension overview.",
            }],
          },
          {
            page_id: "oauth_setup",
            title: "OAuth Setup",
            components: [{
              component_id: "setup_copy",
              type: "markdown",
              content: "Sensitive provider setup.",
            }],
          },
        ],
      },
    });

    renderPage(
      "/extensions/smoke.guardrail.enforcement?oauth_provider_id=foreign&extension_page=oauth_setup",
    );

    expect(await screen.findByText("Neutral extension overview.")).toBeVisible();
    expect(screen.queryByText("Sensitive provider setup.")).not.toBeInTheDocument();
  });

  it("renders a static connector catalog without selecting a project", async () => {
    const catalog = Array.from({ length: 40 }, (_, index) => ({
      connector_type_id: `toolbox_connector_${index + 1}`,
      display_name: `Connector ${index + 1}`,
    }));
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      ui_manifest: {
        pages: [
          {
            page_id: "supported_connectors",
            title: "Supported connectors",
            project_scoped: false,
            components: [
              {
                component_id: "catalog",
                type: "grid",
                title: "Supported connectors",
                query_id: "supported_connectors",
                columns: [
                  { key: "connector_type_id", label: "Connector ID" },
                  { key: "display_name", label: "Connector" },
                ],
                pagination: { page_size: 50 },
              },
            ],
          },
        ],
      },
    });
    mockedExecuteExtensionQuery.mockResolvedValue({
      connectors: catalog,
      total: 40,
    } as never);

    renderPage("/extensions/smoke.guardrail.enforcement");

    expect(await screen.findByText("Connector 20")).toBeInTheDocument();
    expect(screen.queryByText("Connector 21")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Project scope" })).not.toBeInTheDocument();
    expect(mockedFetchProjectsList).not.toHaveBeenCalled();
    expect(mockedExecuteExtensionQuery).toHaveBeenCalledWith(
      "smoke.guardrail.enforcement",
      "supported_connectors",
      expect.objectContaining({ context: {} }),
    );
    expect(mockedExecuteExtensionQuery).toHaveBeenCalledTimes(1);
  });

  it("renders the canonical project-scoped health query instead of the manifest fallback", async () => {
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          {
            page_id: "overview",
            title: "Overview",
            components: [
              {
                component_id: "provider_health",
                type: "health_panel",
                title: "Runtime Health",
                query_id: "provider_health",
              },
            ],
          },
        ],
      },
    });
    mockedExecuteExtensionQuery.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      query_id: "provider_health",
      query: {
        status: "succeeded",
        response: {
          ok: true,
          result: {
            status: "healthy",
            provider: "smoke.guardrail.enforcement",
            runtime: "local_subprocess",
            checks: [{ name: "Subprocess runtime", status: "healthy" }],
          },
        },
      },
    } as never);

    renderPage("/extensions/smoke.guardrail.enforcement?project_id=project-1");

    expect(await screen.findByText("Runtime Health")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText("healthy")).toHaveLength(2);
    });
    expect(screen.queryByText("unknown")).not.toBeInTheDocument();
    expect(mockedExecuteExtensionQuery).toHaveBeenCalledWith(
      "smoke.guardrail.enforcement",
      "provider_health",
      expect.objectContaining({ context: { project_id: "project-1" } }),
    );
  });

  it("queries only the canonical page when switching between cached extensions", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const manifests = {
      "extension.alpha": {
        ui_manifest: {
          pages: [{
            page_id: "alpha-page",
            title: "Alpha page",
            components: [{
              component_id: "alpha-catalog",
              type: "grid",
              title: "Alpha catalog",
              query_id: "alpha-query",
              columns: [],
            }],
          }],
        },
      },
      "extension.beta": {
        ui_manifest: {
          pages: [{
            page_id: "beta-page",
            title: "Beta page",
            components: [{
              component_id: "beta-catalog",
              type: "grid",
              title: "Beta catalog",
              query_id: "beta-query",
              columns: [],
            }],
          }],
        },
      },
    };
    const detailRecord = (extensionId: string) => ({
      extension_id: extensionId,
      display_name: extensionId,
      version: "1.0.0",
      state: "active",
      active: true,
    });
    for (const extensionId of ["extension.alpha", "extension.beta"] as const) {
      queryClient.setQueryData(
        ["extensions", "compiled-manifest", extensionId],
        manifests[extensionId],
      );
      queryClient.setQueryData(["extensions", "detail", extensionId], detailRecord(extensionId));
    }
    mockedFetchExtension.mockImplementation(async (extensionId) => detailRecord(extensionId));
    mockedFetchExtensionCompiledManifest.mockImplementation(
      async (extensionId) => manifests[extensionId as keyof typeof manifests],
    );

    renderCachedExtensionTransition(queryClient);

    expect(await screen.findByText("Alpha catalog")).toBeInTheDocument();
    await waitFor(() => {
      expect(mockedExecuteExtensionQuery).toHaveBeenCalledWith(
        "extension.alpha",
        "alpha-query",
        expect.anything(),
      );
    });
    mockedExecuteExtensionQuery.mockClear();

    await user.click(screen.getByRole("button", { name: "Switch to beta" }));

    expect(await screen.findByText("Beta catalog")).toBeInTheDocument();
    await waitFor(() => {
      expect(
        mockedExecuteExtensionQuery.mock.calls.filter(
          ([extensionId, queryId]) => extensionId === "extension.beta" && queryId === "beta-query",
        ),
      ).toHaveLength(1);
    });
  });

  it("returns to the manifest default when a valid prior page is removed from the URL", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const manifest = {
      ui_manifest: {
        pages: [
          {
            page_id: "setup",
            title: "Setup",
            components: [{ type: "markdown", content: "Default setup page." }],
          },
          {
            page_id: "service",
            title: "Service",
            components: [{ type: "markdown", content: "Selected service page." }],
          },
        ],
      },
    };
    const detail = {
      extension_id: "extension.alpha",
      display_name: "Alpha",
      version: "1.0.0",
      state: "active",
      active: true,
    };
    queryClient.setQueryData(["extensions", "compiled-manifest", "extension.alpha"], manifest);
    queryClient.setQueryData(["extensions", "detail", "extension.alpha"], detail);
    mockedFetchExtension.mockResolvedValue(detail);
    mockedFetchExtensionCompiledManifest.mockResolvedValue(manifest);

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[
          "/extensions/extension.alpha?extension_page=service",
        ]}>
          <ExtensionPageParamRemover />
          <Routes>
            <Route path="/extensions/:extensionId" element={<ExtensionsHubPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Selected service page.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Remove extension page" }));
    expect(await screen.findByText("Default setup page.")).toBeVisible();
    expect(screen.queryByText("Selected service page.")).not.toBeInTheDocument();
  });

  it("hides extension-owned manifest UI immediately after disabling from detail", async () => {
    const user = userEvent.setup();
    const activeExtension = {
      extension_id: "smoke.guardrail.enforcement",
      display_name: "Smoke Guardrail Enforcement",
      version: "1.0.1",
      state: "active",
      active: true,
      vendor_name: "Flow Steward",
    };
    const disabledExtension = {
      ...activeExtension,
      state: "disabled",
      active: false,
    };
    mockedFetchExtensions
      .mockResolvedValueOnce([activeExtension])
      .mockResolvedValue([disabledExtension]);
    mockedFetchExtension
      .mockResolvedValueOnce(activeExtension)
      .mockResolvedValue(disabledExtension);
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "connection",
                type: "connection_form",
                title: "Service connection",
                fields: [],
              },
            ],
          },
        ],
      },
    });

    const { queryClient } = renderPage("/extensions/smoke.guardrail.enforcement");
    queryClient.setQueryData(["model-selection", "options"], { provider_options: [] });
    queryClient.setQueryData(["projects"], []);

    expect(await screen.findByText("Service connection")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Disable" }));

    await waitFor(() => {
      expect(mockedSetExtensionEnabled).toHaveBeenCalledWith(
        "smoke.guardrail.enforcement",
        false,
      );
      expect(screen.getByText("Enable this extension to configure its project-scoped UI.")).toBeInTheDocument();
      expect(screen.queryByText("Service connection")).not.toBeInTheDocument();
      expect(queryClient.getQueryState(["model-selection", "options"])?.isInvalidated).toBe(true);
      expect(queryClient.getQueryState(["projects"])?.isInvalidated).toBe(true);
    });
  });

  it("creates manifest-defined connections with string-list fields, secrets, and defaults", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "service_connection",
                type: "connection_form",
                title: "Service connection",
                data: {
                  connection_kind: "extension",
                  connection_type: "api_token",
                  provider: "example_api",
                  config_fields: ["provider", "endpoint", "timeout", "mode", "client_id", "source"],
                  secret_fields: ["api_token"],
                  required_secret_fields: ["api_token"],
                  tags: ["example-service"],
                },
                defaults: {
                  display_name: "Example Extension",
                  timeout: 30,
                  mode: "sync",
                },
                fields: [
                  { path: "display_name", label: "Display name", widget: "text" },
                  { path: "endpoint", label: "Endpoint", widget: "text", required: true },
                  { path: "timeout", label: "Timeout", widget: "integer", required: true },
                  {
                    path: "mode",
                    label: "Mode",
                    widget: "select",
                    required: true,
                    options: [
                      { value: "sync", label: "Synchronous" },
                      { value: "async", label: "Asynchronous" },
                    ],
                  },
                  { path: "client_id", label: "Client ID", widget: "text", required: true },
                  { path: "api_token", label: "API token", widget: "secret" },
                  { path: "source", label: "Source", widget: "text", required: true },
                ],
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await user.type(await screen.findByLabelText("Endpoint"), "https://api.example.test");
    await user.type(screen.getByLabelText("Client ID"), "example-client");
    await user.type(screen.getByLabelText("API token"), "example-token");
    await user.type(screen.getByLabelText("Source"), "workflow");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() => {
      expect(mockedCreateProjectConnection).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({
          connection_kind: "extension",
          connection_type: "api_token",
          display_name: "Example Extension",
          extension_id: "smoke.guardrail.enforcement",
          connection_config: {
            provider: "example_api",
            endpoint: "https://api.example.test",
            timeout: 30,
            mode: "sync",
            client_id: "example-client",
            source: "workflow",
          },
          secret_refs: { refs: { api_token: "api_token" } },
          tags: ["example-service"],
        }),
      );
      expect(mockedSaveProjectConnectionSecrets).toHaveBeenCalledWith(
        "project-1",
        "conn_created",
        { api_token: "example-token" },
      );
      expect(mockedUpdateProjectConnection).toHaveBeenCalledWith(
        "project-1",
        "conn_created",
        { enabled: true },
      );
    });
  });

  it("validates blank connection fields in manifest order before required secrets", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [{
          page_id: "configuration",
          title: "Configuration",
          components: [{
            component_id: "imap_mailbox_connection",
            type: "connection_form",
            title: "IMAP mailbox connection",
            data: {
              connection_kind: "extension",
              connection_type: "imap_mailbox",
              config_fields: ["imap_host", "username"],
              secret_fields: ["password"],
              required_secret_fields: ["password"],
            },
            fields: [
              { path: "imap_host", label: "IMAP host", widget: "text", required: true },
              { path: "username", label: "Username", widget: "text", required: true },
              { path: "password", label: "Password", widget: "secret", required: true },
            ],
          }],
        }],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    expect(await screen.findByText("IMAP host is required.")).toBeInTheDocument();
    expect(screen.queryByText("password is required.")).not.toBeInTheDocument();
    expect(mockedCreateProjectConnection).not.toHaveBeenCalled();
  });

  it("hydrates saved non-secret connection fields while keeping configured secrets blank", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchProjectConnectionsList.mockResolvedValue([{
      connection_id: "conn_imap",
      extension_id: "smoke.guardrail.enforcement",
      connection_type: "imap_mailbox",
      display_name: "Support mailbox",
      environment: "prod",
      connection_config: {
        imap_host: "imap.example.test",
        username: "support@example.test",
      },
      secret_refs: { refs: { password: "project_connection:conn_imap:password" } }, // pragma: allowlist secret
    }]);
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [{
          page_id: "configuration",
          title: "Configuration",
          components: [{
            component_id: "imap_mailbox_connection",
            type: "connection_form",
            title: "IMAP mailbox connection",
            data: {
              connection_kind: "extension",
              connection_type: "imap_mailbox",
              config_fields: ["imap_host", "username"],
              secret_fields: ["password"],
              required_secret_fields: ["password"],
            },
            fields: [
              { path: "imap_host", label: "IMAP host", widget: "text", required: true },
              { path: "username", label: "Username", widget: "text", required: true },
              { path: "password", label: "Password", widget: "secret", required: true },
            ],
          }],
        }],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await screen.findByText("Support mailbox");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    expect(screen.getByLabelText("IMAP host")).toHaveValue("imap.example.test");
    expect(screen.getByLabelText("Username")).toHaveValue("support@example.test");
    // The stored secret is blank on screen but still held, so demanding it again
    // would tell the user it had been lost.
    const savedSecret = screen.getByLabelText("Password");
    expect(savedSecret).toHaveValue("");
    expect(savedSecret).not.toBeRequired();
    expect(savedSecret).toHaveAccessibleDescription(
      "Saved. Leave blank to keep the stored value, or type a new one to replace it.",
    );
    expect(screen.getByLabelText("IMAP host")).toBeRequired();

    await user.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => {
      expect(mockedUpdateProjectConnection).toHaveBeenCalledWith(
        "project-1",
        "conn_imap",
        expect.objectContaining({
          connection_config: {
            imap_host: "imap.example.test",
            username: "support@example.test",
          },
        }),
      );
    });
    expect(mockedSaveProjectConnectionSecrets).not.toHaveBeenCalled();
  });

  it("removes an explicitly cleared optional email from an existing connection", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchProjectConnectionsList
      .mockResolvedValueOnce([{
        connection_id: "conn_smtp",
        extension_id: "smoke.guardrail.enforcement",
        connection_type: "custom_smtp",
        display_name: "Acme Mailer",
        connection_config: {
          provider: "custom_smtp",
          host: "mail.example.test",
          port: 587,
          security: "starttls",
          from_email: "sender@example.test",
          reply_to: "replies@example.test",
          timeout: 15,
        },
      }])
      .mockResolvedValueOnce([{
        connection_id: "conn_smtp",
        extension_id: "smoke.guardrail.enforcement",
        connection_type: "custom_smtp",
        display_name: "Acme Mailer",
        connection_config: {
          provider: "custom_smtp",
          host: "mail.example.test",
          port: 587,
          security: "starttls",
          from_email: "sender@example.test",
          timeout: 15,
        },
      }]);
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [{
          page_id: "configuration",
          title: "Configuration",
          components: [{
            component_id: "smtp_connection",
            type: "connection_form",
            title: "SMTP connection",
            data: {
              connection_kind: "extension",
              connection_type: "custom_smtp",
              provider: "custom_smtp",
              config_fields: [
                "provider", "host", "port", "security", "from_email", "reply_to", "timeout",
              ],
              secret_fields: ["password"],
            },
            defaults: { display_name: "Acme Mailer", port: 587, security: "starttls", timeout: 15 },
            fields: [
              { path: "display_name", label: "Display name", widget: "text" },
              { path: "host", label: "SMTP host", widget: "text", required: true },
              { path: "port", label: "Port", widget: "integer", required: true },
              {
                path: "security",
                label: "Security",
                widget: "select",
                required: true,
                options: [{ value: "starttls", label: "STARTTLS" }],
              },
              { path: "password", label: "Password", widget: "secret" },
              { path: "from_email", label: "From email", widget: "text", required: true },
              { path: "reply_to", label: "Reply-to", widget: "text" },
              { path: "timeout", label: "Timeout seconds", widget: "integer" },
            ],
          }],
        }],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    await chooseProject(user, "project-1");
    await screen.findByText("Acme Mailer");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const replyTo = screen.getByLabelText("Reply-to");
    expect(replyTo).toHaveValue("replies@example.test");
    await user.clear(replyTo);
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(mockedUpdateProjectConnection).toHaveBeenCalledWith(
      "project-1",
      "conn_smtp",
      expect.objectContaining({
        connection_config: {
          provider: "custom_smtp",
          host: "mail.example.test",
          port: 587,
          security: "starttls",
          from_email: "sender@example.test",
          timeout: 15,
        },
      }),
    ));
    await waitFor(() => expect(mockedFetchProjectConnectionsList).toHaveBeenCalledTimes(2));
    expect(replyTo).toHaveValue("");
  });

  it("hydrates list-projected IMAP config after creating a new connection and editing it", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchProjectConnectionsList
      .mockResolvedValueOnce([])
      .mockResolvedValue([{
        connection_id: "conn_browserqa",
        extension_id: "smoke.guardrail.enforcement",
        connection_type: "imap_mailbox",
        display_name: "Browser review mailbox",
        connection_config: JSON.stringify({
          imap_host: "imap.browserqa.test",
          username: "browserqa@localhost",
        }),
        secret_refs: { refs: { password: "project_connection:conn_browserqa:password" } }, // pragma: allowlist secret
      }]);
    mockedCreateProjectConnection.mockResolvedValue({ connection_id: "conn_browserqa" });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [{
          page_id: "configuration",
          title: "Configuration",
          components: [{
            component_id: "imap_mailbox_connection",
            type: "connection_form",
            title: "IMAP mailbox connection",
            data: {
              connection_kind: "extension",
              connection_type: "imap_mailbox",
              config_fields: ["imap_host", "username"],
              secret_fields: ["password"],
              required_secret_fields: ["password"],
            },
            fields: [
              { path: "imap_host", label: "IMAP host", widget: "text", required: true },
              { path: "username", label: "Username", widget: "text", required: true },
              { path: "password", label: "Password", widget: "secret", required: true },
            ],
          }],
        }],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await user.type(await screen.findByLabelText("IMAP host"), "imap.browserqa.test");
    await user.type(screen.getByLabelText("Username"), "browserqa@localhost");
    await user.type(screen.getByLabelText("Password"), "browserqa-password");
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    await screen.findByText("Browser review mailbox");

    await user.click(screen.getByRole("button", { name: "New connection" }));
    await user.click(screen.getByRole("button", { name: "Edit" }));

    expect(screen.getByLabelText("IMAP host")).toHaveValue("imap.browserqa.test");
    expect(screen.getByLabelText("Username")).toHaveValue("browserqa@localhost");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("renders connection test actions and multiline secret textareas", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "secure_drop_connection",
                type: "connection_form",
                title: "Secure drop connection",
                data: {
                  connection_kind: "extension",
                  connection_type: "secure_drop",
                  provider: "secure_drop",
                  test_action: "validate_connection",
                  config_fields: ["host", "port"],
                  secret_fields: ["username", "client_certificate"],
                },
                fields: [
                  { path: "host", label: "Host", widget: "text", required: true },
                  { path: "port", label: "Port", widget: "integer", minimum: 1, maximum: 65535, step: 1 },
                  { path: "username", label: "Username", widget: "secret", required: true },
                  { path: "client_certificate", label: "Client certificate", widget: "textarea", secret: true, props: { rows: 8 } },
                ],
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await user.type(screen.getByLabelText("Host"), "files.example.test");
    await user.clear(screen.getByLabelText("Port"));
    await user.type(screen.getByLabelText("Port"), "443");
    await user.type(screen.getByLabelText("Username"), "feed-user");
    const privateKey = screen.getByLabelText("Client certificate");
    const port = screen.getByLabelText("Port");
    fireEvent.change(privateKey, {
      target: { value: "-----BEGIN KEY-----\nabc\n-----END KEY-----" },
    });

    expect(privateKey.tagName).toBe("TEXTAREA");
    expect(privateKey).toHaveAttribute("rows", "8");
    expect(privateKey).toHaveClass("fs-manifest-secret-text");
    expect(port).toHaveAttribute("min", "1");
    expect(port).toHaveAttribute("max", "65535");
    expect(port).toHaveAttribute("step", "1");
    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => {
      expect(screen.getByText("Save the connection before testing it.")).toBeInTheDocument();
    });
    expect(mockedExecuteExtensionAction).not.toHaveBeenCalled();
  });

  it("switches one manifest connection form between variant-specific fields", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "secure_drop_connection",
                type: "connection_form",
                title: "Secure drop connection",
                data: {
                  connection_kind: "extension",
                  connection_type_field: "auth_mode",
                  default_connection_type: "key_pair",
                  test_action: "validate_connection",
                  connection_types: {
                    key_pair: {
                      connection_type: "key_pair",
                      provider: "secure_drop",
                      config_fields: ["host", "port", "client_identity"],
                      secret_fields: ["username", "client_certificate"],
                      required_secret_fields: ["username"],
                    },
                    token: {
                      connection_type: "token",
                      provider: "secure_drop",
                      config_fields: ["host", "port", "use_tls"],
                      secret_fields: ["username", "api_token"],
                      required_secret_fields: ["username", "api_token"],
                    },
                  },
                },
                defaults: {
                  auth_mode: "key_pair",
                  port: 443,
                  use_tls: true,
                },
                fields: [
                  {
                    path: "auth_mode",
                    label: "Authentication mode",
                    widget: "select",
                    options: [
                      { value: "key_pair", label: "Key pair" },
                      { value: "token", label: "Token" },
                    ],
                  },
                  { path: "host", label: "Server", widget: "text", required: true },
                  { path: "port", label: "Port", widget: "integer", required: true },
                  {
                    path: "use_tls",
                    label: "Use strict transport",
                    widget: "boolean",
                    visible_if: [{ path: "auth_mode", eq: "token" }],
                  },
                  { path: "username", label: "Username", widget: "secret", required: true },
                  { path: "api_token", label: "API token", widget: "secret" },
                  {
                    path: "client_certificate",
                    label: "Client certificate",
                    widget: "textarea",
                    secret: true,
                    visible_if: [{ path: "auth_mode", eq: "key_pair" }],
                  },
                ],
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    expect(screen.queryByLabelText("Client identity")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Client certificate")).toBeInTheDocument();
    expect(screen.queryByText("Use strict transport")).not.toBeInTheDocument();

    await chooseManifestOption(user, "Authentication mode", "Token");
    await waitFor(() => {
      expect(screen.queryByLabelText("Client identity")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Client certificate")).not.toBeInTheDocument();
      expect(screen.getByText("Use strict transport")).toBeInTheDocument();
    });

    await user.type(screen.getByLabelText("Server"), "files.example.test");
    await user.clear(screen.getByLabelText("Port"));
    await user.type(screen.getByLabelText("Port"), "443");
    await user.type(screen.getByLabelText("Username"), "drop-user");
    await user.type(screen.getByLabelText("API token"), "drop-token");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() => {
      expect(mockedCreateProjectConnection).toHaveBeenCalledWith(
          "project-1",
          expect.objectContaining({
          connection_type: "token",
          connection_config: {
            host: "files.example.test",
            port: 443,
            use_tls: true,
          },
          secret_refs: { refs: { username: "username", api_token: "api_token" } },
        }),
      );
    });
    expect(mockedSaveProjectConnectionSecrets).toHaveBeenCalledWith("project-1", "conn_created", {
      username: "drop-user",
      api_token: "drop-token",
    });
  });

  describe("IMAP preset connections", () => {
    beforeEach(() => {
      vi.spyOn(globalThis, "confirm").mockReturnValue(true);
      mockedFetchProjectsList.mockResolvedValue({
        projects: [{ id: "project-1", name: "Project One" }],
        page: 1,
        per_page: 200,
        total: 1,
        total_pages: 1,
      });
      mockedFetchProjectConnectionsList.mockResolvedValue([
        {
          connection_id: "conn_imap_preset",
          extension_id: "smoke.guardrail.enforcement",
          connection_type: "imap_mailbox",
          display_name: "Preset review mailbox",
          environment: "prod",
          connection_config: {
            provider: "imap",
            provider_preset: "zoho_pro",
            imap_host: "imappro.zoho.com",
            imap_port: 993,
            tls_mode: "ssl",
            username: "mailbox@example.test",
          },
          secret_refs: {
            refs: { password: "project_connection:conn_imap_preset:password" }, // pragma: allowlist secret
          },
        },
      ]);
      mockedCreateProjectConnection.mockResolvedValue({ connection_id: "conn_imap_preset" });
      mockedFetchExtensionCompiledManifest.mockResolvedValue({
        account_scope: { scope_type: "project" },
        manifest_version: 2,
        action_definitions: [
          {
            action_id: "test_connection",
            parameters: [
              { name: "connection_ref", value_type: "connection_ref", required: true },
            ],
          },
        ],
        ui_manifest: {
          pages: [
            {
              page_id: "configuration",
              title: "Configuration",
              components: [imapConnectionFormFixture()],
            },
          ],
        },
      });
    });

    async function openPresetForm() {
      const user = userEvent.setup();
      renderPage("/extensions/smoke.guardrail.enforcement");
      await chooseProject(user, "project-1");
      await screen.findByRole("combobox", { name: "Provider preset" });
      return user;
    }

    it("applies each IMAP preset's host, port and TLS defaults", async () => {
      mockedFetchProjectConnectionsList.mockResolvedValue([]);
      const user = await openPresetForm();
      const imapHost = screen.getByLabelText("IMAP host");
      const imapPort = screen.getByLabelText("IMAP port");

      for (const [preset, host] of [
        ["fastmail", "imap.fastmail.com"],
        ["yahoo", "imap.mail.yahoo.com"],
        ["zoho_personal", "imap.zoho.com"],
        ["zoho_pro", "imappro.zoho.com"],
      ] as const) {
        await chooseManifestOption(user, "Provider preset", PRESET_LABELS[preset]);
        expect(imapHost).toHaveValue(host);
        expect(imapPort).toHaveValue(993);
        expectManifestSelection("TLS mode", "SSL/TLS");
      }
    });

    it("creates an IMAP preset connection with separately persisted secrets", async () => {
      mockedFetchProjectConnectionsList.mockResolvedValueOnce([]);
      const user = await openPresetForm();
      await chooseManifestOption(user, "Provider preset", "Zoho Mail business");
      await user.type(screen.getByLabelText("Username"), "mailbox@example.test");
      await user.type(screen.getByLabelText("Password"), "preset-password");
      await user.click(screen.getByRole("button", { name: "Create connection" }));

      await waitFor(() => {
        expect(mockedCreateProjectConnection).toHaveBeenCalledWith("project-1", {
          connection_kind: "extension",
          connection_type: "imap_mailbox",
          display_name: "Acme Mailbox",
          environment: "",
          extension_id: "smoke.guardrail.enforcement",
          connection_config: {
            provider_preset: "zoho_pro",
            imap_host: "imappro.zoho.com",
            imap_port: 993,
            tls_mode: "ssl",
            username: "mailbox@example.test",
          },
          secret_refs: { refs: { password: "password" } }, // pragma: allowlist secret
          enabled: false,
          tags: ["imap", "email", "mailbox"],
        });
      });
      expect(mockedSaveProjectConnectionSecrets).toHaveBeenCalledWith(
        "project-1",
        "conn_imap_preset",
        { password: "preset-password" }, // pragma: allowlist secret
      );
      await screen.findByText("Preset review mailbox");
    });

    it("reapplies IMAP preset defaults after starting a new connection", async () => {
      const user = await openPresetForm();
      await screen.findByText("Preset review mailbox");
      await user.click(screen.getByRole("button", { name: "Edit" }));
      await user.click(screen.getByRole("button", { name: "New connection" }));
      await chooseManifestOption(user, "Provider preset", "Fastmail");
      expect(screen.getByLabelText("IMAP host")).toHaveValue("imap.fastmail.com");
      await chooseManifestOption(user, "Provider preset", "Yahoo Mail");
      expect(screen.getByLabelText("IMAP host")).toHaveValue("imap.mail.yahoo.com");
    });

    it("preserves saved IMAP overrides and tests by reference without inline secrets", async () => {
      const user = await openPresetForm();
      await screen.findByText("Preset review mailbox");
      await user.click(screen.getByRole("button", { name: "Edit" }));
      await user.clear(screen.getByLabelText("IMAP host"));
      await user.type(screen.getByLabelText("IMAP host"), "mail.custom.example");
      await user.clear(screen.getByLabelText("IMAP port"));
      await user.type(screen.getByLabelText("IMAP port"), "143");
      await chooseManifestOption(user, "TLS mode", "STARTTLS");
      await chooseManifestOption(user, "Provider preset", "Fastmail");
      expect(screen.getByLabelText("IMAP host")).toHaveValue("mail.custom.example");
      expect(screen.getByLabelText("IMAP port")).toHaveValue(143);
      expectManifestSelection("TLS mode", "STARTTLS");
      await user.click(screen.getByRole("button", { name: "Test" }));

      await waitFor(() => {
        expect(mockedExecuteExtensionAction).toHaveBeenCalledWith(
          "smoke.guardrail.enforcement",
          "test_connection",
          {
            component_id: "imap_mailbox_connection",
            context: { project_id: "project-1" },
            input: { connection_ref: "conn_imap_preset" },
            page_id: "configuration",
            target: {
              connection_id: "conn_imap_preset",
              project_id: "",
            },
          },
          expect.any(String),
        );
      });
    });

    it("resets IMAP preset defaults after confirmed deletion of an edited connection", async () => {
      const user = await openPresetForm();
      await screen.findByText("Preset review mailbox");
      await user.click(screen.getByRole("button", { name: "Edit" }));
      await user.clear(screen.getByLabelText("IMAP host"));
      await user.type(screen.getByLabelText("IMAP host"), "mail.custom.example");
      await user.clear(screen.getByLabelText("IMAP port"));
      await user.type(screen.getByLabelText("IMAP port"), "143");
      await chooseManifestOption(user, "TLS mode", "STARTTLS");
      await chooseManifestOption(user, "Provider preset", "Fastmail");
      await user.click(screen.getByRole("button", { name: "Delete" }));
      // Deleting is irreversible, so it waits on the confirmation dialog.
      const deleteDialog = await screen.findByRole("alertdialog");
      expect(deleteDialog).toHaveTextContent("This cannot be undone.");
      await user.click(within(deleteDialog).getByRole("button", { name: "Delete connection" }));
      await waitFor(() => {
        expect(mockedDeleteProjectConnection).toHaveBeenCalledWith("project-1", "conn_imap_preset");
        expectManifestSelection("Provider preset", "Manual setup");
      });
      await chooseManifestOption(user, "Provider preset", "Fastmail");
      expect(screen.getByLabelText("IMAP host")).toHaveValue("imap.fastmail.com");
    });
  });

  it("runs a declarative pre-save action before saving without sending secrets", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(globalThis, "confirm").mockReturnValue(true);
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedExecuteExtensionAction.mockResolvedValueOnce({
      action: {
        status: "succeeded",
        response: {
          ok: true,
          result: {
            server_identity: "SHA256:discoveredIdentity",
            server_identity_type: "ed25519",
          },
        },
      },
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "secure_drop_connection",
                type: "connection_form",
                title: "Secure drop connection",
                data: {
                  connection_kind: "extension",
                  connection_type_field: "transport",
                  default_connection_type: "secure_drop",
                  test_action: "validate_connection",
                  pre_save_actions: [
                    {
                      action_id: "discover_server_identity",
                      status: "Fetching server identity...",
                      run_when: [
                        { path: "transport", eq: "secure_drop" },
                        { path: "server_identity", eq: "" },
                      ],
                      target_connection: {
                        connection_type: "secure_drop",
                        config_fields: [
                          "host",
                          "port",
                          "connect_timeout_seconds",
                          "read_timeout_seconds",
                        ],
                      },
                      values_fields: ["host", "port"],
                      result_mapping: {
                        server_identity: "result.server_identity",
                      },
                      confirmation: {
                        required_result_fields: ["server_identity"],
                        reject_error: "Server identity was not trusted; connection was not saved.",
                        message: "Found {{result.server_identity}} for {{config.host}}.",
                      },
                    },
                  ],
                  connection_types: {
                    secure_drop: {
                      connection_type: "secure_drop",
                      provider: "secure_drop",
                      config_fields: [
                        "host",
                        "port",
                        "server_identity",
                        "connect_timeout_seconds",
                        "read_timeout_seconds",
                      ],
                      secret_fields: ["username", "api_token", "client_certificate"],
                      required_secret_fields: ["username"],
                    },
                  },
                },
                defaults: {
                  transport: "secure_drop",
                  port: 443,
                  connect_timeout_seconds: 15,
                  read_timeout_seconds: 30,
                },
                fields: [
                  {
                    path: "transport",
                    label: "Transport",
                    widget: "select",
                    required: true,
                    options: [{ value: "secure_drop", label: "Secure drop" }],
                  },
                  { path: "host", label: "Server", widget: "text", required: true },
                  { path: "port", label: "Port", widget: "integer", required: true },
                  {
                    path: "connect_timeout_seconds",
                    label: "Connect timeout seconds",
                    widget: "integer",
                  },
                  {
                    path: "read_timeout_seconds",
                    label: "Read timeout seconds",
                    widget: "integer",
                  },
                  { path: "username", label: "Username", widget: "secret", required: true },
                  { path: "api_token", label: "API token", widget: "secret" },
                ],
              },
            ],
          },
        ],
      },
    });

    try {
      renderPage("/extensions/smoke.guardrail.enforcement");

      await chooseProject(user, "project-1");
      expect(screen.queryByLabelText("Server identity")).not.toBeInTheDocument();
      expect(screen.getAllByText("*", { selector: "span" })).toHaveLength(4);
      await user.type(screen.getByLabelText("Server"), "files.example.test");
      await user.clear(screen.getByLabelText("Port"));
      await user.type(screen.getByLabelText("Port"), "443");
      await user.type(screen.getByLabelText("Username"), "drop-user");
      await user.type(screen.getByLabelText("API token"), "drop-token");
      await user.click(screen.getByRole("button", { name: "Create connection" }));

      await waitFor(() => {
        expect(mockedExecuteExtensionAction).toHaveBeenCalledWith(
          "smoke.guardrail.enforcement",
          "discover_server_identity",
          expect.objectContaining({
            target: {
              connection: {
                connection_type_id: "secure_drop",
                config: {
                  host: "files.example.test",
                  port: 443,
                  connect_timeout_seconds: 15,
                  read_timeout_seconds: 30,
                },
              },
            },
          }),
        );
      });
      expect(JSON.stringify(mockedExecuteExtensionAction.mock.calls)).not.toContain(
        "drop-token",
      );
      // The discovered identity is shown for a trust decision, not a native prompt.
      const trustDialog = await screen.findByRole("alertdialog");
      expect(trustDialog).toHaveTextContent("SHA256:discoveredIdentity");
      expect(confirmSpy).not.toHaveBeenCalled();
      await user.click(within(trustDialog).getByRole("button", { name: "Save connection" }));
      await waitFor(() => {
        expect(mockedCreateProjectConnection).toHaveBeenCalledWith(
          "project-1",
          expect.objectContaining({
            connection_type: "secure_drop",
            connection_config: {
              host: "files.example.test",
              port: 443,
              server_identity: "SHA256:discoveredIdentity",
              connect_timeout_seconds: 15,
              read_timeout_seconds: 30,
            },
            secret_refs: { refs: { username: "username", api_token: "api_token" } },
          }),
        );
      });
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it("rejects pre-save result mappings to secret fields", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedExecuteExtensionAction.mockResolvedValueOnce({
      action: {
        status: "succeeded",
        response: {
          ok: true,
          result: {
            api_token: "plaintext-token-from-action",
          },
        },
      },
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "secure_drop_connection",
                type: "connection_form",
                title: "Secure drop connection",
                data: {
                  connection_kind: "extension",
                  connection_type: "secure_drop",
                  provider: "secure_drop",
                  config_fields: ["host", "server_identity"],
                  secret_fields: ["username", "api_token"],
                  required_secret_fields: ["username"],
                  pre_save_actions: [
                    {
                      action_id: "discover_server_identity",
                      result_mapping: {
                        api_token: "result.api_token",
                      },
                    },
                  ],
                },
                fields: [
                  { path: "host", label: "Server", widget: "text", required: true },
                  { path: "server_identity", label: "Server identity", widget: "text" },
                  { path: "username", label: "Username", widget: "secret", required: true },
                  { path: "api_token", label: "API token", widget: "secret" },
                ],
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await user.type(screen.getByLabelText("Server"), "files.example.test");
    await user.type(screen.getByLabelText("Username"), "drop-user");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() => {
      expect(screen.getByText(
        "Pre-save result mapping target 'api_token' must be a declared non-secret config field.",
      )).toBeInTheDocument();
    });
    expect(mockedCreateProjectConnection).not.toHaveBeenCalled();
    expect(JSON.stringify(mockedCreateProjectConnection.mock.calls)).not.toContain(
      "plaintext-token-from-action",
    );
  });

  function preSaveRejectionManifest(failureMessage?: string) {
    return {
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "secure_drop_connection",
                type: "connection_form",
                title: "Secure drop connection",
                data: {
                  connection_kind: "extension",
                  connection_type: "secure_drop",
                  provider: "secure_drop",
                  config_fields: ["host"],
                  secret_fields: [],
                  pre_save_actions: [
                    {
                      action_id: "validate_connection_settings",
                      ...(failureMessage ? { failure_message: failureMessage } : {}),
                    },
                  ],
                },
                fields: [{ path: "host", label: "Server", widget: "text", required: true }],
              },
            ],
          },
        ],
      },
    };
  }

  async function submitRejectedPreSave(manifest: unknown) {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue(manifest);
    renderPage("/extensions/smoke.guardrail.enforcement");
    await chooseProject(user, "project-1");
    await user.type(screen.getByLabelText("Server"), "http://files.example.test");
    await user.click(screen.getByRole("button", { name: "Create connection" }));
  }

  it("shows the extension's own explanation when a pre-save check refuses the form", async () => {
    // The host rejects a failed action at the HTTP layer, so before this the user
    // was told only "Extension action failed" and had nothing to correct.
    mockedExecuteExtensionAction.mockRejectedValueOnce(new HttpError({
      status: 422,
      url: "/api/extensions/smoke.guardrail.enforcement/action/validate_connection_settings",
      payload: {
        message: "Extension action failed",
        action: {
          status: "failed",
          detail: "/private/runtime/provider.py:42",
          response: { ok: false, error: "The site address must start with https://", error_code: "invalid_configuration" },
        },
      },
    }));

    await submitRejectedPreSave(preSaveRejectionManifest("Check the site address."));

    await waitFor(() => {
      expect(screen.getByText(
        "The site address must start with https:// (invalid_configuration)",
      )).toBeInTheDocument();
    });
    expect(screen.queryByText("Extension action failed")).not.toBeInTheDocument();
    expect(mockedCreateProjectConnection).not.toHaveBeenCalled();
  });

  it("falls back to the configured failure message when the response explains nothing", async () => {
    mockedExecuteExtensionAction.mockRejectedValueOnce(new HttpError({
      status: 500,
      url: "/api/extensions/smoke.guardrail.enforcement/action/validate_connection_settings",
      payload: {
        message: "Extension action failed",
        action: { status: "failed", detail: "/private/runtime/provider.py:42", response: {} },
      },
    }));

    await submitRejectedPreSave(
      preSaveRejectionManifest("Acme Shop needs both keys, or neither."),
    );

    await waitFor(() => {
      expect(screen.getByText("Acme Shop needs both keys, or neither.")).toBeInTheDocument();
    });
    expect(screen.queryByText("Extension action failed")).not.toBeInTheDocument();
    expect(screen.queryByText("/private/runtime/provider.py:42")).not.toBeInTheDocument();
  });

  it("names the action when neither the response nor the manifest explains the failure", async () => {
    mockedExecuteExtensionAction.mockRejectedValueOnce(new HttpError({
      status: 500,
      url: "/api/extensions/smoke.guardrail.enforcement/action/validate_connection_settings",
      payload: { message: "Extension action failed" },
    }));

    await submitRejectedPreSave(preSaveRejectionManifest());

    await waitFor(() => {
      expect(screen.getByText(/failed\.$/)).toBeInTheDocument();
    });
  });

  it("marks the required field itself when an empty form is submitted", async () => {
    // The message used to appear only at the top of the form, leaving the user to
    // work out which control it referred to.
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue(preSaveRejectionManifest());
    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    const control = await screen.findByLabelText("Server");
    await waitFor(() => {
      expect(control).toHaveAttribute("aria-invalid", "true");
    });
    expect(control).toHaveAccessibleDescription("Server is required.");
    expect(control).toHaveFocus();
    expect(mockedCreateProjectConnection).not.toHaveBeenCalled();
  });

  it("clears the field error as soon as the named field is edited", async () => {
    // Leaving the message up while the user fixes the field keeps reporting a
    // problem they have already dealt with.
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue(preSaveRejectionManifest());
    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    const control = await screen.findByLabelText("Server");
    await waitFor(() => {
      expect(control).toHaveAttribute("aria-invalid", "true");
    });

    await user.type(control, "h");

    await waitFor(() => {
      expect(control).not.toHaveAttribute("aria-invalid");
    });
    expect(screen.queryByText("Server is required.")).not.toBeInTheDocument();
  });

  it("keeps a field error while an unrelated field is edited", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "secure_drop_connection",
                type: "connection_form",
                title: "Secure drop connection",
                data: {
                  connection_kind: "extension",
                  connection_type: "secure_drop",
                  provider: "secure_drop",
                  config_fields: ["host", "note"],
                  secret_fields: [],
                },
                fields: [
                  { path: "host", label: "Server", widget: "text", required: true },
                  { path: "note", label: "Note", widget: "text" },
                ],
              },
            ],
          },
        ],
      },
    });
    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    const control = await screen.findByLabelText("Server");
    await waitFor(() => {
      expect(control).toHaveAttribute("aria-invalid", "true");
    });

    await user.type(screen.getByLabelText("Note"), "anything");

    expect(control).toHaveAttribute("aria-invalid", "true");
  });

  it("clears the field error state once the connection saves", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue(preSaveRejectionManifest());
    mockedExecuteExtensionAction.mockResolvedValue({
      action: { status: "succeeded", response: { ok: true, result: {} } },
    });
    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    const control = await screen.findByLabelText("Server");
    await waitFor(() => {
      expect(control).toHaveAttribute("aria-invalid", "true");
    });

    await user.type(control, "https://files.example.test");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() => {
      expect(control).not.toHaveAttribute("aria-invalid");
    });
  });

  it("tests saved connections by id without inline secrets", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchProjectConnectionsList.mockResolvedValue([
      {
        connection_id: "conn_secure_drop",
        extension_id: "smoke.guardrail.enforcement",
        connection_type: "secure_drop",
        display_name: "Production Secure Drop",
        environment: "prod",
        connection_config: { host: "files.example.test", port: 443 },
        secret_refs: {
          refs: {
            username: "project_connection:conn_secure_drop:username",
            client_certificate: "project_connection:conn_secure_drop:client_certificate",
          },
        },
      },
    ]);
    mockedExecuteExtensionAction.mockRejectedValueOnce(new HttpError({
      status: 502,
      url: "/api/extensions/smoke.guardrail.enforcement/action/validate_connection",
      payload: {
        message: "Extension action failed",
        action: {
          response: {
            error: "Connection type must be secure_drop",
            error_code: "invalid_connection",
          },
        },
      },
    }));
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      manifest_version: 2,
      action_definitions: [
        {
          action_id: "validate_connection",
          parameters: [
            { name: "connection_ref", value_type: "connection_ref", required: true },
          ],
        },
      ],
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "secure_drop_connection",
                type: "connection_form",
                title: "Secure drop connection",
                data: {
                  connection_kind: "extension",
                  connection_type: "secure_drop",
                  provider: "secure_drop",
                  test_action: "validate_connection",
                  config_fields: ["host", "port"],
                  secret_fields: ["username", "client_certificate"],
                },
                fields: [
                  { path: "host", label: "Host", widget: "text", required: true },
                  { path: "port", label: "Port", widget: "integer", minimum: 1, maximum: 65535, step: 1 },
                  { path: "username", label: "Username", widget: "secret", required: true },
                  { path: "client_certificate", label: "Client certificate", widget: "textarea", secret: true, props: { rows: 8 } },
                ],
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await screen.findByText("Production Secure Drop");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => {
      expect(mockedExecuteExtensionAction).toHaveBeenCalledWith(
        "smoke.guardrail.enforcement",
        "validate_connection",
        expect.objectContaining({
          context: { project_id: "project-1" },
          target: {
            connection_id: "conn_secure_drop",
            project_id: "",
          },
        }),
        expect.any(String),
      );
    });
    const payload = mockedExecuteExtensionAction.mock.calls[0]?.[2] as Record<string, unknown>;
    expect(payload).toEqual({
      component_id: "secure_drop_connection",
      context: { project_id: "project-1" },
      input: { connection_ref: "conn_secure_drop" },
      page_id: "configuration",
      target: {
        connection_id: "conn_secure_drop",
        project_id: "",
      },
    });
    expect(JSON.stringify(mockedExecuteExtensionAction.mock.calls)).not.toContain("BEGIN KEY");
    await waitFor(() => {
      expect(screen.getByText("Connection type must be secure_drop (invalid_connection)")).toBeInTheDocument();
    });
  });

  it("shows a visible success message when a saved connection test passes", async () => {
    const user = userEvent.setup();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedFetchProjectConnectionsList.mockResolvedValue([
      {
        connection_id: "conn_secure_drop",
        extension_id: "smoke.guardrail.enforcement",
        connection_type: "secure_drop",
        display_name: "Production Secure Drop",
        environment: "prod",
        connection_config: { host: "files.example.test", port: 443 },
        secret_refs: {
          refs: {
            username: "project_connection:conn_secure_drop:username",
            client_certificate: "project_connection:conn_secure_drop:client_certificate",
          },
        },
      },
    ]);
    mockedExecuteExtensionAction.mockResolvedValueOnce({
      action: {
        status: "succeeded",
        response: {
          ok: true,
          result: { status: "ok" },
        },
      },
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      account_scope: { scope_type: "project" },
      ui_manifest: {
        pages: [
          {
            page_id: "configuration",
            title: "Configuration",
            components: [
              {
                component_id: "secure_drop_connection",
                type: "connection_form",
                title: "Secure drop connection",
                data: {
                  connection_kind: "extension",
                  connection_type: "secure_drop",
                  provider: "secure_drop",
                  test_action: "validate_connection",
                  config_fields: ["host", "port"],
                  secret_fields: ["username", "client_certificate"],
                },
                fields: [
                  { path: "host", label: "Host", widget: "text", required: true },
                  { path: "port", label: "Port", widget: "integer", minimum: 1, maximum: 65535, step: 1 },
                  { path: "username", label: "Username", widget: "secret", required: true },
                  { path: "client_certificate", label: "Client certificate", widget: "textarea", secret: true, props: { rows: 8 } },
                ],
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");

    await chooseProject(user, "project-1");
    await screen.findByText("Production Secure Drop");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => {
      expect(mockedExecuteExtensionAction).toHaveBeenCalledWith(
        "smoke.guardrail.enforcement",
        "validate_connection",
        expect.objectContaining({
          context: { project_id: "project-1" },
          target: {
            connection_id: "conn_secure_drop",
            project_id: "",
          },
        }),
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Connection test successful.");
    });
  });

  it("renders a redacted structured result for manifest actions", async () => {
    const user = userEvent.setup();
    mockedExecuteExtensionAction.mockResolvedValueOnce({
      action: {
        status: "succeeded",
        response: {
          ok: true,
          warnings: ["One record was skipped"],
          result: {
            processed: 2,
            api_token: "must-not-render",
          },
        },
      },
    });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      ui_manifest: {
        pages: [
          {
            page_id: "operations",
            title: "Operations",
            components: [
              {
                component_id: "score_records",
                type: "form",
                title: "Score records",
                ui_schema: {
                  actions: {
                    score_records: { action_id: "score_records", label: "Score Records" },
                  },
                },
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");

    await user.click(await screen.findByRole("button", { name: "Score Records" }));

    const result = await screen.findByRole("region", { name: "Score Records result" });
    expect(result).toHaveTextContent("succeeded");
    expect(result).toHaveTextContent("processed");
    expect(result).toHaveTextContent("2");
    expect(result).toHaveTextContent("One record was skipped");
    expect(result).toHaveTextContent("[REDACTED]");
    expect(result).not.toHaveTextContent("must-not-render");

    mockedExecuteExtensionAction.mockResolvedValueOnce({
      action: {
        status: "succeeded",
        response: {
          ok: false,
          errors: [{ code: "record_rejected", message: "Record two was rejected" }],
          result: { processed: 1, rejected: 1 },
        },
      },
    });
    await user.click(screen.getByRole("button", { name: "Score Records" }));
    await waitFor(() => {
      expect(result).toHaveTextContent("record_rejected");
    });
    expect(result).toHaveTextContent("failed");
    expect(result).toHaveTextContent("Record two was rejected");

    mockedExecuteExtensionAction.mockRejectedValueOnce(new HttpError({
      status: 502,
      url: "/api/extensions/smoke.guardrail.enforcement/action/score_records",
      payload: {
        error: {
          code: "upstream_unavailable",
          message: "Scoring provider is unavailable",
          credential: "must-not-render-either",
        },
      },
    }));
    await user.click(screen.getByRole("button", { name: "Score Records" }));
    await waitFor(() => {
      expect(result).toHaveTextContent("upstream_unavailable");
    });
    expect(result).toHaveTextContent("Scoring provider is unavailable");
    expect(result).toHaveTextContent("[REDACTED]");
    expect(result).not.toHaveTextContent("must-not-render-either");
  });

  it("reuses one logical action identity after ambiguity and rotates it after success", async () => {
    const user = userEvent.setup();
    const succeeded = {
      action: {
        status: "succeeded",
        response: { ok: true, result: { processed: 1 } },
      },
    };
    mockedExecuteExtensionAction
      .mockRejectedValueOnce(new HttpError({
        status: 502,
        url: "/api/extensions/smoke.guardrail.enforcement/action/score_records",
        payload: {
          action: {
            status: "failed",
            detail: "external_effect_timeout_unknown",
            response: {
              ok: false,
              error_code: "external_effect_timeout_unknown",
              external_effect_status: "timeout_unknown",
              definitely_no_external_effect: true,
            },
          },
        },
      }))
      .mockResolvedValueOnce(succeeded)
      .mockResolvedValueOnce(succeeded);
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      ui_manifest: {
        pages: [
          {
            page_id: "operations",
            title: "Operations",
            components: [
              {
                component_id: "score_records",
                type: "form",
                title: "Score records",
                ui_schema: {
                  actions: {
                    score_records: { action_id: "score_records", label: "Score Records" },
                  },
                },
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    const actionButton = await screen.findByRole("button", { name: "Score Records" });

    await user.click(actionButton);
    expect(await screen.findByText(
      "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.",
    )).toBeVisible();

    await user.click(actionButton);
    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2));
    await user.click(actionButton);
    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(3));

    const firstKey = mockedExecuteExtensionAction.mock.calls[0]?.[3];
    const retryKey = mockedExecuteExtensionAction.mock.calls[1]?.[3];
    const nextIntentKey = mockedExecuteExtensionAction.mock.calls[2]?.[3];
    expect(firstKey).toBeTruthy();
    expect(retryKey).toBe(firstKey);
    expect(nextIntentKey).toBeTruthy();
    expect(nextIntentKey).not.toBe(firstKey);
    expect(JSON.stringify(mockedExecuteExtensionAction.mock.calls)).not.toContain("must-not-store");
  });

  it.each([408, 502, 503, 504])(
    "retains one logical action identity after an untyped gateway HTTP %s",
    async (status) => {
      const user = userEvent.setup();
      mockedExecuteExtensionAction
        .mockRejectedValueOnce(new HttpError({
          status,
          url: "/api/extensions/smoke.guardrail.enforcement/action/score_records",
          payload: { detail: "Gateway could not confirm the action outcome" },
        }))
        .mockResolvedValueOnce({
          action: { status: "succeeded", response: { ok: true, result: {} } },
        });
      mockedFetchExtensionCompiledManifest.mockResolvedValue({
        ui_manifest: {
          pages: [
            {
              page_id: "operations",
              title: "Operations",
              components: [
                {
                  component_id: "score_records",
                  type: "form",
                  title: "Score records",
                  ui_schema: {
                    actions: {
                      score_records: { action_id: "score_records", label: "Score Records" },
                    },
                  },
                },
              ],
            },
          ],
        },
      });

      renderPage("/extensions/smoke.guardrail.enforcement");
      const actionButton = await screen.findByRole("button", { name: "Score Records" });
      await user.click(actionButton);
      expect(await screen.findByText(
        "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.",
      )).toBeVisible();
      await user.click(actionButton);
      await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2));

      expect(mockedExecuteExtensionAction.mock.calls[1]?.[3]).toBe(
        mockedExecuteExtensionAction.mock.calls[0]?.[3],
      );
    },
  );

  it("treats bot_token_required HTTP 400 as definitive", async () => {
    const user = userEvent.setup();
    mockedExecuteExtensionAction
      .mockRejectedValueOnce(new HttpError({
        status: 400,
        url: "/api/extensions/smoke.guardrail.enforcement/action/verify_bot",
        payload: {
          status: "error",
          code: "bad_gateway",
          message: "Extension action failed",
          details: {},
          correlation_id: "corr-typed-failure",
          error: "Extension action failed",
          action: {
            status: "failed",
            detail: "Extension action failed",
            response: {
              ok: false,
              error_code: "bot_token_required",
              error: "Save a bot token on the Setup page before verifying.",
            },
          },
        },
      }))
      .mockResolvedValueOnce({
        action: { status: "succeeded", response: { ok: true, result: {} } },
      });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      ui_manifest: {
        pages: [{
          page_id: "setup",
          title: "Setup",
          components: [{
            component_id: "verify_bot",
            type: "form",
            title: "Acme Notifier",
            ui_schema: {
              actions: {
                verify_bot: { action_id: "verify_bot", label: "Verify" },
              },
            },
          }],
        }],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    const actionButton = await screen.findByRole("button", { name: "Verify" });
    await user.click(actionButton);

    expect(
      await screen.findByText("Save a bot token on the Setup page before verifying."),
    ).toBeVisible();
    expect(screen.queryByText(
      "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.",
    )).not.toBeInTheDocument();

    await user.click(actionButton);
    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2));
    expect(mockedExecuteExtensionAction.mock.calls[1]?.[3]).not.toBe(
      mockedExecuteExtensionAction.mock.calls[0]?.[3],
    );
  });

  it.each([
    ["missing", true],
    ["response_false", true],
    ["response", false],
    ["result", false],
  ] as const)(
    "requires an explicit no-effect marker for typed gateway failure (marker=%s)",
    async (markerLocation, expectedAmbiguous) => {
      const user = userEvent.setup();
      mockedExecuteExtensionAction
        .mockRejectedValueOnce(new HttpError({
          status: 502,
          url: "/api/extensions/smoke.guardrail.enforcement/action/publish",
          payload: {
            action: {
              status: "failed",
              detail: "Extension action failed",
              response: {
                ok: false,
                error_code: "upstream_timeout",
                error: "The upstream request timed out.",
                ...(markerLocation === "response" || markerLocation === "response_false"
                  ? { definitely_no_external_effect: markerLocation === "response" }
                  : {}),
                ...(markerLocation === "result"
                  ? { result: { definitely_no_external_effect: true } }
                  : {}),
              },
            },
          },
        }))
        .mockResolvedValueOnce({
          action: { status: "succeeded", response: { ok: true, result: {} } },
        });
      mockedFetchExtensionCompiledManifest.mockResolvedValue({
        ui_manifest: {
          pages: [{
            page_id: "operations",
            title: "Operations",
            components: [{
              component_id: "publish",
              type: "form",
              title: "Publish",
              ui_schema: {
                actions: { publish: { action_id: "publish", label: "Publish" } },
              },
            }],
          }],
        },
      });

      renderPage("/extensions/smoke.guardrail.enforcement");
      const actionButton = await screen.findByRole("button", { name: "Publish" });
      await user.click(actionButton);
      const ambiguity = screen.queryByText(
        "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.",
      );
      if (expectedAmbiguous) {
        expect(ambiguity).toBeVisible();
      } else {
        expect(ambiguity).not.toBeInTheDocument();
      }

      await user.click(actionButton);
      await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2));
      const firstKey = mockedExecuteExtensionAction.mock.calls[0]?.[3];
      const retryKey = mockedExecuteExtensionAction.mock.calls[1]?.[3];
      if (expectedAmbiguous) {
        expect(retryKey).toBe(firstKey);
      } else {
        expect(retryKey).not.toBe(firstKey);
      }
    },
  );

  it("shows a deterministic provider rejection from a projected HTTP 502", async () => {
    const user = userEvent.setup();
    const providerMessage =
      "Acme Notifier rejected polling because another bot instance is already polling.";
    mockedExecuteExtensionAction
      .mockRejectedValueOnce(new HttpError({
        status: 502,
        url: "/api/extensions/smoke.guardrail.enforcement/action/refresh_discovered_chats",
        payload: {
          action: {
            status: "failed",
            detail: "polling_unavailable",
            response: {
              ok: false,
              error_code: "polling_unavailable",
              error: providerMessage,
              definitely_no_external_effect: true,
            },
          },
        },
      }))
      .mockResolvedValueOnce({
        action: { status: "succeeded", response: { ok: true, result: {} } },
      });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      ui_manifest: {
        pages: [{
          page_id: "operations",
          title: "Operations",
          components: [{
            component_id: "refresh_discovered_chats",
            type: "form",
            title: "Acme Notifier discovery",
            ui_schema: {
              actions: {
                refresh_discovered_chats: {
                  action_id: "refresh_discovered_chats",
                  label: "Refresh Discovered Chats",
                },
              },
            },
          }],
        }],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    const actionButton = await screen.findByRole("button", {
      name: "Refresh Discovered Chats",
    });
    await user.click(actionButton);

    expect(await screen.findByText(providerMessage)).toBeVisible();
    expect(screen.queryByText(
      "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.",
    )).not.toBeInTheDocument();

    await user.click(actionButton);
    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2));
    expect(mockedExecuteExtensionAction.mock.calls[1]?.[3]).not.toBe(
      mockedExecuteExtensionAction.mock.calls[0]?.[3],
    );
  });

  it.each(["host_mutation_failed", "response_envelope_processing_failed"])(
    "keeps an unmarked internal %s failure ambiguous",
    async (errorCode) => {
      const user = userEvent.setup();
      mockedExecuteExtensionAction
        .mockRejectedValueOnce(new HttpError({
          status: 502,
          url: "/api/extensions/smoke.guardrail.enforcement/action/save_settings",
          payload: {
            action: {
              status: "failed",
              detail: errorCode,
              response: { ok: false, error_code: errorCode },
            },
          },
        }))
        .mockResolvedValueOnce({
          action: { status: "succeeded", response: { ok: true, result: {} } },
        });
      mockedFetchExtensionCompiledManifest.mockResolvedValue({
        ui_manifest: {
          pages: [{
            page_id: "settings",
            title: "Settings",
            components: [{
              component_id: "save_settings",
              type: "form",
              title: "Settings",
              ui_schema: {
                actions: {
                  save_settings: { action_id: "save_settings", label: "Save Settings" },
                },
              },
            }],
          }],
        },
      });

      renderPage("/extensions/smoke.guardrail.enforcement");
      const actionButton = await screen.findByRole("button", { name: "Save Settings" });
      await user.click(actionButton);
      expect(await screen.findByText(
        "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.",
      )).toBeVisible();

      await user.click(actionButton);
      await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2));
      expect(mockedExecuteExtensionAction.mock.calls[1]?.[3]).toBe(
        mockedExecuteExtensionAction.mock.calls[0]?.[3],
      );
    },
  );

  it("clears an uncertain action identity after a changed-request conflict", async () => {
    const user = userEvent.setup();
    mockedExecuteExtensionAction
      .mockRejectedValueOnce(new TypeError("network response was lost"))
      .mockRejectedValueOnce(new HttpError({
        status: 409,
        url: "/api/extensions/smoke.guardrail.enforcement/action/score_records",
        payload: {
          error: "external_effect_idempotency_conflict",
        },
      }))
      .mockResolvedValueOnce({
        action: { status: "succeeded", response: { ok: true, result: {} } },
      });
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      ui_manifest: {
        pages: [
          {
            page_id: "operations",
            title: "Operations",
            components: [
              {
                component_id: "score_records",
                type: "form",
                title: "Score records",
                ui_schema: {
                  fields: [{ path: "record", label: "Record", widget: "text" }],
                  actions: {
                    score_records: { action_id: "score_records", label: "Score Records" },
                  },
                },
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    const actionButton = await screen.findByRole("button", { name: "Score Records" });
    const record = screen.getByLabelText("Record");
    await user.type(record, "first");
    await user.click(actionButton);
    await screen.findByText(
      "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.",
    );

    await user.clear(record);
    await user.type(record, "changed");
    await user.click(actionButton);
    expect(await screen.findByText(
      "This action changed after an uncertain result. It was not sent again. Check the provider status, then submit the changed request as a new action.",
    )).toBeVisible();
    expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2);

    await user.click(actionButton);
    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(3));
    const firstKey = mockedExecuteExtensionAction.mock.calls[0]?.[3];
    expect(mockedExecuteExtensionAction.mock.calls[1]?.[3]).toBe(firstKey);
    expect(mockedExecuteExtensionAction.mock.calls[2]?.[3]).not.toBe(firstKey);
  });

  it("retains action identity after replay persistence uncertainty but clears pre-spawn failure", async () => {
    const user = userEvent.setup();
    const succeeded = {
      action: { status: "succeeded", response: { ok: true, result: {} } },
    };
    mockedExecuteExtensionAction
      .mockRejectedValueOnce(new HttpError({
        status: 502,
        url: "/api/extensions/smoke.guardrail.enforcement/action/score_records",
        payload: {
          action: {
            status: "failed",
            detail: "external_effect_replay_persist_failed",
            response: {
              ok: false,
              error_code: "external_effect_replay_persist_failed",
            },
          },
        },
      }))
      .mockResolvedValueOnce(succeeded)
      .mockRejectedValueOnce(new HttpError({
        status: 503,
        url: "/api/extensions/smoke.guardrail.enforcement/action/score_records",
        payload: {
          action: {
            status: "failed",
            detail: "external_effect_pre_spawn_setup_failed",
            response: {
              ok: false,
              error_code: "external_effect_pre_spawn_setup_failed",
              external_effect_status: "failed",
            },
          },
        },
      }))
      .mockResolvedValueOnce(succeeded);
    mockedFetchExtensionCompiledManifest.mockResolvedValue({
      ui_manifest: {
        pages: [
          {
            page_id: "operations",
            title: "Operations",
            components: [
              {
                component_id: "score_records",
                type: "form",
                title: "Score records",
                ui_schema: {
                  actions: {
                    score_records: { action_id: "score_records", label: "Score Records" },
                  },
                },
              },
            ],
          },
        ],
      },
    });

    renderPage("/extensions/smoke.guardrail.enforcement");
    const actionButton = await screen.findByRole("button", { name: "Score Records" });

    await user.click(actionButton);
    await screen.findByText(
      "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.",
    );
    await user.click(actionButton);
    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(2));
    const replayFailureKey = mockedExecuteExtensionAction.mock.calls[0]?.[3];
    expect(mockedExecuteExtensionAction.mock.calls[1]?.[3]).toBe(replayFailureKey);

    await user.click(actionButton);
    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(3));
    await user.click(actionButton);
    await waitFor(() => expect(mockedExecuteExtensionAction).toHaveBeenCalledTimes(4));
    const preSpawnFailureKey = mockedExecuteExtensionAction.mock.calls[2]?.[3];
    expect(preSpawnFailureKey).not.toBe(replayFailureKey);
    expect(mockedExecuteExtensionAction.mock.calls[3]?.[3]).not.toBe(preSpawnFailureKey);
  });
});
