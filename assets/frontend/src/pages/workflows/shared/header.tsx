import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import type { PropsWithChildren, ReactNode } from "react";
import { useState } from "react";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { Link } from "react-router";

import { DsAlert, DsBadge, DsValueChip, RowActionMenu } from "@/components/shared/design-system";
import { useHasCapability } from "@/lib/auth/capabilities";
import { findWorkflowLinkedProjectId } from "@/pages/workflows/shared/project-context";
import {
  fetchWorkflowExportPolicy,
  type WorkflowExportPolicy,
} from "@/pages/workflows/shared/api";
import {
  missingExtensionActionLabel,
  missingExtensionAskAdministrator,
  missingExtensionHeadline,
  missingExtensionHref,
  missingExtensionNotice,
  missingExtensionName,
  type MissingExtensionNoticeModel,
} from "@/pages/workflows/shared/missing-extension";
import { workflowSourceExtension } from "@/pages/workflows/shared/source-extension";
import type { WorkflowPageTab } from "@/pages/workflows/shared/workflow-page-tabs";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export type WorkflowHeaderPage = "detail" | "overview" | "projects" | "builder";

type WorkflowSummaryHeaderProps = {
  activePage: WorkflowHeaderPage;
  description: string;
  edgeCount?: number;
  enabled: boolean;
  errorCount: number;
  isValid: boolean;
  nodeCount?: number;
  portability?: WorkflowExportPolicy;
  scaffoldOnly?: boolean;
  /** The tab selected after Overview and Projects (`?tab=`); empty on Overview itself. */
  activeTab?: string;
  /** Hooks and the settings tabs, in order, linking to the overview page's `?tab=`. */
  extraTabs?: WorkflowPageTab[];
  /** Workflow authoring is admin-only; hide authoring controls (
   * clone, delete, settings, enable-toggle) when false. Defaults to true. */
  canAuthor?: boolean;
  onCloneWorkflow?: (projectId: string) => void;
  onDeleteWorkflow?: () => void;
  onToggleEnabled?: () => void;
  onWorkflowTestRun?: () => void;
  onWorkflowSettings?: () => void;
  stepCount: number;
  projectId?: string;
  summaryId?: string;
  structureRedacted?: boolean;
  title: string;
  togglingEnabled?: boolean;
  warningCount: number;
  workflowId: string;
};

function workflowPath(workflowId: string, suffix = "") {
  const encodedWorkflowId = encodeURIComponent(workflowId);
  return `/registry/workflows/${encodedWorkflowId}${suffix}`;
}

function validationTone(
  structureRedacted: boolean,
  scaffoldOnly: boolean,
  valid: boolean,
): "neutral" | "orange" {
  if (structureRedacted) {
    return "neutral";
  }
  return scaffoldOnly || !valid ? "orange" : "neutral";
}

function validationLabel(
  structureRedacted: boolean,
  scaffoldOnly: boolean,
  valid: boolean,
  errorCount: number,
  warningCount: number, t: I18nContextValue["t"] = fallbackContext.t
): string {
  if (structureRedacted) {
    return t("react.workflows.managed_by_extension", "managed by extension");
  }
  if (scaffoldOnly) {
    return t("react.workflows.scaffold_only", "scaffold only");
  }
  return valid ? t("react.workflows.display_valid", "valid") : t("react.workflows.value0_errors_value1_warnings", "{value0} errors / {value1} warnings", { value0: errorCount, value1: warningCount });
}

type WorkflowHeaderPolicy = {
  cloneAllowed: boolean;
  deleteAllowed: boolean;
  effectiveScaffoldOnly: boolean;
  effectiveStructureRedacted: boolean;
  extensionOwned: boolean;
  fullExportAllowed: boolean;
  internalStructureAllowed: boolean;
  lifecycleToggleAllowed: boolean;
  policyLoaded: boolean;
  protectionReason: string;
  showInternalStructureActions: boolean;
  unavailable: boolean;
};

