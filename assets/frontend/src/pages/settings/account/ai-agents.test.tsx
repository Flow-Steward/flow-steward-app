import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { HttpError } from "@/lib/http/errors";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { AiAgentsSection } from "@/pages/settings/account/ai-agents";
import {
  fetchAgentAccessSetup,
  fetchAgentAccessToken,
  fetchPersonalTokensCollection,
  issueAgentAccessToken,
  revokePersonalToken,
} from "@/pages/settings/shared/api";
import type {
  AgentAccessIssueResult,
  AgentAccessSetup,
  AgentClientPackage,
  AgentSetupStep,
} from "@/pages/settings/shared/types";

vi.mock("@/pages/settings/shared/api", () => ({
  fetchAgentAccessSetup: vi.fn(),
  fetchAgentAccessToken: vi.fn(),
  fetchPersonalTokensCollection: vi.fn(),
  issueAgentAccessToken: vi.fn(),
  revokePersonalToken: vi.fn(),
}));
vi.mock("@/pages/projects/list/api", () => ({ fetchProjectsList: vi.fn() }));

const mockedSetup = vi.mocked(fetchAgentAccessSetup);
const mockedIssue = vi.mocked(issueAgentAccessToken);
const mockedToken = vi.mocked(fetchAgentAccessToken);
const mockedTokens = vi.mocked(fetchPersonalTokensCollection);
const mockedRevoke = vi.mocked(revokePersonalToken);
const mockedProjects = vi.mocked(fetchProjectsList);

const MCP = "https://fs.example.com/mcp";
const SECRET = "fs_pat_secret_value"; // pragma: allowlist secret

function step(overrides: Partial<AgentSetupStep>): AgentSetupStep {
  return {
    id: "s",
    kind: "value",
    title_key: "react.agent_access.test_step",
    title: "A step",
    description_key: "",
    description: "",
    values: {},
    content: "",
    uses_token: false,
    uses_mcp_url: false,
    file_name: "",
    url: "",
    ...overrides,
  };
}

const download = step({ id: "download_package", kind: "link", title: "Download the package", url: "https://github.com/x/releases/tag/v0.9.6" });

function operator(overrides: Partial<AgentClientPackage> = {}): AgentClientPackage {
  return {
    package_id: "flow-steward-operator",
    offered: true,
    available: true,
    auth: "personal_token",
    verified: true,
    message_key: "",
    message: "",
    steps: [
      download,
      step({ id: "mcp_url", title: "Flow Steward MCP address", content: MCP, uses_mcp_url: true }),
      step({ id: "mcp_token", title: "Flow Steward personal access token", content: "{token}", uses_token: true }),
    ],
    ...overrides,
  };
}

const developer: AgentClientPackage = {
  package_id: "flow-steward-developer",
  offered: true,
  available: true,
  auth: "none",
  verified: false,
  message_key: "",
  message: "",
  steps: [download],
};

function setupPayload(overrides: Partial<AgentAccessSetup> = {}): AgentAccessSetup {
  return {
    mcp_url: MCP,
    mcp_blocker: "",
    cloud_readiness: { ready: false, blockers: ["public_https_required"] },
    core_version: "0.9.6",
    packages: [
      { id: "flow-steward-operator", display_name: "Flow Steward Operator", short_description: "", version: "1.0.0", needs_mcp: true, description_key: "react.agent_access.package_operator_description", description: "Operator package" },
      { id: "flow-steward-developer", display_name: "Flow Steward Developer", short_description: "", version: "1.0.0", needs_mcp: false, description_key: "react.agent_access.package_developer_description", description: "Developer package" },
    ],
    presets: [
      { id: "operational", scopes: ["projects.read", "jobs.read"], available: true, reason: "", subset_of_preset: false },
      { id: "full_authoring", scopes: ["admin:all"], available: false, reason: "exceeds_authority", subset_of_preset: false },
    ],
    expiry_days_options: [7, 30, 90],
    default_expiry_days: 30,
    first_request: step({ id: "first_request", title: "Make the first request", content: "List my Flow Steward projects and their enabled workflows." }),
    clients: [
      { id: "claude_code", label: "Local agent", connection: "local", description_key: "react.agent_access.client_claude_code_description", description: "Terminal", mcp_blockers: [], packages: [operator(), developer] },
      {
        id: "chatgpt",
        label: "ChatGPT",
        connection: "cloud",
        description_key: "react.agent_access.client_chatgpt_description",
        description: "Chat",
        mcp_blockers: ["public_https_required"],
        packages: [operator({ auth: "oauth", verified: false, steps: [step({ id: "custom_connector", title: "Add Flow Steward as a custom MCP connector", content: MCP, uses_mcp_url: true })] }), { ...developer, available: false, message_key: "react.agent_access.reason_listing_not_published", message: "Not published", steps: [] }],
      },
    ],
    ...overrides,
  };
}

