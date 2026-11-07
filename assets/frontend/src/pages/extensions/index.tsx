import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";

import {
  AdvancedTableFilters,
  DsAlert,
  DsBadge,
  DsPageHeader,
  DsTable,
  DsTabs,
  PageContainer,
  type DsFilterField,
  type DsTableColumn,
} from "@/components/shared/design-system";
import { ConfirmActionDialog } from "@/components/shared/confirm-action-dialog";
import { CatalogIcon } from "@/components/shared/catalog-browse";
import type { ExtensionRecord } from "@/pages/extensions/api";
import {
  EXTENSION_CATALOG_QUERY_KEY,
  EXTENSION_UPDATES_QUERY_KEY,
  EXTENSION_UPDATES_STALE_TIME_MS,
  fetchExtensionUpdates,
  type ExtensionCatalogEntry,
  type ExtensionCatalogInstallState,
  type ExtensionCatalogListing,
} from "@/pages/extensions/catalog-api";
import {
  incompatibleReason,
  mainAction,
  releaseNotesLink,
  updateLabel,
  updateNoticeTitle,
} from "@/pages/extensions/marketplace-model";
import { extensionDocumentationUrl } from "@/pages/extensions/hub-support";

import "@/pages/extensions/extensions.css";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { ExtensionDetailPanel } from "@/pages/extensions/extension-detail-panel";
import { LlmProviderPanel } from "@/pages/extensions/llm-provider-panel";
import {
  stringFrom,
  extensionState,
  resolveExtensionDetailPath,
  extensionHasUi,
} from "@/pages/extensions/extension-manifest-utils";
import { Activity, ExternalLink, Power, Search, Trash2, Upload } from "lucide-react";
import { ExtensionMarketplaceCatalog } from "@/pages/extensions/marketplace-catalog";
import { ExtensionUninstallPlanModal } from "@/components/shared/extension-uninstall-plan-modal";
import {
  BrowserAuthorityApprovalModal,
} from "@/components/shared/browser-authority-approval-modal";
import {
  renderWhen,
  choose,
  catalogLabel,
  anyTrue,
  type ExtensionsViewFilter,
  detailActionStatusFromStatus,
  detailHealthStatusFromStatus,
  allTrue,
  errorMessage,
  emptyExtensionsTitle,
  emptyExtensionsDescription,
  extensionStateBadgeTone,
  translatedExtensionStatus,
} from "./hub-support";

import { useExtensionsHubState } from "./use-extensions-hub-state";
import { useExtensionLifecycleActions } from "./use-extension-lifecycle-actions";
import { useExtensionManifestActions } from "./use-extension-manifest-actions";
import { useExtensionConnectionActions } from "./use-extension-connection-actions";

/**
 * What the Installed page knows about available updates: WordPress's update
 * check, joined to the rows by extension id. Update runs the Catalog's own
 * install call, so approval, progress and failure behave exactly as there.
 */
type ExtensionUpdates = {
  byId: Record<string, ExtensionCatalogEntry>;
  installStateById: Record<string, ExtensionCatalogInstallState>;
  canAdminister: boolean;
  installBlockedReason: string;
  permissionLabels: Record<string, string>;
  onUpdate: (entry: ExtensionCatalogEntry) => void;
};

function releaseNotesAnchor(entry: ExtensionCatalogEntry, t: I18nContextValue["t"]): ReactNode {
  const notes = releaseNotesLink(entry);
  const label = t("react.extensions.view_version_details", "View version {version} details", { version: entry.version });
  return notes.external
    ? <a href={notes.href} rel="noreferrer" target="_blank">{label}</a>
    : <Link to={notes.href}>{label}</Link>;
}

function updateActionFor(entry: ExtensionCatalogEntry, updates: ExtensionUpdates, t: I18nContextValue["t"]) {
  return mainAction(entry, {
    canAdminister: updates.canAdminister,
    installBlockedReason: updates.installBlockedReason,
    installationVersion: "",
  }, t);
}

function updateButton(entry: ExtensionCatalogEntry, updates: ExtensionUpdates, t: I18nContextValue["t"], primary: boolean): ReactNode {
  const phase = updates.installStateById[entry.extension_id]?.phase ?? "idle";
  return (
    <button
      aria-busy={phase === "installing" || undefined}
      className={primary ? "fs-button primary sm" : "fs-button sm"}
      data-extension-update-action={entry.extension_id}
      disabled={phase !== "idle"}
      onClick={() => updates.onUpdate(entry)}
      type="button"
    >
      {updateLabel(phase, entry.version, t)}
    </button>
  );
}

