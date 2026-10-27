import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { ConfirmActionDialog } from "@/components/shared/confirm-action-dialog";
import { DsSelect } from "@/components/shared/design-system";
import "@/pages/extensions/extension-manifest.css";
import {
  deleteExtensionOAuthConfiguration,
  ensureExtensionOAuthRedirectUri,
  fetchExtensionOAuthConfigurations,
  previewExtensionOAuthConfigurationDelete,
  type ExtensionOAuthScope,
  type OAuthConfigurationImpact,
  type OAuthConfigurationView,
} from "@/pages/extensions/extension-oauth-api";
import { OAuthConfigurationForm } from "@/pages/extensions/extension-oauth-configuration-form";
import type { OAuthProviderDeclaration } from "@/pages/extensions/extension-oauth-types";

import { ConnectionComposer } from "./extension-oauth-connection-composer";
import { ConnectionList } from "./extension-oauth-connection-list";
import {
  activeOAuthConfiguration,
  configurationGuidance,
  isConfigurationReady,
  oauthConnectionDeclaration,
  oauthStatusLabel,
  redirectRegistrationGuidance,
  selectableOAuthConfigurations,
  type ExtensionOAuthSetupProps,
  type OAuthConfigurationDeletePreview,
  type OAuthConfigurationSaveNotice,
  type OAuthConnectionDeclaration,
} from "./extension-oauth-setup-model";
function OAuthSetupHeader({
  canManage,
  onNavigatePage,
  onRetryRedirect,
  provider,
  redirectError,
  redirectFetching,
  redirectUri,
  returnPageId,
  showConfiguration,
}: Readonly<{
  canManage: boolean;
  onNavigatePage: (pageId: string, anchor?: string) => void;
  onRetryRedirect: () => void;
  provider: OAuthProviderDeclaration;
  redirectError: unknown;
  redirectFetching: boolean;
  redirectUri?: string;
  returnPageId: string;
  showConfiguration: boolean;
}>) {
  const { t } = useI18n();
  return (
    <>
      <div>
        <h2 className="fs-manifest-title">{provider.display_name}</h2>
        {provider.description ? <p className="fs-manifest-description">{provider.description}</p> : null}
      </div>
      {showConfiguration && canManage && redirectUri ? (
        <label className="fs-field">
          <span className="fs-field-label">{t("react.extensions.oauth_redirect_uri", "OAuth Redirect URI")}</span>
          <input className="fs-input fs-manifest-code" readOnly value={redirectUri} />
        </label>
      ) : null}
      {showConfiguration && canManage && redirectError ? (
        <div className="fs-manifest-list">
          <div className="fs-field-helper error" role="alert">
            {redirectRegistrationGuidance(redirectError, t)}
          </div>
          <button
            className="fs-button sm"
            disabled={redirectFetching}
            onClick={onRetryRedirect}
            type="button"
          >
            {t("react.extensions.retry_redirect_registration", "Retry redirect registration")}</button>
        </div>
      ) : null}
      {provider.help_page_id && provider.help_page_id !== returnPageId ? (
        <button className="fs-manifest-link" onClick={() => onNavigatePage(provider.help_page_id || "")} type="button">
          {t("react.extensions.open_provider_help", "Open provider help")}</button>
      ) : null}
    </>
  );
}

function OAuthConfigurationQueryStatus({
  error,
  fetching,
  loading,
  onRetry,
}: Readonly<{
  error: boolean;
  fetching: boolean;
  loading: boolean;
  onRetry: () => void;
}>) {
  const { t } = useI18n();
  if (loading) return <p className="fs-empty-copy">{t("react.extensions.loading_configurations", "Loading configurations…")}</p>;
  if (!error) return null;
  return (
    <div className="fs-manifest-list">
      <p className="fs-field-helper error" role="alert">{t("react.extensions.configurations_could_not_be_loaded_try_again", "Configurations could not be loaded. Try again.")}</p>
      <button className="fs-button sm" disabled={fetching} onClick={onRetry} type="button">
        {t("react.extensions.retry_loading_configurations", "Retry loading configurations")}</button>
    </div>
  );
}

