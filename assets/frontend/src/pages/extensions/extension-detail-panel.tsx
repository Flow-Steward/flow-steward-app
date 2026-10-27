import { useMemo, type Dispatch, type KeyboardEvent, type SetStateAction } from "react";

import { ChevronRight } from "lucide-react";

import {
  DsAlert,
  DsBadge,
  DsFormSelect,
  DsValueChip,
  type DsBadgeTone,
} from "@/components/shared/design-system";
import {
  ExtensionActionResultPanel,
  type ExtensionActionResult,
  type ExtensionEffectRecovery,
} from "@/pages/extensions/extension-action-result-panel";
import type {
  ExtensionCallbackRegistration,
  ExtensionRecord,
} from "@/pages/extensions/api";
import { catalogLabel } from "./hub-support";
import { manifestActionHostEffect } from "./manifest-action-support";
import { extensionInstallWarnings } from "@/pages/extensions/extension-install-warnings";
import { renderManifestComponent } from "@/pages/extensions/extension-manifest-renderer";
import { type PendingConnectionDisconnect } from "@/pages/extensions/extension-manifest-render-model";
import {
  extensionState,
  focusManifestAnchor,
  listFromUnknown,
  manifestQueryValues,
  manifestAnchorDomId,
  objectOrEmpty,
  stringFrom,
  type ComponentError,
  type ManifestAction,
} from "@/pages/extensions/extension-manifest-utils";
import type { ProjectListItem } from "@/pages/projects/list/api";
import { safeLinkHref } from "@/pages/jobs/detail/job-detail-core";
import { ignoreAsyncResult } from "@/lib/utils/async";

function extensionStateTone(state: string, active: boolean): DsBadgeTone {
  if (state === "broken") return "danger";
  if (active) return "green";
  return "neutral";
}

function detailStatusSucceeded(status: string): boolean {
  const normalized = status.toLowerCase();
  return normalized.includes("successful") || normalized.includes("completed");
}
import { useHasCapability } from "@/lib/auth/capabilities";
import { useI18n } from "@/lib/i18n/useI18n";
import { localizeExtensionManifest, pageIdentity } from "@/pages/extensions/extension-locale";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ExtensionOAuthSetup } from "@/pages/extensions/extension-oauth-setup";
import type { ExtensionOAuthSetupMode } from "@/pages/extensions/extension-oauth-setup-model";
import {
  oauthConnectionTypesFromManifest,
  oauthProvidersFromManifest,
} from "@/pages/extensions/extension-oauth-types";





type RecordMap = Record<string, Record<string, unknown>>;

type ExtensionDetailPanelProps = {
  activationBlockedReason: string;
  connectionCreationBlockedReason: string;
  activeManifestPage: Record<string, unknown> | undefined;
  busyById: Record<string, boolean>;
  callbackStatuses: RecordMap;
  callbacksQuery: {
    data?: ExtensionCallbackRegistration[];
  };
  componentErrors: Record<string, ComponentError>;
  actionResult?: ExtensionActionResult;
  effectRecoveries?: ExtensionEffectRecovery[];
  componentQueryData: RecordMap;
  componentQueryParams: RecordMap;
  detailExtension: ExtensionRecord;
  extensionInstallId: string;
  detailStatus: string;
  formValuesByComponent: RecordMap;
  handleCallbackSetup: (
    component: Record<string, unknown>,
    setupAction: "configure" | "status",
  ) => void | Promise<void>;
  handleHealthCheck: (extension: ExtensionRecord) => void | Promise<void>;
  handleExternalResourcePickerBootstrap: (
    actionId: string,
    component: Record<string, unknown>,
  ) => Promise<unknown>;
  healthStatus: string;
  handleManifestAction: (
    action: ManifestAction,
    form: HTMLFormElement | null,
    component: Record<string, unknown>,
    overrides?: {
      target?: Record<string, unknown>;
      values?: Record<string, unknown>;
      data?: Record<string, unknown>;
    },
  ) => void | Promise<void>;
  handleProjectSelection: (nextProjectId: string) => void;
  handleToggle: (extension: ExtensionRecord) => void | Promise<void>;
  handleUninstall: (extension: ExtensionRecord) => void | Promise<void>;
  archiveLifecycleAvailable: boolean;
  manifestPages: Array<Record<string, unknown>>;
  manifestQuery: {
    data?: unknown;
    isError: boolean;
    isLoading: boolean;
  };
  oauthConnectionTypes: string[];
  oauthProviderReadiness: Record<string, "blocked" | "loading" | "ready">;
  projectOptions: ProjectListItem[];
  projectConnections: Array<Record<string, unknown>>;
  projectConnectionsQuery: {
    isError: boolean;
    isPending: boolean;
    refetch: () => Promise<unknown>;
  };
  projectState: Array<Record<string, unknown>>;
  pageRequiresProject: boolean;
  projectsQuery: {
    error: unknown;
    isError: boolean;
  };
  rememberedProjectId: string;
  selectedProjectId: string;
  setActiveExtensionPageId: (pageId: string) => void;
  setComponentQueryParams: Dispatch<SetStateAction<RecordMap>>;
  setFormValuesByComponent: Dispatch<SetStateAction<RecordMap>>;
  onFieldCorrected: (componentId: string, fieldName: string) => void;
  onConnectionSave: (
    component: Record<string, unknown>,
    values: Record<string, unknown>,
  ) => void | Promise<void>;
  onConnectionDelete: (
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) => void | Promise<void>;
  onConnectionOAuth: (
    component: Record<string, unknown>,
    row: Record<string, unknown>,
    intent: string,
  ) => void | Promise<void>;
  onConnectionOAuthDisconnect: (
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) => void | Promise<void>;
  pendingDisconnect: PendingConnectionDisconnect | null;
  onConnectionOAuthDisconnectConfirm: (
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) => void | Promise<void>;
  onConnectionOAuthDisconnectCancel: () => void;
  onOAuthConnectionsChanged: () => void | Promise<void>;
  onConnectionSelect: (
    component: Record<string, unknown>,
    row: Record<string, unknown> | null,
  ) => void;
  onProjectStateSave: (
    component: Record<string, unknown>,
    values: Record<string, unknown>,
  ) => void | Promise<void>;
  onProjectStateDelete: (
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) => void | Promise<void>;
  onProjectStateSelect: (
    component: Record<string, unknown>,
    row: Record<string, unknown> | null,
  ) => void;
  oauthFocusConnectionId?: string;
};

