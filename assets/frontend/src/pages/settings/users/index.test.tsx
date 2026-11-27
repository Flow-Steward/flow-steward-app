import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { useAuth } from "@/lib/auth/useAuth";
import { permissionsForRoles } from "@/test/role-permissions";
import { SettingsUsersPage } from "@/pages/settings/users";
import type { UserRecord } from "@/pages/settings/shared/types";
import {
  addAccountMember,
  cancelAccountInvitation,
  fetchSentAccountInvitations,
  createUser,
  deleteUser,
  fetchAccountsCollection,
  fetchUsersCollection,
  inviteAccountMember,
  removeAccountMember,
  searchAccountMemberCandidates,
  unlockUser,
  updateUser,
  updateUserAuthenticator,
} from "@/pages/settings/shared/api";
import {
  answerOwnershipTransfer,
  fetchAccountOwner,
  offerOwnershipTransfer,
  type AccountOwnerState,
} from "@/pages/settings/users/owner-transfer-api";

vi.mock("@/lib/auth/useAuth", () => ({ useAuth: vi.fn() }));
vi.mock("@/pages/settings/shared/api", () => ({
  addAccountMember: vi.fn(),
  cancelAccountInvitation: vi.fn(),
  fetchSentAccountInvitations: vi.fn(),
  SENT_INVITATIONS_QUERY_KEY: ["settings", "users", "invitations"],
  inviteAccountMember: vi.fn(),
  removeAccountMember: vi.fn(),
  searchAccountMemberCandidates: vi.fn(),
  createUser: vi.fn(),
  deleteUser: vi.fn(),
  fetchAccountsCollection: vi.fn(),
  fetchUsersCollection: vi.fn(),
  forceResetUser: vi.fn(),
  setTemporaryUserPassword: vi.fn(),
  unlockUser: vi.fn(),
  updateUser: vi.fn(),
  updateUserAuthenticator: vi.fn(),
}));
vi.mock("@/pages/settings/users/owner-transfer-api", async () => {
  const { useQuery } = await import("@tanstack/react-query");
  const fetchAccountOwner = vi.fn();
  return {
    ACCOUNT_OWNER_QUERY_KEY: ["settings", "users", "owner"],
    answerOwnershipTransfer: vi.fn(),
    cancelOwnershipTransfer: vi.fn(),
    fetchAccountOwner,
    offerOwnershipTransfer: vi.fn(),
    useAccountOwner: (enabled: boolean) =>
      useQuery({ queryKey: ["settings", "users", "owner"], queryFn: () => fetchAccountOwner(), enabled }),
  };
});

const mockedUseAuth = vi.mocked(useAuth);
const mockedCreateUser = vi.mocked(createUser);
const mockedDeleteUser = vi.mocked(deleteUser);
const mockedFetchAccountsCollection = vi.mocked(fetchAccountsCollection);
const mockedFetchUsersCollection = vi.mocked(fetchUsersCollection);
const mockedUnlockUser = vi.mocked(unlockUser);
const mockedUpdateUser = vi.mocked(updateUser);
const mockedUpdateUserAuthenticator = vi.mocked(updateUserAuthenticator);
const mockedAddAccountMember = vi.mocked(addAccountMember);
const mockedRemoveAccountMember = vi.mocked(removeAccountMember);
const mockedSearchCandidates = vi.mocked(searchAccountMemberCandidates);
const mockedInviteAccountMember = vi.mocked(inviteAccountMember);
const mockedFetchSentInvitations = vi.mocked(fetchSentAccountInvitations);
const mockedCancelInvitation = vi.mocked(cancelAccountInvitation);
const mockedFetchAccountOwner = vi.mocked(fetchAccountOwner);
const mockedOfferOwnershipTransfer = vi.mocked(offerOwnershipTransfer);
const mockedAnswerOwnershipTransfer = vi.mocked(answerOwnershipTransfer);

function ownerState(state: Partial<AccountOwnerState> = {}): AccountOwnerState {
  return { owner_user_id: "", owner_name: "", is_owner: false, pending_transfer: null, transfer_is_for_me: false, ...state };
}

