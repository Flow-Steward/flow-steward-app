import { useI18n } from "@/lib/i18n/useI18n";

import { ignoreAsyncResult } from "@/lib/utils/async";
import { type Dispatch, type SetStateAction, type RefObject } from "react";
import { TOKEN_SCOPES } from "@/pages/settings/shared/types";
import { todayIn, addDays, formatCalendarDay, wholeDaysBetween } from "@/lib/time/calendar-days";
import { DsRadio, DsAlert, OptionalLabel } from "@/components/shared/design-system";
import {
  copyValue,
  authorityIncludesScope,
  toggleStringSelection,
  type TokenExpiryMode,
  type TokenDrawerMode,
  customExpiryError,
  TOKEN_EXPIRY_MODES,
} from "./service-account-support";
import { CheckboxRow } from "./checkbox-row";

export function MachineTokenUsageExample({ secret }: Readonly<{ secret: string }>) {
  const { t } = useI18n();
  if (!secret.trim()) {
    return null;
  }

  const mcpEndpoint = `${globalThis.location.origin}/mcp`;
  const authorizationHeader = `Authorization: Bearer ${secret}`;

  return (
    <div className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4">
      <h3 className="fs-panel-title">{t("react.settings.use_from_ci_or_cron", "Use from CI or cron")}</h3>
      <code className="fs-value-chip">Authorization: Bearer $FLOW_STEWARD_TOKEN</code>
      <pre className="fs-log-output">
        {String.raw`export FLOW_STEWARD_TOKEN="${secret}"
curl -H "Authorization: Bearer $FLOW_STEWARD_TOKEN" \\
  -H "Content-Type: application/json" \\
  "$FLOW_STEWARD_URL/api/..."`}
      </pre>
      <div className="fs-panel-group">
        <div className="fs-panel-heading">
          <div>
            <h3 className="fs-panel-title">{t("react.settings.connect_an_mcp_client", "Connect an MCP client")}</h3>
            <p>{t("react.settings.streamable_http_endpoint_and_bearer_header_for_this_one_time_secret", "Streamable HTTP endpoint and bearer header for this one-time secret.")}</p>
          </div>
        </div>
        <code className="fs-value-chip fs-settings-wrap">{mcpEndpoint}</code>
        <div className="fs-footer-actions">
          <button className="fs-button" onClick={() => ignoreAsyncResult(copyValue(mcpEndpoint))} type="button">
            {t("react.settings.copy_mcp_endpoint", "Copy MCP endpoint")} </button>
        </div>
        <code className="fs-value-chip fs-settings-wrap">{authorizationHeader}</code>
        <div className="fs-footer-actions">
          <button className="fs-button" onClick={() => ignoreAsyncResult(copyValue(authorizationHeader))} type="button">
            {t("react.settings.copy_mcp_authorization_header", "Copy MCP authorization header")} </button>
        </div>
      </div>
    </div>
  );
}

function TokenScopesField({
  accountScopes,
  scopes,
  setScopes,
}: Readonly<{
  accountScopes: string[];
  scopes: string[];
  setScopes: Dispatch<SetStateAction<string[]>>;
}>) {
  const { t } = useI18n();
  return (
    <fieldset className="fs-field">
      <legend className="fs-field-label">{t("react.settings.token_scopes_none_selected_no_access", "Token scopes (none selected = no access)")}</legend>
      <div className="fs-scope-grid">
        {TOKEN_SCOPES.map((scope) => {
          const allowed = authorityIncludesScope(accountScopes, scope);
          return (
            <CheckboxRow
              checked={scopes.includes(scope)}
              disabled={!allowed}
              id={`machine-token-scope-${scope.replaceAll(".", "-").replaceAll(":", "-")}`}
              key={scope}
              name="machine_token_scopes"
              onChange={() => setScopes((current) => toggleStringSelection(current, scope))}
              value={scope}
            >
              {scope}
            </CheckboxRow>
          );
        })}
      </div>
      <span className="fs-field-helper">
        {t("react.settings.a_token_can_only_receive_scopes_already_granted_to_this_service_account_it_never_inherits_the_account_apos_s_a", "A token can only receive scopes already granted to this service account; it never inherits the account's authority. Search, security, system, settings and trash answer only a signed-in session or a token holding read:* or admin:all, which these choices do not grant.")} </span>
      {scopes.length ? null : (
        <span className="fs-field-helper fs-ink-attention" role="status">
          {t("react.settings.no_scope_selected_this_token_will_authenticate_and_every_api_call_it_makes_will_be_refused_select_what_the_cli", "No scope selected: this token will authenticate and every API call it makes will be refused. Select what the client needs.")} </span>
      )}
    </fieldset>
  );
}