type ManifestOAuthProvider = ReturnType<typeof oauthProvidersFromManifest>[number];

type ActiveOAuthProvider = {
  provider: ManifestOAuthProvider;
  mode: ExtensionOAuthSetupMode;
  connectionTypeIds: string[];
  intentIds: string[];
  connectionOwners: Array<{ intentId: string; pageId: string }>;
  ownershipKey: string;
  component?: Record<string, unknown>;
};

function manifestPageId(page: Record<string, unknown> | undefined): string {
  return page ? pageIdentity(page) : "";
}

function oauthConnectionComponents(page: Record<string, unknown> | undefined) {
  return listFromUnknown(page?.components)
    .filter((component) => stringFrom(component.type) === "connection_form")
    .map((component) => ({ component, data: objectOrEmpty(component.data) }))
    .filter(({ data }) => Boolean(stringFrom(data.oauth_provider_id)));
}

function manifestPageIsLocked(
  page: Record<string, unknown>,
  providers: ManifestOAuthProvider[],
  readiness: ExtensionDetailPanelProps["oauthProviderReadiness"],
  projectConnections: Array<Record<string, unknown>>,
  extensionInstallId: string,
): boolean {
  const pageId = manifestPageId(page);
  if (providers.some((provider) => provider.setup_page_id === pageId)) return false;
  const components = oauthConnectionComponents(page);
  const providerIds = components.map(({ data }) => stringFrom(data.oauth_provider_id));
  const connectionTypeIds = new Set(components.map(({ data }) => stringFrom(data.connection_type)));
  const ownsExistingConnection = projectConnections.some((connection) => (
    stringFrom(connection.extension_install_id) === extensionInstallId
    && providerIds.includes(stringFrom(connection.oauth_provider_id))
    && connectionTypeIds.has(stringFrom(connection.connection_type))
  ));
  return Boolean(providerIds.length)
    && !ownsExistingConnection
    && providerIds.some((providerId) => readiness[providerId] !== "ready");
}

function manifestOAuthConnectionOwners(manifestPages: Array<Record<string, unknown>>) {
  return manifestPages.flatMap((page) => {
    const pageId = manifestPageId(page);
    return oauthConnectionComponents(page)
      .map(({ data }) => ({
        connectionTypeId: stringFrom(data.connection_type),
        intentId: stringFrom(data.oauth_intent),
        pageId,
        providerId: stringFrom(data.oauth_provider_id),
      }))
      .filter((owner) => Boolean(owner.connectionTypeId && owner.intentId && owner.pageId && owner.providerId));
  });
}

