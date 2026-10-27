import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";

import { fetchJson } from "@/lib/http/client";
import { useAuth } from "@/lib/auth/useAuth";
import { SettingsHubPage } from "@/pages/settings/hub";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

vi.mock("@/lib/auth/useAuth", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/pages/settings/service-accounts", () => ({
  SettingsServiceAccountsPage: () => <div>Service accounts surface</div>,
}));

vi.mock("@/pages/settings/general", () => ({
  SettingsGeneralPage: () => <div>General surface</div>,
}));

vi.mock("@/pages/settings/security", () => ({
  SettingsSecurityPage: () => <div>Security surface</div>,
}));

const mockedUseAuth = vi.mocked(useAuth);
const mockedFetchJson = vi.mocked(fetchJson);

describe("SettingsHubPage", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
    mockedFetchJson.mockResolvedValue({});
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: {
        authenticated: true,
        auth_source: "local",
        bootstrap_required: false,
        external_auth_enabled: false,
        local_auth_enabled: true,
        roles: ["admin"],
        permissions: {
          can_read: true,
          can_mutate: true,
        },
        account_memberships: [],
        i18n: {
          locale: "en",
          fallback_locale: "en",
          supported_locales: [],
          messages: {},
        },
        feature_flags: {},
        environment: {
          edition: "community",
        },
        settings_pages: [
          {
            page_key: "service_accounts",
            title: "Service Accounts",
            path: "/settings/service-accounts",
            settings_tab: true,
            sort_order: 10,
          },
        ],
        registered_pages: [],
        shell_navigation: {},
      },
      status: "authenticated",
    });
  });

  it("localizes the loaded resource count in the registered settings surface", async () => {
    mockedFetchJson.mockResolvedValue({ items: [{ id: "record-1", name: "Fixture record" }, { id: "record-2", name: "Fixture record two" }] });
    const auth = mockedUseAuth();
    mockedUseAuth.mockReturnValue({ ...auth, session: { ...auth.session!, settings_pages: [{ page_key: "fixture", title: "Fixture", path: "/settings/fixture", settings_tab: true, sort_order: 1, control_surface: { kind: "resource_admin", resources: [{ key: "records", title: "Fixture records", list_path: "/private/fixture", item_path: "/private/fixture/{id}", collection_key: "items", id_field: "id", display_field: "name", fields: [{ key: "name", label: "Name", type: "text", required: true }] }] } }] } });
    const messages: Record<string, string> = { "react.settings.records_count": "Įrašų: {count}" };
    render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><MemoryRouter initialEntries={["/settings/fixture"]}><Routes><Route path="/settings/:settingsPageKey" element={<SettingsHubPage />} /></Routes></MemoryRouter></I18nContext.Provider>);
    expect(await screen.findByText("Įrašų: 2")).toBeInTheDocument();
    expect(screen.queryByText("2 records")).not.toBeInTheDocument();
  });

  it.each([undefined, "Owner empty label", ""])("localizes only an absent resource empty label, preserving explicit metadata %s", async (emptyLabel) => {
    mockedFetchJson.mockResolvedValue({ items: [] });
    const auth = mockedUseAuth();
    const resource = { key: "records", title: "Fixture records", list_path: "/private/fixture", item_path: "/private/fixture/{id}", collection_key: "items", id_field: "id", display_field: "name", fields: [{ key: "name", label: "Owner field", type: "text", required: true }], ...(emptyLabel === undefined ? {} : { empty_label: emptyLabel }) };
    mockedUseAuth.mockReturnValue({ ...auth, session: { ...auth.session!, settings_pages: [{ page_key: "fixture", title: "Fixture", path: "/settings/fixture", settings_tab: true, sort_order: 1, control_surface: { kind: "resource_admin", resources: [resource] } }] } });
    const messages: Record<string, string> = { "react.settings.no_records_configured": "Įrašų nesukonfigūruota." };
    render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><MemoryRouter initialEntries={["/settings/fixture"]}><Routes><Route path="/settings/:settingsPageKey" element={<SettingsHubPage />} /></Routes></MemoryRouter></I18nContext.Provider>);
    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledWith("/private/fixture"));
    await waitFor(() => expect(screen.getByRole("table", { name: "Fixture records" })).toHaveAttribute("aria-busy", "false"));
    if (emptyLabel === undefined) expect(screen.getByText("Įrašų nesukonfigūruota.")).toBeInTheDocument();
    else if (emptyLabel) expect(screen.getByText(emptyLabel)).toBeInTheDocument();
    else expect(screen.queryByText("Įrašų nesukonfigūruota.")).not.toBeInTheDocument();
    expect(screen.queryByText("No records are configured.")).not.toBeInTheDocument();
  });

  it("keeps registered service accounts visible in the settings hub", () => {
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <SettingsHubPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("tab", { name: "Service Accounts" })).toBeInTheDocument();
    expect(screen.getByText("Service accounts surface")).toBeInTheDocument();
  });

  it("mounts only the active settings tab", () => {
    mockedUseAuth.mockReturnValueOnce({
      ...mockedUseAuth(),
      session: {
        ...mockedUseAuth().session!,
        settings_pages: [
          {
            page_key: "general",
            title: "General",
            path: "/settings/general",
            settings_tab: true,
            sort_order: 5,
          },
          {
            page_key: "service_accounts",
            title: "Service Accounts",
            path: "/settings/service-accounts",
            settings_tab: true,
            sort_order: 10,
          },
        ],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <SettingsHubPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("General surface")).toBeInTheDocument();
    expect(screen.queryByText("Service accounts surface")).not.toBeInTheDocument();
  });

  it("keeps general and security visible when registry data is unavailable", () => {
    mockedUseAuth.mockReturnValueOnce({
      ...mockedUseAuth(),
      session: {
        ...mockedUseAuth().session!,
        settings_pages: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <SettingsHubPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("tab", { name: "General" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Security" })).toBeInTheDocument();
    expect(screen.getByText("General surface")).toBeInTheDocument();
  });

  it("activates a registered custom settings path through the shell slot", () => {
    mockedUseAuth.mockReturnValueOnce({
      ...mockedUseAuth(),
      session: {
        ...mockedUseAuth().session!,
        settings_pages: [
          {
            page_key: "custom_module",
            title: "Custom Module",
            path: "/settings/custom-module",
            settings_tab: true,
            sort_order: 50,
          },
        ],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/custom-module"]}>
        <Routes>
          <Route path="/settings/:settingsPageKey" element={<SettingsHubPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("tab", { name: "Custom Module" })).toHaveAttribute(
      "data-tab",
      "custom_module",
    );
    expect(screen.getByRole("link", { name: "Open Custom Module" })).toHaveAttribute(
      "href",
      "/settings/custom-module",
    );
    expect(mockedFetchJson).not.toHaveBeenCalled();
  });

  it("renders a registered resource admin control surface from registry metadata", async () => {
    mockedFetchJson.mockImplementation(async (path) => {
      const requestPath = stringFromUnknownValue(path);
      if (requestPath === "/private/sources") {
        return {
          items: [
            {
              key: "workforce-directory",
              name: "Workforce Directory",
              kind: "external",
              enabled: true,
            },
          ],
        };
      }
      if (requestPath === "/private/policies") {
        return {
          items: [
            {
              id: "policy-uuid",
              source_key: "workforce-directory",
              match: "example.com",
              priority: 10,
              enabled: true,
            },
          ],
        };
      }
      return {};
    });
    mockedUseAuth.mockReturnValueOnce({
      ...mockedUseAuth(),
      session: {
        ...mockedUseAuth().session!,
        settings_pages: [
          {
            page_key: "access_admin",
            title: "Access Admin",
            path: "/settings/access-admin",
            settings_tab: true,
            sort_order: 50,
            control_surface: {
              kind: "resource_admin",
              description: "Manage access records.",
              resources: [
                {
                  key: "sources",
                  title: "Directory Sources",
                  description: "Sources available for sign-in.",
                  list_path: "/private/sources",
                  item_path: "/private/sources/{key}",
                  collection_key: "items",
                  record_key: "item",
                  id_field: "key",
                  display_field: "name",
                  summary_fields: ["kind"],
                  fields: [
                    { key: "key", label: "Key", type: "text", required: true, create_only: true },
                    { key: "name", label: "Name", type: "text", required: true },
                    { key: "kind", label: "Kind", type: "select", options: ["external"], default: "external" },
                    { key: "enabled", label: "Enabled", type: "checkbox", default: true },
                  ],
                },
                {
                  key: "policies",
                  title: "Access Policies",
                  list_path: "/private/policies",
                  item_path: "/private/policies/{id}",
                  collection_key: "items",
                  record_key: "item",
                  id_field: "id",
                  display_field: "match",
                  summary_fields: ["source_key", "priority"],
                  fields: [
                    { key: "source_key", label: "Source key", type: "text", required: true },
                    { key: "match", label: "Match", type: "text", required: true },
                    { key: "priority", label: "Priority", type: "number", default: 0 },
                  ],
                },
              ],
            },
          },
        ],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/access-admin"]}>
        <Routes>
          <Route path="/settings/:settingsPageKey" element={<SettingsHubPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Workforce Directory")).toBeInTheDocument();
    expect(screen.getByText("example.com")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Add" })).toHaveLength(2);
    expect(screen.queryByText(/dedicated React screen/i)).not.toBeInTheDocument();
    expect(mockedFetchJson).toHaveBeenCalledWith("/private/sources");
    expect(mockedFetchJson).toHaveBeenCalledWith("/private/policies");
  });

  it("submits create edit and delete actions for registered resource admin surfaces", async () => {
    mockedFetchJson.mockImplementation(async (path, init) => {
      const requestPath = stringFromUnknownValue(path);
      if (!init && requestPath === "/private/sources") {
        return {
          items: [{ key: "source-a", name: "Source A", kind: "external", enabled: true }],
        };
      }
      return { item: {} };
    });
    mockedUseAuth.mockReturnValueOnce({
      ...mockedUseAuth(),
      session: {
        ...mockedUseAuth().session!,
        settings_pages: [
          {
            page_key: "resource_admin",
            title: "Resource Admin",
            path: "/settings/resource-admin",
            settings_tab: true,
            sort_order: 50,
            control_surface: {
              kind: "resource_admin",
              resources: [
                {
                  key: "sources",
                  title: "Sources",
                  list_path: "/private/sources",
                  item_path: "/private/sources/{key}",
                  collection_key: "items",
                  record_key: "item",
                  id_field: "key",
                  display_field: "name",
                  fields: [
                    { key: "key", label: "Key", type: "text", required: true, create_only: true },
                    { key: "name", label: "Name", type: "text", required: true },
                    { key: "kind", label: "Kind", type: "select", options: ["external"], default: "external" },
                    { key: "enabled", label: "Enabled", type: "checkbox", default: true },
                  ],
                },
              ],
            },
          },
        ],
      },
    });

    render(
      <MemoryRouter initialEntries={["/settings/resource-admin"]}>
        <Routes>
          <Route path="/settings/:settingsPageKey" element={<SettingsHubPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Source A")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByLabelText(/^Key/)).toHaveAttribute("id", "settings-sources-key-new");
    expect(screen.getByLabelText(/^Key/)).toHaveAttribute("name", "key");
    expect(screen.getByLabelText("Kind")).toHaveAttribute("id", "settings-sources-kind-new");
    expect(screen.getByLabelText("Enabled")).toHaveAttribute("name", "enabled");
    fireEvent.change(screen.getByLabelText(/^Key/), { target: { value: "source-b" } });
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "Source B" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => {
      expect(mockedFetchJson).toHaveBeenCalledWith(
        "/private/sources",
        expect.objectContaining({ method: "POST" }),
      );
    });

    const editAction = screen.getByRole("button", { name: "Edit" });
    fireEvent.click(editAction);
    expect(screen.getByRole("button", { name: "Edit" })).toBe(editAction);
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "Source A Updated" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(mockedFetchJson).toHaveBeenCalledWith(
        "/private/sources/source-a",
        expect.objectContaining({ method: "PUT" }),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mockedFetchJson).not.toHaveBeenCalledWith(
      "/private/sources/source-a",
      expect.objectContaining({ method: "DELETE" }),
    );
    fireEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete record" }),
    );
    await waitFor(() => {
      expect(mockedFetchJson).toHaveBeenCalledWith(
        "/private/sources/source-a",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
  });

  function renderJsonResource(omitBlankOnUpdate?: boolean) {
    const originalConfig = { nested: { enabled: true }, names: ["alpha", "beta"] };
    const immutableConfig = { schema: "fixed" };
    const state = {
      record: {
        id: "resource-a",
        name: "Resource A",
        config: originalConfig,
        immutable_config: immutableConfig,
      } as Record<string, unknown>,
      reads: 0,
      writes: [] as Record<string, unknown>[],
    };
    mockedFetchJson.mockImplementation(async (path, init) => {
      if (!init && path === "/private/json-resources") {
        state.reads += 1;
        return { items: [{ ...state.record }] } as never;
      }
      if (path === "/private/json-resources/resource-a" && init?.method === "PUT") {
        if (typeof init.body !== "string") {
          throw new Error("Expected a serialized resource body.");
        }
        const body = JSON.parse(init.body) as Record<string, unknown>;
        state.writes.push(body);
        state.record = { ...state.record, ...body };
        return { item: { ...state.record } } as never;
      }
      throw new Error(`Unexpected resource request: ${String(path)} ${init?.method || "GET"}`);
    });
    mockedUseAuth.mockReturnValue({
      ...mockedUseAuth(),
      session: {
        ...mockedUseAuth().session!,
        settings_pages: [{
          page_key: "json_resource_admin",
          title: "JSON Resources",
          path: "/settings/json-resources",
          settings_tab: true,
          sort_order: 50,
          control_surface: {
            kind: "resource_admin",
            resources: [{
              key: "json_resources",
              title: "JSON Resources",
              list_path: "/private/json-resources",
              item_path: "/private/json-resources/{id}",
              collection_key: "items",
              record_key: "item",
              id_field: "id",
              display_field: "name",
              fields: [
                { key: "name", label: "Name", type: "text", required: true },
                {
                  key: "config",
                  label: "Configuration",
                  type: "json",
                  ...(omitBlankOnUpdate === undefined ? {} : { omit_blank_on_update: omitBlankOnUpdate }),
                },
                { key: "immutable_config", label: "Immutable configuration", type: "json", create_only: true },
              ],
            }],
          },
        }],
      },
    });
    render(
      <MemoryRouter initialEntries={["/settings/json-resources"]}>
        <Routes>
          <Route path="/settings/:settingsPageKey" element={<SettingsHubPage />} />
        </Routes>
      </MemoryRouter>,
    );
    return { state, originalConfig, immutableConfig };
  }

  async function editJsonResource() {
    await screen.findByText("Resource A");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    return screen.getByLabelText("Configuration");
  }

  async function saveAndReopenJsonResource(name: string) {
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Record updated.");
    await screen.findByText(name);
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    return screen.getByLabelText("Configuration");
  }

  it.each([undefined, false, true])(
    "preserves stored JSON when only a sibling field changes (omit_blank_on_update=%s)",
    async (omitBlankOnUpdate) => {
      const { state, originalConfig } = renderJsonResource(omitBlankOnUpdate);
      const config = await editJsonResource();
      if (omitBlankOnUpdate) {
        expect(config).toHaveValue("");
      }
      fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "Renamed Resource" } });
      const reopened = await saveAndReopenJsonResource("Renamed Resource");

      expect(state.writes).toEqual([
        omitBlankOnUpdate
          ? { name: "Renamed Resource" }
          : { name: "Renamed Resource", config: originalConfig },
      ]);
      expect(state.reads).toBe(2);
      expect(state.record.config).toEqual(originalConfig);
      if (omitBlankOnUpdate) {
        expect(reopened).toHaveValue("");
      } else {
        expect(JSON.parse((reopened as HTMLTextAreaElement).value)).toEqual(originalConfig);
      }
    },
  );

  it.each([false, true])(
    "persists explicitly entered empty JSON and reads it back (omit_blank_on_update=%s)",
    async (omitBlankOnUpdate) => {
      const { state } = renderJsonResource(omitBlankOnUpdate);
      const config = await editJsonResource();
      fireEvent.change(config, { target: { value: "{}" } });
      const reopened = await saveAndReopenJsonResource("Resource A");

      expect(state.writes).toEqual([{ name: "Resource A", config: {} }]);
      expect(state.reads).toBe(2);
      expect(state.record.config).toEqual({});
      if (omitBlankOnUpdate) {
        expect(reopened).toHaveValue("");
      } else {
        expect(JSON.parse((reopened as HTMLTextAreaElement).value)).toEqual({});
      }
    },
  );

  it("excludes create-only JSON from update and retains it in the read-back record", async () => {
    const { state, immutableConfig } = renderJsonResource(false);
    const config = await editJsonResource();
    expect(screen.queryByLabelText("Immutable configuration")).not.toBeInTheDocument();
    fireEvent.change(config, { target: { value: '{"changed":true}' } });
    await saveAndReopenJsonResource("Resource A");

    expect(state.writes).toEqual([{ name: "Resource A", config: { changed: true } }]);
    expect(state.writes[0]).not.toHaveProperty("immutable_config");
    expect(state.reads).toBe(2);
    expect(state.record.immutable_config).toEqual(immutableConfig);
    expect(state.record.config).toEqual({ changed: true });
  });

  it("rejects invalid JSON without writing or losing the draft, then saves the corrected value", async () => {
    const { state, originalConfig } = renderJsonResource(false);
    const config = await editJsonResource();
    fireEvent.change(config, { target: { value: "{" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).not.toBeEmptyDOMElement();
    expect(state.writes).toEqual([]);
    expect(state.reads).toBe(1);
    expect(state.record.config).toEqual(originalConfig);
    expect(config).toHaveValue("{");

    fireEvent.change(config, { target: { value: '{"corrected":true}' } });
    const reopened = await saveAndReopenJsonResource("Resource A");
    expect(state.writes).toEqual([{ name: "Resource A", config: { corrected: true } }]);
    expect(state.reads).toBe(2);
    expect(state.record.config).toEqual({ corrected: true });
    expect(JSON.parse((reopened as HTMLTextAreaElement).value)).toEqual({ corrected: true });
  });
});
