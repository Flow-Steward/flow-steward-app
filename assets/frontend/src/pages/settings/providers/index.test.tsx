import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { HttpError } from "@/lib/http/errors";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SettingsProvidersPage } from "@/pages/settings/providers";
import {
  createModelProvider,
  discoverModelProviderModels,
  fetchModelProvidersCollection,
  reverifyModelProvider,
  updateModelProvider,
} from "@/pages/settings/shared/api";

vi.mock("@/pages/settings/shared/api", () => ({
  createModelProvider: vi.fn(),
  deleteModelProvider: vi.fn(),
  discoverModelProviderModels: vi.fn(),
  fetchModelProvidersCollection: vi.fn(),
  reverifyModelProvider: vi.fn(),
  updateModelProvider: vi.fn(),
}));

const mockedCreateModelProvider = vi.mocked(createModelProvider);
const mockedDiscoverModelProviderModels = vi.mocked(discoverModelProviderModels);
const mockedFetchModelProvidersCollection = vi.mocked(
  fetchModelProvidersCollection,
);
const mockedReverifyModelProvider = vi.mocked(reverifyModelProvider);
const mockedUpdateModelProvider = vi.mocked(updateModelProvider);


function renderPage(locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  const view = render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages: {}, t: (key, text = "", params) => interpolate(locale === "lt" ? ({ "trash.columns.type": "Tipas", "dashboard.composer.commands_hint_prefix": "Įveskite" } as Record<string, string>)[key] || text : text, params) }}><QueryClientProvider client={queryClient}>
      <SettingsProvidersPage />
    </QueryClientProvider></I18nContext.Provider>,
  );
  return { ...view, queryClient };
}

type User = ReturnType<typeof userEvent.setup>;

/** Provider type is a DsSelect: open the combobox, then pick the option. */
async function chooseType(user: User, label: string) {
  await user.click(screen.getByRole("combobox", { name: "Provider type" }));
  await user.click(await screen.findByRole("option", { name: label }));
}