function workflowHeaderPolicy(
  exportPolicy: WorkflowExportPolicy | undefined,
  scaffoldOnly: boolean,
  structureRedacted: boolean,
): WorkflowHeaderPolicy {
  const policyLoaded = Boolean(exportPolicy);
  const fullExportAllowed = policyLoaded && Boolean(exportPolicy?.full_export_allowed);
  const internalStructureAllowed = policyLoaded && Boolean(exportPolicy?.structure_change_allowed);
  const extensionOwned = Boolean(exportPolicy?.extension_owned);
  const effectiveStructureRedacted = Boolean(structureRedacted)
    || (extensionOwned && exportPolicy?.protected === true);
  return {
    cloneAllowed: policyLoaded && Boolean(exportPolicy?.clone_allowed),
    deleteAllowed: policyLoaded && internalStructureAllowed,
    effectiveScaffoldOnly: !effectiveStructureRedacted && scaffoldOnly,
    effectiveStructureRedacted,
    extensionOwned,
    fullExportAllowed,
    internalStructureAllowed,
    lifecycleToggleAllowed: policyLoaded && (!extensionOwned || exportPolicy?.protected !== true),
    policyLoaded,
    protectionReason: firstStringFromUnknownValue([
      exportPolicy?.availability_reason_text,
      exportPolicy?.reason,
      "",
    ]).trim(),
    unavailable: Boolean(exportPolicy?.availability_status)
      && String(exportPolicy?.availability_status).trim().toLowerCase() !== "available",
    showInternalStructureActions: policyLoaded && internalStructureAllowed,
  };
}

function workflowPathWithProject(
  workflowId: string,
  projectId: string,
  suffix = "",
): string {
  const basePath = workflowPath(workflowId, suffix);
  if (!projectId) return basePath;
  return `${basePath}?project_id=${encodeURIComponent(projectId)}`;
}

function workflowPathWithParams(workflowId: string, projectId: string, params: Record<string, string>): string {
  const query = new URLSearchParams();
  if (projectId) query.set("project_id", projectId);
  for (const [key, value] of Object.entries(params)) query.set(key, value);
  return `${workflowPath(workflowId)}?${query.toString()}`;
}

function menuCommand(
  builderPath: string,
  closeMenu: () => void,
  label: string,
  query: string,
  handler?: () => void,
  contextReady = true,
) {
  if (handler) {
    return (
      <button
        onClick={() => {
          closeMenu();
          handler();
        }}
        role="menuitem"
        type="button"
      >
        {label}
      </button>
    );
  }
  if (!contextReady) return <button disabled role="menuitem" type="button">{label}</button>;
  const separator = builderPath.includes("?") ? "&" : "?";
  return (
    <Link
      onClick={closeMenu}
      role="menuitem"
      to={`${builderPath}${separator}${query}`}
    >
      {label}
    </Link>
  );
}

function workflowMenuItems({
  builderPath,
  settingsPath,
  canAuthor,
  cloneAllowed,
  contextReady,
  closeMenu,
  deleteAllowed,
  exportPath,
  fullExportAllowed,
  onCloneWorkflow,
  onDeleteWorkflow,
  onWorkflowSettings,
  onWorkflowTestRun,
  projectId,
  showInternalStructureActions,
}: {
  builderPath: string;
  settingsPath: string;
  canAuthor: boolean;
  cloneAllowed: boolean;
  contextReady: boolean;
  closeMenu: () => void;
  deleteAllowed: boolean;
  exportPath: string;
  fullExportAllowed: boolean;
  onCloneWorkflow?: (projectId: string) => void;
  onDeleteWorkflow?: () => void;
  onWorkflowSettings?: () => void;
  onWorkflowTestRun?: () => void;
  projectId: string;
  showInternalStructureActions: boolean;
}, t: I18nContextValue["t"] = fallbackContext.t): ReactNode[] {
  const items: ReactNode[] = [];
  if (canAuthor) {
    items.push(
      <div className="fs-row-menu-group" data-workflow-header-action="workflow-settings" key="workflow-settings">
        {menuCommand(settingsPath, closeMenu, t("react.workflows.manual_workflow_settings", "Workflow settings"), "tab=smart-setup", onWorkflowSettings, contextReady)}
      </div>,
    );
  }
  if (showInternalStructureActions && canAuthor) {
    items.push(
      <div className="fs-row-menu-group" data-workflow-header-action="workflow-test-run" key="workflow-test-run">
        {menuCommand(builderPath, closeMenu, t("registry.workflow_detail.run_lab.tab.test_run", "Test Run"), "drawer=workflow-test-run", onWorkflowTestRun, contextReady)}
      </div>,
    );
  }
  if (fullExportAllowed) {
    items.push(
      <div className="fs-row-menu-group" key="export-yaml"><a data-workflow-header-action="export-yaml" href={exportPath} role="menuitem">{t("react.workflows.export_yaml", "Export YAML")}</a></div>,
      <div className="fs-row-menu-group" key="export-yaml-children"><a data-workflow-header-action="export-yaml-children" href={`${exportPath}?include_children=true`} role="menuitem" title={t("react.workflows.export_this_workflow_with_every_workflow_it_calls_in_one_file", "Export this workflow with every workflow it calls, in one file.")}>{t("react.workflows.export_yaml_with_child_workflows", "Export YAML with child workflows")}</a></div>,
      <p key="export-note-images" className="px-3 py-2 text-xs text-muted-foreground">{t("react.workflows.note_images_not_exported", "Note images are not included in exports. Upload them again after importing.")}</p>,
    );
  }
  if (canAuthor && onCloneWorkflow && cloneAllowed) {
    items.push(<div className="fs-row-menu-group" key="clone"><button data-workflow-header-action="clone" onClick={() => { closeMenu(); onCloneWorkflow(projectId); }} role="menuitem" type="button">{t("react.workflows.clone_workflow_variant_1f40b1c8", "Clone Workflow")}</button></div>);
  }
  if (canAuthor && onDeleteWorkflow && deleteAllowed) {
    items.push(
      <div className="fs-row-menu-separator" key="destructive-divider" />,
      <div className="fs-row-menu-group" key="delete"><button className="danger" data-workflow-header-action="delete" onClick={() => { closeMenu(); onDeleteWorkflow(); }} role="menuitem" type="button">{t("react.workflows.delete_workflow", "Delete Workflow")}</button></div>,
    );
  }
  return items;
}