function activeOAuthProviderRows(
  providers: ManifestOAuthProvider[],
  activePageId: string,
  pageComponents: ReturnType<typeof oauthConnectionComponents>,
  owners: ReturnType<typeof manifestOAuthConnectionOwners>,
): ActiveOAuthProvider[] {
  return providers.flatMap<ActiveOAuthProvider>((provider) => {
    const connectionRows = pageComponents.flatMap<ActiveOAuthProvider>(({ component, data }, index) => {
      const connectionTypeId = stringFrom(data.connection_type);
      const intentId = stringFrom(data.oauth_intent);
      if (stringFrom(data.oauth_provider_id) !== provider.provider_id || !connectionTypeId || !intentId) return [];
      return [{
        provider,
        mode: "connections" as const,
        connectionTypeIds: [connectionTypeId],
        intentIds: [intentId],
        connectionOwners: owners
          .filter((owner) => owner.providerId === provider.provider_id && owner.connectionTypeId === connectionTypeId)
          .map((owner) => ({ intentId: owner.intentId, pageId: owner.pageId })),
        ownershipKey: `connections:${activePageId}:${provider.provider_id}:${connectionTypeId}:${intentId}:${index}`,
        component,
      }];
    });
    if (provider.setup_page_id !== activePageId) return connectionRows;
    // A setup page that declares its provider's connection form connects the
    // account right below the app configuration; one that does not (one app
    // shared by several service pages) only configures the app.
    const [setupConnectionRow, ...otherConnectionRows] = connectionRows;
    if (setupConnectionRow) {
      return [{
        ...setupConnectionRow,
        mode: "setup_and_connections" as const,
        ownershipKey: `setup:${setupConnectionRow.ownershipKey}`,
      }, ...otherConnectionRows];
    }
    return [{
      provider,
      mode: "setup" as const,
      connectionTypeIds: [],
      intentIds: [],
      connectionOwners: [],
      ownershipKey: `setup:${provider.provider_id}`,
    }];
  });
}

function ExtensionSummary({
  canAdministerPlatform,
  detailExtension,
  panel,
  safeSupportUrl,
  safeWebsiteUrl,
}: Readonly<{
  canAdministerPlatform: boolean;
  detailExtension: ExtensionRecord;
  panel: ExtensionDetailPanelProps;
  safeSupportUrl: string;
  safeWebsiteUrl: string;
}>) {
  const { t } = useI18n();
  const state = extensionState(detailExtension);
  const actionEffectId = manifestActionHostEffect(panel.actionResult?.payload)?.effectId;
  const actionRecovery = panel.effectRecoveries?.find((recovery) => recovery.effect.effect_id === actionEffectId);
  const recoveryGuidance = (recovery?: ExtensionEffectRecovery) => recovery?.effect.status === "timeout_unknown" && !recovery.effect.resolution_ref
    ? t("react.extensions.display_external_effect_ambiguity", "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.") : "";
  return (
    <>
      <div className="fs-extension-detail-facts">
        <DsValueChip>{t("react.extensions.id", "id:")}{" "}{detailExtension.extension_id}</DsValueChip>
        <DsValueChip>{t("react.extensions.version", "version:")}{" "}{detailExtension.version || "—"}</DsValueChip>
        <DsBadge tone={extensionStateTone(state, Boolean(detailExtension.active))}>{state}</DsBadge>
        {detailExtension.vendor_name || detailExtension.author ? (
          <DsValueChip>{detailExtension.vendor_name || detailExtension.author}</DsValueChip>
        ) : null}
        {safeSupportUrl ? <a className="fs-link" href={safeSupportUrl} rel="noreferrer" target="_blank">{t("extensions.detail.support", "Support")}</a> : null}
        {safeWebsiteUrl ? <a className="fs-link" href={safeWebsiteUrl} rel="noreferrer" target="_blank">{t("extensions.detail.website", "Website")}</a> : null}
      </div>
      {canAdministerPlatform ? <ExtensionInstallWarnings detailExtension={detailExtension} /> : null}
      {detailExtension.broken ? (
        <DsAlert
          title={stringFromUnknownValue(detailExtension.broken_reason) || t("react.extensions.this_extension_is_broken_and_cannot_be_treated_as_active_functionality", "This extension is broken and cannot be treated as active functionality.")}
          tone="danger"
        />
      ) : null}
      {canAdministerPlatform ? <ExtensionAdminActions detailExtension={detailExtension} panel={panel} /> : null}
      {panel.healthStatus ? <p className="fs-extension-detail-note">{panel.healthStatus}</p> : null}
      {panel.detailStatus ? (
        <DsAlert title={panel.detailStatus} tone={detailStatusSucceeded(panel.detailStatus) ? "success" : "neutral"} />
      ) : null}
      {panel.actionResult ? <ExtensionActionResultPanel guidance={recoveryGuidance(actionRecovery)} result={panel.actionResult} recovery={actionRecovery} /> : null}
      {panel.effectRecoveries?.filter((recovery) => recovery.effect.effect_id !== actionEffectId).map((recovery) => <ExtensionActionResultPanel key={recovery.effect.effect_id} guidance={recoveryGuidance(recovery)} result={{ label: catalogLabel(recovery.effect.operation_id), ok: false, payload: { external_effect: recovery.effect } }} recovery={recovery} />)}
    </>
  );
}

