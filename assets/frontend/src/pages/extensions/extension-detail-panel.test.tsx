import { useState, type ComponentProps } from "react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AuthContext } from "@/lib/auth/context";
import { ExtensionDetailPanel } from "@/pages/extensions/extension-detail-panel";
import {
  ensureExtensionOAuthRedirectUri,
  fetchExtensionOAuthConfigurations,
  previewExtensionOAuthConfiguration,
  retryExtensionOAuthPostConnect,
  saveExtensionOAuthConfiguration,
  startExtensionOAuthAuthorization,
} from "@/pages/extensions/extension-oauth-api";

vi.mock("@/pages/extensions/extension-oauth-api", () => ({
  deleteExtensionOAuthConfiguration: vi.fn(),
  disconnectExtensionOAuthConnection: vi.fn(),
  ensureExtensionOAuthRedirectUri: vi.fn(),
  fetchExtensionOAuthConfigurations: vi.fn(),
  previewExtensionOAuthConfiguration: vi.fn(),
  previewExtensionOAuthConfigurationDelete: vi.fn(),
  retryExtensionOAuthPostConnect: vi.fn(),
  saveExtensionOAuthConfiguration: vi.fn(),
  startExtensionOAuthAuthorization: vi.fn(),
}));

function panelProps(): ComponentProps<typeof ExtensionDetailPanel> {
  const noop = vi.fn();
  return {
    activationBlockedReason: "",
    connectionCreationBlockedReason: "",
    activeManifestPage: undefined,
    busyById: {},
    callbackStatuses: {},
    callbacksQuery: {},
    componentErrors: {},
    componentQueryData: {},
    componentQueryParams: {},
    detailExtension: {
      extension_id: "marketplace-extension",
      support_url: "javascript:alert(document.cookie)",
      website_url: "https://vendor.example.test",
    } as never,
    detailStatus: "",
    extensionInstallId: "",
    formValuesByComponent: {},
    handleCallbackSetup: noop,
    handleExternalResourcePickerBootstrap: async () => ({}),
    handleHealthCheck: noop,
    healthStatus: "",
    handleManifestAction: noop,
    handleProjectSelection: noop,
    handleToggle: noop,
    handleUninstall: noop,
    archiveLifecycleAvailable: true,
    manifestPages: [],
    manifestQuery: { isError: false, isLoading: false },
    oauthConnectionTypes: [],
    oauthProviderReadiness: new Proxy({}, { get: () => "ready" }),
    onConnectionDelete: noop,
    onConnectionOAuth: noop,
    onConnectionOAuthDisconnect: noop,
    pendingDisconnect: null,
    onConnectionOAuthDisconnectConfirm: noop,
    onConnectionOAuthDisconnectCancel: noop,
    onOAuthConnectionsChanged: noop,
    onConnectionSave: noop,
    onConnectionSelect: noop,
    onProjectStateDelete: noop,
    onProjectStateSave: noop,
    onProjectStateSelect: noop,
    pageRequiresProject: false,
    projectConnections: [],
    projectConnectionsQuery: {
      isError: false,
      isPending: false,
      refetch: async () => undefined,
    },
    projectOptions: [],
    projectState: [],
    projectsQuery: { error: null, isError: false },
    rememberedProjectId: "",
    selectedProjectId: "",
    setActiveExtensionPageId: noop,
    setComponentQueryParams: noop,
    setFormValuesByComponent: noop,
    onFieldCorrected: noop,
  };
}