/**
 * Why an extension workflow cannot run, said once and for a person: what it
 * needs by name, what to do about it (Install or Turn on for an administrator,
 * ask one for everyone else), and the extension id only under Details. The
 * attention tone is the published warning alert, readable in both themes.
 */
export function WorkflowMissingExtensionNotice({ model }: Readonly<{ model: MissingExtensionNoticeModel }>) {
  const { t } = useI18n();
  const named = model.details.length > 1;
  return (
    <div className="min-w-0" data-testid="workflow-missing-extension-notice">
      <DsAlert title={missingExtensionHeadline(model, t)} tone="warning">
        <div className="grid gap-2">
          {model.viewerCanManageExtensions ? (
            <div className="flex flex-wrap gap-2">
              {model.details.map((detail) => (
                <Link
                  className="fs-button"
                  data-missing-extension-action={detail.state || "unknown"}
                  key={detail.extension_id}
                  to={missingExtensionHref(detail)}
                >
                  {missingExtensionActionLabel(detail, named, t)}
                </Link>
              ))}
            </div>
          ) : (
            <p>{missingExtensionAskAdministrator(model, t)}</p>
          )}
          <details className="fs-settings-section">
            <summary>
              <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
              {t("react.workflows.missing_extension_details", "Details")}
            </summary>
            <div className="fs-settings-section-body">
              <ul className="grid gap-1">
                {model.details.map((detail) => (
                  <li className="flex flex-wrap items-center gap-2" key={detail.extension_id}>
                    <span>{missingExtensionName(detail)}</span>
                    <DsValueChip>{detail.extension_id}</DsValueChip>
                  </li>
                ))}
              </ul>
              {model.technicalReason ? <p className="break-words">{model.technicalReason}</p> : null}
            </div>
          </details>
        </div>
      </DsAlert>
    </div>
  );
}

/**
 * Why the workflow cannot run, below the badge row as a wrapping notice; a
 * sentence is not a state, and as a nowrap pill it ran past the card edge.
 */
function WorkflowAvailabilityNotice({ exportPolicy, policy }: Readonly<{ exportPolicy: WorkflowExportPolicy | undefined; policy: WorkflowHeaderPolicy }>) {
  const { t } = useI18n();
  const canAdministerPlatform = useHasCapability("administer_platform");
  const model = missingExtensionNotice([exportPolicy], canAdministerPlatform);
  if (model) return <WorkflowMissingExtensionNotice model={model} />;
  if (!policy.protectionReason) return null;
  if (policy.unavailable) {
    return (
      <DsAlert title={t("react.workflows.workflow_unavailable", "Workflow unavailable")} tone="warning">
        <p className="break-words">{policy.protectionReason}</p>
      </DsAlert>
    );
  }
  return <p className="break-words text-sm fs-ink-secondary">{policy.protectionReason}</p>;
}

