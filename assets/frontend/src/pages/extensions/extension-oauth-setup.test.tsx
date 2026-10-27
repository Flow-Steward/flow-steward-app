import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";

import { DsDialog } from "@/components/shared/design-system-overlays";
import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import { beginRequest } from "@/lib/http/request-activity";
import { HttpError } from "@/lib/http/errors";
import {
  deleteExtensionOAuthConfiguration,
  disconnectExtensionOAuthConnection,
  ensureExtensionOAuthRedirectUri,
  fetchExtensionOAuthConfigurations,
  previewExtensionOAuthConfiguration,
  previewExtensionOAuthConfigurationDelete,
  saveExtensionOAuthConfiguration,
  startExtensionOAuthAuthorization,
  retryExtensionOAuthPostConnect,
  reconcileExtensionOAuthConfigurationRelease,
  type OAuthPostConnectRetryResult,
} from "@/pages/extensions/extension-oauth-api";
import { ExtensionOAuthSetup } from "@/pages/extensions/extension-oauth-setup";
import { OAuthConfigurationForm } from "@/pages/extensions/extension-oauth-configuration-form";

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
  reconcileExtensionOAuthConfigurationRelease: vi.fn(),
}));

const mockedConfigurations = vi.mocked(fetchExtensionOAuthConfigurations);
const mockedRedirect = vi.mocked(ensureExtensionOAuthRedirectUri);
const mockedPreview = vi.mocked(previewExtensionOAuthConfiguration);
const mockedSave = vi.mocked(saveExtensionOAuthConfiguration);
const mockedStart = vi.mocked(startExtensionOAuthAuthorization);
const mockedDeletePreview = vi.mocked(previewExtensionOAuthConfigurationDelete);
const mockedDelete = vi.mocked(deleteExtensionOAuthConfiguration);
const mockedDisconnect = vi.mocked(disconnectExtensionOAuthConnection);
const mockedRetry = vi.mocked(retryExtensionOAuthPostConnect);
const mockedReconcile = vi.mocked(reconcileExtensionOAuthConfigurationRelease);
const onAuthorization = vi.fn();
const provider = {
  provider_id: "example",
  display_name: "Example Identity",
  description: "Connect an Example account.",
  setup_page_id: "oauth_setup",
  help_page_id: "oauth_help",
  credential_fields: [
    {
      field_id: "client_id",
      value_type: "text",
      required: true,
      secret: false,
      label: "Application ID",
      help_text: "Copy the identifier from the provider console.",
      placeholder: "provider-client-id",
      autocomplete: "username",
      validation: { min_length: 2, max_length: 40 },
    },
    {
      field_id: "client_secret",
      value_type: "secret",
      required: true,
      secret: true,
      label: "Application secret",
      help_text: "Stored encrypted and never displayed again.",
      autocomplete: "current-password",
      validation: { min_length: 4, max_length: 80 },
    },
  ],
  authorization_intents: [
    {
      intent_id: "connect",
      user_inputs: [
        { input_id: "email_hint", validation: { type: "email", max_length: 320 } },
      ],
    },
  ],
} as const;

const originProvider = {
  ...provider,
  credential_fields: [
    ...provider.credential_fields,
    {
      field_id: "browser_origin",
      value_type: "text",
      required: false,
      secret: false,
      label: "Browser origin",
      help_text: "Optional canonical browser origin.",
      autocomplete: "off",
      validation: { type: "https_origin", min_length: 9, max_length: 2048 },
    },
  ],
} as const;

const connectionTypes = [
  {
    connection_type_id: "example_mail",
    display_name: "Example mailbox",
    auth: { type: "oauth2", provider_id: "example", scopes: { required: ["mail.read"] } },
  },
];

const primary = {
  configuration_id: "11111111-1111-4111-8111-111111111111",
  display_name: "Primary application",
  status: "ready_for_authorization" as const,
  fields: [
    { field_id: "client_id", value: "saved-client" },
    { field_id: "client_secret", secret_present: true },
  ],
  revision: 3,
  authorization_revision: 2,
};

function nativeLoaderFocusBoundary() {
  const focus = HTMLElement.prototype.focus;
  // jsdom's showModal stub does not make the page inert; browsers ignore these focus attempts.
  return vi.spyOn(HTMLElement.prototype, "focus").mockImplementation(function (this: HTMLElement, options?: FocusOptions) {
    const loader = screen.queryByRole("dialog", { name: "Loading requests" });
    if (!loader || loader.contains(this)) focus.call(this, options);
  });
}

