import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { useQueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/query/keys";
import { useParams } from "react-router";
import { useWorkflowDetailDomBridge } from "@/pages/workflows/detail/detail-dom-bridge";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { useWorkflowNormalRunController } from "./detail-normal-run-controller";
import { useWorkflowTestRunController } from "@/pages/workflows/detail/detail-test-run-controller";
import { useSelectedWorkflowTestRun } from "@/pages/workflows/detail/detail-selected-test-run";
import type { JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import type { JobDetailInspectionRequest } from "@/pages/jobs/detail";
import { useWorkflowRunBarController } from "@/pages/workflows/detail/detail-run-bar-controller";
import { useWorkflowTestRunEvidence } from "@/pages/workflows/detail/detail-test-run-evidence";
import { KeptTestOutputStepsContext } from "@/pages/workflows/builder/kept-test-output-context";
import type { RunPass } from "@/pages/workflows/detail/detail-test-run-results-model";
import { useWorkflowCanvasHandlers } from "@/pages/workflows/detail/detail-canvas-handlers";
import { useWorkflowHookExecutionMarks } from "@/pages/workflows/detail/detail-hook-run-marks";
import { useWorkflowHookNavigation } from "@/pages/workflows/detail/detail-hook-navigation";
import { canvasPlacement, hookMemberId, parseHookNodeId } from "@/pages/workflows/builder/hook-frames";
import { useWorkflowGroupActions } from "@/pages/workflows/detail/detail-group-actions";
import { useWorkflowStepRendererController } from "@/pages/workflows/detail/detail-step-renderer-controller";
import { useWorkflowDrawerController, type StepCreateContext } from "@/pages/workflows/detail/detail-drawer-controller";
import { WorkflowDetailPageShell } from "@/pages/workflows/detail/detail-page-shell";
import { useWorkflowBuilderDrawerResize } from "@/pages/workflows/detail/detail-builder-drawer-resize";
import { objectOrEmpty, textOrEmpty, workflowContractIssueSection } from "@/pages/workflows/detail/detail-helpers";
import { workflowCanvasNodeIdForExecutedLocation, type WorkflowRuntimeLocation } from "@/pages/workflows/detail/workflow-runtime-location";
import { useWorkflowDetailAuthoringState } from "./detail-page-authoring-state";
import { useStepDraftSession } from "./detail-step-draft-session";
import { stableJson, useWorkflowDetailStepAuthoring } from "./detail-page-step-authoring";
import { liveIssueResolved, withLiveValidation, type LiveStepDraft } from "./detail-live-validation";

type WorkflowsDetailPageProps = Readonly<{
  embedded?: boolean;
  surface?: "detail" | "builder";
}>;

export function WorkflowsDetailPage(props: WorkflowsDetailPageProps = {}) {
  const { workflowId = "" } = useParams();
  // A confirmed route change discards all local editors and pending operations.
  return <WorkflowDetailPageContent key={workflowId} {...props} />;
}

function WorkflowDetailPageContent({
  embedded = false,
  surface = "detail",
}: WorkflowsDetailPageProps) {
  const authoringState = useWorkflowDetailAuthoringState(surface, () => drawer);
  const {
    t, drawerLocation, session, developmentMode,
    workflowId, workflowInterfaceControlIdPrefix, searchParams, setSearchParams,
    drawerMode, setDrawerMode, drawerTab, setDrawerTab,
    workflowTestRunLaunchOpen, setWorkflowTestRunLaunchOpen, workflowTestRunResultsRequested, setWorkflowTestRunResultsRequested,
    pendingAcknowledgedRun, setPendingAcknowledgedRun, runVerificationError, setRunVerificationError,
    openingRunRequest, runNavigationFlight, setRunNavigationFlight, runNavigationFlightRef,
    nextRunNavigationSerial, selectedRegistration, submissionBridge, requestWorkflowTestRunResults,
    handleWorkflowTestRunSubmitted, selectedGroupId, setSelectedGroupId, selectedStepId,
    setSelectedStepId, stepCreateContext, setStepCreateContext, createAsEntry,
    setCreateAsEntry, groupCreateMode, setGroupCreateMode, extractionGroup,
    setExtractionGroup, groupFormSaveFailed, setGroupFormSaveFailed, groupDraft,
    setGroupDraft, builderCanvasFocus, builderRuntimeActiveNodeId, setBuilderRuntimeActiveNodeId,
    workflowContractSectionsExpanded, setWorkflowContractSectionsExpanded, advancedMode, setAdvancedMode,
    stepAgentLocked, setStepAgentLocked, stepDisplayName, setStepDisplayName,
    stepKindDraft, setStepKindDraft, stepToolCategoryDraft, setStepToolCategoryDraft,
    stepOverrideDraft, setStepOverrideDraft, onStepInputValidationChange, stepAgentId,
    setStepAgentId, stepOptional, setStepOptional, stepBlockKey,
    setStepBlockKey, reactionFormVisible, setReactionFormVisible, automationScopeKind,
    setAutomationScopeKind, saveNotice, saveNoticeIsError, setSaveNotice,
    setSaveError, savingWorkflowSurface, setSavingWorkflowSurface, builderConfirmDialog,
    requestConfirmation, data, selectedProjectId, serverRevision,
    workflow, workflowContractQuery, selectedCanvasIssue, setSelectedCanvasIssue,
    header, changeSet, authorDefinitionRows, publishedBlocks,
    validationErrorGroups, validationWarningGroups, openNewStepDrawerRef, openGroupDrawerRef,
    openStepDrawerRef, authorStructureLocked, allowHookDeclaration, hookScope,
    inHook, stepWorkflowId, stepChangeSet, stepServerRevision,
    stepCreateContextInScope, refetchStepSurfaces, selectedStep, selectedGroup,
    deletionGuard, readClipboardAuthority, requestGraphDeleteFromCanvas, definitionRows,
    displayCanvasGraph, executionCanvasGraph, entryKey, handleWorkflowRuntimeLocationChange,
  } = authoringState;
  const {
    settings, triggers, agentWorkflowRecord, agentDrafts,
    agentPreviewId, stepContractPreviewQuery, transformActionsQuery, selectedStepAuthoredId,
    stepOptions, httpIntent, stepActions, stepFormDirty,
    stepFormStatus, hasInvalidStepInput, stepFormUnsaved, groupFormDirty,
    confirmDiscardInvalidInput, confirmDiscardParameters, workflowTestRunBlockedReason, stepTestRunBlockedReason,
    wholeWorkflowRunBlockedReason, groupExtractionBlockedReason, authorStepPlayAllowed, leaveConfirmDialog,
  } = useWorkflowDetailStepAuthoring(authoringState);

  const canRunWorkflow = useIsAdmin();
  const launchLock = useRef<symbol | null>(null);
  const authorTestRun = useWorkflowTestRunController({
    launchLock,
    onRevisionConflict: changeSet.reportConflict,
    onWorkflowTestRunSubmitted: handleWorkflowTestRunSubmitted,
    selectedProjectId,
    selectedStep: inHook ? null : selectedStep,
    serverRevision,
    stepContractPreviewQuery,
    stepTestRunBlockedReason,
    workflowContractQuery,
    workflowId,
    workflowTestRunBlockedReason: wholeWorkflowRunBlockedReason,
    submissionOwner: submissionBridge,
  });
  // Transfer one parent observer only after the selected proof has verified.
  // An uncached history selection while Results is open first retires the old
  // embedded owner, then lets the selected hook verify the new job.
  const resultsDrawerOpen = drawerMode === "workflow-test-run" && drawerTab === "test-run";
  const [embeddedObservationKey, setEmbeddedObservationKey] = useState("");
  // Results read the selected run's observation; the builder no longer embeds Job Details.
  const resultsOwnsSnapshot = false;
  const selectedRun = useSelectedWorkflowTestRun({
    owner: { workflowId, projectId: selectedProjectId || "", editorRevision: serverRevision ?? null },
    restoredJobId: authorTestRun.workflowTestRunJobId,
    resultsOwnsObservation: resultsOwnsSnapshot,
    confirmReadOnlyNavigation: async (isCurrent) => {
      if (!isCurrent()) return false;
      const confirmed = await confirmDiscardInvalidInput();
      return confirmed && isCurrent();
    },
    openResults: (isCurrent) => drawer.openWorkflowTestRunResultsDrawer(isCurrent),
  });
  const verifiedSelectedProof = selectedRun.selected?.proof && selectedRun.verifiedDetail
    ? selectedRun.selected.proof : null;
  const verifiedSelectedKey = verifiedSelectedProof
    ? stableJson([verifiedSelectedProof.workflowId, verifiedSelectedProof.projectId,
      verifiedSelectedProof.editorRevision, verifiedSelectedProof.jobId, verifiedSelectedProof.generation]) : "";
  // A selected-key change may leave observation disabled for this single render.
  // Clearing the old committed handoff before paint enables uncached verification;
  // the old embedded owner is already withheld by the exact key equality below.
  const committedResultsProofKey = useRef("");
  useLayoutEffect(() => {
    const next = resultsDrawerOpen ? verifiedSelectedKey : "";
    committedResultsProofKey.current = next;
    setEmbeddedObservationKey((current) => current === next ? current : next);
  }, [resultsDrawerOpen, verifiedSelectedKey]);
  const embeddedOwnsSelectedRun = resultsDrawerOpen && Boolean(verifiedSelectedKey) && embeddedObservationKey === verifiedSelectedKey;
  useLayoutEffect(() => {
    const registration = {
      captureSubmissionOwner: selectedRun.captureSubmissionOwner,
      acceptAcknowledgement: selectedRun.acceptAcknowledgement,
      captureSelection: selectedRun.captureSelection,
    };
    selectedRegistration.current = registration;
    authorTestRun.refreshWorkflowTestRunSubmissionOwner();
    return () => { if (selectedRegistration.current === registration) selectedRegistration.current = null; };
  }); // Every committed registration, including selected owner readiness after its
      // layout state transition. Controller refresh deduplicates equal stamps;
      // stable method identities or a lucky query notification are not a trigger.
  const capturedSubmissionOwner = selectedRun.captureSubmissionOwner();
  const currentSubmissionOwner = capturedSubmissionOwner && capturedSubmissionOwner.workflowId === workflowId &&
    capturedSubmissionOwner.projectId === selectedProjectId && capturedSubmissionOwner.editorRevision === (serverRevision ?? null)
    ? capturedSubmissionOwner : null;
  const controlledParent = verifiedSelectedProof
    ? { proof: verifiedSelectedProof, snapshot: selectedRun.snapshot } : null;
  const visibleRunVerificationError = currentSubmissionOwner && runVerificationError && selectedRun.selected?.jobId === runVerificationError.jobId && submissionBridge.isCurrent(runVerificationError.owner)
    ? runVerificationError.message : "";
  const runNavigationKey = verifiedSelectedKey || (currentSubmissionOwner
    ? stableJson([currentSubmissionOwner.workflowId, currentSubmissionOwner.projectId, currentSubmissionOwner.editorRevision, currentSubmissionOwner.ownerEpoch]) : "");
  const runNavigationBusy = Boolean(runNavigationFlight?.key === runNavigationKey);
  const [workflowRunInspectionRequest, setWorkflowRunInspectionRequest] = useState<JobDetailInspectionRequest | null>(null);
  const nextInspectionSerial = useRef(0);
  useLayoutEffect(() => { setWorkflowRunInspectionRequest(null); }, [verifiedSelectedKey]);
  useLayoutEffect(() => {
    if (!resultsDrawerOpen) setWorkflowRunInspectionRequest(null);
  }, [resultsDrawerOpen]);
  const navigateSelectedRun = async (action: () => Promise<boolean>) => {
    const capturedOwner = currentSubmissionOwner;
    const capturedSelection = selectedRun.selected?.proof || null;
    if (!capturedOwner || !runNavigationKey || runNavigationFlightRef.current?.key === runNavigationKey ||
      capturedSelection && !selectedRun.isCurrent(capturedSelection)) return false;
    const flight = { key: runNavigationKey, serial: ++nextRunNavigationSerial.current };
    runNavigationFlightRef.current = flight;
    setRunNavigationFlight(flight);
    try {
      if (!submissionBridge.isCurrent(capturedOwner)) return false;
      return await action();
    } catch {
      return false;
    } finally {
      if (runNavigationFlightRef.current === flight) {
        runNavigationFlightRef.current = null;
        setRunNavigationFlight((current) => current?.serial === flight.serial ? null : current);
      }
    }
  };
  // A cached error/foreign row is not a fresh terminal verification result
  // while the old embedded handoff is retiring or the selected GET refetches.
  const selectedVerificationPending = !selectedRun.verifiedDetail && (resultsOwnsSnapshot || selectedRun.detailQuery.isFetching ||
    !selectedRun.detailQuery.isError && !selectedRun.detailQuery.data && Boolean(selectedRun.selected));
  const selectedVerificationFailed = !resultsOwnsSnapshot && !selectedRun.detailQuery.isFetching &&
    (selectedRun.detailQuery.isError || Boolean(selectedRun.detailQuery.data && !selectedRun.verifiedDetail));
  // The Approval step the selected run waits on, from its recorded executions.
  // The approval gate names the step when the recorded executions do not (yet).
  const [gateApprovalStepId, setGateApprovalStepId] = useState("");
  const recordedApproval = selectedRun.statusCode === "awaiting_approval"
    ? [...(selectedRun.snapshot?.steps || [])].reverse().find((step) => step.status.toLowerCase().startsWith("awaiting")) : undefined;
  const waitingApproval = recordedApproval || (selectedRun.statusCode === "awaiting_approval" && gateApprovalStepId ? { stepId: gateApprovalStepId, groupId: undefined, forEachId: undefined, stepExecutionId: "" } : undefined);
  const runNodeId = (stepId: string, groupId?: string, forEachId?: string) => serverRevision && !changeSet.hasChanges && !hookScope.childChangeSet.hasChanges
    ? workflowCanvasNodeIdForExecutedLocation(executionCanvasGraph, { stepId, groupId, forEachId, definitionWorkflowId: workflowId, definitionRevision: serverRevision }, workflowId, serverRevision, hookScope.frames) : "";
  const decisionPanelOpen = Boolean(waitingApproval && drawerMode === "step" && drawerTab === "test-run" && selectedStep?.runtimeId === waitingApproval.stepId);
  const { toolbarAction: canvasTestRunAction, ...runBar } = useWorkflowRunBarController({
    decisionPanelOpen,
    selectedRun,
    selectionKind: selectedVerificationPending ? "loading" : selectedVerificationFailed ? "error" : "empty",
    navigationBusy: runNavigationBusy,
    historyDisabled: !currentSubmissionOwner,
    navigate: navigateSelectedRun,
    roles: session?.user?.roles || session?.roles || [],
    timezone: String(session?.user?.timezone || objectOrEmpty(session).timezone || ""),
    runAgainBusy: authorTestRun.workflowTestRunMutation.isPending,
    runAgainBlockedReason: wholeWorkflowRunBlockedReason || (authorTestRun.workflowTestRunPending ? t("react.workflows.run_again_active", "A Test Run is already active.") : ""),
    requestRunAgain: async (proof) => {
      if (!selectedRun.isCurrent(proof)) return false;
      const outcome = await authorTestRun.startWorkflowTestRunAgain(proof.jobId, () => selectedRun.isCurrent(proof));
      if (outcome === "needs-inputs" && selectedRun.isCurrent(proof)) return Boolean(await drawer.openWorkflowTestRunDrawer(() => selectedRun.isCurrent(proof)));
      return outcome === "admitted";
    },
    requestReviewRequest: async (proof, approvalId, isCurrent) => {
      if (!isCurrent()) return false;
      // The request is decided in the Approval step's own Test Run tab.
      const nodeId = waitingApproval ? runNodeId(waitingApproval.stepId, waitingApproval.groupId, waitingApproval.forEachId) : "";
      if (nodeId) return Boolean(await canvasNavigation.openStep(nodeId, "", "test-run", isCurrent));
      if (!await drawer.openWorkflowTestRunResultsDrawer(isCurrent) || !isCurrent()) return false;
      setWorkflowRunInspectionRequest({ serial: ++nextInspectionSerial.current, kind: "review", jobId: proof.jobId, approvalId, isCurrent });
      return true;
    },
    requestOpenFailedStep: async (proof, target, isCurrent) => {
      if (!isCurrent()) return false;
      const nodeId = !changeSet.hasChanges && !hookScope.childChangeSet.hasChanges && serverRevision
        ? workflowCanvasNodeIdForExecutedLocation(executionCanvasGraph, target, workflowId, serverRevision, hookScope.frames) : "";
      const parsed = parseHookNodeId(nodeId);
      // Deferred cross-Hook routes are not an acknowledged editor opening.
      // Keep those occurrences in the immutable run inspector instead.
      const currentEditor = nodeId && (executionCanvasGraph?.steps || []).some((step) => step.id === nodeId) &&
        (!parsed || parsed.hookId === hookScope.effectiveHookId && hookScope.childRows.definitionRows.stepRowsById.has(parsed.memberStepId));
      if (currentEditor) return Boolean(await canvasNavigation.openStep(nodeId, "", "smart-setup", isCurrent));
      if (!isCurrent() || !await drawer.openWorkflowTestRunResultsDrawer(isCurrent) || !isCurrent()) return false;
      setWorkflowRunInspectionRequest({ serial: ++nextInspectionSerial.current, kind: "step", jobId: proof.jobId, ...target, isCurrent });
      return true;
    },
    confirmStop: () => requestConfirmation({
      title: t("react.workflows.run_bar_stop_title", "Stop Test Run?"),
      body: t("react.jobs.confirm_cancel_current_run", "Cancel this job? The current run will stop."),
      confirmLabel: t("react.workflows.run_bar_stop", "Stop"),
    }),
  });
  const hookRunMarks = useWorkflowHookExecutionMarks(executionCanvasGraph, hookScope.frames, workflowId, changeSet.hasChanges || hookScope.childChangeSet.hasChanges ? 0 : serverRevision ?? 0, selectedRun.selected?.jobId, false, { parent: controlledParent, owner: currentSubmissionOwner && currentSubmissionOwner.workflowId === workflowId &&
      currentSubmissionOwner.projectId === selectedProjectId && currentSubmissionOwner.editorRevision === (serverRevision ?? null)
      ? { projectId: currentSubmissionOwner.projectId, ownerEpoch: currentSubmissionOwner.ownerEpoch } : undefined });
  // A step inside a hook is tested as a step of its own workflow.
  const hookTestRun = useWorkflowTestRunController({
    launchLock,
    onRevisionConflict: hookScope.childChangeSet.reportConflict,
    selectedProjectId,
    selectedStep: inHook ? selectedStep : null,
    serverRevision: hookScope.childRevision,
    stepContractPreviewQuery,
    stepTestRunBlockedReason,
    workflowContractQuery,
    workflowId: hookScope.childWorkflowId || workflowId,
    stepTestRunScope: JSON.stringify([workflowId, serverRevision ?? null, hookScope.effectiveHookId, hookScope.childWorkflowId]),
    workflowTestRunBlockedReason,
  });
  const normalRun = useWorkflowNormalRunController({
    workflowId, projectId: selectedProjectId || "", revision: serverRevision,
    enabled: objectOrEmpty(workflow).enabled === true,
    canRun: canRunWorkflow,
    blockedReason: wholeWorkflowRunBlockedReason, contract: workflowContractQuery.data,
    contractReady: !workflowContractQuery.isPending && !workflowContractQuery.isFetching && !workflowContractQuery.isError,
    summary: authorTestRun.workflowTestRunSummary, inputs: authorTestRun.workflowTestRunInputs,
    fieldErrors: authorTestRun.workflowTestRunValidationErrors,
    testBusy: authorTestRun.workflowTestRunMutation.isPending || authorTestRun.stepTestRunMutation.isPending || hookTestRun.stepTestRunMutation.isPending,
    open: workflowTestRunLaunchOpen, launchLock,
  });

  const testRun = inHook
    ? {
        ...authorTestRun,
        ...Object.fromEntries(
          Object.entries(hookTestRun).filter(([key]) => key.startsWith("stepTestRun") || key.startsWith("setStepTestRun")),
        ),
      } as typeof authorTestRun
    : authorTestRun;
  const selectedIssueWorkflowContractSection =
    workflowContractIssueSection(selectedCanvasIssue);
  const selectedIssueInCurrentDrawer = Boolean(
    selectedCanvasIssue &&
      ((drawerMode === "step" &&
        selectedCanvasIssue.scope === "step" &&
        (selectedCanvasIssue.step_id === selectedStepAuthoredId ||
          selectedCanvasIssue.scope_id === selectedStepAuthoredId)) ||
        (drawerMode === "group" &&
          selectedCanvasIssue.scope === "group" &&
          (selectedCanvasIssue.group_id === selectedGroupId ||
            selectedCanvasIssue.scope_id === selectedGroupId)) ||
        (["workflow-settings", "workflow-interface"].includes(drawerMode) &&
          selectedCanvasIssue.scope === "workflow")),
  );
  let selectedIssueSuggestedAction = "";
  if (selectedCanvasIssue) {
    selectedIssueSuggestedAction = textOrEmpty(selectedCanvasIssue.suggested_action);
    if (selectedIssueInCurrentDrawer && /^open\b/i.test(selectedIssueSuggestedAction)) {
      selectedIssueSuggestedAction = "";
    }
  }

  const hookNavigation = useWorkflowHookNavigation({
    ownerKey: JSON.stringify([workflowId, selectedProjectId]),
    hookScope, authorGroupRows: authorDefinitionRows.groupRows, authorStructureLocked, allowHookDeclaration,
    drawerMode, selectedGroupId, selectedStepId, confirmDiscardParameters,
    searchParams, setSearchParams, setSaveError,
  });
  const drawer = useWorkflowDrawerController({
    localToolDraftOwnerKey: JSON.stringify([workflowId, selectedProjectId]),
    stepToolCategoryDraft,
    captureLocalToolDraftOwner: (canvasId: string) => {
      const member = parseHookNodeId(canvasId);
      if (member?.memberStepId) return member.hookId === hookScope.effectiveHookId
        ? hookScope.childChangeSet.captureStagedStepNoticeOwner(member.memberStepId) : null;
      return changeSet.captureStagedStepNoticeOwner(canvasId);
    },
    drawerRequestKey: drawerLocation.key,
    agentDrafts,
    httpIntent,
    prepareSelection: hookNavigation.needsHookSelectionGuard ? hookNavigation.prepareSelection : undefined,
    resolveGroupRow: hookNavigation.resolveGroupRow,
    protectedWorkflowStructureRedacted: data.protectedWorkflowStructureRedacted,
    hasInvalidStepInput,
    confirmDiscardInvalidInput,
    parameterSwitchNeedsConfirmation: Boolean(stepFormUnsaved || groupFormDirty || groupFormSaveFailed || stepCreateContext || savingWorkflowSurface),
    groupFormDirty,
    savingWorkflowSurface,
    confirmDiscardParameters,
    selectedStepId,
    groupDraft,
    advancedMode,
    definitionRows,
    drawerMode,
    drawerTab,
    groupCreateMode,
    searchParams,
    selectedGroupId,
    selectedIssueWorkflowContractSection,
    setAdvancedMode,
    setAutomationScopeKind,
    setDrawerMode,
    setDrawerTab,
    setGroupCreateMode,
    setGroupDraft,
    setReactionFormVisible,
    setSaveNotice,
    setSearchParams,
    setSelectedCanvasIssue,
    setSelectedGroupId,
    setSelectedStepId,
    setStepAgentId,
    setStepBlockKey,
    setStepCreateContext,
    setStepDisplayName,
    setStepKindDraft,
    setStepToolCategoryDraft,
    setStepOptional,
    setStepOverrideDraft,
    setWorkflowContractSectionsExpanded,
    setWorkflowTestRunApprovalComment: testRun.setWorkflowTestRunApprovalComment,
    setWorkflowTestRunMessage: testRun.setWorkflowTestRunMessage,
    setWorkflowTestRunLaunchOpen,
  });
  // The edited step's configuration issues follow its draft while the value is typed (57).
  const liveStepDraft: LiveStepDraft | null = drawerMode === "step" && selectedStep && !inHook && !selectedStep.local ? {
    stepId: selectedStepAuthoredId || selectedStep.runtimeId,
    savedKind: selectedStep.stepKind || "agent",
    kind: stepKindDraft || "agent",
    overrideDraft: stepOverrideDraft,
    agentId: stepAgentId,
    agentFromParticipant: agentDrafts?.mode === "participant",
    connectionMissing: stepOptions.missingConnectorConnection ? true : null,
  } : null;
  const liveDraftKey = liveStepDraft ? JSON.stringify(liveStepDraft) : "";
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const liveCanvasGraph = useMemo(() => withLiveValidation(displayCanvasGraph, liveStepDraft, t), [displayCanvasGraph, liveDraftKey, t]);
  const liveSelectedCanvasIssue = liveIssueResolved(selectedCanvasIssue, liveStepDraft, t) ? null : selectedCanvasIssue;
  // "Move to…" in a step's menu opens its editor at Placement, the choice focused.
  const [placementFocusNonce, setPlacementFocusNonce] = useState(0);
  const openStepPlacement = async (stepId: string) => {
    const opened = await drawer.openStepDrawer(stepId, "", false, "smart-setup");
    if (opened !== false) setPlacementFocusNonce((nonce) => nonce + 1);
  };
  // Unsaved automation rules ask before the drawer closes or another tab opens.
  const [automationDirty, setAutomationDirty] = useState(false);
  const confirmDiscardAutomation = async () => !automationDirty || requestConfirmation({
    title: t("react.workflows.discard_unsaved_changes_title", "Discard unsaved changes?"),
    body: t("react.workflows.automation_discard_body", "Changes to automation rules that you have not saved will be lost."),
    confirmLabel: t("react.workflows.discard_changes", "Discard changes"),
    destructive: true,
  });
  // Only unsaved rules wait for an answer; otherwise the drawer acts in the same turn, as before.
  const closeDrawerGuarded = async () => {
    if (automationDirty) {
      if (!(await confirmDiscardAutomation())) return;
      setAutomationDirty(false);
    }
    await drawer.closeDrawer();
  };
  const changeDrawerTabGuarded = async (tab: string) => {
    if (tab !== drawerTab && automationDirty) {
      if (!(await confirmDiscardAutomation())) return;
      setAutomationDirty(false);
    }
    await drawer.changeDrawerTab(tab);
  };
  useLayoutEffect(() => { setBuilderRuntimeActiveNodeId(""); }, [verifiedSelectedKey, setBuilderRuntimeActiveNodeId]);
  // Submission stores an original-owner ACK intent, not a guessed selected proof.
  // Wait for committed selection AND matching detail before guarded Results opens.
  const pendingRunIntentRef = useRef({ pendingAcknowledgedRun, workflowTestRunResultsRequested, verifiedSelectedKey });
  useLayoutEffect(() => {
    pendingRunIntentRef.current = { pendingAcknowledgedRun, workflowTestRunResultsRequested, verifiedSelectedKey };
  });
  const selectedRunIsCurrent = selectedRun.isCurrent;
  const openSelectedRunResults = selectedRun.requestOpenResults;
  const selectedRunVerifiedDetail = selectedRun.verifiedDetail;
  const selectedRunSelection = selectedRun.selected;
  useEffect(() => {
    if (workflowTestRunLaunchOpen || (!pendingAcknowledgedRun && !workflowTestRunResultsRequested)) return;
    const intent = pendingAcknowledgedRun;
    const explicit = workflowTestRunResultsRequested;
    if (explicit && (!submissionBridge.isCurrent(explicit.owner) || !selectedRunIsCurrent(explicit.selection))) {
      setWorkflowTestRunResultsRequested((current) => current?.serial === explicit.serial ? null : current);
      return;
    }
    if (intent && !submissionBridge.isCurrent(intent.owner)) {
      setPendingAcknowledgedRun((current) => current?.serial === intent.serial ? null : current);
      return;
    }
    if (intent && selectedRunSelection && selectedRunSelection.jobId !== intent.jobId) {
      setPendingAcknowledgedRun((current) => current?.serial === intent.serial ? null : current);
      return;
    }
    if (selectedVerificationFailed) {
      if (intent) {
        setRunVerificationError((current) => submissionBridge.isCurrent(intent.owner)
          ? { owner: intent.owner, jobId: intent.jobId, message: t("react.workflows.test_run_verify_failed", "The Test Run was started, but its details could not be verified. Select a run from history to inspect it.") } : current);
        setPendingAcknowledgedRun((current) => current?.serial === intent.serial ? null : current);
      }
      setWorkflowTestRunResultsRequested(null);
      return;
    }
    // A pending detail request is not Cancel. Leave the launcher's draft closed.
    if (!verifiedSelectedProof || !selectedRunVerifiedDetail || intent && verifiedSelectedProof.jobId !== intent.jobId) return;
    const captured = verifiedSelectedProof;
    const requestKey = `${intent ? `ack:${intent.serial}` : `explicit:${explicit?.serial}`}:${verifiedSelectedKey}`;
    if (openingRunRequest.current === requestKey) return;
    openingRunRequest.current = requestKey;
    const isCurrent = () => selectedRunIsCurrent(captured) &&
      (!intent || submissionBridge.isCurrent(intent.owner) && pendingRunIntentRef.current.pendingAcknowledgedRun?.serial === intent.serial) &&
      (!explicit || submissionBridge.isCurrent(explicit.owner) && selectedRunIsCurrent(explicit.selection) &&
        pendingRunIntentRef.current.workflowTestRunResultsRequested?.serial === explicit.serial) &&
      pendingRunIntentRef.current.verifiedSelectedKey === verifiedSelectedKey;
    void openSelectedRunResults().then((opened) => {
      if (!isCurrent()) return;
      if (intent) setPendingAcknowledgedRun((current) => current?.serial === intent.serial ? null : current);
      setWorkflowTestRunResultsRequested(null);
      if (!opened) setWorkflowTestRunLaunchOpen(true);
    }).catch(() => {
      if (!isCurrent()) return;
      if (intent) {
        setRunVerificationError((current) => submissionBridge.isCurrent(intent.owner)
          ? { owner: intent.owner, jobId: intent.jobId, message: t("react.workflows.test_run_verify_failed", "The Test Run was started, but its details could not be verified. Select a run from history to inspect it.") } : current);
        setPendingAcknowledgedRun((current) => current?.serial === intent.serial ? null : current);
      }
      setWorkflowTestRunResultsRequested(null);
    }).finally(() => { if (openingRunRequest.current === requestKey) openingRunRequest.current = ""; });
  }, [pendingAcknowledgedRun, selectedVerificationFailed, selectedRunVerifiedDetail, selectedRunSelection, selectedRunIsCurrent, openSelectedRunResults, verifiedSelectedProof,
    submissionBridge, t, verifiedSelectedKey, workflowTestRunResultsRequested, workflowTestRunLaunchOpen, openingRunRequest, setPendingAcknowledgedRun, setRunVerificationError, setWorkflowTestRunLaunchOpen, setWorkflowTestRunResultsRequested]);
  useEffect(() => {
    openNewStepDrawerRef.current = drawer.openNewStepDrawer as (context?: Record<string, unknown>) => unknown;
    openStepDrawerRef.current = drawer.openStepDrawer;
    openGroupDrawerRef.current = drawer.openGroupDrawer;
  }, [drawer.openNewStepDrawer, drawer.openStepDrawer, drawer.openGroupDrawer, openGroupDrawerRef, openNewStepDrawerRef, openStepDrawerRef]);
  // The step editor's unsaved values survive a reload in the edit session.
  const authorPreview = changeSet.preview;
  const stepExistsForDraft = useCallback(
    (stepId: string) => authorPreview.steps.some((row) => row.step_id === stepId),
    [authorPreview],
  );
  useStepDraftSession({
    session: authoringState.editSession,
    enabled: drawerMode === "step" && Boolean(selectedStep) && !selectedStep?.local && !inHook,
    values: {
      stepId: textOrEmpty(selectedStep?.runtimeId), displayName: stepDisplayName, kind: stepKindDraft,
      overrideDraft: stepOverrideDraft, agentId: stepAgentId, optional: stepOptional, blockKey: stepBlockKey,
    },
    unsaved: stepFormUnsaved,
    stepExists: stepExistsForDraft,
    openStep: (stepId) => drawer.openStepDrawer(stepId, "", true, "smart-setup"),
    apply: ({ values: restored, reentry }) => {
      setStepDisplayName(restored.displayName);
      if (restored.kind) setStepKindDraft(restored.kind);
      if (restored.overrideDraft !== null) setStepOverrideDraft(restored.overrideDraft);
      setStepAgentId(restored.agentId);
      setStepOptional(restored.optional);
      setStepBlockKey(restored.blockKey);
      setSaveNotice(reentry.length
        ? t("react.workflows.edit_session_step_reentry", "Your unsaved changes to this step were restored. Parameters that held credentials were not kept; the saved parameters are shown — enter your change to them again.")
        : t("react.workflows.edit_session_step_restored", "Your unsaved changes to this step were restored."));
    },
  });
  // Edits on hook members go to the hook's workflow; the rest stay the author's.
  const graphActions = hookScope.route(useWorkflowCanvasHandlers({
    changeSet,
    openGroupDrawer: drawer.openGroupDrawer,
    openNewStepDrawer: drawer.openNewStepDrawer,
    openStepDrawer: drawer.openStepDrawer,
    openWorkflowSettingsDrawer: drawer.openWorkflowSettingsDrawer,
    requestConfirmation,
    setAutomationScopeKind,
    setDrawerMode,
    setDrawerTab,
    setSaveNotice,
    setSaveError,
    setSelectedCanvasIssue,
  }));
  /**
   * What a click on the canvas opens. A hook frame's header opens its hook
   * step's drawer; a member opens in the step drawer of its own workflow; the
   * frame's Add step adds to that workflow, creating and connecting it first
   * when the hook is empty. One place to change how a hook frame behaves.
   */
  const canvasNavigation = hookNavigation.canvasNavigation(drawer);
  const testRunEvidence = useWorkflowTestRunEvidence({
    selectedRun,
    runBar,
    workflowId,
    canvasGraph: executionCanvasGraph,
    openStep: (stepId, tab) => {
      const nodeId = runNodeId(stepId) || stepId;
      ignoreAsyncResult(canvasNavigation.openStep(nodeId, "", tab === "test-run" ? "test-run" : "smart-setup"));
    },
    lightStep: (pass: RunPass | null) => handleWorkflowRuntimeLocationChange(pass && serverRevision ? {
      stepId: pass.stepId, groupId: pass.groupId || undefined, forEachId: pass.forEachId || undefined,
      definitionWorkflowId: workflowId, definitionRevision: serverRevision,
    } : null),
    changeInputs: () => { ignoreAsyncResult(drawer.openWorkflowTestRunDrawer()); },
  });
  // The remembered run's state poll can see a change before the compact detail polls again:
  // re-read the selected job's detail when its own durable status moves.
  const rememberedPayload = objectOrEmpty(authorTestRun.workflowTestRunPayload);
  const rememberedJobId = textOrEmpty(rememberedPayload.job_id);
  // Its status and its gate: a new approval of the same run is a change too.
  const rememberedStatus = [textOrEmpty(rememberedPayload.final_status) || textOrEmpty(rememberedPayload.status), textOrEmpty(rememberedPayload.approval_id)].join(":");
  const selectedRunJobId = selectedRun.selected?.jobId || "";
  const jobDetailClient = useQueryClient();
  useEffect(() => {
    if (!rememberedJobId || rememberedJobId !== selectedRunJobId || !rememberedStatus) return;
    ignoreAsyncResult(jobDetailClient.invalidateQueries({ queryKey: queryKeys.jobDetail(rememberedJobId) }));
  }, [jobDetailClient, rememberedJobId, rememberedStatus, selectedRunJobId]);
  const keptTestOutputSteps = useMemo(() => new Set(testRunEvidence.kept.keys()), [testRunEvidence.kept]);
  const evidenceApprovalStepId = testRunEvidence.approval?.stepId || "";
  useEffect(() => { setGateApprovalStepId(evidenceApprovalStepId); }, [evidenceApprovalStepId]);
  // An Approval reached in a test run opens its Test Run tab by itself, unless
  // a panel with other work is open; then the run bar offers Review request.
  const openedApprovalRef = useRef("");
  const waitingApprovalKey = waitingApproval && selectedRun.selected ? `${selectedRun.selected.jobId}:${waitingApproval.stepExecutionId || waitingApproval.stepId}` : "";
  useEffect(() => {
    if (!waitingApprovalKey || !waitingApproval || openedApprovalRef.current === waitingApprovalKey) return;
    openedApprovalRef.current = waitingApprovalKey;
    const panelFree = !drawerMode || drawerMode === "workflow-test-run" || (drawerMode === "step" && drawerTab === "test-run" && !stepFormUnsaved);
    if (!panelFree) return;
    const nodeId = runNodeId(waitingApproval.stepId, waitingApproval.groupId, waitingApproval.forEachId);
    if (nodeId) ignoreAsyncResult(canvasNavigation.openStep(nodeId, "", "test-run"));
  });
  const groupActions = useWorkflowGroupActions({
    changeSet: stepChangeSet,
    deletionGuard,
    groupCreateMode,
    groupDraft,
    refetchWorkflowSurfaces: refetchStepSurfaces,
    requestConfirmation,
    selectedGroup,
    serverRevision: stepServerRevision,
    setGroupCreateMode,
    setGroupDraft,
    setSaveError: (message: string) => {
      setGroupFormSaveFailed(true);
      setSaveError(message);
    },
    setSaveNotice: (message: string) => {
      setGroupFormSaveFailed(false);
      setSaveNotice(message);
    },
    setSavingWorkflowSurface,
    setSelectedGroupId: inHook ? hookScope.selectChildGroup : setSelectedGroupId,
    setDrawerMode,
    workflowId: stepWorkflowId,
  });
  // The step drawer edits in the ids of the workflow it shows.
  const structureActions = inHook ? hookScope.childHandlers : graphActions;
  const stepStructureContext = {
    hookName: inHook ? hookScope.framesByHook.get(hookScope.effectiveHookId)?.displayName || hookScope.effectiveHookId : "",
    placementFocusNonce,
    connectPort: structureActions.connectPort,
    createAsEntry,
    disconnectPort: structureActions.disconnectPort,
    moveStep: structureActions.moveStep,
    preview: stepChangeSet.preview,
    selectedStep,
    setCreateAsEntry,
    setEntry: structureActions.setEntry,
    setStepCreateContext: (context: StepCreateContext | null) => setStepCreateContext(inHook && context ? hookScope.frameCreateContext(context) as StepCreateContext : context),
    stepCreateContext: stepCreateContextInScope,
  };
  const renderer = useWorkflowStepRendererController({
    stepContractPreviewQuery,
    agentDrafts, agentWorkflowRecord,
    httpIntent,
    httpFieldFeedback: stepActions.httpFieldFeedback,
    confirmDiscardInvalidInput,
    ...stepOptions,
    handleApplyIfConditionFix: stepActions.handleApplyIfConditionFix,
    onStepInputValidationChange,
    hookDeclarationIdentityLocked: !developmentMode,
    hookDeclarationIssues: stepChangeSet.applyIssues.filter((issue) => issue.path?.startsWith("steps." + (selectedStep?.runtimeId || selectedStepId) + ".")),
    selectedCanvasIssue,
    selectedIssueWorkflowContractSection,
    selectedProjectId,
    selectedStep,
    setStepAgentId,
    setStepKindDraft,
    setStepToolCategoryDraft,
    setStepOverrideDraft,
    setWorkflowContractSectionsExpanded,
    stepAgentId,
    stepAgentLocked,
    stepKindDraft,
    stepOverrideDraft,
    stepStructureContext,
    transformActionsQuery,
    workflowContractSectionsExpanded,
    workflowId: stepWorkflowId,
    workflowInterfaceControlIdPrefix,
    workflowInterfaceNameValidationAttempted: settings.workflowInterfaceNameValidationAttempted,
    workflowInterfaceOutputsDraft: settings.workflowInterfaceOutputsDraft,
    workflowInterfaceReturnedFiles: settings.workflowInterfaceReturnedFiles,
  });

  useWorkflowDetailDomBridge({
    agentDrafts,
    httpIntent,
    ...settings,
    stepKindDraft,
    stepWorkflowId,
    stepServerRevision,
    stepFormDirty,
    savingWorkflowSurface,
    advancedMode,
    automationScopeKind,
    drawerMode,
    drawerTab,
    handleCloneWorkflow: header.handleCloneWorkflow,
    handleDeleteWorkflow: header.handleDeleteWorkflow,
    openGroupDrawer: canvasNavigation.openGroup,
    openNewStepDrawer: canvasNavigation.openNewStep,
    openStepDrawer: canvasNavigation.openStep,
    openWorkflowSettingsDrawer: drawer.openWorkflowSettingsDrawer,
    closeDrawer: drawer.closeDrawer,
    changeDrawerTab: drawer.changeDrawerTab,
    publishedBlocks,
    selectedGroupId,
    selectedStep,
    setAutomationScopeKind,
    setDrawerMode,
    setDrawerTab,
    setReactionFormVisible,
    setStepAgentId,
    setStepAgentLocked,
    setStepBlockKey,
    setStepDisplayName,
    setStepKindDraft,
    setStepOptional,
    setStepOverrideDraft,
    toggleBuilderAdvancedMode: drawer.toggleBuilderAdvancedMode,
    workflow,
    workflowContractQuery,
    workflowId,
  });

  const drawerOpen = Boolean(drawerMode);
  const builderSurface = surface === "builder";
  const drawerResize = useWorkflowBuilderDrawerResize(builderSurface);

  return (
    <KeptTestOutputStepsContext.Provider value={keptTestOutputSteps}>
    <WorkflowDetailPageShell
      ctx={{
        ...data,
        ...header,
        ...settings,
        ...triggers,
        ...testRun,
        ...stepOptions,
        ...drawer,
        ...graphActions,
        ...groupActions,
        ...stepActions,
        ...renderer,
        closeDrawer: closeDrawerGuarded,
        changeDrawerTab: changeDrawerTabGuarded,
        onAutomationDirtyChange: setAutomationDirty,
        openStepPlacement,
        stepFormDirty,
        stepFormUnsaved,
        groupFormDirty,
        groupFormSaveFailed,
        cancelNewDrawerItem: () => {
          setStepCreateContext(null);
          setGroupCreateMode(false);
          ignoreAsyncResult(closeDrawerGuarded());
        },
        agentDrafts, agentWorkflowRecord, agentPreviewId, setStepAgentId, stepAgentLocked,
        httpIntent,
        ...drawerResize,
        advancedMode,
        builderCanvasFocusNodeId: builderCanvasFocus.nodeId,
        builderCanvasFocusNonce: builderCanvasFocus.nonce,
        builderCanvasFocusPlace: builderCanvasFocus.place,
        builderCanvasFocusCreation: builderCanvasFocus.creation,
        builderConfirmDialog: leaveConfirmDialog.open ? leaveConfirmDialog : builderConfirmDialog,
        builderRuntimeActiveNodeId,
        builderSurface,
        changeSet,
        editSession: authoringState.editSession,
        deletionGuard,
        readClipboardAuthority,
        requestGraphDeleteFromCanvas,
        canvasChangeSet: hookScope.canvasChangeSet,
        placementChildChangeSet: hookScope.childChangeSet,
        placementChildWorkflowId: hookScope.childWorkflowId,
        placementChildHookId: hookScope.effectiveHookId,
        canvasCommandViewIdentity: JSON.stringify([hookScope.effectiveHookId, hookScope.childWorkflowId]),
        // A restored revision replaces saved parameters: the next editor opens from the server.
        forgetEditorSelection: () => {
          setSelectedStepId("");
          setSelectedGroupId("");
        },
        canvasNavigation,
        allowHookDeclaration,
        hookFrames: hookScope.frames,
        clearHook: hookScope.clearHook,
        hookScopeActive: inHook,
        hookTestRunBlockedReason: workflowTestRunBlockedReason,
        onHookRuntimeLocationChange: handleWorkflowRuntimeLocationChange,
        onHookTestRunStarted: hookRunMarks.onHookTestRunStarted,
        // Unlike embedded observation, return ownership survives a Results-only close.
        drawerReturnFocusOwner: stableJson([workflowId, selectedProjectId, hookScope.effectiveHookId, hookScope.childWorkflowId,
          selectedRun.selected?.jobId, selectedRun.selected?.proof.editorRevision, selectedRun.selected?.proof.generation]),
        selectedWorkflowTestRun: embeddedOwnsSelectedRun && verifiedSelectedProof && selectedRun.verifiedDetail ? {
          jobId: verifiedSelectedProof.jobId, workflowId: verifiedSelectedProof.workflowId,
          projectId: verifiedSelectedProof.projectId, generation: verifiedSelectedProof.generation,
          isCurrent: () => selectedRun.isCurrent(verifiedSelectedProof) && committedResultsProofKey.current === verifiedSelectedKey,
        } : null,
        workflowRunInspectionRequest,
        onWorkflowExecutionSnapshotChange: (snapshot: JobExecutionSnapshot | null) => {
          if (verifiedSelectedProof && selectedRun.isCurrent(verifiedSelectedProof) && committedResultsProofKey.current === verifiedSelectedKey) selectedRun.acceptSnapshot(verifiedSelectedProof, snapshot);
        },
        onSelectedWorkflowRuntimeLocationChange: (location: WorkflowRuntimeLocation | null) => {
          if (verifiedSelectedProof && selectedRun.isCurrent(verifiedSelectedProof) && committedResultsProofKey.current === verifiedSelectedKey) handleWorkflowRuntimeLocationChange(location);
        },
        selectedTestRunControlled: true,
        workflowTestRunMutationIsCurrent: authorTestRun.workflowTestRunMutationIsCurrent,
        workflowTestRunApprovalMutationIsCurrent: authorTestRun.workflowTestRunApprovalMutationIsCurrent,
        selectedRunLoading: selectedVerificationPending,
        selectedRunVerificationError: visibleRunVerificationError,
        canvasRunBar: runBar,
        testRunEvidence,
        testRunGraph: executionCanvasGraph,
        openStepFromLaunch: (nodeId: string) => canvasNavigation.openStep(nodeId),
        canvasTestRunAction,
        runtimeMarks: hookRunMarks.marks,
        runtimeEdges: hookRunMarks.edges,
        containerRuntimeMarks: hookRunMarks.containerMarks,
        hookExecutionObservers: hookRunMarks.observers,
        groupEditorContext: {
          ...(inHook ? { canConvertGroup: false } : {}),
          preview: stepChangeSet.preview,
          workflowId: stepWorkflowId,
          connectPort: structureActions.connectPort,
          disconnectPort: structureActions.disconnectPort,
          setEntry: structureActions.setEntry,
          openNewStepDrawer: (context: Partial<StepCreateContext>) => canvasNavigation.openNewStep(inHook ? { ...context, ...canvasPlacement(hookScope.effectiveHookId, { groupId: "", forEachId: "", laneId: "", ...context }) } : context),
          openStepDrawer: (id: string) => canvasNavigation.openStep(inHook ? hookMemberId(hookScope.effectiveHookId, id) : id),
          workflowRootEntryId: stepChangeSet.preview.graph.entry ? `${stepChangeSet.preview.graph.entry.kind}:${stepChangeSet.preview.graph.entry.id}` : "",
        },
        hookDeclarationPending: !inHook && [...hookScope.frames.values()].some((frame) => frame.stepId === selectedStep?.runtimeId && frame.declarationPending),
        selectedStepHookId: inHook ? "" : textOrEmpty(objectOrEmpty(objectOrEmpty(objectOrEmpty(objectOrEmpty(selectedStep?.raw).override).invoke_workflow).hook).hook_id),
        stepWorkflowId,
        definitionRows: authorDefinitionRows,
        displayCanvasGraph: liveCanvasGraph,
        drawerMode,
        drawerOpen,
        drawerTab,
        embedded,
        entryKey,
        groupCreateMode,
        groupDraft,
        groupExtractionBlockedReason,
        extractionGroup,
        setExtractionGroup,
        serverRevision,
        handleWorkflowRuntimeLocationChange,
        isValid: data.isValid,
        publishedBlocks,
        reactionFormVisible,
        requestConfirmation,
        saveNotice,
        saveNoticeIsError,
        savingWorkflowSurface,
        scheduleProjectHint: textOrEmpty(searchParams.get("project_id")),
        onScheduleProjectChange: (projectId: string) => {
          const next = new URLSearchParams(searchParams);
          next.set("project_id", projectId);
          setSearchParams(next, { replace: true });
        },
        selectedCanvasIssue: liveSelectedCanvasIssue,
        selectedGroup,
        selectedGroupId,
        selectedIssueSuggestedAction,
        selectedStep,
        selectedStepAuthoredId,
        selectedStepId,
        setAdvancedMode,
        setAutomationScopeKind,
        setDrawerMode,
        setDrawerTab,
        setGroupDraft,
        setReactionFormVisible,
        setStepBlockKey,
        setStepDisplayName,
        setStepOptional,
        setStepOverrideDraft,
        stepAgentId,
        stepBlockKey,
        stepContractPreviewQuery,
        stepCreateContext,
        stepDisplayName,
        stepFormStatus,
        stepKindChangeStaged: Boolean(
          selectedStep &&
            stepChangeSet.operations.some(
              (operation) => operation.op === "change_step_kind" && operation.step_id === selectedStep.runtimeId,
            ),
        ),
        stepKindDraft,
        stepKindSelection: stepKindDraft === "connector" ? stepOptions.stepToolCategory : stepKindDraft,
        stepOptional,
        stepOverrideDraft,
        stepStructurePending: Boolean(selectedStep && stepChangeSet.touchesStep(selectedStep.runtimeId)),
        stepTestRunBlockedReason,
        authorStepPlayAllowed,
        surface,
        validationErrorGroups,
        validationWarningGroups,
        workflowId,
        workflowInterfaceControlIdPrefix,
        workflowRootEntryId: entryKey,
        workflowTestRunBlockedReason: wholeWorkflowRunBlockedReason,
        workflowTestRunLaunchOpen,
        normalRun,
        requestWorkflowTestRunResults,
        closeWorkflowTestRunLaunch: () => {
          if (!authorTestRun.workflowTestRunMutation.isPending && !normalRun.busy) setWorkflowTestRunLaunchOpen(false);
        },
      }}
    />
    </KeptTestOutputStepsContext.Provider>
  );
}
