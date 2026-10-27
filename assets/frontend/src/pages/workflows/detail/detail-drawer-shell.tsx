import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useId, useState, type ReactNode } from "react";
import { DsAlert, DsDrawer, DsTabs } from "@/components/shared/design-system";

import {
  clampBuilderDrawerWidth,
  persistBuilderDrawerWidth,
} from "@/pages/workflows/detail/detail-helpers";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { useChannelTabFallback, workflowChannelTabHidden } from "@/pages/workflows/shared/workflow-page-tabs";
import { DrawerFooterSlotProvider } from "./detail-drawer-footer";

import "./detail-builder-inspectors.css";

type DrawerTabOption = { key: string; label: string };

function localizedSTANDARD_DRAWER_TABS(t: I18nContextValue["t"] = fallbackContext.t): DrawerTabOption[] {
  return [
  { key: "smart-setup", label: t("react.workflows.smart_setup", "Smart setup") },
  { key: "test-run", label: t("registry.workflow_detail.run_lab.tab.test_run", "Test Run") },
  { key: "triggers", label: t("react.workflows.webhook_tab", "Webhook") },
  { key: "schedules", label: t("react.workflows.schedules", "Schedules") },
  { key: "settings", label: t("react.workflows.group_settings_tab", "Settings") },
  { key: "automation", label: t("project_detail.tab.automation", "Automation") },
  { key: "history", label: t("react.workflows.history_tab", "History") },
];
}

function localizedADVANCED_DRAWER_TABS(t: I18nContextValue["t"] = fallbackContext.t): DrawerTabOption[] {
  return [
  { key: "advanced", label: t("registry.workflow_detail.drawer.advanced", "Advanced") },
];
}

const DRAWER_MODE_TAB_KEYS: Record<string, Set<string>> = {
  "workflow-settings": new Set(["smart-setup", "triggers", "schedules", "automation", "history", "advanced"]),
  "workflow-test-run": new Set(),
  library: new Set(["smart-setup"]),
  group: new Set(["settings", "automation"]),
  step: new Set(["smart-setup", "test-run", "automation", "advanced"]),
};

/** Locked author structure keeps authoring tabs closed; isolated testing needs explicit permission. */
const LOCKED_DRAWER_TAB_KEYS: Record<string, Set<string>> = {
  group: new Set(["settings"]),
  step: new Set(["smart-setup"]),
};

const LOCKED_TESTABLE_STEP_TAB_KEYS = new Set(["smart-setup", "test-run"]);

/** A hook step's drawer: the step drawer's tabs, its Test Run running the hook's content (B14). */
const HOOK_STEP_TAB_KEYS = new Set(["smart-setup", "test-run", "automation"]);

function drawerTabs(drawerMode: string, advancedMode: boolean, t: I18nContextValue["t"] = fallbackContext.t, structureLocked = false, forEachContainer = false, hookStep = false, allowStepTestRun = false): DrawerTabOption[] {
  if (drawerMode === "workflow-interface") return [
    { key: "inputs", label: t("react.workflows.interface_inputs", "Inputs") },
    { key: "outputs", label: t("react.workflows.interface_outputs", "Outputs") },
  ];
  const tabs = advancedMode
    ? [...localizedSTANDARD_DRAWER_TABS(t), ...localizedADVANCED_DRAWER_TABS(t)]
    : localizedSTANDARD_DRAWER_TABS(t);
  const allowed = (drawerMode === "step" && hookStep ? HOOK_STEP_TAB_KEYS : undefined)
    || (structureLocked ? drawerMode === "step" && allowStepTestRun ? LOCKED_TESTABLE_STEP_TAB_KEYS : LOCKED_DRAWER_TAB_KEYS[drawerMode] : undefined)
    || DRAWER_MODE_TAB_KEYS[drawerMode]
    || DRAWER_MODE_TAB_KEYS.library;
  // Reactions exist for workflows, Groups and steps; a For each has none.
  return tabs.filter((tab) => allowed.has(tab.key) && !(forEachContainer && tab.key === "automation"));
}

