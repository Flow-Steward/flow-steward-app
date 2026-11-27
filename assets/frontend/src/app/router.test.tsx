import { createElement } from "react";
import type { ReactNode } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";

import type { AuthSession } from "@/lib/auth/AuthProvider";
import type * as AuthGuards from "@/lib/auth/guards";
import { useAuth } from "@/lib/auth/useAuth";
import { AppRouter, HomeGate } from "@/app/router";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { sessionUser, type SystemRole } from "@/test/role-permissions";

vi.mock("@/lib/auth/useAuth", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/auth/guards", async () => {
  const React = await import("react");
  return {
    RequireAuth: ({ children }: { children: ReactNode }) => React.createElement(React.Fragment, null, children),
    RequireAdmin: ({ children }: { children: ReactNode }) => React.createElement(React.Fragment, null, children),
    RequireCapability: ({ children }: { children: ReactNode }) => React.createElement(React.Fragment, null, children),
    // The real gate: a module settings page opens by its own declared permission.
    RequireSettingsPage: (await vi.importActual<typeof AuthGuards>("@/lib/auth/guards")).RequireSettingsPage,
  };
});

vi.mock("@/components/layout/AppShell", async () => {
  const React = await import("react");
  const { Outlet } = await import("react-router");
  return {
    AppShell: () => React.createElement(Outlet),
  };
});

vi.mock("@/app/extensions", async () => {
  const React = await import("react");
  const RegisteredFixturePage = ({ page }: Readonly<{ page: { label: string } }>) =>
    React.createElement("div", null, `Registered page: ${page.label}`);
  return {
    registeredPageComponents: {
      "private.fixture": RegisteredFixturePage,
    },
    resolveRegisteredPageComponent: (componentKey: string) =>
      componentKey === "private.fixture" ? RegisteredFixturePage : undefined,
  };
});

vi.mock("@/pages/projects/detail", () => ({
  ProjectsDetailPage: () => <div>Project detail rendered</div>,
}));

vi.mock("@/pages/operator/section-page", () => ({
  OperatorSectionPage: () => <div>Operator section rendered</div>,
}));

vi.mock("@/pages/settings/hub", () => ({
  SettingsHubPage: () => <div>Settings hub</div>,
  RegisteredSettingsResourceAdminPage: ({ page }: Readonly<{ page: { title: string } }>) => (
    <div>Resource admin: {page.title}</div>
  ),
}));

const mockedUseAuth = vi.mocked(useAuth);

function createSession(overrides: Partial<AuthSession> = {}): AuthSession {
  return {
    authenticated: false,
    local_auth_enabled: true,
    bootstrap_required: false,
    external_auth_enabled: false,
    auth_source: "",
    roles: [],
    permissions: {
      can_read: true,
      can_mutate: false,
    },
    account_memberships: [],
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
    ...overrides,
  };
}

describe("HomeGate", () => {
  beforeEach(() => {
    mockedUseAuth.mockReset();
  });

  it.each([
    { locale: "en", label: "Restoring your session" },
    { locale: "lt", label: "Atkuriamas jūsų seansas" },
  ])("restores the session behind the canon page loading state ($locale)", ({ locale, label }) => {
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: false,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession(),
      status: "loading",
    });

    vi.useFakeTimers();
    try {
      render(
        <I18nContext.Provider value={{ ...fallbackContext, locale, t: (key, fallback = "") => key === "react.routing.restoring_session" ? label : fallback || key }}>
          <MemoryRouter><HomeGate /></MemoryRouter>
        </I18nContext.Provider>,
      );

      // Canon Loader: nothing for the first 400ms, then the named wait.
      expect(screen.getByRole("status")).toBeEmptyDOMElement();
      act(() => {
        vi.advanceTimersByTime(400);
      });
      expect(screen.getByRole("status")).toHaveTextContent(label);
      expect(screen.queryByRole("heading")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ["admin", "Dashboard rendered"],
    // ADR 0075: operators and viewers land on their sections.
    ["operator", "Sections rendered"],
    ["viewer", "Sections rendered"],
  ])("lands a signed-in %s on the right page", (role, expected) => {
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession({ authenticated: true, roles: [role], user: sessionUser(role as SystemRole) }),
      status: "authenticated",
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<HomeGate />} index />
          <Route element={<div>Dashboard rendered</div>} path="/dashboard" />
          <Route element={<div>Sections rendered</div>} path="/sections" />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText(expected)).toBeInTheDocument();
  });
});

