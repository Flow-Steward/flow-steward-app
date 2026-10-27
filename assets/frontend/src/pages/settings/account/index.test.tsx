import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { PointerEventsCheckLevel } from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { AuthContext } from "@/lib/auth/context";
import { useAuth } from "@/lib/auth/useAuth";
import { interpolate } from "@/lib/i18n/interpolate";
import type { TranslateParams } from "@/lib/i18n/context";
import { addDays, todayIn } from "@/lib/time/calendar-days";
import { browserTimezone } from "@/lib/time/user-timezone";
import { SettingsAccountPage } from "@/pages/settings/account";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { fetchWorkflows } from "@/pages/workflows/shared/api";
import {
  createPersonalToken,
  fetchAccountProfileRecord,
  fetchCurrentAccountRecord,
  fetchPersonalTokensCollection,
  saveAccountProfileRecord,
} from "@/pages/settings/shared/api";

vi.mock("@/app/extensions", () => ({ registeredAccountProfileTabs: [], resolveRegisteredPageComponent: vi.fn() }));
vi.mock("@/lib/auth/useAuth", () => ({ useAuth: vi.fn() }));
const i18nState = vi.hoisted(() => ({ locale: "en", messages: {} as Record<string, string> }));

vi.mock("@/lib/i18n/useI18n", () => {
  const supportedLocales = [
    { code: "en", label: "English", native_label: "English" },
    { code: "lt", label: "Lithuanian", native_label: "Lietuvių" },
  ];
  return {
    useI18n: () => {
      const messages = i18nState.messages;
      return {
        locale: i18nState.locale,
        fallbackLocale: "en",
        supportedLocales,
        messages,
        t: (key: string, fallback?: string, params?: TranslateParams) => interpolate(messages[key] || fallback || key, params),
      };
    },
  };
});
vi.mock("@/pages/projects/list/api", () => ({ fetchProjectsList: vi.fn(async () => ({ projects: [] })) }));
vi.mock("@/pages/workflows/shared/api", () => ({ fetchWorkflows: vi.fn(async () => []) }));
vi.mock("@/pages/settings/shared/api", () => ({
  changeAccountPassword: vi.fn(),
  createPersonalToken: vi.fn(),
  fetchAccountProfileRecord: vi.fn(),
  fetchCurrentAccountRecord: vi.fn(),
  fetchPersonalTokensCollection: vi.fn(),
  requestAccountEmailChange: vi.fn(),
  revokePersonalToken: vi.fn(),
  saveAccountProfileRecord: vi.fn(),
}));

const mockedUseAuth = vi.mocked(useAuth);
const mockedCreatePersonalToken = vi.mocked(createPersonalToken);
const mockedFetchCurrentAccountRecord = vi.mocked(fetchCurrentAccountRecord);
const mockedFetchAccountProfileRecord = vi.mocked(fetchAccountProfileRecord);
const mockedFetchPersonalTokensCollection = vi.mocked(fetchPersonalTokensCollection);
const mockedSaveAccountProfileRecord = vi.mocked(saveAccountProfileRecord);

function renderPage(initialEntry = "/settings/account") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[initialEntry]}><AuthContext.Provider value={mockedUseAuth()}><SettingsAccountPage /></AuthContext.Provider></MemoryRouter></QueryClientProvider>);
}

