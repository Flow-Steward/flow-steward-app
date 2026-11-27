import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import type { AuthSession } from "@/lib/auth/AuthProvider";
import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { normalizeFlashMessage } from "@/pages/auth/login/flash";
import { LoginPage } from "@/pages/auth/login/index";
import { PasswordLengthNotice } from "@/pages/auth/login/password-length-notice";

vi.mock("@/lib/auth/useAuth", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

const mockedUseAuth = vi.mocked(useAuth);
const mockedFetchJson = vi.mocked(fetchJson);

function createSession(overrides: Partial<AuthSession> = {}): AuthSession {
  return {
    authenticated: false,
    local_auth_enabled: true,
    bootstrap_required: false,
    external_auth_enabled: false,
    system_email_available: true,
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
    external_sign_in: {},
    environment: {
      edition: "community",
    },
    settings_pages: [],
    registered_pages: [],
    shell_navigation: {},
    ...overrides,
  };
}

type RenderLoginOptions = {
  authenticated?: boolean;
  route?: string;
  sessionOverrides?: Partial<AuthSession>;
};

function renderLogin({
  authenticated = false,
  route = "/login",
  sessionOverrides = {},
}: RenderLoginOptions = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  const refresh = vi.fn(async () => undefined);
  const session = createSession({ authenticated, ...sessionOverrides });

  mockedUseAuth.mockReturnValue({
    error: null,
    isAuthenticated: authenticated,
    logout: vi.fn(async () => undefined),
    refresh,
    switchAccount: vi.fn(async () => undefined),
    session,
    status: authenticated ? "authenticated" : "anonymous",
  });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <PasswordLengthNotice />
        <Routes>
          <Route element={<LoginPage />} path="/login" />
          <Route element={<div>Home destination</div>} path="/" />
          <Route element={<div>Dashboard destination</div>} path="/dashboard" />
          <Route element={<div>Security destination</div>} path="/settings/security" />
          <Route element={<div>Bootstrap destination</div>} path="/bootstrap" />
          <Route element={<div>Reset destination</div>} path="/reset-password" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

  return { refresh };
}

const TEST_PASSWORD = "correct-password"; // pragma: allowlist secret

function fillCredentials(email = "person@example.com", password = TEST_PASSWORD) {
  fireEvent.change(screen.getByLabelText(/^Email address$/i), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(/^Password$/i), { target: { value: password } });
}

function loginError(status: number, payload: Record<string, unknown>) {
  return new HttpError({
    status,
    url: "/api/auth/login",
    payload,
  });
}

describe("LoginPage", () => {
  beforeEach(() => {
    mockedUseAuth.mockReset();
    mockedFetchJson.mockReset();
  });

  it("lets a person signed in externally with roles in two companies choose one", async () => {
    const user = {
      id: "u-1",
      email: "pat@example.com",
      username: "pat@example.com",
      display_name: "Pat",
      roles: ["operator"],
      account_id: "acct-a",
      account_ids: ["acct-a", "acct-b"],
      account_memberships: [
        { account_id: "acct-a", account_name: "Alpha", role: "operator", is_active: true, is_default: true },
        { account_id: "acct-b", account_name: "Beta", role: "admin", is_active: true },
        { account_id: "acct-c", account_name: "Gamma", role: "", is_active: true },
      ],
    };
    renderLogin({
      authenticated: true,
      route: "/login?choose_account=1&next=%2Fdashboard",
      sessionOverrides: { auth_source: "external", user },
    });
    const auth = mockedUseAuth.mock.results[0].value as ReturnType<typeof useAuth>;

    await waitFor(() => expect(screen.getByRole("heading", { name: "Choose an account." })).toBeInTheDocument());
    const select = screen.getByLabelText("Account") as HTMLSelectElement;
    // Only companies with a role; the default one is preselected.
    expect(Array.from(select.options).map((option) => option.textContent)).toEqual(["Choose an account", "Alpha", "Beta"]);
    await waitFor(() => expect(select.value).toBe("acct-a"));

    fireEvent.change(select, { target: { value: "acct-b" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(auth.switchAccount).toHaveBeenCalledWith("acct-b"));
    await waitFor(() => expect(screen.getByText("Dashboard destination")).toBeInTheDocument());
  });

  it("goes straight on when the chooser is asked for but there is one company", async () => {
    renderLogin({ authenticated: true, route: "/login?choose_account=1&next=%2Fdashboard" });
    await waitFor(() => expect(screen.getByText("Dashboard destination")).toBeInTheDocument());
  });

  it("renders the accepted Flow Steward sign-in pattern and local form", () => {
    renderLogin();

    expect(screen.queryByRole("img", { name: "Flow Steward" })).not.toBeInTheDocument();
    expect(screen.queryByText("Steward complex work with clarity.")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText("Welcome back — enter your details to continue.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email address$/i)).toHaveAttribute("autocomplete", "username");
    expect(screen.queryByText("Use the address linked to your workspace.")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/^Email address$/i)).not.toHaveAttribute("aria-describedby");
    expect(screen.getByLabelText(/^Password$/i)).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByLabelText(/^Password$/i)).toHaveAttribute("placeholder", "Enter password");
    expect(screen.getByText("••••••••••")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByRole("checkbox", { name: "Keep me signed in" })).toBeChecked();
    expect(screen.queryByRole("link", { name: /Sign up/i })).not.toBeInTheDocument();
    expect(mockedFetchJson).not.toHaveBeenCalled();
  });

  it("shows design-system field errors for missing credentials", async () => {
    renderLogin();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Enter your email.")).toBeInTheDocument());
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email address$/i)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText(/^Email address$/i)).toHaveAttribute("aria-describedby", "email-error");
    expect(screen.getByLabelText(/^Password$/i)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText(/^Password$/i)).toHaveAttribute("aria-describedby", "password-error");
    expect(mockedFetchJson).not.toHaveBeenCalled();
  });

  it("rejects a malformed email before contacting the API", async () => {
    renderLogin();
    fillCredentials("not-an-email");

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument());
    expect(mockedFetchJson).not.toHaveBeenCalled();
  });

  it("keeps the native password field editable and reveals it without changing its value", async () => {
    const user = userEvent.setup();
    renderLogin();
    const password = screen.getByLabelText(/^Password$/i);
    await user.click(password);
    await user.type(password, "still-secret");

    expect(password).toHaveFocus();
    expect(password).toHaveValue("still-secret");
    await user.keyboard("{Backspace}{Backspace}et");
    expect(password).toHaveValue("still-secret");

    const showPassword = screen.getByRole("button", { name: "Show password" });
    fireEvent.pointerEnter(showPassword);
    expect(showPassword).toHaveAttribute("data-state", "masked");

    fireEvent.click(showPassword);
    expect(password).toHaveAttribute("type", "text");
    expect(password).toHaveValue("still-secret");

    const hidePassword = screen.getByRole("button", { name: "Hide password" });
    expect(hidePassword).toHaveAttribute("data-state", "visible");
    fireEvent.click(hidePassword);
    expect(password).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "Show password" })).toHaveAttribute("data-state", "masked");
  });

  it("collapses a password-manager selection into an editable caret on first focus", async () => {
    renderLogin();
    const password = screen.getByLabelText(/^Password$/i) as HTMLInputElement;
    fireEvent.change(password, { target: { value: "abcdefghij" } });
    password.setSelectionRange(0, 8);

    password.focus();

    await waitFor(() => {
      expect(password).toHaveFocus();
      expect(password.selectionStart).toBe(10);
      expect(password.selectionEnd).toBe(10);
    });
  });

  it("links forgotten-password recovery to the request flow", () => {
    renderLogin();
    expect(screen.getByRole("link", { name: /Forgot password/i })).toHaveAttribute(
      "href",
      "/forgot-password",
    );
  });

  it("offers no emailed recovery when no email extension is connected", () => {
    renderLogin({ sessionOverrides: { system_email_available: false } });
    expect(screen.queryByRole("link", { name: /Forgot password/i })).not.toBeInTheDocument();
  });

  it("renders provider loading and then a safe external sign-in handoff", async () => {
    let resolveProviders: (providers: unknown[]) => void = () => undefined;
    mockedFetchJson.mockReturnValueOnce(new Promise((resolve) => {
      resolveProviders = resolve;
    }));

    renderLogin({
      route: "/login?next=/settings/security",
      sessionOverrides: {
        external_sign_in: { providers_path: "/external-sign-in/providers" },
      },
    });

    // The line waits out the loader delay, then leads with the round mono Loader.
    await waitFor(() => expect(screen.getByText("Loading sign-in providers…")).toBeInTheDocument());
    expect(screen.getByLabelText(/^Email address$/i)).toBeInTheDocument();
    resolveProviders([{
      provider_key: "okta",
      display_name: "Okta",
      login_path: "/external-sign-in/okta/login",
    }]);

    await waitFor(() => expect(screen.getByRole("link", { name: "Continue with Okta" })).toHaveAttribute(
      "href",
      "/external-sign-in/okta/login?next=%2Fsettings%2Fsecurity",
    ));
    expect(screen.queryByText("You’ll return to the page you were viewing.")).not.toBeInTheDocument();
  });

  it("keeps password sign-in available when provider discovery fails", async () => {
    mockedFetchJson.mockRejectedValueOnce(new Error("provider outage"));
    renderLogin({
      sessionOverrides: {
        external_sign_in: { providers_path: "/external-sign-in/providers" },
      },
    });

    await waitFor(() => expect(screen.getByText("No external providers are available.")).toBeInTheDocument());
    expect(screen.getByText("You can still use your email and password.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email address$/i)).toBeInTheDocument();
  });

  it("drops unsafe provider paths instead of rendering an open redirect", async () => {
    mockedFetchJson.mockResolvedValueOnce([{
      provider_key: "unsafe",
      display_name: "Unsafe provider",
      login_path: "//attacker.example/login",
    }]);
    renderLogin({
      sessionOverrides: {
        external_sign_in: { providers_path: "/external-sign-in/providers" },
      },
    });

    await waitFor(() => expect(screen.getByText("No external providers are available.")).toBeInTheDocument());
    expect(screen.queryByRole("link", { name: /Unsafe provider/i })).not.toBeInTheDocument();
  });

  it("supports external-only deployments with a configured provider", async () => {
    mockedFetchJson.mockResolvedValueOnce([{
      provider_key: "microsoft",
      display_name: "Microsoft",
      login_path: "/external-sign-in/microsoft/login",
    }]);
    renderLogin({
      sessionOverrides: {
        external_auth_enabled: true,
        external_sign_in: { providers_path: "/external-sign-in/providers" },
        local_auth_enabled: false,
      },
    });

    await waitFor(() => expect(screen.getByRole("link", { name: "Continue with Microsoft" })).toBeInTheDocument());
    expect(screen.queryByLabelText(/^Email address$/i)).not.toBeInTheDocument();
    expect(screen.getByText("Password sign-in is unavailable.")).toBeInTheDocument();
  });

  it("asks for the company before listing a company's sign-in options", async () => {
    const user = userEvent.setup();
    mockedFetchJson
      .mockResolvedValueOnce({ options: [], account_hint_supported: true })
      .mockResolvedValueOnce({
        options: [{ provider_key: "acme-ticket:main", display_name: "Acme ID", login_path: "/api/auth/external/start?method=acme-ticket&key=main" }],
        account_hint_supported: true,
      });
    renderLogin({
      route: "/login?next=/settings/security",
      sessionOverrides: {
        external_sign_in: { providers_path: "/api/auth/external/options" },
      },
    });

    const company = await screen.findByLabelText("Company identifier");
    expect(mockedFetchJson).toHaveBeenLastCalledWith("/api/auth/external/options");
    expect(screen.queryByText("No external providers are available.")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Acme ID/ })).not.toBeInTheDocument();

    await user.type(company, "acme corp");
    await user.click(screen.getByRole("button", { name: "Company sign-in" }));

    await waitFor(() => expect(screen.getByRole("link", { name: "Continue with Acme ID" })).toHaveAttribute(
      "href",
      "/api/auth/external/start?method=acme-ticket&key=main&next=%2Fsettings%2Fsecurity",
    ));
    expect(mockedFetchJson).toHaveBeenLastCalledWith("/api/auth/external/options?account=acme%20corp");
  });

  it("says when a company has no sign-in options", async () => {
    const user = userEvent.setup();
    mockedFetchJson
      .mockResolvedValueOnce({ options: [], account_hint_supported: true })
      .mockResolvedValueOnce({ options: [], account_hint_supported: true });
    renderLogin({
      sessionOverrides: {
        external_sign_in: { providers_path: "/api/auth/external/options" },
      },
    });

    await user.type(await screen.findByLabelText("Company identifier"), "nobody");
    await user.click(screen.getByRole("button", { name: "Company sign-in" }));

    await waitFor(() => expect(screen.getByText("No sign-in options were found for this company.")).toBeInTheDocument());
    expect(screen.getByLabelText("Company identifier")).toHaveValue("nobody");
  });

  it("lists installation-wide options without a company step", async () => {
    mockedFetchJson.mockResolvedValueOnce({
      options: [{ provider_key: "header:trusted", display_name: "Company network", login_path: "/api/auth/external/start?method=header&key=trusted" }],
      account_hint_supported: false,
    });
    renderLogin({
      sessionOverrides: {
        external_sign_in: { providers_path: "/api/auth/external/options" },
      },
    });

    await waitFor(() => expect(screen.getByRole("link", { name: "Continue with Company network" })).toBeInTheDocument());
    expect(screen.queryByLabelText("Company identifier")).not.toBeInTheDocument();
  });

  it("explains an external-only deployment with no configured provider path", () => {
    renderLogin({
      sessionOverrides: {
        external_auth_enabled: true,
        local_auth_enabled: false,
      },
    });

    expect(screen.queryByLabelText(/^Email address$/i)).not.toBeInTheDocument();
    expect(screen.getByText("No external sign-in flow is configured. Contact your workspace administrator.")).toBeInTheDocument();
    expect(mockedFetchJson).not.toHaveBeenCalled();
  });

  it("shows a single recovery message when external-only provider discovery fails", async () => {
    mockedFetchJson.mockRejectedValueOnce(new Error("provider outage"));
    renderLogin({
      sessionOverrides: {
        external_sign_in: { providers_path: "/external-sign-in/providers" },
        local_auth_enabled: false,
      },
    });

    await waitFor(() => expect(screen.getByText("No external providers are available.")).toBeInTheDocument());
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.queryByText("No external sign-in flow is configured. Contact your workspace administrator.")).not.toBeInTheDocument();
  });

  it("completes the happy flow, refreshes auth, and returns to the requested route", async () => {
    mockedFetchJson.mockResolvedValueOnce({ user: { username: "person@example.com" } });
    const { refresh } = renderLogin({ route: "/login?next=/settings/security" });
    fillCredentials();
    fireEvent.click(screen.getByRole("checkbox", { name: /Keep me signed in/i }));

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Security destination")).toBeInTheDocument());
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: "person@example.com",
        password: TEST_PASSWORD,
        remember_me: false,
        account_id: undefined,
      }),
    });
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it.each([
    { characterCount: 11, warns: true },
    { characterCount: 12, warns: false },
    { characterCount: 13, warns: false },
  ])("keeps the destination and warns as needed after sign-in with a $characterCount-character password", async ({ characterCount, warns }) => {
    mockedFetchJson.mockResolvedValueOnce({ user: { username: "person@example.com" } });
    renderLogin({ route: "/login?next=/settings/security" });
    fillCredentials("person@example.com", "a".repeat(characterCount));

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Security destination")).toBeInTheDocument());
    const changePassword = screen.queryByRole("link", { name: "Change password" });
    if (warns) {
      expect(screen.getByText("Your password is shorter than 12 characters. Choose a longer password.")).toBeInTheDocument();
      expect(changePassword).toHaveAttribute("href", "/settings/account?tab=signin");
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
      expect(screen.queryByRole("link", { name: "Change password" })).not.toBeInTheDocument();
      expect(screen.getByText("Security destination")).toBeInTheDocument();
    } else {
      expect(changePassword).not.toBeInTheDocument();
    }
  });

  it("shows the expiry warning at the requested destination", async () => {
    mockedFetchJson.mockResolvedValueOnce({ user: { username: "person@example.com" }, password_expires_in_days: 14 });
    renderLogin({ route: "/login?next=/settings/security" });
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Security destination")).toBeInTheDocument());
    expect(screen.getByText("Your password expires in 14 days. Change it before it expires.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Change password" })).toHaveAttribute("href", "/settings/account?tab=signin");
  });

  it("keeps the replacement password while prompting for a second factor", async () => {
    mockedFetchJson
      .mockRejectedValueOnce(loginError(403, {
        error: "Password change required",
        password_change_required: true,
        password_expired: true,
        password_min_length: 12,
      }))
      .mockRejectedValueOnce(loginError(403, { error: "Multi-factor authentication is required", mfa_required: true }))
      .mockResolvedValueOnce({ user: { username: "person@example.com" } });
    const { refresh } = renderLogin();
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Your password has expired. Choose a new password to sign in.")).toBeInTheDocument());
    const replacement = "test-Replacement1!";
    fireEvent.change(screen.getByLabelText(/^New password$/i), { target: { value: replacement } });
    fireEvent.change(screen.getByLabelText(/^Confirm password$/i), { target: { value: replacement } });
    fireEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));

    const factor = await screen.findByLabelText("Authenticator or recovery code");
    expect(screen.getByLabelText(/^New password$/i)).toHaveValue(replacement);
    expect(refresh).not.toHaveBeenCalled();
    fireEvent.change(factor, { target: { value: "abcd-efgh-jkmn" } });
    fireEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));

    await waitFor(() => expect(screen.getByText("Home destination")).toBeInTheDocument());
    expect(mockedFetchJson).toHaveBeenLastCalledWith("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: "person@example.com",
        password: TEST_PASSWORD,
        remember_me: true,
        new_password: replacement,
        mfa_code: "abcd-efgh-jkmn",
      }),
    });
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("explains how to recover access if Authenticator enrollment is required", async () => {
    mockedFetchJson.mockRejectedValueOnce(loginError(403, {
      error: "Authenticator enrollment is required",
      mfa_enrollment_required: true,
    }));
    const { refresh } = renderLogin();
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Contact your workspace administrator to set up multi-factor authentication.")).toBeInTheDocument());
    expect(screen.queryByLabelText("Authenticator or recovery code")).not.toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("enrolls an authenticator at sign-in and shows recovery codes before continuing", async () => {
    const token = "a".repeat(64);
    const setupKey = "EXAMPLESETUPKEY";
    const recoveryCode = "abcd-efgh-jkmn";
    mockedFetchJson
      .mockRejectedValueOnce(loginError(403, {
        mfa_enrollment_required: true,
        authenticator_enrollment: { token, secret: setupKey, uri: `otpauth://fixture/Flow%20Steward?secret=${setupKey}` },
      }))
      .mockRejectedValueOnce(loginError(403, { mfa_required: true }))
      .mockResolvedValueOnce({ recovery_codes: [recoveryCode], user: { username: "person@example.com" } });
    const { refresh } = renderLogin();
    fillCredentials();
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByRole("img", { name: "Authenticator setup QR code" })).toBeVisible());
    expect(screen.getByText(setupKey)).toBeVisible();
    const code = screen.getByLabelText("Authenticator code", { exact: true });
    fireEvent.change(code, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and sign in" }));
    await waitFor(() => expect(screen.getByText("The code could not be verified. Enter a new code and try again.")).toBeVisible());
    expect(refresh).not.toHaveBeenCalled();

    fireEvent.change(code, { target: { value: "654321" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and sign in" }));
    await waitFor(() => expect(screen.getByText(recoveryCode)).toBeVisible());
    expect(screen.queryByText(setupKey)).not.toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
    expect(mockedFetchJson).toHaveBeenLastCalledWith("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: "person@example.com",
        password: TEST_PASSWORD,
        remember_me: true,
        mfa_code: "654321",
        authenticator_enrollment_token: token,
      }),
    });

    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(screen.getByText("Home destination")).toBeVisible());
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("uses the canonical in-button loader while sign-in is pending", async () => {
    mockedFetchJson.mockReturnValueOnce(new Promise(() => undefined));
    renderLogin();
    fillCredentials();

    const submit = screen.getByRole("button", { name: "Sign in" });
    fireEvent.click(submit);

    await waitFor(() => expect(submit).toHaveAttribute("aria-busy", "true"));
    expect(submit).toBeDisabled();
    // Public Auth submit exception: after the loader delay the round mono Loader stands centred.
    const loader = await screen.findByTestId("auth-submit-spinner");
    expect(within(loader).getByTestId("loader-mark")).toHaveClass("fs-loader-orbit");
    expect(within(loader).getByTestId("loader-mark")).not.toHaveClass("md");
    expect(submit).toHaveAttribute("data-loading", "true");
    expect(within(submit).getByText("Sign in")).toBeInTheDocument();
  });

  it("keeps the button loading during session refresh and navigates without a success screen", async () => {
    let resolveRefresh: () => void = () => undefined;
    const refresh = vi.fn(() => new Promise<void>((resolve) => {
      resolveRefresh = resolve;
    }));
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: false,
      logout: vi.fn(async () => undefined),
      refresh,
      switchAccount: vi.fn(async () => undefined),
      session: createSession(),
      status: "anonymous",
    });
    mockedFetchJson.mockResolvedValueOnce({ user: { username: "person@example.com" } });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/login"]}>
          <Routes>
            <Route element={<LoginPage />} path="/login" />
            <Route element={<div>Home destination</div>} path="/" />
            <Route element={<div>Dashboard destination</div>} path="/dashboard" />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fillCredentials();

    const submit = screen.getByRole("button", { name: "Sign in" });
    fireEvent.click(submit);

    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    expect(submit).toHaveAttribute("aria-busy", "true");
    expect(submit).toBeDisabled();
    await waitFor(() => expect(screen.getByTestId("auth-submit-spinner")).toBeInTheDocument());
    expect(screen.getByLabelText(/^Email address$/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Welcome back" })).not.toBeInTheDocument();
    expect(screen.queryByText("Taking you to your dashboard…")).not.toBeInTheDocument();
    expect(screen.queryByText("Home destination")).not.toBeInTheDocument();

    resolveRefresh();
    await waitFor(() => expect(screen.getByText("Home destination")).toBeInTheDocument());
  });

  it("without next, a successful sign-in goes to the role-based home, never straight to the Dashboard", async () => {
    // ADR 0075: the index (HomeGate) decides the landing; an operator or viewer
    // must not be sent into the admin Dashboard by the login fallback.
    mockedFetchJson.mockResolvedValueOnce({ user: { username: "operator@example.com" } });
    renderLogin({ sessionOverrides: { roles: ["operator"] } });
    fillCredentials("operator@example.com");

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Home destination")).toBeInTheDocument());
    expect(screen.queryByText("Dashboard destination")).not.toBeInTheDocument();
  });

  it("keeps a safe explicit next after sign-in", async () => {
    mockedFetchJson.mockResolvedValueOnce({ user: { username: "person@example.com" } });
    renderLogin({ route: "/login?next=/settings/security" });
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Security destination")).toBeInTheDocument());
  });

  it("sends the token of an invitation link being opened, and only then (ADR 0085, P5)", async () => {
    mockedFetchJson.mockResolvedValue({ user: { username: "person@example.com" } });
    renderLogin({ route: `/login?next=${encodeURIComponent("/invitations/tok-123")}` });
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalled());
    const [, init] = mockedFetchJson.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body)).invitation_token).toBe("tok-123");
  });

  it("sends no invitation token for any other destination", async () => {
    mockedFetchJson.mockResolvedValue({ user: { username: "person@example.com" } });
    renderLogin({ route: "/login?next=/settings/security" });
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalled());
    const [, init] = mockedFetchJson.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).not.toHaveProperty("invitation_token");
  });

  it("rejects an unsafe next route and returns a successful sign-in to the role-based home", async () => {
    mockedFetchJson.mockResolvedValueOnce({ user: { username: "person@example.com" } });
    renderLogin({ route: "/login?next=/%5Cattacker.example" });
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Home destination")).toBeInTheDocument());
  });

  it("requests account selection, retains credentials, validates it, and then completes sign-in", async () => {
    mockedFetchJson
      .mockRejectedValueOnce(loginError(409, {
        error: "Account selection required",
        requires_account_selection: true,
        account_options: [
          { account_id: "platform", display_name: "Flow Steward Platform" },
          { account_id: "portal", account_name: "Customer Portal" },
        ],
      }))
      .mockResolvedValueOnce({ user: { username: "person@example.com" } });
    renderLogin();
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const accountSelect = await screen.findByRole("combobox", { name: "Account" });
    expect(screen.getByRole("option", { name: "Flow Steward Platform" })).toBeInTheDocument();
    expect(screen.getByText("Choose an account.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email address$/i)).toHaveValue("person@example.com");
    expect(screen.getByLabelText(/^Password$/i)).toHaveValue(TEST_PASSWORD);

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.getByText("Choose an account to continue.")).toBeInTheDocument());
    expect(mockedFetchJson).toHaveBeenCalledTimes(1);

    fireEvent.change(accountSelect, { target: { value: "platform" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Home destination")).toBeInTheDocument());
    expect(mockedFetchJson).toHaveBeenLastCalledWith("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: "person@example.com",
        password: TEST_PASSWORD,
        remember_me: true,
        account_id: "platform",
      }),
    });
  });

  it("recovers when an account-selection response has no usable options", async () => {
    mockedFetchJson.mockRejectedValueOnce(loginError(409, {
      error: "Account selection required",
      requires_account_selection: true,
      account_options: [],
    }));
    renderLogin();
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Account selection is temporarily unavailable.")).toBeInTheDocument());
    expect(screen.getByText("Try again or contact your workspace administrator.")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Account" })).not.toBeInTheDocument();
  });

  it.each([
    [
      "Selected account is not available for this user",
      "That account is unavailable.",
      "Choose an account linked to this sign-in and try again.",
    ],
    [
      "Selected account is disabled",
      "This account is disabled.",
      "Choose another account or contact your workspace administrator.",
    ],
  ])("handles account access failure: %s", async (serverMessage, title, copy) => {
    mockedFetchJson.mockRejectedValueOnce(loginError(403, { error: serverMessage }));
    renderLogin();
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText(title)).toBeInTheDocument());
    expect(screen.getByText(copy)).toBeInTheDocument();
  });

  it("renders invalid credentials without exposing backend details", async () => {
    mockedFetchJson.mockRejectedValueOnce(loginError(401, { error: "Invalid credentials" }));
    renderLogin();
    fillCredentials("person@example.com", "a".repeat(11));

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("We couldn’t sign you in.")).toBeInTheDocument());
    expect(screen.getByText("Check your email and password, then try again.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email address$/i)).toHaveValue("person@example.com");
    expect(screen.queryByRole("link", { name: "Change password" })).not.toBeInTheDocument();
  });

  it("renders throttling with the backend retry interval", async () => {
    mockedFetchJson.mockRejectedValueOnce(loginError(429, {
      error: "Too many attempts",
      retry_after_seconds: 37,
    }));
    renderLogin();
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Too many sign-in attempts.")).toBeInTheDocument());
    expect(screen.getByText("Try again in 37 seconds.")).toBeInTheDocument();
  });

  it("keeps input values available after a service failure", async () => {
    mockedFetchJson.mockRejectedValueOnce(loginError(503, { error: "Database not configured" }));
    renderLogin();
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Sign-in is temporarily unavailable.")).toBeInTheDocument());
    expect(screen.getByText("Your details are still here. Try again in a moment.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email address$/i)).toHaveValue("person@example.com");
  });

  it("handles a network failure with a recovery-oriented message", async () => {
    mockedFetchJson.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    renderLogin();
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("We couldn’t reach Flow Steward.")).toBeInTheDocument());
    expect(screen.getByText("Check your connection and try again.")).toBeInTheDocument();
  });

  it("handles a stale local-auth capability response from the server", async () => {
    mockedFetchJson.mockRejectedValueOnce(loginError(400, { error: "Local auth is disabled" }));
    renderLogin();
    fillCredentials();

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByText("Password sign-in is unavailable.")).toBeInTheDocument());
    expect(screen.getByText("Use a configured external provider to continue.")).toBeInTheDocument();
  });

  it("replaces a temporary password in place and then signs in, without the email reset", async () => {
    const temporary = "temporary-pass-1"; // pragma: allowlist secret
    const permanent = "permanent-pass-22"; // pragma: allowlist secret
    mockedFetchJson
      .mockRejectedValueOnce(loginError(403, {
        error: "Password change required",
        password_change_required: true,
        password_min_length: 12,
      }))
      .mockResolvedValueOnce({ user: { username: "person@example.com" } });
    const { refresh } = renderLogin({ sessionOverrides: { system_email_available: false } });
    fillCredentials("person@example.com", temporary);

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: "Set a new password" })).toBeInTheDocument());
    expect(screen.queryByText("Reset destination")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Forgot password?" })).not.toBeInTheDocument();
    expect(screen.getByLabelText(/^Temporary password$/i)).toHaveValue(temporary);
    expect(screen.getByText("Use at least 12 characters, including uppercase and lowercase letters, a number and a special character.")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/^New password$/i), { target: { value: "short-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    await waitFor(() => expect(screen.getByText("Use at least 12 characters.", { selector: "[id='new-password-error']" })).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/^New password$/i), { target: { value: temporary } });
    fireEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    await waitFor(() => expect(screen.getByText("Choose a password different from the temporary one.")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/^New password$/i), { target: { value: permanent } });
    fireEvent.change(screen.getByLabelText(/^Confirm password$/i), { target: { value: "permanent-pass-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    await waitFor(() => expect(screen.getByText("Passwords do not match.")).toBeInTheDocument());
    expect(mockedFetchJson).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText(/^Confirm password$/i), { target: { value: permanent } });
    fireEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));

    await waitFor(() => expect(screen.getByText("Home destination")).toBeInTheDocument());
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(mockedFetchJson).toHaveBeenCalledTimes(2);
    const [, init] = mockedFetchJson.mock.calls[1] ?? [];
    expect(JSON.parse(String(init?.body))).toMatchObject({
      email: "person@example.com",
      password: temporary,
      new_password: permanent,
    });
  });

  it("shows the server's refusal of a new password on the new password field", async () => {
    mockedFetchJson
      .mockRejectedValueOnce(loginError(403, {
        error: "Password change required",
        password_change_required: true,
        password_min_length: 10,
      }))
      .mockRejectedValueOnce(loginError(400, {
        error: "new_password must be at least 14 characters",
        field: "new_password",
      }));
    renderLogin();
    fillCredentials();
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("heading", { name: "Set a new password" });

    fireEvent.change(screen.getByLabelText(/^New password$/i), { target: { value: "a-new-password" } });
    fireEvent.change(screen.getByLabelText(/^Confirm password$/i), { target: { value: "a-new-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));

    await waitFor(() => expect(screen.getByText("new_password must be at least 14 characters")).toBeInTheDocument());
    expect(screen.queryByText("Home destination")).not.toBeInTheDocument();
  });

  it("renders normalized flash feedback as an informational notice", () => {
    renderLogin({ route: "/login?flash=External+sign-in+session+expired" });

    expect(screen.getByText("Sign-in update")).toBeInTheDocument();
    expect(screen.getByText("External sign-in session expired")).toBeInTheDocument();
  });

  it("redirects an existing authenticated session to the safe requested route", () => {
    renderLogin({ authenticated: true, route: "/login?next=/settings/security" });
    expect(screen.getByText("Security destination")).toBeInTheDocument();
  });

  it("redirects bootstrap-required deployments before rendering sign-in", () => {
    renderLogin({ sessionOverrides: { bootstrap_required: true } });
    expect(screen.getByText("Bootstrap destination")).toBeInTheDocument();
  });
});

describe("normalizeFlashMessage", () => {
  it("returns empty message for blank values", () => {
    expect(normalizeFlashMessage(null)).toBe("");
    expect(normalizeFlashMessage("")).toBe("");
    expect(normalizeFlashMessage("   ")).toBe("");
  });

  it("converts plus signs to spaces and trims", () => {
    expect(normalizeFlashMessage("External+sign-in+session+expired")).toBe(
      "External sign-in session expired",
    );
  });

  it("caps message length to avoid oversized query payload rendering", () => {
    const longMessage = "x".repeat(500);
    expect(normalizeFlashMessage(longMessage)).toHaveLength(240);
  });
});
