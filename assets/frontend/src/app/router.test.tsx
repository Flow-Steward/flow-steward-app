import { createElement } from "react";
import type { ReactNode } from "react";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";

import type { AuthSession } from "@/lib/auth/AuthProvider";
import { useAuth } from "@/lib/auth/useAuth";
import { AppRouter, HomeGate } from "@/app/router";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";

vi.mock("@/lib/auth/useAuth", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/auth/guards", async () => {
  const React = await import("react");
  return {
    RequireAuth: ({ children }: { children: ReactNode }) => React.createElement(React.Fragment, null, children),
    RequireAdmin: ({ children }: { children: ReactNode }) => React.createElement(React.Fragment, null, children),
    RequireCapability: ({ children }: { children: ReactNode }) => React.createElement(React.Fragment, null, children),
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

vi.mock("@/pages/settings/hub", () => ({
  SettingsHubPage: () => <div>Settings hub</div>,
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

  it("lands a signed-in user on the Dashboard", () => {
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession({ authenticated: true }),
      status: "authenticated",
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<HomeGate />} index />
          <Route element={<div>Dashboard rendered</div>} path="/dashboard" />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("Dashboard rendered")).toBeInTheDocument();
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
        session: createSession({ authenticated: true }),
        status: "authenticated",
      });

      render(createElement(AppRouter));

      expect(screen.getByRole("heading", { name: "This page is not available." })).toBeInTheDocument();
      expect(screen.queryByText("Settings hub")).not.toBeInTheDocument();
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

    expect(await screen.findByText("Settings hub")).toBeInTheDocument();
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

    expect(await screen.findByText("Project detail rendered")).toBeInTheDocument();
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
});
