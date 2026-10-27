import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { ProjectNotificationRoutesEditor } from "@/pages/projects/notifications";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import {
  fetchProjectNotificationProviders,
  fetchProjectNotificationRoutes,
  fetchProjectNotificationWorkflows,
  fetchProjectProviderChannels,
  saveProjectNotificationRoutes,
} from "@/pages/projects/notifications/api";

vi.mock("@/pages/projects/notifications/api", () => ({
  fetchProjectNotificationProviders: vi.fn(),
  fetchProjectNotificationRoutes: vi.fn(),
  fetchProjectNotificationWorkflows: vi.fn(),
  fetchProjectProviderChannels: vi.fn(),
  saveProjectNotificationRoutes: vi.fn(),
}));

const mockedFetchProviders = vi.mocked(fetchProjectNotificationProviders);
const mockedFetchRoutes = vi.mocked(fetchProjectNotificationRoutes);
const mockedFetchWorkflows = vi.mocked(fetchProjectNotificationWorkflows);
const mockedFetchChannels = vi.mocked(fetchProjectProviderChannels);
const mockedSaveRoutes = vi.mocked(saveProjectNotificationRoutes);

function renderEditor(locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const messages: Record<string, string> = locale === "lt" ? { "react.projects.provider_configuration": "{provider} konfigūracija", "react.projects.confirm_delete_notification_route": "Ištrinti pranešimų maršrutą {route}?" } : {};
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ProjectNotificationRoutesEditor projectId="project-1" />
      </QueryClientProvider>
    </MemoryRouter>
    </I18nContext.Provider>,
  );
}

