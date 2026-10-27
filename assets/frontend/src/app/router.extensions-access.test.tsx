import { render, screen } from "@testing-library/react";
import { Outlet } from "react-router";

import { AppRouter } from "@/app/router";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";

vi.mock("@/components/layout/AppShell", () => ({
  AppShell: () => <Outlet />,
}));

vi.mock("@/pages/extensions", () => ({
  ExtensionsHubPage: () => <div>Extensions surface reached</div>,
}));

vi.mock("@/pages/auth/login", () => ({
  LoginPage: () => <div>Login required</div>,
}));

function authValue(role?: "admin" | "operator" | "viewer"): AuthContextValue {
  const authenticated = Boolean(role);
  return {
    error: null,
    isAuthenticated: authenticated,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    session: {
      authenticated,
      roles: role ? [role] : [],
      user: { roles: role ? [role] : [] },
    } as AuthContextValue["session"],
    status: authenticated ? "authenticated" : "anonymous",
  };
}

function renderRoute(path: string, role?: "admin" | "operator" | "viewer") {
  globalThis.history.pushState({}, "", path);
  return render(
    <AuthContext.Provider value={authValue(role)}>
      <AppRouter />
    </AuthContext.Provider>,
  );
}

describe("extension route access", () => {
  it.each(["admin", "operator", "viewer"] as const)(
    "lets an authenticated %s reach extension detail",
    async (role) => {
      renderRoute("/extensions/fixture.extension", role);
      expect(await screen.findByText("Extensions surface reached")).toBeInTheDocument();
    },
  );

  it("redirects an anonymous detail request to login", async () => {
    renderRoute("/extensions/fixture.extension");
    expect(await screen.findByText("Login required")).toBeInTheDocument();
  });

  it("keeps the global extensions catalog admin-only", async () => {
    const view = renderRoute("/extensions", "operator");
    expect(await screen.findByRole("heading", { name: "Access denied" })).toBeInTheDocument();
    expect(screen.queryByText("Extensions surface reached")).not.toBeInTheDocument();
    view.unmount();

    renderRoute("/extensions", "admin");
    expect(await screen.findByText("Extensions surface reached")).toBeInTheDocument();
  });
});