export function MachineTokenForm({
  accountScopes,
  message,
  onCreate,
  onUpdate,
  setTokenExpiryDate,
  setTokenExpiryMode,
  setTokenExpiresError,
  setTokenLabelInput,
  setTokenScopesInput,
  timezone,
  tokenDrawerMode,
  tokenExpiryDate,
  tokenExpiryMode,
  tokenExpiresError,
  tokenExpiresRef,
  tokenLabelInput,
  tokenScopesInput,
}: Readonly<{
  accountScopes: string[];
  message: string;
  onCreate: () => void;
  onUpdate: () => void;
  setTokenExpiryDate: (value: string) => void;
  setTokenExpiryMode: (value: TokenExpiryMode) => void;
  setTokenExpiresError: (value: string) => void;
  setTokenLabelInput: (value: string) => void;
  setTokenScopesInput: React.Dispatch<React.SetStateAction<string[]>>;
  timezone: string;
  tokenDrawerMode: TokenDrawerMode;
  tokenExpiryDate: string;
  tokenExpiryMode: TokenExpiryMode;
  tokenExpiresError: string;
  tokenExpiresRef: RefObject<HTMLInputElement | null>;
  tokenLabelInput: string;
  tokenScopesInput: string[];
}>) {
  const { t } = useI18n();
  const creating = tokenDrawerMode === "create";
  const today = todayIn(timezone);
  const earliest = addDays(today, 1);
  const pastDateError = tokenExpiryDate ? customExpiryError(tokenExpiryDate, today, t) : "";
  const expiryError = tokenExpiresError || pastDateError;
  const expiryHelper = tokenExpiryDate && !pastDateError
    ? t("react.settings.token_expiry_date_days_hint", "Expires on {date}, {days} days from today.", { date: formatCalendarDay(tokenExpiryDate), days: wholeDaysBetween(today, tokenExpiryDate) })
    : t("react.settings.token_expiry_future_day_hint", "Must be a future date. The earliest allowed day is tomorrow.");
  return (
    <form
      className="fs-panel-group"
      id="machine-token-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        if (creating) {
          onCreate();
        } else {
          onUpdate();
        }
      }}
    >
      <label className="fs-field">
        <OptionalLabel id="service-account-token-label-name">{t("react.settings.machine_token_label", "Machine token label")}</OptionalLabel>
        <input
          aria-labelledby="service-account-token-label-name"
          className="fs-input"
          id="service-account-token-label"
          name="service_account_token_label"
          onChange={(event) => setTokenLabelInput(event.target.value)}
          value={tokenLabelInput}
        />
      </label>
      {creating ? (
        // Machine credentials cannot be endless, so the canon expiry choice
        // drops its "No expiry" branch and the choice is always made.
        <fieldset className="fs-field">
          <legend className="fs-field-label">{t("react.settings.expiration", "Expiration")}</legend>
          <div className="fs-radio-grid">
            {TOKEN_EXPIRY_MODES.map((mode) => (
              <DsRadio
                checked={tokenExpiryMode === mode}
                key={mode}
                name="service_account_token_expiry"
                onChange={() => {
                  setTokenExpiryMode(mode);
                  setTokenExpiresError("");
                }}
                value={mode}
              >
                {mode === "custom" ? t("project_detail.schedule.custom", "Custom") : t("react.settings.value1_days", "{value1} days", { value1: String(mode) })}
              </DsRadio>
            ))}
          </div>
          {tokenExpiryMode === "custom" ? (
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="service-account-token-expires-on">{t("react.settings.expires_on", "Expires on")}</label>
              <input
                aria-describedby="machine-token-expires-helper"
                aria-invalid={Boolean(expiryError)}
                className="fs-input"
                id="service-account-token-expires-on"
                min={earliest}
                name="service_account_token_expires_on"
                onChange={(event) => {
                  setTokenExpiryDate(event.target.value);
                  setTokenExpiresError("");
                }}
                ref={tokenExpiresRef}
                type="date"
                value={tokenExpiryDate}
              />
              <span
                className={expiryError ? "fs-field-helper error" : "fs-field-helper"}
                id="machine-token-expires-helper"
                role={expiryError ? "alert" : undefined}
              >
                {expiryError || expiryHelper}
              </span>
            </div>
          ) : null}
        </fieldset>
      ) : null}
      <TokenScopesField accountScopes={accountScopes} scopes={tokenScopesInput} setScopes={setTokenScopesInput} />
      {message ? <DsAlert title={message} /> : null}
    </form>
  );
}