/** "Provided by extension: Hook Sample 1.0.1", linking to the extension's page. */
function WorkflowSourceExtensionBadge({ exportPolicy }: Readonly<{ exportPolicy: WorkflowExportPolicy | undefined }>) {
  const { t } = useI18n();
  const { extensionId, label } = workflowSourceExtension(exportPolicy);
  /* One inline wrapper inside the flex badge. Directly in the flex container
     the label and ": " formed one anonymous item and the link another, so the
     trailing space ended a line box and collapsed ("extension:Example").
     Inside one span the space sits between inline content and stays. */
  return (
    <DsBadge tone="orange">
      <span>
        {t("react.workflows.provided_by_extension", "Provided by extension")}
        {label ? ": " : null}
        {label && extensionId ? (
          <Link className="underline underline-offset-2" data-workflow-source-extension={extensionId} to={`/extensions/${encodeURIComponent(extensionId)}`}>
            {label}
          </Link>
        ) : label}
      </span>
    </DsBadge>
  );
}

function WorkflowStatusBadges({
  edgeCount,
  enabled,
  errorCount,
  exportPolicy,
  isValid,
  nodeCount,
  stepCount,
  policy,
  warningCount,
}: Readonly<{
  edgeCount?: number;
  enabled: boolean;
  errorCount: number;
  exportPolicy: WorkflowExportPolicy | undefined;
  isValid: boolean;
  nodeCount?: number;
  stepCount: number;
  policy: WorkflowHeaderPolicy;
  warningCount: number;
}>) {
  const { t } = useI18n();
  return (
    <>
      <DsBadge tone={enabled ? "green" : "neutral"}>{enabled ? t("settings.extensions.enabled", "enabled") : t("settings.notifications.status.disabled", "disabled")}</DsBadge>
      <DsBadge>{policy.effectiveStructureRedacted ? t("react.workflows.structure_read_only_by_extension", "Structure read-only (extension)") : t("react.workflows.value0_steps", "{value0} steps", { value0: stepCount })}</DsBadge>
      {policy.effectiveStructureRedacted || (nodeCount !== undefined && edgeCount !== undefined) ? <DsBadge>{policy.effectiveStructureRedacted ? t("react.workflows.protected_derived_workflow", "Protected derived workflow") : t("react.workflows.value0_nodes_value1_edges", "{value0} nodes / {value1} edges", { value0: nodeCount, value1: edgeCount })}</DsBadge> : null}
      <DsBadge tone={validationTone(policy.effectiveStructureRedacted, policy.effectiveScaffoldOnly, isValid)}>
        {t("react.workflows.validation", "Validation:")}{" "}{validationLabel(policy.effectiveStructureRedacted, policy.effectiveScaffoldOnly, isValid, errorCount, warningCount, t)}
      </DsBadge>
      {policy.extensionOwned ? <WorkflowSourceExtensionBadge exportPolicy={exportPolicy} /> : null}
    </>
  );
}

function WorkflowHeaderActions({
  builderPath,
  canAuthor,
  contextReady,
  enabled,
  menuItems,
  menuOpen,
  onToggleEnabled,
  policy,
  setMenuOpen,
  togglingEnabled,
}: Readonly<{
  builderPath: string;
  canAuthor: boolean;
  contextReady: boolean;
  enabled: boolean;
  menuItems: ReactNode[];
  menuOpen: boolean;
  onToggleEnabled?: () => void;
  policy: WorkflowHeaderPolicy;
  setMenuOpen: (open: boolean) => void;
  togglingEnabled: boolean;
}>) {
  const { t } = useI18n();
  const showToggle = canAuthor
    && policy.policyLoaded
    && Boolean(onToggleEnabled)
    && policy.lifecycleToggleAllowed;
  /* A protected extension workflow has no Open builder: its structure cannot
     change. Reading it is still the reason a client comes here — the builder
     shows it read-only and its hooks are connected there — so the same header
     slot, in the same style, opens it read-only instead of leaving only the
     quick link at the bottom of the overview. */
  const showView = canAuthor
    && contextReady
    && policy.policyLoaded
    && policy.extensionOwned
    && !policy.internalStructureAllowed;
  return (
    <div className="ml-auto flex flex-wrap items-center gap-2" id="workflow-header-actions">
      {showToggle ? (
        <button aria-label={enabled ? t("react.workflows.disable_workflow", "Disable workflow") : t("react.workflows.enable_workflow", "Enable workflow")} className="fs-button" data-workflow-status-toggle="true" disabled={togglingEnabled} onClick={onToggleEnabled} type="button">
          {enabled ? t("extensions.detail.disable", "Disable") : t("extensions.detail.enable", "Enable")}
        </button>
      ) : null}
      {policy.showInternalStructureActions && canAuthor && contextReady ? <Link className="fs-button" data-workflow-header-action="builder" to={builderPath}>{t("react.workflows.open_builder", "Open builder")}</Link> : null}
      {showView ? (
        <Link
          className="fs-button"
          data-workflow-header-action="view"
          title={t("react.workflows.view_workflow_read_only_hint", "Opens the builder read-only: the steps cannot change, the hooks can be connected.")}
          to={builderPath}
        >
          {t("react.workflows.view_workflow_read_only", "View workflow (read-only)")}
        </Link>
      ) : null}
      {menuItems.length ? <RowActionMenu label={t("react.workflows.workflow_actions", "Workflow actions")} onOpenChange={setMenuOpen} open={menuOpen}>{menuItems}</RowActionMenu> : null}
    </div>
  );
}

