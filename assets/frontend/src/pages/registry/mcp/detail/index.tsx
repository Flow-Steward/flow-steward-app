import { useI18n } from "@/lib/i18n/useI18n";
import { mcpStatusLabel } from "../mcp-status-label";
import { type ReactNode } from "react";

import { Copy, ExternalLink, RefreshCw, Unplug } from "lucide-react";

import { Link } from "react-router";

import { AssignmentSelector } from "@/components/shared/assignment-selector";

import {
  DsAlert,
  DsBadge,
  DsCard,
  DsCheckbox,
  DsDialog,
  DsPageHeader,
  DsSelect,
  PageContainer,
  SensitiveInput,
} from "@/components/shared/design-system";

import { ignoreAsyncResult } from "@/lib/utils/async";

import {
  choose,
  renderWhen,
  renderChoice,
  anyTrue,
  allTrue,
  mcpListRoute,
  storedHeadersMessage,
  oauthStartLabel,
  formatDateTime,
  type McpSettingsDraft,
  patchSettingsDraft,
  updateRequestedScope,
  removeRequestedScope,
  updateSettingsAuthMode,
  updateReplacementHeaders,
  updateStoredHeadersMode,
  RESERVED_OAUTH_PARAMETER_IDENTITIES,
} from "./mcp-detail-page-helpers";
import { Fact, KeyValueEditor } from "./mcp-detail-page-components";
import { useMcpDetailState } from "./mcp-detail-state";