/** WordPress's notice under a plugin row: what is available, what changed, update now. */
function extensionUpdateRowNotice(extensionId: string, updates: ExtensionUpdates, t: I18nContextValue["t"]): ReactNode {
  const install = updates.installStateById[extensionId];
  const entry = updates.byId[extensionId];
  if (!entry) {
    return install?.outcome === "updated"
      ? <output className="fs-extensions-update-notice" data-update-outcome="updated">{install.status}</output>
      : null;
  }
  const action = updateActionFor(entry, updates, t);
  const canUpdate = action.kind === "update" && !action.disabled;
  const failed = install?.phase === "idle" && (install.outcome === "failed" || install.outcome === "superseded") ? install.status : "";
  const reason = entry.display_state === "incompatible" ? incompatibleReason(entry, "", t)?.short ?? "" : "";
  return (
    <div className="fs-extensions-update-notice" data-extension-update={extensionId}>
      <span>{updateNoticeTitle(entry, t)}{reason ? ` ${reason}` : ""}</span>
      {releaseNotesAnchor(entry, t)}
      {canUpdate ? updateButton(entry, updates, t, false) : null}
      {failed ? <span className="fs-extensions-update-failure" role="alert">{failed}</span> : null}
    </div>
  );
}

/** The same notice on the extension's own page, with the Catalog page's trust note. */
function extensionUpdatePageNotice(extensionId: string, updates: ExtensionUpdates, t: I18nContextValue["t"]): ReactNode {
  const install = updates.installStateById[extensionId];
  const entry = updates.byId[extensionId];
  if (!entry) {
    return install?.outcome === "updated" ? <DsAlert title={install.status} tone="success" /> : null;
  }
  const action = updateActionFor(entry, updates, t);
  const addedPermissions = action.kind === "update" ? action.addedPermissions : [];
  const failed = install?.phase === "idle" && install.outcome === "failed" ? install.status : "";
  const notice = install?.phase === "idle" && (install.outcome === "cancelled" || install.outcome === "superseded") ? install.status : "";
  return (
    <>
      <div data-extension-update={extensionId} data-testid="extension-update-notice">
        <DsAlert title={updateNoticeTitle(entry, t)} tone="warning">
          {action.note ? <p>{action.note}</p> : null}
          {addedPermissions.length ? (
            <p>
              {t("react.extensions.this_version_also_asks_for", "This version also asks for:")}{" "}
              {addedPermissions.map((id) => updates.permissionLabels[id] || id).join(", ")}.
            </p>
          ) : null}
          <div className="fs-extensions-update-actions">
            {action.kind === "update" && !action.disabled ? updateButton(entry, updates, t, true) : null}
            {releaseNotesAnchor(entry, t)}
          </div>
        </DsAlert>
      </div>
      {failed ? (
        <DsAlert title={t("react.extensions.the_extension_was_not_updated", "The extension was not updated")} tone="danger">
          {failed}
        </DsAlert>
      ) : null}
      {notice ? <DsAlert title={notice} /> : null}
    </>
  );
}