describe("SettingsProvidersPage", () => {
  beforeEach(() => {
    mockedCreateModelProvider.mockReset();
    mockedDiscoverModelProviderModels.mockReset();
    mockedFetchModelProvidersCollection.mockReset();
    mockedReverifyModelProvider.mockReset();
    mockedUpdateModelProvider.mockReset();
    mockedFetchModelProvidersCollection.mockResolvedValue([] as never);
    mockedDiscoverModelProviderModels.mockResolvedValue([
      { model_id: "new-model", label: "New model" },
    ]);
    mockedReverifyModelProvider.mockResolvedValue({
      verification_status: "passed",
    } as never);
  });

  it("uses a noun translation for the provider Type column rather than the composer instruction", async () => {
    renderPage("lt");
    await waitFor(() => expect(screen.getByRole("columnheader", { name: "Tipas" })).toBeInTheDocument());
    expect(screen.queryByRole("columnheader", { name: "Įveskite" })).not.toBeInTheDocument();
  });

  it("keeps the form open and shows the safe reason when verification fails", async () => {
    mockedCreateModelProvider.mockResolvedValue({
      id: "provider-1",
      status: "created",
      verification_status: "failed",
      verification_error_safe: "billing_quota_exceeded",
    } as never);
    mockedUpdateModelProvider.mockResolvedValue({
      id: "provider-1",
      status: "updated",
      verification_status: "passed",
    } as never);
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    await user.type(screen.getByLabelText("Name"), "Anthropic custom");
    await user.type(screen.getByLabelText("Default model"), "claude-sonnet-5");
    await user.click(screen.getByRole("button", { name: "Create provider" }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(
      "Provider saved, but model verification failed: The provider account has insufficient quota or credit.",
    ));
    expect(
      screen.getByRole("heading", { name: "Edit provider" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/saving can run model capability checks.*charges/i)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Save provider" }));

    expect(mockedCreateModelProvider).toHaveBeenCalledTimes(1);
    expect(mockedUpdateModelProvider).toHaveBeenCalledWith(
      "provider-1",
      expect.objectContaining({ name: "Anthropic custom" }),
    );
    await waitFor(() => expect(screen.getByText("Model provider updated.")).toBeInTheDocument());
  });

  it("marks required fields, uses a URL control and explains verification charges", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "Add provider" }));
    expect(screen.getByLabelText("Name")).toBeRequired();
    expect(screen.getByRole("combobox", { name: "Provider type" })).toBeRequired();
    expect(screen.getAllByText("*")).toHaveLength(2);
    expect(screen.getByText(/saving can run model capability checks.*charges/i)).toBeVisible();
    await chooseType(user, "OpenAI Compatible");
    const url = screen.getByLabelText("Base URL");
    expect(url).toHaveAttribute("type", "url");
    expect(url).toBeRequired();
    expect(screen.getByLabelText("Default model")).not.toBeRequired();
    expect(screen.getByLabelText("API key")).not.toBeRequired();
    expect(mockedCreateModelProvider).not.toHaveBeenCalled();
    await chooseType(user, "Azure OpenAI");
    expect(screen.getByLabelText("Base URL")).toBeRequired();
    expect(screen.getByLabelText("API version")).toBeRequired();
    expect(screen.getByLabelText("Deployment name")).toBeRequired();
    expect(screen.getAllByText("*")).toHaveLength(5);
    await chooseType(user, "OpenAI (official)");
    expect(screen.queryByLabelText("Base URL")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("API version")).not.toBeInTheDocument();
  });

  it("loads current models on demand and clears suggestions when the provider changes", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const loadModels = screen.getByRole("button", { name: "Load models" });
    const defaultModel = screen.getByLabelText("Default model");
    expect(loadModels).toBeDisabled();
    expect(defaultModel).not.toHaveAttribute("list");
    expect(screen.queryByLabelText("Base URL")).not.toBeInTheDocument();

    const enteredKey = "openai-test";
    await user.type(screen.getByLabelText("API key"), enteredKey);
    await user.click(loadModels);
    expect(mockedDiscoverModelProviderModels).toHaveBeenCalledWith(
      { provider_type: "openai", api_key: enteredKey, provider_id: "" },
      expect.any(AbortSignal),
    );
    await waitFor(() => expect(defaultModel).toHaveAttribute("list", "provider-default-model-options"));
    expect(screen.getByText("New model", { selector: "option" })).toHaveValue("new-model");

    await chooseType(user, "Anthropic (official)");
    expect(defaultModel).not.toHaveAttribute("list");
    expect(loadModels).toBeDisabled();
    await user.click(screen.getByRole("combobox", { name: "Provider type" }));
    expect(screen.queryByRole("option", { name: /google/i })).not.toBeInTheDocument();
    expect(within(screen.getByRole("dialog")).queryByText(/google|vertex/i)).not.toBeInTheDocument();
    expect(within(screen.getByRole("dialog")).queryByRole("link", { name: "Catalog" })).not.toBeInTheDocument();
  });

  it("aborts model discovery when the key changes and ignores its late result", async () => {
    let finishRequest: (() => void) | undefined;
    let requestSignal: AbortSignal | undefined;
    mockedDiscoverModelProviderModels.mockImplementation((_payload, signal) => {
      requestSignal = signal;
      return new Promise((resolve) => {
        finishRequest = () => resolve([{ model_id: "stale-model", label: "Stale model" }]);
      });
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const key = screen.getByLabelText("API key");
    await user.type(key, "old-key");
    await user.click(screen.getByRole("button", { name: "Load models" }));
    await user.clear(key);
    expect(requestSignal?.aborted).toBe(true);
    finishRequest?.();

    await waitFor(() => expect(screen.getByLabelText("Default model")).not.toHaveAttribute("list"));
  });

  it("shows a safe provider error and keeps the manual model selection", async () => {
    mockedDiscoverModelProviderModels.mockRejectedValue(new HttpError({
      payload: { code: "rate_limited", message: "Could not load models from the provider" },
      status: 502,
      url: "/api/model-providers/discover",
    }));
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    await chooseType(user, "Anthropic (official)");
    await user.type(screen.getByLabelText("API key"), "test-key");
    const model = screen.getByLabelText("Default model");
    await user.type(model, "manual-model");
    await user.click(screen.getByRole("button", { name: "Load models" }));

    await waitFor(() => expect(screen.getByText("The provider is rate limited. Try again later.")).toBeVisible());
    expect(model).toHaveValue("manual-model");
    expect(model).not.toHaveAttribute("list");
  });

  it("keeps existing Google providers verifiable and removable outside the provider form", async () => {
    mockedFetchModelProvidersCollection.mockResolvedValue([
      { id: "google-1", name: "Existing Google", provider_type: "google", default_model: "gemini-model" },
    ]);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Existing Google" }));
    expect(screen.queryByRole("menuitem", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Verify" })).toBeVisible();
    expect(screen.getByRole("menuitem", { name: "Delete" })).toBeVisible();
    expect(screen.getByText("gemini-model")).toBeVisible();
  });

  it("omits hidden Azure fields after switching to OpenAI Compatible", async () => {
    mockedCreateModelProvider.mockResolvedValue({
      id: "provider-1",
      verification_status: "passed",
    } as never);
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    await user.type(screen.getByLabelText("Name"), "Compatible provider");
    await chooseType(user, "Azure OpenAI");
    await user.type(screen.getByLabelText("API version"), "2026-01-01");
    await user.type(
      screen.getByLabelText("Deployment name"),
      "azure-deployment",
    );
    await chooseType(user, "OpenAI Compatible");
    await user.type(
      screen.getByLabelText("Base URL"),
      "https://models.example.test/v1",
    );
    await user.type(screen.getByLabelText("Default model"), "compatible-model");
    await user.click(screen.getByRole("button", { name: "Create provider" }));

    await waitFor(() =>
      expect(mockedCreateModelProvider).toHaveBeenCalledTimes(1),
    );
    expect(mockedCreateModelProvider).toHaveBeenCalledWith({
      api_key: "",
      base_url: "https://models.example.test/v1",
      default_model: "compatible-model",
      name: "Compatible provider",
      provider_type: "openai_compatible",
      upstream_revision: "",
    });
  });

  it("does not submit a stale compatible endpoint after switching to an official provider", async () => {
    mockedCreateModelProvider.mockResolvedValue({
      id: "provider-1",
      verification_status: "passed",
    } as never);
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    await user.type(screen.getByLabelText("Name"), "Official provider");
    await chooseType(user, "OpenAI Compatible");
    await user.type(
      screen.getByLabelText("Base URL"),
      "https://stale.example.test/v1",
    );
    await chooseType(user, "OpenAI (official)");
    await user.type(screen.getByLabelText("Default model"), "gpt-5.6-terra");
    await user.click(screen.getByRole("button", { name: "Create provider" }));

    await waitFor(() =>
      expect(mockedCreateModelProvider).toHaveBeenCalledTimes(1),
    );
    expect(mockedCreateModelProvider).toHaveBeenCalledWith({
      api_key: "",
      default_model: "gpt-5.6-terra",
      name: "Official provider",
      provider_type: "openai",
      upstream_revision: "",
    });
  });

  it("projects verification status and safe errors into the provider grid", async () => {
    mockedFetchModelProvidersCollection.mockResolvedValue([
      {
        id: "provider-1",
        name: "Anthropic custom",
        provider_type: "anthropic",
        default_model: "claude-sonnet-5",
        verification_status: "failed",
        verification_error_safe: "billing_quota_exceeded",
      },
    ] as never);

    renderPage();

    // Read the rendered row: the safe message has to reach the operator, not
    // just the payload.
    const table = await screen.findByRole("table", { name: "Provider settings" });
    await waitFor(() => expect(within(table).getByText("failed")).toBeInTheDocument());
    await waitFor(() => expect(within(table).getByText("The provider account has insufficient quota or credit.")).toBeInTheDocument());
  });

  it("leaves the verification error empty for a passed provider", async () => {
    mockedFetchModelProvidersCollection.mockResolvedValue([
      {
        id: "provider-1",
        name: "Verified provider",
        provider_type: "openai_compatible",
        default_model: "model-1",
        verification_status: "passed",
      },
    ] as never);

    renderPage();

    const table = await screen.findByRole("table", { name: "Provider settings" });
    await waitFor(() => expect(within(table).getByText("passed")).toBeInTheDocument());
    // A provider that passed carries no error text at all.
    expect(
      within(table).queryByText(/insufficient quota|invalid api key|could not be reached/i),
    ).not.toBeInTheDocument();
  });

  it("lets an operator explicitly retry a failed verification", async () => {
    mockedFetchModelProvidersCollection.mockResolvedValue([
      {
        id: "provider-1",
        name: "Failed provider",
        provider_type: "openai_compatible",
        verification_status: "failed",
      },
    ] as never);
    renderPage();

    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "Provider settings" });
    await within(table).findByText("Failed provider");
    await user.click(screen.getByRole("button", { name: /^Actions for / }));
    await user.click(await screen.findByRole("menuitem", { name: "Verify" }));

    await waitFor(() => {
      expect(mockedReverifyModelProvider).toHaveBeenCalledWith("provider-1");
    });
    await waitFor(() => expect(screen.getByText("Model provider verified.")).toBeInTheDocument());
  });

  it("invalidates global model selection consumers after a provider mutation", async () => {
    mockedCreateModelProvider.mockResolvedValue({
      id: "provider-1",
      verification_status: "passed",
    } as never);
    const user = userEvent.setup();
    const { queryClient } = renderPage();
    queryClient.setQueryData(["model-selection", "options"], {
      provider_models: {},
    });
    queryClient.setQueryData(["projects", "detail", "project-1"], {});

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    await user.type(screen.getByLabelText("Name"), "Verified provider");
    await user.type(screen.getByLabelText("Default model"), "model-1");
    await user.click(screen.getByRole("button", { name: "Create provider" }));

    await waitFor(() => {
      expect(
        queryClient.getQueryState(["model-selection", "options"])
          ?.isInvalidated,
      ).toBe(true);
      expect(
        queryClient.getQueryState(["projects", "detail", "project-1"])
          ?.isInvalidated,
      ).toBe(true);
    });
  });

  it("exposes an accessible focus-trapped dialog and restores focus on Escape", async () => {
    const user = userEvent.setup();
    renderPage();
    const opener = screen.getByRole("button", { name: "Add provider" });

    await user.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Add provider" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByLabelText("Name")).toHaveFocus();

    const submit = screen.getByRole("button", { name: "Create provider" });
    submit.focus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Close provider dialog" })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});