describe("AppRouter", () => {
  beforeEach(() => {
    mockedUseAuth.mockReset();
    globalThis.history.pushState({}, "", "/projects/new");
  });

  it.each(["/chat", "/chat/shell", "/chat/contextual-assistant", "/settings/assistant", "/settings/unknown"])(
    "keeps unavailable route %s out of the product",
    (path) => {
      globalThis.history.pushState({}, "", path);
      mockedUseAuth.mockReturnValue({
        error: null,
        isAuthenticated: true,
        logout: vi.fn(async () => undefined),
        refresh: vi.fn(async () => undefined),
        switchAccount: vi.fn(async () => undefined),
        session: createSession({ authenticated: true, user: sessionUser("admin") }),
        status: "authenticated",
      });

      render(createElement(AppRouter));

      expect(screen.getByRole("heading", { name: "This page is not available." })).toBeInTheDocument();
      expect(screen.queryByText("Settings hub")).not.toBeInTheDocument();
    },
  );

  it.each(["/oauth/consent?error=client_not_allowed", "/app/oauth/consent?error=client_not_allowed"])(
    "serves the OAuth consent error page %s without a session and outside the app shell",
    async (path) => {
      globalThis.history.pushState({}, "", path);
      mockedUseAuth.mockReturnValue({
        error: null,
        isAuthenticated: false,
        logout: vi.fn(async () => undefined),
        refresh: vi.fn(async () => undefined),
        switchAccount: vi.fn(async () => undefined),
        session: createSession(),
        status: "anonymous",
      });

      render(createElement(AppRouter));

      await waitFor(() => expect(screen.getByText("This app is not supported")).toBeInTheDocument());
      expect(screen.queryByRole("heading", { name: "This page is not available." })).not.toBeInTheDocument();
    },
  );

  it("renders settings hub control surfaces registered without a dedicated component", async () => {
    globalThis.history.pushState({}, "", "/settings/custom-module");
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession({
        authenticated: true,
        user: sessionUser("admin"),
        settings_pages: [{
          page_key: "custom_module",
          title: "Custom Module",
          path: "/settings/custom-module",
          settings_tab: true,
          control_surface: { kind: "resource_admin", resources: [] },
        }],
      }),
      status: "authenticated",
    });

    render(createElement(AppRouter));

    await waitFor(() => expect(screen.getByText("Settings hub")).toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "This page is not available." })).not.toBeInTheDocument();
  });

  it("reserves /projects/new as unavailable instead of treating it as a project id", () => {
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession({ authenticated: true }),
      status: "authenticated",
    });

    render(createElement(AppRouter));

    expect(screen.getByRole("heading", { name: "This page is not available." })).toBeInTheDocument();
    expect(screen.queryByText("Project detail rendered")).not.toBeInTheDocument();
  });

  it.each(["edit", "history", "scheduled-jobs", "queue"])(
    "has no /projects/:id/%s screen: every project section is a tab of the project page",
    (segment) => {
      globalThis.history.pushState({}, "", `/projects/project-123/${segment}`);
      mockedUseAuth.mockReturnValue({
        error: null,
        isAuthenticated: true,
        logout: vi.fn(async () => undefined),
        refresh: vi.fn(async () => undefined),
        switchAccount: vi.fn(async () => undefined),
        session: createSession({ authenticated: true, roles: ["admin"] }),
        status: "authenticated",
      });

      render(createElement(AppRouter));

      expect(screen.getByRole("heading", { name: "This page is not available." })).toBeInTheDocument();
      expect(screen.queryByText("Project detail rendered")).not.toBeInTheDocument();
    },
  );

  it("opens a published workflow's section at /projects/:id/workflows/:id/work (ADR 0075)", async () => {
    globalThis.history.pushState({}, "", "/projects/project-123/workflows/wf.receipts/work");
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession({ authenticated: true, roles: ["viewer"] }),
      status: "authenticated",
    });

    render(createElement(AppRouter));

    await waitFor(() => expect(screen.getByText("Operator section rendered")).toBeInTheDocument());
    expect(screen.queryByText("Project detail rendered")).not.toBeInTheDocument();
  });

  it("opens the project page for a role that cannot change projects", async () => {
    globalThis.history.pushState({}, "", "/projects/project-123?tab=policy");
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession({ authenticated: true, roles: ["viewer"] }),
      status: "authenticated",
    });

    render(createElement(AppRouter));

    await waitFor(() => expect(screen.getByText("Project detail rendered")).toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "Access denied" })).not.toBeInTheDocument();
  });

  it("keeps the retired project knowledge deep link unavailable", () => {
    globalThis.history.pushState({}, "", "/projects/project-123/kb");
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession({
        authenticated: true,
        registered_pages: [
          {
            key: "retired_fixture",
            page_key: "retired_fixture",
            label: "Retired fixture",
            title: "Retired fixture",
            path: "/projects/project-123/kb",
            component_key: "private.fixture",
          },
        ],
      }),
      status: "authenticated",
    });

    render(createElement(AppRouter));

    expect(screen.getByRole("heading", { name: "This page is not available." })).toBeInTheDocument();
    expect(screen.queryByText("Registered page: Retired fixture")).not.toBeInTheDocument();
  });

  it("renders authenticated registered extension pages outside settings", () => {
    globalThis.history.pushState({}, "", "/private/fixture");
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession({
        authenticated: true,
        registered_pages: [
          {
            key: "private_fixture",
            page_key: "private_fixture",
            label: "Private Fixture",
            title: "Private Fixture",
            path: "/private/fixture",
            component_key: "private.fixture",
          },
        ],
      }),
      status: "authenticated",
    });

    render(createElement(AppRouter));

    expect(screen.getByText("Registered page: Private Fixture")).toBeInTheDocument();
  });

  it("prefers registered pages over the settings hub catch-all", () => {
    globalThis.history.pushState({}, "", "/settings/fixture");
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession({
        authenticated: true,
        user: sessionUser("admin"),
        registered_pages: [
          {
            key: "private_fixture",
            page_key: "private_fixture",
            label: "Private Fixture",
            title: "Private Fixture",
            path: "/settings/fixture",
            component_key: "private.fixture",
          },
        ],
        settings_pages: [
          {
            page_key: "private_fixture",
            title: "Private Fixture",
            path: "/settings/fixture",
            settings_tab: true,
          },
        ],
      }),
      status: "authenticated",
    });

    render(createElement(AppRouter));

    expect(screen.getByText("Registered page: Private Fixture")).toBeInTheDocument();
    expect(screen.queryByText("Settings hub")).not.toBeInTheDocument();
  });

  describe("host-rendered registered pages", () => {
    const resourceAdminSurface = {
      kind: "resource_admin",
      resources: [{
        key: "records",
        title: "Records",
        list_path: "/api/modules/fixture/records",
        id_field: "id",
        fields: [{ key: "name", label: "Name", type: "text" }],
      }],
    };

    function renderAt(path: string, overrides: Partial<AuthSession>) {
      globalThis.history.pushState({}, "", path);
      mockedUseAuth.mockReturnValue({
        error: null,
        isAuthenticated: true,
        logout: vi.fn(async () => undefined),
        refresh: vi.fn(async () => undefined),
        switchAccount: vi.fn(async () => undefined),
        session: createSession({ authenticated: true, user: sessionUser("admin"), ...overrides }),
        status: "authenticated",
      });
      render(createElement(AppRouter));
    }

    it("renders a keyless resource_admin page outside settings with the host renderer", async () => {
      renderAt("/modules/records", {
        registered_pages: [{
          key: "module_records",
          page_key: "module_records",
          label: "Module Records",
          title: "Module Records",
          path: "/modules/records",
          control_surface: resourceAdminSurface,
        }],
      });

      await waitFor(() => expect(screen.getByText("Resource admin: Module Records")).toBeInTheDocument());
      expect(screen.queryByRole("heading", { name: "This page is not available." })).not.toBeInTheDocument();
    });

    it("renders a keyless resource_report page with the host renderer", async () => {
      renderAt("/modules/journal", {
        registered_pages: [{
          key: "module_journal",
          page_key: "module_journal",
          label: "Module Journal",
          title: "Module Journal",
          path: "/modules/journal",
          control_surface: {
            kind: "resource_report",
            list_path: "/api/modules/fixture/journal",
            columns: [{ key: "action", label: "Action", type: "text" }],
          },
        }],
      });

      await waitFor(() => expect(screen.getByText("Resource admin: Module Journal")).toBeInTheDocument());
      expect(screen.queryByRole("heading", { name: "This page is not available." })).not.toBeInTheDocument();
    });

    it("keeps the placeholder for a keyless page with an unknown surface kind", () => {
      renderAt("/modules/dashboard", {
        registered_pages: [{
          key: "module_dashboard",
          page_key: "module_dashboard",
          label: "Module Dashboard",
          title: "Module Dashboard",
          path: "/modules/dashboard",
          control_surface: { kind: "dashboard" },
        }],
      });

      expect(screen.getByRole("heading", { name: "This page is not available." })).toBeInTheDocument();
      expect(screen.getByText("The registered screen is not included in this frontend build.")).toBeInTheDocument();
      expect(screen.queryByText("Resource admin: Module Dashboard")).not.toBeInTheDocument();
    });

    it("prefers a compiled component over the host renderer", () => {
      renderAt("/modules/records", {
        registered_pages: [{
          key: "module_records",
          page_key: "module_records",
          label: "Module Records",
          title: "Module Records",
          path: "/modules/records",
          component_key: "private.fixture",
          control_surface: resourceAdminSurface,
        }],
      });

      expect(screen.getByText("Registered page: Module Records")).toBeInTheDocument();
      expect(screen.queryByText("Resource admin: Module Records")).not.toBeInTheDocument();
    });

    it("keeps a keyless resource_admin settings tab inside the settings hub", async () => {
      renderAt("/settings/module-records", {
        registered_pages: [{
          key: "module_records",
          page_key: "module_records",
          label: "Module Records",
          title: "Module Records",
          path: "/settings/module-records",
          control_surface: resourceAdminSurface,
        }],
        settings_pages: [{
          page_key: "module_records",
          title: "Module Records",
          path: "/settings/module-records",
          settings_tab: true,
          control_surface: resourceAdminSurface,
        }],
      });

      await waitFor(() => expect(screen.getByText("Settings hub")).toBeInTheDocument());
      expect(screen.queryByText("Resource admin: Module Records")).not.toBeInTheDocument();
    });
  });

  describe("module settings pages and their declared permission", () => {
    const reportSurface = {
      kind: "resource_report",
      list_path: "/api/modules/fixture/journal",
      columns: [{ key: "action", label: "Action", type: "text" }],
    };

    function modulePage(pageKey: string, path: string, requiredPermission: string) {
      return {
        page_key: pageKey,
        title: pageKey,
        path,
        settings_tab: true,
        required_permission: requiredPermission,
        control_surface: reportSurface,
      };
    }

    function renderAs(role: SystemRole, path: string, settingsPages: AuthSession["settings_pages"]) {
      globalThis.history.pushState({}, "", path);
      mockedUseAuth.mockReturnValue({
        error: null,
        isAuthenticated: true,
        logout: vi.fn(async () => undefined),
        refresh: vi.fn(async () => undefined),
        switchAccount: vi.fn(async () => undefined),
        session: createSession({ authenticated: true, roles: [role], user: sessionUser(role), settings_pages: settingsPages }),
        status: "authenticated",
      });
      render(createElement(AppRouter));
    }

    it("lets an operator open a viewer-level module page the bootstrap lists", async () => {
      renderAs("operator", "/settings/journal", [modulePage("journal", "/settings/journal", "viewer")]);

      await waitFor(() => expect(screen.getByText("Settings hub")).toBeInTheDocument());
      expect(screen.queryByRole("heading", { name: "Access denied" })).not.toBeInTheDocument();
    });

    it("refuses an operator an admin-level module page", () => {
      renderAs("operator", "/settings/admin-records", [modulePage("admin_records", "/settings/admin-records", "admin")]);
      expect(screen.getByRole("heading", { name: "Access denied" })).toBeInTheDocument();
      expect(screen.queryByText("Settings hub")).not.toBeInTheDocument();
    });

    it("refuses an operator a module page the bootstrap does not list", () => {
      renderAs("operator", "/settings/journal", []);
      expect(screen.getByRole("heading", { name: "Access denied" })).toBeInTheDocument();
    });

    it("keeps every settings page open to an administrator", async () => {
      renderAs("admin", "/settings/admin-records", [modulePage("admin_records", "/settings/admin-records", "admin")]);

      await waitFor(() => expect(screen.getByText("Settings hub")).toBeInTheDocument());
      expect(screen.queryByRole("heading", { name: "Access denied" })).not.toBeInTheDocument();
    });
  });
});