describe("SettingsAccountPage", () => {
  const refresh = vi.fn(async () => undefined);
  beforeEach(() => {
    vi.clearAllMocks();
    i18nState.locale = "en";
    i18nState.messages = {};
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(async () => undefined),
      refresh,
      switchAccount: vi.fn(async () => undefined),
      session: {
        authenticated: true,
        auth_source: "local",
        bootstrap_required: false,
        external_auth_enabled: false,
        local_auth_enabled: true,
        system_email_available: true,
        roles: ["admin"],
        permissions: { can_read: true, can_mutate: true },
        account_memberships: [],
        i18n: { locale: "en", fallback_locale: "en", supported_locales: [], messages: {} },
        feature_flags: {},
        environment: { edition: "community" },
        settings_pages: [], registered_pages: [], shell_navigation: {},
      },
      status: "authenticated",
    });
    mockedFetchCurrentAccountRecord.mockResolvedValue({ account_id: "acc_default", display_name: "Flow Steward" });
    mockedFetchAccountProfileRecord.mockResolvedValue({
      profile: { id: "u1", email: "admin@example.com", username: "admin@example.com", display_name: "Admin User", roles: ["admin"], account_id: "acc_default", account_ids: ["acc_default"], timezone: "UTC", locale: "en", auth_source: "local" },
      email_change_status: {},
    });
    mockedFetchPersonalTokensCollection.mockResolvedValue({ tokens: [], total: 0, page: 1, per_page: 5, total_pages: 0 });
    mockedCreatePersonalToken.mockResolvedValue({ token: { id: "token-1", label: "Local CLI", raw_token: "fs_pat_test_secret" } });
    mockedSaveAccountProfileRecord.mockResolvedValue({ succeeded: ["locale"], failed: {} });
  });

  it("renders the connected account tabs without legacy security controls", async () => {
    renderPage();
    expect(await screen.findByRole("tab", { name: "Profile" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Sign-in" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Personal tokens" })).toBeInTheDocument();
    expect(screen.queryByText("platform-grid")).not.toBeInTheDocument();
  });

  it.each([
    { projectName: "Customer research", workflowName: "Prepare report", projectLabel: "Customer research", workflowLabel: "Prepare report" },
    { projectName: " ", workflowName: "", projectLabel: "Unnamed project", workflowLabel: "Unnamed workflow" },
  ])("uses restriction names without exposing ids and preserves exact selected authority ($projectLabel)", async ({ projectName, workflowName, projectLabel, workflowLabel }) => {
    const projectId = "3565c816-e890-4731-82aa-c6e79a2e713b";
    const workflowId = "00a2a6af-d66f-4c96-8b02-d209361ef97a";
    vi.mocked(fetchProjectsList).mockResolvedValueOnce({
      projects: [{ id: projectId, name: projectName }], total: 1, page: 1, per_page: 200, total_pages: 1,
    });
    vi.mocked(fetchWorkflows).mockResolvedValueOnce([{ workflow_id: workflowId, display_name: workflowName }]);
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage("/settings/account?tab=tokens");
    await screen.findByRole("tabpanel", { name: "Personal tokens" });
    await user.click(screen.getAllByRole("button", { name: "Create token" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Create personal token" });
    await user.type(within(dialog).getByPlaceholderText("e.g. Local CLI"), "Restricted client");
    await user.click(within(dialog).getByRole("combobox", { name: "Project restrictions" }));
    const projectOption = await screen.findByRole("option", { name: projectLabel });
    expect(projectOption).toHaveAttribute("aria-selected", "false");
    expect(document.body).not.toHaveTextContent(new RegExp(projectId));
    await user.click(projectOption);
    await user.click(within(dialog).getByRole("combobox", { name: "Workflow restrictions" }));
    const workflowOption = await screen.findByRole("option", { name: workflowLabel });
    expect(workflowOption).toHaveAttribute("aria-selected", "false");
    expect(document.body).not.toHaveTextContent(new RegExp(workflowId));
    await user.click(workflowOption);
    expect(within(dialog).getByRole("button", { name: `Remove ${projectLabel}` })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: `Remove ${workflowLabel}` })).toBeInTheDocument();
    expect(mockedCreatePersonalToken).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Create token" }));
    await waitFor(() => expect(mockedCreatePersonalToken).toHaveBeenCalledWith(expect.objectContaining({
      label: "Restricted client",
      allowed_project_ids: [projectId], allowed_workflow_ids: [workflowId],
    })));
  });

  it("shows personal identity and roles without requesting or exposing tenant details", async () => {
    renderPage();
    await screen.findByDisplayValue("Admin User");
    expect(screen.getByText("admin@example.com")).toBeVisible();
    expect(screen.getByText("admin")).toBeVisible();
    expect(screen.queryByText(/Active account/)).not.toBeInTheDocument();
    expect(screen.queryByText("acc_default")).not.toBeInTheDocument();
    expect(mockedFetchCurrentAccountRecord).not.toHaveBeenCalled();
  });

  it("offers the complete shared MCP-relevant token scope catalog", async () => {
    const user = userEvent.setup();
    renderPage("/settings/account?tab=tokens");
    await screen.findByRole("tabpanel", { name: "Personal tokens" });
    const [openCreateToken] = screen.getAllByRole("button", { name: "Create token" });
    await user.click(openCreateToken);
    const dialog = await screen.findByRole("dialog", { name: "Create personal token" });

    for (const scope of [
      "jobs.read", "jobs.write", "projects.read", "projects.write",
      "registry.read", "registry.write", "workflows.read", "workflows.write",
      "reports.read", "reports.write",
      "notify.read", "notify:dispatch",
    ]) {
      expect(within(dialog).getByLabelText(scope)).toBeInTheDocument();
    }
  });

  it("offers full administrator access only as explicit personal-token opt-in", async () => {
    const user = userEvent.setup();
    renderPage("/settings/account?tab=tokens");
    await screen.findByRole("tabpanel", { name: "Personal tokens" });
    await user.click(screen.getAllByRole("button", { name: "Create token" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Create personal token" });
    const administratorAccess = within(dialog).getByRole("checkbox", { name: "Full administrator access (admin:all)" });
    expect(administratorAccess).not.toBeChecked();
    expect(within(dialog).getByText(/Allows workflow creation and all other administrator operations/)).toBeVisible();
    await user.type(within(dialog).getByPlaceholderText("e.g. Local CLI"), "Workflow authoring");
    await user.click(within(dialog).getByText("Full administrator access (admin:all)"));
    expect(administratorAccess).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "Create token" }));
    await waitFor(() => expect(mockedCreatePersonalToken).toHaveBeenCalledWith({
      label: "Workflow authoring", expires_in_days: 30,
      scopes: ["jobs.read", "projects.read", "workflows.read", "admin:all"],
      allowed_project_ids: [], allowed_workflow_ids: [],
    }));
  });

  it("keeps the default personal token granular even for administrators", async () => {
    const user = userEvent.setup();
    renderPage("/settings/account?tab=tokens");
    await screen.findByRole("tabpanel", { name: "Personal tokens" });
    await user.click(screen.getAllByRole("button", { name: "Create token" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Create personal token" });
    await user.type(within(dialog).getByPlaceholderText("e.g. Local CLI"), "Read client");
    await user.click(within(dialog).getByRole("button", { name: "Create token" }));
    await waitFor(() => expect(mockedCreatePersonalToken).toHaveBeenCalledWith(expect.objectContaining({
      scopes: ["jobs.read", "projects.read", "workflows.read"],
    })));
  });

  it.each(["viewer", "operator"])("does not offer administrator access to a %s session", async (role) => {
    const current = mockedUseAuth();
    mockedUseAuth.mockReturnValue({ ...current, session: { ...current.session!, roles: [role] } });
    const user = userEvent.setup();
    renderPage("/settings/account?tab=tokens");
    await screen.findByRole("tabpanel", { name: "Personal tokens" });
    await user.click(screen.getAllByRole("button", { name: "Create token" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Create personal token" });
    expect(within(dialog).queryByRole("checkbox", { name: "Full administrator access (admin:all)" })).not.toBeInTheDocument();
    await user.type(within(dialog).getByPlaceholderText("e.g. Local CLI"), "Read client");
    await user.click(within(dialog).getByRole("button", { name: "Create token" }));
    await waitFor(() => expect(mockedCreatePersonalToken).toHaveBeenCalledWith(expect.objectContaining({
      scopes: ["jobs.read", "projects.read", "workflows.read"],
    })));
  });

  it("clears administrator opt-in when the personal-token dialog is dismissed", async () => {
    const user = userEvent.setup();
    renderPage("/settings/account?tab=tokens");
    await screen.findByRole("tabpanel", { name: "Personal tokens" });
    await user.click(screen.getAllByRole("button", { name: "Create token" })[0]);
    let dialog = await screen.findByRole("dialog", { name: "Create personal token" });
    await user.click(within(dialog).getByText("Full administrator access (admin:all)"));
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await user.click(screen.getAllByRole("button", { name: "Create token" })[0]);
    dialog = await screen.findByRole("dialog", { name: "Create personal token" });
    expect(within(dialog).getByRole("checkbox", { name: "Full administrator access (admin:all)" })).not.toBeChecked();
  });

  it("saves only the dirty interface language and refreshes the authenticated session", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByDisplayValue("Admin User");
    await user.click(screen.getByRole("combobox", { name: "Interface language" }));
    await user.click(screen.getByRole("option", { name: /Lietuvių \(Lithuanian\)/i }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(mockedSaveAccountProfileRecord).toHaveBeenCalledWith({ locale: "lt" }, expect.any(Function)));
    expect(refresh).toHaveBeenCalled();
  });

  it("keeps the saved language selected when saving it switches the interface language", async () => {
    // Saving the language refreshes the session, which switches the interface
    // language; the form must not fall back to the profile it was loaded with.
    refresh.mockImplementationOnce(async () => {
      i18nState.locale = "lt";
      i18nState.messages = { "react.settings.profile_changes_saved": "Profilio pakeitimai išsaugoti." };
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByDisplayValue("Admin User");
    await user.click(screen.getByRole("combobox", { name: "Interface language" }));
    await user.click(screen.getByRole("option", { name: /Lietuvių \(Lithuanian\)/i }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("Profilio pakeitimai išsaugoti.")).toBeInTheDocument();
    expect(screen.queryByText("Profile changes saved.")).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Interface language" })).toHaveTextContent(/Lietuvių/);
    expect(screen.getByText("No unsaved changes")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("renders partial-save feedback in the saved interface language and preserves failed edits", async () => {
    mockedSaveAccountProfileRecord.mockResolvedValue({ succeeded: ["locale"], failed: { display_name: "Display name service unavailable" } });
    refresh.mockImplementationOnce(async () => {
      i18nState.locale = "lt";
      i18nState.messages = { "react.settings.profile_partial_one_field": "Išsaugotas 1 laukas. {failures}" };
    });
    const user = userEvent.setup();
    renderPage();
    fireEvent.change(await screen.findByDisplayValue("Admin User"), { target: { value: "Admin Updated" } });
    await user.click(screen.getByRole("combobox", { name: "Interface language" }));
    await user.click(screen.getByRole("option", { name: /Lietuvių \(Lithuanian\)/i }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("Išsaugotas 1 laukas. display_name: Display name service unavailable")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Admin Updated")).toBeInTheDocument();
    expect(screen.getByText("1 unsaved change")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });

  it("keeps failed profile fields dirty after a partial save", async () => {
    mockedSaveAccountProfileRecord.mockResolvedValue({ succeeded: ["display_name"], failed: { timezone: "Timezone service unavailable" } });
    renderPage();
    const name = await screen.findByDisplayValue("Admin User");
    fireEvent.change(name, { target: { value: "Admin Updated" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Timezone" }), {
      target: { value: "Europe/Paris" },
    });
    fireEvent.click(screen.getByRole("option", { name: "Europe/Paris" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText(/Timezone service unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Timezone" })).toHaveValue("Europe/Paris");
    expect(screen.getByText("1 unsaved change")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });

  it("offers an email change only when an email extension can send the verification", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: "Sign-in" }));
    expect(await screen.findByRole("button", { name: "Send verification" })).toBeInTheDocument();
  });

  it("keeps a localized successful email request in the success state", async () => {
    i18nState.locale = "lt";
    i18nState.messages = { "react.settings.verification_email_sent_confirm_the_link_before_it_expires": "Patvirtinimo laiškas išsiųstas. Patvirtinkite nuorodą, kol ji galioja." };
    renderPage("/settings/account?tab=signin");
    fireEvent.change(await screen.findByPlaceholderText("name@company.com"), { target: { value: "new@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Send verification" }));
    expect(await screen.findByText("Patvirtinimo laiškas išsiųstas. Patvirtinkite nuorodą, kol ji galioja.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Email change");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("explains that the sign-in email cannot change without email", async () => {
    const context = mockedUseAuth();
    mockedUseAuth.mockReturnValue({
      ...context,
      session: { ...context.session!, system_email_available: false },
    });
    renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: "Sign-in" }));
    expect(await screen.findByText("Email is not set up")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send verification" })).not.toBeInTheDocument();
  });

  it("uses the approved provider-managed state when local authentication is unavailable", async () => {
    mockedFetchAccountProfileRecord.mockResolvedValue({
      profile: { id: "u1", email: "admin@example.com", username: "admin@example.com", display_name: "Admin User", roles: ["admin"], account_id: "acc_default", account_ids: ["acc_default"], timezone: "UTC", locale: "en", auth_source: "external" },
      email_change_status: {},
    });
    renderPage("/settings/account?tab=signin");
    expect(await screen.findByText("Sign-in is managed by your provider")).toBeInTheDocument();
    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
  });

  it("matches the canonical one-time-secret composition and reports clipboard success", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    renderPage("/settings/account?tab=tokens");
    await screen.findByRole("tabpanel", { name: "Personal tokens" });
    const [openCreateToken] = screen.getAllByRole("button", { name: "Create token" });
    await user.click(openCreateToken);
    const createDialog = await screen.findByRole("dialog", { name: "Create personal token" });
    expect(within(createDialog).getByRole("button", { name: "Close create token dialog" })).toBeVisible();
    fireEvent.change(within(createDialog).getByPlaceholderText("e.g. Local CLI"), { target: { value: "Local CLI" } });
    await user.click(within(createDialog).getByRole("button", { name: "Create token" }));

    const secretDialog = await screen.findByRole("dialog", { name: "Save your personal token" });
    expect(within(secretDialog).getByText("Store this value in a secure credential manager.")).toBeVisible();
    expect(within(secretDialog).getByRole("button", { name: "Close one-time token secret" })).toBeVisible();
    const copyButton = within(secretDialog).getByRole("button", { name: "Copy token" });
    expect(copyButton).toHaveAccessibleName("Copy token");
    expect(copyButton).toHaveTextContent(/^Copy token$/);
    expect(copyButton).not.toHaveClass("fs-icon-button");
    await user.click(copyButton);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("fs_pat_test_secret"));
    expect(await screen.findByText("Token copied to clipboard.")).toBeVisible();
    expect(within(secretDialog).getByRole("button", { name: "I've saved it" })).toBeVisible();
  });

  it("shows manual-copy recovery when the clipboard write fails", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn(async () => { throw new Error("denied"); }) } });
    renderPage("/settings/account?tab=tokens");
    await screen.findByRole("tabpanel", { name: "Personal tokens" });
    const [openCreateToken] = screen.getAllByRole("button", { name: "Create token" });
    await user.click(openCreateToken);
    const createDialog = await screen.findByRole("dialog", { name: "Create personal token" });
    fireEvent.change(within(createDialog).getByPlaceholderText("e.g. Local CLI"), { target: { value: "Local CLI" } });
    await user.click(within(createDialog).getByRole("button", { name: "Create token" }));
    const secretDialog = await screen.findByRole("dialog", { name: "Save your personal token" });
    await user.click(within(secretDialog).getByRole("button", { name: "Copy token" }));
    expect(await screen.findByText("Token could not be copied. Select the value and copy it manually.")).toBeVisible();
  });

  describe("personal token expiry", () => {
    // The published radio hides its native input and takes the click on the label.
    const today = todayIn(browserTimezone());

    async function openCreateDialog(user: ReturnType<typeof userEvent.setup>) {
      renderPage("/settings/account?tab=tokens");
      await screen.findByRole("tabpanel", { name: "Personal tokens" });
      const [openCreateToken] = screen.getAllByRole("button", { name: "Create token" });
      await user.click(openCreateToken);
      const dialog = await screen.findByRole("dialog", { name: "Create personal token" });
      fireEvent.change(within(dialog).getByPlaceholderText("e.g. Local CLI"), { target: { value: "Local CLI" } });
      return dialog;
    }

    it.each([["No expiry", undefined], ["90 days", 90]])("sends %s as chosen", async (label, days) => {
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      const dialog = await openCreateDialog(user);
      await user.click(within(dialog).getByRole("radio", { name: label }));
      await user.click(within(dialog).getByRole("button", { name: "Create token" }));
      await waitFor(() => expect(mockedCreatePersonalToken).toHaveBeenCalled());
      expect(mockedCreatePersonalToken.mock.calls[0][0].expires_in_days).toBe(days);
    });

    it("converts a custom date into whole days from today in the user's zone", async () => {
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      const dialog = await openCreateDialog(user);
      await user.click(within(dialog).getByRole("radio", { name: "Custom" }));
      const expiresOn = within(dialog).getByLabelText("Expires on");
      expect(expiresOn).toHaveAttribute("min", addDays(today, 1));
      fireEvent.change(expiresOn, { target: { value: addDays(today, 12) } });
      expect(within(dialog).getByText(/12 days from today\./)).toBeInTheDocument();
      await user.click(within(dialog).getByRole("button", { name: "Create token" }));
      await waitFor(() => expect(mockedCreatePersonalToken).toHaveBeenCalled());
      expect(mockedCreatePersonalToken.mock.calls[0][0].expires_in_days).toBe(12);
    });

    // A past day used to become a quiet one-day token.
    it.each([["today", 0], ["a past day", -3]])("refuses %s beside the field", async (_name, offset) => {
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      const dialog = await openCreateDialog(user);
      await user.click(within(dialog).getByRole("radio", { name: "Custom" }));
      const expiresOn = within(dialog).getByLabelText("Expires on");
      fireEvent.change(expiresOn, { target: { value: addDays(today, offset) } });
      expect(within(dialog).getByRole("alert")).toHaveTextContent("is not in the future.");
      await user.click(within(dialog).getByRole("button", { name: "Create token" }));
      expect(expiresOn).toHaveFocus();
      expect(mockedCreatePersonalToken).not.toHaveBeenCalled();
    });

    it("asks for a date when Custom is left empty", async () => {
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      const dialog = await openCreateDialog(user);
      await user.click(within(dialog).getByRole("radio", { name: "Custom" }));
      await user.click(within(dialog).getByRole("button", { name: "Create token" }));
      expect(within(dialog).getByRole("alert")).toHaveTextContent("Choose the date the token expires.");
      expect(within(dialog).getByLabelText("Expires on")).toHaveFocus();
      expect(mockedCreatePersonalToken).not.toHaveBeenCalled();
    });
  });
});