describe("ProjectNotificationRoutesEditor", () => {
  beforeEach(() => {
    mockedFetchWorkflows.mockReset();
    mockedFetchWorkflows.mockResolvedValue([]);
    mockedFetchRoutes.mockReset();
    mockedFetchRoutes.mockResolvedValue({ project_id: "project-1", notification_routes: [] });
    mockedFetchProviders.mockReset();
    mockedFetchChannels.mockReset();
    mockedFetchChannels.mockResolvedValue([]);
    mockedSaveRoutes.mockReset();
  });

  it("localizes route deletion confirmation and preserves a declined owner route", async () => {
    const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(false);
    mockedFetchProviders.mockResolvedValue([]);
    mockedFetchRoutes.mockResolvedValue({ project_id: "project-1", notification_routes: [{ route_id: "owner-route-id", route_key: "owner_route", provider_key: "acme.mailer", event_type: "fixture.completed", enabled: false, route_config: { to: ["owner@example.com"] } }] });
    renderEditor("lt");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Remove notification route owner_route" }));
    expect(confirm).toHaveBeenCalledWith("Ištrinti pranešimų maršrutą owner_route?");
    expect(mockedSaveRoutes).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Remove notification route owner_route" })).toBeInTheDocument();
    confirm.mockRestore();
  });

  it("localizes the provider setup link when the selected provider has no channels", async () => {
    mockedFetchProviders.mockResolvedValue([{ provider_key: "fixture.notifications", display_name: "Fixture Notifications", provider_type_key: "notification_provider", enabled: true, provider_enabled: true, features: ["notify"], notification_target_kind: "channel" }]);
    const user = userEvent.setup();
    renderEditor("lt");
    await user.click(await screen.findByRole("button", { name: /create new/i }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: "Provider" }));
    await user.click(screen.getByRole("option", { name: "Fixture Notifications" }));
    const link = await screen.findByRole("link", { name: "Fixture Notifications konfigūracija" });
    expect(link).toHaveAttribute("href", "/extensions/fixture.notifications?project_id=project-1");
    expect(screen.queryByRole("link", { name: "Fixture Notifications configuration" })).not.toBeInTheDocument();
  });

  it.each(["trigger", "option"])("keeps the notification draft when Escape closes the provider %s popup", async (source) => {
    mockedFetchProviders.mockResolvedValue([
      { provider_key: "acme.mailer", display_name: "Acme Mailer", provider_type_key: "notification_provider", enabled: true, provider_enabled: true, features: ["notify"], notification_target_kind: "recipient" },
    ]);
    const user = userEvent.setup();
    renderEditor();
    const opener = await screen.findByRole("button", { name: /create new/i });
    await user.click(opener);
    const dialog = screen.getByRole("dialog");
    const form = within(dialog);
    await user.type(form.getByRole("textbox", { name: "Display name" }), "Keep notification draft");
    const provider = form.getByRole("combobox", { name: "Provider" });
    await user.click(provider);
    if (source === "option") {
      await user.keyboard("{ArrowDown}");
      // The Select menu lives in the viewport layer, outside the dialog's tree.
      expect(screen.getAllByRole("option")[0]).toHaveFocus();
    }
    await user.keyboard("{Escape}");
    expect(dialog).toBeInTheDocument();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(form.getByRole("textbox", { name: "Display name" })).toHaveValue("Keep notification draft");
    await waitFor(() => expect(provider).toHaveFocus());
    expect(mockedSaveRoutes).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("shows a safe provider-inventory failure and retries both route data queries", async () => {
    // This fails if the editor returns to its former raw "Internal server error"
    // treatment or if its retry button only refetches one half of the editor data.
    mockedFetchProviders
      .mockRejectedValueOnce(new Error("Request failed with HTTP 500: Internal server error"))
      .mockResolvedValueOnce([]);

    renderEditor();

    expect(await screen.findByText("Notification provider inventory is unavailable.")).toBeInTheDocument();
    expect(screen.queryByText(/internal server error/i)).not.toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: "Retry notification data" }));

    await waitFor(() => {
      expect(mockedFetchProviders).toHaveBeenCalledTimes(2);
      expect(mockedFetchRoutes).toHaveBeenCalledTimes(2);
    });
  });

  it("creates an outgoing-email route with recipient and owning connection", async () => {
    mockedFetchProviders.mockResolvedValue([
      {
        provider_key: "acme.mailer",
        provider_type_key: "notification_provider",
        display_name: "Acme Mailer",
        enabled: true,
        provider_enabled: true,
      features: ["notify"],
      notification_target_kind: "recipient",
        connection_id: "conn_mailpit",
      },
    ]);
    mockedSaveRoutes.mockResolvedValue({ project_id: "project-1", notification_routes: [] });

    renderEditor();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Create new" }));
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    await user.click(await screen.findByRole("option", { name: "Acme Mailer" }));
    await user.type(screen.getByRole("textbox", { name: /recipient email/i }), "audit060@mailpit.local");
    await user.click(screen.getByRole("button", { name: "Create route" }));

    await waitFor(() => {
      expect(mockedSaveRoutes).toHaveBeenCalledWith("project-1", [
        expect.objectContaining({
          provider_key: "acme.mailer",
          route_config: {
            connection_id: "conn_mailpit",
            to: ["audit060@mailpit.local"],
          },
        }),
      ]);
    });
  });

  it("refuses an empty route without sending anything", async () => {
    // The operator must not be able to submit an empty route, and must be left in
    // the dialog to finish it — not have the save rejected afterwards.
    mockedFetchProviders.mockResolvedValue([
      {
        provider_key: "acme.mailer",
        display_name: "Acme Mailer",
        provider_type_key: "notification_provider",
        enabled: true,
        provider_enabled: true,
        features: ["notify"],
        notification_target_kind: "recipient",
      },
    ]);
    const user = userEvent.setup();
    renderEditor();

    await user.click(await screen.findByRole("button", { name: /create new/i }));
    await user.click(screen.getByRole("button", { name: /^create route$/i }));

    // Nothing is sent, and the dialog stays open on the field that is missing.
    await waitFor(() => expect(mockedSaveRoutes).not.toHaveBeenCalled());
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("asks a recipient-addressed provider for an address, not a channel", async () => {
    // The capability comes from the provider catalog, never from the provider key:
    // a second mail provider has to get the same form the day it is installed.
    mockedFetchProviders.mockResolvedValue([
      {
        provider_key: "acme.mailer",
        display_name: "Acme Mailer",
        provider_type_key: "notification_provider",
        enabled: true,
        provider_enabled: true,
        features: ["notify"],
        notification_target_kind: "recipient",
      },
    ]);
    const user = userEvent.setup();
    renderEditor();

    await user.click(await screen.findByRole("button", { name: /create new/i }));
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    await user.click(await screen.findByRole("option", { name: /acme mailer/i }));

    // The field is an address to type, not a list to choose from.
    expect(await screen.findByRole("textbox", { name: /recipient/i })).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /channel/i })).not.toBeInTheDocument();
    // A recipient is typed, so no channel list is fetched for it.
    expect(mockedFetchChannels).not.toHaveBeenCalled();
  });

  it("asks a channel-addressed provider for a channel", async () => {
    mockedFetchProviders.mockResolvedValue([
      {
        provider_key: "acme.messenger",
        display_name: "Acme Messenger",
        provider_type_key: "notification_provider",
        enabled: true,
        provider_enabled: true,
        features: ["notify"],
        notification_target_kind: "channel",
      },
    ]);
    const user = userEvent.setup();
    renderEditor();

    await user.click(await screen.findByRole("button", { name: /create new/i }));
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    await user.click(await screen.findByRole("option", { name: /acme messenger/i }));

    await waitFor(() => expect(mockedFetchChannels).toHaveBeenCalledWith("project-1", "acme.messenger"));
  });

  it("answers an empty submit inside the dialog, on the field, with focus", async () => {
    // Found in a real browser, not here: the old editor did refuse the submit
    // and send nothing — which the existing test already asserted — but it wrote
    // the reason onto the card *behind* the modal overlay. On screen that is a
    // dead button: no marker, no highlight, no message, focus left on Create.
    mockedFetchProviders.mockResolvedValue([{ provider_key: "fixture.notifications", display_name: "Fixture notifications", provider_type_key: "notification_provider", enabled: true, provider_enabled: true, features: ["notify"], notification_target_kind: "channel" }]);
    const user = userEvent.setup();
    renderEditor();

    await user.click(await screen.findByRole("button", { name: /create new/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /create route/i }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/provider is required/i);

    const provider = within(dialog).getByRole("combobox", { name: "Provider" });
    expect(provider).toHaveAttribute("aria-invalid", "true");
    await waitFor(() => expect(provider).toHaveFocus());
    expect(mockedSaveRoutes).not.toHaveBeenCalled();
  });

  it("drops the complaint once the named field is corrected", async () => {
    // A marker that outlives the value it was about reads as "still wrong".
    mockedFetchProviders.mockResolvedValue([{ provider_key: "fixture.notifications", display_name: "Fixture notifications", provider_type_key: "notification_provider", enabled: true, provider_enabled: true, features: ["notify"], notification_target_kind: "channel" }]);
    const user = userEvent.setup();
    renderEditor();

    await user.click(await screen.findByRole("button", { name: /create new/i }));
    const dialog = await screen.findByRole("dialog");
    const provider = within(dialog).getByRole("combobox", { name: "Provider" });
    await user.click(within(dialog).getByRole("button", { name: /create route/i }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();

    await user.click(provider);
    await user.click(await screen.findByRole("option", { name: "Fixture notifications" }));

    await waitFor(() => expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument());
    expect(provider).not.toHaveAttribute("aria-invalid");
  });

  it("marks the required fields so they are known before submitting", async () => {
    mockedFetchProviders.mockResolvedValue([{ provider_key: "fixture.notifications", display_name: "Fixture notifications", provider_type_key: "notification_provider", enabled: true, provider_enabled: true, features: ["notify"], notification_target_kind: "channel" }]);
    const user = userEvent.setup();
    renderEditor();

    await user.click(await screen.findByRole("button", { name: /create new/i }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).getByRole("textbox", { name: /route key/i })).toHaveAttribute("readonly");
    expect(within(dialog).getByRole("combobox", { name: "Event type" })).toBeRequired();
    expect(within(dialog).getByRole("combobox", { name: "Provider" })).toBeRequired();
    // Optional fields must not be marked, or the marker means nothing.
    expect(within(dialog).getByRole("combobox", { name: "Workflow" })).not.toBeRequired();
  });

  it("shows a backend refusal in the dialog and keeps the draft", async () => {
    // A save that the server rejects must be readable without closing the
    // dialog and losing everything typed into it.
    mockedFetchProviders.mockResolvedValue([
      {
        provider_key: "acme.mailer",
        display_name: "Acme Mailer",
        provider_type_key: "notification_provider",
        enabled: true,
        provider_enabled: true,
        features: ["notify"],
        notification_target_kind: "recipient",
      },
    ]);
    mockedSaveRoutes.mockRejectedValue(
      new Error("Request failed with HTTP 400: notification route provider is not enabled"),
    );
    const user = userEvent.setup();
    renderEditor();

    await user.click(await screen.findByRole("button", { name: /create new/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: "Provider" }));
    await user.click(await screen.findByRole("option", { name: /acme mailer/i }));
    await user.type(within(dialog).getByRole("textbox", { name: /recipient/i }), "ops@example.com");
    await user.click(within(dialog).getByRole("button", { name: /create route/i }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/provider is not enabled/i);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: /recipient/i })).toHaveValue("ops@example.com");
  });

  it("does not rewrite a stored route's delivery shape when its provider is unavailable", async () => {
    // Found in a real browser. With the extension disabled the provider left the
    // inventory, an unknown target kind read as "channel", and saving the route
    // untouched rewrote {"to": ["..."]} into {"channel_id": "..."} — a delivery
    // shape the operator never chose, written without a word on screen.
    mockedFetchProviders.mockResolvedValue([]);
    mockedFetchRoutes.mockResolvedValue({
      project_id: "project-1",
      notification_routes: [
        {
          route_id: "route-1",
          route_key: "acceptance_route",
          provider_key: "acme.mailer",
          event_type: "fixture.custom.completed",
          workflow_id: "retired-owned-workflow",
          enabled: false,
          route_config: { connection_id: "connection-1", to: ["ops@example.com"] },
        },
      ],
    });
    mockedSaveRoutes.mockResolvedValue(undefined as never);
    const user = userEvent.setup();
    renderEditor();

    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /save route/i }));

    await waitFor(() => expect(mockedSaveRoutes).toHaveBeenCalled());
    const [, saved] = mockedSaveRoutes.mock.calls[0];
    expect(saved[0].route_config).toEqual({ connection_id: "connection-1", to: ["ops@example.com"] });
    expect(saved[0].route_config).not.toHaveProperty("channel_id");
    expect(saved[0]).toMatchObject({ route_key: "acceptance_route", event_type: "fixture.custom.completed", workflow_id: "retired-owned-workflow", enabled: false });
  });

  it("names an unavailable provider instead of showing the route as having none", async () => {
    mockedFetchProviders.mockResolvedValue([]);
    mockedFetchRoutes.mockResolvedValue({
      project_id: "project-1",
      notification_routes: [
        {
          route_id: "route-1",
          route_key: "acceptance_route",
          provider_key: "acme.mailer",
          event_type: "workflow.automation.notify",
          enabled: true,
          route_config: { connection_id: "connection-1", to: ["ops@example.com"] },
        },
      ],
    });
    const user = userEvent.setup();
    renderEditor();

    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).getByRole("combobox", { name: "Provider" })).toHaveTextContent(
      /acme\.mailer \(unavailable\)/,
    );
  });

  it("does not open a new route when no provider can resolve its delivery shape", async () => {
    // A new route has no stored shape to preserve, so an unresolvable provider
    // has to be an error, not a default.
    mockedFetchProviders.mockResolvedValue([]);
    const user = userEvent.setup();
    renderEditor();

    await user.click(await screen.findByRole("button", { name: /create new/i }));
    expect(screen.getByRole("button", { name: /create new/i })).toBeDisabled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockedSaveRoutes).not.toHaveBeenCalled();
  });
});