function created(overrides: Partial<AgentAccessIssueResult> = {}): AgentAccessIssueResult {
  return {
    status: "created",
    token: { id: "tok-1", label: "AI agent · Local agent", scopes: ["projects.read"] },
    secret: SECRET,
    secret_already_shown: false,
    ...overrides,
  };
}

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><AiAgentsSection /></QueryClientProvider>);
}

async function chooseLocalOperator() {
  renderSection();
  fireEvent.click(await screen.findByRole("radio", { name: /Local agent/ }));
  await screen.findByRole("radio", { name: /Flow Steward Operator/ });
}

describe("AiAgentsSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSetup.mockResolvedValue(setupPayload());
    mockedProjects.mockResolvedValue({ projects: [{ id: "p-1", name: "Shop" }] } as never);
    mockedToken.mockResolvedValue({ token: { id: "tok-1", label: "AI agent · Local agent", scopes: ["projects.read"], last_used_at: null, expires_at: "2026-11-30T12:00:00Z", allowed_project_ids: [] } });
    mockedTokens.mockResolvedValue({ tokens: [], total: 0, page: 1, per_page: 25, total_pages: 1 } as never);
  });

  it("lists the clients the server describes and offers presets capped by the role", async () => {
    await chooseLocalOperator();
    expect(screen.getByRole("radio", { name: /ChatGPT/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Monitor and run workflows/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Full authoring/ })).toBeDisabled();
    expect(screen.getByText("Not available: it is more than your role allows.")).toBeInTheDocument();
    expect(screen.getByText(/agents and skills belong to the whole account/)).toBeInTheDocument();
    expect(screen.getByLabelText("Flow Steward MCP address")).toHaveTextContent(MCP);
    expect(screen.getByLabelText("Flow Steward personal access token")).toHaveTextContent("<your personal token>");
  });

  it("explains admin:all when full authoring is chosen", async () => {
    mockedSetup.mockResolvedValue(setupPayload({ presets: [
      { id: "operational", scopes: ["projects.read"], available: true, reason: "", subset_of_preset: false },
      { id: "full_authoring", scopes: ["admin:all"], available: true, reason: "", subset_of_preset: false },
    ] }));
    await chooseLocalOperator();
    fireEvent.click(screen.getByRole("radio", { name: /Full authoring/ }));
    expect(screen.getByText("admin:all is full access")).toBeInTheDocument();
  });

  it("sends one request for a double click and fills the token in once", async () => {
    let resolve: (value: AgentAccessIssueResult) => void = () => undefined;
    mockedIssue.mockImplementation(() => new Promise((done) => { resolve = done; }));
    await chooseLocalOperator();
    const button = screen.getByRole("button", { name: "Create token" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(mockedIssue).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Creating…" })).toBeDisabled();
    resolve(created());
    expect(await screen.findByLabelText("Flow Steward personal access token")).toHaveTextContent(SECRET);
    const [key, request] = mockedIssue.mock.calls[0];
    expect(key).toMatch(/\w{16,}/);
    expect(request).toEqual({ client_id: "claude_code", package_id: "flow-steward-operator", preset: "operational", expiry_days: 30, project_ids: [] });
    expect(await screen.findByText("Not used yet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Hide the token" }));
    expect(screen.getByLabelText("Flow Steward personal access token")).not.toHaveTextContent(SECRET);
  });

  it("after a lost response asks about the same attempt and never shows the secret again", async () => {
    mockedIssue.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    mockedIssue.mockResolvedValueOnce(created({ status: "replayed", secret: "", secret_already_shown: true }));
    mockedTokens.mockResolvedValue({ tokens: [{ id: "tok-1", label: "AI agent · Local agent", created_at: "2026-10-31T12:00:00Z", scopes: [], allowed_project_ids: [], allowed_workflow_ids: [] }], total: 1, page: 1, per_page: 25, total_pages: 1 } as never);
    await chooseLocalOperator();
    fireEvent.click(screen.getByRole("button", { name: "Create token" }));
    expect(await screen.findByText("We could not confirm whether the token was created")).toBeInTheDocument();
    expect(await screen.findByText(/AI agent · Local agent/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Monitor and run workflows/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText("This token already exists; its secret was shown once")).toBeInTheDocument();
    expect(mockedIssue).toHaveBeenCalledTimes(2);
    expect(mockedIssue.mock.calls[1][0]).toBe(mockedIssue.mock.calls[0][0]);
    expect(mockedIssue.mock.calls[1][1]).toEqual(mockedIssue.mock.calls[0][1]);
    expect(screen.getByLabelText("Flow Steward personal access token")).toHaveTextContent("<your personal token>");
  });

  it("a refusal creates nothing, and the next click is a new attempt", async () => {
    mockedIssue.mockRejectedValueOnce(new HttpError({ status: 403, url: "/api/auth/agent-access/tokens", payload: { error: "exceeds_authority" } }));
    mockedIssue.mockResolvedValueOnce(created());
    await chooseLocalOperator();
    fireEvent.click(screen.getByRole("button", { name: "Create token" }));
    expect(await screen.findByText("This access is more than your own role allows.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create token" }));
    await waitFor(() => expect(mockedIssue).toHaveBeenCalledTimes(2));
    expect(mockedIssue.mock.calls[1][0]).not.toBe(mockedIssue.mock.calls[0][0]);
  });

  it("revokes the token from the usage facts", async () => {
    mockedIssue.mockResolvedValue(created());
    mockedRevoke.mockResolvedValue({});
    await chooseLocalOperator();
    fireEvent.click(screen.getByRole("button", { name: "Create token" }));
    fireEvent.click(await screen.findByRole("button", { name: "Revoke token" }));
    mockedToken.mockResolvedValue({ token: { id: "tok-1", label: "AI agent · Local agent", scopes: [], revoked: true, last_used_at: "2026-10-31T12:05:00Z" } });
    const dialogButtons = await screen.findAllByRole("button", { name: "Revoke token" });
    fireEvent.click(dialogButtons[dialogButtons.length - 1]);
    await waitFor(() => expect(mockedRevoke).toHaveBeenCalledWith("tok-1"));
    expect(await screen.findByText("Revoked")).toBeInTheDocument();
    expect(screen.getByText(/Access used/)).toBeInTheDocument();
  });

  it("shows a cloud client's blocker instead of an address or a token", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("radio", { name: /ChatGPT/ }));
    expect(await screen.findByText("A public HTTPS address is required")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create token" })).not.toBeInTheDocument();
    expect(screen.queryByText("Add Flow Steward as a custom MCP connector")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Flow Steward Developer/ })).toBeDisabled();
    expect(screen.getByText("Not published")).toBeInTheDocument();
  });

  it("names a missing public address as the blocker for a local client", async () => {
    mockedSetup.mockResolvedValue(setupPayload({
      mcp_url: "",
      mcp_blocker: "public_address_missing",
      clients: [{ ...setupPayload().clients[0], mcp_blockers: ["public_address_missing"] }],
    }));
    await chooseLocalOperator();
    expect(screen.getByText("This installation has no public address")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create token" })).toBeDisabled();
    expect(screen.queryByLabelText("Flow Steward MCP address")).not.toBeInTheDocument();
  });

  it("Developer needs no token", async () => {
    await chooseLocalOperator();
    fireEvent.click(screen.getByRole("radio", { name: /Flow Steward Developer/ }));
    expect(screen.queryByRole("button", { name: "Create token" })).not.toBeInTheDocument();
    expect(screen.getByText("This package does not connect to this installation, so no token is needed.")).toBeInTheDocument();
    expect(mockedIssue).not.toHaveBeenCalled();
  });
});
