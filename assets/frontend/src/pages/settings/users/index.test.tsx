import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { useAuth } from "@/lib/auth/useAuth";
import { SettingsUsersPage } from "@/pages/settings/users";
import {
  createUser,
  deleteUser,
  fetchAccountsCollection,
  fetchUsersCollection,
  unlockUser,
  updateUser,
} from "@/pages/settings/shared/api";

vi.mock("@/lib/auth/useAuth", () => ({ useAuth: vi.fn() }));
vi.mock("@/pages/settings/shared/api", () => ({
  createUser: vi.fn(),
  deleteUser: vi.fn(),
  fetchAccountsCollection: vi.fn(),
  fetchUsersCollection: vi.fn(),
  forceResetUser: vi.fn(),
  setTemporaryUserPassword: vi.fn(),
  unlockUser: vi.fn(),
  updateUser: vi.fn(),
}));

const mockedUseAuth = vi.mocked(useAuth);
const mockedCreateUser = vi.mocked(createUser);
const mockedDeleteUser = vi.mocked(deleteUser);
const mockedFetchAccountsCollection = vi.mocked(fetchAccountsCollection);
const mockedFetchUsersCollection = vi.mocked(fetchUsersCollection);
const mockedUnlockUser = vi.mocked(unlockUser);
const mockedUpdateUser = vi.mocked(updateUser);

function authValue(roles = ["admin"]) {
  return {
    error: null,
    isAuthenticated: true,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    session: {
      authenticated: true, local_auth_enabled: true, bootstrap_required: false, external_auth_enabled: false, auth_source: "local", roles, user: {
        id: "admin-self",
        email: "admin@example.com",
        username: "admin@example.com",
        display_name: "Admin",
        roles,
        account_id: "acc-1",
        account_ids: ["acc-1"],
        account_memberships: [],
      },
      permissions: { can_read: true, can_mutate: roles.includes("admin") },
      account_memberships: [], i18n: { locale: "en", fallback_locale: "en", supported_locales: [], messages: {} }, feature_flags: {},
      environment: { edition: "community" }, settings_pages: [], registered_pages: [], shell_navigation: {},
    },
    status: "authenticated" as const,
  };
}

function renderUsersPage(initialEntry = "/settings/users", locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<I18nContext.Provider value={{ ...fallbackContext, locale, messages: {}, t: (key, text = "", params) => interpolate(locale === "lt" && key === "react.settings.users_count" ? "Naudotojų: {count}" : text, params) }}><QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[initialEntry]}><SettingsUsersPage /></MemoryRouter></QueryClientProvider></I18nContext.Provider>);
}