/** One connection is named in the sentence; several are listed by the dialog. */
function oauthDeleteConsequence(connections: ReadonlyArray<{ connection_id: string; display_name?: string }>, t: I18nContextValue["t"] = fallbackContext.t) {
  if (connections.length === 1) {
    return t("react.extensions.oauth_final_delete_connection", "{name} and its local credentials will also be removed.", { name: connections[0].display_name || connections[0].connection_id });
  }
  return connections.length
    ? t("react.extensions.these_connections_and_their_local_credentials_will_also_be_removed", "These connections and their local credentials will also be removed.")
    : t("react.extensions.the_configuration_is_removed_from_this_extension", "The configuration is removed from this extension.");
}

function OAuthConfigurationDeleteDialog({
  busy,
  onClose,
  onConfirm,
  preview,
}: Readonly<{
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
  preview: OAuthConfigurationDeletePreview | null;
}>) {
  const { t } = useI18n();
  const connections = preview?.impact.connections ?? [];
  const steps = preview?.impact.workflow_steps ?? [];
  // A recreated connection gets a new id: every step bound to the old one
  // stops working until it is pointed at the new one, so name them here.
  const stepsConsequence = steps.length
    ? ` ${steps.length === 1
      ? t("react.extensions.oauth_final_delete_step", "This workflow step uses them and will stop working until you choose another connection in the step.")
      : t("react.extensions.oauth_final_delete_steps", "These {count} workflow steps use them and will stop working until you choose another connection in the step.", { count: steps.length })}`
    : "";
  return (
    <ConfirmActionDialog
      confirmLabel={connections.length ? t("react.extensions.delete_configuration_and_connections", "Delete configuration and connections") : t("react.extensions.delete_configuration", "Delete configuration")}
      consequence={`${oauthDeleteConsequence(connections, t)}${stepsConsequence}`}
      eyebrow={t("react.extensions.oauth_configuration", "OAuth configuration")}
      objects={[
        ...connections.map((connection) => connection.display_name || connection.connection_id),
        ...steps.map((step) => [step.workflow_name, step.group_name, step.step_name].filter(Boolean).join(" / ")),
      ]}
      onCancel={onClose}
      onConfirm={onConfirm}
      open={Boolean(preview)}
      pending={busy}
      pendingLabel={t("react.extensions.deleting_configuration", "Deleting configuration…")}
      title={t("react.extensions.delete_this_configuration", "Delete this configuration?")}
    />
  );
}

