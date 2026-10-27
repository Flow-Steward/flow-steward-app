import { createElement } from "react";

import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { fetchGeneralSettingsOverview } from "@/pages/settings/shared/api";
import { SettingsGeneralPage } from "@/pages/settings/general/index";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

vi.mock("@/pages/settings/shared/api", () => ({
  fetchGeneralSettingsOverview: vi.fn(),
}));

const mockedFetchGeneralSettingsOverview = vi.mocked(fetchGeneralSettingsOverview);

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  return render(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(MemoryRouter, null, createElement(SettingsGeneralPage)),
    ),
  );
}

describe("SettingsGeneralPage", () => {
  beforeEach(() => {
    mockedFetchGeneralSettingsOverview.mockReset();
  });

  it("renders route-specific overview cards from backend snapshots", async () => {
    mockedFetchGeneralSettingsOverview.mockResolvedValueOnce({
      app_bootstrap: {
        auth: {
          local_auth_enabled: true,
          external_auth_enabled: false,
          bootstrap_required: false,
          external_sign_in: {
            providers_path: "/external-sign-in/providers",
          },
        },
      },
      auth_session: {
        auth_source: "local",
        user: {
          username: "admin",
        },
      },
      model_providers: [
        { provider_id: "openai", display_name: "OpenAI" },
        { provider_id: "anthropic", display_name: "Anthropic" },
      ],
    });

    renderPage();

    expect(await screen.findByText("OpenAI")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Auth mode" })).toBeInTheDocument();
    expect(screen.getByText("External sign-in")).toBeInTheDocument();
    expect(screen.queryByText(["S", "SO feature flag"].map((value) => stringFromUnknownValue(value)).join(""))).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Model providers" })).toBeInTheDocument();
    expect(screen.getByText("Anthropic")).toBeInTheDocument();
  });

  it("surfaces backend load errors with an operator-facing message", async () => {
    mockedFetchGeneralSettingsOverview.mockRejectedValueOnce(new Error("bootstrap unavailable"));

    renderPage();

    expect(await screen.findByText("Failed to load general settings")).toBeInTheDocument();
    expect(screen.getByText("bootstrap unavailable")).toBeInTheDocument();
  });
});
