import { ROLE_OPTIONS, userLabel, type EditorState, type ConfirmActionKind } from "./user-form-model";
import { UserEditorDialog, TemporaryPasswordDialog, ConfirmUserActionDialog } from "./user-dialogs";
import { useI18n } from "@/lib/i18n/useI18n";
import {
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router";

import { useAuth } from "@/lib/auth/useAuth";
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  deleteUser,
  fetchUsersCollection,
  forceResetUser,
  setTemporaryUserPassword,
  unlockUser,
  updateUser,
} from "@/pages/settings/shared/api";
import {
  AdvancedTableFilters,
  DsAlert,
  Pagination,
  RowActionMenu,
  Search,
  DsPageHeader,
} from "@/components/shared/design-system";
import type { UserFilters, UserRecord } from "@/pages/settings/shared/types";
import {
  EMPTY_EDITOR,
  initials,
  userStatusLabel,
  editorForUser,
  editorValidationError,
  persistUserEditor,
} from "./user-editor-helpers";


type UserFilterUpdater = (
  next: Partial<UserFilters>,
  resetPage?: boolean,
) => void;

function UsersAccessDenied() {
  const { t } = useI18n();
  return (
    <section className="fs-settings">
      <DsPageHeader
        description={t("react.settings.manage_who_can_access_flow_steward_their_roles_and_how_they_sign_in", "Manage who can access Flow Steward, their roles and how they sign in.")}
        title={t("react.settings.users_access", "Users & access")}
      />
      <article className="fs-card">
        <div className="fs-card-body">
          <DsAlert title={t("react.settings.administrator_access_required", "Administrator access required")}>
            {t("react.settings.you_do_not_have_permission_to_view_this_inventory_ask_an_administrator_to_update_your_role", "You do not have permission to view this inventory. Ask an administrator to update your role.")} </DsAlert>
        </div>
      </article>
    </section>
  );
}

function UsersFeedback({
  feedback,
}: Readonly<{
  feedback: { tone: "success" | "danger"; message: string } | null;
}>) {
  const { t } = useI18n();
  if (!feedback) {
    return null;
  }
  return (
    <DsAlert
      tone={feedback.tone}
      title={
        feedback.tone === "success"
          ? t("react.settings.access_updated", "Access updated")
          : t("react.settings.the_user_update_was_not_saved", "The user update was not saved")
      }
    >
      {feedback.message}
    </DsAlert>
  );
}

