import type { ComponentProps } from "react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AuthContext } from "@/lib/auth/context";
import { HttpError } from "@/lib/http/errors";
import { ExtensionDetailPanel } from "@/pages/extensions/extension-detail-panel";
import { oauthProvidersFromManifest } from "@/pages/extensions/extension-oauth-types";
import pinnedManifests from "@/pages/extensions/oauth-import-manifest.fixture.json";
import {
  ensureExtensionOAuthRedirectUri,
  fetchExtensionOAuthConfigurations,
  importExtensionOAuthRefreshToken,
  startExtensionOAuthAuthorization,
} from "@/pages/extensions/extension-oauth-api";
import { ExtensionOAuthSetup } from "@/pages/extensions/extension-oauth-setup";

vi.mock("@/pages/extensions/extension-oauth-api", () => ({
  deleteExtensionOAuthConfiguration: vi.fn(),
  disconnectExtensionOAuthConnection: vi.fn(),
  ensureExtensionOAuthRedirectUri: vi.fn(),
  fetchExtensionOAuthConfigurations: vi.fn(),
  importExtensionOAuthRefreshToken: vi.fn(),
  previewExtensionOAuthConfiguration: vi.fn(),
  previewExtensionOAuthConfigurationDelete: vi.fn(),
  reconcileExtensionOAuthConfigurationRelease: vi.fn(),
  retryExtensionOAuthPostConnect: vi.fn(),
  saveExtensionOAuthConfiguration: vi.fn(),
  startExtensionOAuthAuthorization: vi.fn(),
}));

const mockedConfigurations = vi.mocked(fetchExtensionOAuthConfigurations);
const mockedImport = vi.mocked(importExtensionOAuthRefreshToken);
const mockedRedirect = vi.mocked(ensureExtensionOAuthRedirectUri);
const mockedStart = vi.mocked(startExtensionOAuthAuthorization);

const PASTED = "pasted-refresh-token-canary";

const provider = {
  provider_id: "console",
  display_name: "Console Identity",
  description: "Connect with a refresh token issued in the provider console.",
  setup_page_id: "oauth_setup",
  help_page_id: "oauth_help",
  flow: { type: "refresh_token_import" },
  credential_fields: [
    {
      field_id: "client_id",
      value_type: "text",
      required: true,
      secret: false,
      label: "Client ID",
      help_text: "Copy the client ID.",
      validation: { min_length: 1, max_length: 100 },
    },
  ],
  authorization_intents: [],
};

const connectionTypes = [
  {
    connection_type_id: "console_account",
    display_name: "Console account",
    auth: { type: "oauth2", provider_id: "console", scopes: { required: [], optional: [] } },
    config_schema: {
      type: "object",
      additionalProperties: false,
      required: ["account_ref"],
      properties: {
        account_ref: { type: "string", title: "Account reference", minLength: 1 },
        region_hint: { type: "string", title: "Region hint" },
      },
    },
  },
];

const configuration = {
  configuration_id: "33333333-3333-4333-8333-333333333333",
  display_name: "Console application",
  status: "verified" as const,
  fields: [{ field_id: "client_id", value: "client" }],
  revision: 2,
  authorization_revision: 1,
};

function renderImport({
  connections = [] as Record<string, unknown>[],
  onConnectionsChanged = vi.fn(),
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <ExtensionOAuthSetup
        allowedConnectionTypeIds={["console_account"]}
        allowedIntentIds={[]}
        canManage
        connectionTypes={connectionTypes}
        connections={connections}
        installId="install-a"
        mode="connections"
        onConnectionsChanged={onConnectionsChanged}
        onNavigatePage={vi.fn()}
        projectId="project-a"
        provider={provider}
        providerSetupReady
        returnPageId="accounts"
      />
    </QueryClientProvider>,
  );
  return { onConnectionsChanged };
}

function refreshTokenInput(container: HTMLElement = document.body): HTMLInputElement {
  return within(container).getAllByLabelText("Refresh token")[0] as HTMLInputElement;
}

