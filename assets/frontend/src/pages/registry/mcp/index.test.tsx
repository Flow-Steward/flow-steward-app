import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { RegistryMcpPage } from "@/pages/registry/mcp";
import { HttpError } from "@/lib/http/errors";
import {
  checkMcpConnectorHealth,
  createMcpConnector,
  deleteMcpConnector,
  fetchMcpConnectorDeletePlan,
  fetchMcpConnectors,
  syncMcpConnector,
} from "@/pages/registry/mcp/api";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { useHasCapability } from "@/lib/auth/capabilities";

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));

vi.mock("@/pages/projects/list/api", () => ({ fetchProjectsList: vi.fn() }));

vi.mock("@/pages/registry/mcp/api", () => ({
  checkMcpConnectorHealth: vi.fn(),
  createMcpConnector: vi.fn(),
  deleteMcpConnector: vi.fn(),
  fetchMcpConnectorDeletePlan: vi.fn(),
  fetchMcpConnector: vi.fn(),
  fetchMcpConnectors: vi.fn(),
  syncMcpConnector: vi.fn(),
  updateMcpConnector: vi.fn(),
}));

const mockedCheckMcpConnectorHealth = vi.mocked(checkMcpConnectorHealth);
const mockedCreateMcpConnector = vi.mocked(createMcpConnector);
const mockedDeleteMcpConnector = vi.mocked(deleteMcpConnector);
const mockedFetchMcpConnectorDeletePlan = vi.mocked(fetchMcpConnectorDeletePlan);
const mockedFetchMcpConnectors = vi.mocked(fetchMcpConnectors);
const mockedSyncMcpConnector = vi.mocked(syncMcpConnector);
const mockedFetchProjectsList = vi.mocked(fetchProjectsList);
const mockedUseHasCapability = vi.mocked(useHasCapability);

