import type { Dispatch, SetStateAction } from "react";
import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { DsAlert, DsCheckbox, DsDialog, RequiredLabel, SensitiveInput } from "@/components/shared/design-system";
import type { UserRecord } from "@/pages/settings/shared/types";
import { ROLE_OPTIONS, userLabel, type EditorState, type EditorMode, type ConfirmActionKind } from "./user-form-model";

function userEditorSubmitLabel(saving: boolean, addingUser: boolean, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (saving) {
    return t("react.settings.saving", "Saving…");
  }
  return addingUser ? t("react.settings.add_user", "Add user") : t("react.settings.save_changes", "Save changes");
}

function confirmUserActionLabel(saving: boolean, kind: ConfirmActionKind, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (saving) {
    return t("react.settings.updating", "Updating…");
  }
  if (kind === "reset") return t("react.settings.require_reset", "Require reset");
  return kind === "delete" ? t("react.settings.delete_user", "Delete user") : t("react.settings.deactivate_user", "Deactivate user");
}

function UserIdentityFields({
  addingUser,
  editor,
  setEditor,
}: Readonly<{
  addingUser: boolean;
  editor: EditorState;
  setEditor: Dispatch<SetStateAction<EditorState>>;
}>) {
  const { t } = useI18n();
  return (
    <>
      <div className="fs-form-grid">
        <label className="fs-field">
          <RequiredLabel>{t("react.settings.display_name", "Display name")}</RequiredLabel>
          <input
            autoComplete="name"
            className="fs-input"
            id="user-display-name"
            name="display-name"
            onChange={(event) =>
              setEditor((value) => ({
                ...value,
                displayName: event.target.value,
              }))
            }
            placeholder={t("react.settings.e_g_alex_morgan", "e.g. Alex Morgan")}
            required
            value={editor.displayName}
          />
        </label>
        <label className="fs-field">
          <RequiredLabel>{t("react.settings.email_address", "Email address")}</RequiredLabel>
          <input
            autoComplete="email"
            className="fs-input"
            id="user-email"
            name="email"
            onChange={(event) =>
              setEditor((value) => ({ ...value, email: event.target.value }))
            }
            readOnly={!addingUser}
            required
            type="email"
            value={editor.email}
          />
          {!addingUser ? (
            <span className="fs-field-helper">
              {t("react.settings.email_editing_is_not_supported_here", "Email editing is not supported here.")} </span>
          ) : null}
        </label>
      </div>
      {addingUser ? (
        <div className="fs-form-grid">
          <div className="fs-field">
            <RequiredLabel>{t("react.settings.temporary_password", "Temporary password")}</RequiredLabel>
            <SensitiveInput
              ariaLabel={t("react.settings.temporary_password", "Temporary password")}
              autoComplete="off"
              minLength={10}
              name="temporary-password"
              onChange={(event) =>
                setEditor((value) => ({
                  ...value,
                  password: event.target.value,
                }))
              }
              required
              value={editor.password}
            />
            <span className="fs-field-helper">{t("react.settings.use_10_or_more_characters", "Use 10 or more characters.")}</span>
          </div>
        </div>
      ) : null}
    </>
  );
}