function UserInventoryRow({
  authUserId,
  busyUserId,
  onConfirmAction,
  onEdit,
  onImmediateAction,
  onPassword,
  rowMenu,
  setRowMenu,
  user,
}: Readonly<{
  authUserId?: string;
  busyUserId: string;
  onConfirmAction: (kind: ConfirmActionKind, user: UserRecord) => void;
  onEdit: (user: UserRecord) => void;
  onImmediateAction: (kind: "unlock" | "activate", user: UserRecord) => void;
  onPassword: (user: UserRecord) => void;
  rowMenu: UserRecord | null;
  setRowMenu: Dispatch<SetStateAction<UserRecord | null>>;
  user: UserRecord;
}>) {
  const { t } = useI18n();
  const statusLabel = userStatusLabel(user, busyUserId, t);
  return (
    <tr>
      <td>
        <span className="fs-user-cell">
          <span className="fs-avatar">{initials(userLabel(user))}</span>
          <span className="fs-user-copy">
            <strong>{userLabel(user)}</strong>
            <span>{user.email}</span>
          </span>
        </span>
      </td>
      <td>
        <span className={`fs-badge ${user.is_active ? "green" : ""}`}>
          {statusLabel}
        </span>
      </td>
      <td>
        <span className="fs-badge-row">
          {user.roles.map((role) => (
            <span className="fs-badge" key={role}>
              {role}
            </span>
          ))}
        </span>
      </td>
      <td>{user.must_change_password ? t("react.settings.change_required", "Change required") : t("monitoring.table.current", "Current")}</td>
      <td className="fs-table-action-cell">
        <RowActionMenu
          label={t("react.settings.actions_for_value1", "Actions for {value1}", { value1: String(userLabel(user)) })}
          onOpenChange={(open) => setRowMenu(open ? user : null)}
          open={rowMenu?.id === user.id}
        >
          <div className="fs-row-menu-group">
            <span className="fs-row-menu-label">{t("settings.access.keys.columns.access", "Access")}</span>
            <button onClick={() => onEdit(user)} role="menuitem" type="button">
              {t("react.settings.edit_access", "Edit access")} </button>
          </div>
          <div className="fs-row-menu-separator" />
          <div className="fs-row-menu-group">
            <span className="fs-row-menu-label">{t("react.settings.sign_in", "Sign-in")}</span>
            <button
              onClick={() => onPassword(user)}
              role="menuitem"
              type="button"
            >
              {t("react.settings.set_temporary_password", "Set temporary password")} </button>
            <button
              onClick={() => onConfirmAction("reset", user)}
              role="menuitem"
              type="button"
            >
              {t("react.settings.require_password_reset", "Require password reset")} </button>
            <button
              onClick={() => onImmediateAction("unlock", user)}
              role="menuitem"
              type="button"
            >
              {t("react.settings.clear_sign_in_lockout", "Clear sign-in lockout")} </button>
          </div>
          <div className="fs-row-menu-separator" />
          <div className="fs-row-menu-group">
            <span className="fs-row-menu-label">{t("react.settings.user_status", "User status")}</span>
            {user.is_active ? (
              <button
                className="danger"
                onClick={() => onConfirmAction("deactivate", user)}
                role="menuitem"
                type="button"
              >
                {t("react.settings.deactivate_user", "Deactivate user")} </button>
            ) : (
              <button
                onClick={() => onImmediateAction("activate", user)}
                role="menuitem"
                type="button"
              >
                {t("react.settings.activate_user", "Activate user")} </button>
            )}
            {user.id !== authUserId ? (
              <button
                className="danger"
                onClick={() => onConfirmAction("delete", user)}
                role="menuitem"
                type="button"
              >
                {t("react.settings.delete_user", "Delete user")} </button>
            ) : null}
          </div>
        </RowActionMenu>
      </td>
    </tr>
  );
}

function UserTableState({
  filters,
  isError,
  isLoading,
  onRetry,
  userCount,
}: Readonly<{
  filters: UserFilters;
  isError: boolean;
  isLoading: boolean;
  onRetry: () => void;
  userCount: number;
}>) {
  const { t } = useI18n();
  if (isLoading) {
    return (
      <div className="fs-table-state">
        <strong>{t("react.settings.loading_users", "Loading users")}</strong>
        <span>{t("react.settings.the_inventory_will_remain_in_this_table_layout", "The inventory will remain in this table layout.")}</span>
      </div>
    );
  }
  if (isError) {
    return (
      <div className="fs-table-state">
        <strong>{t("react.settings.users_could_not_be_loaded", "Users could not be loaded")}</strong>
        <button className="fs-button" onClick={onRetry} type="button">
          {t("job_security.retry", "Retry")} </button>
      </div>
    );
  }
  if (userCount) {
    return null;
  }
  const filtered = Boolean(filters.search || filters.role || filters.status);
  return (
    <div className="fs-table-state">
      <strong>{filtered ? t("react.settings.no_users_match_these_filters", "No users match these filters") : t("react.settings.no_users_yet", "No users yet")}</strong>
      <span>
        {filtered
          ? t("react.settings.change_or_clear_the_active_filters", "Change or clear the active filters.")
          : t("react.settings.add_the_first_user_to_define_access", "Add the first user to define access.")}
      </span>
    </div>
  );
}

