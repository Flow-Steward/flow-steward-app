import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { HttpError } from "@/lib/http/errors";
import { LlmProviderPanel } from "@/pages/extensions/llm-provider-panel";
import {
  fetchLlmProviderSources,
  fetchLlmProviderStatus,
  refreshLlmProviderModels,
  saveLlmProviderCredentials,
  setLlmProviderModelEnabled,
} from "@/pages/extensions/api";

vi.mock("@/pages/extensions/api", () => ({
  fetchLlmProviderSources: vi.fn(),
  fetchLlmProviderStatus: vi.fn(),
  refreshLlmProviderModels: vi.fn(),
  saveLlmProviderCredentials: vi.fn(),
  setLlmProviderModelEnabled: vi.fn(),
}));

const mockedFetchSources = vi.mocked(fetchLlmProviderSources);
const mockedFetchStatus = vi.mocked(fetchLlmProviderStatus);
const mockedRefreshModels = vi.mocked(refreshLlmProviderModels);
const mockedSaveCredentials = vi.mocked(saveLlmProviderCredentials);
const mockedSetModelEnabled = vi.mocked(setLlmProviderModelEnabled);

function renderPanel(
  contract: Record<string, unknown> = { required_provider_secrets: ["api_key"] },
  initialConnection?: {
    initialConnectionConfig?: Record<string, unknown>;
    initialRemoteBaseUrl?: string;
    runtimeMode?: string;
    documentationUrl?: string;
    installEnabled?: boolean;
    onConnectionSaved?: () => Promise<void> | void;
  },
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function panel(installEnabled: boolean) {
    return (
      <QueryClientProvider client={queryClient}>
        <LlmProviderPanel
          {...{ installEnabled }}
          contract={contract}
          initialConnectionConfig={initialConnection?.initialConnectionConfig}
          initialRemoteBaseUrl={initialConnection?.initialRemoteBaseUrl}
          documentationUrl={initialConnection?.documentationUrl}
          installId="install-1"
          onConnectionSaved={initialConnection?.onConnectionSaved}
          runtimeMode={initialConnection?.runtimeMode}
        />
      </QueryClientProvider>
    );
  }
  const view = render(panel(initialConnection?.installEnabled ?? true));
  return { ...view, queryClient, rerenderEnabled: (enabled: boolean) => view.rerender(panel(enabled)) };
}