export function UserEditorDialog({
  authUserId,
  editingUser,
  editor,
  editorError,
  editorMode,
  onClose,
  onSubmit,
  saving,
  setEditor,
  toggleRole,
}: Readonly<{
  authUserId?: string;
  editingUser: UserRecord | null;
  editor: EditorState;
  editorError: string;
  editorMode: EditorMode;
  onClose: () => void;
  onSubmit: (event: React.SubmitEvent<HTMLFormElement>) => void;
  saving: boolean;
  setEditor: Dispatch<SetStateAction<EditorState>>;
  toggleRole: (role: string, checked: boolean) => void;
}>) {
  const { t } = useI18n();
  const addingUser = editorMode === "add";
  const editingUserLabel = editingUser ? userLabel(editingUser) : "user";
  return (
    <DsDialog
      busy={saving}
      closeLabel={
        addingUser ? t("react.settings.close_add_user_dialog", "Close add user dialog") : t("react.settings.close_edit_access_dialog", "Close edit access dialog")
      }
      description={
        addingUser
          ? t("react.settings.provision_a_user_directly_and_define_their_initial_access", "Provision a user directly and define their initial access.")
          : t("react.settings.update_roles_and_sign_in_controls", "Update roles and sign-in controls.")
      }
      eyebrow={t("react.settings.users_access", "Users & access")}
      footer={
        <>
          <button
            className="fs-button"
            disabled={saving}
            onClick={onClose}
            type="button"
          >
            {t("common.cancel", "Cancel")} </button>
          <button
            className="fs-button primary"
            disabled={saving}
            form="user-editor"
            type="submit"
          >
            {userEditorSubmitLabel(saving, addingUser, t)}
          </button>
        </>
      }
      onClose={onClose}
      open={editorMode !== "closed"}
      title={addingUser ? t("react.settings.add_user", "Add user") : t("react.settings.edit_access_for_value1", "Edit access for {value1}", { value1: String(editingUserLabel) })}
      variant={addingUser ? "add-user" : "edit-user"}
    >
      <form
        aria-label={addingUser ? t("react.settings.add_user_form", "Add user form") : t("react.settings.edit_user_form", "Edit user form")}
        autoComplete="off"
        className="fs-dialog-form"
        id="user-editor"
        name={addingUser ? "add-user" : "edit-user"}
        noValidate
        onSubmit={onSubmit}
      >
        <UserIdentityFields
          addingUser={addingUser}
          editor={editor}
          setEditor={setEditor}
        />
        <fieldset className="fs-field">
          <legend className="fs-field-label fs-required-label">
            {t("account.profile.roles", "Roles")}<span aria-hidden="true" className="fs-required">*</span>
          </legend>
          <div className="fs-role-grid">
            {ROLE_OPTIONS.map((role) => (
              <DsCheckbox
                checked={editor.roles.includes(role)}
                key={role}
                onChange={(checked) => toggleRole(role, checked)}
              >
                {role[0]?.toUpperCase() + role.slice(1)}
              </DsCheckbox>
            ))}
          </div>
          {editingUser?.id === authUserId ? (
            <span className="fs-field-helper">
              {t("react.settings.changing_your_own_administrator_role_can_remove_access_to_this_page", "Changing your own administrator role can remove access to this page.")} </span>
          ) : null}
        </fieldset>
        <fieldset className="fs-field">
          <legend className="fs-field-label">{t("react.settings.initial_controls", "Initial controls")}</legend>
          <div className="fs-control-grid">
            <DsCheckbox
              checked={editor.active}
              onChange={(active) =>
                setEditor((value) => ({ ...value, active }))
              }
            >
              <strong>{t("account_settings.status.active", "Active")}</strong>
              <small>{t("react.settings.user_can_sign_in_immediately", "User can sign in immediately.")}</small>
            </DsCheckbox>
            <DsCheckbox
              checked={editor.mustChangePassword}
              onChange={(mustChangePassword) =>
                setEditor((value) => ({ ...value, mustChangePassword }))
              }
            >
              <strong>{t("react.settings.require_password_change", "Require password change")}</strong>
              <small>
                {t("react.settings.user_must_replace_the_temporary_password_at_next_sign_in", "User must replace the temporary password at next sign-in.")} </small>
            </DsCheckbox>
          </div>
        </fieldset>
        {editorError ? (
          <DsAlert
            tone="danger"
            title={
              addingUser ? t("react.settings.user_could_not_be_added", "User could not be added") : t("react.settings.user_could_not_be_updated", "User could not be updated")
            }
          >
            {editorError}
          </DsAlert>
        ) : null}
      </form>
    </DsDialog>
  );
}

export function TemporaryPasswordDialog({
  onClose,
  onSubmit,
  passwordUser,
  saving,
  setTemporaryPassword,
  setTemporaryRequireChange,
  temporaryPassword,
  temporaryRequireChange,
}: Readonly<{
  onClose: () => void;
  onSubmit: (event: React.SubmitEvent<HTMLFormElement>) => void;
  passwordUser: UserRecord | null;
  saving: boolean;
  setTemporaryPassword: (value: string) => void;
  setTemporaryRequireChange: (value: boolean) => void;
  temporaryPassword: string;
  temporaryRequireChange: boolean;
}>) {
  const { t } = useI18n();
  return (
    <DsDialog
      busy={saving}
      closeLabel={t("react.settings.close_temporary_password_dialog", "Close temporary password dialog")}
      description={t("react.settings.the_user_will_receive_this_password_through_your_existing_provisioning_process", "The user will receive this password through your existing provisioning process.")}
      eyebrow={t("react.settings.sign_in", "Sign-in")}
      footer={
        <>
          <button
            className="fs-button"
            disabled={saving}
            onClick={onClose}
            type="button"
          >
            {t("common.cancel", "Cancel")} </button>
          <button
            className="fs-button primary"
            disabled={saving}
            form="temporary-password-form"
            type="submit"
          >
            {saving ? t("react.settings.saving", "Saving…") : t("react.settings.set_password", "Set password")}
          </button>
        </>
      }
      onClose={onClose}
      open={Boolean(passwordUser)}
      title={t("react.settings.set_temporary_password_for_value1", "Set temporary password for {value1}", { value1: String(passwordUser ? userLabel(passwordUser) : "user") })}
    >
      <form
        aria-label={t("react.settings.set_temporary_password_form", "Set temporary password form")}
        autoComplete="off"
        className="fs-dialog-form"
        id="temporary-password-form"
        name="set-temporary-password"
        noValidate
        onSubmit={onSubmit}
      >
        <div className="fs-field">
          <RequiredLabel>{t("react.settings.temporary_password", "Temporary password")}</RequiredLabel>
          <SensitiveInput
            ariaLabel={t("react.settings.temporary_password", "Temporary password")}
            autoComplete="off"
            minLength={10}
            name="temporary-password"
            onChange={(event) => setTemporaryPassword(event.target.value)}
            required
            value={temporaryPassword}
          />
          <span className="fs-field-helper">{t("react.settings.use_10_or_more_characters", "Use 10 or more characters.")}</span>
        </div>
        <DsCheckbox
          checked={temporaryRequireChange}
          onChange={setTemporaryRequireChange}
        >
          {t("react.settings.require_password_change_at_next_sign_in", "Require password change at next sign-in")} </DsCheckbox>
      </form>
    </DsDialog>
  );
}