const mayaRecord = { id: "u1", email: "maya@example.com", username: "maya@example.com", display_name: "Maya Chen", roles: ["operator"], is_active: true, must_change_password: false, account_id: "acc-1", account_ids: ["acc-1"] };

function userPage(users: UserRecord[] = [mayaRecord]) {
  return { users, total: users.length, page: 1, per_page: 5, total_pages: 1 };
}

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
        permissions: permissionsForRoles(roles),
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
    mockedFetchUsersCollection.mockResolvedValue(userPage());
    mockedFetchSentInvitations.mockResolvedValue([]);
    mockedUpdateUserAuthenticator.mockReset();
    mockedFetchAccountOwner.mockResolvedValue(ownerState());
  });

  it("lists pending invitations with the email as typed and cancels one (ADR 0085)", async () => {
    const user = userEvent.setup();
    let open = [
      { id: "inv-1", email: "Sam@Other.example", role: "operator", invited_by: "Admin", expires_at: "2026-12-11T10:00:00Z" },
    ];
    mockedFetchSentInvitations.mockImplementation(async () => open);
    mockedCancelInvitation.mockImplementation(async () => {
      open = [];
      return { id: "inv-1", status: "cancelled" };
    });
    renderUsersPage();

    const section = await screen.findByRole("table", { name: "Pending invitations" });
    expect(within(section).getByText("Sam@Other.example")).toBeInTheDocument();
    expect(within(section).getByText("Operator")).toBeInTheDocument();
    expect(within(section).getByText("Admin")).toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: "Cancel the invitation to Sam@Other.example" }));

    await waitFor(() => expect(mockedCancelInvitation).toHaveBeenCalledWith("inv-1"));
    await waitFor(() => expect(screen.getByText("The invitation to Sam@Other.example was cancelled.")).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByTestId("pending-invitations")).not.toBeInTheDocument());
  });

  it("shows no pending invitations section when nothing waits", async () => {
    renderUsersPage();
    await screen.findByText("Maya Chen");
    await waitFor(() => expect(mockedFetchSentInvitations).toHaveBeenCalled());
    expect(screen.queryByTestId("pending-invitations")).not.toBeInTheDocument();
  });

  it("refreshes the pending invitations after sending one", async () => {
    const user = userEvent.setup();
    mockedSearchCandidates.mockResolvedValue([]);
    mockedInviteAccountMember.mockResolvedValue({ status: "ok", message: "If this person exists, they will get an invitation" });
    renderUsersPage();
    await waitFor(() => expect(mockedFetchSentInvitations).toHaveBeenCalledTimes(1));

    await user.click(await screen.findByRole("button", { name: "Add an existing person" }));
    const dialog = screen.getByRole("dialog", { name: "Add an existing person" });
    await user.type(within(dialog).getByRole("searchbox", { name: "Find a person" }), "nobody@x.example");
    await user.click(await within(dialog).findByRole("button", { name: "Send invitation" }));

    await waitFor(() => expect(mockedFetchSentInvitations).toHaveBeenCalledTimes(2));
  });

  it("localizes the settled user count without changing owner profile values", async () => {
    renderUsersPage("/settings/users", "lt");
    await screen.findByText("Maya Chen");
    expect(screen.getByText("Naudotojų: 1")).toBeInTheDocument();
    expect(screen.queryByText("1 users")).not.toBeInTheDocument();
    expect(screen.getByText("maya@example.com")).toBeInTheDocument();
  });

  it.each([["viewer"], ["operator"]])("does not request users or accounts for a %s", async (role) => {
    mockedUseAuth.mockReturnValue(authValue([role]));
    renderUsersPage();
    await waitFor(() => expect(screen.getByText("Administrator access required")).toBeInTheDocument());
    expect(mockedFetchUsersCollection).not.toHaveBeenCalled();
    expect(mockedFetchAccountsCollection).not.toHaveBeenCalled();
    expect(mockedFetchSentInvitations).not.toHaveBeenCalled();
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
      role: "viewer",
      is_active: true,
      must_change_password: true,
    }));

    await waitFor(() => expect(screen.getByText("Mail warning user was added. User created, but the welcome email was not delivered. Check the email extension's connection.")).toBeInTheDocument());
  });

  it("invites instead of creating when the address is taken, and shows the link once (ADR 0085, C2)", async () => {
    const user = userEvent.setup();
    mockedCreateUser.mockResolvedValue({
      status: "ok",
      message: "If this person exists, they will get an invitation",
      delivery: "link",
      invitation_link: "https://fs.example/invitations/tok-1",
    });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Add user" }));
    const dialog = screen.getByRole("dialog", { name: "Add user" });
    await user.type(within(dialog).getByPlaceholderText("e.g. Alex Morgan"), "Someone");
    await user.type(within(dialog).getByRole("textbox", { name: "Email address" }), "taken@example.com");
    await user.type(within(dialog).getByLabelText("Temporary password"), "temporary-pass");
    await user.click(within(dialog).getByRole("button", { name: "Add user" }));

    await waitFor(() => expect(screen.getByText("If this person exists, they will get an invitation.")).toBeInTheDocument());
    expect(screen.queryByText("Someone was added.")).not.toBeInTheDocument();
    expect(screen.getByText("https://fs.example/invitations/tok-1")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByText("https://fs.example/invitations/tok-1")).not.toBeInTheDocument();
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
    await waitFor(() => expect(screen.getByText("Maya Chen was deleted.")).toBeInTheDocument());
  });

  it("requires an authenticator and reads the updated individual requirement from the user list", async () => {
    const user = userEvent.setup();
    mockedUpdateUserAuthenticator.mockResolvedValue({});
    mockedFetchUsersCollection.mockResolvedValueOnce(userPage()).mockResolvedValue(
      userPage([{ ...mayaRecord, authenticator_required: true }]),
    );
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Require authenticator" }));
    const dialog = screen.getByRole("alertdialog", { name: "Require an authenticator for Maya Chen?" });
    expect(mockedUpdateUserAuthenticator).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Require authenticator" }));

    await waitFor(() => expect(mockedUpdateUserAuthenticator).toHaveBeenCalledWith("u1", "require"));
    await waitFor(() => expect(mockedFetchUsersCollection).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("button", { name: "Actions for Maya Chen" }));
    await waitFor(() => expect(screen.getByRole("menuitem", { name: "Remove individual authenticator requirement" })).toBeVisible());
  });

  it("removes only the individual requirement and explains when an authenticator still applies", async () => {
    const user = userEvent.setup();
    mockedFetchUsersCollection.mockResolvedValue(userPage([{ ...mayaRecord, authenticator_required: true }]));
    mockedUpdateUserAuthenticator.mockResolvedValue({});
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Remove individual authenticator requirement" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove the individual authenticator requirement for Maya Chen?" });
    expect(within(dialog).getByText(/An installation-wide requirement or an existing setup can still require an authenticator code/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Remove individual authenticator requirement" }));

    await waitFor(() => expect(mockedUpdateUserAuthenticator).toHaveBeenCalledWith("u1", "unrequire"));
  });

  it("explains the authenticator reset and submits only after confirmation", async () => {
    const user = userEvent.setup();
    mockedUpdateUserAuthenticator.mockResolvedValue({});
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Reset authenticator" }));
    const dialog = screen.getByRole("alertdialog", { name: "Reset the authenticator for Maya Chen?" });
    expect(within(dialog).getByText("Maya Chen must set up an authenticator app at their next sign-in.")).toBeVisible();
    expect(within(dialog).getByText("The existing authenticator and recovery codes will stop working. All active sessions will end.")).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(mockedUpdateUserAuthenticator).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Reset authenticator" }));
    await user.click(screen.getByRole("button", { name: "Reset authenticator" }));
    await waitFor(() => expect(mockedUpdateUserAuthenticator).toHaveBeenCalledWith("u1", "reset"));
    await waitFor(() => expect(screen.getByText("Authenticator reset for Maya Chen. Active sessions were revoked.")).toBeVisible());
  });

  it("keeps the authenticator reset confirmation open and shows a request error", async () => {
    const user = userEvent.setup();
    mockedUpdateUserAuthenticator.mockRejectedValue(new Error("Authenticator update unavailable"));
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Reset authenticator" }));
    const dialog = screen.getByRole("alertdialog", { name: "Reset the authenticator for Maya Chen?" });
    await user.click(within(dialog).getByRole("button", { name: "Reset authenticator" }));

    await waitFor(() => expect(within(dialog).getByText("Authenticator update unavailable")).toBeVisible());
    expect(mockedFetchUsersCollection).toHaveBeenCalledTimes(1);
  });

  it.each([
    { ...mayaRecord, id: "admin-self" },
    { ...mayaRecord, deleted_at: "2026-11-20T00:00:00Z" },
  ])("hides authenticator actions for protected user row $id", async (record) => {
    const user = userEvent.setup();
    mockedFetchUsersCollection.mockResolvedValue(userPage([record]));
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));

    expect(screen.queryByRole("menuitem", { name: /authenticator/i })).not.toBeInTheDocument();
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
    await waitFor(() => expect(screen.getByText("Sign-in lockout cleared for Maya Chen.")).toBeInTheDocument());
  });

  it("retains accurate failure feedback when unlock rejects", async () => {
    const user = userEvent.setup();
    mockedUnlockUser.mockRejectedValue(new Error("Lockout service unavailable"));
    renderUsersPage();
    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Clear sign-in lockout" }));
    await waitFor(() => expect(screen.getByText("Lockout service unavailable")).toBeInTheDocument());
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

    await waitFor(() => expect(within(dialog).getByText("The saved display name did not match the submitted value.")).toBeInTheDocument());
    expect(screen.queryByText("Access for Maya Chen was updated.")).not.toBeInTheDocument();
  });

  it("does not report persisted success when edit access has no changes", async () => {
    const user = userEvent.setup();
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit access" }));
    const dialog = screen.getByRole("dialog", { name: "Edit access for Maya Chen" });
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(within(dialog).getByText("Change at least one setting before saving.")).toBeInTheDocument());
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
    await waitFor(() => expect(screen.getByText("Access for Maya Chen was updated.")).toBeInTheDocument());
  });
  // ADR 0076 §7: a user holds one system role; the form offers one of three.
  it("offers one role out of three, each with what it can do", async () => {
    const user = userEvent.setup();
    renderUsersPage();
    await user.click(await screen.findByRole("button", { name: "Add user" }));
    const dialog = screen.getByRole("dialog", { name: "Add user" });
    const roles = within(dialog).getByRole("group", { name: /Role/ });

    expect(within(roles).getAllByRole("radio")).toHaveLength(3);
    expect(within(roles).getByRole("radio", { name: /Viewer/ })).toBeChecked();
    expect(within(roles).getByRole("radio", { name: /Viewer/ })).toHaveAccessibleName(/changes nothing/);
    expect(within(roles).getByRole("radio", { name: /Operator/ })).toHaveAccessibleName(/starts workflows, decides approvals/);
    expect(within(roles).getByRole("radio", { name: /Admin/ })).toHaveAccessibleName(/Sets everything up/);
    expect(within(dialog).queryByRole("checkbox", { name: /Admin|Operator|Viewer/ })).not.toBeInTheDocument();

    await user.click(within(roles).getByText("Operator"));
    expect(within(roles).getByRole("radio", { name: /Operator/ })).toBeChecked();
    expect(within(roles).getByRole("radio", { name: /Viewer/ })).not.toBeChecked();
  });

  it("changes a user's role by sending the one chosen role", async () => {
    const user = userEvent.setup();
    mockedUpdateUser.mockResolvedValue({ user: { display_name: "Maya Chen" } });
    renderUsersPage();
    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit access" }));
    const dialog = screen.getByRole("dialog", { name: "Edit access for Maya Chen" });

    expect(within(dialog).getByRole("radio", { name: /Operator/ })).toBeChecked();
    await user.click(within(dialog).getByText("Admin"));
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(mockedUpdateUser).toHaveBeenCalledWith("u1", { role: "admin" }));
  });

  it("shows a user who still holds several roles with the highest one chosen", async () => {
    const user = userEvent.setup();
    mockedFetchUsersCollection.mockResolvedValue({
      users: [{ id: "u1", email: "maya@example.com", username: "maya@example.com", display_name: "Maya Chen", roles: ["viewer", "operator"], is_active: true, must_change_password: false, account_id: "acc-1", account_ids: ["acc-1"] }],
      total: 1, page: 1, per_page: 5, total_pages: 1,
    });
    renderUsersPage();
    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit access" }));
    const dialog = screen.getByRole("dialog", { name: "Edit access for Maya Chen" });

    expect(within(dialog).getByRole("radio", { name: /Operator/ })).toBeChecked();
    expect(within(dialog).getByRole("radio", { name: /Viewer/ })).not.toBeChecked();
  });
  it("lists each person with their role in this company", async () => {
    renderUsersPage();
    const table = await screen.findByRole("table", { name: "Users and access" });
    expect(within(table).getByRole("columnheader", { name: "Role here" })).toBeInTheDocument();
    await waitFor(() => expect(within(table).getByText("Operator")).toBeInTheDocument());
  });

  it("says when a listed person has no role in this company", async () => {
    mockedFetchUsersCollection.mockResolvedValue(userPage([{ ...mayaRecord, roles: [] }]));
    renderUsersPage();
    const table = await screen.findByRole("table", { name: "Users and access" });
    await waitFor(() => expect(within(table).getByText("No role here")).toBeInTheDocument());
  });

  it("adds an existing person to this company with the chosen role", async () => {
    const user = userEvent.setup();
    mockedSearchCandidates.mockResolvedValue([{ id: "u9", email: "lina@example.com", display_name: "Lina Park" }]);
    mockedAddAccountMember.mockResolvedValue({ user_id: "u9", account_id: "acc-1", role: "operator" });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Add an existing person" }));
    const dialog = screen.getByRole("dialog", { name: "Add an existing person" });
    await user.type(within(dialog).getByRole("searchbox", { name: "Find a person" }), "lina");

    // One match is the person: there is nothing to pick between.
    await waitFor(() => expect(within(dialog).getByTestId("existing-person-match")).toHaveTextContent("Lina Park"));
    expect(mockedSearchCandidates).toHaveBeenLastCalledWith("lina");
    expect(within(dialog).queryByRole("radio", { name: /Lina Park/ })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("radio", { name: /Viewer/ })).toBeChecked();
    await user.click(within(dialog).getByText("Operator"));
    await user.click(within(dialog).getByRole("button", { name: "Add to this company" }));

    await waitFor(() => expect(mockedAddAccountMember).toHaveBeenCalledWith("u9", "operator"));
    await waitFor(() => expect(screen.getByText("Lina Park was added to this company.")).toBeInTheDocument());
  });

  it("adds a chosen person of the managed companies by their role only", async () => {
    const user = userEvent.setup();
    mockedSearchCandidates.mockResolvedValue([
      { id: "u9", email: "lina@example.com", display_name: "Lina Park" },
      { id: "u8", email: "lina.k@example.com", display_name: "Lina Kim" },
    ]);
    mockedAddAccountMember.mockResolvedValue({ user_id: "u9", account_id: "acc-1", role: "viewer" });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Add an existing person" }));
    const dialog = screen.getByRole("dialog", { name: "Add an existing person" });
    await user.type(within(dialog).getByRole("searchbox", { name: "Find a person" }), "lina@example.com");
    await waitFor(() => expect(within(dialog).getByRole("radio", { name: /Lina Park/ })).toBeInTheDocument());
    await user.click(within(dialog).getByText("Lina Park"));
    await user.click(within(dialog).getByRole("button", { name: "Add to this company" }));

    await waitFor(() => expect(mockedAddAccountMember).toHaveBeenCalledWith("u9", "viewer"));
    expect(mockedInviteAccountMember).not.toHaveBeenCalled();
  });

  it("invites a full email address nobody of the managed companies has (ADR 0085)", async () => {
    const user = userEvent.setup();
    mockedSearchCandidates.mockResolvedValue([]);
    mockedInviteAccountMember.mockResolvedValue({
      status: "ok",
      message: "If this person exists, they will get an invitation",
    });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Add an existing person" }));
    const dialog = screen.getByRole("dialog", { name: "Add an existing person" });
    await user.type(within(dialog).getByRole("searchbox", { name: "Find a person" }), "sam@other.example");
    const send = await within(dialog).findByRole("button", { name: "Send invitation" });
    await user.click(within(dialog).getByText("Operator"));
    await user.click(send);

    await waitFor(() => expect(mockedInviteAccountMember).toHaveBeenCalledWith("sam@other.example", "operator"));
    expect(mockedAddAccountMember).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText("If this person exists, they will get an invitation")).toBeInTheDocument());
  });

  it("shows the invitation link once when no email could send it (ADR 0085, C1)", async () => {
    const user = userEvent.setup();
    mockedSearchCandidates.mockResolvedValue([]);
    mockedInviteAccountMember.mockResolvedValue({
      status: "ok",
      message: "If this person exists, they will get an invitation",
      delivery: "link",
      invitation_link: "https://fs.example/invitations/tok-9",
    });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Add an existing person" }));
    const dialog = screen.getByRole("dialog", { name: "Add an existing person" });
    await user.type(within(dialog).getByRole("searchbox", { name: "Find a person" }), "sam@other.example");
    await user.click(await within(dialog).findByRole("button", { name: "Send invitation" }));

    await waitFor(() => expect(screen.getByText("https://fs.example/invitations/tok-9")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Copy invitation link" })).toBeInTheDocument();
    expect(screen.getByText(/It is shown only now and works once\./)).toBeInTheDocument();
    // The pending list never carries the link: it is shown only in this answer.
    await user.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByText("https://fs.example/invitations/tok-9")).not.toBeInTheDocument();
  });

  it("shows no link when the invitation was emailed", async () => {
    const user = userEvent.setup();
    mockedSearchCandidates.mockResolvedValue([]);
    mockedInviteAccountMember.mockResolvedValue({
      status: "ok",
      message: "If this person exists, they will get an invitation",
      delivery: "email",
    });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Add an existing person" }));
    const dialog = screen.getByRole("dialog", { name: "Add an existing person" });
    await user.type(within(dialog).getByRole("searchbox", { name: "Find a person" }), "sam@other.example");
    await user.click(await within(dialog).findByRole("button", { name: "Send invitation" }));

    await waitFor(() => expect(screen.getByText("If this person exists, they will get an invitation")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Copy invitation link" })).not.toBeInTheDocument();
  });

  it("offers no invitation for a name that is not an email address", async () => {
    const user = userEvent.setup();
    mockedSearchCandidates.mockResolvedValue([]);
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Add an existing person" }));
    const dialog = screen.getByRole("dialog", { name: "Add an existing person" });
    await user.type(within(dialog).getByRole("searchbox", { name: "Find a person" }), "sam");
    await waitFor(() => expect(within(dialog).getByText(/No one found/)).toBeInTheDocument());
    expect(within(dialog).queryByRole("button", { name: "Send invitation" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Add to this company" })).toBeDisabled();
  });

  it("removes a person from this company only after confirmation", async () => {
    const user = userEvent.setup();
    mockedRemoveAccountMember.mockResolvedValue({ user_id: "u1", account_id: "acc-1" });
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Remove from this company" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Remove Maya Chen from this company?" });
    expect(mockedRemoveAccountMember).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Remove from this company" }));

    await waitFor(() => expect(mockedRemoveAccountMember).toHaveBeenCalledWith("u1"));
    await waitFor(() => expect(screen.getByText("Maya Chen was removed from this company.")).toBeInTheDocument());
    expect(mockedDeleteUser).not.toHaveBeenCalled();
  });

  it("shows why a removal was refused", async () => {
    const user = userEvent.setup();
    mockedRemoveAccountMember.mockRejectedValue(new Error("The last active administrator of this company cannot be removed: give another person the admin role here first"));
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
    await user.click(screen.getByRole("menuitem", { name: "Remove from this company" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Remove Maya Chen from this company?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove from this company" }));

    await waitFor(() => expect(screen.getByText(/last active administrator of this company/)).toBeInTheDocument());
  });

  it("does not offer to remove yourself from this company", async () => {
    const user = userEvent.setup();
    mockedFetchUsersCollection.mockResolvedValue(userPage([{ ...mayaRecord, id: "admin-self", display_name: "Current Admin", roles: ["admin"] }]));
    renderUsersPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Current Admin" }));

    expect(screen.queryByRole("menuitem", { name: "Remove from this company" })).not.toBeInTheDocument();
  });

  describe("the company owner (ADR 0085)", () => {
    const owner = { ...mayaRecord, id: "owner-1", display_name: "Olivia Owner", email: "olivia@example.com", roles: ["admin"] };
    const freelancer = { ...mayaRecord, id: "admin-self", display_name: "Fred Freelancer", roles: ["admin"] };

    it("marks the owner and offers nobody else the actions refused for them", async () => {
      mockedFetchAccountOwner.mockResolvedValue(ownerState({ owner_user_id: "owner-1", owner_name: "Olivia Owner" }));
      mockedFetchUsersCollection.mockResolvedValue(userPage([owner, freelancer]));
      renderUsersPage();

      await waitFor(() => expect(within(screen.getByRole("row", { name: /Olivia Owner/ })).getByTestId("user-owner-badge")).toHaveTextContent("Owner"));
      expect(within(screen.getByRole("row", { name: /Fred Freelancer/ })).queryByTestId("user-owner-badge")).not.toBeInTheDocument();
      // Nothing is offered on the owner's row: no role, name, sign-in or status change.
      expect(screen.queryByRole("button", { name: "Actions for Olivia Owner" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Actions for Fred Freelancer" })).toBeInTheDocument();
    });

    it("lets the owner offer ownership to another administrator, who must accept", async () => {
      const user = userEvent.setup();
      const self = { ...owner, id: "admin-self" };
      const admin = { ...mayaRecord, id: "u2", display_name: "Fred Freelancer", roles: ["admin"] };
      mockedFetchAccountOwner.mockResolvedValue(ownerState({ owner_user_id: "admin-self", is_owner: true }));
      mockedFetchUsersCollection.mockResolvedValue(userPage([self, admin, mayaRecord]));
      mockedOfferOwnershipTransfer.mockResolvedValue({ status: "pending", transfer: { id: "t1", from_user_id: "admin-self", from_name: "Olivia Owner", to_user_id: "u2", to_name: "Fred Freelancer", expires_at: null } });
      renderUsersPage();

      // Only an active administrator can receive it.
      await user.click(await screen.findByRole("button", { name: "Actions for Maya Chen" }));
      expect(screen.queryByRole("menuitem", { name: "Transfer ownership" })).not.toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Actions for Fred Freelancer" }));
      await user.click(screen.getByRole("menuitem", { name: "Transfer ownership" }));
      const dialog = await screen.findByRole("alertdialog", { name: "Transfer ownership to Fred Freelancer?" });
      await user.click(within(dialog).getByRole("button", { name: "Transfer ownership" }));

      await waitFor(() => expect(mockedOfferOwnershipTransfer).toHaveBeenCalledWith("u2"));
      await waitFor(() => expect(screen.getByText("Ownership was offered to Fred Freelancer. You stay the owner until they accept.")).toBeInTheDocument());
    });

    it("shows the recipient the offer and accepts it", async () => {
      const user = userEvent.setup();
      mockedFetchAccountOwner.mockResolvedValue(ownerState({
        owner_user_id: "owner-1",
        owner_name: "Olivia Owner",
        transfer_is_for_me: true,
        pending_transfer: { id: "t1", from_user_id: "owner-1", from_name: "Olivia Owner", to_user_id: "admin-self", to_name: "Fred Freelancer", expires_at: null },
      }));
      mockedAnswerOwnershipTransfer.mockResolvedValue({ status: "accepted" });
      mockedFetchUsersCollection.mockResolvedValue(userPage([owner, freelancer]));
      renderUsersPage();

      await waitFor(() => expect(screen.getByText("Olivia Owner offers you ownership of this company")).toBeInTheDocument());
      await user.click(within(screen.getByTestId("owner-transfer-offer")).getByRole("button", { name: "Accept ownership" }));

      await waitFor(() => expect(mockedAnswerOwnershipTransfer).toHaveBeenCalledWith("t1", true));
      await waitFor(() => expect(screen.getByText("You are now the owner of this company.")).toBeInTheDocument());
    });
  });
});
