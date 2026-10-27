import { createElement, useEffect } from "react";
import userEvent from "@testing-library/user-event";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, configure, fireEvent, getConfig, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { AppShell } from "@/components/layout/AppShell";
import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import { AuthContext } from "@/lib/auth/context";
import { hasPendingRequests } from "@/lib/http/request-activity";
import { ProjectsListPage } from "@/pages/projects/list";
import { RegistryWorkspacesPage } from "@/pages/registry/workspaces";
import {
  approvalJobSubtitle,
  shouldRefreshApprovalsFromEvent,
} from "@/components/layout/app-shell-helpers";
import { useAuth } from "@/lib/auth/useAuth";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { WORKSPACES_CHANGED_EVENT } from "@/lib/utils/workspace-selection";

vi.mock("@/lib/auth/useAuth", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/auth/use-is-admin", () => ({
  useIsAdmin: vi.fn(() => true),
}));

const mockedUseAuth = vi.mocked(useAuth);
const mockedUseIsAdmin = vi.mocked(useIsAdmin);

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;
  readonly url: string;
  readonly withCredentials: boolean;

  constructor(url: string, init?: EventSourceInit) {
    this.url = url;
    this.withCredentials = Boolean(init?.withCredentials);
    FakeEventSource.instances.push(this);
  }

  close() {
    // Test double.
  }

  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

function createDeferred<T>() {
  let reject!: (reason?: unknown) => void;
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

type SessionMembership = {
  account_id: string;
  account_name: string;
  role: string;
  is_active?: boolean;
  is_default?: boolean;
};

function mockAuthenticatedSession({
  switchAccount,
  sessionOverrides = {},
  userMemberships = [],
  sessionMemberships = [],
}: {
  switchAccount: (accountId: string) => Promise<void>;
  sessionOverrides?: Record<string, unknown>;
  userMemberships?: SessionMembership[];
  sessionMemberships?: SessionMembership[];
}) {
  mockedUseAuth.mockReturnValue({
    error: null,
    isAuthenticated: true,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount,
    status: "authenticated",
    session: {
      authenticated: true,
      local_auth_enabled: true,
      bootstrap_required: false,
      external_auth_enabled: false,
      auth_source: "local",
      roles: ["admin"],
      permissions: {
        can_read: true,
        can_mutate: true,
      },
      account_memberships: sessionMemberships,
      user: {
        id: "7",
        email: "owner@example.com",
        username: "owner",
        display_name: "Owner",
        roles: ["admin"],
        account_id: "acct-primary",
        account_ids: ["acct-primary", "acct-ops"],
        account_memberships: userMemberships,
      },
      i18n: {
        locale: "en",
        fallback_locale: "en",
        supported_locales: [{ code: "en", label: "English", native_label: "English" }],
        messages: {},
      },
      feature_flags: {},
      environment: {
        edition: "community",
      },
      settings_pages: [],
      registered_pages: [],
      shell_navigation: {},
      ...sessionOverrides,
    },
  });
}

async function renderShell(initialEntry = "/") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  const view = render(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(
        MemoryRouter,
        { initialEntries: [initialEntry] },
        createElement(AppShell, null, createElement("div", null, "content")),
      ),
    ),
  );
  await waitFor(() => {
    const fetchMock = vi.mocked(globalThis.fetch);
    const requestedUrls = fetchMock.mock.calls.map(([input]) => stringFromUnknownValue(input));
    expect(requestedUrls).toEqual(
      expect.arrayContaining([
        "/api/workspaces",
        "/api/jobs/awaiting-approval",
        "/api/system/maintenance",
      ]),
    );
  });
  return view;
}

