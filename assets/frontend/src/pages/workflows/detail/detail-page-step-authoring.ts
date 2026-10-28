import { useAgentStepDrafts } from "./detail-agent-step-drafts";
import { agentParticipants, agentPreviewBinding } from "./detail-agent-step-fields";
import { useWorkflowLeaveGuard } from "@/pages/workflows/detail/detail-leave-guard";
import { useCallback, useEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchWorkflowStepContractPreview, fetchWorkflowTransformActions } from "@/pages/workflows/shared/api";
import { useWorkflowTriggersController } from "@/pages/workflows/detail/detail-triggers-controller";
import { useWorkflowSettingsController } from "@/pages/workflows/detail/detail-workflow-settings-controller";
import { useHttpRequestIntent } from "./detail-http-request-intent";
import { useWorkflowStepActions, type StepFormSaveState } from "@/pages/workflows/detail/detail-step-actions";
import { useWorkflowStepOptionsController } from "@/pages/workflows/detail/detail-step-options-controller";
import { groupDraftFromRow } from "@/pages/workflows/detail/detail-drawer-controller";
import { editableOverrideForStep, jsonText, objectOrEmpty, stepOverrideObjectFromDraft, textOrEmpty, type WorkflowStepRow } from "@/pages/workflows/detail/detail-helpers";
import { locallyCreatedForEachIds, locallyCreatedStepIds } from "@/pages/workflows/builder/change-set";
import { workflowVariablesContext } from "@/pages/workflows/detail/detail-variable-filter";
import type { useWorkflowDetailAuthoringState } from "./detail-page-authoring-state";

