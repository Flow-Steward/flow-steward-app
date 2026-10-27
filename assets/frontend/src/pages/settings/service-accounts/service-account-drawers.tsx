import { useI18n } from "@/lib/i18n/useI18n";
import { DsCard, DsBadge, DsAlert } from "@/components/shared/design-system";
import { Plus, X, Ban, Trash2, Save } from "lucide-react";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { formatUserDateTime } from "@/lib/time/user-timezone";
import { type ReactNode } from "react";
import { AccessibleDrawer } from "@/pages/settings/service-accounts/accessible-drawer";
import { OneTimeTokenSecret } from "@/pages/settings/shared/one-time-token-secret";
import {
  type EntityRecord,
  serviceAccountKey,
  toCsv,
  tokenId,
  toStringList,
  type AccountDrawerMode,
  type TokenDrawerMode,
} from "./service-account-support";
import { MachineTokenUsageExample } from "./machine-token-form";

export function ServiceAccountsListCard({
  accounts,
  isLoading,
  onCreate,
  onSelect,
  saving,
  selectedKey,
}: Readonly<{
  accounts: EntityRecord[];
  isLoading: boolean;
  onCreate: () => void;
  onSelect: (account: EntityRecord) => void;
  saving: boolean;
  selectedKey: string;
}>) {
  const { t } = useI18n();
  return (
    <DsCard>
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-card-title" id="service-accounts-heading" tabIndex={-1}>
            {t("react.settings.service_accounts_2", "Service accounts")} </h2>
          <p className="fs-card-description">{t("react.settings.each_service_account_is_an_identity_credentials_live_inside_it", "Each service account is an identity; credentials live inside it.")}</p>
        </div>
        <button className="fs-button primary" disabled={saving} onClick={onCreate} type="button">
          <Plus aria-hidden="true" />
          {t("react.settings.new_service_account", "New service account")} </button>
      </div>
      {accounts.length ? (
        <ul className="fs-settings-list">
          {accounts.map((account) => {
            const key = serviceAccountKey(account.service_account_key);
            const active = Boolean(account.is_active);
            return (
              <li key={key || JSON.stringify(account)}>
                <button
                  className="fs-settings-list-item"
                  data-selected={key === selectedKey || undefined}
                  id={`service-account-row-${key}`}
                  onClick={() => onSelect(account)}
                  type="button"
                >
                  <span className="fs-settings-list-head">
                    <strong>{firstStringFromUnknownValue([account.display_name, key])}</strong>
                    <DsBadge tone={active ? "green" : "neutral"}>{active ? t("account_settings.status.active", "Active") : t("account_settings.status.disabled", "Disabled")}</DsBadge>
                  </span>
                  <span className="fs-settings-list-meta">{key || t("react.settings.missing_key", "(missing key)")}</span>
                  <span className="fs-settings-list-meta">{t("react.settings.scopes_2", "scopes:")} {toCsv(account.scopes) || "none"}</span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
      {!isLoading && !accounts.length ? (
        <div className="fs-table-state">
          <strong>{t("react.settings.no_service_accounts_found", "No service accounts found.")}</strong>
          <span>{t("react.settings.create_an_identity_then_issue_machine_tokens_inside_it", "Create an identity, then issue machine tokens inside it.")}</span>
        </div>
      ) : null}
    </DsCard>
  );
}

export function ServiceAccountCredentials({
  accountActive,
  accountKey,
  isLoading,
  onCreate,
  onSelect,
  saving,
  timezone,
  tokens,
}: Readonly<{
  accountActive: boolean;
  accountKey: string;
  isLoading: boolean;
  onCreate: () => void;
  onSelect: (token: EntityRecord) => void;
  saving: boolean;
  timezone: string;
  tokens: EntityRecord[];
}>) {
  const { t } = useI18n();
  return (
    <section className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h3 className="fs-panel-title" id="service-account-credentials-heading" tabIndex={-1}>
            {t("react.settings.credentials", "Credentials")} </h3>
          <p>{t("react.settings.machine_tokens_issued_for_this_identity", "Machine tokens issued for this identity.")}</p>
        </div>
        <button
          className="fs-button"
          disabled={!accountKey || saving || !accountActive}
          onClick={onCreate}
          type="button"
        >
          <Plus aria-hidden="true" />
          {t("react.settings.new_machine_token", "New machine token")} </button>
      </div>
      {tokens.length ? (
        <ul className="fs-settings-list">
          {tokens.map((token) => {
            const currentTokenId = tokenId(token.id ?? token.token_id);
            const revoked = Boolean(token.revoked);
            const scopes = toStringList(token.scopes);
            return (
              <li key={currentTokenId || JSON.stringify(token)}>
                <button
                  className="fs-settings-list-item"
                  id={`machine-token-row-${currentTokenId}`}
                  onClick={() => onSelect(token)}
                  type="button"
                >
                  <span className="fs-settings-list-head">
                    <strong>{firstStringFromUnknownValue([token.label, token.name]).trim() || t("react.settings.unnamed_token", "Unnamed token")}</strong>
                    {revoked ? <DsBadge tone="orange">{t("react.settings.revoked_lowercase", "revoked")}</DsBadge> : null}
                  </span>
                  <span className="fs-settings-list-meta">
                    {t("react.settings.expires", "expires:")}{" "}
                    {token.expires_at ? formatUserDateTime(token.expires_at, timezone) : "never"}
                  </span>
                  {scopes.length ? (
                    <span className="fs-settings-list-meta">{t("react.settings.scopes_2", "scopes:")} {scopes.join(", ")}</span>
                  ) : (
                    <span className="fs-settings-list-meta fs-ink-attention">{t("react.settings.scopes_none_no_access", "scopes: none (no access)")}</span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
      {!isLoading && !tokens.length ? (
        <p className="fs-empty-copy">{t("react.settings.no_machine_tokens_issued_for_this_identity", "No machine tokens issued for this identity.")}</p>
      ) : null}
    </section>
  );
}

export function ServiceAccountDrawer({
  accountActive,
  accountForm,
  accountKey,
  accountName,
  credentials,
  message,
  mode,
  nestedDrawerOpen,
  onClose,
  onDelete,
  onToggleActive,
  saving,
  suspended,
}: Readonly<{
  accountActive: boolean;
  accountForm: ReactNode;
  accountKey: string;
  accountName: string;
  credentials: ReactNode;
  message: string;
  mode: AccountDrawerMode;
  nestedDrawerOpen: boolean;
  onClose: () => void;
  onDelete: () => void;
  onToggleActive: () => void;
  saving: boolean;
  suspended: boolean;
}>) {
  const { t } = useI18n();
  if (mode === "closed" || typeof document === "undefined") {
    return null;
  }
  const detailMode = mode === "detail";
  return (
    <AccessibleDrawer
      active={!nestedDrawerOpen}
      ariaLabel={t("react.settings.service_account_drawer", "Service account drawer")}
      closeLabel={t("react.settings.close_service_account_drawer", "Close service account drawer")}
      onClose={onClose}
      panelClassName="fs-settings-drawer overflow-hidden"
      size="wide"
      suspended={suspended}
    >
      <div className="fs-settings-drawer-head">
        <div>
          <span className="fs-drawer-label">{t("react.settings.service_account", "Service account")}</span>
          <h2 className="fs-drawer-title">{detailMode ? accountName : t("react.settings.create_service_account", "Create service account")}</h2>
          <p className="fs-card-description">
            {detailMode
              ? t("react.settings.identity_key_value1", "Identity key: {value1}", { value1: String(accountKey || "n/a") })
              : t("react.settings.create_the_identity_first_then_issue_credentials_inside_it", "Create the identity first, then issue credentials inside it.")}
          </p>
        </div>
        <button aria-label={t("react.settings.close_service_account_drawer", "Close service account drawer")} className="fs-drawer-close" onClick={onClose} type="button">
          <X aria-hidden="true" />
        </button>
      </div>
      <div
        className="fs-settings-drawer-body min-w-0 flex-1 overflow-x-hidden overflow-y-auto"
        data-testid="service-account-drawer-scroll-region"
      >
        {!nestedDrawerOpen ? accountForm : null}
        {detailMode ? credentials : null}
        {message ? <DsAlert title={message} /> : null}
      </div>
      <div className="fs-settings-drawer-foot">
        {detailMode ? (
          <>
            <button className="fs-button" disabled={saving || !accountKey} onClick={onToggleActive} type="button">
              <Ban aria-hidden="true" />
              {accountActive ? t("react.settings.disable_identity", "Disable identity") : t("react.settings.enable_identity", "Enable identity")}
            </button>
            <button className="fs-button ghost-danger" disabled={saving || !accountKey} onClick={onDelete} type="button">
              <Trash2 aria-hidden="true" />
              {t("projects.grid.action.delete", "Delete")} </button>
            <button className="fs-button primary" disabled={saving || !accountKey} form="service-account-form" type="submit">
              <Save aria-hidden="true" />
              {saving ? t("project_edit.security.saving", "Saving...") : t("react.settings.save_identity", "Save identity")}
            </button>
          </>
        ) : (
          <button className="fs-button primary" disabled={saving} form="service-account-form" type="submit">
            <Plus aria-hidden="true" />
            {saving ? t("project_edit.security.saving", "Saving...") : t("react.settings.create_service_account", "Create service account")}
          </button>
        )}
      </div>
    </AccessibleDrawer>
  );
}

function MachineTokenMetadata({
  revoked,
  timezone,
  token,
  tokenIdValue,
}: Readonly<{
  revoked: boolean;
  timezone: string;
  token: EntityRecord | undefined;
  tokenIdValue: string;
}>) {
  const { t } = useI18n();
  return (
    <div className="fs-detail-rows" data-columns="2">
      <div className="fs-detail-row">
        <span>{t("react.settings.token_id", "Token ID")}</span>
        <span className="fs-detail-value mono">{tokenIdValue || t("monitoring.na", "n/a")}</span>
      </div>
      <div className="fs-detail-row">
        <span>{t("account_settings.card.status", "Status")}</span>
        <span className="fs-detail-value">
          <DsBadge tone={revoked ? "orange" : "green"}>{revoked ? t("react.settings.revoked", "Revoked") : t("account_settings.status.active", "Active")}</DsBadge>
        </span>
      </div>
      <div className="fs-detail-row">
        <span>{t("account.tokens.expires", "Expires")}</span>
        <span className="fs-detail-value">
          {token?.expires_at ? formatUserDateTime(token.expires_at, timezone) : t("account.tokens.never", "Never")}
        </span>
      </div>
      <div className="fs-detail-row">
        <span>{t("react.settings.last_used", "Last used")}</span>
        <span className="fs-detail-value">
          {token?.last_used_at ? formatUserDateTime(token.last_used_at, timezone) : t("react.settings.no_activity", "No activity")}
        </span>
      </div>
    </div>
  );
}

export function MachineTokenDrawer({
  accountKey,
  form,
  mode,
  newTokenLabel,
  newTokenSecret,
  onClose,
  onDelete,
  onHideSecret,
  onRevoke,
  revoked,
  saving,
  selectedToken,
  selectedTokenId,
  suspended,
  timezone,
  tokenLabel,
}: Readonly<{
  accountKey: string;
  form: ReactNode;
  mode: TokenDrawerMode;
  newTokenLabel: string;
  newTokenSecret: string;
  onClose: () => void;
  onDelete: () => void;
  onHideSecret: () => void;
  onRevoke: () => void;
  revoked: boolean;
  saving: boolean;
  selectedToken: EntityRecord | undefined;
  selectedTokenId: string;
  suspended: boolean;
  timezone: string;
  tokenLabel: string;
}>) {
  const { t } = useI18n();
  if (mode === "closed" || typeof document === "undefined") {
    return null;
  }
  const editMode = mode === "edit";
  const actionDisabled = !accountKey || !selectedTokenId || saving;
  return (
    <AccessibleDrawer
      ariaLabel={t("react.settings.machine_token_drawer", "Machine token drawer")}
      closeLabel={t("react.settings.close_machine_token_drawer", "Close machine token drawer")}
      onClose={onClose}
      panelClassName="fs-settings-drawer"
      suspended={suspended}
    >
      <div className="fs-settings-drawer-head">
        <div>
          <span className="fs-drawer-label">{t("react.settings.machine_token", "Machine token")}</span>
          <h2 className="fs-drawer-title">{editMode ? tokenLabel || t("react.settings.machine_token", "Machine token") : t("react.settings.create_machine_token", "Create machine token")}</h2>
          <p className="fs-card-description">
            {accountKey ? t("react.settings.service_account_value1", "Service account: {value1}", { value1: String(accountKey) }) : t("react.settings.no_service_account_selected", "No service account selected.")}
          </p>
        </div>
        <button aria-label={t("react.settings.close_machine_token_drawer", "Close machine token drawer")} className="fs-drawer-close" onClick={onClose} type="button">
          <X aria-hidden="true" />
        </button>
      </div>
      <div className="fs-settings-drawer-body flex-1 overflow-y-auto">
        {editMode ? (
          <MachineTokenMetadata revoked={revoked} timezone={timezone} token={selectedToken} tokenIdValue={selectedTokenId} />
        ) : null}
        {form}
        <OneTimeTokenSecret label={newTokenLabel} onHide={onHideSecret} secret={newTokenSecret} />
        <MachineTokenUsageExample secret={newTokenSecret} />
      </div>
      <div className="fs-settings-drawer-foot">
        {editMode ? (
          <>
            <button className="fs-button" disabled={actionDisabled || revoked} onClick={onRevoke} type="button">
              <Ban aria-hidden="true" />
              {t("account.tokens.revoke", "Revoke")} </button>
            <button
              className="fs-button ghost-danger"
              disabled={actionDisabled}
              id="machine-token-delete-action"
              onClick={onDelete}
              type="button"
            >
              <Trash2 aria-hidden="true" />
              {t("projects.grid.action.delete", "Delete")} </button>
            <button className="fs-button primary" disabled={actionDisabled || revoked} form="machine-token-form" type="submit">
              <Save aria-hidden="true" />
              {saving ? t("project_edit.security.saving", "Saving...") : t("react.settings.save_token", "Save token")}
            </button>
          </>
        ) : (
          <button className="fs-button primary" disabled={!accountKey || saving} form="machine-token-form" type="submit">
            <Plus aria-hidden="true" />
            {saving ? t("project_edit.security.saving", "Saving...") : t("react.settings.create_machine_token", "Create machine token")}
          </button>
        )}
      </div>
    </AccessibleDrawer>
  );
}
