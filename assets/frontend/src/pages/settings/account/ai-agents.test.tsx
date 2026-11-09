import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { HttpError } from "@/lib/http/errors";
import { blobBytes, readStoredZip } from "@/test/zip-helpers";
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
const SERVERS = `{"mcpServers": {"flow-steward": {"type": "http", "url": "${MCP}", "headers": {"Authorization": "Bearer {token}"}}}}`;
const CONFIG = `[mcp_servers.flow-steward]\nurl = "${MCP}"\nhttp_headers = { "Authorization" = "Bearer {token}" }\n`;

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
    archive_layout: "",
    manifest_dir: "",
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
      step({ id: "download_plugin", kind: "package", archive_layout: "claude_marketplace", file_name: "flow-steward-operator-claude-code.zip", title: "Download your plugin", content: SERVERS, uses_token: true, uses_mcp_url: true }),
      step({ id: "install_plugin", kind: "command", title: "Install it", content: "claude plugin install flow-steward-operator@flow-steward-operator", hint_key: "react.agent_access.test_hint", hint: "To install a newer download, first run `claude plugin marketplace remove flow-steward-operator`." }),
      step({ id: "mcp_config", kind: "file", file_name: "config.toml", title: "Add the server", content: CONFIG, uses_token: true, uses_mcp_url: true }),
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

function clickContinue() {
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
}

/** Step 1 → client, step 2 → its preselected package, then the access step. */
async function chooseClient(name: RegExp) {
  fireEvent.click(await screen.findByRole("radio", { name }));
  clickContinue();
  await screen.findByRole("radio", { name: /Flow Steward Operator/ });
}

async function chooseLocalOperator() {
  renderSection();
  await chooseClient(/Local agent/);
  clickContinue();
  await screen.findByRole("button", { name: "Create token" });
}

async function createLocalToken() {
  mockedIssue.mockResolvedValue(created());
  await chooseLocalOperator();
  fireEvent.click(screen.getByRole("button", { name: "Create token" }));
  return screen.findByLabelText("Add the server");
}

function currentStep() {
  return document.querySelector("[aria-current='step']") as HTMLElement;
}