/**
 * The installer's code review: a note for whoever vets extension code, not a
 * warning for everyone. Administrators see it folded, in plain words, with the
 * exact lines under it.
 */
function ExtensionInstallWarnings({ detailExtension }: Readonly<{ detailExtension: ExtensionRecord }>) {
  const { t } = useI18n();
  const warnings = extensionInstallWarnings(detailExtension);
  if (!warnings.length) return null;
  return (
    <details className="fs-settings-section">
      <summary>
        <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
        {t("extensions.detail.code_review_title", "Code review")}
        <span className="fs-settings-section-note">{t("extensions.detail.code_review_count", "{count} to look at", { count: warnings.length })}</span>
      </summary>
      <div className="fs-settings-section-body">
        <p>
          {t(
            "extensions.detail.code_review_body",
            "This extension runs its own code on your server, and some of it starts programs or opens connections. That is normal for many extensions. Install only extensions you trust; a developer can check the lines below.",
          )}
        </p>
        <ul className="fs-extension-install-warnings">
          {warnings.map((warning) => (
            <li key={warning}>
              <code>{warning}</code>
            </li>
          ))}
        </ul>
      </div>
    </details>
  );
}

function ExtensionAdminActions({ detailExtension, panel }: Readonly<{ detailExtension: ExtensionRecord; panel: ExtensionDetailPanelProps }>) {
  const { t } = useI18n();
  const busy = Boolean(panel.busyById[detailExtension.extension_id]);
  return (
    <div className="fs-extension-detail-actions">
      <div className="fs-extension-detail-action-row">
        <button className="fs-button" disabled={busy || Boolean(detailExtension.broken && !detailExtension.active) || Boolean(panel.activationBlockedReason && !detailExtension.active)} onClick={() => ignoreAsyncResult(panel.handleToggle(detailExtension))} type="button">{detailExtension.active ? t("extensions.detail.disable", "Disable") : t("extensions.detail.enable", "Enable")}</button>
        <button className="fs-button" disabled={busy} onClick={() => ignoreAsyncResult(panel.handleHealthCheck(detailExtension))} type="button">{t("extensions.detail.health", "Extension runtime health")}</button>
        {/* Among peer actions a destructive one takes the quiet ghost-danger
            lane; the uninstall plan dialog owns the filled Danger Button. */}
        <button className="fs-button ghost-danger" disabled={busy || !panel.archiveLifecycleAvailable} onClick={() => ignoreAsyncResult(panel.handleUninstall(detailExtension))} type="button">{t("extensions.detail.uninstall", "Uninstall")}</button>
      </div>
      <p className="fs-extension-detail-note">{t("extensions.detail.uninstall_note", "Uninstall removes the extension and the lifecycle data it owns.")}</p>
    </div>
  );
}

function ExtensionProjectScope({ panel }: Readonly<{ panel: ExtensionDetailPanelProps }>) {
  const { t } = useI18n();
  if (!panel.detailExtension.active || !panel.pageRequiresProject) return null;
  return (
    <div className="fs-field fs-extension-detail-scope">
      <span className="fs-field-label">{t("extensions.detail.project_scope", "Project scope")}</span>
      <DsFormSelect
        ariaLabel={t("extensions.detail.project_scope", "Project scope")}
        name="extension_project_scope"
        onChange={(value) => panel.handleProjectSelection(value)}
        options={panel.projectOptions.map((project) => ({ value: project.id, label: project.name || t("react.extensions.project_value0", "Project {value0}", { value0: project.id }) }))}
        placeholder={t("extensions.detail.select_project", "Select a project")}
        value={panel.selectedProjectId || ""}
      />
      {panel.projectsQuery.isError ? (
        <span className="fs-field-helper error" role="alert">
          {panel.projectsQuery.error instanceof Error ? panel.projectsQuery.error.message : t("extensions.detail.projects_failed", "Failed to load projects.")}
        </span>
      ) : null}
      <span className="fs-field-helper">{t("extensions.detail.project_scope_help", "Project-scoped extensions use the selected project for configuration, secrets, channels, and callback setup.")}</span>
      {!panel.selectedProjectId && panel.rememberedProjectId ? (
        <button className="fs-button sm" onClick={() => panel.handleProjectSelection(panel.rememberedProjectId)} type="button">{t("extensions.detail.use_last_project", "Use last project")}</button>
      ) : null}
    </div>
  );
}

