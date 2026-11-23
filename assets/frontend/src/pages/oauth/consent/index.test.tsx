import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { HttpError } from "@/lib/http/errors";
import { useAuth } from "@/lib/auth/useAuth";
import { OAuthConsentPage } from "@/pages/oauth/consent";
import { decideOAuthConsent, fetchOAuthConsent } from "@/pages/settings/shared/api";
import type { OAuthConsentRoleAccess, OAuthConsentView } from "@/pages/settings/shared/types";

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

const OPERATOR_ACCESS: OAuthConsentRoleAccess = {
  preset: "my_role",
  role: "operator",
  scopes: ["read:*", "registry.read", "command:*"],
  permissions: [
    { key: "jobs.read", description: "Read jobs, their results, files, logs and approvals" },
    { key: "workflows.run", description: "Start a workflow and preview which workflow a submission would start" },
  ],
  narrower: false,
  available: true,
  expiry_days: 90,
  expires_at: "2027-01-29T12:00:00Z",
};

/** An operator's request: "as my role", nothing to choose (ADR 0076 §5). */
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
    user: { username: "olga@example.test", display_name: "Olga", role: "operator" },
    requested_scopes: ["admin:all"],
    role_access: OPERATOR_ACCESS,
    advanced_options: false,
    presets: [],
    default_preset: "my_role",
    expiry_days_options: [],
    default_expiry_days: 30,
    ...overrides,
  };
}