describe("AiAgentsSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSetup.mockResolvedValue(setupPayload());
    mockedProjects.mockResolvedValue({ projects: [{ id: "p-1", name: "Shop" }] } as never);
    mockedToken.mockResolvedValue({ token: { id: "tok-1", label: "AI agent · Local agent", scopes: ["projects.read"], last_used_at: null, expires_at: "2026-11-30T12:00:00Z", allowed_project_ids: [] } });
    mockedTokens.mockResolvedValue({ tokens: [], total: 0, page: 1, per_page: 25, total_pages: 1 } as never);
  });

  it("groups the clients the server describes and opens one step at a time", async () => {
    renderSection();
    expect(await screen.findByRole("group", { name: "On your computer" })).toHaveTextContent("Local agent");
    expect(screen.getByRole("group", { name: "In the cloud" })).toHaveTextContent("ChatGPT");
    expect(within(currentStep()).getByRole("heading", { name: "Step 1. AI client" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Step 4. Set up the client" })).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /Flow Steward Operator/ })).not.toBeInTheDocument();
  });

  it("says why Continue does not move on without a client", async () => {
    renderSection();
    await screen.findByRole("radio", { name: /Local agent/ });
    clickContinue();
    expect(screen.getByRole("alert")).toHaveTextContent("Choose a client to continue.");
    fireEvent.click(screen.getByRole("radio", { name: /Local agent/ }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    clickContinue();
    expect(within(currentStep()).getByRole("heading", { name: "Step 2. Package" })).toBeInTheDocument();
    expect(screen.getByText("Local agent · On your computer")).toBeInTheDocument();
  });

  it("shows only the presets the role allows, with their scopes, and no setup before the token", async () => {
    await chooseLocalOperator();
    expect(screen.getByText("Flow Steward Operator")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Monitor and run workflows/ })).toBeChecked();
    expect(screen.queryByRole("radio", { name: /Full authoring/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/more than your role allows/)).not.toBeInTheDocument();
    expect(screen.getByText("jobs.read")).toBeInTheDocument();
    expect(screen.getByText(/agents and skills belong to the whole account/)).toBeInTheDocument();
    expect(screen.getByText(/^The token expires on /)).toBeInTheDocument();
    expect(screen.queryByLabelText("Add the server")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download flow-steward-operator-claude-code/ })).not.toBeInTheDocument();
  });

  it("describes admin:all once, next to its choice", async () => {
    mockedSetup.mockResolvedValue(setupPayload({ presets: [
      { id: "operational", scopes: ["projects.read"], available: true, reason: "", subset_of_preset: false },
      { id: "full_authoring", scopes: ["admin:all"], available: true, reason: "", subset_of_preset: false },
    ] }));
    await chooseLocalOperator();
    fireEvent.click(screen.getByRole("radio", { name: /Full authoring/ }));
    expect(screen.getAllByText(/admin:all is full access/)).toHaveLength(1);
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
    expect(await screen.findByLabelText("Add the server")).toHaveTextContent(`Bearer ${SECRET}`);
    const [key, request] = mockedIssue.mock.calls[0];
    expect(key).toMatch(/^[\w-]{16,}$/);
    expect(request).toEqual({ client_id: "claude_code", package_id: "flow-steward-operator", preset: "operational", expiry_days: 30, project_ids: [] });
    expect(within(currentStep()).getByRole("heading", { name: "Step 4. Set up Local agent" })).toBeInTheDocument();
    expect(screen.getByText("Your token is already in this setup")).toBeInTheDocument();
    expect(await screen.findByText("No request yet")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText(/^Monitor and run workflows · All projects · Expires /)).toBeInTheDocument();
    expect(screen.getByText("The plugin carries this installation's MCP address and your token, so there is nothing to configure.")).toBeInTheDocument();
    expect(screen.queryByText("Copy the token now")).not.toBeInTheDocument();
  });

  it("puts the update command of a hint in code under its block", async () => {
    await createLocalToken();
    expect(screen.getByText("claude plugin marketplace remove flow-steward-operator").tagName).toBe("CODE");
  });

  it("downloads a plugin that already holds the address and the token", async () => {
    mockedIssue.mockResolvedValue(created());
    const blobs: Blob[] = [];
    const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return "blob:plugin"; });
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    try {
      await chooseLocalOperator();
      fireEvent.click(screen.getByRole("button", { name: "Create token" }));
      const downloadButton = await screen.findByRole("button", { name: "Download flow-steward-operator-claude-code.zip" });
      await waitFor(() => expect(downloadButton).toBeEnabled());
      fireEvent.click(downloadButton);
      await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
      expect(await screen.findByText("Downloaded flow-steward-operator-claude-code.zip.")).toBeInTheDocument();
      const files = readStoredZip(await blobBytes(blobs[0]));
      const manifest = JSON.parse(files.get("flow-steward-operator/.claude-plugin/plugin.json") || "{}");
      expect(manifest.mcpServers["flow-steward"]).toEqual({ type: "http", url: MCP, headers: { Authorization: `Bearer ${SECRET}` } });
      expect(manifest.userConfig).toBeUndefined();
    } finally {
      click.mockRestore();
      URL.createObjectURL = original.create;
      URL.revokeObjectURL = original.revoke;
    }
  });

  it("copies in the button itself: Copied for a moment, then Copy again", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    try {
      await createLocalToken();
      const block = screen.getByLabelText("Add the server").parentElement as HTMLElement;
      fireEvent.click(within(block).getByRole("button", { name: "Copy" }));
      expect(await within(block).findByRole("button", { name: "Copied" })).toBeInTheDocument();
      expect(writeText).toHaveBeenCalledWith(expect.stringContaining(`Bearer ${SECRET}`));
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
      await waitFor(() => expect(within(block).getByRole("button", { name: "Copy" })).toBeInTheDocument(), { timeout: 3000 });
    } finally {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    }
  });

  it("keeps Copy and says so under the block when the clipboard is not available", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    await createLocalToken();
    const block = screen.getByLabelText("Add the server").parentElement as HTMLElement;
    fireEvent.click(within(block).getByRole("button", { name: "Copy" }));
    expect(await within(block).findByRole("alert")).toHaveTextContent("Couldn't copy. Select the text and copy it manually.");
    expect(within(block).getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });

  it("creates another token on request, reopening the access step", async () => {
    await createLocalToken();
    fireEvent.click(screen.getByRole("button", { name: "Create another token" }));
    expect(within(currentStep()).getByRole("heading", { name: "Step 3. Access" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create token" })).toBeEnabled();
    expect(screen.queryByLabelText("Add the server")).not.toBeInTheDocument();
  });

  it("changing the client keeps the token and the other choices", async () => {
    await createLocalToken();
    fireEvent.click(screen.getByRole("button", { name: "Change AI client" }));
    expect(screen.getByRole("radio", { name: /Local agent/ })).toBeChecked();
    clickContinue();
    expect(screen.getByRole("radio", { name: /Flow Steward Operator/ })).toBeChecked();
    clickContinue();
    expect(within(currentStep()).getByRole("heading", { name: "Step 4. Set up Local agent" })).toBeInTheDocument();
    expect(screen.getByLabelText("Add the server")).toHaveTextContent(`Bearer ${SECRET}`);
    expect(mockedIssue).toHaveBeenCalledTimes(1);
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
    expect(screen.getByLabelText("Add the server")).toHaveTextContent("<your personal token>");
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

  it("revokes the token in place from the access step", async () => {
    mockedRevoke.mockResolvedValue({});
    await createLocalToken();
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    const confirm = screen.getByRole("group", { name: "Revoke “AI agent · Local agent”?" });
    mockedToken.mockResolvedValue({ token: { id: "tok-1", label: "AI agent · Local agent", scopes: [], revoked: true, last_used_at: "2026-10-31T12:05:00Z" } });
    fireEvent.click(within(confirm).getByRole("button", { name: "Revoke token" }));
    await waitFor(() => expect(mockedRevoke).toHaveBeenCalledWith("tok-1"));
    expect(await screen.findByText("Revoked")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
    expect(screen.getByText(/Access used/)).toBeInTheDocument();
    expect(screen.getByLabelText("Add the server")).toHaveTextContent("<your personal token>");
  });

  it("asks a cloud client for access in Flow Steward, not for a token", async () => {
    mockedSetup.mockResolvedValue(setupPayload({ clients: [setupPayload().clients[0], { ...setupPayload().clients[1], mcp_blockers: [] }] }));
    renderSection();
    await chooseClient(/ChatGPT/);
    clickContinue();
    expect(screen.getByText("ChatGPT connects through OAuth: you choose access in Flow Steward when you connect.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Personal tokens › Connected AI apps" })).toHaveAttribute("href", "/settings/account?tab=tokens");
    expect(screen.queryByRole("button", { name: "Create token" })).not.toBeInTheDocument();
    clickContinue();
    expect(screen.getByLabelText("Add Flow Steward as a custom MCP connector")).toHaveTextContent(MCP);
    expect(screen.getByLabelText("Make the first request")).toHaveTextContent("List my Flow Steward projects");
    expect(screen.getByText(/appears under Personal tokens → Connected AI apps/)).toBeInTheDocument();
  });

  it("shows a cloud client's blocker instead of an address or a token", async () => {
    renderSection();
    await chooseClient(/ChatGPT/);
    // A package this client cannot use is not offered as a choice at all.
    expect(screen.queryByRole("radio", { name: /Flow Steward Developer/ })).not.toBeInTheDocument();
    expect(screen.queryByText("Not published")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Flow Steward Operator/ })).toBeChecked();
    clickContinue();
    expect(await screen.findByText("A public HTTPS address is required")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create token" })).not.toBeInTheDocument();
    clickContinue();
    expect(screen.queryByText("Add Flow Steward as a custom MCP connector")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Make the first request")).not.toBeInTheDocument();
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
    expect(screen.queryByLabelText("Add the server")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download flow-steward-operator-claude-code/ })).not.toBeInTheDocument();
  });

  it("Developer needs no token: access and the first request are not needed", async () => {
    renderSection();
    await chooseClient(/Local agent/);
    fireEvent.click(screen.getByRole("radio", { name: /Flow Steward Developer/ }));
    clickContinue();
    expect(screen.queryByRole("button", { name: "Create token" })).not.toBeInTheDocument();
    expect(screen.getAllByText("Not needed: the Developer package does not connect to this installation.")).toHaveLength(2);
    expect(within(currentStep()).getByText("This package does not connect to this installation, so no token is needed.")).toBeInTheDocument();
    expect(within(currentStep()).getByRole("link", { name: "Open the release" })).toBeInTheDocument();
    expect(mockedIssue).not.toHaveBeenCalled();
  });
});