function UsersInventory({
  authUserId,
  busyUserId,
  filters,
  isError,
  isFetching,
  isLoading,
  onConfirmAction,
  onEdit,
  onImmediateAction,
  onPassword,
  onRetry,
  rowMenu,
  setRowMenu,
  total,
  updateFilters,
  users,
}: Readonly<{
  authUserId?: string;
  busyUserId: string;
  filters: UserFilters;
  isError: boolean;
  isFetching: boolean;
  isLoading: boolean;
  onConfirmAction: (kind: ConfirmActionKind, user: UserRecord) => void;
  onEdit: (user: UserRecord) => void;
  onImmediateAction: (kind: "unlock" | "activate", user: UserRecord) => void;
  onPassword: (user: UserRecord) => void;
  onRetry: () => void;
  rowMenu: UserRecord | null;
  setRowMenu: Dispatch<SetStateAction<UserRecord | null>>;
  total: number;
  updateFilters: UserFilterUpdater;
  users: UserRecord[];
}>) {
  const { t } = useI18n();
  return (
    <article className="fs-card fs-table-card">
      <div className="fs-table-toolbar">
        <AdvancedTableFilters
          disabled={isLoading || isError}
          fields={[
            {
              key: "role",
              label: t("react.settings.role", "Role"),
              options: ROLE_OPTIONS.map((role) => ({
                value: role,
                label: role[0]?.toUpperCase() + role.slice(1),
              })),
            },
            {
              key: "status",
              label: t("account_settings.card.status", "Status"),
              options: [
                { value: "active", label: t("account_settings.status.active", "Active") },
                { value: "inactive", label: t("users.filter.inactive", "Inactive") },
              ],
            },
          ]}
          onChange={(key, value) =>
            updateFilters(
              key === "role"
                ? { role: value }
                : { status: value as UserFilters["status"] },
              true,
            )
          }
          onClear={() => updateFilters({ role: "", status: "" }, true)}
          values={{ role: filters.role, status: filters.status }}
        />
        <label className="fs-search">
          <Search aria-hidden="true" />
          <input
            aria-label={t("react.settings.search_users", "Search users")}
            onChange={(event) =>
              updateFilters({ search: event.target.value }, true)
            }
            placeholder={t("react.settings.search_users_2", "Search users…")}
            type="search"
            value={filters.search}
          />
        </label>
        <span
          aria-live="polite"
          className="fs-management-result-announcement"
        >
          {t("react.settings.users_count", "Users: {count}", { count: total })}
        </span>
      </div>
      <div className="fs-table-scroll">
        <table
          aria-busy={isFetching}
          aria-label={t("react.settings.users_and_access", "Users and access")}
          className="fs-table fs-management-table"
        >
          <thead>
            <tr>
              <th>{t("trash.type.user", "User")}</th><th>{t("account_settings.card.status", "Status")}</th><th>{t("account.profile.roles", "Roles")}</th>
              <th>{t("auth.common.password", "Password")}</th><th aria-label={t("monitoring.table.actions", "Actions")} />
            </tr>
          </thead>
          <tbody>
            {users.map((user) => (
              <UserInventoryRow
                authUserId={authUserId}
                busyUserId={busyUserId}
                key={user.id}
                onConfirmAction={onConfirmAction}
                onEdit={onEdit}
                onImmediateAction={onImmediateAction}
                onPassword={onPassword}
                rowMenu={rowMenu}
                setRowMenu={setRowMenu}
                user={user}
              />
            ))}
          </tbody>
        </table>
        <UserTableState
          filters={filters}
          isError={isError}
          isLoading={isLoading}
          onRetry={onRetry}
          userCount={users.length}
        />
      </div>
      <Pagination
        onPageChange={(page) => updateFilters({ page })}
        onPerPageChange={(perPage) => updateFilters({ perPage }, true)}
        page={filters.page}
        perPage={filters.perPage}
        total={total}
      />
    </article>
  );
}