export function ConfirmUserActionDialog({
  confirmAction,
  onClose,
  onConfirm,
  saving,
}: Readonly<{
  confirmAction: { kind: ConfirmActionKind; user: UserRecord } | null;
  onClose: () => void;
  onConfirm: () => void;
  saving: boolean;
}>) {
  const { t } = useI18n();
  const reset = confirmAction?.kind === "reset";
  const deleting = confirmAction?.kind === "delete";
  const label = confirmAction ? userLabel(confirmAction.user) : "user";
  const firstName = confirmAction ? label.split(" ")[0] : "The user";
  return (
    <DsDialog
      busy={saving}
      closeLabel={
        reset
          ? t("react.settings.close_password_reset_confirmation", "Close password reset confirmation")
          : deleting
            ? t("react.settings.close_delete_user_confirmation", "Close delete user confirmation")
            : t("react.settings.close_deactivate_user_confirmation", "Close deactivate user confirmation")
      }
      description={
        reset
          ? t("react.settings.value1_must_choose_a_new_password_at_next_sign_in_other_active_sessions_will_be_revoked", "{value1} must choose a new password at next sign-in. Other active sessions will be revoked.", { value1: String(firstName) })
          : deleting
            ? t("react.settings.value1_will_be_removed_from_the_user_inventory_historical_workflow_job_and_audit_records_are_retained", "{value1} will be removed from the user inventory. Historical workflow, job, and audit records are retained.", { value1: String(firstName) })
            : t("react.settings.value1_will_no_longer_be_able_to_sign_in_existing_roles_are_retained", "{value1} will no longer be able to sign in. Existing roles are retained.", { value1: String(firstName) })
      }
      eyebrow={reset ? t("react.settings.sign_in", "Sign-in") : deleting ? t("react.settings.delete_user", "Delete user") : t("react.settings.user_status", "User status")}
      footer={
        <>
          <button
            className="fs-button"
            disabled={saving}
            onClick={onClose}
            type="button"
          >
            {t("common.cancel", "Cancel")} </button>
          <button
            className={`fs-button ${reset ? "primary" : "danger"}`}
            disabled={saving}
            onClick={onConfirm}
            type="button"
          >
            {confirmUserActionLabel(saving, confirmAction?.kind || "deactivate", t)}
          </button>
        </>
      }
      onClose={onClose}
      open={Boolean(confirmAction)}
      title={reset ? t("react.settings.require_a_password_reset_for_value1", "Require a password reset for {value1}?", { value1: String(label) }) : deleting ? t("react.settings.delete_value1", "Delete {value1}?", { value1: String(label) }) : t("react.settings.deactivate_value1", "Deactivate {value1}?", { value1: String(label) })}
      variant="confirm"
    >
      <DsAlert
        tone={reset ? "warning" : "danger"}
        title={
          reset
            ? t("react.settings.all_other_sessions_will_end", "All other sessions will end.")
            : deleting
              ? t("react.settings.active_sessions_and_personal_access_tokens_will_be_revoked", "Active sessions and personal access tokens will be revoked.")
              : t("react.settings.sign_in_access_stops_immediately", "Sign-in access stops immediately.")
        }
      >
        {reset
          ? t("react.settings.keep_this_user_informed_before_requiring_the_reset", "Keep this user informed before requiring the reset.")
          : deleting
            ? t("react.settings.this_action_cannot_be_reversed_from_the_user_inventory", "This action cannot be reversed from the user inventory.")
            : t("react.settings.if_this_is_your_own_account_you_may_lose_access_to_this_page", "If this is your own account, you may lose access to this page.")}
      </DsAlert>
    </DsDialog>
  );
}