function OAuthConfigurationPanel({
  active,
  canManage,
  configurations,
  deleteBusy,
  deleteError,
  deletePreview,
  onCloseDelete,
  onConfirmDelete,
  onEdit,
  onPreviewDelete,
  onSaved,
  onSelect,
  provider,
  ready,
  saveNotice,
  scope,
  scopeKey,
  selectionValue,
  show,
}: Readonly<{
  active?: OAuthConfigurationView;
  canManage: boolean;
  configurations: readonly OAuthConfigurationView[];
  deleteBusy: boolean;
  deleteError: string;
  deletePreview: OAuthConfigurationDeletePreview | null;
  onCloseDelete: () => void;
  onConfirmDelete: () => void;
  onEdit: () => void;
  onPreviewDelete: () => void;
  onSaved: (saved?: OAuthConfigurationView, notice?: string) => Promise<void>;
  onSelect: (configurationId: string) => void;
  provider: OAuthProviderDeclaration;
  ready: boolean;
  saveNotice: OAuthConfigurationSaveNotice | null;
  scope: ExtensionOAuthScope;
  scopeKey: string;
  selectionValue: string;
  show: boolean;
}>) {
  const { t } = useI18n();
  if (!ready || !show) return null;
  if (!canManage) {
    return (
      <div className="fs-manifest-list" aria-label={t("react.extensions.oauth_configurations", "OAuth configurations")}>
        {configurations.map((configuration) => (
          <div className="fs-manifest-row" key={configuration.configuration_id}>
            <div className="fs-manifest-row-copy">
              <p className="fs-manifest-row-name">{configuration.display_name}</p>
              <p className="fs-manifest-row-detail">{oauthStatusLabel(configuration.status, t)}</p>
            </div>
          </div>
        ))}
      </div>
    );
  }
  const visibleSaveNotice = saveNotice?.scopeKey === scopeKey
    && saveNotice.configurationId === active?.configuration_id;
  return (
    <>
      <div className="fs-field fs-manifest-measure">
        <label className="fs-field-label" htmlFor={`${scopeKey}-configuration`}>{t("react.extensions.oauth_configuration", "OAuth configuration")}</label>
        <DsSelect
          ariaLabel={t("react.extensions.oauth_configuration", "OAuth configuration")}
          id={`${scopeKey}-configuration`}
          onChange={onSelect}
          options={[
            ...configurations.map((configuration) => ({
              value: configuration.configuration_id,
              label: configuration.display_name,
            })),
            { value: "new", label: t("react.extensions.add_configuration", "Add configuration…") },
          ]}
          value={selectionValue}
        />
      </div>
      <OAuthConfigurationForm
        configuration={active}
        key={`${active?.configuration_id || "new"}:${active?.revision || 0}`}
        onEdit={onEdit}
        onSaved={onSaved}
        provider={provider}
        scope={scope}
      />
      {visibleSaveNotice ? <p aria-live="polite" className="fs-manifest-success" role="status">{saveNotice.message}</p> : null}
      {active ? <p className="fs-manifest-description">{configurationGuidance(active.status, t)}</p> : null}
      {active ? (
        <div className="fs-manifest-actions">
          <button className="fs-button sm ghost-danger" disabled={deleteBusy} onClick={onPreviewDelete} type="button">{t("react.extensions.delete_configuration", "Delete configuration")}</button>
        </div>
      ) : null}
      {deleteError ? <p className="fs-field-helper error" role="alert">{deleteError}</p> : null}
      <OAuthConfigurationDeleteDialog busy={deleteBusy} onClose={onCloseDelete} onConfirm={onConfirmDelete} preview={deletePreview} />
    </>
  );
}

function OAuthProviderSetupRequired({
  configurations,
  linkToSetup,
  onNavigatePage,
  provider,
}: Readonly<{
  configurations: readonly OAuthConfigurationView[];
  linkToSetup: boolean;
  onNavigatePage: (pageId: string, anchor?: string) => void;
  provider: OAuthProviderDeclaration;
}>) {
  const { t } = useI18n();
  return (
    <div className="fs-manifest-inset">
      <h3 className="fs-manifest-section-title">{t("react.extensions.provider_setup_required", "Provider setup required")}</h3>
      <p className="fs-manifest-notice">
        {configurations.some((configuration) => isConfigurationReady(configuration))
          ? t("react.extensions.oauth_callback_registration_must_be_ready_before_connecting_another_account", "OAuth callback registration must be ready before connecting another account.")
          : t("react.extensions.no_oauth_configuration_is_ready_to_connect", "No OAuth configuration is ready to connect.")}
      </p>
      {linkToSetup ? (
        <div className="fs-manifest-actions">
          <button className="fs-button sm" onClick={() => onNavigatePage(provider.setup_page_id)} type="button">
            {t("react.extensions.open_provider_setup", "Open provider setup")}</button>
        </div>
      ) : null}
    </div>
  );
}