describe("AppShell", () => {
  beforeEach(() => {
    mockedUseAuth.mockReset();
    mockedUseIsAdmin.mockReset();
    mockedUseIsAdmin.mockReturnValue(true);
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.classList.remove("dark", "platform-light");
    const storage = globalThis.localStorage as Partial<Storage> | undefined;
    if (storage && typeof storage.removeItem === "function") {
      storage.removeItem("platform.ui_theme");
      storage.removeItem("platform-theme");
      storage.removeItem("platform:selected-workspace-id");
      storage.removeItem("fsb-rail");
    }
    Object.defineProperty(globalThis, "innerWidth", {
      configurable: true,
      value: 1024,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = stringFromUnknownValue(input);
        if (url.startsWith("/api/workspaces")) {
          return new Response(JSON.stringify({ workspaces: [] }), {
            headers: { "content-type": "application/json" },
            status: 200,
          });
        }
        if (url.startsWith("/api/jobs/awaiting-approval")) {
          return new Response(JSON.stringify({ jobs: [] }), {
            headers: { "content-type": "application/json" },
            status: 200,
          });
        }
        return new Response(JSON.stringify({ groups: [] }), {
          headers: { "content-type": "application/json" },
          status: 200,
        });
      }) as unknown as typeof fetch,
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    delete (
      globalThis as typeof globalThis & {
        __FS_GLOBAL_SEARCH?: unknown;
      }
    ).__FS_GLOBAL_SEARCH;
  });

  it.each([true, false])("refreshes the mounted workspace picker only after an actual successful create (%s)", async (createSucceeds) => {
    mockAuthenticatedSession({ switchAccount: vi.fn(async () => undefined) });
    const auth = mockedUseAuth.getMockImplementation()?.();
    if (!auth) throw new Error("Authenticated fixture missing");
    localStorage.setItem("platform:selected-workspace-id", "ws-1");
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const original = {
      id: "ws-1", workspace_key: "default", display_name: "Default workspace",
      description: "", projects_count: 0, jobs_count: 0, status: "active",
      metadata_jsonb: {}, deleted_at: "", updated_at: "2026-10-20T06:00:00Z",
    };
    const created = { ...original, id: "ws-created", workspace_key: "created", display_name: "Created workspace" };
    const inventory = [original];
    const pending: Array<ReturnType<typeof createDeferred<Response>>> = [];
    const submitted: unknown[] = [];
    let holdWorkspaceReads = false;
    let mounts = 0;
    let unmounts = 0;
    const json = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
      status, headers: { "content-type": "application/json" },
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(stringFromUnknownValue(input), "http://localhost").pathname;
      if (path === "/api/workspaces" && init?.method === "POST") {
        submitted.push(JSON.parse(String(init.body)));
        if (!createSucceeds) return json({ message: "Workspace creation refused" }, 409);
        inventory.push(created);
        holdWorkspaceReads = true;
        return json(created);
      }
      if (path === "/api/workspaces") {
        if (!holdWorkspaceReads) return json({ workspaces: inventory });
        const response = createDeferred<Response>();
        pending.push(response);
        return response.promise;
      }
      if (path === "/api/jobs/awaiting-approval") return json({ jobs: [] });
      if (path === "/api/system/maintenance") return json({ status: "disabled", activated_at: null, owner_kind: null });
      throw new Error(`Unexpected request ${path}`);
    }));
    function RegistryProbe() {
      useEffect(() => { mounts += 1; return () => { unmounts += 1; }; }, []);
      return <RegistryWorkspacesPage />;
    }
    const view = render(
      <AuthContext.Provider value={auth}>
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={["/registry/workspaces"]}>
            <AppShell><RegistryProbe /></AppShell>
          </MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
    try {
      const user = userEvent.setup();
      await waitFor(() => expect(screen.getByRole("button", { name: "Workspace" })).toHaveTextContent("Default workspace"));
      await user.click(screen.getByRole("button", { name: "New workspace" }));
      await user.type(screen.getByRole("textbox", { name: "Workspace name" }), "Created workspace");
      await user.click(screen.getByRole("button", { name: "Create" }));
      await waitFor(() => expect(submitted).toEqual([{
        workspace_key: "", display_name: "Created workspace", description: "",
      }]));
      if (createSucceeds) {
        await screen.findByText("Workspace created.");
        const search = screen.getByRole("searchbox", { name: "Search workspaces" });
        await user.type(search, "Created");
        expect(search).toHaveFocus();
        await act(async () => {
          holdWorkspaceReads = false;
          for (const response of pending.splice(0)) response.resolve(json({ workspaces: inventory }));
        });
        await screen.findByRole("link", { name: "Created workspace" });
        expect(screen.getByRole("searchbox", { name: "Search workspaces" })).toBe(search);
        expect(search).toHaveValue("Created");
        expect(search).toHaveFocus();
      } else {
        expect(await screen.findByRole("alert")).toHaveTextContent("Workspace creation refused");
        await user.click(screen.getByRole("button", { name: "Cancel" }));
      }
      expect(mounts).toBe(1);
      expect(unmounts).toBe(0);
      expect(localStorage.getItem("platform:selected-workspace-id")).toBe("ws-1");
      await user.click(screen.getByRole("button", { name: "Workspace" }));
      expect(screen.getByRole("menuitemradio", { name: "Default workspace" })).toBeChecked();
      if (createSucceeds) {
        expect(await screen.findByRole("menuitemradio", { name: "Created workspace" })).not.toBeChecked();
      } else {
        expect(screen.queryByRole("menuitemradio", { name: "Created workspace" })).not.toBeInTheDocument();
      }
    } finally {
      view.unmount();
      for (const response of pending) response.resolve(json({ workspaces: inventory }));
      client.clear();
    }
  });

  it("keeps an actual New project draft focused and mounted during 15s and 30s status refreshes", async () => {
    vi.useFakeTimers();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mockAuthenticatedSession({ switchAccount: vi.fn(async () => undefined) });
    const auth = mockedUseAuth.getMockImplementation()?.();
    if (!auth) throw new Error("Authenticated fixture missing");
    localStorage.setItem("platform:selected-workspace-id", "ws-1");
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
    let polling = false;
    let mounts = 0;
    let unmounts = 0;
    const pending: Array<{ path: string; response: ReturnType<typeof createDeferred<Response>> }> = [];
    const requested: string[] = [];
    const json = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
      status, headers: { "content-type": "application/json" },
    });
    const maintenance = (status = "disabled") => ({ status, activated_at: null, owner_kind: status === "disabled" ? null : "manual" });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(stringFromUnknownValue(input), "http://localhost").pathname;
      requested.push(path);
      if (path === "/api/workspaces") return json({ workspaces: [{
        id: "ws-1", account_id: "acct-primary", tenant_id: "acct-primary", workspace_key: "demo",
        display_name: "Demo workspace", description: "", status: "active", metadata_jsonb: {}, projects_count: 0, jobs_count: 0,
      }] });
      if (path === "/api/v2/projects") return json({ projects: [], page: 1, per_page: 25, total: 0, total_pages: 1 });
      if (path === "/api/workflows") return json([]);
      if (path === "/api/system/maintenance" || path === "/api/jobs/awaiting-approval") {
        if (!polling) return json(path === "/api/system/maintenance" ? maintenance() : { jobs: [] });
        const response = createDeferred<Response>();
        init?.signal?.addEventListener("abort", () => response.reject(new DOMException("Aborted", "AbortError")), { once: true });
        pending.push({ path, response });
        return response.promise;
      }
      throw new Error(`Unexpected request ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    function ProjectFormProbe() {
      useEffect(() => { mounts += 1; return () => { unmounts += 1; }; }, []);
      return <ProjectsListPage />;
    }
    const view = render(
      <AuthContext.Provider value={auth}>
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={["/projects"]}>
            <AppShell><ProjectFormProbe /></AppShell>
            <RequestLoadingIndicator />
          </MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
    let previousAsyncWrapper: ReturnType<typeof getConfig>["asyncWrapper"] | undefined;
    try {
      previousAsyncWrapper = getConfig().asyncWrapper;
      configure({
        asyncWrapper: async (callback) => {
          const result = await callback();
          await act(async () => { await vi.advanceTimersByTimeAsync(0); });
          return result;
        },
      });
      // Finish initial foreground loads before testing the independent idle polls.
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(hasPendingRequests()).toBe(false);
      const user = userEvent.setup({ pointerEventsCheck: 0, advanceTimers: (delay) => { vi.advanceTimersByTime(delay); } });
      await user.click(screen.getByRole("button", { name: "New project" }));
      const form = screen.getByRole("dialog", { name: "Create project" });
      const name = within(form).getByRole("textbox", { name: "Name" });
      let draft = "Unsaved project";
      await user.type(name, draft);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      function expectUninterrupted() {
        expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
        expect(screen.queryByRole("status", { name: "Loading requests" })).not.toBeInTheDocument();
        expect(within(screen.getByRole("dialog", { name: "Create project" })).getByRole("textbox", { name: "Name" })).toBe(name);
        expect(name).toHaveValue(draft);
        expect(name).toHaveFocus();
        expect(mounts).toBe(1);
        expect(unmounts).toBe(0);
      }
      async function finishPolls(approvalStatus = 200, maintenanceStatus = "disabled", jobs: unknown[] = []) {
        await act(async () => {
          for (const request of pending.splice(0)) {
            request.response.resolve(request.path === "/api/system/maintenance"
              ? json(maintenance(maintenanceStatus))
              : json(approvalStatus === 200 ? { jobs } : { error: "Approvals temporarily unavailable" }, approvalStatus));
          }
          await vi.advanceTimersByTimeAsync(0);
        });
      }
      expectUninterrupted();
      polling = true;
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(pending.map((request) => request.path)).toEqual(["/api/system/maintenance"]);
      // The interaction lock must be absent even before its 400ms visible delay.
      expectUninterrupted();
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expectUninterrupted();
      await user.keyboard(" at15"); draft += " at15";
      expectUninterrupted();
      await finishPolls();
      await act(async () => { await vi.advanceTimersByTimeAsync(14_600); });
      expect(pending.map((request) => request.path).sort()).toEqual(["/api/jobs/awaiting-approval", "/api/system/maintenance"]);
      expectUninterrupted();
      await user.keyboard(" at30"); draft += " at30";
      await finishPolls();
      expectUninterrupted();
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      await finishPolls();
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expectUninterrupted();
      await finishPolls(503);
      expectUninterrupted();
      await user.keyboard(" after error"); draft += " after error";
      expectUninterrupted();
      await act(async () => { globalThis.dispatchEvent(new Event("focus")); });
      expectUninterrupted();
      await finishPolls(200, "draining", [{ job_id: 1001, job_type: "workflow", status: "awaiting_approval", selected_workflow: "weekly_report", project_name: "Demo project" }]);
      expect(screen.getByText("Maintenance Mode is starting")).toBeInTheDocument();
      expect(screen.getByTestId("approval-badge")).toHaveTextContent("1");
      expectUninterrupted();
      expect(requested.filter((path) => path === "/api/jobs/awaiting-approval")).toHaveLength(4);
      expect(requested.filter((path) => path === "/api/system/maintenance")).toHaveLength(6);
      expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
    } finally {
      if (previousAsyncWrapper) configure({ asyncWrapper: previousAsyncWrapper });
      await act(async () => {
        view.unmount();
        for (const request of pending.splice(0)) request.response.resolve(json({}));
        await vi.advanceTimersByTimeAsync(0);
      });
      client.clear();
      vi.clearAllTimers();
      vi.useRealTimers();
    }
  });

  it("keeps the personal menu without tenant controls even with historical multiple memberships", async () => {
    const switchAccount = vi.fn(async () => undefined);
    mockAuthenticatedSession({
      switchAccount,
      userMemberships: [
        {
          account_id: "acct-primary",
          account_name: "Primary Account",
          role: "owner",
          is_active: true,
          is_default: true,
        },
        {
          account_id: "acct-ops",
          account_name: "Ops Account",
          role: "admin",
          is_active: true,
        },
      ],
    });

    await renderShell();

    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));

    expect(screen.queryByText("Account context")).not.toBeInTheDocument();
    expect(screen.queryByText("Primary Account")).not.toBeInTheDocument();
    expect(screen.queryByText("Ops Account")).not.toBeInTheDocument();
    expect(screen.queryByText("acct-primary")).not.toBeInTheDocument();
    expect(screen.queryByText("acct-ops")).not.toBeInTheDocument();

    expect(screen.queryByRole("group", { name: "Accounts" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Account profile" })).toHaveAttribute("href", "/settings/account");
    expect(screen.getByRole("menuitem", { name: "Log out" })).toBeVisible();
    expect(screen.getByRole("menuitemradio", { name: "Dark theme" })).toBeVisible();
    expect(switchAccount).not.toHaveBeenCalled();
  });

  it("does not expose the default tenant in the personal menu", async () => {
    const switchAccount = vi.fn(async () => undefined);
    mockAuthenticatedSession({
      switchAccount,
      userMemberships: [
        {
          account_id: "acct-primary",
          account_name: "Primary Account",
          role: "owner",
          is_active: true,
          is_default: true,
        },
      ],
    });

    await renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));
    expect(screen.queryByText("Primary Account")).not.toBeInTheDocument();
    expect(screen.queryByText("Current account")).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Accounts" })).not.toBeInTheDocument();
    expect(switchAccount).not.toHaveBeenCalled();
  });

  it("treats resume-pending approval events as approval refresh triggers", () => {
    expect(
      approvalJobSubtitle({
        id: 228,
        project_name: "Demo",
        selected_workflow: "weekly_report",
        status: "resume_pending",
      }),
    ).toBe("Demo · Weekly Report · Resume queued");
    expect(
      shouldRefreshApprovalsFromEvent({
        job_id: 228,
        event_type: "job.status.changed",
        status: "resume_pending",
      }),
    ).toBe(true);
  });

  it("does not request the admin-only workspace registry for non-admins", async () => {
    const switchAccount = vi.fn(async () => undefined);
    mockAuthenticatedSession({ switchAccount });
    mockedUseIsAdmin.mockReturnValue(false);

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      createElement(
        QueryClientProvider,
        { client: queryClient },
        createElement(
          MemoryRouter,
          null,
          createElement(AppShell, null, createElement("div", null, "content")),
        ),
      ),
    );

    // Wait for the shell to settle by observing an ungated request.
    await waitFor(() => {
      const fetchMock = vi.mocked(globalThis.fetch);
      const requestedUrls = fetchMock.mock.calls.map(([input]) => stringFromUnknownValue(input));
      expect(requestedUrls).toEqual(
        expect.arrayContaining(["/api/jobs/awaiting-approval"]),
      );
    });

    const fetchMock = vi.mocked(globalThis.fetch);
    const requestedUrls = fetchMock.mock.calls.map(([input]) => stringFromUnknownValue(input));
    expect(requestedUrls.some((url) => url.startsWith("/api/workspaces"))).toBe(false);
  });

  it("keeps product navigation and approvals accessible without an assistant surface", async () => {
    const switchAccount = vi.fn(async () => undefined);
    mockAuthenticatedSession({ switchAccount });

    await renderShell();

    expect(screen.getByRole("link", { name: "Projects" })).toHaveAttribute("href", "/projects");
    expect(screen.getByRole("link", { name: "Jobs" })).toHaveAttribute("href", "/jobs");
    expect(screen.getByRole("link", { name: "Workflows" })).toHaveAttribute("href", "/registry/workflows");
    expect(screen.queryByRole("button", { name: /assistant/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /chat|assistant/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /approvals/i }));
    expect(await screen.findByText("No approvals are waiting.")).toBeInTheDocument();
  });

  it("keeps approval queue failures visible and reloads the queue on retry", async () => {
    mockAuthenticatedSession({ switchAccount: vi.fn(async () => undefined) });
    let approvalsAvailable = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (stringFromUnknownValue(input).startsWith("/api/jobs/awaiting-approval")) {
        return new Response(JSON.stringify(approvalsAvailable
          ? { jobs: [{ id: "42", display_summary: "Review deployment" }] }
          : { detail: "Approval queue unavailable" }), {
          status: approvalsAvailable ? 200 : 503,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ workspaces: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }));

    await renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Open approvals" }));

    expect(await screen.findByText("Approval queue unavailable")).toBeInTheDocument();
    expect(screen.queryByText("No approvals are waiting.")).not.toBeInTheDocument();
    approvalsAvailable = true;
    fireEvent.click(screen.getByRole("button", { name: "Retry approvals" }));

    expect(await screen.findByRole("button", { name: "Review deployment" })).toBeInTheDocument();
    expect(screen.queryByText("Approval queue unavailable")).not.toBeInTheDocument();
  });

  it("hides the account switcher when the user has one account", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
      userMemberships: [
        {
          account_id: "acct-primary",
          account_name: "Primary Account",
          role: "owner",
          is_active: true,
          is_default: true,
        },
      ],
    });

    await renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));

    expect(screen.queryByRole("menuitemradio", { name: /Primary Account/i })).not.toBeInTheDocument();
    expect(screen.queryByText("acct-primary")).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Log out" })).toBeInTheDocument();
  });

  it("does not expose session-level tenants when user memberships are missing", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
      userMemberships: [],
      sessionMemberships: [
        {
          account_id: "acct-primary",
          account_name: "Primary Account",
          role: "owner",
          is_active: true,
        },
        {
          account_id: "acct-analytics",
          account_name: "Analytics Account",
          role: "viewer",
          is_active: true,
        },
      ],
    });

    await renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));

    expect(screen.queryByText("Analytics Account")).not.toBeInTheDocument();
    expect(screen.queryByText("Primary Account")).not.toBeInTheDocument();
    expect(screen.queryByText("acct-analytics")).not.toBeInTheDocument();
  });

  it("adds primary navigation to the sidebar without removing role-based sections", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });

    await renderShell();

    const sidebar = screen.getByTestId("app-shell-sidebar");
    expect(sidebar).toHaveTextContent("Dashboard");
    expect(sidebar).toHaveTextContent("Projects");
    expect(sidebar).toHaveTextContent("Jobs");
    // Built-in management chat is retired.
    expect(sidebar).not.toHaveTextContent("Chat");
    expect(sidebar).toHaveTextContent("MCP");
    expect(sidebar).toHaveTextContent("Settings");
    expect(sidebar).not.toHaveTextContent("User settings");
    expect(sidebar).not.toHaveTextContent("Users & access");
  });

  it("keeps workspace, theme, and account controls out of the Header", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });

    await renderShell();

    const header = within(screen.getByTestId("app-shell-header-frame"));
    expect(header.queryByRole("button", { name: "Workspace" })).not.toBeInTheDocument();
    expect(header.queryByRole("button", { name: "Light theme" })).not.toBeInTheDocument();
    expect(header.queryByRole("button", { name: "Dark theme" })).not.toBeInTheDocument();
    expect(header.queryByRole("button", { name: "Account menu" })).not.toBeInTheDocument();
  });

  it("opens and focuses one Advanced Search surface from the keyboard", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    await renderShell();
    const returnTarget = screen.getByRole("button", { name: "Account menu" });
    returnTarget.focus();

    fireEvent.keyDown(globalThis.window, { ctrlKey: true, key: "k" });

    const dialog = await screen.findByRole("dialog", { name: "Advanced search" });
    const combobox = within(dialog).getByRole("combobox", {
      name: "Search Flow Steward",
    });
    await waitFor(() => expect(combobox).toHaveFocus());

    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(
      screen.queryByRole("dialog", { name: "Advanced search" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(returnTarget).toHaveFocus());
  });

  it("opens Advanced Search from the compact trigger and returns focus to it", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    await renderShell();
    const trigger = screen.getByRole("button", { name: "Global search" });
    trigger.focus();

    fireEvent.click(trigger);

    const dialog = await screen.findByRole("dialog", { name: "Advanced search" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Close advanced search" }));

    expect(
      screen.queryByRole("dialog", { name: "Advanced search" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("preserves the original return target across repeated Search shortcuts", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    await renderShell();
    const returnTarget = screen.getByRole("button", { name: "Account menu" });
    returnTarget.focus();

    fireEvent.keyDown(globalThis.window, { ctrlKey: true, key: "k" });
    const dialog = await screen.findByRole("dialog", { name: "Advanced search" });
    // Re-triggering while open must not adopt a control inside the dialog as the target.
    fireEvent.keyDown(globalThis.window, { ctrlKey: true, key: "k" });

    fireEvent.keyDown(dialog, { key: "Escape" });

    await waitFor(() => expect(returnTarget).toHaveFocus());
  });

  it("opens the Search surface from the Header without issuing a search request", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    await renderShell();
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Global search" }));

    await screen.findByRole("dialog", { name: "Advanced search" });
    const searched = fetchMock.mock.calls
      .map(([input]) => stringFromUnknownValue(input))
      .filter((url) => url.startsWith("/api/search?"));
    // An empty query never reaches the backend.
    expect(searched).toEqual([]);
  });

  it("uses the configured title for a registered route breadcrumb", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
      sessionOverrides: {
        registered_pages: [
          {
            component_key: "audit_center",
            page_key: "audit_center",
            path: "/custom/audit-center",
            title: "Audit Center",
          },
        ],
      },
    });

    await renderShell("/custom/audit-center");

    expect(
      screen.getByRole("navigation", { name: "Breadcrumb" }),
    ).toHaveTextContent("Audit Center");
  });

  it("uses the configured title for a settings route breadcrumb", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
      sessionOverrides: {
        settings_pages: [
          {
            page_key: "custom_models",
            path: "/settings/custom-models",
            settings_tab: true,
            title: "Custom Model Providers",
          },
        ],
      },
    });

    await renderShell("/settings/custom-models");

    expect(
      screen.getByRole("navigation", { name: "Breadcrumb" }),
    ).toHaveTextContent("Custom Model Providers");
  });

  it("uses the breakpoint rail default and persists an explicit toggle", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    Object.defineProperty(globalThis, "innerWidth", {
      configurable: true,
      value: 768,
    });

    await renderShell();

    // The rail edge has the same name; Header's PanelLeft is the one asserted here.
    const sidebarTrigger = within(screen.getByRole("banner")).getByRole("button", {
      name: "Toggle sidebar",
    });
    expect(screen.getByTestId("app-shell-layout")).toHaveAttribute(
      "data-rail-state",
      "collapsed",
    );
    expect(sidebarTrigger).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(sidebarTrigger);
    expect(screen.getByTestId("app-shell-layout")).toHaveAttribute(
      "data-rail-state",
      "expanded",
    );
    expect(sidebarTrigger).toHaveAttribute("aria-expanded", "true");
    expect(localStorage.getItem("fsb-rail")).toBe("expanded");
  });

  it("shows workspace failure without removing the current shell and retries", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    let workspaceRequests = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = stringFromUnknownValue(input);
      if (url.startsWith("/api/workspaces")) {
        workspaceRequests += 1;
        if (workspaceRequests === 1) {
          throw new Error("Workspace load failed");
        }
        return new Response(
          JSON.stringify({
            workspaces: [
              {
                id: "workspace-atlas",
                workspace_key: "atlas",
                display_name: "Atlas",
              },
            ],
          }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        );
      }
      if (url.startsWith("/api/jobs/awaiting-approval")) {
        return new Response(JSON.stringify({ jobs: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ groups: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    expect(screen.getByText("Unable to load workspaces.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Retry workspaces" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Workspace" })).toHaveTextContent(
        "Atlas",
      );
    });
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeVisible();
  });

  it("ignores an older workspace failure after a newer refresh succeeds", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    const firstWorkspaceRequest = createDeferred<Response>();
    let workspaceRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = stringFromUnknownValue(input);
        if (url.startsWith("/api/workspaces")) {
          workspaceRequests += 1;
          if (workspaceRequests === 1) return firstWorkspaceRequest.promise;
          return new Response(
            JSON.stringify({
              workspaces: [
                {
                  id: "workspace-new",
                  workspace_key: "new",
                  display_name: "Newest workspace",
                },
              ],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url.startsWith("/api/jobs/awaiting-approval")) {
          return new Response(JSON.stringify({ jobs: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ groups: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }) as unknown as typeof fetch,
    );

    await renderShell();
    await act(async () => {
      globalThis.dispatchEvent(new Event(WORKSPACES_CHANGED_EVENT));
    });
    await waitFor(() => expect(workspaceRequests).toBe(2));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Workspace" })).toHaveTextContent(
        "Newest workspace",
      ),
    );

    await act(async () => {
      firstWorkspaceRequest.reject(new Error("stale failure"));
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    expect(screen.queryByText("Unable to load workspaces.")).not.toBeInTheDocument();
  });

  it("ignores an older workspace success after a newer refresh fails", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    const firstWorkspaceRequest = createDeferred<Response>();
    let workspaceRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = stringFromUnknownValue(input);
        if (url.startsWith("/api/workspaces")) {
          workspaceRequests += 1;
          if (workspaceRequests === 1) return firstWorkspaceRequest.promise;
          throw new Error("newest failure");
        }
        if (url.startsWith("/api/jobs/awaiting-approval")) {
          return new Response(JSON.stringify({ jobs: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ groups: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }) as unknown as typeof fetch,
    );

    await renderShell();
    await act(async () => {
      globalThis.dispatchEvent(new Event(WORKSPACES_CHANGED_EVENT));
    });
    await waitFor(() => expect(workspaceRequests).toBe(2));
    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    expect(await screen.findByText("Unable to load workspaces.")).toBeVisible();

    await act(async () => {
      firstWorkspaceRequest.resolve(
        new Response(
          JSON.stringify({
            workspaces: [
              {
                id: "workspace-stale",
                workspace_key: "stale",
                display_name: "Stale workspace",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
      await Promise.resolve();
    });

    expect(screen.getByText("Unable to load workspaces.")).toBeVisible();
    expect(screen.queryByText("Stale workspace")).not.toBeInTheDocument();
  });

  it("invalidates an in-flight admin workspace request after permission is removed", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    const adminWorkspaceRequest = createDeferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = stringFromUnknownValue(input);
        if (url.startsWith("/api/workspaces")) return adminWorkspaceRequest.promise;
        if (url.startsWith("/api/jobs/awaiting-approval")) {
          return new Response(JSON.stringify({ jobs: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ groups: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }) as unknown as typeof fetch,
    );

    const view = await renderShell();
    mockedUseIsAdmin.mockReturnValue(false);
    view.rerender(
      createElement(
        QueryClientProvider,
        { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
        createElement(
          MemoryRouter,
          null,
          createElement(AppShell, null, createElement("div", null, "content")),
        ),
      ),
    );

    await act(async () => {
      adminWorkspaceRequest.resolve(
        new Response(
          JSON.stringify({
            workspaces: [
              {
                id: "workspace-admin",
                workspace_key: "admin",
                display_name: "Stale admin workspace",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
      await Promise.resolve();
    });

    // The superseded response is ignored, so no workspace ever resolves and the
    // trigger keeps its not-loaded label. It used to read "All workspaces";
    // there is no such scope now.
    expect(screen.getByRole("button", { name: "Workspace" })).toHaveTextContent(
      "Workspace",
    );
    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    expect(screen.queryByText("Stale admin workspace")).not.toBeInTheDocument();
  });

  it("renders injected sidebar sections beyond system navigation", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
      sessionOverrides: {
        shell_navigation: {
          private_tools: [
            {
              page_key: "private_fixture",
              path: "/private/fixture",
              nav_label: "Private Fixture",
              nav_icon: "extension",
            },
          ],
        },
      },
    });

    await renderShell();

    const sidebar = screen.getByTestId("app-shell-sidebar");
    expect(sidebar).toHaveTextContent("Private Tools");
    expect(screen.getByRole("link", { name: "Private Fixture" })).toHaveAttribute(
      "href",
      "/private/fixture",
    );
  });

  it("does not render locale switching options in the user menu", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });

    await renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));

    expect(screen.queryByText("Language")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Русский/i })).not.toBeInTheDocument();
  });

  it("includes MCP in the registry navigation", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });

    await renderShell();

    expect(screen.getAllByText("MCP").length).toBeGreaterThan(0);
  });

  it("toggles root theme attributes when theme switch is clicked", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });

    await renderShell();

    const root = document.documentElement;
    expect(root).toHaveAttribute("data-theme", "dark");
    expect(root).toHaveClass("dark");

    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Light theme" }));

    await waitFor(() => {
      expect(root).toHaveAttribute("data-theme", "light");
      expect(root).not.toHaveClass("dark");
    });
    expect(localStorage.getItem("platform.ui_theme")).toBe("light");
    expect(localStorage.getItem("platform-theme")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Dark theme" }));

    await waitFor(() => expect(root).toHaveAttribute("data-theme", "dark"));
    expect(root).toHaveClass("dark");
    expect(localStorage.getItem("platform.ui_theme")).toBe("dark");
    expect(localStorage.getItem("platform-theme")).toBeNull();
  });

  it("ignores an old-only preference and uses the dark default", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    localStorage.setItem("platform-theme", "light");

    await renderShell();

    await waitFor(() => expect(document.documentElement).toHaveAttribute("data-theme", "dark"));
    expect(document.documentElement).toHaveClass("dark");
    expect(document.documentElement).not.toHaveClass("platform-light");
    expect(localStorage.getItem("platform.ui_theme")).toBe("dark");
    expect(localStorage.getItem("platform-theme")).toBe("light");
  });

  it("restores the current light preference", async () => {
    mockAuthenticatedSession({ switchAccount: vi.fn(async () => undefined) });
    localStorage.setItem("platform.ui_theme", "light");

    await renderShell();

    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(document.documentElement).toHaveAttribute("data-platform-theme", "light");
    expect(document.documentElement).toHaveClass("platform-light");
    expect(document.documentElement).toHaveStyle({ colorScheme: "light" });
    expect(localStorage.getItem("platform-theme")).toBeNull();
  });

  it("synchronizes shell theme when root data-theme is changed externally", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });

    await renderShell();

    const root = document.documentElement;
    expect(root).toHaveAttribute("data-theme", "dark");

    root.setAttribute("data-theme", "light");

    await waitFor(() => {
      expect(root).toHaveClass("platform-light");
      expect(root).not.toHaveClass("dark");
    });
  });


  it("refreshes approval jobs from the global realtime stream", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
    let approvalJobs: unknown[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = stringFromUnknownValue(input);
      if (url.startsWith("/api/jobs/awaiting-approval")) {
        return new Response(JSON.stringify({ jobs: approvalJobs }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ groups: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await renderShell();

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => stringFromUnknownValue(input) === "/api/jobs/awaiting-approval")).toBe(
        true,
      );
    });
    expect(FakeEventSource.instances).toHaveLength(0);

    fireEvent.click(screen.getByTestId("approval-bell"));

    await waitFor(() => {
      expect(FakeEventSource.instances[0]?.url).toBe("/api/events/stream");
    });
    const callsAfterInitialLoad = fetchMock.mock.calls.filter(([input]) =>
      stringFromUnknownValue(input).startsWith("/api/jobs/awaiting-approval"),
    ).length;

    act(() => {
      FakeEventSource.instances[0]?.emit({
        id: 9000,
        job_id: 228,
        event_type: "runtime.message",
        status: "completed",
        message: "Log captured",
      });
    });

    await new Promise((resolve) => {
      setTimeout(resolve, 220);
    });
    expect(
      fetchMock.mock.calls.filter(([input]) => stringFromUnknownValue(input).startsWith("/api/jobs/awaiting-approval")),
    ).toHaveLength(callsAfterInitialLoad);

    approvalJobs = [
      {
        id: 228,
        project_name: "Demo",
        selected_workflow: "weekly_report",
        status: "awaiting_approval",
        task_summary: "Review generated plan",
      },
    ];
    act(() => {
      FakeEventSource.instances[0]?.emit({
        id: 9001,
        job_id: 228,
        event_type: "job.status.changed",
        status: "awaiting_approval",
      });
    });

    await waitFor(() => {
      expect(screen.getByTestId("approval-badge")).toHaveTextContent("1");
    });

    const approvalLink = await screen.findByTestId("approval-job-link");
    expect(screen.getByText(/Weekly Report · Waiting for approval/)).toBeInTheDocument();
    fireEvent.click(approvalLink);
    expect(screen.queryByRole("dialog", { name: "Approvals" })).not.toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent(
      "Job details",
    );
  });

  it("normalizes a stale stored workspace selection to the only active workspace", async () => {
    mockAuthenticatedSession({
      switchAccount: vi.fn(async () => undefined),
    });
    globalThis.localStorage.setItem("platform:selected-workspace-id", "workspace-deleted");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = stringFromUnknownValue(input);
      if (url.startsWith("/api/workspaces")) {
        return new Response(
          JSON.stringify({
            workspaces: [
              {
                id: "workspace-test",
                workspace_key: "test",
                display_name: "Test",
              },
            ],
          }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        );
      }
      if (url.startsWith("/api/jobs/awaiting-approval")) {
        return new Response(JSON.stringify({ jobs: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ groups: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await renderShell();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Workspace" })).toHaveTextContent(
        "Test",
      );
    });
    expect(globalThis.localStorage.getItem("platform:selected-workspace-id")).toBe("workspace-test");
  });
});
