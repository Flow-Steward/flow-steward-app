import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { PlatformClientStep } from "@/pages/settings/account/ai-agents-platform";
import { createPlatformClient, fetchPlatformClients, revokePlatformClient } from "@/pages/settings/shared/api";
import type { AgentSetupStep, PlatformClientsView } from "@/pages/settings/shared/types";

vi.mock("@/lib/auth/use-is-admin", () => ({ useIsAdmin: vi.fn() }));
vi.mock("@/pages/settings/shared/api", () => ({
  createPlatformClient: vi.fn(),
  fetchPlatformClients: vi.fn(),
  revokePlatformClient: vi.fn(),
}));

const mockedAdmin = vi.mocked(useIsAdmin);
const mockedFetch = vi.mocked(fetchPlatformClients);
const mockedCreate = vi.mocked(createPlatformClient);
const mockedRevoke = vi.mocked(revokePlatformClient);

const MCP = "https://fs.example.com/mcp";
const CLIENT_ID = "fs-platform-m365-0123456789abcdef0123456789abcdef";
const STEP: AgentSetupStep = {
  id: "platform_client",
  kind: "platform",
  title_key: "react.agent_access.step_m365_package",
  title: "Register the sign-in and build the agent",
  description_key: "",
  description: "",
  values: {},
  content: MCP,
  uses_token: false,
  uses_mcp_url: true,
  file_name: "flow-steward-operator-m365-copilot.zip",
  archive_layout: "",
  manifest_dir: "",
  platform_preset: "microsoft_365",
  url: "",
};

function view(overrides: Partial<PlatformClientsView> = {}): PlatformClientsView {
  return {
    presets: [{ id: "microsoft_365", name: "Microsoft 365 Copilot", redirect_uris: ["https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect"] }],
    clients: [],
    portal: {
      base_url: MCP,
      authorization_endpoint: "https://fs.example.com/oauth/authorize",
      token_endpoint: "https://fs.example.com/oauth/token",
      refresh_endpoint: "https://fs.example.com/oauth/token",
      scope: "offline_access",
      pkce: true,
      client_secret: "",
    },
    readiness: { ready: true, blockers: [] },
    ...overrides,
  };
}

const ACTIVE = { client_id: CLIENT_ID, preset: "microsoft_365", name: "Microsoft 365 Copilot", created_at: "2026-11-06T10:00:00Z", created_by: "admin", revoked_at: null };

function renderStep() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><PlatformClientStep mcpUrl={MCP} packageVersion="1.0.2" step={STEP} /></QueryClientProvider>);
}

describe("PlatformClientStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedAdmin.mockReturnValue(true);
  });

  it("tells a non-admin who does this step and asks the server nothing", () => {
    mockedAdmin.mockReturnValue(false);
    renderStep();
    expect(screen.getByText("An administrator does this step")).toBeInTheDocument();
    expect(mockedFetch).not.toHaveBeenCalled();
  });

  it("creates the Microsoft 365 client and lists the Developer Portal values", async () => {
    mockedFetch.mockResolvedValue(view());
    mockedCreate.mockResolvedValue(view({ clients: [ACTIVE] }));
    renderStep();
    await waitFor(() => expect(screen.getByText("https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Create the Microsoft 365 client" }));
    await waitFor(() => expect(mockedCreate).toHaveBeenCalledWith("microsoft_365"));
    await waitFor(() => expect(screen.getByText(CLIENT_ID)).toBeInTheDocument());
    expect(screen.getByText("https://fs.example.com/oauth/authorize")).toBeInTheDocument();
    expect(screen.getAllByText("https://fs.example.com/oauth/token")).toHaveLength(2);
    expect(screen.getByText("offline_access")).toBeInTheDocument();
    expect(screen.getByText("Leave empty")).toBeInTheDocument();
    expect(screen.getByText("Any Teams app")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy Client ID" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy Client secret" })).not.toBeInTheDocument();
  });

  it("downloads the package only after a registration ID is pasted", async () => {
    mockedFetch.mockResolvedValue(view({ clients: [ACTIVE] }));
    const created: Blob[] = [];
    const createObjectURL = vi.fn((blob: Blob) => {
      created.push(blob);
      return "blob:package";
    });
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const fetchIcon = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(new Uint8Array([137, 80, 78, 71])));
    renderStep();
    const download = await screen.findByRole("button", { name: "Download flow-steward-operator-m365-copilot.zip" });
    expect(download).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/OAuth client registration ID/), { target: { value: "bad id" } });
    expect(download).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/OAuth client registration ID/), { target: { value: "OTVkNjMyZTEtYmU2Ny00NjYy" } });
    expect(download).toBeEnabled();
    fireEvent.click(download);
    await waitFor(() => expect(screen.getByText("Downloaded flow-steward-operator-m365-copilot.zip.")).toBeInTheDocument());
    expect(created).toHaveLength(1);
    expect(created[0].type).toBe("application/zip");
    expect(click).toHaveBeenCalled();
    expect(fetchIcon).toHaveBeenCalledTimes(2);
    click.mockRestore();
    fetchIcon.mockRestore();
  });

  it("revokes the client after a confirmation", async () => {
    mockedFetch.mockResolvedValue(view({ clients: [ACTIVE] }));
    mockedRevoke.mockResolvedValue(view({ clients: [{ ...ACTIVE, revoked_at: "2026-11-06T11:00:00Z" }] }));
    renderStep();
    fireEvent.click(await screen.findByRole("button", { name: "Revoke the Microsoft 365 client" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Revoke the Microsoft 365 client?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke the Microsoft 365 client" }));
    await waitFor(() => expect(mockedRevoke).toHaveBeenCalledWith(CLIENT_ID));
    await waitFor(() => expect(screen.getByRole("button", { name: "Create the Microsoft 365 client" })).toBeInTheDocument());
    expect(screen.getByText("Revoked earlier: 1")).toBeInTheDocument();
  });

  it("does not offer a client while cloud AI apps are not ready", async () => {
    mockedFetch.mockResolvedValue(view({ portal: null, readiness: { ready: false, blockers: ["public_https_required"] } }));
    renderStep();
    await waitFor(() => expect(screen.getByText("Cloud AI apps are not ready")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Create the Microsoft 365 client" })).toBeDisabled();
  });
});