function OAuthConnectionsPanel({
  active,
  allowedIntentIds,
  besideConfiguration,
  canManage,
  configurations,
  connectionDeclaration,
  connections,
  connectionsQuery,
  focusConnectionId,
  onAuthorization,
  onConnectionsChanged,
  onNavigatePage,
  onSelect,
  onTestConnection,
  provider,
  ready,
  returnPageId,
  scope,
  selectableConfigurations,
  selectionValue,
  show,
}: Readonly<{
  active?: OAuthConfigurationView;
  allowedIntentIds?: readonly string[];
  /** The configuration form is on the same page, so no link to the setup page. */
  besideConfiguration: boolean;
  canManage: boolean;
  configurations: readonly OAuthConfigurationView[];
  connectionDeclaration: OAuthConnectionDeclaration;
  connections: readonly Record<string, unknown>[];
  connectionsQuery: ExtensionOAuthSetupProps["connectionsQuery"];
  focusConnectionId?: string;
  onAuthorization?: (authorizationUrl: string) => void;
  onConnectionsChanged?: () => void | Promise<void>;
  onNavigatePage: (pageId: string, anchor?: string) => void;
  onSelect: (configurationId: string) => void;
  onTestConnection?: (connection: Record<string, unknown>) => void | Promise<void>;
  provider: OAuthProviderDeclaration;
  ready: boolean;
  returnPageId: string;
  scope: ExtensionOAuthScope;
  selectableConfigurations: readonly OAuthConfigurationView[];
  selectionValue: string;
  show: boolean;
}>) {
  const { t } = useI18n();
  if (!ready || !show) return null;
  if (connectionsQuery?.isLoading) return <p aria-live="polite" className="fs-empty-copy" role="status">{t("react.extensions.loading_connections", "Loading connections…")}</p>;
  if (connectionsQuery?.isError) {
    return (
      <div className="fs-manifest-list" role="alert">
        <p className="fs-field-helper error">{t("react.extensions.connections_could_not_be_loaded_try_again", "Connections could not be loaded. Try again.")}</p>
        <div className="fs-manifest-actions">
          <button className="fs-button sm" onClick={() => void connectionsQuery.refetch()} type="button">{t("react.extensions.retry_loading_connections", "Retry loading connections")}</button>
        </div>
      </div>
    );
  }
  return (
    <>
      {canManage && selectableConfigurations.length > 1 ? (
        <div className="fs-field fs-manifest-measure">
          <label className="fs-field-label" htmlFor={`${scope.installId}-${scope.providerId}-new-connection-configuration`}>{t("react.extensions.configuration_for_new_connection", "Configuration for new connection")}</label>
          <DsSelect
            ariaLabel={t("react.extensions.configuration_for_new_connection", "Configuration for new connection")}
            id={`${scope.installId}-${scope.providerId}-new-connection-configuration`}
            onChange={onSelect}
            options={selectableConfigurations.map((configuration) => ({
              value: configuration.configuration_id,
              label: configuration.display_name,
            }))}
            value={selectionValue}
          />
        </div>
      ) : null}
      {!selectableConfigurations.length ? <OAuthProviderSetupRequired configurations={configurations} linkToSetup={!besideConfiguration} onNavigatePage={onNavigatePage} provider={provider} /> : null}
      {canManage && connectionDeclaration.connectionDeclarationIsExact && active ? (
        <ConnectionComposer
          allowedIntentIds={allowedIntentIds}
          configuration={active}
          connectionTypes={connectionDeclaration.providerConnectionTypes}
          key={`connect:${active.configuration_id}:${connectionDeclaration.exactAllowedConnectionTypeIds[0] || "none"}:${connectionDeclaration.exactAllowedIntentIds[0] || "none"}`}
          onAuthorization={onAuthorization}
          provider={provider}
          returnPageId={returnPageId}
          scope={scope}
        />
      ) : null}
      <ConnectionList
        canManage={canManage}
        configurations={configurations}
        connections={connections}
        focusConnectionId={focusConnectionId}
        legacyOwnershipIsExact={connectionDeclaration.legacyOwnershipIsExact}
        onAuthorization={onAuthorization}
        onConnectionsChanged={onConnectionsChanged}
        onNavigatePage={onNavigatePage}
        onTestConnection={onTestConnection}
        ownedConnectionTypeId={connectionDeclaration.ownedConnectionTypeId}
        ownedIntentId={connectionDeclaration.connectionDeclarationIsExact ? connectionDeclaration.exactAllowedIntentIds[0] || "" : ""}
        provider={provider}
        returnPageId={returnPageId}
        scope={scope}
      />
    </>
  );
}

