import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { McpOAuthCard } from "@/pages/settings/security/mcp-oauth-card";
import { fetchMcpOAuthSetting, saveMcpOAuthSetting } from "@/pages/settings/shared/api";

vi.mock("@/pages/settings/shared/api", () => ({
  fetchMcpOAuthSetting: vi.fn(),
  saveMcpOAuthSetting: vi.fn(),
}));

const mockedFetch = vi.mocked(fetchMcpOAuthSetting);
const mockedSave = vi.mocked(saveMcpOAuthSetting);

function renderCard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><McpOAuthCard /></QueryClientProvider>);
}

describe("McpOAuthCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loads the switch off by default and turns it on", async () => {
    mockedFetch.mockResolvedValue({ enabled: false, readiness: { ready: false, blockers: ["disabled_by_admin"] } });
    mockedSave.mockResolvedValue({ enabled: true, readiness: { ready: true, blockers: [] } });
    renderCard();
    const control = await screen.findByRole("switch", { name: "Allow cloud AI apps to connect" });
    await waitFor(() => expect(control).toBeEnabled());
    expect(control).not.toBeChecked();
    expect(screen.queryByText("Cloud AI apps are turned off")).not.toBeInTheDocument();
    fireEvent.click(control);
    await waitFor(() => expect(mockedSave).toHaveBeenCalledWith(true));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Allow cloud AI apps to connect" })).toBeChecked());
  });

  it("shows readiness blockers other than the switch itself", async () => {
    mockedFetch.mockResolvedValue({ enabled: true, readiness: { ready: false, blockers: ["public_https_required", "forward_secret_missing"] } });
    renderCard();
    expect(await screen.findByText("A public HTTPS address is required")).toBeInTheDocument();
    expect(screen.getByText("The MCP server is not linked to the application")).toBeInTheDocument();
  });

  it("turns it off and reports a failed save", async () => {
    mockedFetch.mockResolvedValue({ enabled: true, readiness: { ready: true, blockers: [] } });
    mockedSave.mockRejectedValue(new Error("Admin role required"));
    renderCard();
    const control = await screen.findByRole("switch", { name: "Allow cloud AI apps to connect" });
    await waitFor(() => expect(control).toBeChecked());
    fireEvent.click(control);
    await waitFor(() => expect(mockedSave).toHaveBeenCalledWith(false));
    expect(await screen.findByText("Admin role required")).toBeInTheDocument();
  });
});
