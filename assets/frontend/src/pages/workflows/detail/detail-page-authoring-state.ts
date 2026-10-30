import { useFocusedValidationIssue } from "./detail-focused-validation-issue";
import { useCallback, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router";
import { type WorkflowTestRunSubmissionBridge, type WorkflowTestRunSubmissionProof } from "@/pages/workflows/detail/detail-test-run-controller";
import { type SelectedTestRunProof } from "@/pages/workflows/detail/detail-selected-test-run";
import type { WorkflowTestRunResult } from "@/pages/workflows/shared/simulate-api";
import { useWorkflowDeletionGuard, type DeleteGuardSnapshot } from "@/pages/workflows/detail/detail-canvas-handlers";
import { requestGraphDelete, requestGraphDeleteBatch, type GraphDeleteAuthority, type GraphDeleteBatchAuthority } from "@/pages/workflows/detail/detail-graph-commands";
import type { CanvasClipboardSnapshot, CanvasCommandSelection, CanvasConfirmedCreation, GraphDeleteSelection, RequestGraphDelete } from "@/pages/workflows/builder/canvas-types";
import { deriveDeletionBatch } from "@/pages/workflows/builder/canvas-delete-batch";
import type { ClipboardAuthority, ClipboardCommand } from "@/pages/workflows/detail/detail-clipboard-commands";
import { ROOT_PLACEMENT, stepPlacement, type WorkflowDefinitionModel, type WorkflowPlacement } from "@/pages/workflows/builder/change-set";
import { useWorkflowHookProjectContext } from "./detail-hook-project-context";
import { useWorkflowHookScope } from "@/pages/workflows/detail/detail-hook-scope";
import { hookMemberId, parseHookNodeId, withHookFrames } from "@/pages/workflows/builder/hook-frames";
import { useWorkflowChangeSet, type StagedStepNoticeOwner } from "@/pages/workflows/detail/detail-change-set";
import { useWorkflowEditSession } from "@/pages/workflows/detail/use-workflow-edit-session";
import { emptyGroupDraft, type useWorkflowDrawerController, type GroupDraft, type StepCreateContext } from "@/pages/workflows/detail/detail-drawer-controller";
import { useWorkflowDefinitionRows, useWorkflowDetailDataController } from "@/pages/workflows/detail/detail-data-controller";
import { useWorkflowHeaderActionsController } from "@/pages/workflows/detail/detail-header-actions-controller";
import { type AutomationScopeKind, objectOrEmpty, readWorkflowContractSectionState } from "@/pages/workflows/detail/detail-helpers";
import { workflowCanvasNodeIdForExecutedLocation, type WorkflowRuntimeLocation } from "@/pages/workflows/detail/workflow-runtime-location";
import { canvasGraphWithDefinition } from "@/pages/workflows/builder/change-set";
import { useConfirmDialog } from "@/components/shared/use-confirm-dialog";
import { useI18n } from "@/lib/i18n/useI18n";
import { useAuth } from "@/lib/auth/useAuth";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

// Drawer access stays deferred to the original event callbacks; the caller creates it later.
export function useWorkflowDetailAuthoringState(
  surface: "detail" | "builder",
  getDrawer: () => ReturnType<typeof useWorkflowDrawerController>,
) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const drawerLocation = useLocation();
  const { session } = useAuth();
  const isAdmin = useIsAdmin();
  const developmentMode = session?.environment?.mode === "development";
  const { workflowId = "" } = useParams();
  const workflowInterfaceControlIdPrefix = useId().replaceAll(":", "");
  const builderPath = `/registry/workflows/${encodeURIComponent(
    workflowId,
  )}/builder`;
  const [searchParams, setSearchParams] = useSearchParams();
  const [drawerMode, setDrawerMode] = useState<
    "" | "workflow-settings" | "workflow-test-run" | "step" | "group" | "library"
  >("");
  const [drawerTab, setDrawerTab] = useState("smart-setup");
  const [workflowTestRunLaunchOpen, setWorkflowTestRunLaunchOpen] = useState(false);
  const [workflowTestRunResultsRequested, setWorkflowTestRunResultsRequested] = useState<{ owner: WorkflowTestRunSubmissionProof; selection: SelectedTestRunProof; serial: number } | null>(null);
  const [pendingAcknowledgedRun, setPendingAcknowledgedRun] = useState<{
    jobId: string; owner: WorkflowTestRunSubmissionProof; serial: number;
  } | null>(null);
  const [runVerificationError, setRunVerificationError] = useState<{ owner: WorkflowTestRunSubmissionProof; jobId: string; message: string } | null>(null);
  const nextAcknowledgementSerial = useRef(0);
  const openingRunRequest = useRef("");
  const [runNavigationFlight, setRunNavigationFlight] = useState<{ key: string; serial: number } | null>(null);
  const runNavigationFlightRef = useRef<{ key: string; serial: number } | null>(null);
  const nextRunNavigationSerial = useRef(0);
  const selectedRegistration = useRef<(Pick<WorkflowTestRunSubmissionBridge, "captureSubmissionOwner" | "acceptAcknowledgement"> & { captureSelection: () => SelectedTestRunProof | null }) | null>(null);
  const acceptedAcknowledgement = useRef<{ jobId: string; owner: WorkflowTestRunSubmissionProof; serial: number } | null>(null);
  const submissionBridge = useMemo<WorkflowTestRunSubmissionBridge>(() => ({
    captureSubmissionOwner: () => selectedRegistration.current?.captureSubmissionOwner() ?? null,
    isCurrent: (captured) => {
      const current = selectedRegistration.current?.captureSubmissionOwner();
      return Boolean(current?.workflowId === captured.workflowId && current.projectId === captured.projectId &&
        current.editorRevision === captured.editorRevision && current.ownerEpoch === captured.ownerEpoch);
    },
    acceptAcknowledgement: (payload, captured) => {
      const accepted = selectedRegistration.current?.acceptAcknowledgement(payload, captured) ?? false;
      if (accepted) acceptedAcknowledgement.current = {
        jobId: String(payload.job_id || ""), owner: captured, serial: ++nextAcknowledgementSerial.current,
      };
      return accepted;
    },
  }), []);
  const requestWorkflowTestRunResults = useCallback(() => {
    const owner = submissionBridge.captureSubmissionOwner();
    const selection = selectedRegistration.current?.captureSelection();
    if (!owner || !selection || !submissionBridge.isCurrent(owner)) return;
    setWorkflowTestRunLaunchOpen(false);
    setWorkflowTestRunResultsRequested({ owner, selection, serial: ++nextAcknowledgementSerial.current });
  }, [submissionBridge]);
  const handleWorkflowTestRunSubmitted = useCallback((result: WorkflowTestRunResult) => {
    const accepted = acceptedAcknowledgement.current;
    acceptedAcknowledgement.current = null;
    if (accepted?.jobId !== String(result.job_id || "")) return;
    setRunVerificationError(null);
    setWorkflowTestRunResultsRequested(null);
    setPendingAcknowledgedRun(accepted);
    setWorkflowTestRunLaunchOpen(false);
  }, []);
  const [selectedGroupId, setSelectedGroupId] = useState("");
  const [selectedStepId, setSelectedStepId] = useState("");
  const [stepCreateContext, setStepCreateContext] = useState<StepCreateContext | null>(null);
  const [createAsEntry, setCreateAsEntry] = useState(false);
  const [groupCreateMode, setGroupCreateMode] = useState(false);
  const [extractionGroup, setExtractionGroup] = useState<{ id: string; name: string } | null>(null);
  const [groupFormSaveFailed, setGroupFormSaveFailed] = useState(false);
  const [groupDraft, setGroupDraft] = useState<GroupDraft>(() => emptyGroupDraft());
  // One request per added or inserted step: the canvas centres on that node once it exists.
  const [builderCanvasFocus, setBuilderCanvasFocus] = useState<{ nodeId: string; nonce: number; place: boolean; creation: CanvasConfirmedCreation | null }>({ nodeId: "", nonce: 0, place: false, creation: null });
  const focusCanvasNode = useCallback((nodeId: string, options: { place?: boolean; creation?: Omit<CanvasConfirmedCreation, "nodeId" | "nonce"> } = {}) => {
    setBuilderCanvasFocus((current) => {
      const nonce = current.nonce + 1;
      return { nodeId, nonce, place: Boolean(options.place), creation: options.creation ? { ...options.creation, nodeId, nonce } : null };
    });
  }, []);
  const [builderRuntimeActiveNodeId, setBuilderRuntimeActiveNodeId] =
    useState("");
  const [
    workflowContractSectionsExpanded,
    setWorkflowContractSectionsExpanded,
  ] = useState(readWorkflowContractSectionState);
  const [advancedMode, setAdvancedMode] = useState(false);
  const [stepAgentLocked, setStepAgentLocked] = useState(false);
  const [stepDisplayName, setStepDisplayName] = useState("");
  const [stepKindDraft, setStepKindDraft] = useState("agent");
  const [stepToolCategoryDraft, setStepToolCategoryDraft] = useState("");
  const [stepOverrideDraft, setStepOverrideDraft] = useState("");
  const [stepInputErrors, setStepInputErrors] = useState<Record<string, string>>({});
  const onStepInputValidationChange = useCallback((name: string, error: string, isCurrent?: () => boolean) => {
    setStepInputErrors((current) => {
      if (isCurrent && !isCurrent()) return current;
      if ((current[name] || "") === error) return current;
      const next = { ...current };
      if (error) next[name] = error;
      else delete next[name];
      return next;
    });
  }, []);
  const [stepAgentId, setStepAgentId] = useState("");
  const [stepOptional, setStepOptional] = useState(false);
  const [stepBlockKey, setStepBlockKey] = useState("");
  const [reactionFormVisible, setReactionFormVisible] = useState(false);
  const [automationScopeKind, setAutomationScopeKind] =
    useState<AutomationScopeKind>("workflow");
  const [saveNoticeState, setSaveNoticeState] = useState<{ message: string; isError: boolean; stagedStepOwner?: StagedStepNoticeOwner }>({ message: "", isError: false });
  const saveNotice = saveNoticeState.message;
  // A failed save must look like one: errors render as an alert, other notes do not.
  const saveNoticeIsError = saveNoticeState.isError;
  const setSaveNotice = useCallback((message: string) => {
    setSaveNoticeState({ message, isError: false });
  }, []);
  const setSaveError = useCallback((message: string) => {
    setSaveNoticeState({ message, isError: true });
  }, []);
  const [savingWorkflowSurface, setSavingWorkflowSurface] = useState<
    "" | "workflow" | "group" | "step"
  >("");
  const { confirmDialog: builderConfirmDialog, requestConfirmation } = useConfirmDialog();

  const data = useWorkflowDetailDataController({
    builderPath,
    drawerMode,
    workflowTestRunLaunchOpen,
    searchParams,
    surface,
    workflowId,
  });
  const {
    availableProjectsQuery,
    canvasGraphQuery,
    detailQuery,
    linkedProjectQuery,
    selectedProjectId,
    serverDefinition,
    serverRevision,
    validationErrors,
    validationWarnings,
    workflow,
    workflowContractQuery,
    workflowRecord,
  } = data;
  const { selectedCanvasIssue, setSelectedCanvasIssue } = useFocusedValidationIssue({
    workflowId, projectId: selectedProjectId, revision: serverRevision,
    graph: canvasGraphQuery.data, isSuccess: canvasGraphQuery.isSuccess, isFetching: canvasGraphQuery.isFetching,
  });
  const noticeScopeKey = JSON.stringify([workflowId, selectedProjectId]);
  const [noticeScope, setNoticeScope] = useState({ key: noticeScopeKey });
  if (noticeScope.key !== noticeScopeKey) setNoticeScope({ key: noticeScopeKey });
  const committedNoticeScope = useRef<typeof noticeScope | null>(null);
  useLayoutEffect(() => {
    committedNoticeScope.current = noticeScope;
    return () => { if (committedNoticeScope.current === noticeScope) committedNoticeScope.current = null; };
  }, [noticeScope]);
  const setStagedStepSaveNotice = useCallback((message: string, owner: StagedStepNoticeOwner) => {
    if (committedNoticeScope.current !== noticeScope || !owner.isCurrent()) return;
    setSaveNoticeState({ message, isError: false, stagedStepOwner: {
      isCurrent: () => committedNoticeScope.current === noticeScope && owner.isCurrent(),
    } });
  }, [noticeScope]);
  const header = useWorkflowHeaderActionsController({
    canvasGraphQuery,
    dagQuery: data.dagQuery,
    detailQuery,
    navigate,
    selectedProjectId,
    surface,
    validationQuery: data.validationQuery,
    workflowContractQuery,
    workflowId,
  });
  const { refetchWorkflowSurfaces } = header;
  const changeSet = useWorkflowChangeSet({
    refetchWorkflowSurfaces: () => refetchWorkflowSurfaces(),
    serverDefinition,
    serverRevision,
    workflowId,
  });
  // The author's edit session in this browser tab: history and local copy survive Apply and reload.
  const sessionUserId = String(session?.user?.id || "");
  const sessionAccountId = String(session?.user?.account_id || "");
  const editSessionIdentity = useMemo(
    () => (sessionUserId && workflowId ? { accountId: sessionAccountId, userId: sessionUserId, workflowId } : null),
    [sessionAccountId, sessionUserId, workflowId],
  );
  const editSession = useWorkflowEditSession({
    enabled: Boolean(editSessionIdentity),
    identity: editSessionIdentity,
    workflow: workflowRecord,
    serverRevision,
    changeSet,
  });
  const {
    definitionRows: authorDefinitionRows,
    publishedBlocks,
    selectedGroup: authorSelectedGroup,
    selectedStep: authorSelectedStep,
    validationErrorGroups,
    validationWarningGroups,
  } = useWorkflowDefinitionRows({
    operations: changeSet.operations,
    preview: changeSet.preview,
    selectedGroupId,
    selectedStepId,
    validationErrors,
    validationWarnings,
  });
  // B14: a step drawn inside a hook frame belongs to the hook's connected
  // workflow. While it (or a new step in the frame) is in the step drawer, the
  // step-level controllers below work on that workflow: its id, definition,
  // revision and change set. Everything else stays the author's.
  const openNewStepDrawerRef = useRef<((context?: Record<string, unknown>) => unknown) | null>(null);
  const openGroupDrawerRef = useRef<((groupId: string, tab?: string) => unknown) | null>(null);
  const openStepDrawerRef = useRef<((stepId: string) => unknown) | null>(null);
  const authorStructureLocked = data.exportPolicyQuery.data?.structure_change_allowed === false || objectOrEmpty(workflowRecord.portability).structure_change_allowed === false;
  const allowHookDeclaration = developmentMode && isAdmin && !authorStructureLocked;
  const hookProjectHint = searchParams.has("project_id")
    ? (searchParams.get("project_id") || "").trim()
    : firstStringFromUnknownValue([workflowRecord.project_id, objectOrEmpty(workflowRecord.project).id]);
  const hookProjectContext = useWorkflowHookProjectContext({
    workflowId, projectHint: hookProjectHint,
    enabled: surface === "builder" && isAdmin && detailQuery.isSuccess,
  });
  const hookProjectId = hookProjectContext.projectId;
  const hookScope = useWorkflowHookScope({
    authorChangeSet: changeSet,
    enabled: surface === "builder",
    focusCanvasNode,
    openNewStepDrawerRef,
    openStepDrawerRef,
    openGroupDrawerRef,
    selectedGroupId,
    drawerMode,
    setSelectedGroupId,
    groupCreateMode,
    structureLocked: authorStructureLocked,
    projectId: hookProjectId,
    actorKey: hookProjectContext.actorKey,
    authorProjectId: selectedProjectId,
    requestConfirmation,
    selectedStepId,
    setAutomationScopeKind,
    setDrawerMode,
    setDrawerTab,
    setSaveError,
    setSaveNotice,
    setSearchParams,
    setSelectedCanvasIssue,
    setSelectedStepId,
    stepCreateContext,
    workflow,
    workflowId,
  });
  const inHook = (hookScope.scopeActive || hookScope.scopePending) && (drawerMode === "step" || drawerMode === "group");
  const stepWorkflowId = inHook ? hookScope.childWorkflowId : workflowId;
  const stepChangeSet = inHook ? hookScope.childChangeSet : changeSet;
  const stepServerRevision = inHook ? hookScope.childRevision : serverRevision;
  const stepWorkflow = inHook ? hookScope.childWorkflow : workflow;
  const stepDefinitionRows = inHook ? hookScope.childRows.definitionRows : authorDefinitionRows;
  const stepCreateContextInScope = inHook ? hookScope.scopedCreateContext : stepCreateContext;
  const refetchStepSurfaces = inHook ? hookScope.refetchChild : refetchWorkflowSurfaces;
  const selectedStep = inHook ? hookScope.childRows.selectedStep : authorSelectedStep;
  const selectedGroup = inHook ? hookScope.childRows.selectedGroup : authorSelectedGroup;
  // Run after the change-set layout effects publish their committed journal.
  // Compare the notice object so retirement cannot erase newer feedback.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- Reconcile every committed journal; unchanged notice identity can still retire on Apply/Discard.
  useLayoutEffect(() => {
    if (saveNoticeState.stagedStepOwner && !saveNoticeState.stagedStepOwner.isCurrent()) {
      setSaveNoticeState((current) => current === saveNoticeState ? { message: "", isError: false } : current);
    }
  });
  const deletionGuard = useWorkflowDeletionGuard({
    changeSet: stepChangeSet,
    owner: {
      workflowId: stepWorkflowId,
      projectId: inHook ? hookProjectId : selectedProjectId,
      ownerKey: JSON.stringify([workflowId, inHook ? hookProjectId : selectedProjectId, inHook ? hookScope.authorityEpoch : null, stepWorkflowId, inHook, hookScope.effectiveHookId,
        drawerMode, drawerMode === "step" ? selectedStep?.runtimeId : selectedGroup?.groupId]),
      selectionId: drawerMode === "step" ? selectedStep?.runtimeId || "" : selectedGroup?.groupId || "",
      serverRevision: stepServerRevision,
      structureLocked: authorStructureLocked && !inHook,
    },
    openProtectedScope: async (scope) => {
      if (scope.kind === "group") {
        await getDrawer().openGroupDrawer(inHook ? hookMemberId(hookScope.effectiveHookId, scope.id) : scope.id, "automation");
        return true;
      }
      const opened = await getDrawer().openStepDrawer(inHook ? hookMemberId(hookScope.effectiveHookId, scope.id) : scope.id);
      if (opened) setDrawerTab("automation");
      return opened;
    },
  });
  const currentDeleteEditorRef = useRef<{ mode: string; stepId: string; groupId: string } | null>(null);
  useLayoutEffect(() => {
    currentDeleteEditorRef.current = { mode: drawerMode, stepId: selectedStepId, groupId: selectedGroupId };
    return () => { currentDeleteEditorRef.current = null; };
  }, [drawerMode, selectedStepId, selectedGroupId]);
  const graphDeleteReaderRef = useRef<(selection: GraphDeleteSelection, canvasSelection: CanvasCommandSelection) => GraphDeleteAuthority | null>(() => null);
  const pasteRootReaderRef = useRef<(generation: number) => DeleteGuardSnapshot | null>(() => null);
  const graphDeleteBatchReaderRef = useRef<(selection: readonly GraphDeleteSelection[], canvasSelection: CanvasCommandSelection) => GraphDeleteBatchAuthority | null>(() => null);
  useLayoutEffect(() => {
    const readSelected = (selection: GraphDeleteSelection, canvasSelection: CanvasCommandSelection, batch: boolean): GraphDeleteAuthority | null => {
      if ((!batch && canvasSelection.ids.length !== 1) || !canvasSelection.ids.includes(selection.canvasId) || selection.canvasId.startsWith("visual:")) return null;
      const kind = selection.canvasId.startsWith("group:") ? "group" : selection.canvasId.startsWith("for_each:") ? "for_each" : "step";
      if (selection.kind !== kind) return null;
      const member = parseHookNodeId(selection.canvasId);
      if (member && !member.memberStepId) return null; // A visual Hook frame is never a child Group.
      const child = Boolean(member);
      if (child && (!hookProjectId || member?.hookId !== hookScope.effectiveHookId || !hookScope.childWorkflowId ||
        hookScope.childWorkflow?.workflow_id !== hookScope.childWorkflowId || !hookScope.childRevision ||
        ![...hookScope.frames.values()].some((frame) => frame.hookId === member.hookId && frame.targetWorkflowId === hookScope.childWorkflowId))) return null;
      if (!child && authorStructureLocked) return null;
      const id = member?.memberStepId || selection.canvasId.replace(/^(group|for_each):/, "");
      const commandChangeSet = child ? hookScope.childChangeSet : changeSet;
      const preview = commandChangeSet.preview as WorkflowDefinitionModel;
      if (!id || (kind === "step" ? !preview.steps.some((row) => row.step_id === id)
        : kind === "group" ? !preview.groups.some((row) => row.group_id === id)
          : !preview.forEach.some((row) => row.for_each_id === id))) return null;
      const commandWorkflowId = child ? hookScope.childWorkflowId : workflowId;
      const commandRevision = child ? hookScope.childRevision : serverRevision;
      const localStep = kind === "step" && Boolean((child ? hookScope.childRows.definitionRows : authorDefinitionRows).stepRowsById.get(id)?.local);
      return {
        changeSet: commandChangeSet,
        target: { kind, id }, canvasId: selection.canvasId, generation: canvasSelection.generation, localStep,
        owner: {
          workflowId: commandWorkflowId, projectId: child ? hookProjectId : selectedProjectId, selectionId: batch ? JSON.stringify(canvasSelection.ids) : id,
          serverRevision: commandRevision, structureLocked: !child && authorStructureLocked,
          ownerKey: JSON.stringify([workflowId, child ? hookProjectId : selectedProjectId, child ? hookScope.authorityEpoch : null, commandWorkflowId, child ? member?.hookId : "author", batch ? canvasSelection.ids : selection.canvasId, canvasSelection.generation]),
        },
      };
    };
    // A paste needs no selection: it joins the author's root, never a protected author structure.
    pasteRootReaderRef.current = (generation) => authorStructureLocked ? null : {
      changeSet,
      owner: {
        workflowId, projectId: selectedProjectId, selectionId: "", serverRevision, structureLocked: false,
        ownerKey: JSON.stringify([workflowId, selectedProjectId, workflowId, "author", "paste", generation]),
      },
    };
    graphDeleteReaderRef.current = (selection, canvasSelection) => readSelected(selection, canvasSelection, false);
    graphDeleteBatchReaderRef.current = (selection, canvasSelection) => {
      if (selection.length < 2 || selection.length !== canvasSelection.ids.length ||
        !selection.every((item, index) => item.canvasId === canvasSelection.ids[index] &&
          (index === 0 || canvasSelection.ids[index - 1] < item.canvasId))) return null;
      const rows = selection.map((item) => readSelected(item, canvasSelection, true));
      const first = rows[0];
      if (!first || rows.some((row) => row?.changeSet !== first.changeSet || row.owner.ownerKey !== first.owner.ownerKey ||
        row.owner.workflowId !== first.owner.workflowId || row.owner.projectId !== first.owner.projectId ||
        row.owner.serverRevision !== first.owner.serverRevision)) return null;
      const firstHook = parseHookNodeId(first.canvasId);
      return {
        changeSet: first.changeSet, owner: first.owner,
        targets: rows.map((row) => row!.target), canvasIds: [...canvasSelection.ids], generation: canvasSelection.generation,
        ownerScopeKey: JSON.stringify([workflowId, first.owner.projectId, firstHook ? hookScope.authorityEpoch : null, first.owner.workflowId, firstHook?.hookId || "author"]),
      };
    };
    return () => { graphDeleteReaderRef.current = () => null; graphDeleteBatchReaderRef.current = () => null; pasteRootReaderRef.current = () => null; };
  }, [authorDefinitionRows, authorStructureLocked, changeSet, hookProjectId, hookScope, selectedProjectId, serverRevision, workflowId]);
  const readClipboardAuthority = useCallback((snapshot: CanvasClipboardSnapshot, command: ClipboardCommand): ClipboardAuthority | null => {
    const ids = [...snapshot.ids];
    if (!ids.length) {
      const root = command === "paste" && snapshot.pasteOrigin ? pasteRootReaderRef.current(snapshot.generation) : null;
      const rootRevision = root ? root.changeSet.baseRevision ?? root.owner.serverRevision : null;
      if (!root?.owner.workflowId || root.changeSet.workflowId !== root.owner.workflowId || !Number.isInteger(rootRevision) ||
        Number(rootRevision) <= 0 || root.changeSet.status === "applying" || root.changeSet.status === "conflict") return null;
      return {
        changeSet: root.changeSet, owner: root.owner, canvasWorkflowId: workflowId, viewKey: "author", generation: snapshot.generation,
        authoringAllowed: true, targets: [], worldPositions: new Map(), placement: ROOT_PLACEMENT,
        pasteOrigin: snapshot.pasteOrigin!, pasteMode: snapshot.pasteMode ?? "offset", duplicateOrigin: snapshot.duplicateOrigin,
      };
    }
    if (ids.some((id, index) => index > 0 && ids[index - 1] >= id)) return null;
    const canvasSelection: CanvasCommandSelection = { ids, generation: snapshot.generation };
    const selection: GraphDeleteSelection[] = ids.map((canvasId) => ({
      kind: canvasId.startsWith("group:") ? "group" : canvasId.startsWith("for_each:") ? "for_each" : "step", canvasId,
    }));
    const single = ids.length === 1 ? graphDeleteReaderRef.current(selection[0], canvasSelection) : null;
    const batch = ids.length > 1 ? graphDeleteBatchReaderRef.current(selection, canvasSelection) : null;
    if (!single && !batch) return null;
    const targets = single ? [{ canvasId: single.canvasId, ref: single.target }] : batch!.targets.map((ref, index) => ({ canvasId: ids[index], ref }));
    const owner = single?.owner || batch!.owner;
    const commandChangeSet = single?.changeSet || batch!.changeSet;
    const revision = commandChangeSet.baseRevision ?? owner.serverRevision;
    if (!owner.workflowId || !owner.ownerKey || owner.structureLocked || commandChangeSet.workflowId !== owner.workflowId ||
      !Number.isInteger(revision) || Number(revision) <= 0 || commandChangeSet.status === "applying" || commandChangeSet.status === "conflict") return null;
    const preview = commandChangeSet.preview as WorkflowDefinitionModel;
    const placementOf = (ref: typeof targets[number]["ref"]): WorkflowPlacement | null => {
      if (ref.kind === "step") {
        const row = preview.steps.find((item) => item.step_id === ref.id);
        return row ? stepPlacement(row) : null;
      }
      if (ref.kind === "for_each") {
        const row = preview.forEach.find((item) => item.for_each_id === ref.id);
        return row ? { ...ROOT_PLACEMENT, groupId: row.group_id || "" } : null;
      }
      return preview.groups.some((item) => item.group_id === ref.id) ? ROOT_PLACEMENT : null;
    };
    // Copy encodes a detached fragment and has no destination. Duplicate places
    // only canonical roots; a selected descendant is carried by its container.
    const destinationRoots = command === "duplicate"
      ? deriveDeletionBatch(preview, targets.map(({ ref }) => ref))?.roots
      : undefined;
    if (command === "duplicate" && !destinationRoots?.length) return null;
    const placementTargets = destinationRoots || [targets[0].ref];
    const placement = placementOf(placementTargets[0]);
    if (!placement || (command === "duplicate" && placementTargets.some((ref) => {
      const other = placementOf(ref);
      return other?.groupId !== placement.groupId || other.forEachId !== placement.forEachId || other.laneId !== placement.laneId;
    }))) return null;
    const viewKey = parseHookNodeId(ids[0])?.hookId || "author";
    const worldPositions = new Map<string, { x: number; y: number }>();
    for (const [canvasId, point] of snapshot.worldPositions) {
      const parsed = parseHookNodeId(canvasId);
      if (parsed && !parsed.memberStepId) continue;
      if (viewKey === "author" ? Boolean(parsed) : parsed?.hookId !== viewKey) continue;
      const raw = parsed?.memberStepId || canvasId.replace(/^(group|for_each):/, "");
      if (raw && Number.isFinite(point.x) && Number.isFinite(point.y)) worldPositions.set(raw, point);
    }
    if (targets.some(({ ref }) => !worldPositions.has(ref.id))) return null;
    return {
      changeSet: commandChangeSet, owner,
      canvasWorkflowId: workflowId, viewKey, generation: snapshot.generation,
      authoringAllowed: true, targets, worldPositions, placement,
      pasteOrigin: { x: 0, y: 0 }, pasteMode: "offset", duplicateOrigin: snapshot.duplicateOrigin,
    };
  }, [workflowId]);
  const requestGraphDeleteFromCanvas: RequestGraphDelete = (selection, source, currentCanvasSelection) =>
    selection.length > 1 ? requestGraphDeleteBatch(selection, {
      readAuthority: (items) => graphDeleteBatchReaderRef.current(items, currentCanvasSelection()),
      guard: deletionGuard,
      openProtectedScope: async (authority, scope) => {
        const member = parseHookNodeId(authority.canvasIds[0]);
        const visualId = member ? hookMemberId(member.hookId, scope.id) : scope.id;
        if (scope.kind === "group") {
          await getDrawer().openGroupDrawer(visualId, "automation");
          return true;
        }
        const opened = await getDrawer().openStepDrawer(visualId);
        if (opened) setDrawerTab("automation");
        return opened;
      },
      onAccepted: (authority, batch) => {
        const deletedMember = parseHookNodeId(authority.canvasIds[0]);
        const sameSelectedOwner = (selectedId: string) => {
          const selectedMember = parseHookNodeId(selectedId);
          return deletedMember ? selectedMember?.hookId === deletedMember.hookId : !selectedMember;
        };
        const currentEditor = currentDeleteEditorRef.current;
        const selectedStepRawId = parseHookNodeId(currentEditor?.stepId || "")?.memberStepId || currentEditor?.stepId || "";
        const selectedGroupRawId = parseHookNodeId(currentEditor?.groupId || "")?.memberStepId || currentEditor?.groupId || "";
        if (currentEditor?.mode === "step" && currentEditor.stepId && sameSelectedOwner(currentEditor.stepId) && batch.closure.steps.includes(selectedStepRawId)) {
          setSelectedStepId(""); setDrawerMode("");
        }
        if (currentEditor?.mode === "group" && currentEditor.groupId && sameSelectedOwner(currentEditor.groupId) &&
          (batch.closure.groups.includes(selectedGroupRawId) || batch.closure.forEach.includes(selectedGroupRawId))) {
          setSelectedGroupId(""); setDrawerMode("");
        }
        setSaveNotice(t("react.workflows.selected_delete_staged", "Selected items deletion added to the unapplied structure."));
      },
      t,
    }) : requestGraphDelete(selection, source, {
      readAuthority: (item) => graphDeleteReaderRef.current(item, currentCanvasSelection()),
      guard: deletionGuard,
      openProtectedScope: async (authority, scope) => {
        const member = parseHookNodeId(authority.canvasId);
        const visualId = member ? hookMemberId(member.hookId, scope.id) : scope.id;
        if (scope.kind === "group") {
          await getDrawer().openGroupDrawer(visualId, "automation");
          return true;
        }
        const opened = await getDrawer().openStepDrawer(visualId);
        if (opened) setDrawerTab("automation");
        return opened;
      },
      onAccepted: (_item, authority, closure) => {
        const deletedMember = parseHookNodeId(authority.canvasId);
        const sameSelectedOwner = (selectedId: string) => {
          const selectedMember = parseHookNodeId(selectedId);
          return deletedMember ? selectedMember?.hookId === deletedMember.hookId : !selectedMember;
        };
        const currentEditor = currentDeleteEditorRef.current;
        const selectedStepRawId = parseHookNodeId(currentEditor?.stepId || "")?.memberStepId || currentEditor?.stepId || "";
        const selectedGroupRawId = parseHookNodeId(currentEditor?.groupId || "")?.memberStepId || currentEditor?.groupId || "";
        if (currentEditor?.mode === "step" && currentEditor.stepId && sameSelectedOwner(currentEditor.stepId) && closure.steps.includes(selectedStepRawId)) {
          setSelectedStepId("");
          setDrawerMode("");
        }
        if (currentEditor?.mode === "group" && currentEditor.groupId && sameSelectedOwner(currentEditor.groupId) &&
          (closure.groups.includes(selectedGroupRawId) || closure.forEach.includes(selectedGroupRawId))) {
          setSelectedGroupId("");
          setDrawerMode("");
        }
        const notice = authority.target.kind === "step"
          ? t("react.workflows.step_delete_staged", "Step deletion added to the unapplied structure.")
          : authority.target.kind === "for_each"
            ? t("react.workflows.for_each_delete_staged", "For each deletion added to the unapplied structure.")
            : t("react.workflows.group_delete_staged", "Group deletion added to the unapplied structure.");
        setSaveNotice(notice);
      },
      t,
    });
  // The drawer finds every step it may open: the author's and the active hook's, in canvas ids.
  const definitionRows = useMemo(() => {
    if (!hookScope.childWorkflowId) return authorDefinitionRows;
    const members = new Map(authorDefinitionRows.stepRowsById);
    hookScope.childRows.definitionRows.stepRowsById.forEach((row, stepId) => {
      members.set(hookMemberId(hookScope.effectiveHookId, stepId), row);
    });
    return {
      ...authorDefinitionRows, stepRowsById: members,
      groupRows: [
        ...authorDefinitionRows.groupRows,
        ...hookScope.childRows.definitionRows.groupRows.map((row) => ({ ...row, groupId: hookMemberId(hookScope.effectiveHookId, row.groupId) })),
      ],
    };
  }, [authorDefinitionRows, hookScope.childRows.definitionRows, hookScope.childWorkflowId, hookScope.effectiveHookId]);
  if (detailQuery.isSuccess && selectedStepId && !selectedStep && !stepCreateContext && !hookScope.scopePending && !parseHookNodeId(selectedStepId)) {
    setSelectedStepId("");
    if (drawerMode === "step") setDrawerMode("");
  } else if (drawerMode === "step" && !selectedStepId && !stepCreateContext) {
    // The step editor shows either a step or a new step's form. Once its step is
    // deleted (or dropped from the unapplied set) neither remains, so it closes
    // instead of turning into a placement-less "New step" form that cannot be added.
    setDrawerMode("");
  }
  const displayCanvasGraph = useMemo(
    () =>
      canvasGraphQuery.data
        ? canvasGraphWithDefinition(canvasGraphQuery.data, changeSet.preview, t)
        : null,
    [canvasGraphQuery.data, changeSet.preview, t],
  );
  const executionCanvasGraph = useMemo(
    () => displayCanvasGraph ? withHookFrames(displayCanvasGraph, hookScope.frames) : undefined,
    [displayCanvasGraph, hookScope.frames],
  );
  const entry = changeSet.preview.graph.entry;
  const entryKey = entry ? `${entry.kind}:${entry.id}` : "";
  const handleWorkflowRuntimeLocationChange = useCallback(
    (location: WorkflowRuntimeLocation | null) => {
      setBuilderRuntimeActiveNodeId(
        serverRevision === null
          ? ""
          : workflowCanvasNodeIdForExecutedLocation(executionCanvasGraph, location, workflowId, serverRevision, hookScope.frames),
      );
    },
    [executionCanvasGraph, hookScope.frames, serverRevision, workflowId],
  );
  return {
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
    setGroupDraft, builderCanvasFocus, focusCanvasNode, builderRuntimeActiveNodeId,
    setBuilderRuntimeActiveNodeId, workflowContractSectionsExpanded, setWorkflowContractSectionsExpanded, advancedMode,
    setAdvancedMode, stepAgentLocked, setStepAgentLocked, stepDisplayName,
    setStepDisplayName, stepKindDraft, setStepKindDraft, stepToolCategoryDraft,
    setStepToolCategoryDraft, stepOverrideDraft, setStepOverrideDraft, stepInputErrors,
    onStepInputValidationChange, stepAgentId, setStepAgentId, stepOptional,
    setStepOptional, stepBlockKey, setStepBlockKey, reactionFormVisible,
    setReactionFormVisible, automationScopeKind, setAutomationScopeKind, saveNotice,
    saveNoticeIsError, setSaveNotice, setSaveError, savingWorkflowSurface,
    setSavingWorkflowSurface, builderConfirmDialog, requestConfirmation, data,
    availableProjectsQuery, linkedProjectQuery, selectedProjectId, hookProjectId, hookProjectContext, serverRevision,
    workflow, workflowContractQuery, workflowRecord, selectedCanvasIssue,
    setSelectedCanvasIssue, setStagedStepSaveNotice, header, refetchWorkflowSurfaces,
    changeSet, editSession, authorDefinitionRows, publishedBlocks, validationErrorGroups,
    validationWarningGroups, openNewStepDrawerRef, openGroupDrawerRef, openStepDrawerRef,
    authorStructureLocked, allowHookDeclaration, hookScope, inHook,
    stepWorkflowId, stepChangeSet, stepServerRevision, stepWorkflow,
    stepDefinitionRows, stepCreateContextInScope, refetchStepSurfaces, selectedStep,
    selectedGroup, deletionGuard, readClipboardAuthority, requestGraphDeleteFromCanvas,
    definitionRows, displayCanvasGraph, executionCanvasGraph, entryKey,
    handleWorkflowRuntimeLocationChange,
  } as const;
}
