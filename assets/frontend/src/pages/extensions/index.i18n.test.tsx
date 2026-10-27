import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { I18nProvider } from "@/lib/i18n/I18nProvider";
import { ExtensionsHubPage } from "@/pages/extensions";
import {
  executeExtensionAction,
  executeExtensionQuery,
  fetchExtension,
  fetchExtensionPreview,
  fetchExtensionArchiveLifecycleAvailability,
  fetchExtensionCallbacks,
  fetchExtensionCompiledManifest,
  fetchExtensions,
  fetchLlmProviderSources,
} from "@/pages/extensions/api";
import { fetchProjectConnectionsList } from "@/pages/projects/connections/api";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { fetchExtensionOAuthConfigurations } from "@/pages/extensions/extension-oauth-api";

vi.mock("@/pages/extensions/api", () => ({
  checkExtensionHealth: vi.fn(),
  confirmExtensionUninstall: vi.fn(),
  createExtensionCallbackRegistration: vi.fn(),
  executeExtensionAction: vi.fn(),
  executeExtensionQuery: vi.fn(),
  fetchExtension: vi.fn(),
  fetchExtensionArchiveLifecycleAvailability: vi.fn(),
  fetchExtensionUninstallPlan: vi.fn(),
  fetchExtensionCallbacks: vi.fn(),
  fetchExtensionCompiledManifest: vi.fn(),
  fetchExtensionPreview: vi.fn(),
  fetchExtensions: vi.fn(),
  fetchLlmProviderSources: vi.fn(),
  installExtensionFromFile: vi.fn(),
  normalizeExtensionRecord: vi.fn((value: unknown) => value),
  refreshExtensionCallbackStatus: vi.fn(),
  refreshLlmProviderModels: vi.fn(),
  saveLlmProviderCredentials: vi.fn(),
  setExtensionEnabled: vi.fn(),
  setLlmProviderModelEnabled: vi.fn(),
}));
vi.mock("@/lib/auth/use-is-admin", () => ({ useIsAdmin: vi.fn() }));
vi.mock("@/pages/extensions/extension-oauth-api", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, fetchExtensionOAuthConfigurations: vi.fn(), fetchExtensionOAuthSetupStatus: vi.fn() };
});
vi.mock("@/pages/projects/list/api", () => ({ fetchProjectsList: vi.fn() }));
vi.mock("@/pages/projects/connections/api", () => ({
  createProjectConnection: vi.fn(),
  deleteExtensionProjectState: vi.fn(),
  deleteProjectConnection: vi.fn(),
  fetchExtensionProjectState: vi.fn(),
  fetchProjectConnectionsList: vi.fn(),
  putExtensionProjectState: vi.fn(),
  saveProjectConnectionSecrets: vi.fn(),
  startProjectConnectionOAuth: vi.fn(),
  updateProjectConnection: vi.fn(),
}));

const EXTENSION_ID = "sample.integration";

/** A two-language manifest whose fallbacks are all distinguishable. */
function bilingualManifest() {
  return {
    display_name: "Sample integration",
    locale_bundles: {
      en: {
        "ext.display_name": "Sample integration",
        "page.setup.title": "Setup",
        "component.setup_form.title": "Client setup",
        "component.setup_form.field.client.label": "Client",
        "component.setup_form.field.client.help": "Who is billed (en)",
        "component.setup_form.field.Legacy note.label": "Legacy note",
        "component.setup_form.save_label": "Save setup",
      },
      lt: {
        "ext.display_name": "Pavyzdinė integracija",
        "page.setup.title": "Nustatymai",
        "component.setup_form.title": "Kliento nustatymai",
        "component.setup_form.field.client.label": "Klientas",
        // Empty: falls back to English.
        "component.setup_form.field.client.help": "",
        "component.setup_form.field.Legacy note.label": "Sena pastaba",
        "component.setup_form.save_label": "Išsaugoti",
        // No key for the placeholder: falls back to the declaration's own text.
      },
      de: {
        "page.setup.title": "Einrichtung",
      },
    },
    ui_manifest: {
      pages: [
        {
          page_id: "setup",
          title: "Setup (declared)",
          project_scoped: false,
          components: [
            {
              component_id: "setup_form",
              type: "form",
              title: "Client setup (declared)",
              ui_schema: {
                fields: [
                  {
                    field_id: "client",
                    path: "client",
                    label: "Client (declared)",
                    help: "Who is billed (declared)",
                    placeholder: "ACME (declared)",
                    widget: "text",
                  },
                  // An old declaration: no id, so its label is its name.
                  { label: "Legacy note", widget: "text" },
                ],
                actions: { save_label: "Save (declared)" },
              },
              data: { save_action: "save_setup" },
            },
          ],
        },
      ],
    },
  };
}