export function WorkflowSummaryHeader({
  activePage,
  activeTab = "",
  children,
  description,
  edgeCount,
  enabled,
  errorCount,
  extraTabs = [],
  isValid,
  nodeCount,
  portability,
  scaffoldOnly = false,
  canAuthor = true,
  onCloneWorkflow,
  onDeleteWorkflow,
  onToggleEnabled,
  onWorkflowTestRun,
  onWorkflowSettings,
  stepCount,
  projectId = "",
  summaryId = "workflow-route-summary",
  structureRedacted = false,
  title,
  togglingEnabled = false,
  warningCount,
  workflowId,
}: Readonly<PropsWithChildren<WorkflowSummaryHeaderProps>>) {
  const { t } = useI18n();
  const [menuOpen, setMenuOpen] = useState(false);
  const exportPolicyQuery = useQuery({
    queryKey: ["workflows", "export-policy", workflowId],
    queryFn: () => fetchWorkflowExportPolicy(workflowId, { presentation: "background" }),
    enabled: Boolean(workflowId) && !portability,
    retry: false,
  });
  const linkedProjectQuery = useQuery({
    queryKey: ["workflows", workflowId, "linked-project-id"],
    queryFn: () => findWorkflowLinkedProjectId(workflowId, { presentation: "background" }),
    // Only admins can read project workflow bindings; running this for non-admins
    // fans out one GET /api/projects/{id}/workflows -> 403 per project (audit noise),
    // and the resolved id only builds authoring links a non-admin can't use anyway.
    enabled: Boolean(workflowId) && !projectId && canAuthor,
    staleTime: 30_000,
    retry: false,
  });
  const exportPolicy = portability || exportPolicyQuery.data;
  const policy = workflowHeaderPolicy(exportPolicy, scaffoldOnly, structureRedacted);
  const metadataError = (!portability && exportPolicyQuery.error) || (!projectId && linkedProjectQuery.error);
  const contextReady = Boolean(projectId || linkedProjectQuery.isSuccess);
  const metadataPending = (!portability && exportPolicyQuery.isPending) || (canAuthor && !projectId && linkedProjectQuery.isPending);
  const effectiveProjectId = projectId || linkedProjectQuery.data || "";
  const overviewPath = workflowPathWithProject(workflowId, effectiveProjectId);
  const builderPath = workflowPathWithProject(workflowId, effectiveProjectId, "/builder");
  const projectsPath = workflowPathWithProject(workflowId, effectiveProjectId, "/projects");
  const exportPath = `/api/workflows/${encodeURIComponent(workflowId)}/export.yaml`;

  /* Canon's Button rule, verbatim: a button is a command on the current surface,
     and "if the user must choose between multiple related options, use a menu …
     instead of a pile of buttons". Only two of these are commands here — the
     enable toggle, and nothing else — so everything secondary moves into the
     published action menu, where a destructive command is danger text rather
     than a red surface. */
  const menuItems = workflowMenuItems({
    builderPath,
    settingsPath: overviewPath,
    canAuthor,
    cloneAllowed: policy.cloneAllowed && contextReady,
    contextReady,
    closeMenu: () => setMenuOpen(false),
    deleteAllowed: policy.deleteAllowed,
    exportPath,
    fullExportAllowed: policy.fullExportAllowed,
    onCloneWorkflow,
    onDeleteWorkflow,
    onWorkflowSettings,
    onWorkflowTestRun,
    projectId: effectiveProjectId,
    showInternalStructureActions: policy.showInternalStructureActions,
  }, t);

  /* Overview and Projects are sibling views of one workflow, which is canon's own
     rule for Tabs. Canon also requires the list to sit directly on the panel it
     switches, with no vertical gap and the panel carrying the 3px primary top
     strip — a strip floating above unrelated cards is not the component. */
  const tabStrip = (
    <nav aria-label={t("react.workflows.workflow_views", "Workflow views")} className="fs-tabs-scroll">
      <div className="fs-tabs-list">
        <Link
          aria-current={activePage === "overview" && !activeTab ? "page" : undefined}
          className="fs-tab"
          data-workflow-header-action="overview"
          to={overviewPath}
        >
          {t("project_edit.tab.overview", "Overview")}</Link>
        <Link
          aria-current={activePage === "projects" ? "page" : undefined}
          className="fs-tab"
          data-workflow-header-action="projects"
          to={projectsPath}
        >
          {t("shell.nav.projects", "Projects")}</Link>
        {extraTabs.map((tab) => (
          <Link
            aria-current={activePage === "overview" && activeTab === tab.key ? "page" : undefined}
            className="fs-tab"
            data-workflow-header-action={`tab-${tab.key}`}
            key={tab.key}
            to={workflowPathWithParams(workflowId, effectiveProjectId, { tab: tab.key })}
          >
            {tab.label}</Link>
        ))}
      </div>
    </nav>
  );

  const summary = (
    <article className="fs-card" id={summaryId}>
      <header className="fs-card-header">
        <p className="fs-page-eyebrow">{t("shell.nav.workflows", "Workflows")}</p>
        <h2 className="fs-card-title">{title}</h2>
        <p className="fs-card-description">{description}</p>
      </header>
      {/* The row is a child of the body, not the body itself: `.fs-card-body` is
          `display: grid` and it is declared after Tailwind's utilities, so a
          `flex` on the same element loses the cascade and every badge becomes a
          full-width grid row. */}
      <div className="fs-card-body">
        {/* A badge never runs past the card edge: at a narrow width one too
            long for the row wraps inside itself instead of overflowing. */}
        <div className="flex min-w-0 flex-wrap items-center gap-2 [&>.fs-badge]:max-w-full [&>.fs-badge]:whitespace-normal [&>.fs-badge]:[overflow-wrap:anywhere]">
          {/* The strip reports state and nothing else. The toggle is a command,
              so it sits with the actions — a control parked between two badges
              reads as one of them. Canon has no green metric state but it does
              publish a green badge tone, which is what a healthy state is. */}
          <WorkflowStatusBadges edgeCount={edgeCount} enabled={enabled} errorCount={errorCount} exportPolicy={exportPolicy} isValid={isValid} nodeCount={nodeCount} stepCount={stepCount} policy={policy} warningCount={warningCount} />
          <WorkflowHeaderActions builderPath={builderPath} canAuthor={canAuthor} contextReady={contextReady} enabled={enabled} menuItems={menuItems} menuOpen={menuOpen} onToggleEnabled={onToggleEnabled} policy={policy} setMenuOpen={setMenuOpen} togglingEnabled={togglingEnabled} />
        </div>
        <WorkflowAvailabilityNotice exportPolicy={exportPolicy} policy={policy} />
        {metadataPending ? <p className="text-sm fs-ink-secondary" role="status">{t("react.workflows.loading_workflow_data", "Loading workflow data...")}</p> : null}
        {metadataError ? (
          <DsAlert title={t("react.workflows.workflow_unavailable", "Workflow unavailable")} tone="danger">
            {metadataError instanceof Error
              ? metadataError.message
              : t("react.workflows.failed_to_load_workflow_summary", "Failed to load workflow summary.")}
            <button className="fs-button" disabled={exportPolicyQuery.isFetching || linkedProjectQuery.isFetching} onClick={() => {
              if (!portability && exportPolicyQuery.isError) ignoreAsyncResult(exportPolicyQuery.refetch());
              if (canAuthor && !projectId && linkedProjectQuery.isError) ignoreAsyncResult(linkedProjectQuery.refetch());
            }} type="button">{t("settings.watchdog.retry", "Retry")}</button>
          </DsAlert>
        ) : null}
      </div>
    </article>
  );

  /* Without children the caller has not moved onto the connected composition
     yet, so it keeps the summary alone rather than being handed a tab strip with
     nothing under it. The builder shell is the only such caller. */
  if (!children) {
    return summary;
  }

  return (
    <>
      {summary}
      {/* No gap inside this group: the strip and the panel are one surface. */}
      <section className="min-w-0">
        {tabStrip}
        <div className="fs-tabs-panel">
          <div className="fs-panel-stack">{children}</div>
        </div>
      </section>
    </>
  );
}