function moveManifestTab(
  event: KeyboardEvent<HTMLButtonElement>,
  page: Record<string, unknown>,
  enabledPages: Array<Record<string, unknown>>,
): Record<string, unknown> | undefined {
  const enabledIndex = enabledPages.indexOf(page);
  if (event.key === "ArrowRight") return enabledPages[(enabledIndex + 1) % enabledPages.length];
  if (event.key === "ArrowLeft") return enabledPages[(enabledIndex - 1 + enabledPages.length) % enabledPages.length];
  if (event.key === "Home") return enabledPages[0];
  if (event.key === "End") return enabledPages.at(-1);
  return undefined;
}

function ManifestTabList({
  activeManifestPage,
  activePageOAuthBlocked,
  detailExtension,
  firstUnlockedManifestPage,
  manifestPages,
  pageIsLocked,
  setActiveExtensionPageId,
}: Readonly<{
  activeManifestPage?: Record<string, unknown>;
  activePageOAuthBlocked: boolean;
  detailExtension: ExtensionRecord;
  firstUnlockedManifestPage?: Record<string, unknown>;
  manifestPages: Array<Record<string, unknown>>;
  pageIsLocked: (page: Record<string, unknown>) => boolean;
  setActiveExtensionPageId: (pageId: string) => void;
}>) {
  const { t } = useI18n();
  const enabledPages = manifestPages.filter((page) => !pageIsLocked(page));
  return (
    <div className="fs-tabs-scroll">
      <div aria-label={t("react.extensions.value0_sections", "{value0} sections", { value0: detailExtension.display_name || t("extension_page.extension_fallback", "Extension") })} className="fs-tabs-list" role="tablist">
        {manifestPages.map((page) => {
          const pageId = manifestPageId(page);
          const domPageId = pageId.replace(/[^A-Za-z0-9_-]/g, "-");
          const selected = page === activeManifestPage;
          const locked = pageIsLocked(page);
          return (
            <button
              aria-controls={`extension-page-panel-${domPageId}`}
              aria-disabled={locked}
              aria-selected={selected}
              className="fs-tab"
              disabled={locked}
              id={`extension-page-tab-${domPageId}`}
              key={pageId}
              onClick={() => { if (!locked) setActiveExtensionPageId(pageId); }}
              onKeyDown={(event) => {
                const targetPage = moveManifestTab(event, page, enabledPages);
                if (!targetPage) return;
                event.preventDefault();
                const targetPageId = manifestPageId(targetPage);
                setActiveExtensionPageId(targetPageId);
                document.getElementById(`extension-page-tab-${targetPageId.replace(/[^A-Za-z0-9_-]/g, "-")}`)?.focus();
              }}
              role="tab"
              tabIndex={!locked && (selected || (activePageOAuthBlocked && page === firstUnlockedManifestPage)) ? 0 : -1}
              type="button"
            >
              {stringFrom(page.title || page.label || pageId)}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ActiveOAuthProviderSet({
  activeOAuthProviders,
  activePageId,
  canManageProject,
  oauthConnectionDeclarations,
  panel,
}: Readonly<{
  activeOAuthProviders: ActiveOAuthProvider[];
  activePageId: string;
  canManageProject: boolean;
  oauthConnectionDeclarations: ReturnType<typeof oauthConnectionTypesFromManifest>;
  panel: ExtensionDetailPanelProps;
}>) {
  const { t } = useI18n();
  if (!panel.selectedProjectId || !panel.extensionInstallId) return null;
  return activeOAuthProviders.map(({ provider, mode, connectionTypeIds, intentIds, connectionOwners, ownershipKey, component }) => {
    const testAction = stringFrom(objectOrEmpty(component?.data).test_action);
    return (
      <ExtensionOAuthSetup
        allowedConnectionTypeIds={connectionTypeIds}
        allowedIntentIds={intentIds}
        canManage={canManageProject}
        connectionOwners={connectionOwners}
        connectionTypes={oauthConnectionDeclarations}
        connections={panel.projectConnections}
        connectionsQuery={{ isError: panel.projectConnectionsQuery.isError, isLoading: panel.projectConnectionsQuery.isPending, refetch: panel.projectConnectionsQuery.refetch }}
        focusConnectionId={panel.oauthFocusConnectionId}
        installId={panel.extensionInstallId}
        key={ownershipKey}
        mode={mode}
        onConnectionsChanged={panel.onOAuthConnectionsChanged}
        onNavigatePage={(pageId, anchor) => {
          panel.setActiveExtensionPageId(pageId);
          focusManifestAnchor(pageId, anchor);
        }}
        onTestConnection={testAction && component ? (connection) => panel.handleManifestAction(
          { actionId: testAction, queryId: "", label: t("react.extensions.test_connection", "Test connection"), kind: "action" },
          null,
          component,
          { target: { connection_id: stringFrom(connection.connection_id), project_id: panel.selectedProjectId }, values: { connection_id: stringFrom(connection.connection_id) } },
        ) : undefined}
        projectId={panel.selectedProjectId}
        provider={provider}
        providerSetupReady={panel.oauthProviderReadiness[provider.provider_id] === "ready"}
        returnPageId={activePageId}
      />
    );
  });
}

function ManifestComponentSections({
  activePageId,
  canManageProject,
  extensionDescriptorSha256,
  extensionReleaseId,
  pageOAuthConnectionProviderIds,
  panel,
}: Readonly<{
  activePageId: string;
  canManageProject: boolean;
  extensionDescriptorSha256: string;
  extensionReleaseId: string;
  pageOAuthConnectionProviderIds: string[];
  panel: ExtensionDetailPanelProps;
}>) {
  const { t } = useI18n();
  return listFromUnknown(panel.activeManifestPage?.components).map((component) => {
    const anchorId = stringFrom(component.anchor_id);
    const anchorDomId = manifestAnchorDomId(activePageId, anchorId);
    return (
      <section
        aria-label={anchorDomId ? anchorId : undefined}
        className={anchorDomId ? "scroll-mt-4 outline-none" : "contents"}
        data-extension-anchor={anchorDomId ? anchorId : undefined}
        id={anchorDomId || undefined}
        key={stringFrom(component.component_id || component.id || component.key)}
        tabIndex={anchorDomId ? -1 : undefined}
      >
        {renderManifestComponent(component, {
          t,
          busy: Boolean(panel.busyById[panel.detailExtension.extension_id]),
          canManageProject,
          connectionCreationBlockedReason: panel.connectionCreationBlockedReason,
          callbacks: panel.callbacksQuery.data || [],
          callbackStatuses: panel.callbackStatuses,
          componentErrors: panel.componentErrors,
          componentQueryParams: panel.componentQueryParams,
          componentQueryData: panel.componentQueryData,
          extensionId: panel.detailExtension.extension_id,
          extensionInstallId: panel.extensionInstallId,
          extensionReleaseId,
          extensionDescriptorSha256,
          formValuesByComponent: panel.formValuesByComponent,
          oauthConnectionTypes: panel.oauthConnectionTypes,
          oauthConnectionProviderIds: pageOAuthConnectionProviderIds,
          projectConnections: panel.projectConnections,
          projectState: panel.projectState,
          projectId: panel.selectedProjectId,
          onNavigateToAnchor: (pageId, nextAnchorId) => {
            panel.setActiveExtensionPageId(pageId);
            focusManifestAnchor(pageId, nextAnchorId);
          },
          onCallbackSetup: (sourceComponent, setupAction) => ignoreAsyncResult(panel.handleCallbackSetup(sourceComponent, setupAction)),
          onComponentQueryParamsChange: (componentId, nextParams) => panel.setComponentQueryParams((current) => ({ ...current, [componentId]: nextParams })),
          onFieldChange: (componentId, fieldName, value) => {
            panel.onFieldCorrected(componentId, fieldName);
            panel.setFormValuesByComponent((current) => ({ ...current, [componentId]: { ...(current[componentId] || manifestQueryValues(panel.componentQueryData[componentId])), [fieldName]: value } }));
          },
          onExternalResourcePickerBootstrap: panel.handleExternalResourcePickerBootstrap,
          onManifestAction: (action, form, sourceComponent, overrides) => ignoreAsyncResult(panel.handleManifestAction(action, form, sourceComponent, overrides)),
          onConnectionSave: (sourceComponent, values) => ignoreAsyncResult(panel.onConnectionSave(sourceComponent, values)),
          onConnectionDelete: (sourceComponent, row) => ignoreAsyncResult(panel.onConnectionDelete(sourceComponent, row)),
          onConnectionOAuth: (sourceComponent, row, intent) => ignoreAsyncResult(panel.onConnectionOAuth(sourceComponent, row, intent)),
          onConnectionOAuthDisconnect: (sourceComponent, row) => ignoreAsyncResult(panel.onConnectionOAuthDisconnect(sourceComponent, row)),
          pendingDisconnect: panel.pendingDisconnect,
          onConnectionOAuthDisconnectConfirm: (sourceComponent, row) => ignoreAsyncResult(panel.onConnectionOAuthDisconnectConfirm(sourceComponent, row)),
          onConnectionOAuthDisconnectCancel: panel.onConnectionOAuthDisconnectCancel,
          onConnectionSelect: panel.onConnectionSelect,
          onProjectStateSave: (sourceComponent, values) => ignoreAsyncResult(panel.onProjectStateSave(sourceComponent, values)),
          onProjectStateDelete: (sourceComponent, row) => ignoreAsyncResult(panel.onProjectStateDelete(sourceComponent, row)),
          onProjectStateSelect: panel.onProjectStateSelect,
        }, t)}
      </section>
    );
  });
}

function ActiveManifestPagePanel({
  activeOAuthProviders,
  activePageId,
  activePageOAuthBlocked,
  activePageSetupId,
  canManageProject,
  extensionDescriptorSha256,
  extensionReleaseId,
  oauthConnectionDeclarations,
  pageOAuthConnectionProviderIds,
  panel,
}: Readonly<{
  activeOAuthProviders: ActiveOAuthProvider[];
  activePageId: string;
  activePageOAuthBlocked: boolean;
  activePageSetupId: string;
  canManageProject: boolean;
  extensionDescriptorSha256: string;
  extensionReleaseId: string;
  oauthConnectionDeclarations: ReturnType<typeof oauthConnectionTypesFromManifest>;
  pageOAuthConnectionProviderIds: string[];
  panel: ExtensionDetailPanelProps;
}>) {
  const { t } = useI18n();
  const page = panel.activeManifestPage;
  if (!page) return null;
  const domPageId = activePageId.replace(/[^A-Za-z0-9_-]/g, "-");
  let content;
  if (panel.pageRequiresProject && !panel.selectedProjectId) {
    content = (
      <DsAlert title={t("extensions.detail.select_project_title", "Select a project to continue.")} tone="warning">
        {t("extensions.detail.select_project_body", "This extension stores project-scoped configuration, secrets, channels, and routes. Choose the project above to load its project binding.")}
      </DsAlert>
    );
  } else if (activePageOAuthBlocked) {
    content = (
      <DsAlert title={t("react.extensions.finish_provider_setup", "Finish provider setup")} tone="warning">
        <p>{canManageProject ? t("react.extensions.save_a_ready_oauth_configuration_before_connecting_an_account", "Save a ready OAuth configuration before connecting an account.") : t("react.extensions.a_project_manager_must_finish_provider_setup_before_this_page_is_available", "A project manager must finish provider setup before this page is available.")}</p>
        {canManageProject && activePageSetupId ? (
          <button className="fs-button sm" onClick={() => panel.setActiveExtensionPageId(activePageSetupId)} type="button">{t("react.extensions.open_provider_setup", "Open provider setup")}</button>
        ) : null}
      </DsAlert>
    );
  } else {
    content = <><ActiveOAuthProviderSet activeOAuthProviders={activeOAuthProviders} activePageId={activePageId} canManageProject={canManageProject} oauthConnectionDeclarations={oauthConnectionDeclarations} panel={panel} /><ManifestComponentSections activePageId={activePageId} canManageProject={canManageProject} extensionDescriptorSha256={extensionDescriptorSha256} extensionReleaseId={extensionReleaseId} pageOAuthConnectionProviderIds={pageOAuthConnectionProviderIds} panel={panel} /></>;
  }
  return (
    <div aria-labelledby={`extension-page-tab-${domPageId}`} className="fs-tabs-panel fs-extension-detail-page" id={`extension-page-panel-${domPageId}`} role="tabpanel">
      {stringFrom(page.description) ? <p className="fs-extension-detail-page-description">{stringFrom(page.description)}</p> : null}
      {content}
    </div>
  );
}

export function ExtensionDetailPanel(panelProps: Readonly<ExtensionDetailPanelProps>) {
  const { t } = useI18n();
  const {
    activationBlockedReason,
    activeManifestPage,
    detailExtension,
    extensionInstallId,
    manifestPages,
    manifestQuery,
    oauthProviderReadiness,
    projectConnections,
    setActiveExtensionPageId,
  } = panelProps;
  const safeSupportUrl = safeLinkHref(detailExtension.support_url);
  const safeWebsiteUrl = safeLinkHref(detailExtension.website_url);
  const releaseMetadata = objectOrEmpty(objectOrEmpty(detailExtension.metadata).release);
  const extensionReleaseId = stringFrom(detailExtension.release_id || releaseMetadata.release_id);
  const extensionDescriptorSha256 = stringFrom(releaseMetadata.descriptor_sha256);
  const canManageProject = useHasCapability("mutate_projects");
  const canAdministerPlatform = useHasCapability("administer_platform");
  const { locale } = useI18n();
  const localizedManifest = useMemo(
    () => localizeExtensionManifest(manifestQuery.data, locale),
    [locale, manifestQuery.data],
  );
  const oauthProviders = oauthProvidersFromManifest(localizedManifest);
  const oauthConnectionDeclarations = oauthConnectionTypesFromManifest(localizedManifest);
  const activePageId = manifestPageId(activeManifestPage);
  const pageOAuthConnectionComponents = oauthConnectionComponents(activeManifestPage);
  const pageOAuthConnectionProviderIds = pageOAuthConnectionComponents
    .map(({ data }) => stringFrom(data.oauth_provider_id));
  const pageIsOAuthSetup = oauthProviders.some((provider) => provider.setup_page_id === activePageId);
  const activePageBlockedProviderId = pageOAuthConnectionProviderIds.find(
    (providerId) => oauthProviderReadiness[providerId] !== "ready",
  );
  const activePageSetupId = oauthProviders.find(
    (provider) => provider.provider_id === activePageBlockedProviderId,
  )?.setup_page_id || "";
  const pageIsLocked = (page: Record<string, unknown>): boolean => manifestPageIsLocked(
    page,
    oauthProviders,
    oauthProviderReadiness,
    projectConnections,
    extensionInstallId,
  );
  const activePageOAuthBlocked = !pageIsOAuthSetup
    && Boolean(activeManifestPage && pageIsLocked(activeManifestPage));
  const firstUnlockedManifestPage = manifestPages.find((page) => !pageIsLocked(page));
  const activeOAuthProviders = activeOAuthProviderRows(
    oauthProviders,
    activePageId,
    pageOAuthConnectionComponents,
    manifestOAuthConnectionOwners(manifestPages),
  );

  return (
    <article className="fs-card fs-extension-detail" id="extension-detail-panel">
      <div className="fs-card-body fs-extension-detail-body">
        <ExtensionSummary
          canAdministerPlatform={canAdministerPlatform}
          detailExtension={detailExtension}
          panel={panelProps}
          safeSupportUrl={safeSupportUrl}
          safeWebsiteUrl={safeWebsiteUrl}
        />
        <ExtensionProjectScope panel={panelProps} />
        {!detailExtension.active ? (
          <DsAlert title={activationBlockedReason || t("react.extensions.enable_this_extension_to_configure_its_project_scoped_ui", "Enable this extension to configure its project-scoped UI.")} tone="warning" />
        ) : null}
        {detailExtension.active && manifestQuery.isLoading ? (
          <p className="fs-extension-detail-note">{t("react.extensions.loading_extension_ui", "Loading extension UI…")}</p>
        ) : null}
        {detailExtension.active && manifestQuery.isError ? (
          <DsAlert title={t("react.extensions.extension_ui_could_not_be_loaded", "Extension UI could not be loaded.")} tone="danger">
            {t("react.extensions.refresh_the_page_or_run_a_health_check_for_this_extension", "Refresh the page or run a health check for this extension.")}</DsAlert>
        ) : null}

        {detailExtension.active && manifestPages.length ? (
          <div className="fs-extension-detail-pages">
            <ManifestTabList
              activeManifestPage={activeManifestPage}
              activePageOAuthBlocked={activePageOAuthBlocked}
              detailExtension={detailExtension}
              firstUnlockedManifestPage={firstUnlockedManifestPage}
              manifestPages={manifestPages}
              pageIsLocked={pageIsLocked}
              setActiveExtensionPageId={setActiveExtensionPageId}
            />
            <ActiveManifestPagePanel
              activeOAuthProviders={activeOAuthProviders}
              activePageId={activePageId}
              activePageOAuthBlocked={activePageOAuthBlocked}
              activePageSetupId={activePageSetupId}
              canManageProject={canManageProject}
              extensionDescriptorSha256={extensionDescriptorSha256}
              extensionReleaseId={extensionReleaseId}
              oauthConnectionDeclarations={oauthConnectionDeclarations}
              pageOAuthConnectionProviderIds={pageOAuthConnectionProviderIds}
              panel={panelProps}
            />
          </div>
        ) : null}
        {!manifestPages.length && detailExtension.active &&
          !manifestQuery.isLoading &&
          !manifestQuery.isError ? (
          <DsAlert title={t("react.extensions.this_extension_does_not_publish_custom_ui_pages_yet", "This extension does not publish custom UI pages yet.")} />
        ) : null}

        {canAdministerPlatform ? (
          <details className="fs-settings-section">
            <summary>
              <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
              {t("registry.workflow_detail.drawer.advanced", "Advanced")}<span className="fs-settings-section-note">{t("react.extensions.compiled_manifest", "Compiled manifest")}</span>
            </summary>
            <div className="fs-settings-section-body">
              <pre className="fs-extension-detail-manifest">
                {JSON.stringify(objectOrEmpty(manifestQuery.data), null, 2)}
              </pre>
            </div>
          </details>
        ) : null}
      </div>
    </article>
  );
}
