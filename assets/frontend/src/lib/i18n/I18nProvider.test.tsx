import { render, screen } from "@testing-library/react";

import type { AuthSession } from "@/lib/auth/AuthProvider";
import { AuthContext } from "@/lib/auth/context";
import { I18nProvider } from "@/lib/i18n/I18nProvider";
import { useI18n } from "@/lib/i18n/useI18n";

const session: AuthSession = {
  authenticated: true,
  local_auth_enabled: true,
  bootstrap_required: false,
  external_auth_enabled: false,
  roles: ["operator"],
  permissions: { can_read: true },
  account_memberships: [],
  i18n: {
    locale: "en",
    fallback_locale: "en",
    supported_locales: [
      { code: "en", label: "English", native_label: "English" },
      { code: "lt", label: "Lithuanian", native_label: "Lietuvių" },
    ],
    messages: {},
  },
  feature_flags: {},
  environment: { edition: "community" },
  settings_pages: [],
  registered_pages: [],
  shell_navigation: {},
};

function Greeting() {
  const { t } = useI18n();
  return <p>{t("greeting", "Hello, {name}", { name: "ACME" })}</p>;
}

function View({ locale }: Readonly<{ locale: "en" | "lt" }>) {
  const localizedSession: AuthSession = {
    ...session,
    i18n: { ...session.i18n, locale, messages: locale === "lt" ? { greeting: "Sveiki, {name}" } : {} },
  };
  return (
    <AuthContext.Provider value={{
      session: localizedSession,
      status: "authenticated",
      isAuthenticated: true,
      error: null,
      logout: async () => undefined,
      refresh: async () => undefined,
      switchAccount: async () => undefined,
    }}>
      <I18nProvider><Greeting /></I18nProvider>
    </AuthContext.Provider>
  );
}

describe("platform locale refresh", () => {
  it("updates rendered translations and the document language on an existing session", () => {
    const { rerender } = render(<View locale="en" />);
    expect(screen.getByText("Hello, ACME")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");

    rerender(<View locale="lt" />);
    expect(screen.getByText("Sveiki, ACME")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("lt");

    rerender(<View locale="en" />);
    expect(screen.getByText("Hello, ACME")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
  });
});