function renderPage(initialEntry = "/registry/mcp?project_id=project-1") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<RegistryMcpPage />} path="/registry/mcp" />
          <Route element={<div>mcp detail page</div>} path="/registry/mcp/:connectorId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RegistryMcpPage", () => {
  beforeEach(() => {
    mockedUseHasCapability.mockReturnValue(true);
    mockedFetchProjectsList.mockReset();
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "project-1", name: "Project One" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
    mockedCheckMcpConnectorHealth.mockReset();
    mockedCreateMcpConnector.mockReset();
    mockedDeleteMcpConnector.mockReset();
    mockedFetchMcpConnectorDeletePlan.mockReset();
    mockedFetchMcpConnectors.mockReset();
    mockedSyncMcpConnector.mockReset();

    mockedFetchMcpConnectors.mockResolvedValue([
      {
        connector_id: "mcp_fs",
        account_id: "acc_default",
        project_id: "project-1",
        tenant_id: "tenant-1",
        display_name: "Flow Steward MCP",
        description: "Registry MCP",
        transport: "streamable_http",
        base_url: "https://fs.example/mcp",
        auth_mode: "bearer",
        namespace_prefix: "fs",
        allowlisted_tools: ["fs.catalog.read"],
        discovered_tools_jsonb: [],
        projected_tools: [],
        status: "active",
        auth_status: "connected",
        oauth_status: {
          status: "connected",
          issuer: "https://auth.example",
          registration_mode: "client_metadata_document",
          resource: "https://fs.example/mcp",
          expires_at: "2026-05-20T06:00:00Z",
          granted_scopes: ["tools.read"],
          connected_identity: "operator@example.test",
          has_access_token: true,
          has_refresh_token: true,
          last_error: "",
        },
        last_synced_at: "2026-05-20T05:00:00Z",
        last_sync_status: "success",
        last_sync_error: "",
        enabled: true,
        created_at: "2026-05-20T04:00:00Z",
        updated_at: "2026-05-20T05:00:00Z",
      },
    ] as never);
    mockedCreateMcpConnector.mockResolvedValue({} as never);
    mockedCheckMcpConnectorHealth.mockResolvedValue({
      connector_id: "mcp_fs",
      status: "active",
      latency_ms: 42,
      error: null,
      checked_at: "2026-05-20T05:00:00Z",
    } as never);
    mockedDeleteMcpConnector.mockResolvedValue({
      status: "deleted",
      connector_id: "mcp_fs",
    } as never);
    mockedFetchMcpConnectorDeletePlan.mockResolvedValue({
      connector_id: "mcp_fs",
      connector: {
        display_name: "Flow Steward MCP",
        enabled: true,
        status: "active",
      },
      remove: {
        tools: [
          {
            tool_id: "fs.catalog.read",
            display_name: "Catalog Read",
            affected_workflow_ids: ["wf_direct", "wf_agent"],
          },
          {
            tool_id: "fs.catalog.write",
            display_name: "Catalog Write",
            affected_workflow_ids: ["wf_agent"],
          },
        ],
      },
      affected: {
        workflows: [
          {
            workflow_id: "wf_direct",
            display_name: "Direct workflow",
            status: "active",
            enabled: true,
            tool_ids: ["fs.catalog.read"],
            via_connector: false,
          },
          {
            workflow_id: "wf_agent",
            display_name: "Agent workflow",
            status: "disabled",
            enabled: false,
            tool_ids: ["fs.catalog.read", "fs.catalog.write"],
            via_connector: false,
          },
        ],
      },
      warnings: [
        "Running workflows are not cancelled automatically; a later tool call may fail.",
      ],
      revision: "revision-1",
    });
    mockedSyncMcpConnector.mockResolvedValue({} as never);
  });

  it("shows project-scoped mutation controls without platform-admin capability", async () => {
    renderPage();

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^Actions for / }));

    expect(screen.queryByText("Read only")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New MCP" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Sync" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Health" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Delete" })).toBeInTheDocument();
    expect(await screen.findByRole("menuitem", { name: "View" })).toBeInTheDocument();
  });

  it("keeps MCP visible but hides mutation controls from viewers", async () => {
    mockedUseHasCapability.mockReturnValue(false);
    renderPage();

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^Actions for / }));
    expect(screen.queryByRole("button", { name: "New MCP" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Sync" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Health" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Delete" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "View" })).toBeInTheDocument();
  });

  it("focuses and marks the endpoint rejected by backend validation", async () => {
    const user = userEvent.setup();
    const canaryValue = "query-canary-ui-7f31c9";
    const message = "MCP connector endpoint must not include credentials in query parameters; use Headers or OAuth settings.";
    mockedCreateMcpConnector.mockRejectedValueOnce(new HttpError({
      payload: {
        error: "mcp_endpoint_query_credentials_forbidden",
        field: "base_url",
        message,
      },
      status: 400,
      url: "/api/mcp",
    }));
    renderPage();
    await user.click(await screen.findByRole("button", { name: "New MCP" }));
    await user.type(screen.getByLabelText("Name *"), "Unsafe MCP");
    await user.type(
      screen.getByLabelText("Endpoint *"),
      `https://public.example.test/mcp?key=${canaryValue}`,
    );
    await user.click(screen.getByRole("button", { name: "Save MCP" }));

    const endpoint = await screen.findByLabelText("Endpoint *");
    await waitFor(() => expect(endpoint).toHaveFocus());
    expect(endpoint).toHaveAttribute("aria-invalid", "true");
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(alert).not.toHaveTextContent(canaryValue);
  });

  it.each([
    ["requested_scopes", "Requested scopes"],
    ["refresh_token_required", "Refresh token required"],
  ])("focuses the %s field rejected by backend OAuth validation", async (field, label) => {
    const user = userEvent.setup();
    mockedCreateMcpConnector.mockRejectedValueOnce(new HttpError({
      payload: { error: "invalid_oauth_config", field },
      status: 400,
      url: "/api/mcp",
    }));
    renderPage();
    await user.click(await screen.findByRole("button", { name: "New MCP" }));
    await user.type(screen.getByLabelText("Name *"), "OAuth MCP");
    await user.type(screen.getByLabelText("Endpoint *"), "https://public.example.test/mcp");
    await user.click(screen.getByRole("button", { name: "Save MCP" }));

    const target = await screen.findByRole("group", { name: label });
    await waitFor(() => expect(target).toHaveFocus());
    expect(target).toHaveClass("fs-field-error-state");
    expect(target).toHaveAttribute("aria-describedby", "mcp-create-error");
  });

  it("lists every project's connectors in one request, with a Project column", async () => {
    renderPage("/registry/mcp");

    await waitFor(() => expect(mockedFetchMcpConnectors).toHaveBeenCalledWith());
    expect(await screen.findByRole("columnheader", { name: "Project" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Endpoint" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Project scope" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add filter" })).toBeInTheDocument();
  });

  it("narrows the list to the project the URL filters to", async () => {
    renderPage("/registry/mcp?project_id=project-2");

    expect(await screen.findByText("No MCP connectors match this search")).toBeInTheDocument();
    expect(mockedFetchMcpConnectors).toHaveBeenCalledWith();
  });

  it("asks which project a new connector belongs to, with the only one chosen", async () => {
    const user = userEvent.setup();
    renderPage("/registry/mcp");

    await user.click(await screen.findByRole("button", { name: "New MCP" }));
    const dialog = screen.getByRole("dialog", { name: "Create MCP connector" });
    expect(within(dialog).getByRole("combobox", { name: "Project" })).toHaveTextContent("Project One");
  });

  it("renders the canonical MCP catalog table", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "MCP" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "Name" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "Auth" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "Allowed Tools" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Flow Steward MCP")).toBeInTheDocument();

    // The standalone Open link became row-menu navigation; assert where it goes.
    await user.click(screen.getByRole("button", { name: "Actions for Flow Steward MCP" }));
    await user.click(await screen.findByRole("menuitem", { name: "View" }));
    expect(await screen.findByText("mcp detail page")).toBeInTheDocument();
  });

  it("submits the create modal without an allowlist payload", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New MCP" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Flow Steward MCP");
    await user.type(screen.getByRole("textbox", { name: "Endpoint" }), "https://fs.example/mcp");
    await user.click(screen.getByRole("button", { name: "Add headers row" }));
    await user.type(screen.getByRole("textbox", { name: "Headers key" }), "Authorization");
    const headerValue = screen.getByLabelText("Headers value");
    expect(headerValue).toHaveAttribute("type", "password");
    await user.type(headerValue, "Bearer token");
    await user.click(screen.getByRole("button", { name: "Show headers value" }));
    expect(headerValue).toHaveAttribute("type", "text");
    await user.click(screen.getByRole("button", { name: "Hide headers value" }));
    expect(headerValue).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Save MCP" }));

    await waitFor(() => {
      expect(mockedCreateMcpConnector).toHaveBeenCalled();
    });
    expect(mockedCreateMcpConnector.mock.calls[0]).toEqual([{
      display_name: "Flow Steward MCP",
      base_url: "https://fs.example/mcp",
      transport: "streamable_http",
      auth_mode: "none",
      headers: { Authorization: "Bearer token" },
      namespace_prefix: "",
      description: "",
      enabled: true,
    }, "project-1"]);
    expect(mockedCreateMcpConnector.mock.calls[0]?.[0]).not.toHaveProperty(
      "allowlisted_tools",
    );
  });

  it("opens the custom form on Name with empty generic defaults and no template picker", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New MCP" }));
    const dialog = screen.getByRole("dialog", { name: "Create MCP connector" });
    // Ready-made services moved to the Catalog tab; this form is for any other server.
    expect(within(dialog).queryByRole("combobox", { name: "Template" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveFocus();
    expect(within(dialog).getByRole("textbox", { name: "Endpoint" })).toHaveValue("");
    expect(within(dialog).getByRole("textbox", { name: "Description" })).toHaveValue("");
    expect(within(dialog).getByRole("combobox", { name: "Auth Mode" })).toHaveTextContent("none");
    expect(within(dialog).queryByRole("textbox", { name: /^Requested scope/u })).not.toBeInTheDocument();
  });

  it("keeps a newly typed requested scope mounted and focused", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New MCP" }));
    const dialog = screen.getByRole("dialog", { name: "Create MCP connector" });
    await user.click(within(dialog).getByRole("button", { name: "Add requested scope" }));
    const scope = within(dialog).getByRole("textbox", { name: "Requested scope 1" });
    const value = "https://scope.example.test/catalog.read";
    await user.type(scope, value);

    expect(scope).toHaveValue(value);
    expect(scope).toHaveFocus();
  });

  it("rejects reserved OAuth parameters in their own row", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New MCP" }));
    const dialog = screen.getByRole("dialog", { name: "Create MCP connector" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "Example OAuth MCP");
    await user.type(within(dialog).getByRole("textbox", { name: "Endpoint" }), "https://mcp.example.test/v1");
    await user.click(within(dialog).getByRole("button", { name: "Add authorization parameters row" }));
    const key = within(dialog).getByRole("textbox", { name: "Authorization parameters key" });
    await user.type(key, "clientCredential");
    await user.type(within(dialog).getByRole("textbox", { name: "Authorization parameters value" }), "must-not-leak");
    await user.click(within(dialog).getByRole("button", { name: "Save MCP" }));

    expect(await within(dialog).findByText("Authorization parameters key is reserved and cannot be overridden.")).toBeVisible();
    expect(key).toHaveAttribute("aria-invalid", "true");
    expect(key).toHaveFocus();
    expect(mockedCreateMcpConnector).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/added to the browser authorization URL/u)).toBeVisible();
    expect(within(dialog).getByTestId("mcp-create-authorization-parameters-help")).toHaveClass("fs-field", "full");
  });

  it("allows public OAuth authorization_details in its own row", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New MCP" }));
    const dialog = screen.getByRole("dialog", { name: "Create MCP connector" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "Example OAuth MCP");
    await user.type(within(dialog).getByRole("textbox", { name: "Endpoint" }), "https://mcp.example.test/v1");
    await user.click(within(dialog).getByRole("button", { name: "Add authorization parameters row" }));
    await user.type(within(dialog).getByRole("textbox", { name: "Authorization parameters key" }), "authorization_details");
    await user.type(within(dialog).getByRole("textbox", { name: "Authorization parameters value" }), "catalog.read");
    await user.click(within(dialog).getByRole("button", { name: "Save MCP" }));

    await waitFor(() => expect(mockedCreateMcpConnector).toHaveBeenCalled());
    expect(mockedCreateMcpConnector.mock.calls[0]?.[0]).toMatchObject({
      oauth_config: { authorization_parameters: { authorization_details: "catalog.read" } },
    });
  }, 15_000);

  it("only advertises the supported streamable_http transport", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New MCP" }));

    // The published Select lists its options only while the listbox is open.
    await user.click(screen.getByRole("combobox", { name: "Transport" }));
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveTextContent("streamable_http");
    expect(screen.queryByRole("option", { name: "sse" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "stdio" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: "sse" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: "stdio" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "The current tool runtime supports streamable HTTP MCP endpoints.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps create modal focus contained and restores the opener on Escape", async () => {
    const user = userEvent.setup();
    renderPage();

    const opener = await screen.findByRole("button", { name: "New MCP" });
    await user.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Create MCP connector" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveFocus();

    const close = within(dialog).getByRole("button", { name: "Close new MCP form" });
    const save = within(dialog).getByRole("button", { name: "Save MCP" });
    close.focus();
    await user.tab({ shift: true });
    expect(save).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Create MCP connector" })).not.toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("marks create identity fields required and owns validation messages", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New MCP" }));

    const name = screen.getByRole("textbox", { name: "Name" });
    const endpoint = screen.getByRole("textbox", { name: "Endpoint" });
    expect(name).toBeRequired();
    expect(endpoint).toBeRequired();
    expect(screen.getByText("Name", { selector: "label" })).toBeVisible();
    expect(screen.getByText("Endpoint", { selector: "label" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Save MCP" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required.");
    expect(name).toHaveFocus();
    expect(mockedCreateMcpConnector).not.toHaveBeenCalled();

    await user.type(name, "Flow Steward MCP");
    await user.type(endpoint, "not-a-url");
    await user.click(screen.getByRole("button", { name: "Save MCP" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Enter a valid HTTP(S) endpoint URL.",
    );
    expect(endpoint).toHaveFocus();

    await user.clear(endpoint);
    await user.type(endpoint, "https://fs.example/mcp");
    await user.click(screen.getByRole("button", { name: "Save MCP" }));
    expect(mockedCreateMcpConnector).toHaveBeenCalledTimes(1);
  }, 15_000);

  it("previews connector delete impact and cancellation performs no mutation", async () => {
    const user = userEvent.setup();
    renderPage();

    const opener = await screen.findByRole("button", { name: /^Actions for / });
    await user.click(opener);
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    const dialog = await screen.findByRole("dialog", { name: "Delete MCP connector" });
    expect(within(dialog).getByText("Tools moved to Trash")).toBeInTheDocument();
    expect(within(dialog).getByText("Catalog Read")).toBeInTheDocument();
    expect(within(dialog).getByText("Catalog Write")).toBeInTheDocument();
    expect(
      within(dialog).getByText("Workflows that will stop working"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("Direct workflow")).toBeInTheDocument();
    expect(within(dialog).getByText("Agent workflow")).toBeInTheDocument();
    expect(within(dialog).getByText(/not cancelled automatically/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Delete connector" })).toBeDisabled();
    await waitFor(() => expect(within(dialog).getByRole("checkbox")).toHaveFocus());

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "Delete MCP connector" })).not.toBeInTheDocument();
    expect(mockedDeleteMcpConnector).not.toHaveBeenCalled();
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("confirms a revision-bound delete and keeps API errors in the modal", async () => {
    const user = userEvent.setup();
    mockedDeleteMcpConnector.mockRejectedValueOnce(new Error("Delete impact changed."));
    renderPage();

    await user.click(await screen.findByRole("button", { name: /^Actions for / }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete MCP connector" });
    await user.click(within(dialog).getByRole("checkbox"));
    const confirm = within(dialog).getByRole("button", { name: "Delete connector" });
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => {
      expect(mockedDeleteMcpConnector).toHaveBeenCalledWith("mcp_fs", "revision-1", "project-1");
    });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Delete impact changed.",
    );
    expect(screen.getByRole("dialog", { name: "Delete MCP connector" })).toBeInTheDocument();
  });
});
