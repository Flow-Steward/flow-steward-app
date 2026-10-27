import { useI18n } from "@/lib/i18n/useI18n";
import type { I18nContextValue } from "@/lib/i18n/context";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { AlertTriangle, GitBranch } from "lucide-react";
import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { DrawerGeometry } from "@/components/shared/design-system-overlays";
import { Link } from "react-router";

import { PageLoadingState } from "@/components/shared/page-loading-state";

import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { WorkflowCanvasPaletteSurface } from "@/pages/workflows/builder/canvas-palette-surface";
import type { CanvasRunBarProps } from "@/pages/workflows/builder/canvas-run-bar";
import type { ReviewCommentDraft } from "@/pages/jobs/detail/job-detail-review-panels";
import { parseHookNodeId } from "@/pages/workflows/builder/hook-frames";
import { WorkflowHookOfLinks } from "@/pages/workflows/detail/detail-hook-of-links";
import { DsAlert } from "@/components/shared/design-system";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { DrawerResizeHandle, WorkflowDrawerShell } from "@/pages/workflows/detail/detail-drawer-shell";
import { WorkflowStepLibrarySurface } from "@/pages/workflows/detail/detail-library-surface";
import { WorkflowDetailMainContent } from "@/pages/workflows/detail/detail-main-content";
import { GroupEditorSurface } from "@/pages/workflows/detail/detail-group-editor-surface";
import { GroupExtractionDialog } from "@/pages/workflows/detail/group-extraction-dialog";
import { WorkflowEditConflictDialog, WorkflowEditSessionNotice } from "@/pages/workflows/detail/workflow-edit-conflict-dialog";
import type { WorkflowEditSessionController } from "@/pages/workflows/detail/use-workflow-edit-session";
import { StepEditorSurface } from "@/pages/workflows/detail/detail-step-editor-surface";
import { WorkflowTriggersSurface } from "@/pages/workflows/detail/detail-triggers-surface";
import { WorkflowSettingsSmartSetup } from "@/pages/workflows/detail/detail-workflow-settings-smart-setup";
import { WorkflowHookProjectPicker, WorkflowScheduleProjectContext } from "@/pages/workflows/detail/detail-schedule-project-context";
import { WorkflowTestRunSurface } from "@/pages/workflows/detail/detail-workflow-test-run-surface";
import { WorkflowTestRunLaunchDialog, type WorkflowTestRunLaunchContext } from "@/pages/workflows/detail/detail-test-run-launch-dialog";
import { WorkflowHistorySurface } from "@/pages/workflows/detail/detail-workflow-history-surface";
import { AutomationRuleEditor } from "@/pages/workflows/detail/detail-helpers";
import { WorkflowSummaryHeader } from "@/pages/workflows/shared/header";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

function workflowDetailLoadError(t: I18nContextValue["t"], ...errors: unknown[]): string {
  const resolvedError = errors.find((error) => error instanceof Error);
  return resolvedError instanceof Error
    ? resolvedError.message
    : t("react.workflows.display_unknown_detail_error", "Unknown workflow detail error.");
}

function choose<T>(condition: boolean, yes: T, no: T): T {
  return condition ? yes : no;
}

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

type WorkflowDetailPageShellContext = WorkflowTestRunLaunchContext & Record<string, any> & {
  canvasRunBar?: CanvasRunBarProps;
};

/** One ephemeral draft survives a Results-only close, never a different run owner. */
function useOwnedApprovalCommentDraft(owner: string): ReviewCommentDraft | undefined {
  const [draft, setDraft] = useState({ owner, epoch: 1, identity: "", value: "" });
  if (draft.owner !== owner) setDraft({ owner, epoch: draft.epoch + 1, identity: "", value: "" });
  const committed = useRef({ owner, epoch: draft.epoch });
  useLayoutEffect(() => {
    committed.current = { owner, epoch: draft.epoch };
    return () => { committed.current = { owner: "", epoch: 0 }; };
  }, [owner, draft.epoch]);
  const epoch = draft.epoch;
  const onChange = useCallback((identity: string, value: string) => {
    if (!owner || !identity || committed.current.owner !== owner || committed.current.epoch !== epoch) return;
    setDraft((current) => {
      if (current.owner !== owner || current.epoch !== epoch || committed.current.owner !== owner || committed.current.epoch !== epoch) return current;
      return current.identity === identity && current.value === value ? current : { ...current, identity, value };
    });
  }, [owner, epoch]);
  return owner ? { identity: draft.identity, value: draft.value, onChange } : undefined;
}