describe("SettingsUsersPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedUseAuth.mockReturnValue(authValue());
    mockedFetchAccountsCollection.mockResolvedValue([{ id: "acc-1", name: "Flow Steward" }]);
    mockedFetchUsersCollection.mockResolvedValue({
      users: [{ id: "u1", email: "maya@example.com", username: "maya@example.com", display_name: "Maya Chen", roles: ["operator"], is_active: true, must_change_password: false, account_id: "acc-1", account_ids: ["acc-1"] }],
      total: 1, page: 1, per_page: 5, total_pages: 1,
    });
  });

  it("localizes the settled user count without changing owner profile values", async () => {
    renderUsersPage("/settings/users", "lt");
    await screen.findByText("Maya Chen");
    expect(screen.getByText("Naudotojų: 1")).toBeInTheDocument();
    expect(screen.queryByText("1 users")).not.toBeInTheDocument();
    expect(screen.getByText("maya@example.com")).toBeInTheDocument();
  });

  it("does not request users or accounts for a non-admin", async () => {
    mockedUseAuth.mockReturnValue(authValue(["viewer"]));
    renderUsersPage();
    expect(await screen.findByText("Administrator access required")).toBeInTheDocument();
    expect(mockedFetchUsersCollection).not.toHaveBeenCalled();
    expect(mockedFetchAccountsCollection).not.toHaveBeenCalled();
  });

  it("serializes server-owned search state through the filter-complete query", async () => {
    const user = userEvent.setup();
    renderUsersPage();
    const search = await screen.findByRole("searchbox", { name: "Search users" });
    await user.type(search, "maya");
    await waitFor(() => expect(mockedFetchUsersCollection).toHaveBeenLastCalledWith(expect.objectContaining({ search: "maya", page: 1, perPage: 5 })));
  });

  it("uses the canonical field chooser and removable pills for server filters", async () => {
    const user = userEvent.setup();
    renderUsersPage();
    await screen.findByRole("table", { name: "Users and access" });

    expect(screen.queryByRole("combobox", { name: "Filter by role" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Filter by status" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add filter" }));
    await user.click(screen.getByRole("menuitem", { name: "Role" }));
    await user.click(screen.getByRole("menuitem", { name: "Operator" }));
    expect(screen.getByText("Role · Operator")).toBeInTheDocument();
    await waitFor(() => expect(mockedFetchUsersCollection).toHaveBeenLastCalledWith(expect.objectContaining({ role: "operator", page: 1 })));

    await user.click(screen.getByRole("button", { name: "Add filter" }));
    await user.click(screen.getByRole("menuitem", { name: "Status" }));
    await user.click(screen.getByRole("menuitem", { name: "Inactive" }));
    expect(screen.getByText("Status · Inactive")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear all" })).toBeInTheDocument();
    await waitFor(() => expect(mockedFetchUsersCollection).toHaveBeenLastCalledWith(expect.objectContaining({ role: "operator", status: "inactive", page: 1 })));

    await user.click(screen.getByRole("button", { name: "Remove Role filter" }));
    expect(screen.queryByText("Role · Operator")).not.toBeInTheDocument();
    await waitFor(() => expect(mockedFetchUsersCollection).toHaveBeenLastCalledWith(expect.objectContaining({ role: "", status: "inactive", page: 1 })));
  });

  it("uses the canonical single administrator-issued temporary password field", async () => {
    const user = userEvent.setup();
    renderUsersPage();
    await user.click(await screen.findByRole("button", { name: "Add user" }));
    const password = screen.getByLabelText("Temporary password");
    expect(password).toHaveAttribute("autocomplete", "off");
    expect(password).toHaveAttribute("name", "temporary-password");
    expect(screen.getByRole("form", { name: "Add user form" })).toHaveAttribute("autocomplete", "off");
    expect(screen.getByRole("button", { name: "Close add user dialog" })).toBeVisible();
    expect(screen.queryByLabelText("Confirm temporary password")).not.toBeInTheDocument();
  });

  it("keeps a non-blocking signup notification warning visible after user creation", async () => {
    const user = userEvent.setup();
    mockedCreateUser.mockResolvedValue({
      status: "created",
      user: { id: "u2" },
      warnings: [
        "User created, but the welcome email was not delivered. Check the email extension's connection.",
      ],
    });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Add user" }));
    const dialog = screen.getByRole("dialog", { name: "Add user" });
    await user.type(within(dialog).getByPlaceholderText("e.g. Alex Morgan"), "Mail warning user");
    await user.type(within(dialog).getByRole("textbox", { name: "Email address" }), "mail-warning@example.com");
    await user.type(within(dialog).getByLabelText("Temporary password"), "temporary-pass");
    await user.click(within(dialog).getByRole("button", { name: "Add user" }));

    await waitFor(() => expect(mockedCreateUser).toHaveBeenCalledWith({
      display_name: "Mail warning user",
      email: "mail-warning@example.com",
      password: "temporary-pass", // pragma: allowlist secret - disposable user fixture
      roles: ["viewer"],
      is_active: true,
      must_change_password: true,
    }));

    expect(
      await screen.findByText(
        "Mail warning user was added. User created, but the welcome email was not delivered. Check the email extension's connection.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps password-change policy explicit in the set-password dialog", async () => {
    const user = userEvent.setup();
    renderUsersPage();
    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Set temporary password" }));
    const password = screen.getByLabelText("Temporary password");
    const requireChange = screen.getByRole("checkbox", { name: "Require password change at next sign-in" });
    expect(password).toHaveAttribute("autocomplete", "off");
    expect(requireChange).toBeChecked();
    expect(screen.getByText("The user will receive this password through your existing provisioning process.")).toBeVisible();
    expect(screen.getByText("Sign-in")).toBeVisible();
    expect(screen.getByRole("button", { name: "Close temporary password dialog" })).toBeVisible();
    expect(screen.queryByLabelText("Confirm temporary password")).not.toBeInTheDocument();
  });

  it("uses the canonical reset and deactivation confirmation consequences", async () => {
    const user = userEvent.setup();
    renderUsersPage();
    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Require password reset" }));
    const resetDialog = await screen.findByRole("alertdialog", { name: "Require a password reset for Maya Chen?" });
    expect(within(resetDialog).getByText("Maya must choose a new password at next sign-in. Other active sessions will be revoked.")).toBeVisible();
    expect(within(resetDialog).getByText("All other sessions will end.")).toBeVisible();
    await user.click(within(resetDialog).getByRole("button", { name: "Close password reset confirmation" }));

    await user.click(screen.getByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Deactivate user" }));
    const deactivateDialog = await screen.findByRole("alertdialog", { name: "Deactivate Maya Chen?" });
    expect(within(deactivateDialog).getByText("Maya will no longer be able to sign in. Existing roles are retained.")).toBeVisible();
    expect(within(deactivateDialog).getByText("Sign-in access stops immediately.")).toBeVisible();
    expect(within(deactivateDialog).getByRole("button", { name: "Close deactivate user confirmation" })).toBeVisible();
  });

  it("requires explicit confirmation before deleting another user", async () => {
    const user = userEvent.setup();
    mockedDeleteUser.mockResolvedValue({ status: "deleted", user_id: "u1", revoked_sessions: 2 });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete user" }));

    const dialog = await screen.findByRole("alertdialog", { name: "Delete Maya Chen?" });
    expect(within(dialog).getByText(/sessions and personal access tokens will be revoked/i)).toBeVisible();
    expect(mockedDeleteUser).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Delete user" }));

    await waitFor(() => expect(mockedDeleteUser).toHaveBeenCalledWith("u1"));
    expect(await screen.findByText("Maya Chen was deleted.")).toBeInTheDocument();
  });

  it("does not offer self-delete", async () => {
    const user = userEvent.setup();
    mockedUseAuth.mockReturnValue(authValue());
    mockedFetchUsersCollection.mockResolvedValue({
      users: [{ id: "admin-self", email: "admin@example.com", username: "admin@example.com", display_name: "Current Admin", roles: ["admin"], is_active: true, must_change_password: false, account_ids: ["acc-1"] }],
      total: 1, page: 1, per_page: 5, total_pages: 1,
    });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Current Admin" }));

    expect(screen.queryByRole("menuitem", { name: "Delete user" })).not.toBeInTheDocument();
  });

  it("names the user after clearing a sign-in lockout", async () => {
    const user = userEvent.setup();
    mockedUnlockUser.mockResolvedValue({ status: "unlocked" });
    renderUsersPage();
    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Clear sign-in lockout" }));
    await waitFor(() => expect(mockedUnlockUser).toHaveBeenCalledWith("u1"));
    expect(await screen.findByText("Sign-in lockout cleared for Maya Chen.")).toBeInTheDocument();
  });

  it("retains accurate failure feedback when unlock rejects", async () => {
    const user = userEvent.setup();
    mockedUnlockUser.mockRejectedValue(new Error("Lockout service unavailable"));
    renderUsersPage();
    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Clear sign-in lockout" }));
    expect(await screen.findByText("Lockout service unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/cleared for Maya/)).not.toBeInTheDocument();
  });

  it("keeps edit open for a blank name and only succeeds when the returned name matches", async () => {
    const user = userEvent.setup();
    mockedUpdateUser.mockResolvedValue({
      user: { display_name: "Maya Chen" },
    });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit access" }));
    const dialog = screen.getByRole("dialog", { name: "Edit access for Maya Chen" });
    const name = within(dialog).getByRole("textbox", { name: /Display name/ });
    await user.clear(name);
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    expect(within(dialog).getByText("Display name is required.")).toBeInTheDocument();
    expect(mockedUpdateUser).not.toHaveBeenCalled();

    await user.type(name, "Maya Updated");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    expect(await within(dialog).findByText("The saved display name did not match the submitted value.")).toBeInTheDocument();
    expect(screen.queryByText("Access for Maya Chen was updated.")).not.toBeInTheDocument();
  });

  it("does not report persisted success when edit access has no changes", async () => {
    const user = userEvent.setup();
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit access" }));
    const dialog = screen.getByRole("dialog", { name: "Edit access for Maya Chen" });
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    expect(await within(dialog).findByText("Change at least one setting before saving.")).toBeInTheDocument();
    expect(mockedUpdateUser).not.toHaveBeenCalled();
    expect(screen.queryByText("Access for Maya Chen was updated.")).not.toBeInTheDocument();
  });

  it("hides tenant inventory and controls while editing access without changing memberships", async () => {
    const user = userEvent.setup();
    mockedUpdateUser.mockResolvedValue({ user: { display_name: "Maya Updated", account_id: "acc-1", account_ids: ["acc-1"] } });
    renderUsersPage();
    const table = await screen.findByRole("table", { name: "Users and access" });
    expect(within(table).queryByRole("columnheader", { name: "Accounts" })).not.toBeInTheDocument();
    expect(within(table).queryByText("acc-1")).not.toBeInTheDocument();
    expect(mockedFetchAccountsCollection).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Add user" }));
    const addDialog = screen.getByRole("dialog", { name: "Add user" });
    expect(within(addDialog).queryByRole("combobox", { name: "Account memberships" })).not.toBeInTheDocument();
    expect(within(addDialog).queryByRole("combobox", { name: "Primary account" })).not.toBeInTheDocument();
    await user.click(within(addDialog).getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit access" }));
    const dialog = screen.getByRole("dialog", { name: "Edit access for Maya Chen" });
    expect(within(dialog).queryByRole("combobox", { name: "Account memberships" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("combobox", { name: "Primary account" })).not.toBeInTheDocument();
    const name = within(dialog).getByRole("textbox", { name: /Display name/ });
    await user.clear(name);
    await user.type(name, "Maya Updated");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(mockedUpdateUser).toHaveBeenCalledWith("u1", { display_name: "Maya Updated" }));
    expect(await screen.findByText("Access for Maya Chen was updated.")).toBeInTheDocument();
  });
});
