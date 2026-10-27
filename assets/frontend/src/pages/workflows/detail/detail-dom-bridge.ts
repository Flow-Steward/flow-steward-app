export { workflowIOIssueMessage } from "@/pages/workflows/detail/detail-webhook";
/* eslint-disable @typescript-eslint/no-explicit-any, react-hooks/exhaustive-deps */
import { useEffect, useRef } from "react";

import { useWorkflowSettingsHydration } from "./detail-workflow-settings-controller";
import {
  editableOverrideForStep,
  jsonText,
} from "@/pages/workflows/detail/detail-helpers";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

/**
 * Keeps the drawer drafts in step with the loaded workflow, and exposes the
 * few window hooks browser automation drives the builder with.
 */
export function useWorkflowDetailDomBridge(ctx: Record<string, any>) {
  const {
    advancedMode,
    openWorkflowSettingsDrawer,
    closeDrawer,
    changeDrawerTab,
    automationScopeKind,
    drawerMode,
    drawerTab,
    handleCloneWorkflow,
    handleDeleteWorkflow,
    openGroupDrawer,
    openNewStepDrawer,
    openStepDrawer,
    publishedBlocks,
    selectedGroupId,
    selectedStep,
    setAutomationScopeKind,
    setDrawerMode,
    setDrawerTab,
    setStepAgentId,
    setStepAgentLocked,
    setStepBlockKey,
    setStepDisplayName,
    setStepKindDraft,
    setStepOptional,
    setStepOverrideDraft,
    toggleBuilderAdvancedMode,
    workflowId,
    stepFormDirty,
    savingWorkflowSurface,
  } = ctx;

  useWorkflowSettingsHydration(ctx);

  const loadedStepId = useRef("");
  const lastObservedStepSignature = useRef("");
  // Reload the step drafts only when the step itself changed (its stored or
  // staged config), not whenever an unrelated structure change re-derives rows.
  const selectedStepSignature = selectedStep
    ? `${selectedStep.runtimeId}:${JSON.stringify(selectedStep.raw)}`
    : "";
  const httpReadbackScopeSignature = selectedStep?.stepKind === "http_request"
    ? JSON.stringify([ctx.stepWorkflowId, ctx.stepServerRevision]) : "";
  useEffect(() => {
    const sourceChanged = lastObservedStepSignature.current !== selectedStepSignature;
    lastObservedStepSignature.current = selectedStepSignature;
    // The new revision dependency must not reload an Agent/non-HTTP draft.
    if (!sourceChanged && ctx.stepKindDraft !== "http_request") return;
    if (!selectedStep || (selectedStep.stepKind === "agent" && ctx.agentDrafts && !ctx.agentDrafts.mayReconcileReadback()) || (selectedStep.stepKind === "http_request" && ctx.httpIntent && !ctx.httpIntent.mayReconcileReadback()) || (loadedStepId.current === selectedStep.runtimeId &&
      stepFormDirty && savingWorkflowSurface !== "step")) {
      return;
    }
    const ownsAcknowledgedReadback = ctx.stepKindDraft === "http_request" && selectedStep.stepKind === "http_request" &&
      ctx.httpIntent?.reconcileAcknowledgedReadback({
        workflowId: ctx.stepWorkflowId, revision: ctx.stepServerRevision, signature: selectedStepSignature,
      });
    if (!ownsAcknowledgedReadback) ctx.httpIntent?.beginEditorSession();
    loadedStepId.current = selectedStep.runtimeId;
    setStepDisplayName(selectedStep.primaryLabel || selectedStep.runtimeId);
    setStepKindDraft(selectedStep.stepKind || "agent");
    setStepOverrideDraft(jsonText(editableOverrideForStep(selectedStep)));
    setStepAgentId(selectedStep.agentId || "");
    setStepOptional(Boolean(selectedStep.optional));
    setStepBlockKey(selectedStep.blockKey || "");
  }, [selectedStepSignature, httpReadbackScopeSignature]);

  useEffect(() => {
    if (!workflowId) {
      return undefined;
    }
    const workflowWindow = globalThis as unknown as Window;
    workflowWindow.WF_ID = workflowId;
    workflowWindow.switchTab = () => undefined;
    workflowWindow.PUBLISHED_BLOCKS = publishedBlocks;
    workflowWindow._advancedMode = advancedMode;
    workflowWindow.DRAWER_STATE = {
      mode: drawerMode,
      tab: drawerTab,
    };
    workflowWindow.STEP_MODAL_STATE = {
      editing: drawerMode === "step",
    };
    workflowWindow._hasResolvablePublishedBlocks = () =>
      (workflowWindow.PUBLISHED_BLOCKS || []).some(
        (row) => row?.mapping_resolved !== false,
      );
    workflowWindow.closeBuilderDrawer = () => ignoreAsyncResult(closeDrawer());
    workflowWindow.toggleAdvancedMode = toggleBuilderAdvancedMode;
    workflowWindow.switchBuilderDrawerTab = (tab: string) => {
      const normalized = firstStringFromUnknownValue([tab, ""]).trim();
      if (!normalized) {
        return;
      }
      ignoreAsyncResult(changeDrawerTab(normalized));
    };
    workflowWindow.openGroupDrawer = (groupId: string, options?: { defaultTab?: string }) => {
      const normalizedGroupId = firstStringFromUnknownValue([groupId, ""]).trim();
      if (!normalizedGroupId) {
        return;
      }
      openGroupDrawer(normalizedGroupId, firstStringFromUnknownValue([options?.defaultTab, "settings"]));
    };
    workflowWindow.editStep = (stepId: string) => {
      const normalizedStepId = firstStringFromUnknownValue([stepId, ""]).trim();
      if (!normalizedStepId) {
        return;
      }
      openStepDrawer(normalizedStepId);
    };
    workflowWindow.addStep = (groupId = "", laneId = "") => {
      openNewStepDrawer({
        groupId: firstStringFromUnknownValue([groupId, ""]).trim(),
        laneId: firstStringFromUnknownValue([laneId, ""]).trim(),
      });
    };
    workflowWindow._setStepExecutorControlState = (input) => {
      const mappedAgents = Array.isArray(input?.mappedAgents)
        ? input.mappedAgents
        : [];
      const resolvedAgent = firstStringFromUnknownValue([input?.resolvedAgent, ""]).trim();
      const shouldLock = Boolean(resolvedAgent || mappedAgents.length === 1);
      setStepAgentLocked(shouldLock);
    };
    workflowWindow.openLibraryDrawerForStepRecovery = () => {
      setDrawerMode("library");
      setDrawerTab("smart-setup");
    };
    workflowWindow._publishedBlockDisplayName = (blockKey: string) => {
      const normalized = firstStringFromUnknownValue([blockKey, ""]).trim();
      const row = (workflowWindow.PUBLISHED_BLOCKS || []).find(
        (item) => firstStringFromUnknownValue([item?.block_key, ""]).trim() === normalized,
      );
      return firstStringFromUnknownValue([row?.display_name, normalized]);
    };
    workflowWindow.addStepFromLibrary = async (blockKey: string) => {
      const normalized = firstStringFromUnknownValue([blockKey, ""]).trim();
      openNewStepDrawer({ groupId: selectedGroupId || "" }, normalized, {
        displayName: workflowWindow._publishedBlockDisplayName?.(normalized) || normalized,
      });
    };
    workflowWindow.AUTOMATION_SCOPE = { kind: automationScopeKind };
    workflowWindow.addReaction = () => {
      const requestedScope = firstStringFromUnknownValue([workflowWindow.AUTOMATION_SCOPE?.kind, ""]).trim();
      if (
        requestedScope === "workflow" ||
        requestedScope === "group" ||
        requestedScope === "step"
      ) {
        setAutomationScopeKind(requestedScope);
      }
      if (requestedScope === "group") {
        setDrawerMode("group");
      } else if (requestedScope === "step") {
        setDrawerMode("step");
      } else {
        setDrawerMode("workflow-settings");
      }
      setDrawerTab("automation");
    };
    const onBuilderAction = (event: Event) => {
      const detail =
        (event as CustomEvent<{ action?: string }>).detail &&
        typeof (event as CustomEvent<{ action?: string }>).detail === "object"
          ? (event as CustomEvent<{ action?: string }>).detail
          : {};
      const action = firstStringFromUnknownValue([detail?.action, ""]).trim();
      if (!action) {
        return;
      }
      if (action === "workflow-settings") {
        ignoreAsyncResult(openWorkflowSettingsDrawer("smart-setup"));
        return;
      }
      if (action === "automation") {
        ignoreAsyncResult(openWorkflowSettingsDrawer("automation"));
        return;
      }
      if (action === "clone-workflow") {
        ignoreAsyncResult(handleCloneWorkflow());
        return;
      }
      if (action === "delete-workflow") {
        ignoreAsyncResult(handleDeleteWorkflow());
      }
    };
    globalThis.addEventListener(
      "platform:workflow-builder-action",
      onBuilderAction as EventListener,
    );
    return () => {
      globalThis.removeEventListener(
        "platform:workflow-builder-action",
        onBuilderAction as EventListener,
      );
    };
  }, [
    closeDrawer,
    changeDrawerTab,
    openWorkflowSettingsDrawer,
    advancedMode,
    automationScopeKind,
    drawerMode,
    drawerTab,
    handleCloneWorkflow,
    handleDeleteWorkflow,
    openGroupDrawer,
    openNewStepDrawer,
    openStepDrawer,
    publishedBlocks,
    selectedGroupId,
    toggleBuilderAdvancedMode,
    workflowId,
  ]);
}
