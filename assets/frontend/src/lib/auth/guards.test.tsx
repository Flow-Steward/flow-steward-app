import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { RequireCapability } from "@/lib/auth/guards";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";

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

describe("RequireCapability", () => {
  it("localizes denied access without rendering privileged content", () => {
    const messages: Record<string, string> = { "react.auth.access_denied": "Prieiga uždrausta" };
    render(
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", t: (key, fallback = "") => messages[key] || fallback || key }}>
        <AuthContext.Provider value={authValue("viewer")}>
          <MemoryRouter>
            <RequireCapability capability="administer_platform"><div>Sensitive inventory fetched</div></RequireCapability>
          </MemoryRouter>
        </AuthContext.Provider>
      </I18nContext.Provider>,
    );
    expect(screen.getByRole("heading", { name: "Prieiga uždrausta" })).toBeInTheDocument();
    expect(screen.queryByText("Sensitive inventory fetched")).not.toBeInTheDocument();
  });
  it("denies a viewer before the privileged route element renders", () => {
    render(
      <AuthContext.Provider value={authValue("viewer")}>
        <MemoryRouter>
          <RequireCapability capability="administer_platform">
            <div>Sensitive inventory fetched</div>
          </RequireCapability>
        </MemoryRouter>
      </AuthContext.Provider>,
    );

    expect(screen.getByRole("heading", { name: "Access denied" })).toBeInTheDocument();
    expect(screen.queryByText("Sensitive inventory fetched")).not.toBeInTheDocument();
  });

  it("allows an operator to render registry read surfaces", () => {
    render(
      <AuthContext.Provider value={authValue("operator")}>
        <MemoryRouter>
          <RequireCapability capability="read_registry">
            <div>Registry inventory</div>
          </RequireCapability>
        </MemoryRouter>
      </AuthContext.Provider>,
    );

    expect(screen.getByText("Registry inventory")).toBeInTheDocument();
  });
});