describe("LlmProviderPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [
          {
            id: "model-row-1",
            model_id: "owner/model",
            enabled: false,
            verification_status: "unverified",
          },
        ],
      },
    ] as never);
    mockedRefreshModels.mockResolvedValue({} as never);
    mockedFetchStatus.mockResolvedValue({ install_id: "install-1", saved_secrets: [], last_check: null });
    mockedSaveCredentials.mockResolvedValue({} as never);
    mockedSetModelEnabled.mockResolvedValue({} as never);
  });

  it("blocks disabled discovery and every verification action while preserving Remove and historical status", async () => {
    mockedFetchSources.mockResolvedValue([{
      id: "source-1", extension_install_id: "install-1",
      models: [
        { id: "new", model_id: "owner/new", enabled: false, verification_status: "unverified" },
        { id: "stale", model_id: "owner/stale", enabled: true, verification_status: "stale" },
        { id: "failed", model_id: "owner/failed", enabled: false, verification_status: "failed" },
        { id: "added", model_id: "owner/added", enabled: true, verification_status: "passed" },
      ],
    }] as never);
    mockedFetchStatus.mockResolvedValue({
      install_id: "install-1", saved_secrets: [{ name: "api_key" }],
      last_check: { status: "ok", checked_at: "2026-10-24T08:00:00Z" },
    });
    const user = userEvent.setup();
    renderPanel(undefined, { installEnabled: false });
    await screen.findByText("Connected");
    expect(screen.getByText("Enable this extension before checking the connection.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Discover models" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save connection" })).toBeEnabled();
    for (const name of ["Add owner/new", "Reverify owner/stale", "Try verifying owner/failed again"]) {
      const action = screen.getByRole("button", { name });
      expect(action).toHaveAttribute("aria-disabled", "true");
      expect(action).toHaveAccessibleDescription("Enable this extension before checking the connection.");
      fireEvent.click(action);
    }
    fireEvent.click(screen.getByRole("button", { name: "Discover models" }));
    expect(mockedRefreshModels).not.toHaveBeenCalled();
    expect(mockedSetModelEnabled).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Remove owner/added" }));
    await screen.findByText("owner/added removed.");
    expect(mockedSetModelEnabled).toHaveBeenCalledExactlyOnceWith("added", false);
  });

  it("saves disabled remote setup without checking and asks the operator to Enable", async () => {
    const user = userEvent.setup();
    renderPanel({ required_provider_secrets: ["api_key"], required_connection_config: ["upstream_base_url"] }, {
      installEnabled: false, runtimeMode: "remote_https",
    });
    await screen.findByText("owner/model");
    expect(screen.getByRole("button", { name: "Save connection" })).toBeDisabled();
    await user.type(screen.getByLabelText("Runtime endpoint URL"), "https://runtime.example.com");
    await user.type(screen.getByLabelText("Base URL"), "https://upstream.example.com");
    await user.type(screen.getByLabelText("API key"), "test-credential"); // pragma: allowlist secret - test sentinel
    await user.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Settings saved without checking the connection."));
    expect(screen.getByRole("status")).toHaveTextContent("Enable this extension before checking the connection.");
    expect(mockedSaveCredentials).toHaveBeenCalledExactlyOnceWith("install-1", {
      secrets: { api_key: "test-credential" }, // pragma: allowlist secret - test sentinel
      connection_config: { upstream_base_url: "https://upstream.example.com" },
      remote_base_url: "https://runtime.example.com",
    });
    expect(mockedRefreshModels).not.toHaveBeenCalled();
    expect(mockedSetModelEnabled).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Save connection" })).toBeEnabled();
  });

  it("preserves unsaved settings and search across same-install Enable and Disable", async () => {
    const user = userEvent.setup();
    const view = renderPanel({ optional_provider_secrets: ["api_key"], optional_connection_config: ["upstream_base_url"] }, {
      installEnabled: false, runtimeMode: "remote_https",
    });
    await screen.findByText("owner/model");
    await user.type(screen.getByLabelText("API key (optional)"), "unsaved-value"); // pragma: allowlist secret - test sentinel
    await user.type(screen.getByLabelText("Base URL (optional)"), "https://unsaved.example.com");
    await user.type(screen.getByLabelText("Runtime endpoint URL"), "https://runtime.example.com");
    await user.type(screen.getByRole("searchbox"), "owner");
    view.rerenderEnabled(true);
    await user.click(screen.getByRole("button", { name: "Discover models" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Discover models" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Add owner/model" }));
    await screen.findByText("owner/model added: it passed the checks.");
    view.rerenderEnabled(false);
    expect(screen.getByRole("button", { name: "Discover models" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add owner/model" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByLabelText("API key (optional)")).toHaveValue("unsaved-value");
    expect(screen.getByLabelText("Base URL (optional)")).toHaveValue("https://unsaved.example.com");
    expect(screen.getByLabelText("Runtime endpoint URL")).toHaveValue("https://runtime.example.com");
    expect(screen.getByRole("searchbox")).toHaveValue("owner");
    expect(mockedRefreshModels).toHaveBeenCalledTimes(1);
    expect(mockedSetModelEnabled).toHaveBeenCalledExactlyOnceWith("model-row-1", true);
  });

  it.each([true, false])("keeps Save save-only when availability changes from %s during persistence", async (initialEnabled) => {
    let completeSave!: () => void;
    mockedSaveCredentials.mockImplementationOnce(() => new Promise((resolve) => {
      completeSave = () => resolve({ changed: true } as never);
    }));
    const user = userEvent.setup();
    const view = renderPanel(undefined, { installEnabled: initialEnabled });
    await screen.findByText("owner/model");
    await user.click(screen.getByRole("button", { name: "Save connection" }));
    view.rerenderEnabled(!initialEnabled);
    await act(async () => { completeSave(); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save connection" })).toBeEnabled());
    expect(screen.getByRole("status")).toHaveTextContent("Settings saved without checking the connection.");
    expect(mockedSaveCredentials).toHaveBeenCalledTimes(1);
    expect(mockedRefreshModels).not.toHaveBeenCalled();
  });

  it("rechecks latest availability after the saved-connection callback finishes", async () => {
    let finishCallback!: () => void;
    const onConnectionSaved = vi.fn(() => new Promise<void>((resolve) => { finishCallback = resolve; }));
    const user = userEvent.setup();
    const view = renderPanel(undefined, { onConnectionSaved });
    await screen.findByText("owner/model");
    await user.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(onConnectionSaved).toHaveBeenCalledTimes(1));
    view.rerenderEnabled(false);
    await act(async () => { finishCallback(); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save connection" })).toBeEnabled());
    expect(mockedRefreshModels).not.toHaveBeenCalled();
  });

  it("keeps host catalog retry available while the install is disabled", async () => {
    mockedFetchSources.mockRejectedValueOnce(new Error("host catalog unavailable"));
    const user = userEvent.setup();
    renderPanel(undefined, { installEnabled: false });
    await screen.findByText("Could not load provider models.");
    await user.click(screen.getByRole("button", { name: "Retry loading models" }));
    await screen.findByText("owner/model");
    expect(mockedFetchSources).toHaveBeenCalledTimes(2);
    expect(mockedRefreshModels).not.toHaveBeenCalled();
    expect(mockedSetModelEnabled).not.toHaveBeenCalled();
  });

  it("keeps a spaced optional secret name isolated from help and saved status and preserves its raw save key", async () => {
    const user = userEvent.setup();
    mockedFetchStatus.mockResolvedValue({
      install_id: "install-1",
      saved_secrets: [{ name: "cloud api key" }],
      last_check: null,
    });
    renderPanel({
      optional_provider_secrets: ["cloud api key"],
      provider_secret_help: { "cloud api key": "Paste the credential supplied by your cloud administrator." },
    }, { runtimeMode: "subprocess" });
    await screen.findByText("Cloud api key saved");
    expect(screen.getByText("Paste the credential supplied by your cloud administrator.")).toBeInTheDocument();
    const secret = screen.getByPlaceholderText("Saved. Enter a new cloud api key to replace it");

    expect(secret).toHaveAccessibleName("Cloud api key (optional)");
    expect(secret).toHaveAttribute("type", "password");
    expect(secret).toHaveValue("");
    await user.type(secret, "replacement-value"); // pragma: allowlist secret - test sentinel
    await user.click(screen.getByRole("button", { name: "Save connection" }));
    expect(mockedSaveCredentials).toHaveBeenCalledWith("install-1", {
      secrets: { "cloud api key": "replacement-value" }, // pragma: allowlist secret - test sentinel
      connection_config: {},
    });
  });

  it("refetches provider state after saving credentials", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText("owner/model");

    await user.type(screen.getByLabelText("API key"), "secret");
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(mockedFetchSources).toHaveBeenCalledTimes(2));
  });

  it("distinguishes source loading, load failure, and an empty catalog", async () => {
    let rejectLoad: ((error: Error) => void) | undefined;
    mockedFetchSources.mockImplementationOnce(
      () => new Promise((_resolve, reject) => {
        rejectLoad = reject;
      }),
    );
    const user = userEvent.setup();
    renderPanel();

    expect(screen.getByText("Loading provider models…")).toBeInTheDocument();
    rejectLoad?.(new Error("source service unavailable"));
    expect(await screen.findByText("Could not load provider models.")).toBeInTheDocument();
    expect(screen.queryByText("No models discovered yet.")).not.toBeInTheDocument();

    mockedFetchSources.mockResolvedValueOnce([] as never);
    await user.click(screen.getByRole("button", { name: "Retry loading models" }));
    expect(await screen.findByText("No models discovered yet.")).toBeInTheDocument();
  });

  it("saves the host-to-runtime endpoint separately from upstream configuration", async () => {
    const user = userEvent.setup();
    renderPanel(
      {
        required_provider_secrets: ["api_key"],
        required_connection_config: ["upstream_base_url"],
      },
      { runtimeMode: "remote_https" },
    );
    await screen.findByText("owner/model");

    await user.type(screen.getByLabelText("Runtime endpoint URL"), "https://runtime.example.com");
    await user.type(screen.getByLabelText("Base URL"), "https://upstream.example.com");
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    expect(mockedSaveCredentials).toHaveBeenCalledWith("install-1", {
      secrets: {},
      connection_config: { upstream_base_url: "https://upstream.example.com" },
      remote_base_url: "https://runtime.example.com",
    });
  });

  it("marks manifest connection config as required and blocks incomplete actions", async () => {
    const user = userEvent.setup();
    renderPanel({ required_connection_config: ["upstream_base_url"] });
    await screen.findByText("owner/model");

    const upstreamInput = screen.getByLabelText("Base URL");
    expect(upstreamInput).toBeRequired();
    expect(screen.getByText("*", { selector: "span.fs-required" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save connection" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Discover models" })).toBeDisabled();

    await user.type(upstreamInput, "https://acme-llm.example.com");

    expect(screen.getByRole("button", { name: "Save connection" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Discover models" })).toBeEnabled();
  });

  it("restores saved runtime and upstream endpoints after reload", async () => {
    mockedFetchSources.mockResolvedValue([] as never);
    renderPanel(
      { required_connection_config: ["upstream_base_url"] },
      {
        initialConnectionConfig: {
          upstream_base_url: "https://upstream.saved.example.com",
        },
        initialRemoteBaseUrl: "https://runtime.saved.example.com",
        runtimeMode: "remote_https",
      },
    );

    await waitFor(() => {
      expect(screen.getByLabelText("Runtime endpoint URL")).toHaveValue(
        "https://runtime.saved.example.com",
      );
      expect(screen.getByLabelText("Base URL")).toHaveValue(
        "https://upstream.saved.example.com",
      );
    });
  });

  it("distinguishes an empty search result from an undiscovered catalog", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText("owner/model");

    await user.type(screen.getByLabelText("Search provider models"), "no-match");

    expect(screen.getByText("No models match this search.")).toBeInTheDocument();
    expect(screen.queryByText("No models discovered yet.")).not.toBeInTheDocument();
  });

  it("labels runtime and upstream credentials with a replaceable endpoint example", async () => {
    renderPanel(
      { required_provider_secrets: ["runtime_bearer_token", "api_key"] },
      { runtimeMode: "remote_https" },
    );

    await screen.findByText("owner/model");

    expect(screen.getByLabelText("Runtime endpoint URL")).toHaveAttribute(
      "placeholder",
      "https://llm-runtime.your-company.com",
    );
    expect(screen.getByText("Runtime bearer token")).toBeInTheDocument();
    expect(screen.getByText("API key")).toBeInTheDocument();
  });

  it("asks an in-process provider only for its own settings", async () => {
    const user = userEvent.setup();
    renderPanel(
      {
        // A stale manifest may still list the runtime bearer; it has no meaning
        // without a separately hosted runtime.
        optional_provider_secrets: ["api_key", "runtime_bearer_token"],
        optional_connection_config: ["upstream_base_url"],
      },
      { runtimeMode: "subprocess" },
    );
    await screen.findByText("owner/model");

    expect(screen.queryByLabelText("Runtime endpoint URL")).not.toBeInTheDocument();
    expect(screen.queryByText("Runtime bearer token")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Base URL (optional)")).not.toBeRequired();
    expect(screen.getByLabelText("Base URL (optional)")).toHaveAttribute(
      "placeholder",
      "Leave empty for the provider's default",
    );
    // No declared default, help or documentation: no helper text and no link.
    expect(screen.getByLabelText("Base URL (optional)")).not.toHaveAttribute("aria-describedby");
    expect(screen.getByLabelText("API key (optional)")).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByRole("link", { name: /How to fill this in/ })).not.toBeInTheDocument();
    // The optional note is part of each control name, including secret fields.
    expect(screen.getByLabelText("Base URL (optional)")).toBeInTheDocument();
    expect(screen.getByLabelText("API key (optional)")).toBeInTheDocument();
    // Nothing is required, so discovery is available straight away.
    expect(screen.getByRole("button", { name: "Discover models" })).toBeEnabled();

    await user.type(screen.getByLabelText("API key (optional)"), "provider-key"); // pragma: allowlist secret - test sentinel
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    expect(mockedSaveCredentials).toHaveBeenCalledWith("install-1", {
      secrets: { api_key: "provider-key" }, // pragma: allowlist secret - test sentinel
      connection_config: { upstream_base_url: "" },
    });
  });

  it("saves an emptied optional field so the provider default comes back", async () => {
    const user = userEvent.setup();
    mockedFetchSources.mockResolvedValue([] as never);
    renderPanel(
      { optional_connection_config: ["upstream_base_url"] },
      {
        initialConnectionConfig: { upstream_base_url: "http://ollama.lan:11434" },
        runtimeMode: "subprocess",
      },
    );
    const baseUrl = await screen.findByLabelText("Base URL (optional)");
    await waitFor(() => expect(baseUrl).toHaveValue("http://ollama.lan:11434"));

    await user.clear(baseUrl);
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    // No key typed: the saved key stays; the empty value clears the base URL.
    expect(mockedSaveCredentials).toHaveBeenCalledWith("install-1", {
      secrets: {},
      connection_config: { upstream_base_url: "" },
    });
  });

  it("hides models the provider says can't qualify until Show all models is on", async () => {
    const user = userEvent.setup();
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [
          { id: "m1", model_id: "owner/tools-and-json", verification_status: "unverified", missing_capabilities: [] },
          { id: "m2", model_id: "owner/text-only", verification_status: "unverified", missing_capabilities: ["structured_output", "tools"] },
          // Enabled despite a gap in what the provider reports: verification decided.
          { id: "m3", model_id: "owner/verified", enabled: true, verification_status: "passed", missing_capabilities: ["tools"] },
        ],
      },
    ] as never);
    renderPanel();

    expect(await screen.findByText("owner/tools-and-json")).toBeInTheDocument();
    expect(screen.getByText("owner/verified")).toBeInTheDocument();
    expect(screen.queryByText("owner/text-only")).not.toBeInTheDocument();

    await user.click(screen.getByText("Show all models (1 not supported)"));

    expect(screen.getByText("owner/text-only")).toBeInTheDocument();
    expect(screen.getByText("Not supported: no structured output, no tool calling")).toBeInTheDocument();
    // Still selectable: verification is the final authority.
    expect(screen.getByRole("button", { name: "Add owner/text-only" })).toBeEnabled();
  });

  it("explains an empty list when no model reports the required capabilities", async () => {
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [{ id: "m2", model_id: "owner/text-only", verification_status: "unverified", missing_capabilities: ["tools"] }],
      },
    ] as never);
    renderPanel();

    expect(
      await screen.findByText("No model reports tool calling and structured output. Show all models to try one anyway."),
    ).toBeInTheDocument();
  });

  it("marks a required provider secret as required", async () => {
    renderPanel({ required_provider_secrets: ["api_key"] }, { runtimeMode: "subprocess" });
    await screen.findByText("owner/model");

    expect(screen.getByText("*", { selector: "span.fs-required" })).toBeInTheDocument();
    expect(screen.queryByText(/\(optional\)/)).not.toBeInTheDocument();
  });

  it("refetches persisted failed verification state after enable rejects", async () => {
    const user = userEvent.setup();
    mockedSetModelEnabled.mockRejectedValue(new Error("Capability verification failed"));
    renderPanel();
    await screen.findByText("owner/model");

    await user.click(screen.getByRole("button", { name: "Add owner/model" }));

    expect(await screen.findByText("Capability verification failed")).toBeInTheDocument();
    await waitFor(() => expect(mockedFetchSources).toHaveBeenCalledTimes(2));
  });

  it("names the failed check on the row and offers Try again", async () => {
    const user = userEvent.setup();
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [
          {
            id: "model-row-1",
            model_id: "owner/model",
            enabled: false,
            verification_status: "failed",
            verification_error_safe: "capability_mismatch_tool_result",
          },
        ],
      },
    ] as never);
    renderPanel();

    expect(
      await screen.findByText("Failed the tool-result check: the model did not use the value the test tool returned."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try verifying owner/model again" }));

    expect(mockedSetModelEnabled).toHaveBeenCalledWith("model-row-1", true);
    expect(await screen.findByText("owner/model added: it passed the checks.")).toBeInTheDocument();
  });

  it("says a plan or credit refusal comes from the named provider, what to do, and when it was checked", async () => {
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [
          {
            id: "model-row-1",
            model_id: "kimi-k2.6",
            enabled: false,
            verification_status: "failed",
            verification_error_safe: "billing_quota_exceeded",
            verification_started_at: "2026-10-04T10:42:00Z",
          },
        ],
      },
    ] as never);
    renderPanel({ required_provider_secrets: ["api_key"], supplier_label: "Ollama" });

    expect(
      await screen.findByText(/^Ollama refused this model: your plan doesn't include it, or the account is out of credit\. Choose another model, or upgrade or top up your Ollama account\. \(checked .+\)$/),
    ).toBeInTheDocument();
    expect(screen.getByText("not available")).toBeInTheDocument();
    // An error, not a hint: the reason carries the error style.
    expect(screen.getByText(/^Ollama refused this model/)).toHaveClass("is-error");
    expect(screen.queryByText("failed")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try verifying kimi-k2.6 again" })).toBeInTheDocument();
  });

  it("says an extension update is why a model needs verifying again", async () => {
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [
          {
            id: "model-row-1",
            model_id: "owner/model",
            enabled: false,
            verification_status: "stale",
            verification_error_safe: "extension_release_or_descriptor_changed",
          },
        ],
      },
    ] as never);
    renderPanel();

    expect(
      await screen.findByText("The extension was updated. Verify this model again before using it."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reverify owner/model" })).toBeEnabled();
  });

  it("names the failed check right away when verification is refused", async () => {
    const user = userEvent.setup();
    mockedSetModelEnabled.mockRejectedValue(new Error("Capability verification failed: capability_mismatch_structured_output"));
    renderPanel();
    await screen.findByText("owner/model");

    await user.click(screen.getByRole("button", { name: "Add owner/model" }));

    expect(
      await screen.findByText("Failed the structured-output check: the answer did not match the required JSON schema."),
    ).toBeInTheDocument();
  });

  it("says so when a save changed nothing, then still checks the connection", async () => {
    // Seen on a stand: the same half key saved twice more, "unchanged", with no
    // hint. Now the panel says nothing changed and runs the check anyway.
    const user = userEvent.setup();
    mockedSaveCredentials.mockResolvedValue({ status: "saved", changed: false, verification_invalidated: false } as never);
    renderPanel({ optional_connection_config: ["upstream_base_url"] }, { runtimeMode: "subprocess" });
    await screen.findByText("owner/model");

    await user.click(screen.getByRole("button", { name: "Save connection" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "No changes. The saved key and settings are the same. Discovery complete. Add the models you want to use.",
    );
    expect(mockedRefreshModels).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/stale until re-verified/)).not.toBeInTheDocument();
  });

  it("invalidates every host model-selection consumer after verification", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderPanel();
    queryClient.setQueryData(["model-selection", "options"], { provider_models: {} });
    queryClient.setQueryData(["projects", "detail", "project-1"], {});
    await screen.findByText("owner/model");

    await user.click(screen.getByRole("button", { name: "Add owner/model" }));

    await waitFor(() => {
      expect(queryClient.getQueryState(["model-selection", "options"])?.isInvalidated).toBe(true);
      expect(queryClient.getQueryState(["projects", "detail", "project-1"])?.isInvalidated).toBe(true);
    });
  });

  it("renders safe verification categories as user-facing messages", async () => {
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [
          {
            id: "model-row-1",
            model_id: "owner/model",
            enabled: false,
            verification_status: "failed",
            verification_error_safe: "runtime_endpoint_required",
          },
        ],
      },
    ] as never);

    renderPanel();

    expect(await screen.findByText(/A public HTTPS runtime endpoint is required/)).toBeInTheDocument();
    expect(screen.queryByText(/runtime_endpoint_required/)).not.toBeInTheDocument();
  });

  it("explains a stale model after a key change and leaves unverified models plain", async () => {
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [
          {
            id: "model-row-1",
            model_id: "owner/verified-before",
            enabled: false,
            verification_status: "stale",
            verification_error_safe: "credentials_changed",
          },
          {
            id: "model-row-2",
            model_id: "owner/never-verified",
            enabled: false,
            verification_status: "unverified",
          },
        ],
      },
    ] as never);

    renderPanel();

    expect(
      await screen.findByText("The provider key changed. Verify this model again before using it."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Verification failed.")).not.toBeInTheDocument();
    expect(screen.getByText("unverified")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reverify owner/verified-before" })).toBeInTheDocument();
  });

  it("disables a model toggle while verification is pending", async () => {
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [
          {
            id: "model-row-1",
            model_id: "owner/model",
            enabled: false,
            verification_status: "pending",
          },
        ],
      },
    ] as never);

    renderPanel();

    const action = await screen.findByRole("button", { name: "Checking owner/model" });
    expect(action).toHaveAttribute("aria-busy", "true");
    expect(action).toHaveAttribute("aria-disabled", "true");
  });

  it("offers an explicit reverify action for an enabled stale model", async () => {
    const user = userEvent.setup();
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [
          {
            id: "model-row-1",
            model_id: "owner/model",
            enabled: true,
            verification_status: "stale",
            verification_error_safe: "verification_expired",
          },
        ],
      },
    ] as never);

    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Reverify owner/model" }));

    expect(mockedSetModelEnabled).toHaveBeenCalledWith("model-row-1", true);
    expect(await screen.findByText("owner/model added: it passed the checks.")).toBeInTheDocument();
  });

  it("announces model verification progress and success", async () => {
    let resolveVerification: (() => void) | undefined;
    mockedSetModelEnabled.mockImplementation(
      () => new Promise((resolve) => {
        resolveVerification = () => resolve({} as never);
      }),
    );
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText("owner/model");

    await user.click(screen.getByRole("button", { name: "Add owner/model" }));

    expect(screen.getByRole("status")).toHaveTextContent("Checking owner/model… This can take up to a minute.");
    resolveVerification?.();
    expect(await screen.findByText("owner/model added: it passed the checks.")).toBeInTheDocument();
  });

  it("announces mutation status and prevents duplicate requests while busy", async () => {
    let resolveSave: (() => void) | undefined;
    mockedSaveCredentials.mockImplementation(
      () => new Promise((resolve) => {
        resolveSave = () => resolve({} as never);
      }),
    );
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText("owner/model");

    await user.click(screen.getByRole("button", { name: "Save connection" }));

    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent("Saving connection…");
    expect(screen.getByRole("button", { name: "Save connection" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Discover models" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add owner/model" })).toHaveAttribute("aria-disabled", "true");

    await user.click(screen.getByRole("button", { name: "Save connection" }));
    expect(mockedSaveCredentials).toHaveBeenCalledTimes(1);

    resolveSave?.();
    expect(await screen.findByText(/Connection saved/)).toBeInTheDocument();
  });
  it("shows the declared default, the extension's field help and its documentation", async () => {
    // The product owner tried https://ollama.com, …/api/chat and …/v1 and could
    // not tell which was right: the form now says what the default is and what
    // format the extension expects, in the extension's own words.
    renderPanel(
      {
        optional_provider_secrets: ["api_key"],
        optional_connection_config: ["upstream_base_url"],
        connection_config_defaults: { upstream_base_url: "https://llm.acme.example" },
        connection_config_help: {
          upstream_base_url: "The server address only, without /api or /v1.",
        },
        provider_secret_help: {
          api_key: "Copy the whole key; it has two parts joined by a dot.", // pragma: allowlist secret - help text, not a key
        },
      },
      { runtimeMode: "subprocess", documentationUrl: "https://docs.acme.example/llm" },
    );
    await screen.findByText("owner/model");

    const baseUrl = screen.getByLabelText("Base URL (optional)");
    expect(baseUrl).toHaveAccessibleName("Base URL (optional)");
    expect(baseUrl).toHaveAttribute("placeholder", "https://llm.acme.example");
    expect(baseUrl).toHaveAccessibleDescription("The server address only, without /api or /v1.");
    expect(screen.getByText("The server address only, without /api or /v1.")).toHaveClass("fs-field-helper");
    // A secret the extension describes is one it needs: say none is saved yet.
    expect(await screen.findByText("No API key saved")).toHaveClass("fs-field-helper");
    expect(screen.getByLabelText("API key (optional)")).toHaveAccessibleName("API key (optional)");
    expect(screen.getByLabelText("API key (optional)")).toHaveAccessibleDescription(
      "No API key saved Copy the whole key; it has two parts joined by a dot.",
    );
    const docs = screen.getByRole("link", { name: /How to fill this in/ });
    expect(docs).toHaveAttribute("href", "https://docs.acme.example/llm");
    expect(docs).toHaveAttribute("target", "_blank");
  });

  it("offers only an https documentation link", async () => {
    renderPanel(
      { optional_connection_config: ["upstream_base_url"] },
      { runtimeMode: "subprocess", documentationUrl: "javascript:alert(1)" },
    );
    await screen.findByText("owner/model");

    expect(screen.queryByRole("link", { name: /How to fill this in/ })).not.toBeInTheDocument();
  });

  it("says the key was rejected when discovery fails authentication", async () => {
    // Ollama raises authentication_error from discovery when the key is cut
    // short; the route answers 502 {"detail": "authentication_error"}.
    // The server records the failed check; the panel reads it back.
    const user = userEvent.setup();
    mockedRefreshModels.mockRejectedValueOnce(
      new HttpError({ payload: { detail: "authentication_error" }, status: 502, url: "/refresh-models" }),
    );
    renderPanel({ optional_provider_secrets: ["api_key"] }, { runtimeMode: "subprocess" });
    await screen.findByText("owner/model");
    await screen.findByText("Not checked yet.");
    mockedFetchStatus.mockResolvedValue({
      install_id: "install-1",
      saved_secrets: [{ name: "api_key" }],
      last_check: { status: "authentication_error", checked_at: new Date().toISOString() },
    });

    await user.click(screen.getByRole("button", { name: "Discover models" }));

    expect(
      await screen.findByText(
        /^The provider rejected the API key\. Check that you copied the whole key, then save it again\. \(checked/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Check failed")).toBeInTheDocument();
    expect(screen.queryByText("authentication_error")).not.toBeInTheDocument();
    expect(screen.queryByText("Discovery failed.")).not.toBeInTheDocument();
  });
  it("shows when a key is saved, without ever showing it", async () => {
    mockedFetchStatus.mockResolvedValue({
      install_id: "install-1",
      saved_secrets: [{ name: "api_key", saved_at: "2026-10-04T10:42:00Z" }],
      last_check: null,
    });
    renderPanel({ required_provider_secrets: ["api_key"] }, { runtimeMode: "subprocess" });

    expect(await screen.findByText("API key saved")).toHaveClass("fs-field-helper");
    const key = screen.getByLabelText("API key");
    expect(key).toHaveAttribute("placeholder", "Saved. Enter a new key to replace it");
    expect(key).toHaveValue("");
    expect(key).toHaveAccessibleDescription("API key saved");
  });

  it("says when no key is saved for a key the provider needs", async () => {
    renderPanel({ required_provider_secrets: ["api_key"] }, { runtimeMode: "subprocess" });

    expect(await screen.findByText("No API key saved")).toHaveClass("fs-field-helper", "error");
    expect(screen.getByLabelText("API key")).toHaveAttribute("placeholder", "API key");
  });

  it("stays quiet about an optional key nobody described", async () => {
    renderPanel({ optional_provider_secrets: ["api_key"] }, { runtimeMode: "subprocess" });
    await screen.findByText("Not checked yet.");

    expect(screen.queryByText(/API key saved/)).not.toBeInTheDocument();
  });

  it.each([
    [null, "Not checked", "Not checked yet."],
    [{ status: "ok", checked_at: new Date().toISOString() }, "Connected", /^The provider accepted the settings \(checked .+\)\.$/],
    [
      { status: "authentication_error", checked_at: new Date().toISOString() },
      "Check failed",
      /^The provider rejected the API key\. Check that you copied the whole key, then save it again\. \(checked .+\)$/,
    ],
    [{ status: "timeout", checked_at: null }, "Check failed", "The provider request timed out. Try again later."],
  ])("shows the last check %#", async (lastCheck, badge, text) => {
    mockedFetchStatus.mockResolvedValue({ install_id: "install-1", saved_secrets: [], last_check: lastCheck });
    renderPanel({ optional_provider_secrets: ["api_key"] }, { runtimeMode: "subprocess" });

    expect(await screen.findByText(badge)).toBeInTheDocument();
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it("checks the connection right after saving and shows the answer", async () => {
    const user = userEvent.setup();
    mockedSaveCredentials.mockResolvedValue({ status: "saved", changed: true, verification_invalidated: true } as never);
    renderPanel({ required_provider_secrets: ["api_key"] }, { runtimeMode: "subprocess" });
    await screen.findByText("Not checked yet.");
    mockedFetchStatus.mockResolvedValue({
      install_id: "install-1",
      saved_secrets: [{ name: "api_key" }],
      last_check: { status: "ok", checked_at: new Date().toISOString() },
    });

    await user.type(screen.getByLabelText("API key"), "provider-key"); // pragma: allowlist secret - test sentinel
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(mockedRefreshModels).toHaveBeenCalledWith("install-1", {}));
    expect(mockedSaveCredentials.mock.invocationCallOrder[0]).toBeLessThan(
      mockedRefreshModels.mock.invocationCallOrder[0],
    );
    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(await screen.findByText("API key saved")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Connection saved; previous verification is stale until re-verified. Discovery complete.",
    );
  });
  it("shows a model being checked as busy, with a hint, while other rows stay usable", async () => {
    let resolveVerification: (() => void) | undefined;
    mockedSetModelEnabled.mockImplementationOnce(
      () => new Promise((resolve) => {
        resolveVerification = () => resolve({} as never);
      }),
    );
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [
          { id: "m1", model_id: "owner/first", enabled: false, verification_status: "unverified" },
          { id: "m2", model_id: "owner/second", enabled: false, verification_status: "unverified" },
        ],
      },
    ] as never);
    const user = userEvent.setup();
    renderPanel();

    const first = await screen.findByRole("button", { name: "Add owner/first" });
    await user.click(first);

    const busy = screen.getByRole("button", { name: "Checking owner/first" });
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(busy).toHaveTextContent("Checking…");
    expect(busy).toHaveAccessibleDescription(
      "Checking chat, tool calling and structured output. This can take up to a minute.",
    );
    // Focus stays on the row's action while it works.
    expect(busy).toHaveFocus();
    // The other model can be added meanwhile.
    const second = screen.getByRole("button", { name: "Add owner/second" });
    expect(second).not.toHaveAttribute("aria-disabled");
    await user.click(second);
    expect(mockedSetModelEnabled).toHaveBeenCalledWith("m2", true);

    resolveVerification?.();
    expect(await screen.findByRole("status")).toHaveTextContent(/added: it passed the checks\./);
  });

  it("shows an added model as Added and removes it with Remove", async () => {
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [{ id: "m1", model_id: "owner/model", enabled: true, verification_status: "passed" }],
      },
    ] as never);
    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByText("Added")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove owner/model" }));

    expect(mockedSetModelEnabled).toHaveBeenCalledWith("m1", false);
    expect(await screen.findByText("owner/model removed.")).toBeInTheDocument();
  });

  it("keeps the readable failure and Try again on a failed model", async () => {
    mockedFetchSources.mockResolvedValue([
      {
        id: "source-1",
        extension_install_id: "install-1",
        models: [{
          id: "m1",
          model_id: "owner/model",
          enabled: false,
          verification_status: "failed",
          verification_error_safe: "capability_mismatch_tools",
        }],
      },
    ] as never);
    const user = userEvent.setup();
    renderPanel();

    expect(
      await screen.findByText(
        "Failed the tool-calling check: the model did not call the test tool once with the right arguments.",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try verifying owner/model again" }));
    expect(mockedSetModelEnabled).toHaveBeenCalledWith("m1", true);
  });
});
