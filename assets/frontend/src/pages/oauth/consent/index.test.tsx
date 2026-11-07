import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { HttpError } from "@/lib/http/errors";
import { useAuth } from "@/lib/auth/useAuth";
import { OAuthConsentPage } from "@/pages/oauth/consent";
import { decideOAuthConsent, fetchOAuthConsent } from "@/pages/settings/shared/api";
import type { OAuthConsentView } from "@/pages/settings/shared/types";

vi.mock("@/lib/auth/useAuth", () => ({ useAuth: vi.fn() }));
vi.mock("@/pages/projects/list/api", () => ({
  fetchProjectsList: vi.fn(async () => ({ projects: [{ id: "p-1", name: "Shop" }] })),
}));
vi.mock("@/pages/settings/shared/api", () => ({
  decideOAuthConsent: vi.fn(),
  fetchOAuthConsent: vi.fn(),
}));

const mockedUseAuth = vi.mocked(useAuth);
const mockedFetchConsent = vi.mocked(fetchOAuthConsent);
const mockedDecide = vi.mocked(decideOAuthConsent);

function authState(isAuthenticated: boolean) {
  return {
    error: null,
    isAuthenticated,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    session: null,
    status: isAuthenticated ? "authenticated" : "anonymous",
  } as unknown as ReturnType<typeof useAuth>;
}

function view(overrides: Partial<OAuthConsentView> = {}): OAuthConsentView {
  return {
    request_id: "req-1",
    expires_at: "2026-10-31T12:10:00Z",
    client: {
      client_id: "https://claude.ai/oauth/mcp-client",
      client_name: "Claude",
      client_host: "claude.ai",
      redirect_uri: "https://claude.ai/api/mcp/auth_callback",
      redirect_uri_host: "claude.ai",
      redirect_uri_loopback: false,
    },
    requested_scopes: ["projects.read", "jobs.read", "admin:all"],
    presets: [
      { id: "operational", scopes: ["projects.read", "jobs.read"], available: true, reason: "", subset_of_preset: true },
      { id: "full_authoring", scopes: ["admin:all"], available: false, reason: "exceeds_authority", subset_of_preset: false },
    ],
    default_preset: "operational",
    expiry_days_options: [7, 30, 90],
    default_expiry_days: 30,
    ...overrides,
  };
}

function LoginProbe() {
  const location = useLocation();
  return <div>login page {location.search}</div>;
}