function createExtensionColumns({
  rememberedProjectId,
  statusById,
  busyById,
  archiveLifecycleAvailable,
}: Pick<ReturnType<typeof useExtensionsHubState>,
  "rememberedProjectId" | "statusById" | "busyById" | "archiveLifecycleAvailable"
>, {
  handleToggle,
  handleHealthCheck,
  handleUninstall,
}: Pick<ReturnType<typeof useExtensionLifecycleActions>,
  "handleToggle" | "handleHealthCheck" | "handleUninstall"
>, updates: ExtensionUpdates, t: I18nContextValue["t"] = fallbackContext.t): DsTableColumn<ExtensionRecord>[] {
  return [
    {
      key: "extension",
      header: t("extension_page.extension_fallback", "Extension"),
      cell: (extension) => {
        const extId = extension.extension_id;
        return (
          <div className="fs-catalog-icon-lockup">
          <CatalogIcon size="row" src={extension.icon_url || ""} />
          <div className="fs-extensions-name-cell">
            <span className="fs-extensions-name-line">
              <Link to={resolveExtensionDetailPath(extension, rememberedProjectId)}>
                {extension.display_name || extId}
              </Link>
              {renderWhen(extensionHasUi(extension), <DsBadge>UI</DsBadge>)}
            </span>
            {renderWhen(Boolean(extension.description), (
              <small>{stringFromUnknownValue(extension.description)}</small>
            ))}
            <small className="fs-extensions-id">{extId}</small>
            {extensionUpdateRowNotice(extId, updates, t)}
          </div>
          </div>
        );
      },
    },
    {
      key: "publisher",
      header: t("react.extensions.publisher", "Publisher"),
      // Without its files there is no manifest to read: say nothing rather
      // than placeholder metadata that reads like a badly made extension.
      cell: (extension) => (extension.files_missing ? "" : extension.vendor_name || extension.author || "—"),
    },
    {
      key: "category",
      header: t("trash.columns.category", "Category"),
      cell: (extension) => (extension.files_missing ? "" : catalogLabel(extension.category) || "Uncategorized"),
    },
    {
      key: "version",
      header: t("registry.data_contracts.column.version", "Version"),
      cell: (extension) => {
        const update = updates.byId[extension.extension_id];
        return (
          <div className="fs-extensions-status-cell">
            <span className="fs-extensions-version">{extension.version || "—"}</span>
            {renderWhen(Boolean(update), (
              <DsBadge tone={update?.display_state === "incompatible" ? "neutral" : "orange"}>
                {t("react.extensions.version_available", "{version} available", { version: update?.version ?? "" })}
              </DsBadge>
            ))}
          </div>
        );
      },
    },
    {
      key: "status",
      header: t("extensions.runtime.status", "Status"),
      width: 140,
      cell: (extension) => {
        const state = extensionState(extension);
        const status = statusById[extension.extension_id]
          || (extension.broken ? extension.broken_reason || t("react.extensions.display_extension_broken", "This extension is broken.") : "");
        return (
          <div className="fs-extensions-status-cell">
            <DsBadge tone={extensionStateBadgeTone(state, Boolean(extension.active))}>{state}</DsBadge>
            {renderWhen(Boolean(status), <small>{translatedExtensionStatus(status, t)}</small>)}
          </div>
        );
      },
    },
    {
      key: "actions",
      header: null,
      ariaLabel: t("extensions.table.actions", "Actions"),
      width: 152,
      cell: (extension) => {
        const extId = extension.extension_id;
        const busy = Boolean(busyById[extId]);
        const toggleLabel = choose(Boolean(extension.active), t("react.extensions.display_disable", "Disable"), t("react.extensions.display_enable", "Enable"));
        return (
          <div className="fs-extensions-actions">
            <Link
              aria-label={t("settings.common.open", "Open")}
              className="fs-icon-button"
              title={t("settings.common.open", "Open")}
              to={resolveExtensionDetailPath(extension, rememberedProjectId)}
            >
              <ExternalLink aria-hidden="true" />
            </Link>
            <button
              aria-label={toggleLabel}
              className="fs-icon-button"
              data-ext-action-index={extId}
              disabled={busy || Boolean(extension.broken && !extension.active)}
              onClick={() => ignoreAsyncResult(handleToggle(extension))}
              title={toggleLabel}
              type="button"
            >
              <Power aria-hidden="true" />
            </button>
            <button
              aria-label={t("react.extensions.extension_runtime_health", "Extension runtime health")}
              className="fs-icon-button"
              disabled={busy}
              onClick={() => ignoreAsyncResult(handleHealthCheck(extension))}
              title={t("react.extensions.extension_runtime_health", "Extension runtime health")}
              type="button"
            >
              <Activity aria-hidden="true" />
            </button>
            <button
              aria-label={t("react.extensions.uninstall", "Uninstall")}
              className="fs-icon-button fs-extensions-uninstall"
              disabled={anyTrue([busy, !archiveLifecycleAvailable])}
              onClick={() => ignoreAsyncResult(handleUninstall(extension))}
              title={t("react.extensions.uninstall", "Uninstall")}
              type="button"
            >
              <Trash2 aria-hidden="true" />
            </button>
          </div>
        );
      },
    },
  ];
}


/**
 * The installed view is a tab panel in the hub and a plain section stack on
 * an extension's own page, where there are no tabs to attach to.
 */
function extensionsPanelClass(detailMode: boolean, active: boolean): string {
  if (detailMode) return "fs-page-sections";
  return active ? "fs-tabs-panel fs-panel-stack" : "hidden";
}