/** The drawer's identity line: the ID of what it edits, with its container for a step inside one. */
function drawerSubtitleFor({ drawerMode, groupCreateMode, groupId, selectedStep, selectedStepAuthoredId, t, workflowId }: Readonly<{
  drawerMode: string;
  groupCreateMode: boolean;
  groupId: string;
  selectedStep: { forEachId?: string; groupId?: string } | null | undefined;
  selectedStepAuthoredId: string;
  t: (key: string, fallback: string) => string;
  workflowId: string;
}>): string {
  if (drawerMode === "step") {
    if (!selectedStep) return t("react.workflows.new_step", "New step");
    const containerId = selectedStep.forEachId || selectedStep.groupId || "";
    return containerId ? `${containerId} · ${selectedStepAuthoredId}` : selectedStepAuthoredId;
  }
  if (drawerMode === "group") return groupCreateMode ? t("react.workflows.new_group", "New group") : groupId;
  if (drawerMode === "workflow-settings" || drawerMode === "workflow-interface") return workflowId;
  return "";
}

export function WorkflowDetailPageShell({ ctx }: Readonly<{ ctx: WorkflowDetailPageShellContext }>) {
  const { t } = useI18n();
  const approvalCommentDraft = useOwnedApprovalCommentDraft(ctx.selectedTestRunControlled === true && typeof ctx.drawerReturnFocusOwner === "string"
    ? ctx.drawerReturnFocusOwner : "");
  // Workflow authoring is admin-only; hide authoring affordances on the read-only
  // detail surface (clone/delete/settings, canvas edit) for non-admins.
  const canAuthor = useIsAdmin();
  const authorStepPlayAllowed = ctx.authorStepPlayAllowed === true;
  const stepTestRunAllowed = useCallback((stepId: string) => canAuthor && (
    Boolean(parseHookNodeId(stepId)?.memberStepId) || authorStepPlayAllowed
  ), [canAuthor, authorStepPlayAllowed]);
  const drawerFocusFallbackRef = useRef<HTMLElement>(null);
  const {
    advancedMode, beginBuilderDrawerResize, builderCanvasFocusNodeId, builderCanvasFocusNonce, builderCanvasFocusPlace, builderCanvasFocusCreation,
    builderConfirmDialog, builderRuntimeActiveNodeId, builderDrawerResizable, builderDrawerStyle,
    builderDrawerViewportWidth, builderDrawerWidth, builderPathWithProject, builderSurface,
    canvasStructureLocked, canvasGraphQuery, changeSet, clampedBuilderDrawerWidth, closeDrawer,
    dagEdgeCount, dagNodeCount, dagQuery, definitionRows, detailQuery, displayCanvasGraph,
    drawerMode, drawerOpen, drawerTab, drawerTitle, embedded, errorCount, exportYamlHref,
    exportYamlLabel, handleCanvasValidationIssue, handleCloneWorkflow, handleDeleteSelectedStep,
    handleDeleteWorkflow, handleSaveStep, handleStepKindChange,
    extensionConnectionReadinessBlocked,
    connectorMissingRequiredInputNames, connectorRequiredInputsMissing, isValid,
    installedSourceStructureLocked,
    openGroupAutomationDrawer, openGroupDrawer, openNewGroupDrawer, openNewStepDrawer,
    openStepAutomationDrawer, openStepDrawer, openWorkflowAutomationDrawer,
    openWorkflowSettingsDrawer, changeDrawerTab, openWorkflowTestRunDrawer, protectedWorkflowStructureRedacted, publishedBlocks,
    renderProjectLinkControl, renderStepKindSmartSetup, renderStepStructureSetup,
    renderWorkflowVariableInput, saveNotice, saveNoticeIsError, savingWorkflowSurface,
    selectedCanvasIssue, selectedIssueSuggestedAction, selectedProjectId, selectedStep,
    selectedStepAuthoredId, selectedStepId, setAdvancedMode,
    setBuilderDrawerWidth, setDrawerTab, setStepBlockKey,
    setStepDisplayName, setStepOptional, setStepOverrideDraft, setStepTestRunInputs,
    setStepTestRunSummary, setWorkflowTestRunApprovalComment, setWorkflowTestRunInputs,
    setWorkflowTestRunSummary, stepAgentId, stepBlockKey, stepContractPreviewQuery,
    stepCreateContext, stepDisplayName, stepFormStatus, stepKindDraft, stepOptional,
    stepOverrideDraft, stepSkillOptions, stepStructurePending,
    stepTestRunBlockedReason, stepTestRunInputRows, stepTestRunInputs, stepTestRunMessage,
    stepTestRunMutation, stepTestRunPayload, stepTestRunSummary, surface,
    toggleBuilderAdvancedMode, toggleWorkflowEnabled, togglingWorkflowEnabled,
    validationErrorGroups, validationErrors, validationQuery, validationWarningGroups,
    validationWarnings, warningCount, workflow, workflowAvailabilityNotice, workflowDescription,
    workflowDetailPathWithProject, workflowDrawerNotice, workflowDrawerNoticeTitle, workflowId,
    workflowProtectionNotice, workflowRunsBlocked, workflowScaffoldOnly,
    workflowTestRunApprovalComment, workflowTestRunApprovalMutation,
    workflowTestRunBlockedReason, workflowTestRunInputRows, workflowTestRunInputs,
    workflowTestRunJobId, workflowTestRunMessage, workflowTestRunMutation,
    workflowTestRunPayload, workflowTestRunSummary, workflowTitle,
  } = ctx;

  const drawerGeometryOwner = JSON.stringify([workflowId, selectedProjectId, drawerOpen, drawerMode, drawerTab, ctx.selectedWorkflowTestRun?.generation]);
  const committedDrawerGeometryOwner = useRef<string | null>(null);
  const [measuredDrawer, setMeasuredDrawer] = useState<{ owner: string; geometry: DrawerGeometry } | null>(null);
  // Edit session (Canvas › Undo): a newer server version holding the local copy opens the comparison.
  const editSession = ctx.editSession as WorkflowEditSessionController | undefined;
  const [compareVersionsOpen, setCompareVersionsOpen] = useState(false);
  const localCopyHeld = Boolean(changeSet?.checkpoint?.held);
  const [shownHeld, setShownHeld] = useState(false);
  if (shownHeld !== localCopyHeld) {
    setShownHeld(localCopyHeld);
    if (localCopyHeld) setCompareVersionsOpen(true);
  }
  useLayoutEffect(() => {
    committedDrawerGeometryOwner.current = drawerGeometryOwner;
    return () => {
      if (committedDrawerGeometryOwner.current === drawerGeometryOwner) committedDrawerGeometryOwner.current = null;
    };
  }, [drawerGeometryOwner]);
  const onDrawerGeometryChange = useCallback((geometry: DrawerGeometry | null) => {
    if (committedDrawerGeometryOwner.current !== drawerGeometryOwner) return;
    setMeasuredDrawer((current) => {
      if (committedDrawerGeometryOwner.current !== drawerGeometryOwner) return current;
      if (!geometry) return current?.owner === drawerGeometryOwner ? null : current;
      if (current?.owner === drawerGeometryOwner && current.geometry.top === geometry.top &&
        current.geometry.right === geometry.right && current.geometry.width === geometry.width && current.geometry.height === geometry.height) return current;
      return { owner: drawerGeometryOwner, geometry };
    });
  }, [drawerGeometryOwner]);
  const drawerGeometry = drawerOpen && measuredDrawer?.owner === drawerGeometryOwner ? measuredDrawer.geometry : null;

  const resolvePlacementOwner = (nodeIds: readonly string[]) => {
    if (!canAuthor) return null;
    // Once Delete clears the selected cards, canvas-focused Undo still belongs
    // to the active author or Hook-child view, never a composite canvas draft.
    if (!nodeIds.length) {
      if (ctx.hookScopeActive && ctx.placementChildWorkflowId &&
        ctx.placementChildChangeSet?.workflowId === ctx.placementChildWorkflowId) return ctx.placementChildChangeSet;
      if (!canvasStructureLocked) return changeSet;
      return ctx.placementChildWorkflowId && ctx.placementChildChangeSet?.workflowId === ctx.placementChildWorkflowId
        ? ctx.placementChildChangeSet : null;
    }
    const members = nodeIds.map((id) => parseHookNodeId(id));
    // A visual Hook frame is a projection, never a serializable member move.
    if (members.some((member) => member && !member.memberStepId)) return null;
    const hookIds = new Set(members.filter((member) => member?.memberStepId).map((member) => member!.hookId));
    if (!hookIds.size) return canvasStructureLocked ? null : changeSet;
    if (hookIds.size !== 1 || members.some((member) => !member)) return null;
    const hookId = [...hookIds][0];
    return hookId === ctx.placementChildHookId &&
      ctx.placementChildWorkflowId &&
      ctx.placementChildChangeSet?.workflowId === ctx.placementChildWorkflowId
      ? ctx.placementChildChangeSet : null;
  };

  return (
    <section
      aria-label={workflowTitle}
      ref={drawerFocusFallbackRef}
      tabIndex={-1}
      className={choose(
        builderSurface,
        "h-full min-w-0 overflow-hidden bg-[var(--fs-bg-canvas)]",
        "min-w-0 space-y-6 overflow-x-hidden",
      )}
    >
      {renderWhen(!embedded && surface === "detail", (
        <WorkflowSummaryHeader
          activePage="detail"
          canAuthor={canAuthor}
          description={workflowDescription}
          edgeCount={dagEdgeCount}
          enabled={Boolean(workflow?.enabled)}
          errorCount={choose(protectedWorkflowStructureRedacted, 0, errorCount)}
          isValid={choose(protectedWorkflowStructureRedacted, true, isValid)}
          nodeCount={dagNodeCount}
          scaffoldOnly={choose(protectedWorkflowStructureRedacted, false, workflowScaffoldOnly)}
          onCloneWorkflow={() => ignoreAsyncResult(handleCloneWorkflow())}
          onDeleteWorkflow={() => ignoreAsyncResult(handleDeleteWorkflow())}
          onToggleEnabled={() => ignoreAsyncResult(toggleWorkflowEnabled())}
          onWorkflowTestRun={openWorkflowTestRunDrawer}
          onWorkflowSettings={() => ignoreAsyncResult(openWorkflowSettingsDrawer("smart-setup"))}
          stepCount={definitionRows.stepRows.length}
          projectId={selectedProjectId}
          structureRedacted={protectedWorkflowStructureRedacted}
          summaryId="workflow-detail-summary"
          title={workflowTitle}
          togglingEnabled={togglingWorkflowEnabled}
          warningCount={choose(protectedWorkflowStructureRedacted, 0, warningCount)}
          workflowId={workflowId}
        />
      ))}
      {renderWhen(Boolean(surface === "detail" && workflowId && canAuthor), (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-cyan-300/25 bg-[linear-gradient(135deg,rgba(8,145,178,0.18),rgba(15,23,42,0.62))] px-4 py-3 shadow-lg shadow-cyan-950/20">
          <div className="min-w-0">
            <p className="text-sm font-medium text-cyan-100">
              {t("react.workflows.canvas_builder_beta", "Canvas builder beta")}</p>
            <p className="mt-1 text-xs text-[var(--fs-text-secondary)]">
              {t("react.workflows.visual_step_branch_group_validation_and_automation_map_for_this_workflow", "Visual step, branch, group, validation, and automation map for this workflow.")}</p>
          </div>
          <Link
            className="inline-flex shrink-0 items-center gap-2 rounded-full border border-cyan-200/45 bg-cyan-300/15 px-3 py-1.5 text-sm font-medium text-cyan-50 transition hover:border-cyan-100/70 hover:bg-cyan-300/25"
            to={builderPathWithProject}
          >
            <GitBranch className="h-4 w-4" />
            {t("react.workflows.open_canvas_builder", "Open canvas builder")}</Link>
        </div>
      ))}
      {renderWhen(surface === "detail", (
        <div className="flex justify-end">
          <button
            aria-pressed={advancedMode}
            className={[
              "rounded-full border px-3 py-1 text-xs transition",
              choose(
                advancedMode,
                "border-cyan-300/45 bg-cyan-500/10 text-cyan-100",
                "border-white/10 text-slate-200 hover:border-cyan-300/40",
              ),
            ].map((value) => stringFromUnknownValue(value)).join(" ")}
            onClick={() => {
              const nextAdvancedMode = !advancedMode;
              setAdvancedMode(nextAdvancedMode);
              if (
                !nextAdvancedMode &&
                drawerTab === "advanced"
              ) {
                setDrawerTab("smart-setup");
              }
            }}
            type="button"
          >
            {choose(advancedMode, t("react.workflows.display_advanced_mode_on", "Advanced mode on"), t("react.workflows.display_advanced_mode", "Advanced mode"))}
          </button>
        </div>
      ))}

      <WorkflowTestRunLaunchDialog ctx={ctx} />
      <WorkflowDrawerShell
        allowStepTestRun={canAuthor && authorStepPlayAllowed}
        ctx={{
          advancedMode,
          beginBuilderDrawerResize,
          builderDrawerResizable,
          onDrawerGeometryChange: builderSurface ? onDrawerGeometryChange : undefined,
          builderDrawerStyle,
          builderDrawerViewportWidth,
          builderDrawerWidth,
          builderSurface,
          clampedBuilderDrawerWidth,
          drawerMode,
          drawerOpen,
          drawerTab,
          drawerTitle,
          drawerSubtitle: drawerSubtitleFor({
            drawerMode, groupCreateMode: Boolean(ctx.groupCreateMode), groupId: ctx.selectedGroup?.groupId || "",
            selectedStep, selectedStepAuthoredId, t, workflowId,
          }),
          drawerFocusContext: JSON.stringify([workflowId, drawerMode, selectedStepId, ctx.selectedGroupId, ctx.groupDraft?.containerKind, ctx.groupDraft?.groupId, ctx.selectedStepHookId]),
          // Closing clears drawerMode; scope/selection replacement still retires return focus.
          drawerReturnFocusOwner: JSON.stringify([ctx.drawerReturnFocusOwner, selectedStepId, ctx.selectedGroupId, ctx.groupDraft?.containerKind, ctx.groupDraft?.groupId, ctx.selectedStepHookId]),
          drawerFocusFallback: () => drawerFocusFallbackRef.current,
          // A step inside a hook is the project's own workflow: not locked (B14).
          structureLocked: canvasStructureLocked && !ctx.hookScopeActive,
          selectedStepHookId: ctx.selectedStepHookId,
          groupDraft: ctx.groupDraft,
          saveNotice,
          saveNoticeIsError,
          deletionGuard: ctx.deletionGuard,
          selectedCanvasIssue,
          selectedIssueSuggestedAction,
          setBuilderDrawerWidth,
          closeDrawer,
          changeDrawerTab,
          // Webhook and Schedules show only while their channel is on (R2-16): the builder's drawer needs the channels too.
          workflowInterfaceTriggerChannelsDraft: ctx.workflowInterfaceTriggerChannelsDraft,
          workflowInterfaceEnabledDraft: ctx.workflowInterfaceEnabledDraft,
        }}
      >
        <div
          className={choose(["workflow-settings", "workflow-interface", "workflow-test-run"].includes(drawerMode), "mt-3 block", "mt-3 hidden")}
          id="workflow-settings-surface"
        >
          <WorkflowSettingsSmartSetup ctx={{ ...ctx, structureLocked: canvasStructureLocked }} />
          {drawerMode === "workflow-settings" && drawerTab === "schedules" ? (
            <WorkflowScheduleProjectContext
              key={workflowId}
              workflowId={workflowId}
              projectHint={ctx.scheduleProjectHint}
              onProjectChange={ctx.onScheduleProjectChange}
              onTurnOnScheduleChannel={() => ignoreAsyncResult(Promise.resolve(changeDrawerTab("smart-setup")))}
            />
          ) : null}
          <WorkflowTriggersSurface ctx={ctx} />
          <WorkflowHistorySurface ctx={{ ...ctx, changeSet: ctx.canvasChangeSet || changeSet }} />
          {renderWhen(drawerMode === "workflow-settings" && drawerTab === "automation", (
            <div
              className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] p-3"
              id="workflow-automation-surface"
            >
              <p className="text-xs uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
                {t("react.workflows.workflow_automation_reactions", "Workflow automation & reactions")}</p>
              <p className="text-xs text-[var(--fs-text-secondary)]">
                {t("react.workflows.configure_workflow_level_reaction_rules_approval_responses_and_notification_triggers", "Configure workflow-level reaction rules, approval responses, and notification triggers.")}</p>
              <AutomationRuleEditor
                onDirtyChange={ctx.onAutomationDirtyChange}
                projectId={selectedProjectId}
                scopeKind="workflow"
                workflowId={workflowId}
              />
            </div>
          ))}
          <WorkflowTestRunSurface
            ctx={{
              approvalCommentDraft,
              drawerMode,
              drawerTab,
              openWorkflowTestRunDrawer,
              renderProjectLinkControl,
              selectedProjectId,
              setWorkflowTestRunApprovalComment,
              setWorkflowTestRunInputs,
              setWorkflowTestRunSummary,
              workflowDrawerNotice,
              workflowDrawerNoticeTitle,
              workflowId,
              onWorkflowRuntimeLocationChange: ctx.onSelectedWorkflowRuntimeLocationChange,
              onWorkflowExecutionSnapshotChange: ctx.onWorkflowExecutionSnapshotChange,
              selectedTestRunControlled: ctx.selectedTestRunControlled,
              stepInspectorHost: builderSurface ? drawerGeometry : undefined,
              stepInspectorChrome: builderSurface ? <div className="workflow-builder-drawer"><DrawerResizeHandle
                beginResize={beginBuilderDrawerResize}
                resizable={builderDrawerResizable}
                setWidth={setBuilderDrawerWidth}
                viewportWidth={builderDrawerViewportWidth}
                width={builderDrawerWidth}
              /></div> : undefined,
              selectedWorkflowTestRun: ctx.selectedWorkflowTestRun,
              testRunEvidence: ctx.testRunEvidence,
              workflowRunInspectionRequest: ctx.workflowRunInspectionRequest,
              selectedRunLoading: ctx.selectedRunLoading,
              selectedRunVerificationError: ctx.selectedRunVerificationError,
              workflowTestRunMutationIsCurrent: ctx.workflowTestRunMutationIsCurrent,
              workflowTestRunApprovalMutationIsCurrent: ctx.workflowTestRunApprovalMutationIsCurrent,
              workflowRunsBlocked,
              workflowTestRunApprovalComment,
              workflowTestRunApprovalMutation,
              workflowTestRunBlockedReason,
              workflowTestRunInputRows,
              workflowTestRunInputs,
              workflowTestRunJobId,
              workflowTestRunMessage,
              workflowTestRunMutation,
              workflowTestRunPayload,
              workflowTestRunSummary,
            }}
          />
          {renderWhen(drawerTab === "advanced", (
            <div className="mt-3 space-y-2">
              <button
                className="w-full rounded-xl border border-[var(--fs-border-default)] px-3 py-2 text-left text-sm text-[var(--fs-text-primary)]"
                type="button"
              >
                {t("react.workflows.refresh_workflow_contracts", "Refresh workflow contracts")}</button>
              <button
                className="w-full rounded-xl border border-[var(--fs-border-default)] px-3 py-2 text-left text-sm text-[var(--fs-text-primary)]"
                onClick={() => openWorkflowTestRunDrawer()}
                type="button"
              >
                {t("react.workflows.open_test_run", "Open Test Run")}</button>
            </div>
          ))}
        </div>
        <WorkflowStepLibrarySurface
          ctx={{
            drawerMode,
            openNewStepDrawer,
            publishedBlocks,
            selectedGroupId: ctx.selectedGroupId,
            setStepBlockKey,
          }}
        />
        <StepEditorSurface
          ctx={{
            agentDrafts: ctx.agentDrafts, agentWorkflowRecord: ctx.agentWorkflowRecord,
            agentCatalogQuery: ctx.agentCatalogQuery, agentBlockCatalogQuery: ctx.agentBlockCatalogQuery, agentBlockOptions: ctx.agentBlockOptions,
            stepAgentOptions: ctx.stepAgentOptions, stepAgentLocked: ctx.stepAgentLocked, setStepAgentId: ctx.setStepAgentId,
            httpIntent: ctx.httpIntent,
            httpFieldFeedback: ctx.httpFieldFeedback,
            selectedConnectorDisplayName: ctx.selectedConnectorDisplayName,
            advancedMode,
            callableWorkflows: ctx.callableWorkflows,
            drawerMode,
            drawerTab,
            handleDeleteSelectedStep,
            handleSaveStep,
            handleStepKindChange,
            hookTestRunBlockedReason: ctx.hookTestRunBlockedReason,
            onHookTestRunStarted: ctx.onHookTestRunStarted,
            hookFrames: ctx.hookFrames,
            clearHook: ctx.clearHook,
            requestConfirmation: ctx.requestConfirmation,
            onProjectChange: ctx.onScheduleProjectChange,
            extensionConnectionReadinessBlocked,
            connectorMissingRequiredInputNames,
            connectorRequiredInputsMissing,
            publishedBlocks,
            renderProjectLinkControl,
            renderStepKindSmartSetup,
            renderStepStructureSetup,
            savingWorkflowSurface,
            selectedProjectId,
            selectedStep,
            selectedStepAuthoredId,
            selectedStepId,
            setStepBlockKey,
            setStepDisplayName,
            setStepOptional,
            setStepOverrideDraft,
            setStepTestRunInputs,
            setStepTestRunSummary,
            stepAgentId: stepKindDraft === "agent" && ctx.agentDrafts?.mode === "participant" ? ctx.agentPreviewId : stepAgentId,
            stepBlockKey,
            stepContractPreviewQuery,
            stepDisplayName,
            stepKindDraft,
            stepKindSelection: ctx.stepKindSelection,
            stepOptional,
            stepCreateContext,
            stepFormStatus,
            stepFormDirty: ctx.stepFormDirty,
            stepFormUnsaved: ctx.stepFormUnsaved,
            stepFormSaveState: ctx.stepFormSaveState,
            stepSaveSerial: ctx.stepSaveSerial,
            pendingKindChange: ctx.pendingKindChange,
            cancelNewDrawerItem: ctx.cancelNewDrawerItem,
            onAutomationDirtyChange: ctx.onAutomationDirtyChange,
            stepOverrideDraft,
            stepSkillOptions,
            stepStructurePending,
            stepTestRunBlockedReason,
            stepTestRunInputRows,
            stepTestRunInputs,
            stepTestRunMessage,
            stepTestRunMutation,
            stepTestRunPayload,
            stepTestRunSummary,
            testRunEvidence: ctx.hookScopeActive ? null : ctx.testRunEvidence,
            approvalCommentDraft,
            structureLocked: canvasStructureLocked && !ctx.hookScopeActive,
            workflowDrawerNotice,
            workflowDrawerNoticeTitle,
            workflowId: ctx.stepWorkflowId || workflowId,
            ownerWorkflowId: workflowId,
            workflowRunsBlocked,
          }}
        />
        <GroupEditorSurface
          ctx={{
            drawerMode,
            drawerTab,
            groupCreateMode: ctx.groupCreateMode,
            groupDraft: ctx.groupDraft,
            groupFormDirty: ctx.groupFormDirty,
            groupFormSaveFailed: ctx.groupFormSaveFailed,
            groupSaveSerial: ctx.groupSaveSerial,
            cancelNewDrawerItem: ctx.cancelNewDrawerItem,
            onAutomationDirtyChange: ctx.onAutomationDirtyChange,
            canConvertGroup: canAuthor,
            groupExtractionBlockedReason: ctx.groupExtractionBlockedReason,
            onConvertGroup: () => ctx.setExtractionGroup({ id: ctx.selectedGroup.groupId, name: ctx.groupDraft.displayName || ctx.selectedGroup.groupId }),
            handleDeleteGroup: ctx.handleDeleteGroup,
            connectPort: ctx.connectPort,
            disconnectPort: ctx.disconnectPort,
            handleMoveForEach: ctx.handleMoveForEach,
            handleSaveGroup: ctx.handleSaveGroup,
            openNewStepDrawer,
            openStepDrawer,
            preview: changeSet.preview,
            renderWorkflowVariableInput,
            savingWorkflowSurface,
            selectedGroup: ctx.selectedGroup,
            selectedProjectId,
            setEntry: ctx.setEntry,
            setGroupDraft: ctx.setGroupDraft,
            structureLocked: canvasStructureLocked && !ctx.hookScopeActive,
            workflowId,
            workflowRootEntryId: ctx.workflowRootEntryId,
            ...ctx.groupEditorContext,
          }}
        />
      </WorkflowDrawerShell>

      {renderWhen(Boolean(detailQuery.isLoading ||
      validationQuery.isLoading ||
      dagQuery.isLoading ||
      (surface === "builder" && canvasGraphQuery.isLoading)), (
        <PageLoadingState label={t("react.workflows.preparing_workflow", "Preparing the workflow")} />
      ))}

      {renderWhen(surface === "builder" && installedSourceStructureLocked, (
        <div className="sr-only" id={`workflow-protection-${workflowId}`} role="note" aria-label={protectedWorkflowStructureRedacted
          ? t("react.workflows.protected_extension_workflow", "Protected extension workflow")
          : t("react.workflows.structure_read_only_by_extension", "Structure read-only (extension)")}>
          <strong>{protectedWorkflowStructureRedacted
            ? t("react.workflows.protected_extension_workflow", "Protected extension workflow")
            : t("react.workflows.structure_read_only_by_extension", "Structure read-only (extension)")}</strong>
          <p>{workflowProtectionNotice || (authorStepPlayAllowed
            ? t("react.workflows.this_workflow_is_provided_by_an_extension_and_cannot_be_edited_here", "This workflow is provided by an extension and cannot be edited here.")
            : t("react.workflows.protected_workflow_read_only", "This workflow is provided by an extension and protected. You can read its structure and connect your own workflows to its hooks; its steps cannot be edited or run one by one."))}</p>
        </div>
      ))}

      {renderWhen(Boolean(!protectedWorkflowStructureRedacted &&
      (detailQuery.isError ||
        validationQuery.isError ||
        dagQuery.isError ||
        (surface === "builder" && canvasGraphQuery.isError))), (
        <Card className="border-rose-300/30 bg-rose-500/10">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-xl">
              <AlertTriangle className="h-5 w-5" />
              {t("react.workflows.workflow_detail_load_failed", "Workflow detail load failed")}</CardTitle>
            <CardDescription>
              {workflowDetailLoadError(
                t,
                detailQuery.error,
                validationQuery.error,
                dagQuery.error,
                canvasGraphQuery.error,
              )}
            </CardDescription>
          </CardHeader>
        </Card>
      ))}

      {/* No page-level "Workflow unavailable" card: the builder says it once,
          under its toolbar, and the detail summary says it in its card. */}
      {ctx.selectedRunVerificationError ? <DsAlert title={ctx.selectedRunVerificationError} tone="danger" /> : null}
      <ConfirmDialog {...builderConfirmDialog} />
      {ctx.hookExecutionObservers}
      {canAuthor && ctx.extractionGroup ? (
        <GroupExtractionDialog
          key={`${workflowId}:${ctx.extractionGroup.id}`}
          workflowId={workflowId}
          groupId={ctx.extractionGroup.id}
          groupName={ctx.extractionGroup.name}
          revision={ctx.serverRevision}
          projectId={selectedProjectId}
          blockedReason={ctx.groupExtractionBlockedReason}
          onClose={() => ctx.setExtractionGroup(null)}
          onReload={() => ctx.refetchWorkflowSurfaces()}
          onApplied={async () => {
            // This page wrote the conversion: its revision is not someone else's change.
            changeSet.expectOwnRevision?.();
            await closeDrawer();
            await ctx.refetchWorkflowSurfaces();
          }}
          stepName={(stepId) => displayCanvasGraph?.steps.find((step: { id?: string; step_id?: string; display_name?: string }) => step.id === stepId || step.step_id === stepId)?.display_name || stepId}
          onOpenStep={(stepId) => {
            ctx.setExtractionGroup(null);
            if (ctx.canvasNavigation) ctx.canvasNavigation.openStep(stepId, "");
            else openStepDrawer(stepId, "", false);
          }}
        />
      ) : null}
      {canAuthor && editSession ? (
        <>
          <WorkflowEditSessionNotice session={editSession} />
          <WorkflowEditConflictDialog
            changeSet={changeSet}
            onClose={() => setCompareVersionsOpen(false)}
            open={compareVersionsOpen && changeSet.status === "conflict"}
            refetchWorkflowSurfaces={() => ctx.refetchWorkflowSurfaces()}
            session={editSession}
          />
        </>
      ) : null}
      {renderWhen(surface === "builder", (
        <WorkflowHookOfLinks projectId={selectedProjectId || ""} workflowId={workflowId} />
      ))}
      {renderWhen(Boolean(surface === "builder" && displayCanvasGraph), (
        <WorkflowCanvasPaletteSurface
          structureLocked={canvasStructureLocked}
          hookFrames={ctx.hookFrames}
          advancedMode={advancedMode}
          availabilityNotice={workflowAvailabilityNotice}
          missingExtensions={ctx.workflowMissingExtensions}
          headerContext={canAuthor && ctx.hookFrames instanceof Map && ctx.hookFrames.size
            ? <WorkflowHookProjectPicker compact workflowId={workflowId} projectId={selectedProjectId || ""} onProjectChange={ctx.onScheduleProjectChange} />
            : null}
          backHref={workflowDetailPathWithProject}
          canvasGraph={displayCanvasGraph}
          changeSet={ctx.canvasChangeSet || changeSet}
          resolvePlacementOwner={resolvePlacementOwner}
          placementHistoryOwners={[
            changeSet,
            ...(ctx.placementChildWorkflowId && ctx.placementChildChangeSet?.workflowId === ctx.placementChildWorkflowId
              ? [ctx.placementChildChangeSet] : []),
          ]}
          projectId={selectedProjectId}
          commandViewIdentity={ctx.canvasCommandViewIdentity}
          onCreateStep={ctx.canvasNavigation?.createStep}
          allowHookDeclaration={ctx.allowHookDeclaration}
          exportYamlHref={exportYamlHref}
          exportYamlLabel={exportYamlLabel}
          focusNodeId={builderCanvasFocusNodeId}
          focusNodeNonce={builderCanvasFocusNonce}
          focusNodeCreation={builderCanvasFocusCreation}
          focusNodePlace={builderCanvasFocusPlace}
          runtimeActiveNodeId={builderRuntimeActiveNodeId}
          runtimeMarks={ctx.runtimeMarks}
          runtimeEdges={ctx.runtimeEdges}
          containerRuntimeMarks={ctx.containerRuntimeMarks}
          canvasRunBar={ctx.canvasRunBar}
          canvasTestRunAction={ctx.canvasTestRunAction}
          inspectorOpen={drawerOpen}
          inspectorWidth={drawerGeometry ? Math.max(0, drawerGeometry.right + drawerGeometry.width) : drawerOpen && builderDrawerResizable ? clampedBuilderDrawerWidth : 0}
          onCloseInspector={closeDrawer}
          onConnectPort={ctx.connectPort}
          onDisconnectPort={ctx.disconnectPort}
          onInsertStep={ctx.insertStepOnEdge}
          onAppendStep={ctx.appendStepAfterOutcome}
          onOpenGroup={ctx.canvasNavigation?.openGroup || openGroupDrawer}
          onOpenGroupAutomation={openGroupAutomationDrawer}
          onOpenNewGroup={ctx.canvasNavigation?.openNewGroup || openNewGroupDrawer}
          onOpenNewStep={ctx.canvasNavigation?.openNewStep || openNewStepDrawer}
          stepTestRunAllowed={stepTestRunAllowed}
          onOpenStep={(stepId, requestedTab) => (ctx.canvasNavigation ? ctx.canvasNavigation.openStep(stepId, "", requestedTab) : openStepDrawer(stepId, "", false, requestedTab))}
          onRequestGraphDelete={ctx.requestGraphDeleteFromCanvas}
          onReadClipboardAuthority={canAuthor ? ctx.readClipboardAuthority : undefined}
          clipboardGuard={ctx.deletionGuard}
          onDeleteSelectionInvalidated={ctx.deletionGuard.cancelCommand}
          deleteGuardNotice={drawerOpen ? null : ctx.deletionGuard.notice}
          onRetryGraphDelete={ctx.deletionGuard.retry}
          onOpenDeleteAutomation={ctx.deletionGuard.openAutomation}
          onOpenStepAutomation={openStepAutomationDrawer}
          onOpenWorkflowAutomation={openWorkflowAutomationDrawer}
          onOpenWorkflowSettings={() => ignoreAsyncResult(openWorkflowSettingsDrawer("smart-setup"))}
          onOpenWorkflowInterface={(tab) => ignoreAsyncResult(ctx.openWorkflowInterfaceDrawer(tab))}
          onOpenWorkflowHistory={() => ignoreAsyncResult(openWorkflowSettingsDrawer("history"))}
          onCompareVersions={canAuthor && editSession ? () => setCompareVersionsOpen(true) : undefined}
          onOpenStepPlacement={ctx.openStepPlacement ? (stepId: string) => ignoreAsyncResult(ctx.openStepPlacement(stepId)) : undefined}
          onConvertGroup={canAuthor && ctx.groupEditorContext?.canConvertGroup !== false ? (groupId: string, name: string) => ctx.setExtractionGroup({ id: groupId, name }) : undefined}
          convertGroupBlockedReason={ctx.groupExtractionBlockedReason}
          onToggleAdvancedMode={toggleBuilderAdvancedMode}
          onValidationIssue={handleCanvasValidationIssue}
          workflowId={workflowId}
          scheduleProjectHint={ctx.scheduleProjectHint}
          workflow={workflow}
          onAnnotationsSaved={() => ctx.refetchWorkflowSurfaces()}
          onAnnotationsReloaded={async (revision) => {
            if (changeSet.hasChanges && revision !== workflow?.revision) changeSet.reportConflict();
            await ctx.refetchWorkflowSurfaces();
          }}
        />
      ))}

      {renderWhen(surface === "detail", (
        <WorkflowDetailMainContent
          ctx={{
            changeSet,
            definitionRows,
            entryKey: ctx.entryKey,
            isValid,
            openGroupDrawer,
            openNewGroupDrawer,
            openNewStepDrawer,
            openStepDrawer,
            openValidationIssueGroup: ctx.openValidationIssueGroup,
            openValidationIssueSourceStep: ctx.openValidationIssueSourceStep,
            openValidationIssueStep: ctx.openValidationIssueStep,
            selectedStep,
            validationErrorGroups,
            validationErrors,
            validationWarningGroups,
            validationWarnings,
          }}
        />
      ))}
    </section>
  );
}