export function RegistryMcpDetailPage() {
  const { t } = useI18n();
  const {
    projectId,
    connectorMissing,
    connectorQuery,
    refresh,
    canManageProject,
    syncMutation,
    healthMutation,
    connector,
    openSettings,
    connectorId,
    message,
    messageTone,
    settingsOpen,
    settingsDraft,
    setSettingsOpen,
    saveSettingsMutation,
    settingsNameRef,
    submitSettings,
    settingsErrorField,
    setSettingsDraft,
    settingsEndpointRef,
    settingsRequestedScopesRef,
    requestedScopeRows,
    requestedScopeKeyState,
    settingsAuthorizationParametersRef,
    settingsRefreshTokenRequiredRef,
    settingsHeadersRef,
    settingsError,
    authModeIsOauth,
    oauthStatus,
    copyOAuthRedirectUri,
    requestedScopes,
    hasInsufficientScope,
    credentialsOpen,
    resumeAuthorizationAfterCredentials,
    credentialsErrorField,
    setManualClientId,
    setCredentialsErrorField,
    credentialsClientIdRef,
    manualClientId,
    credentialsClientSecretRef,
    setManualClientSecret,
    manualClientSecret,
    credentialsError,
    saveCredentialsMutation,
    submitCredentials,
    hasAuthorizedAccount,
    authorizationMustRestart,
    startOAuthMutation,
    oauthStatusQuery,
    disconnectOAuthMutation,
    hasStoredClientCredentials,
    setResumeAuthorizationAfterCredentials,
    setCredentialsError,
    setCredentialsOpen,
    remoteToolsMessage,
    saveAllowlistMutation,
    setSearchValue,
    toggleSelectedTool,
    selectorRows,
    searchValue,
  } = useMcpDetailState();

  let pageBody: ReactNode;
  if (!projectId) {
    pageBody = (
      <section aria-labelledby="mcp-connector-project-required-title" className="fs-page-sections">
        <Link className="fs-link" to="/registry/mcp">{t("react.registry.back_to_mcp", "Back to MCP")}</Link>
        <DsCard testId="mcp-connector-project-required">
          <div className="fs-table-state" role="alert">
            <h2 className="fs-card-title" id="mcp-connector-project-required-title">{t("react.registry.select_a_project_first", "Select a project first")}</h2>
            <span>{t("react.registry.mcp_connector_details_are_scoped_to_an_active_project", "MCP connector details are scoped to an active project.")}</span>
          </div>
        </DsCard>
      </section>
    );
  } else if (connectorMissing) {
    pageBody = (
      <section aria-labelledby="mcp-connector-not-found-title" className="fs-page-sections">
        <Link className="fs-link" to={mcpListRoute(projectId)}>
          {t("react.registry.back_to_mcp", "Back to MCP")}</Link>
        <DsCard testId="mcp-connector-not-found">
          <div className="fs-table-state" role="alert">
            <h2 className="fs-card-title" id="mcp-connector-not-found-title">{t("react.registry.mcp_connector_not_found", "MCP connector not found")}</h2>
            <span>{t("react.registry.this_connector_may_have_been_deleted_or_purged_or_you_may_not_have_access_to_it", "This connector may have been deleted or purged, or you may not have access to it.")}</span>
          </div>
        </DsCard>
      </section>
    );
  } else if (connectorQuery.isError) {
    pageBody = (
      <section aria-labelledby="mcp-connector-load-failed-title" className="fs-page-sections">
        <Link className="fs-link" to={mcpListRoute(projectId)}>
          {t("react.registry.back_to_mcp", "Back to MCP")}</Link>
        <DsCard testId="mcp-connector-load-failed">
          <div className="fs-table-state" role="alert">
            <h2 className="fs-card-title" id="mcp-connector-load-failed-title">{t("react.registry.mcp_connector_could_not_be_loaded", "MCP connector could not be loaded")}</h2>
            <span>{t("react.registry.check_the_connection_and_try_again", "Check the connection and try again.")}</span>
            <button
              className="fs-button"
              onClick={() => ignoreAsyncResult(connectorQuery.refetch())}
              type="button"
            >
              {t("settings.watchdog.retry", "Retry")}</button>
          </div>
        </DsCard>
      </section>
    );
  } else {
    pageBody = (
    <section className="fs-page-sections">
      <Link className="fs-link" to={mcpListRoute(projectId)}>
        {t("react.registry.back_to_mcp", "Back to MCP")}</Link>

      <DsPageHeader
        action={
          <div className="flex flex-wrap items-center justify-end gap-2">
            <button
              className="fs-button secondary"
              onClick={() => ignoreAsyncResult(refresh())}
              type="button"
            >
              <RefreshCw aria-hidden="true" />
              {t("trash.refresh", "Refresh")}</button>
            {renderWhen(canManageProject, (
              <>
                <button
                  className="fs-button secondary"
                  disabled={syncMutation.isPending}
                  onClick={() => syncMutation.mutate()}
                  type="button"
                >
                  {choose(syncMutation.isPending, t("react.registry.mcp_final_syncing", "Syncing…"), t("react.registry.sync", "Sync"))}
                </button>
                <button
                  className="fs-button secondary"
                  disabled={healthMutation.isPending}
                  onClick={() => healthMutation.mutate()}
                  type="button"
                >
                  {t("settings.mcp.health", "Health")}</button>
                <button
                  className="fs-button primary"
                  disabled={!connector || connectorQuery.isLoading}
                  onClick={openSettings}
                  type="button"
                >
                  {t("react.registry.edit_settings", "Edit settings")}</button>
              </>
            ))}
          </div>
        }
        description={connector?.connector_id || connectorId}
        eyebrow="MCP"
        title={connector?.display_name || t("react.registry.mcp_connector", "MCP connector")}
      />

      {renderWhen(Boolean(message), <DsAlert title={message} tone={messageTone} />)}

      <DsCard
        description={t("react.registry.connector_definition_and_current_platform_status_for_this_mcp_catalog", "Connector definition and current platform status for this MCP catalog.")}
        title={t("project_edit.tab.overview", "Overview")}
      >
        {/* No tinted surface here: these are facts inside a DsCard, and the card
            is already the surface. Canon's key-value rule — "never place the list
            on `--fs-bg-surface-muted` inside a muted panel … drop the container"
            — is why the grid sits bare on the card body. */}
        {renderChoice(connectorQuery.isLoading, (
          <p className="fs-empty-copy">{t("react.registry.loading_connector_details", "Loading connector details…")}</p>
        ), (
          <div className="fs-form-grid">
            <Fact label={t("settings.mcp.fields.name", "Name")} value={connector?.display_name || "—"} />
            <Fact label={t("extensions.runtime.status", "Status")} value={mcpStatusLabel(choose(Boolean(connector?.enabled), connector?.status ?? "", "disabled"), t)} />
            <Fact label={t("settings.mcp.fields.transport", "Transport")} value={connector?.transport || "—"} />
            <Fact label={t("react.registry.auth_mode", "Auth Mode")} value={connector?.auth_mode || "—"} />
            <Fact label={t("react.registry.auth_status", "Auth Status")} value={mcpStatusLabel(connector?.auth_status || "—", t)} />
            <Fact label={t("react.registry.namespace_prefix", "Namespace Prefix")} value={connector?.namespace_prefix || "—"} />
            <Fact code full label={t("react.registry.endpoint", "Endpoint")} value={connector?.base_url || "—"} />
            <Fact label={t("settings.mcp.status.enabled", "Enabled")} value={mcpStatusLabel(choose(Boolean(connector?.enabled), "true", "false"), t)} />
            <Fact full label={t("registry.workflows.simulation.description", "Description")} value={connector?.description || t("react.registry.mcp_final_no_description", "No description.")} />
          </div>
        ))}
      </DsCard>

      {settingsOpen && settingsDraft ? (
        <DsDialog
          description={t("react.registry.update_connection_settings_existing_secret_headers_are_never_shown_provide_replacement_headers_only_when_rotat", "Update connection settings. Existing secret headers are never shown; provide replacement headers only when rotating them.")}
          eyebrow="MCP"
          footer={
            <>
              <button className="fs-button" onClick={() => setSettingsOpen(false)} type="button">
                {t("extensions.repeater.cancel", "Cancel")}</button>
              <button
                className="fs-button primary"
                disabled={saveSettingsMutation.isPending}
                form="mcp-settings-form"
                type="submit"
              >
                {choose(saveSettingsMutation.isPending, t("react.extensions.saving", "Saving…"), t("react.registry.mcp_final_save_settings", "Save settings"))}
              </button>
            </>
          }
          initialFocusRef={settingsNameRef}
          onClose={() => setSettingsOpen(false)}
          open
          title={t("react.registry.edit_mcp_connector_settings", "Edit MCP connector settings")}
        >
          {/* The submit button lives in the dialog's footer, outside this form,
              so it reaches the form by id rather than by nesting. */}
          <form className="fs-form-grid" id="mcp-settings-form" noValidate onSubmit={submitSettings}>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="mcp-settings-name">{t("react.registry.manual_name", "Name *")}</label>
              {/* The visible label carries the asterisk; the accessible name must
                  stay "Name", which three tests query by. */}
              <input
                aria-describedby={choose(settingsErrorField === "name", "mcp-settings-error", undefined)}
                aria-invalid={settingsErrorField === "name"}
                aria-label={t("settings.mcp.fields.name", "Name")}
                className="fs-input"
                id="mcp-settings-name"
                onChange={(event) => setSettingsDraft((current) => patchSettingsDraft(current, { display_name: event.target.value }))}
                ref={settingsNameRef}
                required
                value={settingsDraft.display_name}
              />
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="mcp-settings-endpoint">{t("react.registry.manual_endpoint", "Endpoint *")}</label>
              <input
                aria-describedby={choose(settingsErrorField === "endpoint", "mcp-settings-error", undefined)}
                aria-invalid={settingsErrorField === "endpoint"}
                aria-label={t("react.registry.endpoint", "Endpoint")}
                className="fs-input"
                id="mcp-settings-endpoint"
                onChange={(event) => setSettingsDraft((current) => patchSettingsDraft(current, { base_url: event.target.value }))}
                ref={settingsEndpointRef}
                required
                type="url"
                value={settingsDraft.base_url}
              />
            </div>
            <div className="fs-field">
              <span className="fs-field-label">{t("settings.mcp.fields.transport", "Transport")}</span>
              <DsSelect
                ariaLabel={t("settings.mcp.fields.transport", "Transport")}
                onChange={(value) => setSettingsDraft((current) => patchSettingsDraft(current, { transport: value }))}
                options={[{ label: "Streamable HTTP", value: "streamable_http" }]}
                value={settingsDraft.transport}
              />
              <span className="fs-field-helper">
                {t("react.registry.the_current_tool_runtime_supports_streamable_http_mcp_endpoints", "The current tool runtime supports streamable HTTP MCP endpoints.")}</span>
            </div>
            <div className="fs-field">
              <span className="fs-field-label">{t("settings.mcp.fields.authentication", "Authentication")}</span>
              <DsSelect
                ariaLabel={t("settings.mcp.fields.authentication", "Authentication")}
                onChange={(value) => setSettingsDraft((current) => updateSettingsAuthMode(current, value))}
                options={[
                  { label: t("react.registry.none", "None"), value: "none" },
                  { label: t("settings.mcp.option.bearer", "Bearer token"), value: "bearer" },
                  { label: t("react.registry.api_key", "API key"), value: "api_key" },
                  { label: "OAuth 2.0", value: "oauth2" },
                ]}
                value={settingsDraft.auth_mode}
              />
            </div>
            <fieldset
              aria-describedby={choose(settingsErrorField === "requested_scopes", "mcp-settings-error", undefined)}
              aria-label={t("react.registry.requested_scopes", "Requested scopes")}
              className={`fs-field full m-0 min-w-0 border-0 p-0${choose(settingsErrorField === "requested_scopes", " fs-field-error-state", "")}`}
              ref={settingsRequestedScopesRef}
              tabIndex={-1}
            >
              <legend className="fs-field-label">{t("react.registry.requested_scopes", "Requested scopes")}</legend>
              {requestedScopeRows.map(({ index, key, scope }) => (
                <div className="fs-mcp-key-value-row" key={key}>
                  <input
                    aria-label={t("react.registry.requested_scope_value0", "Requested scope {value0}", { value0: index + 1 })}
                    aria-describedby={choose(settingsErrorField === "requested_scopes", "mcp-settings-error", undefined)}
                    aria-invalid={settingsErrorField === "requested_scopes"}
                    className="fs-input"
                    onChange={(event) => setSettingsDraft((current) => updateRequestedScope(current, index, event.target.value))}
                    value={scope}
                  />
                  <button className="fs-button secondary" onClick={() => {
                    requestedScopeKeyState.remove(index);
                    setSettingsDraft((current) => removeRequestedScope(current, index));
                  }} type="button">{t("extensions.repeater.remove", "Remove")}</button>
                </div>
              ))}
              <button className="fs-button secondary" onClick={() => {
                requestedScopeKeyState.append();
                setSettingsDraft((current) => current ? patchSettingsDraft(current, { requested_scopes: [...current.requested_scopes, ""] }) : current);
              }} type="button">{t("react.registry.add_requested_scope", "Add requested scope")}</button>
            </fieldset>
            <KeyValueEditor
              addLabel={t("react.registry.mcp_final_add_authorization_parameter", "Add authorization parameter")}
              keyAriaLabel={t("react.registry.mcp_final_authorization_parameter_key", "Authorization parameter key")}
              label={t("react.registry.authorization_parameters", "Authorization parameters")}
              onChange={(authorization_parameters) => setSettingsDraft((current) => patchSettingsDraft(current, { authorization_parameters }))}
              reservedKeys={RESERVED_OAUTH_PARAMETER_IDENTITIES}
              rows={settingsDraft.authorization_parameters}
              serverInvalid={settingsErrorField === "authorization_parameters"}
              valueAriaLabel={t("react.registry.mcp_final_authorization_parameter_value", "Authorization parameter value")}
              wrapperRef={settingsAuthorizationParametersRef}
            />
            <div className="fs-field full" data-testid="mcp-settings-authorization-parameters-help">
              <span className="fs-field-helper">{t("react.registry.these_parameters_are_added_to_the_browser_authorization_url_do_not_enter_secrets_or_credentials_here", "These parameters are added to the browser authorization URL. Do not enter secrets or credentials here.")}</span>
            </div>
            <fieldset
              aria-describedby={choose(settingsErrorField === "refresh_token_required", "mcp-settings-error", undefined)}
              aria-label={t("react.registry.refresh_token_required", "Refresh token required")}
              className={`fs-field m-0 min-w-0 border-0 p-0${choose(settingsErrorField === "refresh_token_required", " fs-field-error-state", "")}`}
              ref={settingsRefreshTokenRequiredRef}
              tabIndex={-1}
            >
              <legend className="fs-field-label">{t("react.registry.refresh_token_required", "Refresh token required")}</legend>
              <DsCheckbox checked={settingsDraft.refresh_token_required} onChange={(checked) => setSettingsDraft((current) => patchSettingsDraft(current, { refresh_token_required: checked }))}>
                {t("react.registry.require_a_refresh_token_when_connecting", "Require a refresh token when connecting.")}</DsCheckbox>
            </fieldset>
            <KeyValueEditor
              addLabel={t("react.registry.mcp_final_add_replacement_header", "Add replacement header")}
              keyAriaLabel={t("react.registry.mcp_final_replacement_header_key", "Replacement header key")}
              label={t("react.registry.replacement_headers", "Replacement headers")}
              onChange={(replacement_headers) => setSettingsDraft((current) => updateReplacementHeaders(current, replacement_headers))}
              rows={settingsDraft.replacement_headers}
              sensitiveValues
              serverInvalid={settingsErrorField === "headers"}
              valueAriaLabel={t("react.registry.mcp_final_replacement_header_value", "Replacement header value")}
              wrapperRef={settingsHeadersRef}
            />
            <div className="fs-field">
              <span className="fs-field-label">{t("react.registry.stored_headers", "Stored headers")}</span>
              <DsSelect
                ariaLabel={t("react.registry.stored_headers_action", "Stored headers action")}
                onChange={(value) => setSettingsDraft((current) => updateStoredHeadersMode(current, value as McpSettingsDraft["headers_mode"]))}
                options={[
                  { label: t("react.registry.keep_existing", "Keep existing"), value: "keep" },
                  { label: t("react.workflows.replace", "Replace"), value: "replace" },
                  { label: t("settings.common.clear", "Clear"), value: "clear" },
                ]}
                value={settingsDraft.headers_mode}
              />
            </div>
            <div className="fs-field full">
              <span className="fs-field-helper">
                {storedHeadersMessage(settingsDraft.headers_mode, t)}
              </span>
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="mcp-settings-namespace">{t("react.registry.namespace_prefix", "Namespace Prefix")}</label>
              <input
                className="fs-input"
                id="mcp-settings-namespace"
                onChange={(event) => setSettingsDraft((current) => patchSettingsDraft(current, { namespace_prefix: event.target.value }))}
                value={settingsDraft.namespace_prefix}
              />
            </div>
            <div className="fs-field">
              <span className="fs-field-label">{t("settings.mcp.status.enabled", "Enabled")}</span>
              <DsCheckbox
                checked={settingsDraft.enabled}
                onChange={(checked) => setSettingsDraft((current) => patchSettingsDraft(current, { enabled: checked }))}
              >
                {t("react.registry.connector_can_be_used_after_it_is_saved", "Connector can be used after it is saved.")}</DsCheckbox>
            </div>
            <div className="fs-field full">
              <label className="fs-field-label" htmlFor="mcp-settings-description">{t("registry.workflows.simulation.description", "Description")}</label>
              <textarea
                className="fs-textarea"
                id="mcp-settings-description"
                onChange={(event) => setSettingsDraft((current) => patchSettingsDraft(current, { description: event.target.value }))}
                value={settingsDraft.description}
              />
            </div>
            {/* `DsAlert` already carries `role="alert"`, so the wrapper must not
                repeat it — two roles made `findByRole("alert")` ambiguous. It
                carries only the id the fields point at through
                `aria-describedby`, which `DsAlert` does not publish. */}
            {renderWhen(Boolean(settingsError), (
              <div className="fs-field full" id="mcp-settings-error">
                <DsAlert title={settingsError} tone="danger" />
              </div>
            ))}
          </form>
        </DsDialog>
      ) : null}

      {renderWhen(authModeIsOauth, (
        <DsCard
          description={t("react.registry.authorize_this_mcp_connector_before_syncing_protected_remote_tools", "Authorize this MCP connector before syncing protected remote tools.")}
          testId="mcp-oauth-card"
          title="OAuth"
        >
          <div className="fs-form-grid">
            <div className="fs-field full">
              <label className="fs-field-label" htmlFor="mcp-oauth-redirect-uri">{t("react.registry.oauth_redirect_uri", "OAuth redirect URI")}</label>
              <div className="flex items-center gap-2">
                <input
                  aria-label={t("react.registry.oauth_redirect_uri", "OAuth redirect URI")}
                  className="fs-input font-[var(--ff-code)]"
                  id="mcp-oauth-redirect-uri"
                  readOnly
                  value={oauthStatus.redirect_uri || ""}
                />
                <button
                  aria-label={t("react.registry.copy_oauth_redirect_uri", "Copy OAuth redirect URI")}
                  className="fs-button secondary cursor-pointer"
                  disabled={!oauthStatus.redirect_uri}
                  onClick={() => ignoreAsyncResult(copyOAuthRedirectUri())}
                  type="button"
                >
                  <Copy aria-hidden="true" />
                  {t("account.tokens.copy", "Copy")}</button>
              </div>
              <span className="fs-field-helper">{t("react.registry.register_this_exact_uri_with_your_oauth_provider_before_starting_authorization", "Register this exact URI with your OAuth provider before starting authorization.")}</span>
            </div>
            <Fact label={t("extensions.runtime.status", "Status")} value={mcpStatusLabel(oauthStatus.status, t)} />
            <Fact
              label={t("react.registry.connected_identity", "Connected identity")}
              value={oauthStatus.connected_identity || choose(oauthStatus.status === "connected", t("react.registry.mcp_final_identity_unavailable", "Identity unavailable"), t("react.registry.mcp_final_not_connected", "Not connected"))}
            />
            <Fact helper={choose(Boolean(connector?.has_client_id), t("react.registry.mcp_final_stored", "Stored"), undefined)} label={t("react.registry.client_id_variant_8726db01", "Client ID")} value={choose(Boolean(connector?.has_client_id), "••••••••", t("react.registry.mcp_final_not_configured", "Not configured"))} />
            <Fact helper={choose(Boolean(connector?.has_client_secret), t("react.registry.mcp_final_stored", "Stored"), undefined)} label={t("react.registry.client_secret", "Client Secret")} value={choose(Boolean(connector?.has_client_secret), "••••••••", t("react.registry.mcp_final_not_configured", "Not configured"))} />
            <Fact
              helper={t("react.registry.mcp_final_token_storage", "access {access} / refresh {refresh}", {
                access: oauthStatus.has_access_token ? t("react.registry.mcp_final_stored_lower", "stored") : t("react.registry.mcp_final_missing", "missing"),
                refresh: oauthStatus.has_refresh_token ? t("react.registry.mcp_final_stored_lower", "stored") : t("react.registry.mcp_final_missing", "missing"),
              })}
              label={t("react.registry.registration", "Registration")}
              value={mcpStatusLabel(oauthStatus.registration_mode || "unknown", t)}
            />
            <Fact code label={t("react.registry.issuer", "Issuer")} value={oauthStatus.issuer || "—"} />
            <Fact code label={t("react.registry.resource", "Resource")} value={oauthStatus.resource || "—"} />
            <Fact label={t("settings.access.keys.columns.expires", "Expires")} value={formatDateTime(oauthStatus.expires_at, t)} />
            <div className="fs-field full">
              <span className="fs-field-label">{t("react.registry.requested_scopes", "Requested scopes")}</span>
              {renderChoice(Boolean(requestedScopes.length), (
                <ul className="fs-list fs-list-compact">
                  {requestedScopes.map((scope) => <li key={scope}><code>{scope}</code></li>)}
                </ul>
              ), <span className="fs-ink-primary">{t("react.registry.none_configured", "None configured")}</span>)}
              <span className="fs-field-helper">{t("react.registry.configured_scopes_are_sent_to_oauth_granted_scopes_are_shown_below", "Configured scopes are sent to OAuth; granted scopes are shown below.")}</span>
            </div>
            <div className="fs-field full">
              <span className="fs-field-label">{t("react.registry.granted_scopes", "Granted scopes")}</span>
              {renderChoice(Boolean(oauthStatus.granted_scopes.length), (
                <ul className="fs-list fs-list-compact">
                  {oauthStatus.granted_scopes.map((scope) => <li key={scope}><code>{scope}</code></li>)}
                </ul>
              ), <span className="fs-ink-primary">{t("react.registry.none", "None")}</span>)}
            </div>
            <div className="fs-field full">
              <span className="fs-field-label">{t("react.registry.authorization_parameters", "Authorization parameters")}</span>
              {renderChoice(Boolean(Object.keys(connector?.oauth_authorization_parameters ?? {}).length), (
                <ul className="fs-list fs-list-compact">
                  {Object.entries(connector?.oauth_authorization_parameters ?? {}).map(([key, value]) => (
                    <li key={key}><code>{key}</code><span> = </span><code>{value}</code></li>
                  ))}
                </ul>
              ), <span className="fs-ink-primary">{t("react.registry.none_configured", "None configured")}</span>)}
            </div>
          </div>

          {renderWhen(Boolean(oauthStatus.last_error), (
            <DsAlert
              title={choose(hasInsufficientScope, t("react.registry.mcp_final_oauth_extra_scopes", "OAuth needs additional scopes"), t("react.registry.mcp_final_oauth_error", "OAuth reported an error"))}
              tone="danger"
            >
              {choose(
                hasInsufficientScope,
                t("react.registry.mcp_final_oauth_scope_help", "The connected account did not grant all configured scopes. Reconnect authorization to request them again (insufficient_scope)."),
                oauthStatus.last_error,
              )}
            </DsAlert>
          ))}

          {renderWhen(allTrue([canManageProject, credentialsOpen]), (
            <div className="fs-form-grid">
              {renderWhen(resumeAuthorizationAfterCredentials, (
                <div className="fs-field full" data-testid="mcp-oauth-credentials-guidance">
                  <p className="fs-field-helper">
                    {t("react.registry.this_authorization_server_requires_client_credentials_enter_both_values_to_continue_authorization", "This authorization server requires client credentials. Enter both values to continue authorization.")}</p>
                </div>
              ))}
              <div className="fs-field">
                <label className="fs-field-label" htmlFor="mcp-oauth-client-id">{t("react.registry.client_id", "Client ID *")}</label>
                <input
                  aria-describedby={credentialsErrorField === "client_id" ? "mcp-oauth-credentials-error" : undefined}
                  aria-invalid={credentialsErrorField === "client_id"}
                  aria-label={t("react.registry.client_id_variant_8726db01", "Client ID")}
                  className="fs-input"
                  id="mcp-oauth-client-id"
                  onChange={(event) => {
                    setManualClientId(event.target.value);
                    if (credentialsErrorField === "client_id") setCredentialsErrorField("");
                  }}
                  ref={credentialsClientIdRef}
                  required
                  value={manualClientId}
                />
              </div>
              <div className="fs-field" ref={credentialsClientSecretRef}>
                <span className="fs-field-label">{t("react.registry.client_secret_variant_9064714f", "Client Secret *")}</span>
                <SensitiveInput
                  aria-describedby={credentialsErrorField === "client_secret" ? "mcp-oauth-credentials-error" : undefined}
                  aria-invalid={credentialsErrorField === "client_secret"}
                  ariaLabel={t("react.registry.client_secret", "Client Secret")}
                  className="fs-input"
                  onChange={(event) => {
                    setManualClientSecret(event.target.value);
                    if (credentialsErrorField === "client_secret") setCredentialsErrorField("");
                  }}
                  required
                  value={manualClientSecret}
                />
              </div>
              {renderWhen(Boolean(credentialsError), (
                <div className="fs-field full" id="mcp-oauth-credentials-error">
                  <DsAlert title={credentialsError} tone="danger" />
                </div>
              ))}
              <button
                className="fs-button primary cursor-pointer"
                disabled={anyTrue([saveCredentialsMutation.isPending, manualClientId.length === 0, manualClientSecret.length === 0])}
                onClick={submitCredentials}
                type="button"
              >
                {choose(saveCredentialsMutation.isPending, t("react.extensions.saving", "Saving…"), t("react.registry.mcp_final_save_credentials", "Save credentials"))}
              </button>
            </div>
          ))}

          {renderWhen(canManageProject, <div className="flex flex-wrap items-center gap-2">
            {renderWhen(allTrue([
              anyTrue([!hasAuthorizedAccount, authorizationMustRestart]),
              !allTrue([credentialsOpen, resumeAuthorizationAfterCredentials]),
            ]), (
              <button
                className="fs-button primary"
                disabled={anyTrue([startOAuthMutation.isPending, oauthStatusQuery.isLoading])}
                onClick={() => startOAuthMutation.mutate()}
                type="button"
              >
                <ExternalLink aria-hidden="true" />
                {oauthStartLabel(startOAuthMutation.isPending, authorizationMustRestart, t)}
              </button>
            ))}
            {renderWhen(hasAuthorizedAccount, (
                <button
                  className="fs-button secondary"
                  disabled={disconnectOAuthMutation.isPending}
                  onClick={() => disconnectOAuthMutation.mutate()}
                  type="button"
                >
                  <Unplug aria-hidden="true" />
                  {t("react.registry.disconnect", "Disconnect")}</button>
            ))}
            {renderWhen(anyTrue([hasStoredClientCredentials, hasAuthorizedAccount]), (
              <button
                className="fs-button secondary cursor-pointer"
                onClick={() => {
                  setResumeAuthorizationAfterCredentials(false);
                  setCredentialsError("");
                  setCredentialsErrorField("");
                  setCredentialsOpen(true);
                }}
                type="button"
              >
                {t("react.registry.replace_credentials", "Replace credentials")}</button>
            ))}
          </div>)}
        </DsCard>
      ))}

      <DsCard
        description={t("react.registry.sync_remote_actions_choose_the_allowlist_and_publish_only_the_tools_you_want_in_the_registry", "Sync remote actions, choose the allowlist, and publish only the tools you want in the registry.")}
        title={t("react.registry.remote_tools", "Remote Tools")}
      >
        <AssignmentSelector
          emptyState={t("react.registry.mcp_final_discover_empty", "Run Sync to discover remote MCP tools.")}
          loading={connectorQuery.isLoading}
          loadingLabel={t("react.registry.loading_remote_tools", "Loading remote tools...")}
          message={remoteToolsMessage}
          onSave={() => saveAllowlistMutation.mutate()}
          onSearchChange={setSearchValue}
          onToggleRow={toggleSelectedTool}
          renderRowAction={(row) =>
            row.actionHref && row.actionLabel ? (
              <Link className="fs-link" to={row.actionHref}>
                {row.actionLabel}
              </Link>
            ) : null
          }
          rows={selectorRows}
          readOnly={!canManageProject}
          saveLabel={t("react.registry.mcp_final_save_allowlist", "Save allowlist")}
          saving={saveAllowlistMutation.isPending}
          searchPlaceholder={t("react.registry.search_remote_tools", "Search remote tools")}
          searchValue={searchValue}
          selectedCount={selectorRows.filter((row) => row.selected && !row.disabled).length}
          selectedLabel="selected"
          totalCount={selectorRows.length}
          totalLabel="remote tools"
        />
      </DsCard>

      <DsCard
        description={t("react.registry.derived_registry_tools_published_from_the_current_allowlist", "Derived registry tools published from the current allowlist.")}
        title={t("react.registry.projected_tools", "Projected Tools")}
      >
        {renderChoice(Boolean((connector?.projected_tools ?? []).length), (
          <div className="fs-panel-stack">
            {(connector?.projected_tools ?? []).map((tool) => (
              <div
                className="fs-surface-radius fs-surface-inset border border-subtle flex flex-wrap items-center gap-3"
                key={tool.tool_id}
              >
                <div className="min-w-0 flex-1">
                  <Link className="fs-link" to={`/registry/tools/${encodeURIComponent(tool.tool_id)}`}>
                    {tool.display_name || tool.tool_id}
                  </Link>
                  <div className="font-[var(--ff-code)] truncate fs-ink-tertiary">{tool.tool_id}</div>
                </div>
                {/* A tool switched off on purpose is not a warning. */}
                <DsBadge tone={choose(tool.enabled, "green" as const, "neutral" as const)}>
                  {choose(tool.enabled, t("settings.extensions.enabled", "enabled"), t("settings.notifications.status.disabled", "disabled"))}
                </DsBadge>
              </div>
            ))}
          </div>
        ), (
          <div className="fs-table-state">
            <strong>{t("react.registry.no_projected_tools_yet", "No projected tools yet")}</strong>
            <span>{t("react.registry.no_derived_registry_tools_have_been_published_for_this_connector_yet", "No derived registry tools have been published for this connector yet.")}</span>
          </div>
        ))}
      </DsCard>
    </section>
    );
  }

  return <PageContainer width="standard">{pageBody}</PageContainer>;
}