describe("ExtensionDetailPanel marketplace links", () => {
  it.each([
    ["admin", true],
    ["operator", false],
    ["viewer", false],
  ] as const)("shows global lifecycle controls to %s only when authorized", (role, visible) => {
    render(
      <AuthContext.Provider value={{ session: { roles: [role] } } as never}>
        <ExtensionDetailPanel
          {...panelProps()}
          detailExtension={{ extension_id: "fixture.extension", active: true } as never}
        />
      </AuthContext.Provider>,
    );

    for (const name of ["Disable", "Extension runtime health", "Uninstall"]) {
      if (visible) {
        expect(screen.getByRole("button", { name })).toBeInTheDocument();
      } else {
        expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
      }
    }
    if (visible) {
      expect(screen.getByText("Advanced")).toBeInTheDocument();
    } else {
      expect(screen.queryByText("Advanced")).not.toBeInTheDocument();
    }
  });

  it("shows administrators the code review folded, in plain words, with file and line", () => {
    const props = panelProps();
    render(
      <AuthContext.Provider value={{ session: { roles: ["admin"] } } as never}>
        <ExtensionDetailPanel
          {...props}
          detailExtension={{
            ...props.detailExtension,
            validation_summary: {
              status: "warning",
              validation_warnings: [
                {
                  code: "extension_code_review",
                  message: "smtp_extension.py:9: import socket opens raw network connections",
                  file_path: "smtp_extension.py",
                  field_path: "line 9",
                  severity: "warning",
                },
              ],
            },
          }}
        />
      </AuthContext.Provider>,
    );

    expect(screen.getByText("Code review")).toBeInTheDocument();
    expect(screen.getByText("1 to look at")).toBeInTheDocument();
    expect(screen.getByText("smtp_extension.py:9: import socket opens raw network connections")).toBeInTheDocument();
    expect(screen.queryByText(/Install check/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not show the code review to someone who cannot manage extensions", () => {
    const props = panelProps();
    render(
      <ExtensionDetailPanel
        {...props}
        detailExtension={{
          ...props.detailExtension,
          validation_summary: { status: "warning", validation_warnings: [{ code: "extension_code_review", message: "main.py:3: subprocess.run() starts a process", severity: "warning" }] },
        }}
      />,
    );

    expect(screen.queryByText("Code review")).not.toBeInTheDocument();
    expect(screen.queryByText(/subprocess/)).not.toBeInTheDocument();
  });

  it("shows no install check block when nothing needs review", () => {
    render(<ExtensionDetailPanel {...panelProps()} />);

    expect(screen.queryByText(/Install check/)).not.toBeInTheDocument();
  });

  it("describes Uninstall the same way for every extension", () => {
    render(
      <AuthContext.Provider value={{ session: { roles: ["admin"] } } as never}>
        <ExtensionDetailPanel
          {...panelProps()}
          detailExtension={{ extension_id: "fixture.extension", active: true } as never}
        />
      </AuthContext.Provider>,
    );

    // There are no system extensions: every install uninstalls the same way.
    expect(screen.getByText("Uninstall removes the extension and the lifecycle data it owns.")).toBeInTheDocument();
    expect(screen.queryByText(/system extension/i)).not.toBeInTheDocument();
  });

  it("does not render executable schemes while retaining safe HTTPS links", () => {
    render(<ExtensionDetailPanel {...panelProps()} />);

    expect(
      screen.queryByRole("link", { name: "Support" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Website" })).toHaveAttribute(
      "href",
      "https://vendor.example.test",
    );
  });

  it("exposes manifest pages as keyboard-operable tabs and a labelled panel", async () => {
    const user = userEvent.setup();
    const setActiveExtensionPageId = vi.fn();
    const firstPage = { page_id: "accounts", title: "Accounts", components: [] };
    const secondPage = { page_id: "resources", title: "Resources", components: [] };

    render(
      <ExtensionDetailPanel
        {...panelProps()}
        activeManifestPage={firstPage}
        detailExtension={{
          active: true,
          display_name: "Fixture extension",
          extension_id: "fixture.extension",
        } as never}
        manifestPages={[firstPage, secondPage]}
        setActiveExtensionPageId={setActiveExtensionPageId}
      />,
    );

    const tablist = screen.getByRole("tablist", {
      name: "Fixture extension sections",
    });
    const accountsTab = screen.getByRole("tab", { name: "Accounts" });
    const resourcesTab = screen.getByRole("tab", { name: "Resources" });
    expect(tablist).toContainElement(accountsTab);
    expect(accountsTab).toHaveAttribute("aria-selected", "true");
    expect(resourcesTab).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tabpanel")).toHaveAttribute(
      "aria-labelledby",
      accountsTab.id,
    );

    accountsTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(resourcesTab).toHaveFocus();
    expect(setActiveExtensionPageId).toHaveBeenLastCalledWith("resources");
    await user.keyboard("{Home}");
    expect(accountsTab).toHaveFocus();
    await user.keyboard("{End}");
    expect(resourcesTab).toHaveFocus();
  });

  it("locks OAuth service tabs and safely redirects a manager deep-link to Setup", async () => {
    const user = userEvent.setup();
    const setActiveExtensionPageId = vi.fn();
    const setup = { page_id: "oauth_setup", title: "Setup", components: [] };
    const mail = {
      page_id: "mail",
      title: "Mail",
      components: [{
        component_id: "mail_connection",
        type: "connection_form",
        data: {
          oauth_provider_id: "provider-a",
          connection_type: "mailbox",
          oauth_intent: "connect",
        },
      }],
    };
    const manifest = {
      oauth_providers: [{
        provider_id: "provider-a",
        display_name: "Identity",
        setup_page_id: "oauth_setup",
        credential_fields: [{
          field_id: "client_id",
          value_type: "text",
          required: true,
          secret: false,
          label: "Client ID",
          help_text: "Provider application identifier.",
        }],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
      connection_types: [{
        connection_type_id: "mailbox",
        auth: { type: "oauth2", provider_id: "provider-a" },
      }],
    };

    render(
      <AuthContext.Provider value={{ session: { roles: ["admin"] } } as never}>
        <ExtensionDetailPanel
          {...panelProps()}
          activeManifestPage={mail}
          detailExtension={{ active: true, display_name: "Fixture", extension_id: "fixture.extension" } as never}
          manifestPages={[setup, mail]}
          manifestQuery={{ data: manifest, isError: false, isLoading: false }}
          oauthProviderReadiness={{ "provider-a": "blocked" }}
          setActiveExtensionPageId={setActiveExtensionPageId}
        />
      </AuthContext.Provider>,
    );

    const mailTab = screen.getByRole("tab", { name: "Mail" });
    const setupTab = screen.getByRole("tab", { name: "Setup" });
    expect(mailTab).toBeDisabled();
    expect(mailTab).toBeDisabled();
    expect(setupTab).toHaveAttribute("tabindex", "0");
    setupTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(setupTab).toHaveFocus();
    setActiveExtensionPageId.mockClear();
    await user.click(mailTab);
    expect(setActiveExtensionPageId).not.toHaveBeenCalled();
    expect(screen.getByText("Finish provider setup")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Open provider setup" }));
    expect(setActiveExtensionPageId).toHaveBeenCalledWith("oauth_setup");
    expect(screen.queryByRole("form", { name: "Connect an account" })).not.toBeInTheDocument();
  });

  it("routes a multi-provider locked page to the first provider that still needs setup", async () => {
    const user = userEvent.setup();
    const setActiveExtensionPageId = vi.fn();
    const setupA = { page_id: "setup-a", title: "Setup A", components: [] };
    const setupB = { page_id: "setup-b", title: "Setup B", components: [] };
    const service = {
      page_id: "service",
      title: "Service",
      components: ["provider-a", "provider-b"].map((providerId) => ({
        type: "connection_form",
        data: {
          oauth_provider_id: providerId,
          connection_type: `${providerId}-account`,
          oauth_intent: "connect",
        },
      })),
    };
    const provider = (providerId: string, setupPageId: string) => ({
      provider_id: providerId,
      display_name: providerId,
      setup_page_id: setupPageId,
      credential_fields: [{
        field_id: "client_id",
        value_type: "text",
        required: true,
        secret: false,
        label: "Client ID",
        help_text: "Provider client identifier.",
      }],
      authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
    });

    render(
      <AuthContext.Provider value={{ session: { roles: ["admin"] } } as never}>
        <ExtensionDetailPanel
          {...panelProps()}
          activeManifestPage={service}
          detailExtension={{ active: true, display_name: "Fixture", extension_id: "fixture.extension" } as never}
          manifestPages={[setupA, setupB, service]}
          manifestQuery={{
            data: {
              oauth_providers: [
                provider("provider-a", "setup-a"),
                provider("provider-b", "setup-b"),
              ],
            },
            isError: false,
            isLoading: false,
          }}
          oauthProviderReadiness={{ "provider-a": "ready", "provider-b": "blocked" }}
          setActiveExtensionPageId={setActiveExtensionPageId}
        />
      </AuthContext.Provider>,
    );

    await user.click(screen.getByRole("button", { name: "Open provider setup" }));
    expect(setActiveExtensionPageId).toHaveBeenCalledWith("setup-b");
  });

  it("explains a locked OAuth deep-link to viewers without a mutation CTA", () => {
    const mail = {
      page_id: "mail",
      title: "Mail",
      components: [{
        component_id: "mail_connection",
        type: "connection_form",
        data: { oauth_provider_id: "provider-a", connection_type: "mailbox", oauth_intent: "connect" },
      }],
    };
    const manifest = {
      oauth_providers: [{
        provider_id: "provider-a",
        display_name: "Identity",
        setup_page_id: "oauth_setup",
        credential_fields: [],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
    };

    render(
      <AuthContext.Provider value={{ session: { roles: ["viewer"] } } as never}>
        <ExtensionDetailPanel
          {...panelProps()}
          activeManifestPage={mail}
          detailExtension={{ active: true, display_name: "Fixture", extension_id: "fixture.extension" } as never}
          manifestPages={[mail]}
          manifestQuery={{ data: manifest, isError: false, isLoading: false }}
          oauthProviderReadiness={{ "provider-a": "blocked" }}
        />
      </AuthContext.Provider>,
    );

    expect(screen.getByText("A project manager must finish provider setup before this page is available."))
      .toBeVisible();
    expect(screen.queryByRole("button", { name: "Open provider setup" })).not.toBeInTheDocument();
  });

  it("keeps existing OAuth connections inspectable while provider setup needs repair", async () => {
    vi.mocked(fetchExtensionOAuthConfigurations).mockResolvedValue([{
      configuration_id: "11111111-1111-4111-8111-111111111111",
      display_name: "Existing application",
      status: "ready_for_authorization",
      fields: [],
      revision: 1,
      authorization_revision: 1,
    }]);
    vi.mocked(ensureExtensionOAuthRedirectUri).mockRejectedValue(new Error("unavailable"));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const mail = {
      page_id: "mail",
      title: "Mail",
      components: [{
        component_id: "mail_connection",
        type: "connection_form",
        data: { oauth_provider_id: "provider-a", connection_type: "mailbox", oauth_intent: "connect" },
      }],
    };
    const manifest = {
      oauth_providers: [{
        provider_id: "provider-a",
        display_name: "Identity",
        setup_page_id: "oauth_setup",
        credential_fields: [{
          field_id: "client_id",
          value_type: "text",
          required: true,
          secret: false,
          label: "Client ID",
          help_text: "Provider identifier.",
        }],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
      connection_types: [{
        connection_type_id: "mailbox",
        auth: { type: "oauth2", provider_id: "provider-a" },
      }],
    };

    render(
      <AuthContext.Provider value={{ session: { roles: ["admin"] } } as never}>
        <QueryClientProvider client={queryClient}>
          <ExtensionDetailPanel
            {...panelProps()}
            activeManifestPage={mail}
            detailExtension={{ active: true, display_name: "Fixture", extension_id: "fixture.extension" } as never}
            extensionInstallId="install-a"
            manifestPages={[mail]}
            manifestQuery={{ data: manifest, isError: false, isLoading: false }}
            oauthProviderReadiness={{ "provider-a": "blocked" }}
            pageRequiresProject
            projectConnections={[{
              connection_id: "connection-a",
              display_name: "Existing mailbox",
              extension_install_id: "install-a",
              oauth_provider_id: "provider-a",
              oauth_configuration_id: "11111111-1111-4111-8111-111111111111",
              connection_type: "mailbox",
              connection_config: { oauth: { lifecycle_status: "connected", authorization_intent_id: "connect" } },
            }]}
            selectedProjectId="project-a"
          />
        </QueryClientProvider>
      </AuthContext.Provider>,
    );

    expect(screen.getByRole("tab", { name: "Mail" })).toBeEnabled();
    expect(await screen.findByText("Existing mailbox")).toBeVisible();
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeVisible();
    expect(screen.queryByRole("form", { name: "Connect an account" })).not.toBeInTheDocument();
    expect(screen.getByText(
      "OAuth callback registration must be ready before connecting another account.",
    )).toBeVisible();
  });

  it("mounts provider-neutral OAuth Setup only on the provider-declared page", async () => {
    vi.mocked(fetchExtensionOAuthConfigurations).mockResolvedValue([]);
    vi.mocked(ensureExtensionOAuthRedirectUri).mockResolvedValue({
      redirect_uri: "https://app.example.test/exact-callback",
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const setup = {
      page_id: "oauth_setup",
      title: "Setup",
      project_scoped: true,
      components: [{
        component_id: "setup_copy",
        type: "markdown",
        content: "Provider setup instructions.",
      }],
    };
    const manifest = {
      oauth_providers: [{
        provider_id: "example",
        display_name: "Example Identity",
        setup_page_id: "oauth_setup",
        credential_fields: [{
          field_id: "client_id",
          value_type: "text",
          required: true,
          secret: false,
          label: "Client ID",
          help_text: "Declared help",
        }, {
          field_id: "client_secret",
          value_type: "text",
          required: true,
          secret: true,
          label: "Client secret",
          help_text: "Declared secret help",
        }],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
      connection_types: [{
        connection_type_id: "mailbox",
        auth: { type: "oauth2", provider_id: "example" },
      }],
    };

    render(
      <AuthContext.Provider value={{ session: { roles: ["operator"] } } as never}>
        <QueryClientProvider client={queryClient}>
          <ExtensionDetailPanel
            {...panelProps()}
            activeManifestPage={setup}
            detailExtension={{ active: true, extension_id: "fixture.extension" } as never}
            extensionInstallId="install-a"
            manifestPages={[setup]}
            manifestQuery={{ data: manifest, isError: false, isLoading: false }}
            pageRequiresProject
            selectedProjectId="project-a"
          />
        </QueryClientProvider>
      </AuthContext.Provider>,
    );

    expect(await screen.findByRole("heading", { name: "Example Identity" })).toBeVisible();
    expect(screen.getByText("Provider setup instructions.")).toBeVisible();
    expect(await screen.findByDisplayValue("https://app.example.test/exact-callback")).toBeVisible();
    expect(screen.getByLabelText(/Client ID/)).toBeVisible();
    expect(screen.getByLabelText(/Client secret/)).toBeVisible();
  });

  it("resets a shared provider composer to each page-owned connection declaration", async () => {
    const user = userEvent.setup();
    const startAuthorization = vi.mocked(startExtensionOAuthAuthorization);
    const retryPostConnect = vi.mocked(retryExtensionOAuthPostConnect);
    vi.mocked(fetchExtensionOAuthConfigurations).mockResolvedValue([{
      configuration_id: "11111111-1111-4111-8111-111111111111",
      display_name: "Workspace application",
      status: "ready_for_authorization",
      fields: [],
      revision: 1,
      authorization_revision: 1,
    }]);
    vi.mocked(ensureExtensionOAuthRedirectUri).mockResolvedValue({
      redirect_uri: "https://app.example.test/callback",
    });
    startAuthorization.mockResolvedValue({} as never);
    retryPostConnect.mockResolvedValue({
      outcome: "connected",
      connection_id: "connection-a",
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const mail = {
      page_id: "mail",
      title: "Mail",
      components: [{
        component_id: "mail_connection",
        type: "connection_form",
        data: {
          oauth_provider_id: "fixture_oauth",
          connection_type: "mail_account",
          oauth_intent: "mail_connect",
        },
      }],
    };
    const files = {
      page_id: "files",
      title: "Files",
      components: [{
        component_id: "files_connection",
        type: "connection_form",
        data: {
          oauth_provider_id: "fixture_oauth",
          connection_type: "storage_account",
          oauth_intent: "storage_connect",
        },
      }, {
        component_id: "records_connection",
        type: "connection_form",
        data: {
          oauth_provider_id: "fixture_oauth",
          connection_type: "records_account",
          oauth_intent: "sheet_connect",
        },
      }],
    };
    const incomplete = {
      page_id: "incomplete",
      title: "Incomplete",
      components: [{
        component_id: "incomplete_connection",
        type: "connection_form",
        data: { oauth_provider_id: "fixture_oauth", connection_type: "" },
      }],
    };
    const manifest = {
      oauth_providers: [{
        provider_id: "fixture_oauth",
        display_name: "Workspace Identity",
        setup_page_id: "oauth_setup",
        credential_fields: [{
          field_id: "client_id",
          value_type: "text",
          required: true,
          secret: false,
          label: "Client ID",
          help_text: "Configured on the Setup page.",
        }],
        authorization_intents: [
          { intent_id: "mail_connect", user_inputs: [] },
          { intent_id: "storage_connect", user_inputs: [] },
          { intent_id: "sheet_connect", user_inputs: [] },
        ],
      }],
      connection_types: [
        { connection_type_id: "mail_account", auth: { type: "oauth2", provider_id: "fixture_oauth" } },
        { connection_type_id: "storage_account", auth: { type: "oauth2", provider_id: "fixture_oauth" } },
        { connection_type_id: "records_account", auth: { type: "oauth2", provider_id: "fixture_oauth" } },
      ],
    };
    const connections = [
      {
        connection_id: "mail-connection",
        display_name: "Mail connection",
        extension_install_id: "install-a",
        oauth_provider_id: "fixture_oauth",
        oauth_configuration_id: "11111111-1111-4111-8111-111111111111",
        connection_type: "mail_account",
        connection_config: {
          oauth: {
            lifecycle_status: "needs_attention",
            last_error_code: "provider_unavailable",
          },
        },
      },
      {
        connection_id: "storage-connection",
        display_name: "Storage connection",
        extension_install_id: "install-a",
        oauth_provider_id: "fixture_oauth",
        oauth_configuration_id: "11111111-1111-4111-8111-111111111111",
        connection_type: "storage_account",
        connection_config: {
          oauth: { lifecycle_status: "authorization_required", authorization_intent_id: "storage_connect" },
        },
      },
      {
        connection_id: "sheet-connection",
        display_name: "Sheet connection",
        extension_install_id: "install-a",
        oauth_provider_id: "fixture_oauth",
        oauth_configuration_id: "11111111-1111-4111-8111-111111111111",
        connection_type: "records_account",
        connection_config: {
          oauth: {
            lifecycle_status: "needs_attention",
            last_error_code: "provider_unavailable",
          },
        },
      },
    ];

    function Harness() {
      const [activePage, setActivePage] = useState<Record<string, unknown>>(mail);
      return (
        <ExtensionDetailPanel
          {...panelProps()}
          activeManifestPage={activePage}
          detailExtension={{ active: true, display_name: "Workspace", extension_id: "fixture.extension" } as never}
          extensionInstallId="install-a"
          manifestPages={[mail, files, incomplete]}
          manifestQuery={{ data: manifest, isError: false, isLoading: false }}
          pageRequiresProject
          projectConnections={connections}
          selectedProjectId="project-a"
          setActiveExtensionPageId={(pageId) => {
            setActivePage(
              pageId === files.page_id ? files : pageId === incomplete.page_id ? incomplete : mail,
            );
          }}
        />
      );
    }

    render(
      <AuthContext.Provider value={{ session: { roles: ["operator"] } } as never}>
        <QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>
      </AuthContext.Provider>,
    );

    await screen.findByRole("form", { name: "Connect an account" });
    expect(screen.getByText("Mail connection")).toBeVisible();
    expect(screen.queryByText("Storage connection")).not.toBeInTheDocument();
    expect(screen.queryByText("Sheet connection")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(retryPostConnect).toHaveBeenLastCalledWith(expect.anything(), "mail-connection", {
      configuration_id: "11111111-1111-4111-8111-111111111111",
      connection_type: "mail_account",
    });
    await user.type(screen.getByLabelText(/Connection name/), "Mail account");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(startAuthorization).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({
      connection_type: "mail_account",
      intent_id: "mail_connect",
    }));

    await user.click(screen.getByRole("tab", { name: "Files" }));
    await screen.findByRole("tab", { name: "Files", selected: true });
    const filesComposers = screen.getAllByRole("form", { name: "Connect an account" });
    expect(filesComposers).toHaveLength(2);
    expect(screen.queryByText("Mail connection")).not.toBeInTheDocument();
    expect(screen.getAllByText("Storage connection")).toHaveLength(1);
    expect(screen.getAllByText("Sheet connection")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Disconnect" })).toHaveLength(2);
    expect(within(filesComposers[0]).queryByLabelText("Connection type")).not.toBeInTheDocument();
    expect(within(filesComposers[1]).queryByLabelText("Connection type")).not.toBeInTheDocument();
    await user.type(within(filesComposers[0]).getByLabelText(/Connection name/), "Storage account");
    await user.click(within(filesComposers[0]).getByRole("button", { name: "Connect" }));
    expect(startAuthorization).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({
      connection_type: "storage_account",
      intent_id: "storage_connect",
    }));
    await user.type(within(filesComposers[1]).getByLabelText(/Connection name/), "Sheet account");
    await user.click(within(filesComposers[1]).getByRole("button", { name: "Connect" }));
    expect(startAuthorization).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({
      connection_type: "records_account",
      intent_id: "sheet_connect",
    }));
    await user.click(screen.getByRole("button", { name: "Reconnect Storage connection" }));
    expect(startAuthorization).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({
      connection_type: "storage_account",
      intent_id: "storage_connect",
      existing_connection_id: "storage-connection",
    }));
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(retryPostConnect).toHaveBeenLastCalledWith(expect.anything(), "sheet-connection", {
      configuration_id: "11111111-1111-4111-8111-111111111111",
      connection_type: "records_account",
    });

    await user.click(screen.getByRole("tab", { name: "Mail" }));
    await screen.findByRole("tab", { name: "Mail", selected: true });
    expect(screen.getByText("Mail connection")).toBeVisible();
    expect(screen.queryByText("Storage connection")).not.toBeInTheDocument();
    expect(screen.queryByText("Sheet connection")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Disconnect" })).toHaveLength(1);
    expect(screen.queryByLabelText("Connection type")).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText(/Connection name/));
    await user.type(screen.getByLabelText(/Connection name/), "Mail account again");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(startAuthorization).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({
      connection_type: "mail_account",
      intent_id: "mail_connect",
    }));

    await user.click(screen.getByRole("tab", { name: "Incomplete" }));
    await screen.findByRole("tab", { name: "Incomplete", selected: true });
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
  });

  it("binds an OAuth Test control to its exact owning manifest component", async () => {
    const user = userEvent.setup();
    const handleManifestAction = vi.fn();
    vi.mocked(fetchExtensionOAuthConfigurations).mockResolvedValue([{
      configuration_id: "configuration-a",
      display_name: "Fixture application",
      status: "verified",
      fields: [],
      revision: 1,
      authorization_revision: 1,
    }]);
    const legacy = {
      component_id: "legacy_connection",
      type: "connection_form",
      data: { connection_type: "legacy", test_action: "wrong_action" },
    };
    const oauth = {
      component_id: "exact_oauth_connection",
      type: "connection_form",
      data: {
        oauth_provider_id: "fixture_oauth",
        connection_type: "mail_account",
        oauth_intent: "connect",
        test_action: "test_runtime_connection",
      },
    };
    const page = { page_id: "mail", title: "Mail", components: [legacy, oauth] };
    const manifest = {
      oauth_providers: [{
        provider_id: "fixture_oauth",
        display_name: "Fixture Identity",
        setup_page_id: "setup",
        credential_fields: [{
          field_id: "client_id",
          value_type: "text",
          required: true,
          secret: false,
          label: "Client ID",
          help_text: "Enter the provider client ID.",
        }],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
      connection_types: [{
        connection_type_id: "mail_account",
        auth: { type: "oauth2", provider_id: "fixture_oauth" },
      }],
    };
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <AuthContext.Provider value={{ session: { roles: ["operator"] } } as never}>
        <QueryClientProvider client={queryClient}>
          <ExtensionDetailPanel
            {...panelProps()}
            activeManifestPage={page}
            detailExtension={{ active: true, extension_id: "fixture.extension" } as never}
            extensionInstallId="install-a"
            handleManifestAction={handleManifestAction}
            manifestPages={[page]}
            manifestQuery={{ data: manifest, isError: false, isLoading: false }}
            pageRequiresProject
            projectConnections={[{
              connection_id: "connection-a",
              display_name: "Fixture account",
              extension_install_id: "install-a",
              oauth_provider_id: "fixture_oauth",
              oauth_configuration_id: "configuration-a",
              connection_type: "mail_account",
              connection_config: { oauth: { lifecycle_status: "connected" } },
            }]}
            selectedProjectId="project-a"
          />
        </QueryClientProvider>
      </AuthContext.Provider>,
    );

    await user.click(await screen.findByRole("button", { name: "Test Fixture account" }));
    expect(handleManifestAction).toHaveBeenCalledWith(
      expect.objectContaining({ actionId: "test_runtime_connection" }),
      null,
      oauth,
      {
        target: { connection_id: "connection-a", project_id: "project-a" },
        values: { connection_id: "connection-a" },
      },
    );
  });

  it("renders every OAuth provider that declares the same Setup page", async () => {
    vi.mocked(fetchExtensionOAuthConfigurations).mockResolvedValue([]);
    vi.mocked(ensureExtensionOAuthRedirectUri).mockResolvedValue({
      redirect_uri: "https://app.example.test/exact-callback",
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const setup = { page_id: "oauth_setup", title: "Setup", components: [] };
    const provider = (providerId: string, displayName: string) => ({
      provider_id: providerId,
      display_name: displayName,
      setup_page_id: "oauth_setup",
      credential_fields: [{
        field_id: "client_id",
        value_type: "text",
        required: true,
        secret: false,
        label: `${displayName} client ID`,
        help_text: "Declared help",
      }],
      authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
    });

    render(
      <AuthContext.Provider value={{ session: { roles: ["operator"] } } as never}>
        <QueryClientProvider client={queryClient}>
          <ExtensionDetailPanel
            {...panelProps()}
            activeManifestPage={setup}
            detailExtension={{ active: true, extension_id: "fixture.extension" } as never}
            extensionInstallId="install-a"
            manifestPages={[setup]}
            manifestQuery={{
              data: {
                oauth_providers: [
                  provider("primary", "Primary Identity"),
                  provider("secondary", "Secondary Identity"),
                ],
                connection_types: [],
              },
              isError: false,
              isLoading: false,
            }}
            pageRequiresProject
            selectedProjectId="project-a"
          />
        </QueryClientProvider>
      </AuthContext.Provider>,
    );

    expect(await screen.findByRole("heading", { name: "Primary Identity" })).toBeVisible();
    expect(await screen.findByRole("heading", { name: "Secondary Identity" })).toBeVisible();
  });

  it("switches to declared help and focuses its exact safe anchor", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchExtensionOAuthConfigurations).mockResolvedValue([]);
    vi.mocked(ensureExtensionOAuthRedirectUri).mockResolvedValue({
      redirect_uri: "https://app.example.test/exact-callback",
    });
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    const accounts = {
      page_id: "accounts",
      title: "Accounts",
      components: [{
        component_id: "account_connection",
        type: "connection_form",
        data: {
          oauth_provider_id: "example",
          connection_type: "example_account",
          oauth_intent: "connect",
        },
      }],
    };
    const help = {
      page_id: "oauth_help",
      title: "Help",
      components: [{
        anchor_id: "provider-status",
        component_id: "provider_status_help",
        type: "markdown",
        title: "Provider status help",
        content: "Safe provider-owned troubleshooting.",
      }],
    };
    const manifest = {
      oauth_providers: [{
        provider_id: "example",
        display_name: "Example Identity",
        setup_page_id: "oauth_setup",
        help_page_id: "oauth_help",
        error_help_anchors: { provider_unavailable: "provider-status" },
        credential_fields: [{
          field_id: "client_id",
          value_type: "text",
          required: true,
          secret: false,
          label: "Client ID",
          help_text: "Declared help",
        }],
        authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
      }],
      connection_types: [{
        connection_type_id: "example_account",
        auth: { type: "oauth2", provider_id: "example" },
      }],
    };
    const connections = [{
      connection_id: "connection-a",
      display_name: "Example account",
      extension_install_id: "install-a",
      oauth_provider_id: "example",
      oauth_configuration_id: "configuration-a",
      connection_type: "example_account",
      connection_config: {
        oauth: {
          lifecycle_status: "needs_attention",
          last_error_code: "provider_unavailable",
        },
      },
    }];
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    function Harness() {
      const [activePage, setActivePage] = useState<Record<string, unknown>>(accounts);
      return (
        <ExtensionDetailPanel
          {...panelProps()}
          activeManifestPage={activePage}
          detailExtension={{ active: true, extension_id: "fixture.extension" } as never}
          extensionInstallId="install-a"
          manifestPages={[accounts, help]}
          manifestQuery={{ data: manifest, isError: false, isLoading: false }}
          pageRequiresProject
          projectConnections={connections}
          selectedProjectId="project-a"
          setActiveExtensionPageId={(pageId) => {
            setActivePage(pageId === help.page_id ? help : accounts);
          }}
        />
      );
    }

    render(
      <AuthContext.Provider value={{ session: { roles: ["operator"] } } as never}>
        <QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>
      </AuthContext.Provider>,
    );

    await user.click(await screen.findByRole("button", { name: "Open troubleshooting help" }));
    expect(await screen.findByText("Safe provider-owned troubleshooting.")).toBeVisible();
    const anchor = screen.getByRole("region", { name: "provider-status" });
    expect(anchor).toHaveAttribute("id", "extension-anchor-oauth_help-provider-status");
    expect(anchor).toHaveAttribute("tabindex", "-1");
    await vi.waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(anchor).toHaveFocus();
    });
  });

  it("renders safe projected viewer intents only on their exact owner page without controls", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchExtensionOAuthConfigurations).mockResolvedValue([{
      configuration_id: "11111111-1111-4111-8111-111111111111",
      display_name: "Viewer application",
      status: "ready_for_authorization",
      fields: [],
      revision: 1,
      authorization_revision: 1,
    }]);
    const readPage = {
      page_id: "read_items",
      title: "Read items",
      components: [{ type: "connection_form", data: {
        oauth_provider_id: "generic_identity",
        connection_type: "generic_account",
        oauth_intent: "read_items",
      } }],
    };
    const writePage = {
      page_id: "write_items",
      title: "Write items",
      components: [{ type: "connection_form", data: {
        oauth_provider_id: "generic_identity",
        connection_type: "generic_account",
        oauth_intent: "write_items",
      } }],
    };
    const manifest = {
      oauth_providers: [{
        provider_id: "generic_identity",
        display_name: "Generic Identity",
        setup_page_id: "provider_setup",
        credential_fields: [{
          field_id: "client_id", value_type: "text", required: true, secret: false,
          label: "Client ID", help_text: "Client identifier.",
        }],
        authorization_intents: [
          { intent_id: "read_items", user_inputs: [] },
          { intent_id: "write_items", user_inputs: [] },
        ],
      }],
      connection_types: [{
        connection_type_id: "generic_account",
        auth: { type: "oauth2", provider_id: "generic_identity" },
      }],
    };
    const connections = [
      {
        connection_id: "viewer-read",
        display_name: "Read account",
        extension_install_id: "install-a",
        oauth_provider_id: "generic_identity",
        oauth_configuration_id: "11111111-1111-4111-8111-111111111111",
        connection_type: "generic_account",
        connection_config: { oauth: {
          lifecycle_status: "connected", authorization_intent_id: "read_items",
        } },
      },
      {
        connection_id: "viewer-write",
        display_name: "Write account",
        extension_install_id: "install-a",
        oauth_provider_id: "generic_identity",
        oauth_configuration_id: "11111111-1111-4111-8111-111111111111",
        connection_type: "generic_account",
        connection_config: { oauth: {
          lifecycle_status: "connected", authorization_intent_id: "write_items",
        } },
      },
    ];
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    function Harness() {
      const [activePage, setActivePage] = useState<Record<string, unknown>>(readPage);
      return (
        <ExtensionDetailPanel
          {...panelProps()}
          activeManifestPage={activePage}
          detailExtension={{ active: true, extension_id: "fixture.generic" } as never}
          extensionInstallId="install-a"
          manifestPages={[readPage, writePage]}
          manifestQuery={{ data: manifest, isError: false, isLoading: false }}
          pageRequiresProject
          projectConnections={connections}
          selectedProjectId="project-a"
          setActiveExtensionPageId={(pageId) => setActivePage(
            pageId === writePage.page_id ? writePage : readPage,
          )}
        />
      );
    }

    render(
      <AuthContext.Provider value={{ session: { roles: ["viewer"] } } as never}>
        <QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>
      </AuthContext.Provider>,
    );
    expect(await screen.findByText("Read account")).toBeVisible();
    expect(screen.queryByText("Write account")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reconnect|Disconnect|Test|Retry|Connect/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Write items" }));
    expect(await screen.findByText("Write account")).toBeVisible();
    expect(screen.queryByText("Read account")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reconnect|Disconnect|Test|Retry|Connect/ })).not.toBeInTheDocument();
  });

  it.each([undefined, "retired_read"] as const)("falls back legacy intent %s only for its unique provider/type owner", async (storedIntentId) => {
    vi.mocked(fetchExtensionOAuthConfigurations).mockResolvedValue([{
      configuration_id: "11111111-1111-4111-8111-111111111111",
      display_name: "Generic application",
      status: "ready_for_authorization",
      fields: [],
      revision: 1,
      authorization_revision: 1,
    }]);
    const messagesPage = {
      page_id: "messages",
      title: "Messages",
      components: [{ type: "connection_form", data: {
        oauth_provider_id: "generic_identity",
        connection_type: "message_account",
        oauth_intent: "read_messages",
      } }],
    };
    const documentsPage = {
      page_id: "documents",
      title: "Documents",
      components: [{ type: "connection_form", data: {
        oauth_provider_id: "generic_identity",
        connection_type: "document_account",
        oauth_intent: "write_documents",
      } }],
    };
    const manifest = {
      oauth_providers: [{
        provider_id: "generic_identity",
        display_name: "Generic Identity",
        setup_page_id: "provider_setup",
        credential_fields: [{
          field_id: "client_id", value_type: "text", required: true, secret: false,
          label: "Client ID", help_text: "Client identifier.",
        }],
        authorization_intents: [
          { intent_id: "read_messages", user_inputs: [] },
          { intent_id: "write_documents", user_inputs: [] },
        ],
      }],
      connection_types: [
        { connection_type_id: "message_account", auth: { type: "oauth2", provider_id: "generic_identity" } },
        { connection_type_id: "document_account", auth: { type: "oauth2", provider_id: "generic_identity" } },
      ],
    };
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <AuthContext.Provider value={{ session: { roles: ["operator"] } } as never}>
        <QueryClientProvider client={queryClient}>
          <ExtensionDetailPanel
            {...panelProps()}
            activeManifestPage={messagesPage}
            detailExtension={{ active: true, extension_id: "fixture.generic" } as never}
            extensionInstallId="install-a"
            manifestPages={[messagesPage, documentsPage]}
            manifestQuery={{ data: manifest, isError: false, isLoading: false }}
            pageRequiresProject
            projectConnections={[{
              connection_id: "legacy-message",
              display_name: "Legacy message account",
              extension_install_id: "install-a",
              oauth_provider_id: "generic_identity",
              oauth_configuration_id: "11111111-1111-4111-8111-111111111111",
              connection_type: "message_account",
              connection_config: { oauth: {
                lifecycle_status: "authorization_required",
                ...(storedIntentId ? { authorization_intent_id: storedIntentId } : {}),
              } },
            }]}
            selectedProjectId="project-a"
          />
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
    expect(await screen.findByText("Legacy message account")).toBeVisible();
  });

  describe("an OAuth provider's setup page", () => {
    const readyConfiguration = {
      configuration_id: "44444444-4444-4444-8444-444444444444",
      display_name: "Files app",
      status: "ready_for_authorization" as const,
      fields: [{ field_id: "client_id", value: "app-key" }, { field_id: "client_secret", secret_present: true }],
      revision: 1,
      authorization_revision: 1,
    };
    const credentialFields = [{
      field_id: "client_id", value_type: "text", required: true, secret: false,
      label: "App key", help_text: "From the app console.",
    }, {
      field_id: "client_secret", value_type: "text", required: true, secret: true,
      label: "App secret", help_text: "From the app console.",
    }];

    function renderPanel(pages: Array<Record<string, unknown>>, manifest: Record<string, unknown>) {
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
      function Harness() {
        const [activePage, setActivePage] = useState<Record<string, unknown>>(pages[0]);
        return (
          <ExtensionDetailPanel
            {...panelProps()}
            activeManifestPage={activePage}
            detailExtension={{ active: true, display_name: "Files", extension_id: "fixture.files" } as never}
            extensionInstallId="install-a"
            manifestPages={pages}
            manifestQuery={{ data: manifest, isError: false, isLoading: false }}
            pageRequiresProject
            selectedProjectId="project-a"
            setActiveExtensionPageId={(pageId) => setActivePage(pages.find((page) => page.page_id === pageId) || pages[0])}
          />
        );
      }
      render(
        <AuthContext.Provider value={{ session: { roles: ["operator"] } } as never}>
          <QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>
        </AuthContext.Provider>,
      );
    }

    it("saves the app and connects an account on the same page when it declares the provider's connection form", async () => {
      const user = userEvent.setup();
      vi.mocked(ensureExtensionOAuthRedirectUri).mockReset().mockResolvedValue({ redirect_uri: "https://app.example.test/callback" });
      vi.mocked(fetchExtensionOAuthConfigurations).mockReset()
        .mockResolvedValueOnce([])
        .mockResolvedValue([readyConfiguration]);
      vi.mocked(previewExtensionOAuthConfiguration).mockReset().mockResolvedValue({ requires_confirmation: false, change_policies: [] } as never);
      vi.mocked(saveExtensionOAuthConfiguration).mockReset().mockResolvedValue(readyConfiguration as never);
      const startAuthorization = vi.mocked(startExtensionOAuthAuthorization).mockReset()
        .mockResolvedValue({ authorization_url: "https://provider.example.test/authorize" } as never);
      const filesPage = {
        page_id: "files",
        title: "Files",
        project_scoped: true,
        components: [{
          component_id: "files_guide",
          type: "markdown",
          content: "Create the app, then connect an account.",
        }, {
          component_id: "files_accounts",
          type: "connection_form",
          title: "Accounts",
          data: { oauth_provider_id: "files_oauth", connection_type: "files_account", oauth_intent: "files_connect" },
        }],
      };
      renderPanel([filesPage], {
        oauth_providers: [{
          provider_id: "files_oauth",
          display_name: "Files Identity",
          setup_page_id: "files",
          help_page_id: "files",
          credential_fields: credentialFields,
          authorization_intents: [{ intent_id: "files_connect", user_inputs: [] }],
        }],
        connection_types: [{ connection_type_id: "files_account", auth: { type: "oauth2", provider_id: "files_oauth" } }],
      });

      expect(await screen.findByLabelText(/App key/)).toBeVisible();
      expect(screen.getByText("Create the app, then connect an account.")).toBeVisible();
      expect(await screen.findByText("No OAuth configuration is ready to connect.")).toBeVisible();
      expect(screen.queryByRole("button", { name: "Open provider setup" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Open provider help" })).not.toBeInTheDocument();
      expect(screen.queryByRole("form", { name: "Connect an account" })).not.toBeInTheDocument();

      await user.type(screen.getByLabelText(/Configuration name/), "Files app");
      await user.type(screen.getByLabelText(/App key/), "app-key");
      await user.type(screen.getByLabelText(/App secret/), "app-secret");
      await user.click(screen.getByRole("button", { name: "Save configuration" }));

      const composer = await screen.findByRole("form", { name: "Connect an account" });
      expect(screen.getAllByRole("region", { name: "Files Identity OAuth setup" })).toHaveLength(1);
      expect(screen.getByRole("combobox", { name: "OAuth configuration" })).toHaveTextContent("Files app");
      expect(screen.queryByText("No OAuth configuration is ready to connect.")).not.toBeInTheDocument();
      await user.type(within(composer).getByLabelText(/Connection name/), "Team files");
      await user.click(within(composer).getByRole("button", { name: "Connect" }));
      expect(startAuthorization).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({
        configuration_id: readyConfiguration.configuration_id,
        connection_type: "files_account",
        intent_id: "files_connect",
        return_page_id: "files",
      }));
    });

    it("keeps a setup page without a connection form apart from its service pages", async () => {
      const user = userEvent.setup();
      vi.mocked(ensureExtensionOAuthRedirectUri).mockReset().mockResolvedValue({ redirect_uri: "https://app.example.test/callback" });
      vi.mocked(fetchExtensionOAuthConfigurations).mockReset().mockResolvedValue([readyConfiguration]);
      const setupPage = {
        page_id: "setup",
        title: "Setup",
        project_scoped: true,
        components: [{ component_id: "setup_guide", type: "markdown", content: "One app for every service." }],
      };
      const servicePage = (pageId: string, title: string, intentId: string) => ({
        page_id: pageId,
        title,
        project_scoped: true,
        components: [{
          component_id: `${pageId}_accounts`,
          type: "connection_form",
          data: { oauth_provider_id: "suite_oauth", connection_type: `${pageId}_account`, oauth_intent: intentId },
        }],
      });
      renderPanel([setupPage, servicePage("drive", "Drive", "drive_connect"), servicePage("mail", "Mail", "mail_connect")], {
        oauth_providers: [{
          provider_id: "suite_oauth",
          display_name: "Suite Identity",
          setup_page_id: "setup",
          help_page_id: "setup",
          credential_fields: credentialFields,
          authorization_intents: [
            { intent_id: "drive_connect", user_inputs: [] },
            { intent_id: "mail_connect", user_inputs: [] },
          ],
        }],
        connection_types: [
          { connection_type_id: "drive_account", auth: { type: "oauth2", provider_id: "suite_oauth" } },
          { connection_type_id: "mail_account", auth: { type: "oauth2", provider_id: "suite_oauth" } },
        ],
      });

      expect(await screen.findByLabelText(/App key/)).toBeVisible();
      expect(screen.getByText("One app for every service.")).toBeVisible();
      expect(screen.queryByRole("form", { name: "Connect an account" })).not.toBeInTheDocument();

      await user.click(screen.getByRole("tab", { name: "Drive" }));
      await screen.findByRole("form", { name: "Connect an account" });
      expect(screen.queryByLabelText(/App key/)).not.toBeInTheDocument();
      expect(screen.queryByRole("combobox", { name: "OAuth configuration" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Open provider help" })).toBeVisible();
    });
  });
});