/** An admin keeps the presets, the expiry and the projects, folded away. */
function adminView(overrides: Partial<OAuthConsentView> = {}): OAuthConsentView {
  return view({
    user: { username: "ada@example.test", display_name: "Ada", role: "admin" },
    requested_scopes: ["projects.read", "jobs.read", "admin:all"],
    role_access: { ...OPERATOR_ACCESS, role: "admin", scopes: ["admin:all"] },
    advanced_options: true,
    presets: [
      { id: "my_role", scopes: ["admin:all"], available: true, reason: "", subset_of_preset: false },
      { id: "operational", scopes: ["projects.read", "jobs.read"], available: true, reason: "", subset_of_preset: true },
      { id: "full_authoring", scopes: ["admin:all"], available: false, reason: "exceeds_authority", subset_of_preset: false },
    ],
    expiry_days_options: [7, 30, 90],
    ...overrides,
  });
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
    await waitFor(() => expect(screen.getByText("A public HTTPS address is required")).toBeInTheDocument());
    expect(mockedFetchConsent).not.toHaveBeenCalled();
  });

  it.each([
    ["disabled_by_admin", "Cloud AI apps are turned off"],
    ["client_not_allowed", "This app is not supported"],
    ["invalid_redirect_uri", "The return address is not accepted"],
    ["something_new", "The app could not connect"],
  ])("explains the %s error", async (code, title) => {
    mountConsent(`/oauth/consent?error=${code}`);
    await waitFor(() => expect(screen.getByText(title)).toBeInTheDocument());
  });

  it("sends an unauthenticated request to login with the consent path as next", async () => {
    mockedUseAuth.mockReturnValue(authState(false));
    mountConsent("/oauth/consent?request=req-1");
    await waitFor(() => expect(screen.getByText(/login page/)).toHaveTextContent(
      `?next=${encodeURIComponent("/oauth/consent?request=req-1")}`,
    ));
    expect(mockedFetchConsent).not.toHaveBeenCalled();
  });

  it("tells an operator who they are, what the app can do and for how long, with Allow and Deny only", async () => {
    mockedFetchConsent.mockResolvedValue(view());
    mockedDecide.mockResolvedValue({ redirect_to: "https://claude.ai/api/mcp/auth_callback?code=x" });
    const onRedirect = mountConsent("/oauth/consent?request=req-1");
    await waitFor(() => expect(screen.getByText("Allow Claude to access Flow Steward?")).toBeInTheDocument());
    expect(screen.getByText("Signed in as Olga · Operator")).toBeInTheDocument();
    expect(screen.getByText(/Claude will be able to see what you see and do the work you do/)).toBeInTheDocument();
    expect(screen.getByText("Start a workflow and preview which workflow a submission would start")).toBeInTheDocument();
    expect(screen.getByText(/^Works for 90 days, until /)).toHaveTextContent(/2027/);
    // No preset, scope, project or expiry choice.
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Advanced options" })).not.toBeInTheDocument();
    expect(screen.queryByText("read:*")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deny" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Hide advanced options" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(onRedirect).toHaveBeenCalledWith("https://claude.ai/api/mcp/auth_callback?code=x"));
    expect(mockedDecide).toHaveBeenCalledWith("req-1", { decision: "approve" });
  });

  it("says in plain words when the app gets less than the person's role", async () => {
    mockedFetchConsent.mockResolvedValue(view({
      user: { username: "vik@example.test", display_name: "", role: "viewer" },
      role_access: { ...OPERATOR_ACCESS, role: "viewer", narrower: true, scopes: ["jobs.read"], permissions: [{ key: "jobs.read", description: "Read jobs, their results, files, logs and approvals" }] },
    }));
    mountConsent("/oauth/consent?request=req-1");
    await waitFor(() => expect(screen.getByText("Claude asked for less than your role allows, so it gets only this part of your access.")).toBeInTheDocument());
    expect(screen.getByText("Signed in as vik@example.test · Viewer")).toBeInTheDocument();
    expect(screen.getByText("Read jobs, their results, files, logs and approvals")).toBeVisible();
  });

  it("gives an admin their role preselected and the presets under Advanced options", async () => {
    mockedFetchConsent.mockResolvedValue(adminView());
    mockedDecide.mockResolvedValue({ redirect_to: "https://claude.ai/cb?code=x" });
    const onRedirect = mountConsent("/oauth/consent?request=req-1");
    await waitFor(() => expect(screen.getByText("Claude will be able to do what you can do in Flow Steward.")).toBeInTheDocument());
    expect(screen.getByText("Same access as you · 90 days · All projects")).toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Advanced options" }));
    expect(screen.getByRole("radio", { name: "Same access as you" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Full authoring" })).toBeDisabled();
    expect(screen.getByText("Not available: this access is more than your own role allows.")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "90 days" })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "Monitor and run workflows" }));
    fireEvent.click(screen.getByRole("radio", { name: "30 days" }));
    fireEvent.click(screen.getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(onRedirect).toHaveBeenCalledWith("https://claude.ai/cb?code=x"));
    expect(mockedDecide).toHaveBeenCalledWith("req-1", { decision: "approve", preset: "operational", expiry_days: 30, project_ids: [] });
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
    await waitFor(() => expect(screen.getByText("Allow Microsoft 365 Copilot to access Flow Steward?")).toBeInTheDocument());
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
    await waitFor(() => expect(screen.getByText("The app returns to this computer")).toBeInTheDocument());
  });

  it("offers only Deny when nothing can be granted", async () => {
    mockedFetchConsent.mockResolvedValue(view({
      role_access: { ...OPERATOR_ACCESS, scopes: [], permissions: [], available: false },
      default_preset: null,
    }));
    mountConsent("/oauth/consent?request=req-1");
    await waitFor(() => expect(screen.getByText("Nothing can be granted")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Allow" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deny" })).toBeEnabled();
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
    const allow = await screen.findByRole("button", { name: "Allow" });
    fireEvent.click(allow);
    fireEvent.click(allow);
    fireEvent.click(screen.getByRole("button", { name: "Deny" }));
    expect(mockedDecide).toHaveBeenCalledTimes(1);
    expect(allow).toHaveAttribute("aria-busy", "true");
    // The other decision keeps its normal look; its press is ignored, not disabled.
    expect(screen.getByRole("button", { name: "Deny" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Deny" })).not.toHaveAttribute("aria-busy");
  });

  it("shows the server's refusal of a decision", async () => {
    mockedFetchConsent.mockResolvedValue(view());
    mockedDecide.mockRejectedValue(new HttpError({ status: 403, url: "/x", payload: { error: "advanced_options_admin_only" } }));
    const onRedirect = mountConsent("/oauth/consent?request=req-1");
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    await waitFor(() => expect(screen.getByText("Your agent gets the access of your role; only an admin chooses the access, the expiry and the projects.")).toBeInTheDocument());
    expect(onRedirect).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Allow" })).toBeEnabled();
  });

  it.each([
    [404, "authorization_not_found", "This connection request was not found"],
    [403, "browser_mismatch", "Open the connection in the same browser"],
    [409, "authorization_already_decided", "This request has already been answered"],
    [410, "authorization_expired", "This connection request has expired"],
  ])("explains a %s %s view error", async (status, code, title) => {
    mockedFetchConsent.mockRejectedValue(new HttpError({ status, url: "/x", payload: { error: code } }));
    mountConsent("/oauth/consent?request=req-1");
    await waitFor(() => expect(screen.getByText(title)).toBeInTheDocument());
  });
});
