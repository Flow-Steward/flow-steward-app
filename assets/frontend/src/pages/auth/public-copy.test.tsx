import { createElement } from "react";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import type { AuthSession } from "@/lib/auth/AuthProvider";
import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { BootstrapPage } from "@/pages/auth/bootstrap";
import { LoginPage } from "@/pages/auth/login";
import { ResetPasswordPage } from "@/pages/auth/reset-password";

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
    environment: {
      edition: "community",
    },
    settings_pages: [],
    registered_pages: [],
    shell_navigation: {},
    ...overrides,
  };
}

function renderWithProviders(node: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  return render(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(MemoryRouter, null, node),
    ),
  );
}

describe("auth public copy", () => {
  beforeEach(() => {
    mockedUseAuth.mockReset();
    mockedFetchJson.mockReset();
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: false,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession(),
      status: "anonymous",
    });
  });

  it("uses the approved unbranded login header", () => {
    renderWithProviders(createElement(LoginPage));

    expect(screen.queryByRole("img", { name: "Flow Steward" })).not.toBeInTheDocument();
    expect(screen.queryByText("Steward complex work with clarity.")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText("Welcome back — enter your details to continue.")).toBeInTheDocument();
  });

  it("uses Flow Steward branding in password reset confirmation", async () => {
    mockedFetchJson.mockResolvedValueOnce({ status: "ok" } as never);

    renderWithProviders(createElement(ResetPasswordPage));

    fireEvent.change(screen.getByLabelText(/Email/i), {
      target: { value: "person@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(
      await screen.findByText((content) =>
        content.includes("exists in Flow Steward, the reset instructions are on the way."),
      ),
    ).toBeInTheDocument();
  });

  it("opens ordinary password recovery with an empty focused email field", async () => {
    render(
      createElement(
        QueryClientProvider,
        { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
        createElement(
          MemoryRouter,
          { initialEntries: ["/forgot-password?email=must-not-carry@example.com"] },
          createElement(ResetPasswordPage),
        ),
      ),
    );

    const email = screen.getByLabelText("Email address");
    expect(email).toHaveValue("");
    await waitFor(() => expect(email).toHaveFocus());
    expect(email).not.toHaveAttribute("placeholder");
  });

  it("sends people to an administrator when no email extension is connected", () => {
    mockedUseAuth.mockReturnValue({
      ...mockedUseAuth(),
      session: createSession({ system_email_available: false }),
    });
    renderWithProviders(createElement(ResetPasswordPage));

    expect(screen.getByRole("heading", { name: "Ask your administrator" })).toBeInTheDocument();
    expect(screen.getByText(/set a new password for you in Settings → Users/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Email address")).not.toBeInTheDocument();
  });

  it("shows the API Retry-After interval without clearing the reset email", async () => {
    mockedFetchJson.mockRejectedValueOnce(new HttpError({
      headers: { "Retry-After": "37" },
      status: 429,
      url: "/api/auth/password-reset/request",
      payload: { error: "Too many requests" },
    }) as never);
    renderWithProviders(createElement(ResetPasswordPage));

    const email = screen.getByLabelText("Email address");
    fireEvent.change(email, { target: { value: "person@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(await screen.findByText("Too many reset requests.")).toBeInTheDocument();
    expect(screen.getByText("Try again in 37 seconds.")).toBeInTheDocument();
    expect(email).toHaveValue("person@example.com");
  });

  it("uses the canonical in-button loader while requesting a reset link", async () => {
    mockedFetchJson.mockReturnValueOnce(new Promise(() => undefined));
    renderWithProviders(createElement(ResetPasswordPage));

    fireEvent.change(screen.getByLabelText("Email address"), {
      target: { value: "person@example.com" },
    });
    const submit = screen.getByRole("button", { name: "Send reset link" });
    fireEvent.click(submit);

    await waitFor(() => expect(submit).toHaveAttribute("aria-busy", "true"));
    expect(submit).toBeDisabled();
    expect(screen.getByTestId("auth-submit-spinner")).toBeInTheDocument();
    expect(within(submit).getByText("Send reset link")).toBeInTheDocument();
  });

  it("shows password reset request API failures", async () => {
    mockedFetchJson.mockRejectedValueOnce(new HttpError({
      status: 503,
      url: "/api/auth/password-reset/request",
      payload: { error: "Database not configured" },
    }) as never);

    renderWithProviders(createElement(ResetPasswordPage));

    fireEvent.change(screen.getByLabelText(/Email/i), {
      target: { value: "person@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(await screen.findByText("Password recovery is temporarily unavailable.")).toBeInTheDocument();
    expect(screen.getByText("Your email is still here. Try again in a moment.")).toBeInTheDocument();
  });

  it("confirms a token reset with password confirmation and session revocation", async () => {
    mockedFetchJson
      .mockResolvedValueOnce({ valid: true, email: "person@example.com" } as never)
      .mockResolvedValueOnce({ status: "password_reset_confirmed", revoked_sessions: 2 } as never);

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: {
          retry: false,
        },
      },
    });
    render(
      createElement(
        QueryClientProvider,
        { client: queryClient },
        createElement(MemoryRouter, { initialEntries: ["/reset-password?token=reset-token"] }, createElement(ResetPasswordPage)),
      ),
    );

    expect(await screen.findByText("Resetting password for person@example.com.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/^New password$/i), {
      target: { value: "N3wStr0ngP@ss!!" },
    });
    fireEvent.change(screen.getByLabelText(/^Confirm password$/i), {
      target: { value: "N3wStr0ngP@ss!!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Update password" }));

    await screen.findByText(/Password updated. 2 other sessions revoked./i);
    expect(mockedFetchJson).toHaveBeenNthCalledWith(1, "/api/auth/password-reset/verify", {
      method: "POST",
      body: JSON.stringify({ token: "reset-token" }),
    });
    expect(mockedFetchJson).toHaveBeenNthCalledWith(2, "/api/auth/password-reset/confirm", {
      method: "POST",
      body: JSON.stringify({
        token: "reset-token",
        new_password: "N3wStr0ngP@ss!!",
        revoke_other_sessions: true,
      }),
    });
  });

  it("verifies reset tokens before showing password fields", async () => {
    mockedFetchJson.mockResolvedValueOnce({ valid: false } as never);

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      createElement(
        QueryClientProvider,
        { client: queryClient },
        createElement(MemoryRouter, { initialEntries: ["/reset-password?token=fake-token"] }, createElement(ResetPasswordPage)),
      ),
    );

    expect(await screen.findByText("This password reset link is invalid or expired.")).toBeInTheDocument();
    expect(screen.queryByLabelText(/New password/i)).not.toBeInTheDocument();
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/auth/password-reset/verify", {
      method: "POST",
      body: JSON.stringify({ token: "fake-token" }),
    });
  });

  it("uses Flow Steward branding in bootstrap", () => {
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: false,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: createSession({ bootstrap_required: true }),
      status: "anonymous",
    });

    renderWithProviders(createElement(BootstrapPage));

    expect(
      screen.getByRole("heading", { name: "Create the initial Flow Steward admin account" }),
    ).toBeInTheDocument();
  });
});