export function ExtensionsHubPage() {
  const { t } = useI18n();
  const ARCHIVE_LIFECYCLE_CHECKING = t("react.extensions.display_archive_lifecycle_checking", "Checking extension archive lifecycle availability…");
  const ARCHIVE_LIFECYCLE_UNVERIFIED = t("react.extensions.display_archive_lifecycle_unverified", "Could not verify whether extension archive changes are available.");
  const ARCHIVE_LIFECYCLE_UNAVAILABLE = t("react.extensions.display_archive_lifecycle_unavailable", "Extension archive changes are unavailable from this web interface.");
  const hubState = useExtensionsHubState();
  const lifecycleActions = useExtensionLifecycleActions(hubState);
  const manifestActions = useExtensionManifestActions(hubState, lifecycleActions);
  const connectionActions = useExtensionConnectionActions(hubState, lifecycleActions);
  const {
    oauthCallbackNotice,
    setOAuthCallbackNotice,
    detailMode,
    detailExtension,
    visibleExtensions,
    extensions,
    detailBackLink,
    activeTab,
    archiveLifecycleQuery,
    installDropActive,
    viewFilter,
    fileInputRef,
    archiveLifecycleAvailable,
    busyById,
    setExtensionSearch,
    extensionSearch,
    setCategoryFilter,
    categoryFilter,
    categoryOptions,
    statusById,
    toggleErrorById,
    activationBlockedReason,
    connectionCreationBlockedReason,
    actionResultByExtension,
    activeManifestPage,
    callbackStatuses,
    callbacksQuery,
    componentErrors,
    componentQueryData,
    componentQueryParams,
    extensionInstallId,
    formValuesByComponent,
    handleProjectSelection,
    manifestPages,
    manifestQuery,
    oauthConnectionTypes,
    oauthProviderReadiness,
    oauthProviderBlockedReasons,
    projectConnectionsQuery,
    projectOptions,
    projectStateQuery,
    activePageRequiresProject,
    projectsQuery,
    rememberedProjectId,
    activePageProjectId,
    handleExtensionPageSelection,
    setComponentQueryParams,
    setFormValuesByComponent,
    oauthFocusConnectionId,
    llmProviderInstallId,
    llmProviderContract,
    llmProviderConnectionConfig,
    queryClient,
    extensionsQuery,
    detailQuery,
    hasCatalogFilters,
    disabledExtensionCount,
    canAdministerPlatform,
    catalogInstallById,
    uninstallPlan,
    uninstallLifecycleAvailable,
    uninstallError,
    setUninstallPlan,
    setUninstallError,
    pendingBrowserApproval,
    browserApprovalError,
    setStatusById,
    setToggleErrorById,
    setPendingBrowserApproval,
    setBrowserApprovalError,
  } = hubState;
  const {
    switchTab,
    handleInstallDragOver,
    handleInstallDragLeave,
    handleInstallDrop,
    handleViewFilterChange,
    handleInstallFileChange,
    handleHealthCheck,
    handleToggle,
    handleUninstall,
    installCatalogExtension,
    confirmUninstall,
    confirmBrowserAuthorityApproval,
  } = lifecycleActions;
  const {
    handleCallbackSetup,
    handleExternalResourcePickerBootstrap,
    handleManifestAction,
  } = manifestActions;
  const {
    oauthCallbackMessage,
    showArchiveLifecycleLoading,
    showArchiveLifecycleVerificationFailure,
    showArchiveLifecycleUnavailable,
    clearFieldError,
    handleConnectionSave,
    handleConnectionDelete,
    handleConnectionOAuth,
    handleConnectionOAuthDisconnect,
    handleConnectionSelect,
    handleProjectStateSave,
    handleProjectStateDelete,
    handleProjectStateSelect,
    confirmConnectionOAuthDisconnect,
    pendingDisconnect,
    setPendingDisconnect,
    confirmation,
    confirmationPending,
    confirmationError,
    cancelConfirmation,
    acceptConfirmation,
  } = connectionActions;

  const statusFilterOptions = [
    { value: "active", label: t("users.status.active", "Active") },
    { value: "disabled", label: t("react.extensions.disabled", "Disabled") },
  ];
  const filterFields: DsFilterField[] = [
    { key: "state", label: t("extensions.runtime.status", "Status"), options: statusFilterOptions },
    {
      key: "category",
      label: t("trash.columns.category", "Category"),
      options: categoryOptions.map((category) => ({ value: category, label: catalogLabel(category) })),
    },
  ];
  const installNoticeFailed = Boolean(toggleErrorById.__install__);
  // The page's own words for why archive installs are off, beside Install on
  // a Marketplace extension's page as they are in the alert above the tabs.
  let archiveInstallBlockedReason = "";
  if (showArchiveLifecycleLoading) archiveInstallBlockedReason = ARCHIVE_LIFECYCLE_CHECKING;
  else if (showArchiveLifecycleVerificationFailure) archiveInstallBlockedReason = ARCHIVE_LIFECYCLE_UNVERIFIED;
  else if (!archiveLifecycleAvailable) archiveInstallBlockedReason = ARCHIVE_LIFECYCLE_UNAVAILABLE;
  const detailPublisher = stringFromUnknownValue(detailExtension?.vendor_name || detailExtension?.author).trim();

  // WordPress's update check: the server reuses the catalog's answer for an
  // hour, and a page that cannot reach it says so without blocking anything.
  const updatesQuery = useQuery({
    queryKey: EXTENSION_UPDATES_QUERY_KEY,
    queryFn: () => fetchExtensionUpdates(),
    retry: false,
    staleTime: EXTENSION_UPDATES_STALE_TIME_MS,
  });
  const extensionUpdates: ExtensionUpdates = {
    byId: Object.fromEntries((updatesQuery.data?.items ?? []).map((entry) => [entry.extension_id, entry])),
    installStateById: catalogInstallById,
    canAdminister: canAdministerPlatform,
    installBlockedReason: archiveInstallBlockedReason,
    permissionLabels: updatesQuery.data?.permission_labels ?? {},
    onUpdate: installCatalogExtension,
  };
  const updatesUnchecked = !detailMode && (
    updatesQuery.isError
    || Boolean(updatesQuery.data && !updatesQuery.data.checked && updatesQuery.data.availability.configured)
  );
  const checkForUpdatesAgain = () => ignoreAsyncResult(queryClient.fetchQuery({
    queryKey: EXTENSION_UPDATES_QUERY_KEY,
    queryFn: () => fetchExtensionUpdates(true),
    staleTime: 0,
  }));

  const extensionColumns = createExtensionColumns(hubState, lifecycleActions, extensionUpdates, t);
  let tableState: ReactNode = null;
  if (extensionsQuery.isLoading) {
    tableState = (
      <div className="fs-table-state">
        <strong>{t("react.extensions.loading_extensions", "Loading extensions")}</strong>
        <span>{t("react.extensions.the_table_keeps_its_shape_while_the_list_loads", "The table keeps its shape while the list loads.")}</span>
      </div>
    );
  } else if (!visibleExtensions.length) {
    tableState = (
      <div className="fs-table-state">
        <strong>{emptyExtensionsTitle(hasCatalogFilters, viewFilter, disabledExtensionCount, t)}</strong>
        <span>{emptyExtensionsDescription(hasCatalogFilters, viewFilter, disabledExtensionCount, t)}</span>
        {renderWhen(
          allTrue([!hasCatalogFilters, viewFilter === "active", disabledExtensionCount > 0]),
          // One click to the thing they already have. Telling someone to
          // "choose Disabled or All" is the same dead end one sentence later.
          <button
            className="fs-button"
            data-testid="extensions-show-disabled"
            onClick={() => handleViewFilterChange("disabled")}
            type="button"
          >
            {t("react.extensions.show_installed_extensions", "Show installed extensions")}</button>,
        )}
      </div>
    );
  }

  const installAction = renderWhen(!detailMode, (
    <>
      <input
        accept=".zip,application/zip"
        aria-label={t("react.extensions.extension_package_file", "Extension package file")}
        className="fs-sr-only"
        disabled={!archiveLifecycleAvailable}
        id="extension-package-file"
        name="extension_package_file"
        onChange={(event) => {
          const nextFile = event.target.files?.[0] || null;
          ignoreAsyncResult(handleInstallFileChange(nextFile));
          event.currentTarget.value = "";
        }}
        ref={fileInputRef}
        tabIndex={-1}
        type="file"
      />
      <button
        className="fs-button primary"
        disabled={anyTrue([!archiveLifecycleAvailable, Boolean(busyById.__install__)])}
        onClick={() => fileInputRef.current?.click()}
        type="button"
      >
        <Upload aria-hidden="true" />
        {t("react.extensions.install_extension_from_file", "Install extension from file")}</button>
    </>
  ));

  return (
    <PageContainer className="fs-page-sections" width="wide">
      {renderWhen(Boolean(oauthCallbackNotice), (
        <DsAlert
          title={oauthCallbackMessage}
          tone={choose(oauthCallbackNotice?.kind === "error", "danger" as const, "success" as const)}
        >
          <button
            aria-label={t("react.extensions.dismiss_authorization_notice", "Dismiss authorization notice")}
            className="fs-button sm"
            onClick={() => setOAuthCallbackNotice(null)}
            type="button"
          >
            {t("react.extensions.dismiss", "Dismiss")}</button>
        </DsAlert>
      ))}
      <DsPageHeader
        action={
          <>
            {renderWhen(detailMode, (
              <Link className="fs-button" to={detailBackLink.target}>
                {detailBackLink.label}
              </Link>
            ))}
            {installAction}
          </>
        }
        description={choose(
          detailMode,
          [
            detailExtension?.description || t("react.extensions.display_manage_configuration_runtime", "Manage this extension's configuration and runtime."),
            detailPublisher ? t("react.extensions.display_published_by", "Published by {publisher}.", { publisher: detailPublisher }) : "",
          ].filter(Boolean).join(" "),
          t("react.extensions.display_manage_installed_extensions", "Manage installed extensions and review available integrations."),
        )}
        // Canon's page header has no meta row: the eyebrow carries the object's
        // type, and the id is already on the detail panel.
        eyebrow={choose(detailMode, catalogLabel(detailExtension?.category) || t("extension_page.extension_fallback", "Extension"), t("react.extensions.display_system", "System"))}
        media={detailMode ? <CatalogIcon size="page" src={stringFromUnknownValue(detailExtension?.icon_url)} /> : undefined}
        title={choose(
          detailMode,
          detailExtension?.display_name || detailExtension?.extension_id || t("react.extensions.display_extension_detail", "Extension detail"),
          t("react.extensions.display_extensions_hub", "Extensions hub"),
        )}
      />

      {renderWhen(showArchiveLifecycleLoading, (
        <DsAlert title={ARCHIVE_LIFECYCLE_CHECKING} />
      ))}
      {renderWhen(showArchiveLifecycleVerificationFailure, (
        <DsAlert title={ARCHIVE_LIFECYCLE_UNVERIFIED} tone="warning" />
      ))}
      {renderWhen(showArchiveLifecycleUnavailable, (
        <DsAlert title={ARCHIVE_LIFECYCLE_UNAVAILABLE} tone="warning">
          {(archiveLifecycleQuery.data?.remediation.install_update ?? []).map((command) => (
            <p key={`install-update-${command}`}>
              {t("react.extensions.install_or_update_with", "Install or update with")}{" "}<code>{command}</code>.
            </p>
          ))}
          {(archiveLifecycleQuery.data?.remediation.uninstall ?? []).map((command) => (
            <p key={`uninstall-${command}`}>
              {t("react.extensions.uninstall_with", "Uninstall with")}{" "}<code>{command}</code>.
            </p>
          ))}
        </DsAlert>
      ))}
      {renderWhen(Boolean(statusById.__install__), (
        // One slot reports progress, success and failure. A refusal is
        // whatever the server said and is printed unchanged — nothing
        // here reads it — but it is framed as a failure so it cannot be
        // mistaken for the confirmation that follows a good install.
        <div data-install-notice={choose(installNoticeFailed, "error", "status")} data-testid="extension-install-notice">
          <DsAlert
            title={translatedExtensionStatus(statusById.__install__, t)}
            tone={choose(installNoticeFailed, "danger" as const, "neutral" as const)}
          />
        </div>
      ))}

      {/* Canon: the tab row sits directly on its panel, with no section gap
          between them, so both live in one wrapper that adds none. */}
      <div className={choose(detailMode, "fs-page-sections", "fs-settings-tabs")}>
      {renderWhen(!detailMode, (
        <DsTabs
          active={activeTab}
          ariaLabel={t("react.extensions.extensions_tabs", "Extensions tabs")}
          idPrefix="extensions"
          items={[
            { key: "installed", label: t("react.extensions.installed", "Installed") },
            { key: "marketplace", label: t("react.extensions.marketplace", "Catalog") },
          ]}
          onSelect={(key) => switchTab(key as "installed" | "marketplace")}
          panelIdFor={(key) => `extensions-${key}-content`}
        />
      ))}

      <div
        aria-labelledby={choose(detailMode, undefined, "extensions-tab-installed")}
        className={extensionsPanelClass(detailMode, activeTab === "installed")}
        id="extensions-installed-content"
        role={choose(detailMode, undefined, "tabpanel" as const)}
      >
        {detailExtension ? (
          <>
            {extensionUpdatePageNotice(detailExtension.extension_id, extensionUpdates, t)}
            <ExtensionDetailPanel
            activationBlockedReason={activationBlockedReason}
            connectionCreationBlockedReason={connectionCreationBlockedReason}
            actionResult={actionResultByExtension[detailExtension.extension_id]?.scopeKey && actionResultByExtension[detailExtension.extension_id]?.scopeKey !== manifestActions.scopeKey ? undefined : actionResultByExtension[detailExtension.extension_id]}
            effectRecoveries={manifestActions.effectRecoveries}
            activeManifestPage={activeManifestPage}
            busyById={busyById}
            callbackStatuses={callbackStatuses}
            callbacksQuery={callbacksQuery}
            componentErrors={componentErrors}
            componentQueryData={componentQueryData}
            componentQueryParams={componentQueryParams}
            detailExtension={detailExtension}
            extensionInstallId={extensionInstallId}
            detailStatus={
              translatedExtensionStatus(toggleErrorById[detailExtension.extension_id]
              || detailActionStatusFromStatus(statusById[detailExtension.extension_id]), t)
            }
            formValuesByComponent={formValuesByComponent}
            handleCallbackSetup={handleCallbackSetup}
            handleHealthCheck={handleHealthCheck}
            handleExternalResourcePickerBootstrap={handleExternalResourcePickerBootstrap}
            healthStatus={translatedExtensionStatus(detailHealthStatusFromStatus(statusById[detailExtension.extension_id]), t)}
            handleManifestAction={handleManifestAction}
            handleProjectSelection={handleProjectSelection}
            handleToggle={handleToggle}
            handleUninstall={handleUninstall}
            archiveLifecycleAvailable={archiveLifecycleAvailable}
            manifestPages={manifestPages}
            manifestQuery={manifestQuery}
            oauthConnectionTypes={oauthConnectionTypes}
            oauthProviderReadiness={oauthProviderReadiness}
            oauthProviderBlockedReasons={oauthProviderBlockedReasons}
            projectConnections={projectConnectionsQuery.data || []}
            projectConnectionsQuery={projectConnectionsQuery}
            projectOptions={projectOptions}
            projectState={projectStateQuery.data || []}
            pageRequiresProject={activePageRequiresProject}
            projectsQuery={projectsQuery}
            rememberedProjectId={rememberedProjectId}
            selectedProjectId={activePageProjectId}
            setActiveExtensionPageId={handleExtensionPageSelection}
            setComponentQueryParams={setComponentQueryParams}
            onFieldCorrected={clearFieldError}
            setFormValuesByComponent={setFormValuesByComponent}
            onConnectionSave={handleConnectionSave}
            onConnectionDelete={handleConnectionDelete}
            onConnectionOAuth={handleConnectionOAuth}
            onConnectionOAuthDisconnect={handleConnectionOAuthDisconnect}
            pendingDisconnect={pendingDisconnect}
            onConnectionOAuthDisconnectConfirm={confirmConnectionOAuthDisconnect}
            onConnectionOAuthDisconnectCancel={() => setPendingDisconnect(null)}
            onOAuthConnectionsChanged={() => projectConnectionsQuery.refetch().then(() => undefined)}
            onConnectionSelect={handleConnectionSelect}
            onProjectStateSave={handleProjectStateSave}
            onProjectStateDelete={handleProjectStateDelete}
            onProjectStateSelect={handleProjectStateSelect}
            oauthFocusConnectionId={oauthFocusConnectionId}
            />
            {renderWhen(Boolean(llmProviderInstallId), (
              <LlmProviderPanel
                contract={llmProviderContract}
                initialConnectionConfig={llmProviderConnectionConfig}
                initialRemoteBaseUrl={stringFrom(detailExtension.remote_base_url)}
                runtimeMode={stringFrom(detailExtension.runtime_mode)}
                documentationUrl={extensionDocumentationUrl(
                  detailExtension,
                  queryClient.getQueryData<ExtensionCatalogListing>(EXTENSION_CATALOG_QUERY_KEY),
                )}
                installId={llmProviderInstallId}
                installEnabled={detailExtension.active === true}
                key={llmProviderInstallId}
                onConnectionSaved={() => Promise.all([
                  queryClient.invalidateQueries({ queryKey: ["extensions", "all"] }),
                  queryClient.invalidateQueries({ queryKey: ["extensions", "detail", detailExtension.extension_id] }),
                ]).then(() => undefined)}
              />
            ))}
          </>
        ) : null}

        {renderWhen(allTrue([detailMode, !detailExtension, !extensionsQuery.isLoading, !detailQuery.isLoading]), (
          <DsAlert title={t("react.extensions.extension_was_not_found", "Extension was not found.")} tone="danger" />
        ))}

        {renderWhen(allTrue([!detailMode, extensionsQuery.isError]), (
          <DsAlert title={t("react.extensions.extensions_could_not_be_loaded", "Extensions could not be loaded")} tone="danger">
            {errorMessage(extensionsQuery.error, t("react.extensions.failed_to_load_extensions", "Failed to load extensions."))}
          </DsAlert>
        ))}

        {renderWhen(updatesUnchecked, (
          <div data-testid="extension-updates-unchecked">
            <DsAlert title={t("react.extensions.updates_check_failed", "Couldn't check for extension updates.")} tone="warning">
              <p>{t("react.extensions.updates_check_failed_detail", "The catalog could not be reached. Installed extensions keep working.")}</p>
              <button
                className="fs-button sm"
                disabled={updatesQuery.isFetching}
                onClick={checkForUpdatesAgain}
                type="button"
              >
                {t("react.extensions.check_again", "Check again")}
              </button>
            </DsAlert>
          </div>
        ))}

        {renderWhen(!detailMode, (
          <article
            className={choose(installDropActive, "fs-card fs-table-card is-drop-target", "fs-card fs-table-card")}
            data-testid="extension-install-dropzone"
            onDragEnter={handleInstallDragOver}
            onDragLeave={handleInstallDragLeave}
            onDragOver={handleInstallDragOver}
            onDrop={(event) => {
              ignoreAsyncResult(handleInstallDrop(event));
            }}
          >
            <div className="fs-table-toolbar">
              <AdvancedTableFilters
                fields={filterFields}
                onChange={(key, value) => {
                  if (key === "state") handleViewFilterChange((value || "all") as ExtensionsViewFilter);
                  if (key === "category") setCategoryFilter(value || "all");
                }}
                onClear={() => {
                  handleViewFilterChange("all");
                  setCategoryFilter("all");
                }}
                values={{
                  state: choose(viewFilter === "all", "", viewFilter),
                  category: choose(categoryFilter === "all", "", categoryFilter),
                }}
              />
              <label className="fs-search">
                <Search aria-hidden="true" />
                <input
                  aria-label={t("react.extensions.search_extensions", "Search extensions")}
                  onChange={(event) => setExtensionSearch(event.target.value)}
                  placeholder={t("react.extensions.search_extensions_variant_0f46a3c9", "Search extensions…")}
                  type="search"
                  value={extensionSearch}
                />
              </label>
              <span aria-live="polite" className="fs-extensions-count">
                {choose(
                  extensionsQuery.isLoading,
                  "",
                  t("react.extensions.display_extension_visible_count", "{visible} of {total} · drop a .zip here to install", { visible: visibleExtensions.length, total: extensions.length }),
                )}
              </span>
            </div>
            <DsTable
              ariaLabel={t("react.extensions.installed_extensions", "Installed extensions")}
              busy={extensionsQuery.isFetching && !extensionsQuery.isLoading}
              columns={extensionColumns}
              rowKey={(extension) => extension.extension_id}
              rows={visibleExtensions}
              state={tableState}
            />
          </article>
        ))}
      </div>

      <div
        aria-labelledby="extensions-tab-marketplace"
        className={choose(allTrue([activeTab === "marketplace", !detailMode]), "fs-tabs-panel", "hidden")}
        id="extensions-marketplace-content"
        role="tabpanel"
      >
        {/* The tab keeps its place and its API surface; the static catalog is
            what fills it. Installing from a file remains the other way in. */}
        <ExtensionMarketplaceCatalog
          active={allTrue([activeTab === "marketplace", !detailMode])}
          // Both preconditions the installer itself enforces for an uploaded
          // archive, each with its own visible reason; offering the action to
          // anyone else only produces a refusal.
          canAdminister={canAdministerPlatform}
          installBlockedReason={archiveInstallBlockedReason}
          installStateById={catalogInstallById}
          onInstall={installCatalogExtension}
        />
      </div>
      </div>
      <ConfirmActionDialog
        confirmLabel={confirmation?.confirmLabel ?? ""}
        confirmTone={confirmation?.tone}
        consequence={confirmation?.consequence ?? ""}
        error={confirmationError}
        eyebrow={detailExtension?.display_name || detailExtension?.extension_id || t("extension_page.extension_fallback", "Extension")}
        irreversible={confirmation?.irreversible ?? true}
        onCancel={cancelConfirmation}
        onConfirm={() => ignoreAsyncResult(acceptConfirmation())}
        open={Boolean(confirmation)}
        pending={confirmationPending}
        pendingLabel={confirmation?.pendingLabel ?? ""}
        title={confirmation?.title ?? ""}
      />
      <ExtensionUninstallPlanModal
        busy={Boolean(uninstallPlan && busyById[uninstallPlan.extension_id])}
        confirmDisabled={!uninstallLifecycleAvailable}
        error={uninstallError}
        key={choose(Boolean(uninstallPlan), `${uninstallPlan?.extension_id}:${uninstallPlan?.revision}`, "closed")}
        onClose={() => { setUninstallPlan(null); setUninstallError(""); }}
        onConfirm={() => ignoreAsyncResult(confirmUninstall())}
        plan={uninstallPlan}
      />
      <BrowserAuthorityApprovalModal
        approval={pendingBrowserApproval?.approval ?? null}
        // A catalog install holds its busy state in `catalogInstallById`, not
        // `busyById`; without it here Confirm stayed enabled during the retry
        // and a second click started a second install.
        busy={anyTrue([
          Object.values(busyById).some(Boolean),
          Object.values(catalogInstallById).some((state) => state.phase === "installing"),
        ])}
        error={browserApprovalError}
        onCancel={() => {
          if (pendingBrowserApproval) {
            // A caller with its own state reports the cancellation itself.
            // Writing `statusById` as well would leave the message on the
            // installed extension's row — keyed by extension id, never
            // cleared, and still there after a later successful install.
            if (pendingBrowserApproval.onAbandoned) {
              pendingBrowserApproval.onAbandoned();
            } else {
              setStatusById((current) => ({
                ...current,
                [pendingBrowserApproval.statusKey]: pendingBrowserApproval.cancelMessage,
              }));
            }
            // Declining an approval is an outcome, not a failure, so the notice
            // must not stay framed as one — whoever reported it.
            setToggleErrorById((current) => ({
              ...current,
              [pendingBrowserApproval.statusKey]: "",
            }));
          }
          setPendingBrowserApproval(null);
          setBrowserApprovalError("");
        }}
        onConfirm={() => ignoreAsyncResult(confirmBrowserAuthorityApproval())}
      />
    </PageContainer>
  );
}