export function DrawerResizeHandle({
  beginResize,
  resizable,
  setWidth,
  viewportWidth,
  width,
}: Readonly<{
  beginResize: (initialClientX: number, pointerId: number, target: HTMLElement) => void;
  resizable: boolean;
  setWidth: (value: number) => void;
  viewportWidth: number;
  width: number;
}>) {
  const { t } = useI18n();
  if (!resizable) return null;
  return (
    <button
      aria-label={t("react.workflows.resize_builder_inspector", "Resize builder inspector")}
      className="workflow-builder-drawer-resize-handle"
      onKeyDown={(event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const delta = event.key === "ArrowLeft" ? 24 : -24;
        const nextWidth = clampBuilderDrawerWidth(width + delta, viewportWidth);
        setWidth(nextWidth);
        persistBuilderDrawerWidth(nextWidth);
      }}
      onPointerDown={(event) => {
        event.preventDefault();
        beginResize(event.clientX, event.pointerId, event.currentTarget);
      }}
      tabIndex={0}
      title={t("react.workflows.drag_to_resize_inspector", "Drag to resize inspector")}
      type="button"
    />
  );
}

function CanvasIssueNotice({ issue, suggestedAction }: Readonly<{ issue: any; suggestedAction: string }>) {
  const { t } = useI18n();
  if (!issue) return null;
  const message = issue.message && issue.message !== issue.title ? issue.message : "";
  const rawTitle = issue.title || issue.message || t("react.workflows.issue_needs_attention", "Issue needs attention");
  const titleKey = issue.title ? issue.title_key : issue.message_key;
  const titleParams = issue.title ? issue.title_params : issue.message_params;
  return (
    <DsAlert title={t("react.workflows.validation_issue", "Validation issue")} tone={issue.severity === "error" ? "danger" : "warning"}>
      <p className="workflow-builder-issue-title">{titleKey ? t(titleKey, rawTitle, titleParams) : rawTitle}</p>
      {message ? <p>{issue.message_key ? t(issue.message_key, message, issue.message_params) : message}</p> : null}
      {suggestedAction ? <p className="workflow-builder-issue-action">{suggestedAction}</p> : null}
      {issue.field_path ? <p className="workflow-builder-issue-path">{issue.field_path}</p> : null}
    </DsAlert>
  );
}

