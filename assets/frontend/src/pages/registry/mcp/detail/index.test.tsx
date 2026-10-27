import { StrictMode } from "react";

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { HttpError } from "@/lib/http/errors";
import { useHasCapability } from "@/lib/auth/capabilities";
import { RegistryMcpDetailPage } from "@/pages/registry/mcp/detail";
import {
  checkMcpConnectorHealth,
  disconnectMcpConnectorOAuth,
  fetchMcpConnector,
  fetchMcpConnectorOAuthStatus,
  saveMcpConnectorOAuthCredentials,
  startMcpConnectorOAuth,
  syncMcpConnector,
  updateMcpConnector,
} from "@/pages/registry/mcp/api";

vi.mock("@/pages/registry/mcp/api", () => ({
  checkMcpConnectorHealth: vi.fn(),
  createMcpConnector: vi.fn(),
  deleteMcpConnector: vi.fn(),
  disconnectMcpConnectorOAuth: vi.fn(),
  fetchMcpConnector: vi.fn(),
  fetchMcpConnectorOAuthStatus: vi.fn(),
  saveMcpConnectorOAuthCredentials: vi.fn(),
  fetchMcpConnectors: vi.fn(),
  startMcpConnectorOAuth: vi.fn(),
  syncMcpConnector: vi.fn(),
  updateMcpConnector: vi.fn(),
}));
vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));

const mockedCheckMcpConnectorHealth = vi.mocked(checkMcpConnectorHealth);
const mockedDisconnectMcpConnectorOAuth = vi.mocked(disconnectMcpConnectorOAuth);
const mockedFetchMcpConnector = vi.mocked(fetchMcpConnector);
const mockedFetchMcpConnectorOAuthStatus = vi.mocked(fetchMcpConnectorOAuthStatus);
const mockedStartMcpConnectorOAuth = vi.mocked(startMcpConnectorOAuth);
const mockedSaveMcpConnectorOAuthCredentials = vi.mocked(saveMcpConnectorOAuthCredentials);
const mockedSyncMcpConnector = vi.mocked(syncMcpConnector);
const mockedUpdateMcpConnector = vi.mocked(updateMcpConnector);
const mockedUseHasCapability = vi.mocked(useHasCapability);

function renderPage(
  initialEntry = "/registry/mcp/mcp_fs?project_id=project-1",
  { strict = false }: { strict?: boolean } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  const page = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <LocationProbe />
        <Routes>
          <Route element={<RegistryMcpDetailPage />} path="/registry/mcp/:connectorId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );

  return render(strict ? <StrictMode>{page}</StrictMode> : page);
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-search">{location.search}</output>;
}

function connectorFixture(overrides: Record<string, unknown> = {}) {
  return {
    connector_id: "mcp_fs",
    account_id: "acc_default",
    project_id: "project-1",
    tenant_id: "tenant-1",
    oauth_requested_scopes: [],
    oauth_authorization_parameters: {},
    oauth_refresh_token_required: false,
    display_name: "Flow Steward MCP",
    description: "Registry MCP",
    transport: "streamable_http",
    base_url: "https://fs.example/mcp",
    auth_mode: "none",
    namespace_prefix: "fs",
    allowlisted_tools: [],
    discovered_tools_jsonb: [
      {
        remote_tool_name: "catalog.read",
        projected_tool_id: "fs.catalog.read",
        display_name: "Catalog Read",
        description: "Read catalog rows.",
        input_schema: {
          type: "object",
          properties: {
            limit: { type: "integer" },
          },
        },
        output_schema: {
          type: "object",
          properties: {
            rows: { type: "array" },
          },
        },
        annotations: {},
      },
    ],
    projected_tools: [
      {
        tool_id: "fs.catalog.read",
        display_name: "Flow Steward MCP / Catalog Read",
        enabled: false,
        remote_tool_name: "catalog.read",
      },
    ],
    status: "active",
    auth_status: "not_configured",
    oauth_status: {
      status: "not_configured",
      issuer: "",
      registration_mode: "",
      resource: "",
      expires_at: "",
      granted_scopes: [],
      connected_identity: "",
      has_access_token: false,
      has_refresh_token: false,
      last_error: "",
    },
    last_synced_at: "2026-05-20T05:00:00Z",
    last_sync_status: "success",
    last_sync_error: "",
    enabled: true,
    created_at: "2026-05-20T04:00:00Z",
    updated_at: "2026-05-20T05:00:00Z",
    ...overrides,
  };
}

function oauthStatusFixture(overrides: Record<string, unknown> = {}) {
  return {
    status: "auth_required",
    redirect_uri: "https://flow.example.test/api/integrations/oauth/callback",
    issuer: "https://auth.example",
    registration_mode: "client_metadata_document",
    resource: "https://fs.example/mcp",
    expires_at: "",
    granted_scopes: [],
    connected_identity: "",
    has_access_token: false,
    has_refresh_token: false,
    last_error: "",
    ...overrides,
  };
}