function mountConsent(entry: string, onRedirect = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/oauth/consent" element={<OAuthConsentPage onRedirect={onRedirect} />} />
          <Route path="/login" element={<LoginProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return onRedirect;
}

describe("OAuthConsentPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedUseAuth.mockReturnValue(authState(true));
  });

  it("renders the error variant without a session", async () => {
    mockedUseAuth.mockReturnValue(authState(false));
    mountConsent("/oauth/consent?error=public_https_required");
    expect(await screen.findByText("A public HTTPS address is required")).toBeInTheDocument();
    expect(mockedFetchConsent).not.toHaveBeenCalled();
  });

  it.each([
    ["disabled_by_admin", "Cloud AI apps are turned off"],
    ["client_not_allowed", "This app is not supported"],
    ["invalid_redirect_uri", "The return address is not accepted"],
    ["something_new", "The app could not connect"],
  ])("explains the %s error", async (code, title) => {
    mountConsent(`/oauth/consent?error=${code}`);
    expect(await screen.findByText(title)).toBeInTheDocument();
  });

  it("sends an unauthenticated request to login with the consent path as next", async () => {
    mockedUseAuth.mockReturnValue(authState(false));
    mountConsent("/oauth/consent?request=req-1");
    expect(await screen.findByText(/login page/)).toHaveTextContent(
      `?next=${encodeURIComponent("/oauth/consent?request=req-1")}`,
    );
    expect(mockedFetchConsent).not.toHaveBeenCalled();
  });

  it("offers the requested subset and disables a preset above the user's authority", async () => {
    mockedFetchConsent.mockResolvedValue(view());
    mountConsent("/oauth/consent?request=req-1");
    expect(await screen.findByText("Allow Claude to access Flow Steward?")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Monitor and run workflows" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Full authoring" })).toBeDisabled();
    expect(screen.getByText("Only the access this app requested:")).toBeInTheDocument();
    expect(screen.getByText("Not available: this access is more than your own role allows.")).toBeInTheDocument();
    expect(screen.getByText(/It is not a narrow permission/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "30 days" })).toBeChecked();
    expect(screen.queryByText("The app returns to this computer")).not.toBeInTheDocument();
  });

  it("names an administrator, not a published identity, for a platform client", async () => {
    mockedFetchConsent.mockResolvedValue(view({
      client: {
        client_id: "fs-platform-m365-0123456789abcdef0123456789abcdef",
        client_name: "Microsoft 365 Copilot",
        client_host: "",
        client_kind: "platform",
        redirect_uri: "https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect",
        redirect_uri_host: "teams.microsoft.com",
        redirect_uri_loopback: false,
      },
    }));
    mountConsent("/oauth/consent?request=req-1");
    expect(await screen.findByText("Allow Microsoft 365 Copilot to access Flow Steward?")).toBeInTheDocument();
    expect(screen.getByText("Registered by")).toBeInTheDocument();
    expect(screen.getByText("An administrator of this Flow Steward installation")).toBeInTheDocument();
    expect(screen.queryByText("Identity published by")).not.toBeInTheDocument();
    expect(screen.getByText("teams.microsoft.com")).toBeInTheDocument();
  });

  it("warns when the redirect goes to this computer", async () => {
    mockedFetchConsent.mockResolvedValue(view({
      client: { ...view().client, redirect_uri: "http://127.0.0.1:53111/callback", redirect_uri_host: "127.0.0.1", redirect_uri_loopback: true },
    }));
    mountConsent("/oauth/consent?request=req-1");
    expect(await screen.findByText("The app returns to this computer")).toBeInTheDocument();
  });

  it("offers only Deny when nothing can be granted", async () => {
    mockedFetchConsent.mockResolvedValue(view({
      presets: [{ id: "full_authoring", scopes: ["admin:all"], available: false, reason: "exceeds_authority", subset_of_preset: false }],
      default_preset: null,
    }));
    mountConsent("/oauth/consent?request=req-1");
    expect(await screen.findByText("Nothing can be granted")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Allow access" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deny" })).toBeEnabled();
  });

  it("approves with the chosen preset, duration and no project limit, then redirects", async () => {
    mockedFetchConsent.mockResolvedValue(view());
    mockedDecide.mockResolvedValue({ redirect_to: "https://claude.ai/api/mcp/auth_callback?code=x&state=s&iss=https%3A%2F%2Ffs.example" });
    const onRedirect = mountConsent("/oauth/consent?request=req-1");
    fireEvent.click(await screen.findByRole("radio", { name: "90 days" }));
    fireEvent.click(screen.getByRole("button", { name: "Allow access" }));
    await waitFor(() => expect(onRedirect).toHaveBeenCalledWith("https://claude.ai/api/mcp/auth_callback?code=x&state=s&iss=https%3A%2F%2Ffs.example"));
    expect(mockedDecide).toHaveBeenCalledTimes(1);
    expect(mockedDecide).toHaveBeenCalledWith("req-1", { decision: "approve", preset: "operational", expiry_days: 90, project_ids: [] });
  });

  it("denies and redirects", async () => {
    mockedFetchConsent.mockResolvedValue(view());
    mockedDecide.mockResolvedValue({ redirect_to: "https://claude.ai/cb?error=access_denied" });
    const onRedirect = mountConsent("/oauth/consent?request=req-1");
    fireEvent.click(await screen.findByRole("button", { name: "Deny" }));
    await waitFor(() => expect(onRedirect).toHaveBeenCalledWith("https://claude.ai/cb?error=access_denied"));
    expect(mockedDecide).toHaveBeenCalledWith("req-1", { decision: "deny" });
  });

  it("does not send a second decision while the first is in flight", async () => {
    mockedFetchConsent.mockResolvedValue(view());
    mockedDecide.mockReturnValue(new Promise(() => undefined));
    mountConsent("/oauth/consent?request=req-1");
    const allow = await screen.findByRole("button", { name: "Allow access" });
    fireEvent.click(allow);
    fireEvent.click(allow);
    fireEvent.click(screen.getByRole("button", { name: "Deny" }));
    expect(mockedDecide).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Deny" })).toBeDisabled();
  });

  it("shows the server's refusal of a decision", async () => {
    mockedFetchConsent.mockResolvedValue(view());
    mockedDecide.mockRejectedValue(new HttpError({ status: 403, url: "/x", payload: { error: "Requested scopes exceed the caller's role authority" } }));
    const onRedirect = mountConsent("/oauth/consent?request=req-1");
    fireEvent.click(await screen.findByRole("button", { name: "Allow access" }));
    expect(await screen.findByText("Requested scopes exceed the caller's role authority")).toBeInTheDocument();
    expect(onRedirect).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Allow access" })).toBeEnabled();
  });

  it.each([
    [404, "authorization_not_found", "This connection request was not found"],
    [403, "browser_mismatch", "Open the connection in the same browser"],
    [409, "authorization_already_decided", "This request has already been answered"],
    [410, "authorization_expired", "This connection request has expired"],
  ])("explains a %s %s view error", async (status, code, title) => {
    mockedFetchConsent.mockRejectedValue(new HttpError({ status, url: "/x", payload: { error: code } }));
    mountConsent("/oauth/consent?request=req-1");
    expect(await screen.findByText(title)).toBeInTheDocument();
  });
});