export function ExtensionOAuthSetup({
  canManage,
  allowedConnectionTypeIds,
  allowedIntentIds,
  connectionOwners = [],
  connectionTypes,
  connections,
  connectionsQuery,
  installId,
  mode = "setup",
  onAuthorization,
  onConnectionsChanged,
  onNavigatePage,
  onTestConnection,
  projectId,
  provider,
  providerSetupReady = true,
  returnPageId,
  focusConnectionId,
}: Readonly<ExtensionOAuthSetupProps>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<{
    configurationId: string;
    scopeKey: string;
  } | null>(null);
  const [deletePreview, setDeletePreview] = useState<OAuthConfigurationDeletePreview | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [saveNotice, setSaveNotice] = useState<OAuthConfigurationSaveNotice | null>(null);
  const scope = useMemo(() => ({ projectId, installId, providerId: provider.provider_id }), [installId, projectId, provider.provider_id]);
  const scopeKey = `${projectId}:${installId}:${provider.provider_id}`;
  const selectedId = selection?.scopeKey === scopeKey ? selection.configurationId : "";
  const setSelectedId = (configurationId: string) => setSelection({ configurationId, scopeKey });
  const activeDeletePreview = deletePreview?.scopeKey === scopeKey ? deletePreview : null;
  const configurationsQuery = useQuery({
    queryKey: ["extension-oauth", projectId, installId, provider.provider_id, "configurations"],
    queryFn: () => fetchExtensionOAuthConfigurations(scope),
    enabled: Boolean(projectId && installId && provider.provider_id),
  });
  const setupStatusQueryKey = [
    "extension-oauth",
    projectId,
    installId,
    provider.provider_id,
    "setup-status",
  ] as const;
  const redirectQuery = useQuery({
    queryKey: ["extension-oauth", projectId, installId, provider.provider_id, "redirect-uri"],
    queryFn: async () => {
      const result = await ensureExtensionOAuthRedirectUri(scope);
      await queryClient.cancelQueries({ queryKey: setupStatusQueryKey, exact: true });
      queryClient.setQueryData(setupStatusQueryKey, { callback_registration_ready: true });
      await queryClient.invalidateQueries({ queryKey: setupStatusQueryKey, exact: true });
      return result;
    },
    enabled: canManage && Boolean(projectId && installId && provider.provider_id),
  });
  const configurations = configurationsQuery.data || [];
  const showsConfiguration = mode !== "connections";
  const showsConnections = mode !== "setup";
  const selectableConfigurations = selectableOAuthConfigurations(configurations, mode, providerSetupReady);
  const configurationsReady = configurationsQuery.isSuccess;
  const active = activeOAuthConfiguration(showsConfiguration ? configurations : selectableConfigurations, selectedId);
  const selectionValue = active?.configuration_id || "new";
  // Next to the configuration form, an account connects through the selected
  // configuration once it is ready, else through the first ready one.
  const connectionActive = showsConfiguration
    ? activeOAuthConfiguration(selectableConfigurations, selectedId === "new" ? "" : selectedId)
    : active;
  const connectionDeclaration = oauthConnectionDeclaration({
    allowedConnectionTypeIds,
    allowedIntentIds,
    connectionOwners,
    connectionTypes,
    mode,
    providerId: provider.provider_id,
    returnPageId,
  });

  async function refreshConfigurations(saved?: OAuthConfigurationView, notice?: string) {
    if (saved && notice) {
      setSaveNotice({
        configurationId: saved.configuration_id,
        message: notice,
        scopeKey,
      });
    }
    await queryClient.invalidateQueries({
      queryKey: ["extension-oauth", projectId, installId, provider.provider_id, "configurations"],
    });
    if (saved) {
      setSelectedId(saved.configuration_id);
    }
  }

  async function previewDelete() {
    if (!active || deleteBusy) {
      return;
    }
    setDeleteBusy(true);
    setDeleteError("");
    try {
      const impact = await previewExtensionOAuthConfigurationDelete(
        scope,
        active.configuration_id,
        active.revision,
      ) as OAuthConfigurationImpact;
      setDeletePreview({
        scopeKey,
        configurationId: active.configuration_id,
        expectedRevision: active.revision,
        impact,
      });
    } catch {
      setDeleteError(t("react.extensions.configuration_deletion_could_not_be_prepared_refresh_and_try_again", "Configuration deletion could not be prepared. Refresh and try again."));
    } finally {
      setDeleteBusy(false);
    }
  }

  async function confirmDelete() {
    if (!activeDeletePreview || deleteBusy) {
      return;
    }
    setDeleteBusy(true);
    setDeleteError("");
    try {
      await deleteExtensionOAuthConfiguration(
        scope,
        activeDeletePreview.configurationId,
        activeDeletePreview.expectedRevision,
        activeDeletePreview.impact.confirmation_token,
      );
      setDeletePreview(null);
      setSelectedId("new");
      await refreshConfigurations();
      await onConnectionsChanged?.();
    } catch {
      setDeletePreview(null);
      setDeleteError(t("react.extensions.configuration_could_not_be_deleted_refresh_and_try_again", "Configuration could not be deleted. Refresh and try again."));
    } finally {
      setDeleteBusy(false);
    }
  }

  function selectConfiguration(configurationId: string) {
    setSelectedId(configurationId);
    setDeletePreview(null);
    setDeleteError("");
    setSaveNotice(null);
  }

  function closeDeletePreview() {
    if (!deleteBusy) setDeletePreview(null);
  }

  return (
    <section className="fs-manifest-block" aria-label={t("react.extensions.value0_oauth_setup", "{value0} OAuth setup", { value0: provider.display_name })}>
      <OAuthSetupHeader
        canManage={canManage}
        onNavigatePage={onNavigatePage}
        onRetryRedirect={() => void redirectQuery.refetch()}
        provider={provider}
        redirectError={redirectQuery.error}
        redirectFetching={redirectQuery.isFetching}
        redirectUri={redirectQuery.data?.redirect_uri}
        returnPageId={returnPageId}
        showConfiguration={showsConfiguration}
      />
      <OAuthConfigurationQueryStatus
        error={configurationsQuery.isError}
        fetching={configurationsQuery.isFetching}
        loading={configurationsQuery.isLoading}
        onRetry={() => void configurationsQuery.refetch()}
      />
      <OAuthConfigurationPanel
        active={active}
        canManage={canManage}
        configurations={configurations}
        deleteBusy={deleteBusy}
        deleteError={deleteError}
        deletePreview={activeDeletePreview}
        onCloseDelete={closeDeletePreview}
        onConfirmDelete={() => void confirmDelete()}
        onEdit={() => setSaveNotice(null)}
        onPreviewDelete={() => void previewDelete()}
        onSaved={refreshConfigurations}
        onSelect={selectConfiguration}
        provider={provider}
        ready={configurationsReady}
        saveNotice={saveNotice}
        scope={scope}
        scopeKey={scopeKey}
        selectionValue={selectionValue}
        show={showsConfiguration}
      />
      <OAuthConnectionsPanel
        active={connectionActive}
        allowedIntentIds={allowedIntentIds}
        besideConfiguration={showsConfiguration}
        canManage={canManage}
        configurations={configurations}
        connectionDeclaration={connectionDeclaration}
        connections={connections}
        connectionsQuery={connectionsQuery}
        focusConnectionId={focusConnectionId}
        onAuthorization={onAuthorization}
        onConnectionsChanged={onConnectionsChanged}
        onNavigatePage={onNavigatePage}
        onSelect={setSelectedId}
        onTestConnection={onTestConnection}
        provider={provider}
        ready={configurationsReady}
        returnPageId={returnPageId}
        scope={scope}
        selectableConfigurations={selectableConfigurations}
        selectionValue={connectionActive?.configuration_id || ""}
        show={showsConnections}
      />
    </section>
  );
}