export function stableJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(stableJson).join(",")}]`;
  }
  if (value && typeof value === "object") {
    return `{${Object.keys(value as Record<string, unknown>)
      .sort((left, right) => left.localeCompare(right))
      .map((key) => `${JSON.stringify(key)}:${stableJson((value as Record<string, unknown>)[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

/** Whether the open step form differs from the step it was loaded from. */
function stepFormDiffers(
  step: WorkflowStepRow,
  drafts: {
    agentId: string;
    blockKey: string;
    displayName: string;
    kind: string;
    optional: boolean;
    overrideDraft: string;
  },
): boolean {
  if ((drafts.displayName || "") !== (step.primaryLabel || step.runtimeId)) return true;
  if ((drafts.kind || "agent") !== (step.stepKind || "agent")) return true;
  if ((drafts.agentId || "") !== (step.agentId || "")) return true;
  if ((drafts.blockKey || "") !== (step.blockKey || "")) return true;
  if (Boolean(drafts.optional) !== Boolean(step.optional)) return true;
  let current: Record<string, unknown>;
  try {
    current = stepOverrideObjectFromDraft(drafts.overrideDraft);
  } catch {
    return true;
  }
  return stableJson(current) !== stableJson(editableOverrideForStep(step));
}

export function useWorkflowDetailStepAuthoring(state: ReturnType<typeof useWorkflowDetailAuthoringState>) {
  const {
    t, workflowId, workflowInterfaceControlIdPrefix, searchParams,
    setSearchParams, drawerMode, drawerTab, setDrawerTab,
    selectedStepId, setSelectedStepId, stepCreateContext, setStepCreateContext,
    createAsEntry, setCreateAsEntry, groupCreateMode, setGroupCreateMode,
    groupFormSaveFailed, setGroupFormSaveFailed, groupDraft, setGroupDraft,
    focusCanvasNode, stepDisplayName, setStepDisplayName, stepKindDraft,
    setStepKindDraft, stepToolCategoryDraft, setStepToolCategoryDraft, stepOverrideDraft,
    setStepOverrideDraft, stepInputErrors, onStepInputValidationChange, stepAgentId,
    setStepAgentId, stepOptional, setStepOptional, stepBlockKey,
    setStepBlockKey, setSaveNotice, setSaveError, savingWorkflowSurface,
    setSavingWorkflowSurface, requestConfirmation, data, availableProjectsQuery,
    linkedProjectQuery, selectedProjectId, hookProjectId, workflowContractQuery, workflowRecord,
    setStagedStepSaveNotice, refetchWorkflowSurfaces, changeSet, editSession, hookScope,
    inHook, stepWorkflowId, stepChangeSet, stepServerRevision,
    stepWorkflow, stepDefinitionRows, stepCreateContextInScope, refetchStepSurfaces,
    selectedStep, selectedGroup, deletionGuard,
  } = state;
  const settings = useWorkflowSettingsController({
    changeSet,
    drawerMode,
    refetchWorkflowSurfaces: () => refetchWorkflowSurfaces(),
    setDrawerTab,
    setSaveError,
    setSaveNotice,
    setSavingWorkflowSurface,
    workflowId,
    workflowInterfaceControlIdPrefix,
    workflowRecord,
  });
  const triggers = useWorkflowTriggersController({
    drawerMode,
    drawerTab,
    linkedProjectQuery,
    searchParams,
    selectedProjectId,
    workflowContractQuery,
    workflowId,
    workflowInterfaceInputsDraft: settings.workflowInterfaceInputsDraft,
    workflowRecord,
  });
  const agentWorkflowRecord = objectOrEmpty(stepWorkflow);
  const agentDrafts = useAgentStepDrafts({
    owner: stableJson([workflowId, inHook ? hookProjectId : selectedProjectId, inHook ? hookScope.authorityEpoch : null, stepWorkflowId, inHook, hookScope.effectiveHookId, selectedStepId || "new", stepKindDraft]),
    raw: objectOrEmpty(selectedStep?.raw), createContext: stepCreateContext,
  });
  const agentPreviewBindingValue = agentPreviewBinding(stepOverrideDraft);
  const agentBlockKey = agentPreviewBindingValue.key;
  const agentParticipant = agentParticipants(agentWorkflowRecord).find((row) => row.key === agentDrafts.participantKey);
  const agentPreviewId = agentDrafts.mode === "participant" ? agentParticipant?.agentId || "" : stepAgentId;
  const selectedStepOnServer = Boolean(selectedStep && !selectedStep.local);
  const previewRequestedStepId = selectedStepOnServer ? selectedStep?.runtimeId || "" : "";
  const stepPreviewEnabled =
    drawerMode === "step" &&
    Boolean(stepWorkflowId) &&
    (stepKindDraft === "agent" ? agentPreviewBindingValue.valid && Boolean(agentPreviewId) : selectedStepOnServer);
  const stepContractPreviewQuery = useQuery({
    queryKey: [
      "workflows",
      "step-contract-preview",
      stepWorkflowId,
      previewRequestedStepId,
      stepKindDraft === "agent" ? agentPreviewId : stepAgentId,
      stepKindDraft,
      stepKindDraft === "agent" ? agentBlockKey : stepBlockKey,
      stepKindDraft === "agent" ? agentDrafts.mode : "",
      stepKindDraft === "agent" ? agentDrafts.participantKey : "",
    ],
    queryFn: () =>
      fetchWorkflowStepContractPreview(stepWorkflowId, {
        stepId: previewRequestedStepId,
        agentId: stepKindDraft === "agent" ? agentPreviewId : "",
        participantKey: stepKindDraft === "agent" && agentDrafts.mode === "participant" ? agentDrafts.participantKey : "",
        blockKey: stepKindDraft === "agent" ? agentBlockKey : "",
        blockBindingMode: stepKindDraft === "agent" ? agentBlockKey ? "explicit" : "auto" : undefined,
      }),
    enabled: stepPreviewEnabled,
    retry: false,
  });
  const transformActionsQuery = useQuery({
    queryKey: ["workflows", "transform-actions", stepWorkflowId],
    queryFn: () => fetchWorkflowTransformActions(stepWorkflowId),
    enabled:
      drawerMode === "step" &&
      (stepKindDraft === "transform" || stepKindDraft === "tabular_feed") &&
      Boolean(stepWorkflowId) && (!inHook || hookScope.childChangeSet.ownerReady),
    retry: false,
  });
  const selectedStepAuthoredId =
    textOrEmpty(objectOrEmpty(selectedStep?.raw).step_id) ||
    textOrEmpty(selectedStep?.runtimeId) ||
    "—";

  // B7: the variables a step's fields offer follow where the step runs, applied or not.
  const variablesContext = useMemo(
    () =>
      workflowVariablesContext({
        createContext: selectedStep ? null : stepCreateContextInScope,
        drawerMode,
        groupAnchorStepId: selectedGroup?.steps.find((step) => !step.local)?.runtimeId || "",
        localForEachIds: locallyCreatedForEachIds(stepChangeSet.operations),
        localStepIds: locallyCreatedStepIds(stepChangeSet.operations),
        preview: stepChangeSet.preview,
        selectedStep,
      }),
    [stepChangeSet.operations, stepChangeSet.preview, drawerMode, selectedGroup, selectedStep, stepCreateContextInScope],
  );
  const activeHookFrame = inHook ? hookScope.framesByHook.get(hookScope.effectiveHookId) : undefined;
  const savedHookStep = Array.isArray(state.workflow?.steps)
    ? state.workflow.steps.find((step) => step.step_id === activeHookFrame?.stepId) : undefined;
  const savedHookDeclaration = objectOrEmpty(objectOrEmpty(objectOrEmpty(savedHookStep?.override).invoke_workflow).hook);
  const hookPresentation = hookProjectId && activeHookFrame && !activeHookFrame.declarationPending &&
    activeHookFrame.targetWorkflowId === stepWorkflowId && activeHookFrame.bindingRevision > 0 &&
    textOrEmpty(savedHookDeclaration.hook_id) === hookScope.effectiveHookId
    ? { parentWorkflowId: workflowId, projectId: hookProjectId, hookId: hookScope.effectiveHookId,
      stepId: activeHookFrame.stepId, bindingRevision: activeHookFrame.bindingRevision,
      childWorkflowId: stepWorkflowId, declaration: savedHookDeclaration } : null;
  const stepOptions = useWorkflowStepOptionsController({
    availableProjectsQuery,
    hookPresentation,
    definitionRows: stepDefinitionRows,
    drawerMode,
    linkedProjectQuery,
    selectedProjectId: inHook ? hookProjectId : selectedProjectId,
    selectedStep,
    setSearchParams,
    stepAgentId,
    stepKindDraft,
    stepToolCategoryDraft,
    stepOverrideDraft,
    variablesContext,
    workflow: stepWorkflow,
    workflowId: stepWorkflowId,
  });
  const httpIntent = useHttpRequestIntent(stableJson([stepWorkflowId, selectedStep?.runtimeId || "new", stepKindDraft]), onStepInputValidationChange);
  const stepActionsController = useWorkflowStepActions({
    agentDrafts, agentWorkflowRecord,
    httpIntent,
    changeSet: stepChangeSet,
    deletionGuard,
    connectorConnectionNeedsProjectLink: stepOptions.connectorConnectionNeedsProjectLink,
    connectorMissingRequiredInputNames: stepOptions.connectorMissingRequiredInputNames,
    connectorRequiredInputsMissing: stepOptions.connectorRequiredInputsMissing,
    createAsEntry,
    extensionConnectionReadinessBlocked: stepOptions.extensionConnectionReadinessBlocked,
    extensionConnectionReadinessMessage: stepOptions.extensionConnectionReadinessMessage,
    focusCanvasNode: inHook ? hookScope.focusChildNode : focusCanvasNode,
    missingConnectorConnection: stepOptions.missingConnectorConnection,
    refetchWorkflowSurfaces: refetchStepSurfaces,
    requestConfirmation,
    selectedStep,
    serverRevision: stepServerRevision,
    setCreateAsEntry,
    setSaveError,
    setSaveNotice,
    setStagedStepSaveNotice,
    setSavingWorkflowSurface,
    setSelectedStepId: inHook ? hookScope.selectChildStep : setSelectedStepId,
    setStepCreateContext,
    setStepOverrideDraft,
    staleConnectorConnectionId: stepOptions.staleConnectorConnectionId,
    staleConnectorOperationId: stepOptions.staleConnectorOperationId,
    stepAgentId,
    stepBlockKey,
    stepCreateContext: stepCreateContextInScope,
    stepDisplayName,
    stepInputErrors,
    stepKindDraft,
    stepOptional,
    stepOverrideDraft,
    workflowId: stepWorkflowId,
  });
  const stepActions = {
    ...stepActionsController,
    handleSaveStep: (...args: Parameters<typeof stepActionsController.handleSaveStep>) => {
      if (inHook && !hookScope.isChildAuthorityCurrent()) return Promise.resolve();
      return stepActionsController.handleSaveStep(...args);
    },
  };
  const { setStepFormSaveState, stepFormSaveState } = stepActions;
  useEffect(() => {
    setStepFormSaveState("idle");
  }, [selectedStepId, setStepFormSaveState]);
  useEffect(() => {
    setStepFormSaveState((current) => current === "saved_with_issues" ? "idle" : current);
  }, [stepWorkflowId, stepKindDraft, setStepFormSaveState]);
  const stepFormDirty = (stepKindDraft === "agent" && agentDrafts.dirty) || Boolean(
    selectedStep &&
      stepFormDiffers(selectedStep, {
        agentId: stepAgentId,
        blockKey: stepBlockKey,
        displayName: stepDisplayName,
        kind: stepKindDraft,
        optional: stepOptional,
        overrideDraft: stepOverrideDraft,
      }),
  );
  let stepFormStatus: StepFormSaveState | "dirty" = "idle";
  if (savingWorkflowSurface === "step") stepFormStatus = "saving";
  else if (stepFormSaveState === "error") stepFormStatus = "error";
  else if (stepFormDirty) stepFormStatus = "dirty";
  else if (stepFormSaveState === "saved") stepFormStatus = "saved";
  else if (stepFormSaveState === "saved_with_issues") stepFormStatus = "saved_with_issues";
  const hasInvalidStepInput = Object.values(stepInputErrors).some(Boolean) || httpIntent.hasRaw() || (stepKindDraft === "agent" && agentDrafts.invalid);
  const stepFormUnsaved =
    hasInvalidStepInput ||
    stepFormStatus === "dirty" || stepFormStatus === "saving" || stepFormStatus === "error";
  const groupFormDirty = Boolean(
    groupCreateMode ||
    (selectedGroup && groupDraft.groupId === selectedGroup.groupId &&
      stableJson(groupDraft) !== stableJson(groupDraftFromRow(selectedGroup))),
  );
  const confirmDiscardInvalidInput = useCallback(async (deferHttpReset = false) => {
    if (!hasInvalidStepInput && !httpIntent.hasRaw()) return true;
    const token = httpIntent.hasRaw() ? httpIntent.capture() : null;
    const agentToken = stepKindDraft === "agent" && agentDrafts.invalid ? agentDrafts.capture() : null;
    const confirmed = await requestConfirmation({
      title: t("react.workflows.discard_invalid_input_title", "Discard invalid input?"),
      body: t("react.workflows.discard_invalid_input_body", "This field contains invalid input. Keep editing it, or discard it before leaving this editor."),
      confirmLabel: t("react.workflows.discard_invalid_input", "Discard input"),
    });
    const current = (!token || httpIntent.isUnedited(token)) && (!agentToken || agentDrafts.isUnedited(agentToken));
    if (confirmed && current && agentToken && !deferHttpReset) agentDrafts.reset();
    if (confirmed && current && token && !deferHttpReset) httpIntent.beginEditorSession();
    return confirmed && current;
  }, [agentDrafts, stepKindDraft, hasInvalidStepInput, httpIntent, requestConfirmation, t]);
  const confirmDiscardParameters = useCallback(async () => {
    if (savingWorkflowSurface) return false;
    if (!stepFormUnsaved && !groupFormDirty && !groupFormSaveFailed && !stepCreateContext) return true;
    const agentToken = stepKindDraft === "agent" ? agentDrafts.capture() : null;
    const confirmed = await requestConfirmation({
      title: t("react.workflows.discard_parameters_title", "Discard unsaved parameters?"),
      body: t("react.workflows.discard_parameters_body", "The current step or group has unsaved parameters. Discard them before opening another editor?"),
      confirmLabel: t("react.workflows.discard_parameters", "Discard parameters"),
    });
    if (!confirmed || (agentToken && !agentDrafts.isUnedited(agentToken))) return false;
    httpIntent.beginEditorSession();
    agentDrafts.reset();
    if (selectedStep) {
      setStepDisplayName(selectedStep.primaryLabel || selectedStep.runtimeId);
      setStepKindDraft(selectedStep.stepKind || "agent");
      setStepToolCategoryDraft("");
      setStepOverrideDraft(jsonText(editableOverrideForStep(selectedStep)));
      setStepAgentId(selectedStep.agentId || "");
      setStepBlockKey(selectedStep.blockKey || "");
      setStepOptional(Boolean(selectedStep.optional));
    }
    if (selectedGroup) setGroupDraft(groupDraftFromRow(selectedGroup));
    setStepCreateContext(null);
    setGroupCreateMode(false);
    setStepFormSaveState("idle");
    setGroupFormSaveFailed(false);
    setSaveNotice("");
    return true;
  }, [agentDrafts, groupFormDirty, groupFormSaveFailed, httpIntent, requestConfirmation, savingWorkflowSurface, selectedGroup, selectedStep, setSaveNotice, setStepFormSaveState, stepCreateContext, stepFormUnsaved, stepKindDraft, t, setGroupCreateMode, setGroupDraft, setGroupFormSaveFailed, setStepAgentId, setStepBlockKey, setStepCreateContext, setStepDisplayName, setStepKindDraft, setStepOptional, setStepOverrideDraft, setStepToolCategoryDraft]);
  const applyOrDiscardMessage = t(
    "react.workflows.test_run_blocked_structure",
    "Apply or discard the structure changes before running a test.",
  );
  const saveParametersMessage = t(
    "react.workflows.test_run_blocked_parameters",
    "Save the step parameters before running a test.",
  );
  let workflowTestRunBlockedReason = "";
  // The whole run includes the hooks: their unapplied changes block it too.
  if (changeSet.hasChanges || hookScope.childChangeSet.hasChanges) workflowTestRunBlockedReason = applyOrDiscardMessage;
  else if (stepFormUnsaved || stepCreateContext || groupFormDirty || settings.workflowFormDirty ||
    settings.workflowFormSaveFailed || savingWorkflowSurface || groupFormSaveFailed) {
    workflowTestRunBlockedReason = t(
      "react.workflows.test_run_blocked_all_parameters",
      "Save the workflow, group and step parameters before running a test.",
    );
  }
  let stepTestRunBlockedReason = "";
  const interfaceDefinitionIssues = objectOrEmpty(workflowContractQuery.data).definition_issues;
  const missingInterfaceChannel = objectOrEmpty(objectOrEmpty(workflowContractQuery.data).interface).enabled === true
    && Array.isArray(interfaceDefinitionIssues) && interfaceDefinitionIssues.some((issue) =>
      objectOrEmpty(issue).code === "workflow.interface.trigger_channels_missing");
  const wholeWorkflowRunBlockedReason = workflowTestRunBlockedReason || (data.workflowRunsBlocked
    ? data.workflowDrawerNotice || t("react.workflows.workflow_unavailable", "Workflow unavailable") : "")
    || (missingInterfaceChannel ? t("react.workflows.test_run_channels_required", "Choose at least one trigger channel before running a Test Run.") : "");
  const groupExtractionBlockedReason = changeSet.hasChanges
    ? t("react.workflows.convert_save_structure", "Apply or discard structure changes before converting a group.")
    : (stepFormUnsaved || stepCreateContext || groupFormDirty || settings.workflowFormDirty ||
      settings.workflowFormSaveFailed || savingWorkflowSurface || groupFormSaveFailed)
      ? t("react.workflows.convert_save_parameters", "Save the workflow, group and step parameters before converting a group.")
      : "";
  // B28: the server refuses a Step Test Run of a protected author workflow
  // (403); say so before the click and keep the whole-workflow Test Run.
  // Contextual navigation requires an explicit public permission; denial wins.
  // The existing author/child submission controller and blocked reasons stay unchanged.
  const publishedStepPermission = data.exportPolicyQuery.data?.step_test_run_allowed;
  const portabilityStepPermission = objectOrEmpty(workflowRecord.portability).step_test_run_allowed;
  const authorStepPlayAllowed = publishedStepPermission !== false && portabilityStepPermission !== false &&
    (publishedStepPermission === true || portabilityStepPermission === true);
  const stepTestRunAllowed = inHook || (
    data.exportPolicyQuery.data?.step_test_run_allowed !== false &&
    objectOrEmpty(workflowRecord.portability).step_test_run_allowed !== false);
  if (!stepTestRunAllowed) {
    stepTestRunBlockedReason = t(
      "react.workflows.step_test_run_protected",
      "A step of a protected workflow does not run on its own. Run the whole workflow with Test Run, or test your own workflow in the hook panel.",
    );
  } else if (selectedStep?.local) {
    stepTestRunBlockedReason = t(
      "react.workflows.test_run_blocked_new_step",
      "This step exists only in the unapplied changes. Apply them before testing it.",
    );
  } else if (stepChangeSet.hasChanges) {
    stepTestRunBlockedReason = applyOrDiscardMessage;
  } else if (stepFormUnsaved) {
    stepTestRunBlockedReason = saveParametersMessage;
  }

  // One dirty rule for the guard: not saved on the server (structure, forms, a held
  // copy, an Apply in flight). An Undo history after Apply alone is not unsaved work.
  const leaveConfirmDialog = useWorkflowLeaveGuard(
    changeSet.hasChanges || hookScope.childChangeSet.hasChanges || Boolean(workflowTestRunBlockedReason) || editSession.dirty,
    { onNavigate: editSession.noteNavigation, onDiscard: editSession.discardOnLeave },
  );
  return {
    settings, triggers, agentWorkflowRecord, agentDrafts,
    agentPreviewId, stepContractPreviewQuery, transformActionsQuery, selectedStepAuthoredId,
    stepOptions, httpIntent, stepActions, stepFormDirty,
    stepFormStatus, hasInvalidStepInput, stepFormUnsaved, groupFormDirty,
    confirmDiscardInvalidInput, confirmDiscardParameters, workflowTestRunBlockedReason, stepTestRunBlockedReason,
    wholeWorkflowRunBlockedReason, groupExtractionBlockedReason, authorStepPlayAllowed, leaveConfirmDialog,
  } as const;
}