function authValue(locale: string): AuthContextValue {
  return {
    error: null,
    isAuthenticated: true,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    session: {
      authenticated: true,
      roles: ["admin"],
      user: { roles: ["admin"] },
      i18n: { locale, fallback_locale: "en", supported_locales: [], messages: {} },
    } as unknown as AuthContextValue["session"],
    status: "authenticated",
  };
}

function page(locale: string, queryClient: QueryClient) {
  return (
    <AuthContext.Provider value={authValue(locale)}>
      <I18nProvider>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[`/extensions/${EXTENSION_ID}?extension_page=setup`]}>
            <Routes>
              <Route path="/extensions/:extensionId" element={<ExtensionsHubPage />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </I18nProvider>
    </AuthContext.Provider>
  );
}

function renderIn(locale: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(page(locale, queryClient));
  return { ...view, switchTo: (next: string) => view.rerender(page(next, queryClient)) };
}

describe("extension pages in the user's language", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useIsAdmin).mockReturnValue(false);
    vi.mocked(executeExtensionAction).mockResolvedValue({});
    vi.mocked(executeExtensionQuery).mockResolvedValue({});
    vi.mocked(fetchExtensionCallbacks).mockResolvedValue([]);
    vi.mocked(fetchExtensionPreview).mockResolvedValue({});
    vi.mocked(fetchExtensionCompiledManifest).mockResolvedValue(bilingualManifest());
    vi.mocked(fetchExtensions).mockResolvedValue([]);
    vi.mocked(fetchLlmProviderSources).mockResolvedValue([]);
    vi.mocked(fetchExtension).mockResolvedValue({
      extension_id: EXTENSION_ID,
      display_name: "Sample integration",
      version: "1.0.0",
      state: "active",
      active: true,
    });
    vi.mocked(fetchExtensionArchiveLifecycleAvailability).mockResolvedValue({
      available: true,
      reason_code: "available",
      message: "",
      remediation: { install_update: [], uninstall: [] },
    });
    vi.mocked(fetchExtensionOAuthConfigurations).mockResolvedValue([]);
    vi.mocked(fetchProjectConnectionsList).mockResolvedValue([]);
    vi.mocked(fetchProjectsList).mockResolvedValue({
      projects: [], page: 1, per_page: 200, total: 0, total_pages: 0,
    });
  });

  it("shows English, then Lithuanian, then English again without reloading", async () => {
    const user = userEvent.setup();
    const { switchTo } = renderIn("en");

    expect(await screen.findByRole("tab", { name: "Setup" })).toBeVisible();
    expect(screen.getByText("Client setup")).toBeVisible();
    expect(screen.getByLabelText("Client")).toBeVisible();
    await user.type(screen.getByLabelText("Client"), "ACME-UAB");

    switchTo("lt");
    expect(await screen.findByRole("tab", { name: "Nustatymai" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Kliento nustatymai")).toBeVisible();
    // The value typed before the switch is still there.
    expect(screen.getByLabelText("Klientas")).toHaveValue("ACME-UAB");
    expect(screen.getByRole("heading", { name: "Pavyzdinė integracija" })).toBeVisible();

    switchTo("en");
    expect(await screen.findByRole("tab", { name: "Setup" })).toBeVisible();
    expect(screen.getByLabelText("Client")).toHaveValue("ACME-UAB");
  });

  it("falls back to English for an empty translation and to the declaration for a missing key", async () => {
    renderIn("lt");

    const client = await screen.findByLabelText("Klientas");
    expect(screen.getByText("Who is billed (en)")).toBeVisible();
    expect(client).toHaveAttribute("placeholder", "ACME (declared)");
  });

  it("uses a dictionary for a further language with no renderer change", async () => {
    renderIn("de");

    expect(await screen.findByRole("tab", { name: "Einrichtung" })).toBeVisible();
    // Keys de does not have come from English.
    expect(screen.getByText("Client setup")).toBeVisible();
  });

  it("sends the same field names and values in every language", async () => {
    const user = userEvent.setup();
    const payloadIn = async (locale: string, legacyLabel: string, saveLabel: string) => {
      const view = renderIn(locale);
      await user.type(await screen.findByLabelText(legacyLabel), "note");
      await user.click(screen.getByRole("button", { name: saveLabel }));
      await waitFor(() => expect(executeExtensionAction).toHaveBeenCalled());
      // The last argument is a fresh request id; the payload is everything before it.
      const call = vi.mocked(executeExtensionAction).mock.calls.at(-1)?.slice(0, -1);
      view.unmount();
      vi.mocked(executeExtensionAction).mockClear();
      return call;
    };

    const english = await payloadIn("en", "Legacy note", "Save setup");
    const lithuanian = await payloadIn("lt", "Sena pastaba", "Išsaugoti");

    expect(lithuanian).toEqual(english);
    expect(JSON.stringify(english)).toContain("Legacy note");
  });
});