function renderSetup({
  canManage = true,
  connections = [] as Record<string, unknown>[],
  connectionTypeDeclarations = connectionTypes,
  mode = "setup" as "setup" | "connections",
  allowedConnectionTypeIds = undefined as readonly string[] | undefined,
  allowedIntentIds = undefined as readonly string[] | undefined,
  connectionOwners = undefined as readonly { intentId: string; pageId: string }[] | undefined,
  onConnectionsChanged = undefined as (() => void | Promise<void>) | undefined,
  onNavigatePage = vi.fn(),
  onTestConnection = undefined as ((connection: Record<string, unknown>) => void) | undefined,
  providerDeclaration = provider as typeof provider | Record<string, unknown>,
  providerSetupReady = true,
  returnPageId = "oauth_setup",
  focusConnectionId = undefined as string | undefined,
  strictMode = false,
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const setup = (
    <QueryClientProvider client={queryClient}>
      <ExtensionOAuthSetup
        canManage={canManage}
        allowedConnectionTypeIds={allowedConnectionTypeIds || (mode === "connections" ? ["example_mail"] : undefined)}
        allowedIntentIds={allowedIntentIds || (mode === "connections" ? ["connect"] : undefined)}
        connectionTypes={connectionTypeDeclarations}
        connectionOwners={connectionOwners || (mode === "connections" ? [{
          intentId: allowedIntentIds?.[0] || "connect",
          pageId: returnPageId,
        }] : [])}
        connections={connections}
        installId="install-a"
        mode={mode}
        onAuthorization={onAuthorization}
        onConnectionsChanged={onConnectionsChanged}
        onNavigatePage={onNavigatePage}
        onTestConnection={onTestConnection}
        projectId="project-a"
        provider={providerDeclaration as never}
        providerSetupReady={providerSetupReady}
        returnPageId={returnPageId}
        focusConnectionId={focusConnectionId}
      />
    </QueryClientProvider>
  );
  return render(strictMode ? <StrictMode>{setup}</StrictMode> : setup);
}

describe("ExtensionOAuthSetup", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedConfigurations.mockResolvedValue([primary]);
    mockedRedirect.mockResolvedValue({ redirect_uri: "https://app.example.test/callback/exact" });
    mockedPreview.mockResolvedValue({
      configuration_id: primary.configuration_id,
      expected_revision: 3,
      changed_fields: [],
      change_policies: [],
      connections: [],
      requires_confirmation: false,
    });
    mockedSave.mockResolvedValue(primary);
    mockedStart.mockResolvedValue({
      authorization_url: "https://identity.example.test/authorize?opaque=1",
      redirect_uri: "https://app.example.test/callback/exact",
      expires_at: "2030-01-01T00:00:00Z",
    });
    mockedDeletePreview.mockResolvedValue({
      configuration_id: primary.configuration_id,
      expected_revision: 3,
      changed_fields: ["__delete__"],
      change_policies: [],
      connections: [],
      requires_confirmation: false,
    });
    mockedDelete.mockResolvedValue({ deleted: true });
    mockedDisconnect.mockResolvedValue({});
    mockedRetry.mockResolvedValue({ outcome: "connected", connection_id: "connection-a" });
  });

  it("renders only account authorization controls on an extension-declared connection page", async () => {
    renderSetup({ mode: "connections" });

    expect(await screen.findByRole("heading", { name: "Example Identity" })).toBeVisible();
    expect(await screen.findByRole("heading", { name: "Connect an account" })).toBeVisible();
    expect(screen.queryByRole("form", { name: "OAuth configuration" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("OAuth Redirect URI")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Configuration for new connection" }))
      .not.toBeInTheDocument();
    expect(screen.queryByLabelText("Connection type")).not.toBeInTheDocument();
  });

  it("shows a configuration selector only when multiple ready configurations exist", async () => {
    const secondary = {
      ...primary,
      configuration_id: "22222222-2222-4222-8222-222222222222",
      display_name: "Secondary application",
    };
    mockedConfigurations.mockResolvedValue([primary, secondary]);

    renderSetup({ mode: "connections" });

    const selector = await screen.findByRole("combobox", {
      name: "Configuration for new connection",
    });
    expect(selector).toHaveTextContent(primary.display_name);
    await userEvent.setup().click(selector);
    expect(within(await screen.findByRole("listbox")).getAllByRole("option")).toHaveLength(2);
  });

  it("fails closed when a page ambiguously declares multiple allowed types", async () => {
    renderSetup({
      allowedConnectionTypeIds: ["example_mail", "example_files"],
      allowedIntentIds: ["connect"],
      connectionTypeDeclarations: [
        ...connectionTypes,
        {
          connection_type_id: "example_files",
          display_name: "Example files",
          auth: { type: "oauth2", provider_id: "example", scopes: { required: ["files.read"] } },
        },
      ],
      mode: "connections",
    });

    await screen.findByRole("heading", { name: "Example Identity" });
    expect(screen.queryByRole("form", { name: "Connect an account" })).not.toBeInTheDocument();
  });

  it("shows a provider setup prerequisite instead of an empty connection composer", async () => {
    const user = userEvent.setup();
    const onNavigatePage = vi.fn();
    mockedConfigurations.mockResolvedValue([]);

    renderSetup({ mode: "connections", onNavigatePage });

    expect(await screen.findByRole("heading", { name: "Provider setup required" }))
      .toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Configuration for new connection" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Connect an account" }))
      .not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open provider setup" }));
    expect(onNavigatePage).toHaveBeenCalledWith("oauth_setup");
  });

  it("keeps fresh connection controls hidden until callback registration is ready", async () => {
    renderSetup({ mode: "connections", providerSetupReady: false });

    expect(await screen.findByText(
      "OAuth callback registration must be ready before connecting another account.",
    )).toBeVisible();
    expect(screen.queryByRole("form", { name: "Connect an account" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open provider setup" })).toBeVisible();
  });

  it.each(["incomplete", "needs_attention"] as const)(
    "routes a project manager to setup when only %s configurations exist",
    async (status) => {
      const user = userEvent.setup();
      const onNavigatePage = vi.fn();
      mockedConfigurations.mockResolvedValue([{ ...primary, status }]);

      renderSetup({ mode: "connections", onNavigatePage });

      expect(await screen.findByText("No OAuth configuration is ready to connect."))
        .toBeVisible();
      expect(screen.queryByRole("combobox", { name: "Configuration for new connection" }))
        .not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Connect" }))
        .not.toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Open provider setup" }));
      expect(onNavigatePage).toHaveBeenCalledWith("oauth_setup");
    },
  );

  it("offers a safe retry when redirect URI registration fails", async () => {
    const user = userEvent.setup();
    mockedRedirect
      .mockRejectedValueOnce(new HttpError({
        payload: {
          error: "oauth_public_base_url_unavailable",
          reason: "missing",
          detail: "private-token-must-not-render",
        },
        status: 503,
        url: "/api/test",
      }))
      .mockResolvedValueOnce({
        redirect_uri: "https://localhost:8099/webhook/extensions/example/oauth/stable",
      });

    renderSetup();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Public host");
    expect(alert).toHaveTextContent("Public port");
    expect(alert).toHaveTextContent("./flow-steward config set --public-host");
    expect(alert).toHaveTextContent("./flow-steward config apply");
    expect(screen.queryByText(/private-token-must-not-render/u)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry redirect registration" }));
    expect(await screen.findByLabelText("OAuth Redirect URI")).toHaveValue(
      "https://localhost:8099/webhook/extensions/example/oauth/stable",
    );
  });

  it.each(["unresolvable", "disallowed"])(
    "does not ask the operator to reset a configured origin when it is %s",
    async (reason) => {
      mockedRedirect.mockRejectedValueOnce(new HttpError({
        payload: { error: "oauth_public_base_url_unavailable", reason, detail: "do-not-display" },
        status: 503,
        url: "/api/test",
      }));
      renderSetup();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(reason === "unresolvable" ? "DNS" : "public address");
      expect(alert).not.toHaveTextContent("config set");
      expect(alert).not.toHaveTextContent("do-not-display");
      expect(screen.getByRole("button", { name: "Retry redirect registration" })).toBeEnabled();
    },
  );

  it("does not let a stale setup-status response overwrite successful registration", async () => {
    let resolveStaleStatus: ((value: { callback_registration_ready: boolean }) => void) | undefined;
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const statusKey = [
      "extension-oauth",
      "project-a",
      "install-a",
      "example",
      "setup-status",
    ] as const;
    const staleRequest = queryClient.fetchQuery({
      queryKey: statusKey,
      queryFn: () => new Promise<{ callback_registration_ready: boolean }>((resolve) => {
        resolveStaleStatus = resolve;
      }),
    }).catch(() => undefined);

    render(
      <QueryClientProvider client={queryClient}>
        <ExtensionOAuthSetup
          canManage
          connectionTypes={connectionTypes}
          connections={[]}
          installId="install-a"
          mode="setup"
          onNavigatePage={vi.fn()}
          projectId="project-a"
          provider={provider}
          returnPageId="oauth_setup"
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByLabelText("OAuth Redirect URI")).toHaveValue(
      "https://app.example.test/callback/exact",
    );
    resolveStaleStatus?.({ callback_registration_ready: false });
    await staleRequest;
    expect(queryClient.getQueryData(statusKey)).toEqual({ callback_registration_ready: true });
  });

  it.each(["setup", "connections"] as const)(
    "renders only the loading state before %s configurations resolve",
    async (mode) => {
      mockedConfigurations.mockImplementation(() => new Promise<never>(() => undefined));

      renderSetup({ mode });

      expect(await screen.findByText("Loading configurations…")).toBeVisible();
      expect(screen.queryByRole("form", { name: "OAuth configuration" })).not.toBeInTheDocument();
      expect(screen.queryByRole("combobox", { name: "OAuth configuration" })).not.toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Connect an account" })).not.toBeInTheDocument();
    },
  );

  it("renders an inert configuration error with an explicit successful retry", async () => {
    const user = userEvent.setup();
    mockedConfigurations
      .mockRejectedValueOnce(new Error("raw configuration failure token=secret"))
      .mockResolvedValueOnce([primary]);

    renderSetup();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Configurations could not be loaded.",
    );
    expect(screen.queryByRole("form", { name: "OAuth configuration" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "OAuth configuration" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry loading configurations" }));

    expect(await screen.findByRole("form", { name: "OAuth configuration" })).toBeVisible();
    expect(mockedConfigurations).toHaveBeenCalledTimes(2);
  });

  it.each([
    { allowedConnectionTypeIds: [], allowedIntentIds: [] },
    { allowedConnectionTypeIds: ["example_mail"], allowedIntentIds: [] },
    {
      allowedConnectionTypeIds: ["example_mail", "other_mail"],
      allowedIntentIds: ["connect", "other_connect"],
    },
  ])("fails closed without at least one exact type and one exact intent", async (declaration) => {
    renderSetup({ mode: "connections", ...declaration });

    expect(await screen.findByRole("heading", { name: "Example Identity" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Connect an account" })).not.toBeInTheDocument();
  });

  it("renders declared copy, exact read-only Redirect URI, and stored-secret form semantics", async () => {
    const user = userEvent.setup();
    renderSetup();

    expect(await screen.findByRole("heading", { name: "Example Identity" })).toBeVisible();
    expect(screen.getByText("Connect an Example account.")).toBeVisible();
    expect(await screen.findByDisplayValue("https://app.example.test/callback/exact")).toHaveAttribute("readonly");
    const applicationId = screen.getByLabelText(/Application ID/);
    const applicationSecret = screen.getByLabelText(/Application secret/);
    expect(applicationId).toBeRequired();
    expect(applicationSecret).toBeRequired();
    expect(applicationSecret).toBeDisabled();
    expect(applicationSecret).toHaveValue("••••••••••••");
    expect(screen.getByText("Stored value will be kept.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Replace" })).toHaveClass("fs-button", "sm");
    expect(screen.getByRole("button", { name: "Save configuration" })).toBeDisabled();

    await user.clear(screen.getByLabelText(/Configuration name/));
    await user.type(screen.getByLabelText(/Configuration name/), "Renamed application");
    expect(screen.getByRole("button", { name: "Save configuration" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    await waitFor(() => expect(mockedSave).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        display_name: "Renamed application",
        secret_replacements: {},
        values: { client_id: "saved-client" },
      }),
    ));
  });

  it("hides provider help when it would reopen the current setup guide", async () => {
    renderSetup({
      providerDeclaration: {
        ...provider,
        setup_page_id: "setup-guide",
        help_page_id: "setup-guide",
      },
      returnPageId: "setup-guide",
    });

    expect(await screen.findByRole("heading", { name: "Example Identity" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Open provider help" })).not.toBeInTheDocument();
  });

  it("requires, validates, and can cancel an explicit stored-secret replacement", async () => {
    const user = userEvent.setup();
    renderSetup();
    await screen.findByText("Stored value will be kept.");
    const secret = await screen.findByLabelText(/Application secret/);

    await user.click(screen.getByRole("button", { name: "Replace" }));
    expect(secret).toBeEnabled();
    expect(secret).toBeRequired();
    expect(secret).toHaveValue("");
    expect(secret).toHaveFocus();
    await user.type(secret, "abc");
    expect(screen.getByRole("button", { name: "Save configuration" })).toBeEnabled();
    await user.tab();
    expect(await screen.findByText("Application secret must be at least 4 characters.")).toHaveAttribute("role", "alert");
    await user.click(screen.getByRole("button", { name: "Save configuration" }));
    expect(await screen.findByText("Application secret must be at least 4 characters.")).toHaveAttribute("role", "alert");
    expect(secret).toHaveAttribute("aria-invalid", "true");
    expect(secret).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Cancel replacement" }));
    expect(secret).toBeDisabled();
    expect(secret).toHaveValue("••••••••••••");
    expect(screen.getByText("Stored value will be kept.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Save configuration" })).toBeDisabled();
  });

  it("lets Save explain untouched required fields and focuses the first invalid field", async () => {
    const user = userEvent.setup();
    mockedConfigurations.mockResolvedValue([]);
    renderSetup();

    await user.type(await screen.findByLabelText(/Configuration name/), "New application");
    const save = screen.getByRole("button", { name: "Save configuration" });
    expect(save).toBeEnabled();
    await user.click(save);

    const applicationId = screen.getByLabelText(/Application ID/);
    const applicationSecret = screen.getByLabelText(/Application secret/);
    await waitFor(() => expect(applicationId).toHaveFocus());
    for (const field of [applicationId, applicationSecret]) {
      expect(field).toHaveAttribute("aria-invalid", "true");
      expect(field).toHaveAttribute("aria-describedby", expect.stringContaining("error"));
      expect(field).toHaveClass("fs-input");
    }
    expect(screen.getByText("Application ID is required.")).toHaveAttribute("role", "alert");
    expect(screen.getByText("Application secret is required.")).toHaveAttribute("role", "alert");
  });

  it("keeps Save enabled while dirty values are below declared minimums", async () => {
    const user = userEvent.setup();
    mockedConfigurations.mockResolvedValue([]);
    renderSetup();

    await user.type(await screen.findByLabelText(/Configuration name/), "New application");
    const save = screen.getByRole("button", { name: "Save configuration" });
    expect(save).toBeEnabled();

    const applicationId = screen.getByLabelText(/Application ID/);
    const applicationSecret = screen.getByLabelText(/Application secret/);
    await user.type(applicationId, "a");
    await user.type(applicationSecret, "abc");
    expect(save).toBeEnabled();
    await user.type(applicationId, "b");
    await user.type(applicationSecret, "d");
    expect(save).toBeEnabled();
  });

  it("validates and submits secret lengths without trimming secret bytes", async () => {
    const user = userEvent.setup();
    mockedConfigurations.mockResolvedValue([]);
    renderSetup();

    await user.type(await screen.findByLabelText(/Configuration name/), "Raw secret");
    await user.type(screen.getByLabelText(/Application ID/), "client");
    await user.type(screen.getByLabelText(/Application secret/), " ab ");
    const save = screen.getByRole("button", { name: "Save configuration" });
    expect(save).toBeEnabled();
    await user.click(save);

    await waitFor(() => expect(mockedPreview).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ secret_replacements: { client_secret: " ab " } }),
    ));
  });

  it("maps a declared backend credential error to a safe focused field error", async () => {
    const user = userEvent.setup();
    mockedPreview.mockRejectedValueOnce(new HttpError({
      payload: {
        error: "invalid_credential_field",
        field_id: "client_id",
        reason: "provider_error=secret-value",
      },
      status: 400,
      url: "/api/test",
    }));
    renderSetup();

    await user.clear(await screen.findByLabelText(/Application ID/));
    await user.type(screen.getByLabelText(/Application ID/), "changed-client");
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    const applicationId = screen.getByLabelText(/Application ID/);
    expect(await screen.findByText("Application ID is invalid. Check this field and try again.")).toHaveAttribute("role", "alert");
    expect(applicationId).toHaveAttribute("aria-invalid", "true");
    expect(applicationId).toHaveFocus();
    expect(screen.queryByText(/secret-value/)).not.toBeInTheDocument();
  });

  it("keeps unknown backend credential payloads as a safe form error", async () => {
    const user = userEvent.setup();
    mockedPreview.mockRejectedValueOnce(new HttpError({
      payload: {
        error: "invalid_credential_field",
        field_id: "untrusted_field",
        reason: "provider_error=secret-value",
      },
      status: 400,
      url: "/api/test",
    }));
    renderSetup();

    await user.clear(await screen.findByLabelText(/Application ID/));
    await user.type(screen.getByLabelText(/Application ID/), "changed-client");
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Configuration could not be saved. Check the fields and try again.");
    expect(screen.queryByText(/secret-value/)).not.toBeInTheDocument();
  });

  it("enforces declared maximum with inline focus and accessible association", async () => {
    const user = userEvent.setup();
    renderSetup();
    await screen.findByText("Stored value will be kept.");
    const applicationId = await screen.findByLabelText(/Application ID/);
    fireEvent.change(applicationId, { target: { value: "x".repeat(41) } });
    expect(screen.getByRole("button", { name: "Save configuration" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    const error = await screen.findByText("Application ID must be at most 40 characters.");
    expect(error).toHaveAttribute("role", "alert");
    expect(applicationId).toHaveAttribute("aria-invalid", "true");
    expect(applicationId).toHaveAttribute("aria-describedby", expect.stringContaining("error"));
    await waitFor(() => expect(applicationId).toHaveFocus());
  });

  it.each([
    "not-a-url",
    "http://identity.example.test",
    "http://localhost:3000",
    "https://user@identity.example.test",
    "https://identity.example.test/path",
    "https://identity.example.test?query=1",
    "https://identity.example.test#fragment",
    "https://[invalid",
    "https://identity.example.test:invalid",
    "https://identity.example.test:0",
    "https://identity.example.test:443",
    "https://identity.example.test/",
    "https://Identity.example.test",
    "https://identity..example.test",
  ])("rejects a non-canonical declared HTTPS origin before any API call: %s", async (origin) => {
    renderSetup({ providerDeclaration: originProvider });
    await screen.findByText("Stored value will be kept.");
    const originInput = await screen.findByLabelText("Browser origin");

    fireEvent.change(originInput, { target: { value: origin } });
    expect(screen.getByRole("button", { name: "Save configuration" })).toBeEnabled();
    await userEvent.setup().click(screen.getByRole("button", { name: "Save configuration" }));

    const error = await screen.findByText(
      "Browser origin must be a canonical HTTPS origin.",
    );
    expect(error).toHaveAttribute("role", "alert");
    expect(originInput).toHaveAttribute("aria-invalid", "true");
    expect(originInput).toHaveAttribute("aria-describedby", expect.stringContaining("error"));
    expect(originInput).toHaveClass("fs-input");
    expect(originInput).toHaveFocus();
    expect(mockedPreview).not.toHaveBeenCalled();
    expect(mockedSave).not.toHaveBeenCalled();
  });

  it("focuses an invalid browser origin when Save focus finishes before error commit", async () => {
    const user = userEvent.setup();
    renderSetup({ providerDeclaration: originProvider });
    await screen.findByText("Stored value will be kept.");
    const browserOrigin = await screen.findByLabelText("Browser origin");
    await user.type(browserOrigin, "http://browser.example.test/path");
    const save = screen.getByRole("button", { name: "Save configuration" });
    const form = screen.getByRole("form", { name: "OAuth configuration" });
    act(() => {
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      save.focus();
    });

    expect(
      screen.getByText("Browser origin must be a canonical HTTPS origin."),
    ).toBeVisible();
    await waitFor(() => expect(browserOrigin).toHaveFocus());
    expect(mockedPreview).not.toHaveBeenCalled();
    expect(mockedSave).not.toHaveBeenCalled();
  });

  it("focuses the invalid field after the first pointer submission and blur settle", async () => {
    const user = userEvent.setup();
    renderSetup({ providerDeclaration: originProvider });
    await screen.findByText("Stored value will be kept.");
    const browserOrigin = await screen.findByLabelText("Browser origin");
    await user.type(browserOrigin, "http://browser.example.test/path");
    const save = screen.getByRole("button", { name: "Save configuration" });
    let focusFrame: FrameRequestCallback | undefined;
    const requestFrame = vi.spyOn(globalThis, "requestAnimationFrame")
      .mockImplementation((callback) => {
        focusFrame = callback;
        return 1;
      });
    const cancelFrame = vi.spyOn(globalThis, "cancelAnimationFrame").mockImplementation(() => {});
    try {
      fireEvent.pointerDown(save);
      act(() => save.focus());
      expect(
        screen.getByText("Browser origin must be a canonical HTTPS origin."),
      ).toBeVisible();
      expect(browserOrigin).toHaveAttribute("aria-invalid", "true");
      fireEvent.pointerUp(save);
      expect(focusFrame).toBeTypeOf("function");

      // In a real browser, inserting the blur error can move the button enough
      // to cancel click activation and restore its native pointer focus.
      act(() => save.focus());
      expect(save).toHaveFocus();
      act(() => focusFrame?.(globalThis.performance.now()));
      expect(browserOrigin).toHaveFocus();
      act(() => save.focus());
      expect(save).toHaveFocus();
    } finally {
      requestFrame.mockRestore();
      cancelFrame.mockRestore();
    }
  });

  it("does not steal focus for blur-only validation", async () => {
    const user = userEvent.setup();
    renderSetup({ providerDeclaration: originProvider });
    await screen.findByText("Stored value will be kept.");
    const browserOrigin = await screen.findByLabelText("Browser origin");
    await user.type(browserOrigin, "http://browser.example.test/path");
    const configurationName = screen.getByLabelText(/Configuration name/);

    act(() => configurationName.focus());

    expect(
      screen.getByText("Browser origin must be a canonical HTTPS origin."),
    ).toBeVisible();
    expect(configurationName).toHaveFocus();
  });

  it("cancels pending invalid-field focus when pointer activation is canceled", async () => {
    const user = userEvent.setup();
    renderSetup({ providerDeclaration: originProvider });
    await screen.findByText("Stored value will be kept.");
    const browserOrigin = await screen.findByLabelText("Browser origin");
    await user.type(browserOrigin, "http://browser.example.test/path");
    const save = screen.getByRole("button", { name: "Save configuration" });

    fireEvent.pointerDown(save);
    act(() => save.focus());
    fireEvent.pointerCancel(save);
    act(() => browserOrigin.focus());
    act(() => save.focus());
    await act(async () => {
      await new Promise((resolve) => globalThis.setTimeout(resolve, 30));
    });

    expect(save).toHaveFocus();
  });

  it("does not restore stale validation focus after the field is corrected", async () => {
    renderSetup({ providerDeclaration: originProvider });
    await screen.findByText("Stored value will be kept.");
    const browserOrigin = await screen.findByLabelText("Browser origin");
    fireEvent.change(browserOrigin, { target: { value: "http://browser.example.test/path" } });
    fireEvent.click(screen.getByRole("button", { name: "Save configuration" }));

    fireEvent.change(browserOrigin, { target: { value: "https://browser.example.test" } });
    const configurationName = screen.getByLabelText(/Configuration name/);
    act(() => configurationName.focus());
    await act(async () => {
      await new Promise((resolve) => globalThis.setTimeout(resolve, 30));
    });

    expect(configurationName).toHaveFocus();
    expect(
      screen.queryByText("Browser origin must be a canonical HTTPS origin."),
    ).not.toBeInTheDocument();
  });

  it("accepts a canonical declared HTTPS origin", async () => {
    const user = userEvent.setup();
    renderSetup({ providerDeclaration: originProvider });
    await screen.findByText("Stored value will be kept.");
    const originInput = await screen.findByLabelText("Browser origin");

    await user.type(originInput, "https://identity.example.test");
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    await waitFor(() => expect(mockedPreview).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        values: expect.objectContaining({
          browser_origin: "https://identity.example.test",
        }),
      }),
    ));
    expect(originInput).not.toHaveAttribute("aria-invalid");
  });

  it("requires explicit confirmation for a reauthorization impact", async () => {
    const user = userEvent.setup();
    mockedPreview.mockResolvedValue({
      configuration_id: primary.configuration_id,
      expected_revision: 3,
      changed_fields: ["client_id"],
      change_policies: ["reauthorization_required"],
      connections: [{ connection_id: "connection-a", display_name: "Mailbox" }],
      requires_confirmation: true,
      confirmation_token: "confirm-impact",
    });
    renderSetup();

    await user.clear(await screen.findByLabelText(/Application ID/));
    await user.type(screen.getByLabelText(/Application ID/), "new-client");
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    const impactDialog = await screen.findByRole("alertdialog", {
      name: "Confirm configuration impact",
    });
    expect(impactDialog).toHaveTextContent("Mailbox");
    expect(impactDialog).toHaveAccessibleDescription("Mailbox will need to authorize again.");
    // Canon: a dialog opens on its first control, never on the page behind it.
    const impactClose = within(impactDialog).getByRole("button", { name: "Close Confirm configuration impact" });
    await waitFor(() => expect(impactClose).toHaveFocus());
    await user.tab();
    const impactCancel = within(impactDialog).getByRole("button", { name: "Cancel" });
    expect(impactCancel).toHaveFocus();
    await user.tab();
    expect(within(impactDialog).getByRole("button", { name: "Confirm and save" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(impactCancel).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog", { name: "Confirm configuration impact" }))
      .not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Save configuration" }))
      .toHaveFocus());
    expect(mockedSave).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Save configuration" }));
    await screen.findByRole("alertdialog", { name: "Confirm configuration impact" });
    await user.click(screen.getByRole("button", { name: "Confirm and save" }));
    expect(mockedSave).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ confirmation_token: "confirm-impact" }),
    );
  });

  it("selects the exact newly created configuration in Setup", async () => {
    const user = userEvent.setup();
    const created = {
      ...primary,
      configuration_id: "33333333-3333-4333-8333-333333333333",
      display_name: "Created application",
    };
    mockedConfigurations.mockResolvedValueOnce([]).mockResolvedValue([primary, created]);
    mockedSave.mockResolvedValue(created);
    renderSetup();

    await user.type(await screen.findByLabelText(/Configuration name/), "Created application");
    await user.type(screen.getByLabelText(/Application ID/), "created-client");
    await user.type(screen.getByLabelText(/Application secret/), "created-secret");
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    await waitFor(() => expect(screen.getByRole("combobox", { name: "OAuth configuration" })).toHaveTextContent(created.display_name));
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
  });

  it("resets Setup configuration selection when the project scope changes in place", async () => {
    const user = userEvent.setup();
    const secondary = {
      ...primary,
      configuration_id: "22222222-2222-4222-8222-222222222222",
      display_name: "Project A secondary",
    };
    const projectBPrimary = {
      ...primary,
      configuration_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      display_name: "Project B primary",
    };
    mockedConfigurations.mockImplementation(async (scope) => (
      scope.projectId === "project-a" ? [primary, secondary] : [projectBPrimary]
    ));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const renderForProject = (projectId: string) => (
      <QueryClientProvider client={queryClient}>
        <ExtensionOAuthSetup
          canManage
          connectionTypes={connectionTypes}
          connections={[]}
          installId="install-a"
          onNavigatePage={vi.fn()}
          projectId={projectId}
          provider={provider}
          returnPageId="oauth_setup"
        />
      </QueryClientProvider>
    );
    const view = render(renderForProject("project-a"));

    await user.click(await screen.findByRole("combobox", { name: "OAuth configuration" }));
    await user.click(await screen.findByRole("option", { name: "Project A secondary" }));
    expect(screen.getByRole("combobox", { name: "OAuth configuration" })).toHaveTextContent(
      secondary.display_name,
    );

    view.rerender(renderForProject("project-b"));

    await waitFor(() => expect(
      screen.getByRole("combobox", { name: "OAuth configuration" }),
    ).toHaveTextContent(projectBPrimary.display_name));
    await user.click(screen.getByRole("combobox", { name: "OAuth configuration" }));
    expect(await screen.findByRole("option", { name: "Project B primary" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Project A secondary" })).not.toBeInTheDocument();
  });

  it("keeps Connections usable after the project scope changes in place", async () => {
    const user = userEvent.setup();
    const secondary = {
      ...primary,
      configuration_id: "22222222-2222-4222-8222-222222222222",
      display_name: "Project A secondary",
    };
    const projectBPrimary = {
      ...primary,
      configuration_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      display_name: "Project B primary",
    };
    mockedConfigurations.mockImplementation(async (scope) => (
      scope.projectId === "project-a" ? [primary, secondary] : [projectBPrimary]
    ));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const renderForProject = (projectId: string) => (
      <QueryClientProvider client={queryClient}>
        <ExtensionOAuthSetup
          allowedConnectionTypeIds={["example_mail"]}
          allowedIntentIds={["connect"]}
          canManage
          connectionTypes={connectionTypes}
          connections={[]}
          installId="install-a"
          mode="connections"
          onNavigatePage={vi.fn()}
          projectId={projectId}
          provider={provider}
          returnPageId="connections"
        />
      </QueryClientProvider>
    );
    const view = render(renderForProject("project-a"));

    await user.click(await screen.findByRole("combobox", { name: "Configuration for new connection" }));
    await user.click(await screen.findByRole("option", { name: "Project A secondary" }));
    view.rerender(renderForProject("project-b"));

    await waitFor(() => expect(mockedConfigurations).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: "project-b" }),
    ));
    await screen.findByRole("form", { name: "Connect an account" });
    expect(screen.queryByRole("combobox", { name: "Configuration for new connection" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Project A secondary" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect" })).toBeEnabled();
  });

  it("starts only a declared intent from a connection page", async () => {
    const user = userEvent.setup();
    mockedConfigurations.mockResolvedValue([
      primary,
      { ...primary, configuration_id: "22222222-2222-4222-8222-222222222222", display_name: "Secondary" },
    ]);
    renderSetup({ mode: "connections" });

    await screen.findByRole("form", { name: "Connect an account" });
    expect(screen.getByRole("combobox", { name: "Configuration for new connection" })).toBeVisible();
    await user.type(screen.getByLabelText(/Connection name/), "My mailbox");
    await user.type(screen.getByLabelText("Email hint"), "person@example.test");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(mockedStart).toHaveBeenCalledWith(
      expect.anything(),
      {
        configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        intent_id: "connect",
        display_name: "My mailbox",
        return_page_id: "oauth_setup",
        intent_inputs: { email_hint: "person@example.test" },
      },
    );
    expect(onAuthorization).toHaveBeenCalledWith("https://identity.example.test/authorize?opaque=1");
  });

  it("lets a user submit an empty connection name to reveal its accessible validation error", async () => {
    const user = userEvent.setup();
    renderSetup({ mode: "connections" });

    await screen.findByRole("form", { name: "Connect an account" });
    const connect = await screen.findByRole("button", { name: "Connect" });
    expect(connect).toBeEnabled();
    await user.click(connect);

    const connectionName = screen.getByLabelText(/Connection name/);
    expect(await screen.findByText("Connection name is required.")).toHaveAttribute("role", "alert");
    expect(connectionName).toHaveAttribute("aria-invalid", "true");
    expect(connectionName).toHaveFocus();
  });

  it("validates declared email intent inputs before starting authorization", async () => {
    const user = userEvent.setup();
    renderSetup({ mode: "connections" });

    await screen.findByRole("form", { name: "Connect an account" });
    await user.type(await screen.findByLabelText(/Connection name/), "Mailbox");
    await user.type(screen.getByLabelText("Email hint"), "not-an-email");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    const emailHint = screen.getByRole("textbox", { name: "Email hint" });
    expect(await screen.findByText("Email hint must be a valid email address.")).toHaveAttribute("role", "alert");
    expect(emailHint).toHaveAttribute("aria-invalid", "true");
    expect(emailHint).toHaveClass("fs-input");
    expect(emailHint).toHaveFocus();
    expect(mockedStart).not.toHaveBeenCalled();
  });

  it("maps a declared authorization error to the exact intent input", async () => {
    const user = userEvent.setup();
    mockedStart.mockRejectedValueOnce(new HttpError({
      payload: {
        error: "invalid_oauth_intent_input_value",
        field: "email_hint",
        reason: "provider_error=secret-value",
      },
      status: 400,
      url: "/api/test",
    }));
    renderSetup({ mode: "connections" });

    await user.type(await screen.findByLabelText(/Connection name/), "Mailbox");
    const emailHint = screen.getByRole("textbox", { name: "Email hint" });
    await user.type(emailHint, "person@example.test");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByText("Email hint is invalid. Check this field and try again.")).toHaveAttribute("role", "alert");
    expect(emailHint).toHaveAttribute("aria-invalid", "true");
    expect(emailHint).toHaveAttribute("aria-describedby", expect.stringContaining("error"));
    expect(emailHint).toHaveClass("fs-input");
    expect(emailHint).toHaveFocus();
    expect(screen.queryByText(/secret-value/)).not.toBeInTheDocument();
  });

  it("keeps an undeclared authorization error on the generic safe form message", async () => {
    const user = userEvent.setup();
    mockedStart.mockRejectedValueOnce(new HttpError({
      payload: {
        error: "invalid_oauth_intent_input_value",
        field: "redirect_uri",
        reason: "provider_error=secret-value",
      },
      status: 400,
      url: "/api/test",
    }));
    renderSetup({ mode: "connections" });

    await user.type(await screen.findByLabelText(/Connection name/), "Mailbox");
    const emailHint = screen.getByRole("textbox", { name: "Email hint" });
    await user.type(emailHint, "person@example.test");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Authorization could not be started. Check the configuration and try again.",
    );
    expect(emailHint).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByText(/secret-value|redirect_uri/)).not.toBeInTheDocument();
  });

  it("moves a setup an extension update left behind, then starts authorization", async () => {
    const user = userEvent.setup();
    mockedStart
      .mockRejectedValueOnce(new HttpError({
        payload: { error: "oauth_release_transition_required" },
        status: 412,
        url: "/api/test",
      }))
      .mockResolvedValueOnce({ authorization_url: "https://provider.example/authorize" } as never);
    mockedReconcile.mockResolvedValueOnce({} as never);
    renderSetup({ mode: "connections" });

    await user.type(await screen.findByLabelText(/Connection name/), "Mailbox");
    await user.type(screen.getByRole("textbox", { name: "Email hint" }), "person@example.test");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(onAuthorization).toHaveBeenCalledWith("https://provider.example/authorize"));
    expect(mockedReconcile).toHaveBeenCalledTimes(1);
    expect(mockedStart).toHaveBeenCalledTimes(2);
  });

  it("explains a release that changed how the extension signs in", async () => {
    const user = userEvent.setup();
    mockedStart.mockRejectedValueOnce(new HttpError({
      payload: { error: "oauth_release_transition_required" },
      status: 412,
      url: "/api/test",
    }));
    mockedReconcile.mockRejectedValueOnce(new HttpError({
      payload: { error: "oauth_release_reapproval_required" },
      status: 412,
      url: "/api/test",
    }));
    renderSetup({ mode: "connections" });

    await user.type(await screen.findByLabelText(/Connection name/), "Mailbox");
    await user.type(screen.getByRole("textbox", { name: "Email hint" }), "person@example.test");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This extension update changed how it signs in.",
    );
  });

  it("explains an invalid compiled OAuth connection definition without exposing internals", async () => {
    const user = userEvent.setup();
    mockedStart.mockRejectedValueOnce(new HttpError({
      payload: {
        error: "unknown_oauth_connection_type",
        reason: "descriptor_path=secret-value",
      },
      status: 400,
      url: "/api/test",
    }));
    renderSetup({ mode: "connections" });

    await user.type(await screen.findByLabelText(/Connection name/), "Mailbox");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This extension's OAuth connection setup is invalid. Update or reinstall the extension, then try again.",
    );
    expect(screen.queryByText(/secret-value|descriptor_path/)).not.toBeInTheDocument();
  });

  it("keeps authorization failures safe when the intent omits optional user inputs", async () => {
    const user = userEvent.setup();
    mockedStart.mockRejectedValueOnce(new HttpError({
      payload: {
        error: "invalid_oauth_intent_input_value",
        field: "undeclared_input",
        reason: "provider_error=secret-value",
      },
      status: 400,
      url: "/api/test",
    }));
    renderSetup({
      mode: "connections",
      providerDeclaration: {
        ...provider,
        authorization_intents: [{ intent_id: "connect" }],
      },
    });

    await user.type(await screen.findByLabelText(/Connection name/), "Mailbox");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Authorization could not be started. Check the configuration and try again.",
    );
    expect(screen.queryByText(/secret-value|undeclared_input/)).not.toBeInTheDocument();
  });

  it("scopes connection form IDs and validation focus to the owning composer", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <>
          <ExtensionOAuthSetup
            canManage
            allowedConnectionTypeIds={["example_mail"]}
            allowedIntentIds={["connect"]}
            connectionTypes={connectionTypes}
            connections={[]}
            installId="install-a"
            mode="connections"
            onNavigatePage={vi.fn()}
            projectId="project-a"
            provider={provider}
            returnPageId="connections"
          />
          <ExtensionOAuthSetup
            canManage
            allowedConnectionTypeIds={["example_mail"]}
            allowedIntentIds={["connect"]}
            connectionTypes={connectionTypes}
            connections={[]}
            installId="install-b"
            mode="connections"
            onNavigatePage={vi.fn()}
            projectId="project-b"
            provider={provider}
            returnPageId="connections"
          />
        </>
      </QueryClientProvider>,
    );

    await screen.findAllByRole("form", { name: "Connect an account" });
    const connectionNames = await screen.findAllByLabelText(/Connection name/);
    expect(connectionNames).toHaveLength(2);
    expect(connectionNames[0]).not.toHaveAttribute("id", connectionNames[1].id);
    const connectButtons = screen.getAllByRole("button", { name: "Connect" });
    await user.click(connectButtons[1]);

    await screen.findAllByText("Connection name is required.");
    expect(connectionNames[1]).toHaveAttribute("aria-invalid", "true");
    expect(connectionNames[1]).toHaveFocus();
    expect(connectionNames[0]).not.toHaveAttribute("aria-invalid", "true");
  });

  it("scopes configuration field IDs and validation focus to the owning form", async () => {
    const user = userEvent.setup();
    render(
      <>
        <OAuthConfigurationForm
          onSaved={vi.fn()}
          provider={provider}
          scope={{ projectId: "project-a", installId: "install-a", providerId: "example" }}
        />
        <OAuthConfigurationForm
          onSaved={vi.fn()}
          provider={provider}
          scope={{ projectId: "project-b", installId: "install-b", providerId: "example" }}
        />
      </>,
    );

    const applicationIds = screen.getAllByLabelText(/Application ID/);
    expect(applicationIds).toHaveLength(2);
    expect(applicationIds[0]).not.toHaveAttribute("id", applicationIds[1].id);
    const configurationNames = screen.getAllByLabelText(/Configuration name/);
    await user.type(configurationNames[1], "Second application");
    const secondSave = screen.getAllByRole("button", { name: "Save configuration" })[1];
    expect(secondSave).toBeEnabled();
    await user.click(secondSave);

    expect(applicationIds[1]).toHaveAttribute("aria-invalid", "true");
    expect(applicationIds[1]).toHaveFocus();
    expect(applicationIds[0]).not.toHaveAttribute("aria-invalid", "true");
  });

  it.each([
    { page: "Mail", connectionType: "fixture_mail_account", intent: "mail_connect" },
    { page: "Files", connectionType: "fixture_files_account", intent: "files_connect" },
    { page: "Records", connectionType: "fixture_records_account", intent: "records_connect" },
  ])("restricts the $page connection page to its exact declared type and intent", async ({ connectionType, intent }) => {
    const user = userEvent.setup();
    const workspaceProvider = {
      ...provider,
      authorization_intents: [
        { intent_id: "mail_connect", user_inputs: [] },
        { intent_id: "files_connect", user_inputs: [] },
        { intent_id: "records_connect", user_inputs: [] },
      ],
    };
    const workspaceConnectionTypes = [
      { connection_type_id: "fixture_mail_account", display_name: "Mail account", auth: { type: "oauth2", provider_id: "example", scopes: { required: ["mail.read"] } } },
      { connection_type_id: "fixture_files_account", display_name: "Files account", auth: { type: "oauth2", provider_id: "example", scopes: { required: ["files.read"] } } },
      { connection_type_id: "fixture_records_account", display_name: "Records account", auth: { type: "oauth2", provider_id: "example", scopes: { required: ["records.read"] } } },
    ];
    renderSetup({
      allowedConnectionTypeIds: [connectionType],
      allowedIntentIds: [intent],
      connectionTypeDeclarations: workspaceConnectionTypes,
      mode: "connections",
      providerDeclaration: workspaceProvider,
    });

    await screen.findByRole("form", { name: "Connect an account" });
    expect(screen.queryByLabelText("Connection type")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Authorization intent")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText(/Connection name/), `${intent} connection`);
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(mockedStart).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({
      connection_type: connectionType,
      intent_id: intent,
    }));
    expect(JSON.stringify(mockedStart.mock.calls)).not.toContain(
      connectionType === "fixture_mail_account" ? "files_connect" : "mail_connect",
    );
  });

  it("keeps viewer data readable while removing every mutation control", async () => {
    renderSetup({
      canManage: false,
      mode: "connections",
      onTestConnection: vi.fn(),
      connections: [{
        connection_id: "connection-a",
        display_name: "Viewer mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: "needs_attention" } },
      }],
    });

    const viewerRow = await screen.findByLabelText("Connection Viewer mailbox");
    expect(viewerRow).toHaveTextContent("Configuration: Primary application");
    expect(screen.getByText("Viewer mailbox")).toBeVisible();
    expect(screen.getByText("Needs attention")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save configuration" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Test Viewer mailbox/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reconnect/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
    expect(mockedRedirect).not.toHaveBeenCalled();
  });

  it("shows each viewer connection configuration without an inert selector", async () => {
    const secondary = {
      ...primary,
      configuration_id: "22222222-2222-4222-8222-222222222222",
      display_name: "Secondary application",
    };
    mockedConfigurations.mockResolvedValue([primary, secondary]);
    renderSetup({
      canManage: false,
      mode: "connections",
      connections: [
        {
          connection_id: "connection-primary",
          display_name: "Primary mailbox",
          extension_install_id: "install-a",
          oauth_provider_id: "example",
          oauth_configuration_id: primary.configuration_id,
          connection_type: "example_mail",
          connection_config: { oauth: { lifecycle_status: "connected" } },
        },
        {
          connection_id: "connection-secondary",
          display_name: "Secondary mailbox",
          extension_install_id: "install-a",
          oauth_provider_id: "example",
          oauth_configuration_id: secondary.configuration_id,
          connection_type: "example_mail",
          connection_config: { oauth: { lifecycle_status: "connected" } },
        },
      ],
    });

    const primaryRow = await screen.findByLabelText("Connection Primary mailbox");
    const secondaryRow = screen.getByLabelText("Connection Secondary mailbox");
    expect(screen.queryByRole("combobox", { name: /configuration/i })).not.toBeInTheDocument();
    expect(primaryRow).toHaveTextContent("Configuration: Primary application");
    expect(secondaryRow).toHaveTextContent("Configuration: Secondary application");
  });

  it.each([
    ["post_connect_pending", "Post connect pending"],
    ["disconnecting", "Disconnecting"],
    ["reconnect_required", "Reconnect required"],
  ] as const)("renders the persisted %s lifecycle accurately for viewers", async (lifecycle, label) => {
    renderSetup({
      canManage: false,
      mode: "connections",
      onTestConnection: vi.fn(),
      connections: [{
        connection_id: `connection-${lifecycle}`,
        display_name: "Viewer connection",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: lifecycle } },
      }],
    });

    expect(await screen.findByText(label)).toBeVisible();
    expect(screen.queryByText("Pending")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save configuration" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Test Viewer connection/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reconnect/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
  });

  it.each([
    ["authorization_required", "A project manager must reconnect this account."],
    ["reconnect_required", "A project manager must reconnect this account."],
    ["disconnected", "A project manager can reconnect this account."],
  ] as const)("gives viewers explicit recovery guidance for %s", async (lifecycle, guidance) => {
    renderSetup({
      canManage: false,
      mode: "connections",
      onTestConnection: vi.fn(),
      connections: [{
        connection_id: `connection-${lifecycle}`,
        display_name: "Viewer recovery account",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: lifecycle } },
      }],
    });

    expect(await screen.findByText(guidance)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Test Viewer recovery account/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reconnect/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
  });

  it("shows a disconnecting connection to managers without offering another action", async () => {
    renderSetup({
      mode: "connections",
      onTestConnection: vi.fn(),
      connections: [{
        connection_id: "connection-disconnecting",
        display_name: "Disconnecting mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: "disconnecting" } },
      }],
    });

    expect(await screen.findByText("Disconnect was interrupted. Resume it to finish revocation and local cleanup.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Resume disconnect Disconnecting mailbox" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Test Disconnecting mailbox/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reconnect/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
  });

  it("treats a malformed disconnect lease as an interrupted recoverable state", async () => {
    renderSetup({
      mode: "connections",
      connections: [{
        connection_id: "connection-malformed-lease",
        display_name: "Malformed lease mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: {
            lifecycle_status: "disconnecting",
            disconnect_operation_id: "private-malformed-operation",
            disconnect_lease_expires_at: "not-a-timestamp",
          },
        },
      }],
    });

    expect(await screen.findByText("Disconnect was interrupted. Resume it to finish revocation and local cleanup.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Resume disconnect Malformed lease mailbox" })).toBeVisible();
    expect(screen.queryByText(/private-malformed-operation/)).not.toBeInTheDocument();
  });

  it("keeps a live disconnect lease read-only for managers", async () => {
    const lease = new Date(Date.now() + 60_000).toISOString();
    renderSetup({
      mode: "connections",
      onTestConnection: vi.fn(),
      connections: [{
        connection_id: "connection-live-disconnect",
        display_name: "Live disconnect mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: {
            lifecycle_status: "disconnecting",
            disconnect_operation_id: "private-live-operation",
            disconnect_lease_expires_at: lease,
          },
        },
      }],
    });

    expect(await screen.findByText("Disconnect in progress. This connection is temporarily read-only.")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Resume disconnect/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Test Live disconnect mailbox/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reconnect/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
  });

  it("resumes an expired disconnect through the exact endpoint and refetches", async () => {
    const onConnectionsChanged = vi.fn();
    renderSetup({
      mode: "connections",
      onConnectionsChanged,
      connections: [{
        connection_id: "connection-expired-disconnect",
        display_name: "Interrupted mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: {
            lifecycle_status: "disconnecting",
            disconnect_lease_expires_at: new Date(Date.now() - 1_000).toISOString(),
          },
        },
      }],
    });

    expect(await screen.findByText("Disconnect was interrupted. Resume it to finish revocation and local cleanup.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Resume disconnect Interrupted mailbox" }));

    await waitFor(() => expect(mockedDisconnect).toHaveBeenCalledWith(
      expect.anything(),
      primary.configuration_id,
      "connection-expired-disconnect",
    ));
    await waitFor(() => expect(onConnectionsChanged).toHaveBeenCalledTimes(1));
  });

  it("reveals disconnect recovery when a live lease expires without another state change", async () => {
    renderSetup({
      mode: "connections",
      connections: [{
        connection_id: "connection-clock-transition",
        display_name: "Clock transition mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: {
            lifecycle_status: "disconnecting",
            disconnect_lease_expires_at: new Date(Date.now() + 500).toISOString(),
          },
        },
      }],
    });

    const liveGuidance = await screen.findByText("Disconnect in progress. This connection is temporarily read-only.");
    expect(liveGuidance).toBeVisible();
    expect(liveGuidance).toHaveAttribute("aria-live", "polite");
    expect(await screen.findByRole("button", { name: "Resume disconnect Clock transition mailbox" })).toBeVisible();
  });

  it("announces a busy recovery without exposing private metadata", async () => {
    mockedDisconnect.mockImplementationOnce(() => new Promise(() => undefined));
    renderSetup({
      mode: "connections",
      connections: [{
        connection_id: "connection-busy-recovery",
        display_name: "Busy recovery mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: {
            lifecycle_status: "disconnecting",
            disconnect_operation_id: "private-busy-operation",
          },
        },
      }],
    });

    const resume = await screen.findByRole("button", { name: "Resume disconnect Busy recovery mailbox" });
    fireEvent.click(resume);

    expect(await screen.findByRole("button", { name: "Resume disconnect Busy recovery mailbox" })).toHaveTextContent("Resuming…");
    expect(screen.getByRole("button", { name: "Resume disconnect Busy recovery mailbox" })).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText(/private-busy-operation/)).not.toBeInTheDocument();
  });

  it("keeps a backend live-lease conflict understandable during disconnect recovery", async () => {
    const onConnectionsChanged = vi.fn().mockRejectedValueOnce(
      new Error("private refresh failure payload"),
    );
    mockedDisconnect.mockRejectedValueOnce(new HttpError({
      payload: { error: "oauth_disconnect_in_progress", detail: "private-operation-id" },
      status: 409,
      url: "/api/test",
    }));
    renderSetup({
      mode: "connections",
      onConnectionsChanged,
      connections: [{
        connection_id: "connection-conflicted-disconnect",
        display_name: "Conflicted mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: {
            lifecycle_status: "disconnecting",
            disconnect_lease_expires_at: new Date(Date.now() - 1_000).toISOString(),
          },
        },
      }],
    });

    fireEvent.click(await screen.findByRole("button", { name: "Resume disconnect Conflicted mailbox" }));

    expect(await screen.findByText("Disconnect is still in progress. Try again after the recovery lease expires.")).toHaveAttribute("role", "alert");
    expect(onConnectionsChanged).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Resume disconnect Conflicted mailbox" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Resume disconnect Conflicted mailbox" })).toHaveAttribute("aria-busy", "false");
    expect(screen.queryByText(/private-operation-id/)).not.toBeInTheDocument();
    expect(screen.queryByText(/private refresh failure payload/)).not.toBeInTheDocument();
  });

  it("never exposes disconnect recovery controls or private lease metadata to viewers", async () => {
    renderSetup({
      canManage: false,
      mode: "connections",
      connections: [{
        connection_id: "connection-viewer-disconnect",
        display_name: "Viewer disconnect mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: {
            lifecycle_status: "disconnecting",
            disconnect_operation_id: "private-viewer-operation",
            disconnect_lease_expires_at: new Date(Date.now() - 1_000).toISOString(),
          },
        },
      }],
    });

    expect(await screen.findByText("Disconnecting")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Resume disconnect/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/private-viewer-operation/)).not.toBeInTheDocument();
  });

  it.each([
    "disconnected",
    "reconnect_required",
    "authorization_required",
    "post_connect_pending",
    "needs_attention",
    "disconnecting",
  ] as const)("does not offer Test to managers for a %s connection", async (lifecycle) => {
    renderSetup({
      mode: "connections",
      onTestConnection: vi.fn(),
      connections: [{
        connection_id: `connection-${lifecycle}`,
        display_name: "Non-runnable mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: lifecycle } },
      }],
    });

    await screen.findByText("Non-runnable mailbox");
    expect(screen.queryByRole("button", { name: /Test Non-runnable mailbox/ })).not.toBeInTheDocument();
  });

  it("offers Test to managers only for a connected connection", async () => {
    const user = userEvent.setup();
    const onTestConnection = vi.fn();
    const connection = {
      connection_id: "connection-connected",
      display_name: "Runnable mailbox",
      extension_install_id: "install-a",
      oauth_provider_id: "example",
      oauth_configuration_id: primary.configuration_id,
      connection_type: "example_mail",
      connection_config: { oauth: { lifecycle_status: "connected" } },
    };
    renderSetup({ mode: "connections", onTestConnection, connections: [connection] });

    await user.click(await screen.findByRole("button", { name: "Test Runnable mailbox" }));

    expect(onTestConnection).toHaveBeenCalledWith(connection);
  });

  it.each([
    [
      "post_connect_pending",
      "provider_unavailable",
      "Verification needs attention. A project manager must retry verification.",
    ],
    [
      "needs_attention",
      "invalid_client",
      "This connection needs attention. A project manager must repair and save its configuration in Setup.",
    ],
  ] as const)("gives viewers role-aware guidance for %s", async (lifecycle, errorCode, guidance) => {
    renderSetup({
      canManage: false,
      mode: "connections",
      connections: [{
        connection_id: `connection-${lifecycle}`,
        display_name: "Viewer mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: { lifecycle_status: lifecycle, last_error_code: errorCode },
        },
      }],
    });

    expect(await screen.findByText(guidance)).toBeVisible();
    expect(screen.queryByText("Verification needs attention. Retry when the provider is available.")).not.toBeInTheDocument();
    expect(screen.queryByText("Repair and save this connection's configuration in Setup before trying again.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save configuration" })).not.toBeInTheDocument();
  });

  it.each([
    ["incomplete", "Incomplete — complete every required field before connecting."],
    ["ready_for_authorization", "Ready for authorization."],
    ["verified", "Verified and ready for authorization."],
    ["needs_attention", "Needs attention — review the fields or replace the stored secret, then save."],
  ] as const)("renders closed configuration status guidance for %s", async (status, guidance) => {
    mockedConfigurations.mockResolvedValue([{ ...primary, status }]);
    renderSetup();
    expect((await screen.findAllByText(guidance))[0]).toBeVisible();
  });

  it("names the workflow steps a configuration delete would leave without a connection", async () => {
    const user = userEvent.setup();
    mockedDeletePreview.mockResolvedValue({
      configuration_id: primary.configuration_id,
      expected_revision: 3,
      changed_fields: ["__delete__"],
      change_policies: ["reauthorization_required"],
      connections: [{ connection_id: "connection-a", display_name: "Mailbox" }],
      requires_confirmation: true,
      confirmation_token: "delete-confirm",
      workflow_steps: [
        {
          workflow_id: "invoices",
          workflow_name: "Invoices",
          group_name: "Fetch",
          step_name: "Search mailbox",
          connection_id: "connection-a",
        },
      ],
    });
    renderSetup();

    await user.click(await screen.findByRole("button", { name: "Delete configuration" }));
    const deleteDialog = await screen.findByRole("alertdialog", {
      name: "Delete this configuration?",
    });
    expect(deleteDialog).toHaveTextContent("Invoices / Fetch / Search mailbox");
    expect(deleteDialog).toHaveAccessibleDescription(
      "Mailbox and its local credentials will also be removed. This workflow step uses them and will stop working until you choose another connection in the step.",
    );
  });

  it("previews referenced configuration deletion and requires explicit cascade confirmation", async () => {
    const user = userEvent.setup();
    mockedDeletePreview.mockResolvedValue({
      configuration_id: primary.configuration_id,
      expected_revision: 3,
      changed_fields: ["__delete__"],
      change_policies: ["reauthorization_required"],
      connections: [{ connection_id: "connection-a", display_name: "Mailbox" }],
      requires_confirmation: true,
      confirmation_token: "delete-confirm",
    });
    renderSetup();

    const deleteTrigger = await screen.findByRole("button", { name: "Delete configuration" });
    await user.click(deleteTrigger);
    const deleteDialog = await screen.findByRole("alertdialog", {
      name: "Delete this configuration?",
    });
    expect(deleteDialog).toHaveTextContent("Mailbox");
    expect(deleteDialog).toHaveTextContent("This cannot be undone.");
    expect(deleteDialog).toHaveAccessibleDescription(
      "Mailbox and its local credentials will also be removed.",
    );
    await waitFor(() => expect(
      within(deleteDialog).getByRole("button", { name: "Close Delete this configuration?" }),
    ).toHaveFocus());
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog", { name: "Delete this configuration?" }))
      .not.toBeInTheDocument();
    await waitFor(() => expect(deleteTrigger).toHaveFocus());
    expect(mockedDelete).not.toHaveBeenCalled();
    await user.click(deleteTrigger);
    await screen.findByRole("alertdialog", { name: "Delete this configuration?" });
    await user.click(screen.getByRole("button", { name: "Delete configuration and connections" }));

    expect(mockedDelete).toHaveBeenCalledWith(
      expect.anything(),
      primary.configuration_id,
      3,
      "delete-confirm",
    );
  });

  it.each([undefined, "delete-token"])("binds delete confirmation to the previewed configuration (token %s)", async (confirmationToken) => {
    const user = userEvent.setup();
    const secondary = {
      ...primary,
      configuration_id: "22222222-2222-4222-8222-222222222222",
      display_name: "Secondary",
      revision: 9,
    };
    mockedConfigurations.mockResolvedValue([primary, secondary]);
    mockedDeletePreview.mockResolvedValue({
      configuration_id: primary.configuration_id,
      expected_revision: primary.revision,
      changed_fields: ["__delete__"],
      change_policies: [],
      connections: [],
      requires_confirmation: Boolean(confirmationToken),
      confirmation_token: confirmationToken,
    });
    renderSetup();

    await user.click(await screen.findByRole("button", { name: "Delete configuration" }));
    expect(await screen.findByRole("alertdialog")).toBeVisible();
    await user.click(screen.getByRole("combobox", { name: "OAuth configuration" }));
    await user.click(await screen.findByRole("option", { name: secondary.display_name }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(mockedDelete).not.toHaveBeenCalled();

    await user.click(screen.getByRole("combobox", { name: "OAuth configuration" }));
    await user.click(await screen.findByRole("option", { name: primary.display_name }));
    await user.click(screen.getByRole("button", { name: "Delete configuration" }));
    // Nothing else is removed, so the confirming action names only the configuration.
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete configuration" }));
    expect(mockedDelete).toHaveBeenCalledWith(
      expect.anything(),
      primary.configuration_id,
      primary.revision,
      confirmationToken,
    );
  });

  it("shows safe status, redirect failure, and declared provider help navigation", async () => {
    const user = userEvent.setup();
    const onNavigatePage = vi.fn();
    mockedRedirect.mockRejectedValue(new Error("raw redirect failure token=secret"));
    mockedConfigurations.mockResolvedValue([{ ...primary, status: "needs_attention" }]);
    renderSetup({
      onNavigatePage,
      providerDeclaration: {
        ...provider,
        error_help_anchors: { provider_unavailable: "provider-status" },
      },
    });

    expect((await screen.findAllByText(/Needs attention — review the fields/))[0]).toBeVisible();
    expect(await screen.findByText("Redirect URI could not be registered. Try again."))
      .toHaveAttribute("role", "alert");
    expect(screen.getByRole("button", { name: "Retry redirect registration" })).toBeVisible();
    expect(screen.queryByText(/token=secret/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open provider help" }));
    expect(onNavigatePage).toHaveBeenCalledWith("oauth_help");
  });

  it("explains runtime revalidation without rendering transport details", async () => {
    const user = userEvent.setup();
    const saved = { ...primary, revision: primary.revision + 1 };
    mockedSave.mockResolvedValue(saved);
    mockedConfigurations.mockResolvedValueOnce([primary]).mockResolvedValue([saved]);
    mockedPreview.mockResolvedValue({
      configuration_id: primary.configuration_id,
      expected_revision: primary.revision,
      changed_fields: ["client_secret"],
      change_policies: ["runtime_revalidate"],
      connections: [],
      requires_confirmation: false,
    });
    renderSetup();
    await screen.findByText("Stored value will be kept.");
    await user.click(screen.getByRole("button", { name: "Replace" }));
    await user.type(screen.getByLabelText(/Application secret/), "replacement-secret");
    await user.click(screen.getByRole("button", { name: "Save configuration" }));
    expect(await screen.findByText("Configuration saved. Runtime credentials will be revalidated on the next use.")).toHaveAttribute("role", "status");
    await waitFor(() => expect(mockedConfigurations).toHaveBeenCalledTimes(2));
    expect(screen.getByText("Configuration saved. Runtime credentials will be revalidated on the next use.")).toBeVisible();
    await user.clear(screen.getByLabelText(/Configuration name/));
    await user.type(screen.getByLabelText(/Configuration name/), "Edited after save");
    expect(screen.queryByText("Configuration saved. Runtime credentials will be revalidated on the next use.")).not.toBeInTheDocument();
  });

  it("keeps start and reconnect failures provider-neutral and secret-safe", async () => {
    const user = userEvent.setup();
    mockedStart.mockRejectedValue(new Error("raw invalid_client token=secret"));
    renderSetup({
      mode: "connections",
      connections: [{
        connection_id: "connection-a",
        display_name: "Mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: "authorization_required" } },
      }],
    });
    await screen.findByRole("form", { name: "Connect an account" });
    await user.type(await screen.findByLabelText(/Connection name/), "New mailbox");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByText("Authorization could not be started. Check the configuration and try again.")).toHaveAttribute("role", "alert");
    await user.click(screen.getByRole("button", { name: "Reconnect Mailbox" }));
    expect(await screen.findByText("Reauthorization could not be started. Check the configuration and try again.")).toHaveAttribute("role", "alert");
    expect(screen.queryByText(/token=secret/)).not.toBeInTheDocument();
  });

  it.each([
    ["oauth_reconnect_subject_unavailable", 412],
    ["oauth_reconnect_subject_mismatch", 400],
  ] as const)("keeps one-click reconnect actionable for %s", async (error, status) => {
    const user = userEvent.setup();
    mockedStart.mockRejectedValueOnce(new HttpError({
      payload: {
        error,
        field: "tenant_domain",
        subject: "must-not-render.identity.example.test",
      },
      status,
      url: "/api/test",
    }));
    renderSetup({
      mode: "connections",
      connections: [{
        connection_id: "connection-a",
        display_name: "Mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: "disconnected" } },
      }],
    });

    await user.click(await screen.findByRole("button", { name: "Reconnect Mailbox" }));

    expect(mockedStart).toHaveBeenCalledWith(
      expect.anything(),
      {
        configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        intent_id: "connect",
        display_name: "Mailbox",
        return_page_id: "oauth_setup",
        intent_inputs: {},
        existing_connection_id: "connection-a",
      },
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This connection cannot be safely reauthorized. Create a new connection for this account instead.",
    );
    expect(screen.queryByText(/must-not-render|tenant_domain/)).not.toBeInTheDocument();
    expect(onAuthorization).not.toHaveBeenCalled();
  });

  it.each([
    "authorization_required",
    "reconnect_required",
  ] as const)("offers reconnect for %s grants using the same exact configuration", async (lifecycle) => {
    const user = userEvent.setup();
    renderSetup({
      mode: "connections",
      connections: [{
        connection_id: "connection-a",
        display_name: "Mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: lifecycle } },
      }],
    });

    await user.click(await screen.findByRole("button", { name: "Reconnect Mailbox" }));
    expect(mockedStart).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        intent_id: "connect",
        display_name: "Mailbox",
        existing_connection_id: "connection-a",
        return_page_id: "oauth_setup",
      }),
    );
    expect(onAuthorization).toHaveBeenCalled();
  });

  it("offers reconnect for a disconnected grant without offering disconnect again", async () => {
    const user = userEvent.setup();
    renderSetup({
      mode: "connections",
      connections: [{
        connection_id: "connection-a",
        display_name: "Mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: "disconnected" } },
      }],
    });

    await user.click(await screen.findByRole("button", { name: "Reconnect Mailbox" }));

    expect(mockedStart).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ existing_connection_id: "connection-a" }),
    );
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
  });

  it("focuses and highlights only the exact scoped callback connection", async () => {
    renderSetup({
      mode: "connections",
      focusConnectionId: "connection:a-b",
      connections: [
        {
          connection_id: "connection-a-b",
          display_name: "Collision candidate",
          extension_install_id: "install-a",
          oauth_provider_id: "example",
          oauth_configuration_id: primary.configuration_id,
          connection_type: "example_mail",
          connection_config: { oauth: { lifecycle_status: "connected" } },
        },
        {
          connection_id: "connection:a-b",
          display_name: "Callback mailbox",
          extension_install_id: "install-a",
          oauth_provider_id: "example",
          oauth_configuration_id: primary.configuration_id,
          connection_type: "example_mail",
          connection_config: { oauth: { lifecycle_status: "connected" } },
        },
      ],
    });

    const target = await screen.findByLabelText("Connection Callback mailbox");
    await waitFor(() => expect(target).toHaveFocus());
    expect(target).toHaveClass("is-focused");
    expect(screen.getByLabelText("Connection Collision candidate")).not.toHaveClass("is-focused");
  });

  it.each([false, true])("focuses the callback connection after overlapping page requests release the loader (StrictMode: %s)", async (strictMode) => {
    render(<button type="button">Page action</button>);
    screen.getByRole("button", { name: "Page action" }).focus();
    const focusBoundary = nativeLoaderFocusBoundary();
    const finishRequest = beginRequest();
    try {
      render(<StrictMode><RequestLoadingIndicator /></StrictMode>);
      renderSetup({
        strictMode,
        mode: "connections",
        focusConnectionId: "connection-a",
        connections: [{
          connection_id: "connection-a",
          display_name: "Preserved mailbox",
          extension_install_id: "install-a",
          oauth_provider_id: "example",
          oauth_configuration_id: primary.configuration_id,
          connection_type: "example_mail",
          connection_config: { oauth: { lifecycle_status: "connected" } },
        }],
      });

      const target = await screen.findByLabelText("Connection Preserved mailbox");
      expect(target).toHaveClass("is-focused");
      expect(screen.getByRole("dialog", { name: "Loading requests" })).toHaveFocus();
      await act(async () => { finishRequest(); });

      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      expect(target).toHaveFocus();
    } finally {
      await act(async () => { finishRequest(); });
      focusBoundary.mockRestore();
    }
  });

  it("keeps a newly opened modal focused instead of the deferred callback connection", async () => {
    const focusBoundary = nativeLoaderFocusBoundary();
    const finishRequest = beginRequest();
    try {
      render(<RequestLoadingIndicator />);
      renderSetup({
        mode: "connections",
        focusConnectionId: "connection-a",
        connections: [{
          connection_id: "connection-a",
          display_name: "Preserved mailbox",
          extension_install_id: "install-a",
          oauth_provider_id: "example",
          oauth_configuration_id: primary.configuration_id,
          connection_type: "example_mail",
          connection_config: { oauth: { lifecycle_status: "connected" } },
        }],
      });
      await screen.findByLabelText("Connection Preserved mailbox");
      render(<DsDialog open onClose={() => {}} title="Action plan" description="Plan details" eyebrow="Plan">Plan</DsDialog>);

      await act(async () => { finishRequest(); });

      const plan = screen.getByRole("dialog", { name: "Action plan" });
      expect(within(plan).getByRole("button", { name: "Close Action plan" })).toHaveFocus();
      expect(screen.getByLabelText("Connection Preserved mailbox")).not.toHaveFocus();
    } finally {
      await act(async () => { finishRequest(); });
      focusBoundary.mockRestore();
    }
  });

  it("cancels callback focus when its connection page unmounts during loading", async () => {
    const focusBoundary = nativeLoaderFocusBoundary();
    const finishRequest = beginRequest();
    try {
      render(<RequestLoadingIndicator />);
      const view = renderSetup({
        mode: "connections",
        focusConnectionId: "connection-a",
        connections: [{
          connection_id: "connection-a",
          display_name: "Preserved mailbox",
          extension_install_id: "install-a",
          oauth_provider_id: "example",
          oauth_configuration_id: primary.configuration_id,
          connection_type: "example_mail",
          connection_config: { oauth: { lifecycle_status: "connected" } },
        }],
      });
      const target = await screen.findByLabelText("Connection Preserved mailbox");
      focusBoundary.mockClear();
      view.unmount();

      await act(async () => { finishRequest(); });

      expect(target.isConnected).toBe(false);
      expect(focusBoundary.mock.contexts).not.toContain(target);
    } finally {
      await act(async () => { finishRequest(); });
      focusBoundary.mockRestore();
    }
  });

  it.each([undefined, "retired_intent"] as const)("uses the sole declared page-owned intent to reconnect a legacy row with stored intent %s", async (storedIntentId) => {
    const user = userEvent.setup();
    renderSetup({
      mode: "connections",
      allowedIntentIds: ["connect"],
      connectionOwners: [
        { intentId: "connect", pageId: "oauth_setup" },
        { intentId: "connect", pageId: "oauth_setup" },
      ],
      connections: [{
        connection_id: "connection-a",
        display_name: "Mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: {
            lifecycle_status: "authorization_required",
            ...(storedIntentId ? { authorization_intent_id: storedIntentId } : {}),
          },
        },
      }],
    });

    await user.click(await screen.findByRole("button", { name: "Reconnect Mailbox" }));
    expect(mockedStart).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        intent_id: "connect",
        existing_connection_id: "connection-a",
      }),
    );
  });

  it.each([undefined, "retired_intent"] as const)("fails closed on every shared-type page for an ambiguous legacy row with stored intent %s", async (storedIntentId) => {
    const providerWithTwoIntents = {
      ...provider,
      authorization_intents: [
        ...provider.authorization_intents,
        { intent_id: "expanded_access", user_inputs: [] },
      ],
    };
    const connection = {
      connection_id: "connection-ambiguous-legacy",
      display_name: "Ambiguous legacy mailbox",
      extension_install_id: "install-a",
      oauth_provider_id: "example",
      oauth_configuration_id: primary.configuration_id,
      connection_type: "example_mail",
      connection_config: {
        oauth: {
          lifecycle_status: "authorization_required",
          ...(storedIntentId ? { authorization_intent_id: storedIntentId } : {}),
        },
      },
    };
    const { unmount } = renderSetup({
      mode: "connections",
      allowedIntentIds: ["connect"],
      connectionOwners: [
        { intentId: "connect", pageId: "oauth_setup" },
        { intentId: "expanded_access", pageId: "expanded_page" },
      ],
      providerDeclaration: providerWithTwoIntents,
      connections: [connection],
    });
    await screen.findByRole("form", { name: "Connect an account" });
    expect(screen.queryByText("Ambiguous legacy mailbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reconnect Ambiguous legacy mailbox" })).not.toBeInTheDocument();
    unmount();

    renderSetup({
      mode: "connections",
      allowedIntentIds: ["expanded_access"],
      connectionOwners: [
        { intentId: "connect", pageId: "oauth_setup" },
        { intentId: "expanded_access", pageId: "expanded_page" },
      ],
      providerDeclaration: providerWithTwoIntents,
      connections: [connection],
    });
    await screen.findByRole("form", { name: "Connect an account" });
    expect(screen.queryByText("Ambiguous legacy mailbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reconnect Ambiguous legacy mailbox" })).not.toBeInTheDocument();
    expect(mockedStart).not.toHaveBeenCalled();
  });

  it("renders and reconnects a declared-intent row only on its owning page", async () => {
    const user = userEvent.setup();
    const providerWithTwoIntents = {
      ...provider,
      authorization_intents: [
        ...provider.authorization_intents,
        { intent_id: "expanded_access", user_inputs: [] },
      ],
    };
    const connection = {
      connection_id: "connection-shared-type",
      display_name: "Shared type mailbox",
      extension_install_id: "install-a",
      oauth_provider_id: "example",
      oauth_configuration_id: primary.configuration_id,
      connection_type: "example_mail",
      connection_config: {
        oauth: {
          lifecycle_status: "authorization_required",
          authorization_intent_id: "expanded_access",
        },
      },
    };
    const { unmount } = renderSetup({
      mode: "connections",
      allowedIntentIds: ["connect"],
      providerDeclaration: providerWithTwoIntents,
      connections: [connection],
    });
    await screen.findByRole("form", { name: "Connect an account" });
    expect(screen.queryByText("Shared type mailbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reconnect Shared type mailbox" })).not.toBeInTheDocument();
    unmount();

    renderSetup({
      mode: "connections",
      allowedIntentIds: ["expanded_access"],
      providerDeclaration: providerWithTwoIntents,
      connections: [connection],
    });
    await user.click(await screen.findByRole("button", { name: "Reconnect Shared type mailbox" }));

    expect(mockedStart).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        intent_id: "expanded_access",
        existing_connection_id: "connection-shared-type",
      }),
    );
  });

  it("does not expose reconnect when the page has no exact allowed intent", async () => {
    renderSetup({
      mode: "connections",
      allowedIntentIds: [],
      connections: [{
        connection_id: "connection-a",
        display_name: "Mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: { oauth: { lifecycle_status: "authorization_required" } },
      }],
    });

    await screen.findByRole("heading", { name: "Example Identity" });
    expect(screen.queryByRole("form", { name: "Connect an account" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reconnect Mailbox" })).not.toBeInTheDocument();
    expect(mockedStart).not.toHaveBeenCalled();
  });

  it("retries and disconnects an owned connection through exact generic endpoints", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(true);
    const connection = {
      connection_id: "connection-a",
      display_name: "Mailbox",
      extension_install_id: "install-a",
      oauth_provider_id: "example",
      oauth_configuration_id: primary.configuration_id,
      connection_type: "example_mail",
      connection_config: {
        oauth: {
          lifecycle_status: "needs_attention",
          last_error_code: "provider_unavailable",
        },
      },
    };
    renderSetup({ connections: [connection], mode: "connections" });

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(mockedRetry).toHaveBeenCalledWith(expect.anything(), "connection-a", {
      configuration_id: primary.configuration_id,
      connection_type: "example_mail",
    });
    await user.click(screen.getByRole("button", { name: "Disconnect" }));
    // Reversible but costly: the row turns into its Inline confirm, No focused.
    const strip = screen.getByRole("group", { name: /^Disconnect Mailbox\?/ });
    expect(within(strip).getByRole("button", { name: "No" })).toHaveFocus();
    expect(mockedDisconnect).not.toHaveBeenCalled();
    await user.click(within(strip).getByRole("button", { name: "Disconnect account" }));
    expect(mockedDisconnect).toHaveBeenCalledWith(expect.anything(), primary.configuration_id, "connection-a");
    expect(confirm).not.toHaveBeenCalled();

    confirm.mockRestore();
  });

  it("shows retry progress and explains a repeated needs-attention outcome", async () => {
    const user = userEvent.setup();
    const onConnectionsChanged = vi.fn();
    const onNavigatePage = vi.fn();
    let resolveRetry: ((value: OAuthPostConnectRetryResult) => void) | undefined;
    mockedRetry.mockImplementation(() => new Promise((resolve) => {
      resolveRetry = resolve;
    }));
    renderSetup({
      mode: "connections",
      onConnectionsChanged,
      onNavigatePage,
      connections: [{
        connection_id: "connection-a",
        display_name: "Mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: {
            lifecycle_status: "needs_attention",
            last_error_code: "provider_unavailable",
          },
        },
      }],
    });

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    const pending = screen.getByRole("button", { name: "Retrying…" });
    expect(pending).toBeDisabled();
    expect(pending).toHaveAttribute("aria-busy", "true");

    const retryResponse: OAuthPostConnectRetryResult & { detail: string } = {
      outcome: "needs_attention",
      connection_id: "connection-a",
      help_target: { page_id: "oauth_help", anchor: "provider-status" },
      detail: "raw-token=must-not-render",
    };
    await act(async () => resolveRetry?.(retryResponse));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Connection verification still needs attention. Review the provider troubleshooting guidance and try again.",
    );
    expect(screen.queryByText(/raw-token=must-not-render/u)).not.toBeInTheDocument();
    expect(onConnectionsChanged).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Open retry help" }));
    expect(onNavigatePage).toHaveBeenCalledWith("oauth_help", "provider-status");
  });

  it("renders safe retry and disconnect failures without raw provider details", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(true);
    mockedRetry.mockRejectedValue(new Error("raw retry token=secret"));
    mockedDisconnect.mockRejectedValue(new Error("raw disconnect token=secret"));
    renderSetup({ mode: "connections", connections: [{
      connection_id: "connection-a",
      display_name: "Mailbox",
      extension_install_id: "install-a",
      oauth_provider_id: "example",
      oauth_configuration_id: primary.configuration_id,
      connection_type: "example_mail",
      connection_config: {
        oauth: {
          lifecycle_status: "needs_attention",
          last_error_code: "provider_unavailable",
        },
      },
    }] });

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Connection verification could not be retried. Try again.")).toHaveAttribute("role", "alert");
    await user.click(screen.getByRole("button", { name: "Disconnect" }));
    await user.click(screen.getByRole("button", { name: "Disconnect account" }));
    expect(await screen.findByText("Connection could not be disconnected. Try again.")).toHaveAttribute("role", "alert");
    expect(screen.queryByText(/token=secret/)).not.toBeInTheDocument();
    confirm.mockRestore();
  });

  it("refreshes instead of offering a retry when the connection is already gone", async () => {
    const user = userEvent.setup();
    const onConnectionsChanged = vi.fn();
    mockedDisconnect.mockRejectedValue(new HttpError({
      payload: { detail: "Connection not found" },
      status: 404,
      url: "/api/test",
    }));
    renderSetup({ mode: "connections", onConnectionsChanged, connections: [{
      connection_id: "connection-a",
      display_name: "Mailbox",
      extension_install_id: "install-a",
      oauth_provider_id: "example",
      oauth_configuration_id: primary.configuration_id,
      connection_type: "example_mail",
      connection_config: { oauth: { lifecycle_status: "connected" } },
    }] });

    await user.click(await screen.findByRole("button", { name: "Disconnect" }));
    await user.click(screen.getByRole("button", { name: "Disconnect account" }));

    expect(await screen.findByText("This connection no longer exists. The list was refreshed.")).toBeVisible();
    expect(onConnectionsChanged).toHaveBeenCalled();
    expect(screen.queryByText("Connection could not be disconnected. Try again.")).not.toBeInTheDocument();
  });

  it("routes invalid-client attention to Setup without offering an impossible retry", async () => {
    renderSetup({
      mode: "connections",
      connections: [{
        connection_id: "connection-a",
        display_name: "Mailbox",
        extension_install_id: "install-a",
        oauth_provider_id: "example",
        oauth_configuration_id: primary.configuration_id,
        connection_type: "example_mail",
        connection_config: {
          oauth: {
            lifecycle_status: "needs_attention",
            last_error_code: "invalid_client",
          },
        },
      }],
    });

    expect(await screen.findByText(
      "Repair and save this connection's configuration in Setup before trying again.",
    )).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(mockedRetry).not.toHaveBeenCalled();
  });

  it("renders a closed safe fallback instead of provider or transport error text", async () => {
    const user = userEvent.setup();
    mockedPreview.mockRejectedValue(new Error("raw invalid_client secret=must-not-render"));
    renderSetup();
    await user.clear(await screen.findByLabelText(/Configuration name/));
    await user.type(screen.getByLabelText(/Configuration name/), "Changed");
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Configuration could not be saved. Check the fields and try again.");
    expect(screen.queryByText(/must-not-render/)).not.toBeInTheDocument();
  });
});
