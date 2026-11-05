import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { HttpError } from "@/lib/http/errors";
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

    expect(await screen.findByRole("heading", { name: "Connect an account" })).toBeVisible();
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
    expect(await screen.findByRole("status")).toHaveTextContent("Account connected.");
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

    expect(await screen.findByText("Refresh token is required.")).toBeVisible();
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

    expect(
      await screen.findByText("The provider did not accept this refresh token. Check it and try again."),
    ).toBeVisible();
    expect(document.body).not.toHaveTextContent(new RegExp(PASTED));
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
});