export function SettingsUsersPage() {
  const { t } = useI18n();
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const isAdmin = auth.session?.roles.includes("admin") || false;
  const filters: UserFilters = {
    search: params.get("search") || "",
    role: params.get("role") || "",
    status: params.get("status") === "active" || params.get("status") === "inactive" ? params.get("status") as "active" | "inactive" : "",
    page: Math.max(1, Number(params.get("page") || 1)),
    perPage: [5, 10, 20, 50].includes(Number(params.get("per_page"))) ? Number(params.get("per_page")) : 5,
  };
  function updateFilters(next: Partial<UserFilters>, resetPage = false) {
    const merged = { ...filters, ...next, ...(resetPage ? { page: 1 } : {}) };
    const updated = new URLSearchParams(params);
    const entries: Array<[string, string | number]> = [["search", merged.search], ["role", merged.role], ["status", merged.status], ["page", merged.page], ["per_page", merged.perPage]];
    entries.forEach(([key, value]) => { if (value === "" || (key === "page" && value === 1) || (key === "per_page" && value === 5)) updated.delete(key); else updated.set(key, String(value)); });
    setParams(updated, { replace: true });
  }

  const usersQuery = useQuery({ queryKey: ["settings", "users", filters], queryFn: () => fetchUsersCollection(filters), enabled: isAdmin, placeholderData: (previous) => previous });

  const [rowMenu, setRowMenu] = useState<UserRecord | null>(null);
  const [editorMode, setEditorMode] = useState<"closed" | "add" | "edit">("closed");
  const [editingUser, setEditingUser] = useState<UserRecord | null>(null);
  const [editor, setEditor] = useState<EditorState>(EMPTY_EDITOR);
  const [editorError, setEditorError] = useState("");
  const [saving, setSaving] = useState(false);
  const [passwordUser, setPasswordUser] = useState<UserRecord | null>(null);
  const [temporaryPassword, setTemporaryPassword] = useState("");
  const [temporaryRequireChange, setTemporaryRequireChange] = useState(true);
  const [confirmAction, setConfirmAction] = useState<{ kind: ConfirmActionKind; user: UserRecord } | null>(null);
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; message: string } | null>(null);
  const [busyUserId, setBusyUserId] = useState("");

  function openAdd() { setEditorMode("add"); setEditingUser(null); setEditor({ ...EMPTY_EDITOR }); setEditorError(""); setFeedback(null); }
  function openEdit(user: UserRecord) { setEditorMode("edit"); setEditingUser(user); setEditor(editorForUser(user)); setEditorError(""); setFeedback(null); setRowMenu(null); }
  function closeEditor() { if (!saving) { setEditorMode("closed"); setEditingUser(null); setEditorError(""); } }
  function toggleRole(role: string, checked: boolean) { setEditor((value) => ({ ...value, roles: checked ? [...value.roles, role] : value.roles.filter((item) => item !== role) })); }

  async function refreshUsers() { await queryClient.invalidateQueries({ queryKey: ["settings", "users"] }); }

  async function saveEditor(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const validationError = editorValidationError(editor, editorMode, t);
    if (validationError) {
      setEditorError(validationError);
      return;
    }
    setSaving(true); setEditorError("");
    try {
      const message = await persistUserEditor(editorMode, editor, editingUser, t);
      setFeedback({ tone: "success", message });
      setEditorMode("closed"); setEditingUser(null); await refreshUsers();
    } catch (error) { setEditorError(error instanceof Error ? error.message : t("react.settings.the_user_update_was_not_saved", "The user update was not saved.")); }
    finally { setSaving(false); }
  }

  async function saveTemporaryPassword(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!passwordUser || temporaryPassword.length < 10) { setFeedback({ tone: "danger", message: t("react.settings.enter_a_temporary_password_of_at_least_10_characters", "Enter a temporary password of at least 10 characters.") }); return; }
    setSaving(true);
    try { await setTemporaryUserPassword(passwordUser.id, temporaryPassword, temporaryRequireChange); setFeedback({ tone: "success", message: t("react.settings.temporary_password_set_for_value1", "Temporary password set for {value1}.", { value1: String(userLabel(passwordUser)) }) }); setPasswordUser(null); setTemporaryPassword(""); setTemporaryRequireChange(true); await refreshUsers(); }
    catch (error) { setFeedback({ tone: "danger", message: error instanceof Error ? error.message : t("react.settings.temporary_password_could_not_be_set", "Temporary password could not be set.") }); }
    finally { setSaving(false); }
  }

  async function runImmediateAction(kind: "unlock" | "activate", user: UserRecord) {
    setBusyUserId(user.id); setRowMenu(null); setFeedback(null);
    try {
      if (kind === "unlock") await unlockUser(user.id); else await updateUser(user.id, { is_active: true });
      setFeedback({ tone: "success", message: kind === "unlock" ? t("react.settings.sign_in_lockout_cleared_for_value1", "Sign-in lockout cleared for {value1}.", { value1: String(userLabel(user)) }) : t("react.settings.value1_was_activated", "{value1} was activated.", { value1: String(userLabel(user)) }) });
      await refreshUsers();
    } catch (error) { setFeedback({ tone: "danger", message: error instanceof Error ? error.message : t("react.settings.the_action_could_not_be_completed", "The action could not be completed.") }); }
    finally { setBusyUserId(""); }
  }

  async function confirmUserAction() {
    if (!confirmAction) return;
    setSaving(true);
    try {
      if (confirmAction.kind === "reset") await forceResetUser(confirmAction.user.id);
      else if (confirmAction.kind === "delete") await deleteUser(confirmAction.user.id);
      else await updateUser(confirmAction.user.id, { is_active: false });
      const message = confirmAction.kind === "reset"
        ? t("react.settings.value1_must_reset_their_password_other_sessions_were_revoked", "{value1} must reset their password. Other sessions were revoked.", { value1: String(userLabel(confirmAction.user)) })
        : confirmAction.kind === "delete"
          ? t("react.settings.value1_was_deleted", "{value1} was deleted.", { value1: String(userLabel(confirmAction.user)) })
          : t("react.settings.value1_was_deactivated", "{value1} was deactivated.", { value1: String(userLabel(confirmAction.user)) });
      setFeedback({ tone: "success", message });
      setConfirmAction(null); await refreshUsers();
    } catch (error) { setFeedback({ tone: "danger", message: error instanceof Error ? error.message : t("react.settings.the_action_could_not_be_completed", "The action could not be completed.") }); }
    finally { setSaving(false); }
  }

  if (!isAdmin) {
    return <UsersAccessDenied />;
  }

  const users = usersQuery.data?.users || [];
  const total = usersQuery.data?.total || 0;
  const openPassword = (user: UserRecord) => {
    setPasswordUser(user);
    setRowMenu(null);
    setTemporaryPassword("");
    setTemporaryRequireChange(true);
  };
  const openConfirmation = (
    kind: ConfirmActionKind,
    user: UserRecord,
  ) => {
    setConfirmAction({ kind, user });
    setRowMenu(null);
  };
  const closeConfirmation = () => setConfirmAction(null);

  return (
    <section className="fs-settings">
      <DsPageHeader
        action={
          <button className="fs-button primary" onClick={openAdd} type="button">
            {t("react.settings.add_user", "Add user")} </button>
        }
        description={t("react.settings.manage_who_can_access_flow_steward_their_roles_and_how_they_sign_in", "Manage who can access Flow Steward, their roles and how they sign in.")}
        title={t("react.settings.users_access", "Users & access")}
      />
      <UsersFeedback feedback={feedback} />
      <UsersInventory
        authUserId={auth.session?.user?.id}
        busyUserId={busyUserId}
        filters={filters}
        isError={usersQuery.isError}
        isFetching={usersQuery.isFetching}
        isLoading={usersQuery.isLoading}
        onConfirmAction={openConfirmation}
        onEdit={openEdit}
        onImmediateAction={(kind, user) =>
          ignoreAsyncResult(runImmediateAction(kind, user))
        }
        onPassword={openPassword}
        onRetry={() => ignoreAsyncResult(usersQuery.refetch())}
        rowMenu={rowMenu}
        setRowMenu={setRowMenu}
        total={total}
        updateFilters={updateFilters}
        users={users}
      />
      <UserEditorDialog
        authUserId={auth.session?.user?.id}
        editingUser={editingUser}
        editor={editor}
        editorError={editorError}
        editorMode={editorMode}
        onClose={closeEditor}
        onSubmit={(event) => ignoreAsyncResult(saveEditor(event))}
        saving={saving}
        setEditor={setEditor}
        toggleRole={toggleRole}
      />
      <TemporaryPasswordDialog
        onClose={() => setPasswordUser(null)}
        onSubmit={(event) => ignoreAsyncResult(saveTemporaryPassword(event))}
        passwordUser={passwordUser}
        saving={saving}
        setTemporaryPassword={setTemporaryPassword}
        setTemporaryRequireChange={setTemporaryRequireChange}
        temporaryPassword={temporaryPassword}
        temporaryRequireChange={temporaryRequireChange}
      />
      <ConfirmUserActionDialog
        confirmAction={confirmAction}
        onClose={closeConfirmation}
        onConfirm={() => ignoreAsyncResult(confirmUserAction())}
        saving={saving}
      />
    </section>
  );
}