export function WorkflowDrawerShell({
  children,
  ctx,
  allowStepTestRun = false,
}: Readonly<{
  children: ReactNode;
  ctx: Record<string, any>;
  allowStepTestRun?: boolean;
}>) {
  const { t } = useI18n();
  const instanceId = useId();
  const advancedMode = Boolean(ctx.advancedMode);
  const beginBuilderDrawerResize = ctx.beginBuilderDrawerResize as (
    initialClientX: number,
    pointerId: number,
    target: HTMLElement,
  ) => void;
  const builderDrawerResizable = Boolean(ctx.builderDrawerResizable);
  const builderDrawerStyle = ctx.builderDrawerStyle;
  const builderDrawerViewportWidth = Number(ctx.builderDrawerViewportWidth || 0);
  const builderDrawerWidth = Number(ctx.builderDrawerWidth || 0);
  const builderSurface = Boolean(ctx.builderSurface);
  const drawerMode = firstStringFromUnknownValue([ctx.drawerMode, ""]);
  const drawerOpen = Boolean(ctx.drawerOpen);
  const drawerTab = firstStringFromUnknownValue([ctx.drawerTab, ""]);
  const structureLocked = Boolean(ctx.structureLocked);
  const forEachContainer = drawerMode === "group" && ctx.groupDraft?.containerKind === "for_each";
  const drawerTitle = firstStringFromUnknownValue([ctx.drawerTitle, ""]);
  const drawerSubtitle = firstStringFromUnknownValue([ctx.drawerSubtitle, ""]);
  const [footerSlot, setFooterSlot] = useState<HTMLDivElement | null>(null);
  const saveNotice = firstStringFromUnknownValue([ctx.saveNotice, ""]);
  const selectedCanvasIssue = ctx.selectedCanvasIssue;
  const selectedIssueSuggestedAction = firstStringFromUnknownValue([ctx.selectedIssueSuggestedAction, ""]);
  const setBuilderDrawerWidth = ctx.setBuilderDrawerWidth as (
    value: number,
  ) => void;
  const closeDrawer = ctx.closeDrawer as () => void;
  const changeDrawerTab = ctx.changeDrawerTab as (value: string) => void;

  const tabs = drawerTabs(drawerMode, advancedMode, t, structureLocked, forEachContainer, Boolean(ctx.selectedStepHookId), allowStepTestRun)
    .filter((tab) => drawerMode !== "workflow-settings" || !workflowChannelTabHidden(tab.key, ctx));
  // Turning a channel off while its tab is open goes back to Smart setup.
  const openTabHidden = drawerOpen && drawerMode === "workflow-settings" && workflowChannelTabHidden(drawerTab, ctx);
  useChannelTabFallback({ active: drawerOpen && drawerMode === "workflow-settings", hidden: openTabHidden, onFallback: () => changeDrawerTab("smart-setup"), tab: drawerTab });
  const interfaceMode = drawerMode === "workflow-interface";
  const idPrefix = interfaceMode ? "workflow-interface" : `builder-inspector-${instanceId}`;
  const bodyId = `${idPrefix}-panel`;
  const activeTab = tabs.find((tab) => tab.key === drawerTab);

  return (
    <>
      {drawerOpen && !builderSurface ? <button
        aria-label={t("registry.workflow_detail.builder.close_drawer", "Close builder drawer")}
        className="workflow-builder-inspector-backdrop"
        id="builder-drawer-overlay"
        onClick={closeDrawer}
        tabIndex={-1}
        type="button"
      /> : null}
      <div className="hidden" id="modal-add-step" />
      <DsDrawer
        className={`workflow-builder-drawer ${builderSurface ? "workflow-builder-inspector-contextual" : "workflow-builder-inspector-detail"}${interfaceMode ? " workflow-builder-interface" : ""}`}
        closeLabel={t("registry.common.close", "Close")}
        dataAttributes={{ "data-resizable": builderDrawerResizable ? "true" : "false", "data-width": builderSurface ? String(ctx.clampedBuilderDrawerWidth) : undefined }}
        focusContext={ctx.drawerFocusContext || drawerMode}
        returnFocusOwner={ctx.drawerReturnFocusOwner}
        focusFallback={ctx.drawerFocusFallback}
        onClose={closeDrawer}
        onGeometryChange={ctx.onDrawerGeometryChange}
        open={drawerOpen}
        style={builderDrawerStyle}
        subtitle={drawerSubtitle}
        title={drawerTitle}
        titleId="builder-drawer-title"
      >
        <DrawerResizeHandle
          beginResize={beginBuilderDrawerResize}
          resizable={builderDrawerResizable}
          setWidth={setBuilderDrawerWidth}
          viewportWidth={builderDrawerViewportWidth}
          width={builderDrawerWidth}
        />
        {tabs.length ? <DsTabs
          active={drawerTab}
          ariaLabel={drawerTitle}
          idPrefix={idPrefix}
          items={tabs}
          onSelect={changeDrawerTab}
          revealActiveTab
          panelIdFor={interfaceMode ? (key) => `workflow-contract-section-${key}` : () => bodyId}
        /> : null}
        <div
          aria-labelledby={!interfaceMode && activeTab ? `${idPrefix}-tab-${activeTab.key}` : undefined}
          className={`workflow-builder-drawer-body overflow-y-auto${tabs.length ? " fs-tabs-panel" : ""}`}
          data-testid="builder-drawer-body"
          id={!interfaceMode ? bodyId : undefined}
          role={!interfaceMode && activeTab ? "tabpanel" : undefined}
        >
          {/* A save that landed says so inside its button; only a failure stays on screen. */}
          {saveNotice && ctx.saveNoticeIsError ? <DsAlert title={t("react.workflows.not_saved", "Not saved")} tone="danger">
            <p>{saveNotice}</p>
          </DsAlert> : null}
          {ctx.deletionGuard?.notice ? <DsAlert
            title={ctx.deletionGuard.notice.message}
            tone={ctx.deletionGuard.notice.state === "checking" ? "neutral" : "warning"}
          >
            {ctx.deletionGuard.notice.state === "unknown" ? <button
              className="fs-button"
              onClick={ctx.deletionGuard.retry}
              type="button"
            >{t("job_security.retry", "Retry")}</button> : null}
            {ctx.deletionGuard.notice.canOpenAutomation ? <button
              className="fs-button"
              onClick={ctx.deletionGuard.openAutomation}
              type="button"
            >{t("react.workflows.delete_guard_open_automation", "Open Automation")}</button> : null}
          </DsAlert> : null}
          <CanvasIssueNotice issue={selectedCanvasIssue} suggestedAction={selectedIssueSuggestedAction} />
          {interfaceMode ? tabs.filter((tab) => tab.key !== drawerTab).map((tab) => <div
            aria-labelledby={`workflow-interface-tab-${tab.key}`}
            hidden
            id={`workflow-contract-section-${tab.key}`}
            key={tab.key}
            role="tabpanel"
          />) : null}
          <DrawerFooterSlotProvider value={footerSlot}>{children}</DrawerFooterSlotProvider>
        </div>
        <div className="workflow-builder-drawer-footer" data-testid="builder-drawer-footer" ref={setFooterSlot} />
      </DsDrawer>
    </>
  );
}