describe("RegistryMcpDetailPage", () => {
  beforeEach(() => {
    mockedUseHasCapability.mockReturnValue(true);
    mockedCheckMcpConnectorHealth.mockReset();
    mockedDisconnectMcpConnectorOAuth.mockReset();
    mockedFetchMcpConnector.mockReset();
    mockedFetchMcpConnectorOAuthStatus.mockReset();
    mockedStartMcpConnectorOAuth.mockReset();
    mockedSaveMcpConnectorOAuthCredentials.mockReset();
    mockedSyncMcpConnector.mockReset();
    mockedUpdateMcpConnector.mockReset();

    mockedFetchMcpConnector.mockResolvedValue(connectorFixture() as never);
    mockedFetchMcpConnectorOAuthStatus.mockResolvedValue(oauthStatusFixture() as never);
    mockedStartMcpConnectorOAuth.mockResolvedValue({
      authorization_url: "https://auth.example/authorize?state=abc",
    } as never);
    mockedSaveMcpConnectorOAuthCredentials.mockResolvedValue({ saved: true } as never);
    mockedDisconnectMcpConnectorOAuth.mockResolvedValue({ status: "disconnected" } as never);
    mockedCheckMcpConnectorHealth.mockResolvedValue({
      connector_id: "mcp_fs",
      status: "active",
      latency_ms: 42,
      error: null,
      checked_at: "2026-05-20T05:00:00Z",
    } as never);
    mockedSyncMcpConnector.mockResolvedValue({} as never);
    mockedUpdateMcpConnector.mockResolvedValue({} as never);
  });

  it("does not make connector or OAuth network calls without project_id", async () => {
    renderPage("/registry/mcp/mcp_fs");

    const projectRequiredHeading = await screen.findByRole("heading", { name: "Select a project first" });
    expect(projectRequiredHeading).toBeVisible();
    expect(
      within(screen.getByTestId("page-container")).getByRole("heading", { name: "Select a project first" }),
    ).toBe(projectRequiredHeading);
    expect(mockedFetchMcpConnector).not.toHaveBeenCalled();
    expect(mockedFetchMcpConnectorOAuthStatus).not.toHaveBeenCalled();
  });

  it.each([
    {
      suffix: "oauth=error&reason=oauth_failed",
      message: "OAuth authorization failed. Review the connector status and try again.",
    },
    {
      suffix: "oauth=error&reason=%3Cscript%3Eprovider-text%3C%2Fscript%3E",
      message: "OAuth authorization could not be completed. Review the connector status and try again.",
    },
  ])("shows and consumes the safe callback outcome for $suffix", async ({ suffix, message }) => {
    renderPage(`/registry/mcp/mcp_fs?project_id=project-1&${suffix}`);

    expect(await screen.findByText(message)).toBeVisible();
    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent("?project_id=project-1");
    });
    expect(screen.queryByText(/script.*provider-text/u)).not.toBeInTheDocument();
    expect(mockedSyncMcpConnector).not.toHaveBeenCalled();
  });

  it("shows backend callback sync success without calling the sync API", async () => {
    renderPage("/registry/mcp/mcp_fs?project_id=project-1&oauth=success&sync=success");

    expect(await screen.findByText("OAuth authorization completed and remote tools synchronized.")).toBeVisible();
    expect(mockedSyncMcpConnector).not.toHaveBeenCalled();
    expect(screen.getByTestId("location-search")).toHaveTextContent("?project_id=project-1");
  });

  it("keeps backend callback sync display-only under StrictMode", async () => {
    renderPage(
      "/registry/mcp/mcp_fs?project_id=project-1&oauth=success&sync=success",
      { strict: true },
    );

    await screen.findByText("OAuth authorization completed and remote tools synchronized.");
    expect(mockedSyncMcpConnector).not.toHaveBeenCalled();
    expect(screen.getByTestId("location-search")).toHaveTextContent("?project_id=project-1");
  });

  it("shows retry guidance for backend callback sync failure without calling sync", async () => {
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({ auth_mode: "oauth2", auth_status: "connected" }) as never,
    );
    mockedFetchMcpConnectorOAuthStatus.mockResolvedValue(
      oauthStatusFixture({ status: "connected", has_access_token: true }) as never,
    );

    renderPage("/registry/mcp/mcp_fs?project_id=project-1&oauth=success&sync=failed");

    expect(
      await screen.findByText("OAuth connected, but tools could not be synced. Use Sync to retry."),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Sync" })).toBeEnabled();
    expect((await screen.findAllByText("connected")).length).toBeGreaterThan(0);
    expect(mockedSyncMcpConnector).not.toHaveBeenCalled();
    expect(screen.getByTestId("location-search")).toHaveTextContent("?project_id=project-1");
  });

  it("does not sync for a bare or forged OAuth success marker", async () => {
    renderPage("/registry/mcp/mcp_fs?project_id=project-1&oauth=success");

    expect(await screen.findByText("OAuth authorization completed successfully.")).toBeVisible();
    expect(mockedSyncMcpConnector).not.toHaveBeenCalled();
    expect(screen.getByTestId("location-search")).toHaveTextContent("?project_id=project-1");
  });

  it("shows pending state and returned tools for manual Sync", async () => {
    const user = userEvent.setup();
    let resolveSync!: (value: ReturnType<typeof connectorFixture>) => void;
    mockedSyncMcpConnector.mockImplementation(
      () => new Promise((resolve) => { resolveSync = resolve; }) as never,
    );

    renderPage();
    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Sync" }));

    expect(screen.getByRole("button", { name: "Syncing…" })).toBeDisabled();
    resolveSync(connectorFixture({
      discovered_tools_jsonb: [
        {
          remote_tool_name: "gmail.labels.list",
          projected_tool_id: "fs.gmail.labels.list",
          display_name: "Gmail Labels List",
          description: "List Gmail labels.",
          input_schema: {},
          output_schema: {},
          annotations: {},
        },
      ],
    }));
    expect(await screen.findByText("Gmail Labels List")).toBeVisible();
    expect(await screen.findByText("MCP sync completed.")).toBeVisible();
  });

  it("keeps details readable but hides every connector mutation from viewers", async () => {
    mockedUseHasCapability.mockReturnValue(false);
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({ auth_mode: "oauth2", auth_status: "connected", oauth_status: oauthStatusFixture({ status: "connected", has_access_token: true }) }) as never,
    );
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    expect(screen.queryByRole("button", { name: "Sync" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Health" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit settings" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start Authorization" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Replace credentials" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save allowlist" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Toggle Catalog Read/u })).not.toBeInTheDocument();
  });

  it("renders an explicit inert not-found state for a purged connector deep link", async () => {
    mockedFetchMcpConnector.mockRejectedValue(
      new HttpError({ payload: {}, status: 404, url: "/api/mcp/mcp_fs" }),
    );

    renderPage();

    const notFoundHeading = await screen.findByRole("heading", { name: "MCP connector not found" });
    expect(notFoundHeading).toBeInTheDocument();
    // The not-found branch is a branch assigned to pageBody above the
    // container; it must still land inside PageContainer rather than escape it.
    expect(
      within(screen.getByTestId("page-container")).getByRole("heading", { name: "MCP connector not found" }),
    ).toBe(notFoundHeading);
    expect(screen.getByRole("link", { name: "Back to MCP" })).toHaveAttribute(
      "href",
      "/registry/mcp?project_id=project-1",
    );
    expect(screen.queryByRole("button", { name: "Sync" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Health" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Overview" })).not.toBeInTheDocument();
  });

  it("keeps non-404 connector failures distinct and retryable", async () => {
    const user = userEvent.setup();
    mockedFetchMcpConnector.mockRejectedValue(
      new HttpError({ payload: {}, status: 503, url: "/api/mcp/mcp_fs" }),
    );

    renderPage();

    const loadFailedHeading = await screen.findByRole("heading", { name: "MCP connector could not be loaded" });
    expect(loadFailedHeading).toBeInTheDocument();
    expect(
      within(screen.getByTestId("page-container")).getByRole("heading", { name: "MCP connector could not be loaded" }),
    ).toBe(loadFailedHeading);
    expect(screen.queryByText("MCP connector not found")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sync" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mockedFetchMcpConnector).toHaveBeenCalledTimes(2));
  });

  it("starts with zero selected tools after sync discovery", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Flow Steward MCP" })).toBeInTheDocument();
    expect(screen.getByText(/Sync status: success/u)).toBeVisible();
    expect(screen.queryByText("Last sync failed")).not.toBeInTheDocument();
    expect(screen.queryByText("Sync completed with warnings")).not.toBeInTheDocument();
    expect(screen.getByText("0 selected")).toBeInTheDocument();
    expect(screen.getByText("1 remote tools")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Toggle Catalog Read" })).not.toBeChecked();
  });

  it("renders a successful sync message as a warning instead of a failure", async () => {
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({
        last_sync_status: "success",
        last_sync_error: "Catalog Read is in Trash. Restore or purge it before syncing again.",
      }) as never,
    );
    renderPage();

    expect(await screen.findByText("Sync completed with warnings")).toBeVisible();
    expect(
      screen.getByText("Catalog Read is in Trash. Restore or purge it before syncing again."),
    ).toBeVisible();
    expect(screen.queryByText("Last sync failed")).not.toBeInTheDocument();
  });

  it("renders a failed sync error as a failure alert", async () => {
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({
        last_sync_status: "failed",
        last_sync_error: "Remote MCP endpoint did not respond.",
      }) as never,
    );
    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Last sync failed");
    expect(alert).toHaveTextContent("Remote MCP endpoint did not respond.");
    expect(screen.queryByText("Sync completed with warnings")).not.toBeInTheDocument();
  });

  it("keeps a trashed remote action discoverable but not selectable", async () => {
    const user = userEvent.setup();
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({
        allowlisted_tools: ["fs.catalog.read"],
        projected_tools: [],
        discovered_tools_jsonb: [
          {
            remote_tool_name: "catalog.read",
            projected_tool_id: "fs.catalog.read",
            display_name: "Catalog Read",
            description: "Read catalog rows.",
            input_schema: {},
            output_schema: {},
            annotations: {},
            lifecycle_state: "in_trash",
            can_publish: false,
            availability_reason_code: "projected_tool_in_trash",
            availability_reason_text: "This action is in Trash and will not be republished by Sync. Restore it from Trash or purge it there before adding it again.",
            recovery_action: "open_trash",
            deleted_at: "2026-08-22T12:00:00Z",
            deleted_by: "admin-a",
          },
        ],
      }) as never,
    );
    renderPage();

    expect(await screen.findByText("In Trash")).toBeInTheDocument();
    const checkbox = screen.getByRole("checkbox", { name: "Toggle Catalog Read" });
    expect(checkbox).toBeDisabled();
    expect(checkbox).not.toBeChecked();
    expect(screen.getByText("0 selected")).toBeInTheDocument();
    expect(screen.getByText(/will not be republished by Sync/u)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View in Trash" })).toHaveAttribute(
      "href",
      "/trash?entity_type=registry_tool&entity_id=fs.catalog.read",
    );
    expect(screen.getByText(/Moved to Trash on .* by admin-a/u)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save allowlist" }));
    await waitFor(() => {
      expect(mockedUpdateMcpConnector).toHaveBeenCalledWith("mcp_fs", {
        allowlisted_tools: [],
      }, "project-1");
    });
  });

  it("saves the selected allowlist through the MCP update endpoint", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("checkbox", { name: "Toggle Catalog Read" }));
    await user.click(screen.getByRole("button", { name: "Save allowlist" }));

    await waitFor(() => {
      expect(mockedUpdateMcpConnector).toHaveBeenCalledWith("mcp_fs", {
        allowlisted_tools: ["fs.catalog.read"],
      }, "project-1");
    });
  });

  it("edits connector settings without sending stored headers back to the browser", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    expect(screen.getByRole("button", { name: "Add replacement header" })).toBeInTheDocument();
    await user.clear(screen.getByRole("textbox", { name: "Endpoint" }));
    await user.type(screen.getByRole("textbox", { name: "Endpoint" }), "https://new.example/mcp");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => {
      expect(mockedUpdateMcpConnector).toHaveBeenCalledWith("mcp_fs", {
        display_name: "Flow Steward MCP",
        base_url: "https://new.example/mcp",
        transport: "streamable_http",
        auth_mode: "none",
        namespace_prefix: "fs",
        description: "Registry MCP",
        enabled: true,
        oauth_config: {
          requested_scopes: [],
          authorization_parameters: {},
          refresh_token_required: false,
        },
      }, "project-1");
    });
  });

  it("sends headers only when an operator explicitly replaces them", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    await user.click(screen.getByRole("button", { name: "Add replacement header" }));
    await user.type(screen.getByRole("textbox", { name: "Replacement header key" }), "X-Api-Key");
    await user.type(screen.getByLabelText("Replacement header value"), "replacement");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => {
      expect(mockedUpdateMcpConnector).toHaveBeenCalledWith("mcp_fs", {
        display_name: "Flow Steward MCP",
        base_url: "https://fs.example/mcp",
        transport: "streamable_http",
        auth_mode: "none",
        namespace_prefix: "fs",
        description: "Registry MCP",
        enabled: true,
        headers_mode: "replace",
        headers: { "X-Api-Key": "replacement" },
        oauth_config: {
          requested_scopes: [],
          authorization_parameters: {},
          refresh_token_required: false,
        },
      }, "project-1");
    });
  });

  it("explicitly clears masked stored headers without exposing their values", async () => {
    const user = userEvent.setup();
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({ auth_mode: "bearer", header_names: ["Authorization"] }) as never,
    );
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    expect(screen.queryByDisplayValue(/configured-token/u)).not.toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Stored headers action" }));
    await user.click(screen.getByRole("option", { name: "Clear" }));
    expect(screen.getByText("Saved headers will be removed.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => {
      expect(mockedUpdateMcpConnector).toHaveBeenCalledWith(
        "mcp_fs",
        expect.objectContaining({ auth_mode: "bearer", headers_mode: "clear" }),
        "project-1",
      );
    });
  });

  it("rejects Replace with no completed header rows instead of silently keeping secrets", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    await user.click(screen.getByRole("combobox", { name: "Stored headers action" }));
    await user.click(screen.getByRole("option", { name: "Replace" }));
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(
      await screen.findByText(
        "Add at least one replacement header, or choose Keep existing or Clear.",
      ),
    ).toBeInTheDocument();
    const replacementHeaders = screen.getByRole("group", { name: "Replacement headers" });
    expect(replacementHeaders).toHaveClass("fs-field-error-state");
    expect(replacementHeaders).toHaveFocus();
    expect(mockedUpdateMcpConnector).not.toHaveBeenCalled();
  });

  it("clears credential headers when switching bearer authentication to OAuth", async () => {
    const user = userEvent.setup();
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({ auth_mode: "bearer", header_names: ["Authorization"] }) as never,
    );
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    await user.click(screen.getByRole("combobox", { name: "Authentication" }));
    await user.click(screen.getByRole("option", { name: "OAuth 2.0" }));
    expect(screen.getByText("Saved headers will be removed.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => {
      expect(mockedUpdateMcpConnector).toHaveBeenCalledWith(
        "mcp_fs",
        expect.objectContaining({ auth_mode: "oauth2", headers_mode: "clear" }),
        "project-1",
      );
    });
  });

  it("cancels an explicit header clear without sending an update", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    await user.click(screen.getByRole("combobox", { name: "Stored headers action" }));
    await user.click(screen.getByRole("option", { name: "Clear" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(mockedUpdateMcpConnector).not.toHaveBeenCalled();
  });

  it("keeps the settings dialog open when a replacement header row is incomplete", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    await user.click(screen.getByRole("button", { name: "Add replacement header" }));
    await user.type(screen.getByRole("textbox", { name: "Replacement header key" }), "X-Api-Key");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByText("Replacement header rows require both a key and value.")).toBeInTheDocument();
    expect(mockedUpdateMcpConnector).not.toHaveBeenCalled();
  });

  it("only advertises Streamable HTTP in edit settings", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));

    const dialog = screen.getByRole("dialog");
    const transport = within(dialog).getByRole("combobox", { name: "Transport" });
    // `DsSelect` is a trigger plus a listbox, so its options exist only while the
    // menu is open — a native `<select>` kept them in the DOM at all times. The
    // claim under test is unchanged: exactly one transport is offered.
    expect(transport).toHaveTextContent("Streamable HTTP");
    await user.click(transport);
    // The menu lives in the viewport layer, outside the dialog's own tree.
    const transports = screen.getByRole("listbox", { name: "Transport options" });
    expect(within(transports).getAllByRole("option")).toHaveLength(1);
    expect(within(transports).getByRole("option", { name: "Streamable HTTP" })).toBeInTheDocument();
    expect(within(transports).queryByRole("option", { name: "SSE" })).not.toBeInTheDocument();
    expect(within(transports).queryByRole("option", { name: "stdio" })).not.toBeInTheDocument();
    // Not Escape: the dialog listens for it too and would close entirely.
    await user.click(transport);
    expect(
      within(dialog).getByText(
        "The current tool runtime supports streamable HTTP MCP endpoints.",
      ),
    ).toBeVisible();
  });

  it("keeps edit modal focus contained and restores the opener on Escape", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    const opener = screen.getByRole("button", { name: "Edit settings" });
    await user.click(opener);

    const dialog = screen.getByRole("dialog", {
      name: "Edit MCP connector settings",
    });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveFocus();

    // Containment is the claim, asserted at both ends of the tab ring.
    // `DsDialog` adds a close button the old dialog did not have, so the order
    // now starts at Close and ends at Save — tabbing off either end wraps to the
    // other rather than escaping to the page.
    const close = within(dialog).getByRole("button", {
      name: "Close Edit MCP connector settings",
    });
    const save = within(dialog).getByRole("button", { name: "Save settings" });
    save.focus();
    await user.tab();
    expect(close).toHaveFocus();
    close.focus();
    await user.tab({ shift: true });
    expect(save).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "Edit MCP connector settings" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("keeps edit open and focuses app-owned required and URL errors", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    const dialog = screen.getByRole("dialog");
    const name = within(dialog).getByRole("textbox", { name: "Name" });
    const endpoint = within(dialog).getByRole("textbox", { name: "Endpoint" });
    expect(name).toBeRequired();
    expect(endpoint).toBeRequired();
    expect(within(dialog).getByText("Name *")).toBeVisible();
    expect(within(dialog).getByText("Endpoint *")).toBeVisible();

    await user.clear(name);
    await user.click(within(dialog).getByRole("button", { name: "Save settings" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Name is required.");
    expect(name).toHaveFocus();
    expect(mockedUpdateMcpConnector).not.toHaveBeenCalled();

    await user.type(name, "Flow Steward MCP");
    await user.clear(endpoint);
    await user.type(endpoint, "not-a-url");
    await user.click(within(dialog).getByRole("button", { name: "Save settings" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Enter a valid HTTP(S) endpoint URL.",
    );
    expect(endpoint).toHaveFocus();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(mockedUpdateMcpConnector).not.toHaveBeenCalled();
  });

  it("maps incomplete replacement-header rows to a stable inline error", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Add replacement header" }));
    const headers = within(dialog).getByRole("textbox", { name: "Replacement header key" });
    await user.type(headers, "X-Api-Key");
    await user.click(within(dialog).getByRole("button", { name: "Save settings" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Replacement header rows require both a key and value.",
    );
    expect(headers).toHaveFocus();
    expect(headers).toHaveAttribute("aria-invalid", "true");
    expect(mockedUpdateMcpConnector).not.toHaveBeenCalled();
  });

  it("maps each invalid repeater row to its own section and focus target", async () => {
    const user = userEvent.setup();
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({
        oauth_authorization_parameters: { prompt: "consent" },
      }) as never,
    );
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Add replacement header" }));
    const headerKey = within(dialog).getByRole("textbox", { name: "Replacement header key" });
    await user.type(headerKey, "X-Api-Key");
    await user.click(within(dialog).getByRole("button", { name: "Save settings" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Replacement header rows require both a key and value.",
    );
    expect(headerKey).toHaveFocus();
    expect(headerKey).toHaveAttribute("aria-invalid", "true");
    expect(within(dialog).getByRole("textbox", { name: "Authorization parameter key" }))
      .toHaveAttribute("aria-invalid", "false");

    await user.type(within(dialog).getByLabelText("Replacement header value"), "value");
    await user.clear(within(dialog).getByRole("textbox", { name: "Authorization parameter value" }));
    await user.click(within(dialog).getByRole("button", { name: "Save settings" }));

    const authorizationKey = within(dialog).getByRole("textbox", { name: "Authorization parameter key" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Authorization parameter rows require both a key and value.",
    );
    expect(authorizationKey).toHaveFocus();
    expect(authorizationKey).toHaveAttribute("aria-invalid", "true");
    expect(mockedUpdateMcpConnector).not.toHaveBeenCalled();
  });

  it("rejects reserved OAuth parameters in edit settings", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Add authorization parameter" }));
    const key = within(dialog).getByRole("textbox", { name: "Authorization parameter key" });
    await user.type(key, "dbPasswd");
    await user.type(within(dialog).getByLabelText("Authorization parameter value"), "must-not-leak");
    await user.click(within(dialog).getByRole("button", { name: "Save settings" }));

    expect(await within(dialog).findByText("Authorization parameters key is reserved and cannot be overridden.")).toBeVisible();
    expect(key).toHaveAttribute("aria-invalid", "true");
    expect(key).toHaveFocus();
    expect(mockedUpdateMcpConnector).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/added to the browser authorization URL/u)).toBeVisible();
    expect(within(dialog).getByTestId("mcp-settings-authorization-parameters-help")).toHaveClass("fs-field", "full");
  });

  it("allows public OAuth authorization_details in edit settings", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Add authorization parameter" }));
    await user.type(within(dialog).getByRole("textbox", { name: "Authorization parameter key" }), "authorization_details");
    await user.type(within(dialog).getByLabelText("Authorization parameter value"), "catalog.read");
    await user.click(within(dialog).getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(mockedUpdateMcpConnector).toHaveBeenCalled());
    expect(mockedUpdateMcpConnector.mock.calls[0]?.[1]).toMatchObject({
      oauth_config: { authorization_parameters: { authorization_details: "catalog.read" } },
    });
  });

  it("does not misreport a connected OAuth account as not connected when identity is unavailable", async () => {
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({ auth_mode: "oauth2", oauth_status: oauthStatusFixture({ status: "connected" }) }) as never,
    );
    mockedFetchMcpConnectorOAuthStatus.mockResolvedValue(
      oauthStatusFixture({ status: "connected", connected_identity: "" }) as never,
    );
    renderPage();

    await screen.findByRole("heading", { name: "OAuth" });
    expect(screen.getByText("Identity unavailable")).toBeVisible();
  });

  it("shows masked stored credentials and validates replacement fields without revealing values", async () => {
    const user = userEvent.setup();
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({
        auth_mode: "oauth2",
        auth_status: "auth_required",
        has_client_id: true,
        has_client_secret: true,
        oauth_status: oauthStatusFixture(),
      }) as never,
    );
    renderPage();

    const oauthCard = await screen.findByTestId("mcp-oauth-card");
    expect(screen.getAllByText("••••••••")).toHaveLength(2);
    expect(screen.getAllByText("Stored")).toHaveLength(2);
    expect(screen.queryByText("client-secret-value")).not.toBeInTheDocument();
    expect(within(oauthCard).getByRole("button", { name: "Start Authorization" })).toBeVisible();
    expect(within(oauthCard).queryByRole("button", { name: "Refresh" })).not.toBeInTheDocument();
    expect(within(oauthCard).queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Replace credentials" }));
    const clientId = screen.getByRole("textbox", { name: "Client ID" });
    const clientSecret = screen.getByLabelText("Client Secret");
    const save = screen.getByRole("button", { name: "Save credentials" });
    expect(clientId).toBeRequired();
    expect(clientSecret).toBeRequired();
    expect(save).toBeDisabled();
    expect(screen.getByText("Client ID *")).toBeVisible();
    expect(screen.getByText("Client Secret *")).toBeVisible();

    await user.type(clientId, "   ");
    await user.type(clientSecret, "  opaque secret  ");
    expect(save).toBeEnabled();
    await user.click(save);
    expect(await screen.findByRole("alert")).toHaveTextContent("Client ID is required.");
    expect(clientId).toHaveFocus();
    expect(clientId).toHaveAttribute("aria-invalid", "true");

    await user.clear(clientId);
    await user.type(clientId, "client-1");
    await user.click(save);
    await waitFor(() => expect(mockedSaveMcpConnectorOAuthCredentials).toHaveBeenCalledWith(
      "mcp_fs",
      { client_id: "client-1", client_secret: "  opaque secret  " },
      "project-1",
    ));
  });

  it("shows only Start Authorization for a fresh OAuth connector", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({
        auth_mode: "oauth2",
        auth_status: "auth_required",
        oauth_status: oauthStatusFixture(),
      }) as never,
    );
    renderPage();

    const oauthCard = await screen.findByTestId("mcp-oauth-card");
    expect(within(oauthCard).getByRole("textbox", { name: "OAuth redirect URI" })).toHaveValue(
      "https://flow.example.test/api/integrations/oauth/callback",
    );
    expect(within(oauthCard).getByText("Register this exact URI with your OAuth provider before starting authorization.")).toBeVisible();
    await user.click(within(oauthCard).getByRole("button", { name: "Copy OAuth redirect URI" }));
    expect(writeText).toHaveBeenCalledWith("https://flow.example.test/api/integrations/oauth/callback");
    expect(within(oauthCard).getByRole("button", { name: "Start Authorization" })).toBeVisible();
    expect(within(oauthCard).queryByRole("button", { name: "Replace credentials" })).not.toBeInTheDocument();
    expect(within(oauthCard).queryByRole("button", { name: "Refresh" })).not.toBeInTheDocument();
    expect(within(oauthCard).queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
  });

  it("requests manual credentials by error code and resumes authorization after save", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const openMock = vi.fn();
    vi.stubGlobal("open", openMock);
    mockedStartMcpConnectorOAuth
      .mockRejectedValueOnce(new HttpError({
        payload: {
          code: "manual_credentials_required",
          message: "This authorization server requires client credentials.",
        },
        status: 400,
        url: "/api/mcp/mcp_fs/oauth/start",
      }))
      .mockResolvedValueOnce({
        authorization_url: "https://auth.example/authorize?state=abc",
      } as never);
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({
        auth_mode: "oauth2",
        auth_status: "auth_required",
        oauth_status: oauthStatusFixture(),
      }) as never,
    );
    renderPage();

    expect(await screen.findByRole("heading", { name: "OAuth" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start Authorization" }));
    const oauthCard = await screen.findByTestId("mcp-oauth-card");
    expect(await within(oauthCard).findByText(/requires client credentials/u)).toBeVisible();
    expect(within(oauthCard).getByTestId("mcp-oauth-credentials-guidance")).toHaveClass("fs-field", "full");
    expect(within(oauthCard).getByRole("button", { name: "Save credentials" })).toBeVisible();
    expect(within(oauthCard).queryByRole("button", { name: "Start Authorization" })).not.toBeInTheDocument();
    expect(within(oauthCard).queryByRole("button", { name: "Refresh" })).not.toBeInTheDocument();
    expect(within(oauthCard).queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
    expect(within(oauthCard).queryByRole("alert")).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Client ID" }), "client-1");
    await user.type(screen.getByLabelText("Client Secret"), "secret-1");
    await user.click(screen.getByRole("button", { name: "Save credentials" }));
    await waitFor(() => expect(mockedSaveMcpConnectorOAuthCredentials).toHaveBeenCalledWith(
      "mcp_fs",
      { client_id: "client-1", client_secret: "secret-1" },
      "project-1",
    ));

    await waitFor(() => expect(mockedStartMcpConnectorOAuth).toHaveBeenCalledTimes(2));
    expect(mockedStartMcpConnectorOAuth).toHaveBeenLastCalledWith(
      "mcp_fs",
      "project-1",
      {
        return_path: "/registry/mcp/mcp_fs?project_id=project-1",
      },
    );
    expect(openMock).toHaveBeenCalledWith(
      "https://auth.example/authorize?state=abc",
      "_self",
      "noopener,noreferrer",
    );
  });

  it("offers reauthorization and disconnect when OAuth refresh has failed", async () => {
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({
        auth_mode: "oauth2",
        auth_status: "refresh_failed",
        has_client_id: true,
        has_client_secret: true,
      }) as never,
    );
    mockedFetchMcpConnectorOAuthStatus.mockResolvedValue(
      oauthStatusFixture({
        status: "refresh_failed",
        connected_identity: "operator@example.test",
        has_access_token: true,
        has_refresh_token: true,
      }) as never,
    );
    renderPage();

    const oauthCard = await screen.findByTestId("mcp-oauth-card");
    expect(await within(oauthCard).findByRole("button", { name: "Reconnect Authorization" })).toBeVisible();
    expect(await within(oauthCard).findByRole("button", { name: "Disconnect" })).toBeVisible();
    expect(within(oauthCard).getByRole("button", { name: "Replace credentials" })).toBeVisible();
    expect(within(oauthCard).queryByRole("button", { name: "Refresh" })).not.toBeInTheDocument();
  });

  it("offers an actionable reconnect without disconnecting after insufficient_scope", async () => {
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({
        auth_mode: "oauth2",
        auth_status: "auth_required",
        oauth_requested_scopes: ["files:read", "files:write"],
      }) as never,
    );
    mockedFetchMcpConnectorOAuthStatus.mockResolvedValue(
      oauthStatusFixture({
        status: "auth_required",
        last_error: "insufficient_scope",
        has_access_token: true,
        has_refresh_token: true,
      }) as never,
    );
    renderPage();

    const oauthCard = await screen.findByTestId("mcp-oauth-card");
    expect(
      await within(oauthCard).findByText(/did not grant all configured scopes/u),
    ).toBeVisible();
    expect(
      within(oauthCard).getByRole("button", { name: "Reconnect Authorization" }),
    ).toBeVisible();
    expect(within(oauthCard).getByRole("button", { name: "Disconnect" })).toBeVisible();
    expect(within(oauthCard).getByText("files:write")).toBeVisible();
  });

  it("offers authorization restart for an expired access-only grant", async () => {
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({ auth_mode: "oauth2", auth_status: "auth_required" }) as never,
    );
    mockedFetchMcpConnectorOAuthStatus.mockResolvedValue(
      oauthStatusFixture({
        status: "auth_required",
        last_error: "MCP OAuth refresh_token is missing; reconnect required",
        has_access_token: true,
        has_refresh_token: false,
      }) as never,
    );
    renderPage();

    const oauthCard = await screen.findByTestId("mcp-oauth-card");
    expect(
      await within(oauthCard).findByRole("button", { name: "Reconnect Authorization" }),
    ).toBeVisible();
    expect(within(oauthCard).getByRole("button", { name: "Disconnect" })).toBeVisible();
    expect(within(oauthCard).getByRole("button", { name: "Replace credentials" })).toBeVisible();
  });

  it("offers reauthorization after the OAuth grant expires", async () => {
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({ auth_mode: "oauth2", auth_status: "expired" }) as never,
    );
    mockedFetchMcpConnectorOAuthStatus.mockResolvedValue(
      oauthStatusFixture({
        status: "expired",
        connected_identity: "operator@example.test",
        has_access_token: true,
        has_refresh_token: true,
      }) as never,
    );
    renderPage();

    const oauthCard = await screen.findByTestId("mcp-oauth-card");
    expect(
      await within(oauthCard).findByRole("button", { name: "Reconnect Authorization" }),
    ).toBeVisible();
    expect(within(oauthCard).getByRole("button", { name: "Disconnect" })).toBeVisible();
  });

  it("shows disconnect but no authorization restart for a connected account", async () => {
    mockedFetchMcpConnector.mockResolvedValue(
      connectorFixture({ auth_mode: "oauth2", auth_status: "connected" }) as never,
    );
    mockedFetchMcpConnectorOAuthStatus.mockResolvedValue(
      oauthStatusFixture({ status: "connected", has_access_token: true }) as never,
    );
    renderPage();

    const oauthCard = await screen.findByTestId("mcp-oauth-card");
    expect(await within(oauthCard).findByRole("button", { name: "Disconnect" })).toBeVisible();
    expect(within(oauthCard).queryByRole("button", { name: "Start Authorization" })).not.toBeInTheDocument();
    expect(within(oauthCard).queryByRole("button", { name: "Refresh" })).not.toBeInTheDocument();
  });

  it("focuses and marks headers rejected by backend validation", async () => {
    const user = userEvent.setup();
    mockedUpdateMcpConnector.mockRejectedValueOnce(new HttpError({
      payload: { error: "oauth_header_collision", field: "headers" },
      status: 400,
      url: "/api/mcp/mcp_fs",
    }));
    renderPage();
    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    await user.click(screen.getByRole("button", { name: "Add replacement header" }));
    await user.type(screen.getByRole("textbox", { name: "Replacement header key" }), "Authorization");
    await user.type(screen.getByLabelText("Replacement header value"), "opaque");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    const headerKey = await screen.findByRole("textbox", { name: "Replacement header key" });
    await waitFor(() => expect(headerKey).toHaveFocus());
    expect(headerKey).toHaveAttribute("aria-invalid", "true");
  });

  it.each([
    ["requested_scopes", "Requested scopes"],
    ["refresh_token_required", "Refresh token required"],
  ])("focuses the %s field rejected by backend OAuth validation", async (field, label) => {
    const user = userEvent.setup();
    mockedUpdateMcpConnector.mockRejectedValueOnce(new HttpError({
      payload: { error: "invalid_oauth_config", field },
      status: 400,
      url: "/api/mcp/mcp_fs",
    }));
    renderPage();
    await screen.findByRole("heading", { name: "Flow Steward MCP" });
    await user.click(screen.getByRole("button", { name: "Edit settings" }));
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    const group = await screen.findByRole("group", { name: label });
    const target = field === "requested_scopes"
      ? within(group).queryByRole("textbox") || group
      : group;
    await waitFor(() => expect(target).toHaveFocus());
    if (target === group) {
      expect(group).toHaveClass("fs-field-error-state");
    } else {
      expect(target).toHaveAttribute("aria-invalid", "true");
    }
    expect(target).toHaveAttribute("aria-describedby", "mcp-settings-error");
  });
});