describe("refresh-token import connections", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedConfigurations.mockResolvedValue([configuration]);
    mockedImport.mockResolvedValue({
      outcome: "connected",
      connection_id: "connection-new",
      connection_type: "console_account",
    });
  });

  it("connects from a pasted token with the connection type's settings and no redirect", async () => {
    const user = userEvent.setup();
    const { onConnectionsChanged } = renderImport();

    await waitFor(() => expect(screen.getByRole("heading", { name: "Connect an account" })).toBeVisible());
    expect(screen.queryByLabelText("OAuth Redirect URI")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Authorization intent")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText(/Connection name/u), "Primary account");
    await user.type(refreshTokenInput(), `  ${PASTED} `);
    await user.type(screen.getByLabelText(/Account reference/u), "acct-1");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(mockedImport).toHaveBeenCalledTimes(1));
    expect(mockedImport).toHaveBeenCalledWith(
      { projectId: "project-a", installId: "install-a", providerId: "console" },
      {
        configuration_id: configuration.configuration_id,
        connection_type: "console_account",
        display_name: "Primary account",
        refresh_token: PASTED,
        connection_config: { account_ref: "acct-1" },
      },
    );
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Account connected."));
    expect(refreshTokenInput().value).toBe("");
    expect(onConnectionsChanged).toHaveBeenCalled();
    expect(mockedRedirect).not.toHaveBeenCalled();
    expect(mockedStart).not.toHaveBeenCalled();
  });

  it("requires the token and the declared settings before sending anything", async () => {
    const user = userEvent.setup();
    renderImport();

    await screen.findByRole("heading", { name: "Connect an account" });
    await user.type(screen.getByLabelText(/Connection name/u), "Primary account");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(screen.getByText("Refresh token is required.")).toBeVisible());
    expect(screen.getByText("Account reference is required.")).toBeVisible();
    expect(mockedImport).not.toHaveBeenCalled();
  });

  it("shows a provider refusal on the token field without echoing the token", async () => {
    const user = userEvent.setup();
    mockedImport.mockRejectedValue(
      new HttpError({
        payload: { error: "oauth_refresh_token_rejected", field: "refresh_token" },
        status: 400,
        url: "/api/test",
      }),
    );
    renderImport();

    await screen.findByRole("heading", { name: "Connect an account" });
    await user.type(screen.getByLabelText(/Connection name/u), "Primary account");
    await user.type(refreshTokenInput(), PASTED);
    await user.type(screen.getByLabelText(/Account reference/u), "acct-1");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(screen.getByText("The provider did not accept this refresh token. Check it and try again.")).toBeVisible());
    expect(document.body).not.toHaveTextContent(new RegExp(PASTED));
    // A refused token is of no further use, so it leaves the page.
    expect(refreshTokenInput().value).toBe("");
  });

  it("keeps the token when the refusal concerns the client credentials, not the token", async () => {
    const user = userEvent.setup();
    mockedImport.mockRejectedValue(
      new HttpError({
        payload: { error: "oauth_invalid_client", help_anchor: "app-credentials" },
        status: 412,
        url: "/api/test",
      }),
    );
    renderImport();

    await screen.findByRole("heading", { name: "Connect an account" });
    await user.type(screen.getByLabelText(/Connection name/u), "Primary account");
    await user.type(refreshTokenInput(), PASTED);
    await user.type(screen.getByLabelText(/Account reference/u), "acct-1");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(mockedImport).toHaveBeenCalledTimes(1));
    // A console may show a token only once; fixing the configuration must not
    // cost the operator a new one.
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect" })).toBeEnabled());
    expect(refreshTokenInput().value).toBe(PASTED);
  });

  it("replaces the token of an existing connection with its revision and stored settings", async () => {
    const user = userEvent.setup();
    renderImport({
      connections: [
        {
          connection_id: "connection-existing",
          display_name: "Existing account",
          extension_install_id: "install-a",
          oauth_provider_id: "console",
          oauth_configuration_id: configuration.configuration_id,
          connection_type: "console_account",
          credential_revision: 6,
          connection_config: {
            account_ref: "acct-old",
            oauth: { lifecycle_status: "authorization_required" },
          },
        },
      ],
    });

    await user.click(
      await screen.findByRole("button", { name: "Replace refresh token for Existing account" }),
    );
    const form = screen.getByRole("form", { name: "Replace refresh token for Existing account" });
    expect(within(form).getByLabelText(/Account reference/u)).toHaveValue("acct-old");
    await user.type(refreshTokenInput(form), PASTED);
    await user.click(within(form).getByRole("button", { name: "Replace refresh token" }));

    await waitFor(() => expect(mockedImport).toHaveBeenCalledTimes(1));
    expect(mockedImport.mock.calls[0][1]).toEqual({
      configuration_id: configuration.configuration_id,
      connection_type: "console_account",
      display_name: "Existing account",
      refresh_token: PASTED,
      connection_config: { account_ref: "acct-old" },
      existing_connection_id: "connection-existing",
      connection_credential_revision: 6,
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("form", { name: "Replace refresh token for Existing account" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("refuses to replace a token when the row carries no valid credential revision", async () => {
    const user = userEvent.setup();
    renderImport({
      connections: [
        {
          connection_id: "connection-existing",
          display_name: "Existing account",
          extension_install_id: "install-a",
          oauth_provider_id: "console",
          oauth_configuration_id: configuration.configuration_id,
          connection_type: "console_account",
          connection_config: { account_ref: "acct-old", oauth: { lifecycle_status: "authorization_required" } },
        },
      ],
    });

    await user.click(
      await screen.findByRole("button", { name: "Replace refresh token for Existing account" }),
    );
    const form = screen.getByRole("form", { name: "Replace refresh token for Existing account" });
    await user.type(refreshTokenInput(form), PASTED);

    expect(within(form).getByRole("button", { name: "Replace refresh token" })).toBeDisabled();
    expect(within(form).getByRole("alert")).toHaveTextContent("current version is unknown");
    expect(mockedImport).not.toHaveBeenCalled();
  });

  it("sends only the declared settings, so a key it cannot render is never cleared", async () => {
    const user = userEvent.setup();
    renderImport({
      connections: [
        {
          connection_id: "connection-existing",
          display_name: "Existing account",
          extension_install_id: "install-a",
          oauth_provider_id: "console",
          oauth_configuration_id: configuration.configuration_id,
          connection_type: "console_account",
          credential_revision: 3,
          connection_config: {
            account_ref: "acct-old",
            legacy_key: "kept-by-the-server",
            oauth: { lifecycle_status: "needs_attention", last_error_code: "subject_changed" },
          },
        },
      ],
    });

    await user.click(
      await screen.findByRole("button", { name: "Replace refresh token for Existing account" }),
    );
    const form = screen.getByRole("form", { name: "Replace refresh token for Existing account" });
    await user.type(refreshTokenInput(form), PASTED);
    await user.click(within(form).getByRole("button", { name: "Replace refresh token" }));

    await waitFor(() => expect(mockedImport).toHaveBeenCalledTimes(1));
    expect(mockedImport.mock.calls[0][1].connection_config).toEqual({ account_ref: "acct-old" });
    expect(mockedImport.mock.calls[0][1].connection_credential_revision).toBe(3);
  });
});

describe("the Connection page renders the manifest shapes Core actually emits", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedConfigurations.mockResolvedValue([configuration]);
  });

  it("normalizes a compiled import provider that names no intents", () => {
    const compiled = pinnedManifests.compiled.oauth_providers[0] as Record<string, unknown>;
    expect(compiled).not.toHaveProperty("authorization_intents");

    const [provider] = oauthProvidersFromManifest(pinnedManifests.compiled);

    expect(provider.authorization_intents).toEqual([]);
    expect(provider.error_help_anchors).toEqual({});
    expect(provider.flow).toEqual({ type: "refresh_token_import" });
  });

  it.each([
    ["admin", "compiled"],
    ["operator", "projected"],
  ] as const)("renders the paste form for an %s from the %s manifest", async (role, shape) => {
    const manifest = pinnedManifests[shape];
    const pages = manifest.ui_manifest.pages as Record<string, unknown>[];
    const accounts = pages.find((page) => page.page_id === "accounts");
    const noop = vi.fn();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={{ session: { roles: [role] } } as never}>
          <ExtensionDetailPanel
            {...({
              activationBlockedReason: "",
              connectionCreationBlockedReason: "",
              busyById: {},
              callbackStatuses: {},
              callbacksQuery: {},
              componentErrors: {},
              componentQueryData: {},
              componentQueryParams: {},
              detailStatus: "",
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
              projectConnectionsQuery: { isError: false, isPending: false, refetch: async () => undefined },
              projectOptions: [],
              projectState: [],
              projectsQuery: { error: null, isError: false },
              rememberedProjectId: "",
              setActiveExtensionPageId: noop,
              setComponentQueryParams: noop,
              setFormValuesByComponent: noop,
              onFieldCorrected: noop,
            } as unknown as ComponentProps<typeof ExtensionDetailPanel>)}
            activeManifestPage={accounts}
            detailExtension={{ active: true, display_name: "Fixture", extension_id: manifest.extension_id } as never}
            extensionInstallId="install-a"
            manifestPages={pages}
            manifestQuery={{ data: manifest, isError: false, isLoading: false }}
            oauthConnectionTypes={["console_account"]}
            oauthProviderReadiness={{ console: "ready" }}
            selectedProjectId="project-a"
          />
        </AuthContext.Provider>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByRole("heading", { name: "Connect an account" })).toBeVisible());
    expect(screen.getAllByLabelText("Refresh token")[0]).toBeInTheDocument();
    expect(screen.getByLabelText(/Account reference/u)).toBeInTheDocument();
    expect(mockedRedirect).not.toHaveBeenCalled();
  });
});
