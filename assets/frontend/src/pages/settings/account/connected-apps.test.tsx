import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ConnectedAppsSection } from "@/pages/settings/account/connected-apps";
import { fetchOAuthGrants, revokeOAuthGrant } from "@/pages/settings/shared/api";
import type { OAuthGrant, OAuthGrantsPage } from "@/pages/settings/shared/types";

vi.mock("@/pages/settings/shared/api", () => ({
  fetchOAuthGrants: vi.fn(),
  revokeOAuthGrant: vi.fn(),
}));

const mockedFetchGrants = vi.mocked(fetchOAuthGrants);
const mockedRevoke = vi.mocked(revokeOAuthGrant);

function grant(overrides: Partial<OAuthGrant> = {}): OAuthGrant {
  return {
    id: "g-1",
    client_id: "https://chatgpt.com/oauth/client.json",
    client_name: "ChatGPT",
    client_host: "chatgpt.com",
    preset: "operational",
    scopes: ["projects.read", "jobs.read"],
    allowed_project_ids: [],
    created_at: "2026-10-01T10:00:00Z",
    expires_at: "2026-10-31T10:00:00Z",
    last_used_at: null,
    revoked_at: null,
    revoked_reason: null,
    status: "active",
    ...overrides,
  };
}

function page(grants: OAuthGrant[], readiness = { ready: true, blockers: [] as string[] }): OAuthGrantsPage {
  return { grants, readiness };
}

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><ConnectedAppsSection /></QueryClientProvider>);
}

describe("ConnectedAppsSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("lists grants with access, projects, last use and status", async () => {
    mockedFetchGrants.mockResolvedValue(page([
      grant(),
      grant({ id: "g-2", client_name: "", client_host: "claude.ai", preset: "full_authoring", scopes: ["admin:all"], allowed_project_ids: ["p-1", "p-2"], last_used_at: "2026-10-20T10:00:00Z", status: "revoked", revoked_at: "2026-10-21T10:00:00Z", revoked_reason: "user_revoked" }),
      grant({ id: "g-3", status: "expired" }),
    ]));
    renderSection();
    const table = await screen.findByRole("table", { name: "Connected AI apps" });
    // The table renders while the query loads; wait for its rows, not just the table.
    await waitFor(() => expect(within(table).getAllByRole("row").slice(1)).toHaveLength(3));
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("ChatGPT");
    expect(rows[0]).toHaveTextContent("Monitor and run workflows");
    expect(rows[0]).toHaveTextContent("projects.read, jobs.read");
    expect(rows[0]).toHaveTextContent("All projects within your access");
    expect(rows[0]).toHaveTextContent("Not used yet");
    expect(rows[0]).toHaveTextContent("Active");
    expect(rows[1]).toHaveTextContent("claude.ai");
    expect(rows[1]).toHaveTextContent("Full authoring");
    expect(rows[1]).toHaveTextContent("2 projects");
    expect(rows[1]).toHaveTextContent("Access used");
    expect(rows[1]).toHaveTextContent("Revoked");
    expect(rows[2]).toHaveTextContent("Expired");
    expect(within(rows[0]).getByRole("button", { name: "Actions for ChatGPT" })).toBeInTheDocument();
    expect(within(rows[1]).queryByRole("button")).not.toBeInTheDocument();
    expect(within(rows[2]).queryByRole("button")).not.toBeInTheDocument();
  });

  it("revokes after confirmation and reloads the list", async () => {
    mockedFetchGrants.mockResolvedValueOnce(page([grant()])).mockResolvedValueOnce(page([grant({ status: "revoked" })]));
    mockedRevoke.mockResolvedValue({ status: "revoked" });
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Actions for ChatGPT" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Revoke access" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Revoke “ChatGPT”?" });
    expect(mockedRevoke).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke access" }));
    await waitFor(() => expect(mockedRevoke).toHaveBeenCalledWith("g-1"));
    await waitFor(() => expect(mockedFetchGrants).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Revoked")).toBeInTheDocument();
  });

  it("explains a readiness blocker and the empty state", async () => {
    mockedFetchGrants.mockResolvedValue(page([], { ready: false, blockers: ["public_https_required"] }));
    renderSection();
    expect(await screen.findByText("A public HTTPS address is required")).toBeInTheDocument();
    expect(screen.getByText("No connected AI apps")).toBeInTheDocument();
  });

  it("shows a recoverable error when the grants cannot be loaded", async () => {
    mockedFetchGrants.mockRejectedValue(new Error("storage_missing"));
    renderSection();
    expect(await screen.findByText("Connected apps could not be loaded")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
