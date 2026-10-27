import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SettingsProvidersPage } from "@/pages/settings/providers";
import {
  createModelProvider,
  fetchModelCatalogSettings,
  fetchModelProvidersCollection,
  reverifyModelProvider,
  updateModelProvider,
} from "@/pages/settings/shared/api";

vi.mock("@/pages/settings/shared/api", () => ({
  createModelProvider: vi.fn(),
  deleteModelProvider: vi.fn(),
  fetchModelCatalogSettings: vi.fn(),
  fetchModelProvidersCollection: vi.fn(),
  reverifyModelProvider: vi.fn(),
  updateModelProvider: vi.fn(),
}));

const mockedCreateModelProvider = vi.mocked(createModelProvider);
const mockedFetchModelCatalogSettings = vi.mocked(fetchModelCatalogSettings);
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
    mockedFetchModelCatalogSettings.mockReset();
    mockedFetchModelProvidersCollection.mockReset();
    mockedReverifyModelProvider.mockReset();
    mockedUpdateModelProvider.mockReset();
    mockedFetchModelProvidersCollection.mockResolvedValue([] as never);
    mockedFetchModelCatalogSettings.mockResolvedValue({
      families: [
        {
          family_id: "openai",
          models: [{ model_id: "gpt-5.6-terra", label: "GPT-5.6 Terra" }],
        },
        {
          family_id: "anthropic",
          models: [{ model_id: "claude-sonnet-5", label: "Claude Sonnet 5" }],
        },
        {
          family_id: "google",
          models: [{ model_id: "gemini-3-pro", label: "Gemini 3 Pro" }],
        },
      ],
    } as never);
    mockedReverifyModelProvider.mockResolvedValue({
      verification_status: "passed",
    } as never);
  });

  it("uses a noun translation for the provider Type column rather than the composer instruction", async () => {
    renderPage("lt");
    expect(await screen.findByRole("columnheader", { name: "Tipas" })).toBeInTheDocument();
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

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Provider saved, but model verification failed: The provider account has insufficient quota or credit.",
    );
    expect(
      screen.getByRole("heading", { name: "Edit provider" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save provider" }));

    expect(mockedCreateModelProvider).toHaveBeenCalledTimes(1);
    expect(mockedUpdateModelProvider).toHaveBeenCalledWith(
      "provider-1",
      expect.objectContaining({ name: "Anthropic custom" }),
    );
    expect(
      await screen.findByText("Model provider updated."),
    ).toBeInTheDocument();
  });

  it("offers official provider presets with catalog model choices and no editable endpoint", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add provider" }));

    expect(screen.getByRole("combobox", { name: "Provider type" })).toHaveTextContent("OpenAI (official)");
    expect(screen.queryByLabelText("Base URL")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Default model")).toHaveAttribute(
      "list",
      "provider-default-model-options",
    );
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
    expect(await within(table).findByText("failed")).toBeInTheDocument();
    expect(
      await within(table).findByText("The provider account has insufficient quota or credit."),
    ).toBeInTheDocument();
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
    expect(await within(table).findByText("passed")).toBeInTheDocument();
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
    expect(
      await screen.findByText("Model provider verified."),
    ).toBeInTheDocument();
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
