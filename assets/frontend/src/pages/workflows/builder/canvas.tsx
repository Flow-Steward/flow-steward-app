import {
  nodeRefFromNodeId,
  useCanvasRenderedStepHeights,
  useCanvasInteractiveEdges,
  useCanvasGraphEditing,
  useCanvasSearchNavigation,
  useCanvasConnectionGestures,
  useCanvasClipboardPublication,
  type PendingClipboardCommit,
} from "./canvas-surface-interaction-hooks";
import type { CanvasPlacementDelta, CanvasPlacementValues } from "./canvas-history";
import { CanvasPortProvider } from "./canvas-port-control";
import { canvasConnectionDefinition, connectionPlacement, connectionRefusal } from "./canvas-connection-policy";
import type { CanvasCreationIntent, CanvasPlacementPoint } from "./canvas-types";
import { arrangeSnapshot, useCanvasArrangeMode } from "./canvas-layout-menu";
import { useInlineLoopIds } from "./canvas-inline-loop-layout";
import type { CanvasShellExtras } from "./canvas-context-menus";
import { useI18n } from "@/lib/i18n/useI18n";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  type Node,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  buildCanvasLayout,
} from "@/pages/workflows/builder/layout";
import {
  applyCanvasNodeState,
  buildCanvasSearchResults,
  draftEdgeKeysFromOperations,
  localNodeIdsFromOperations,
  structureStatusView,
} from "@/pages/workflows/builder/canvas-state-helpers";
import { WorkflowCanvasShell } from "@/pages/workflows/builder/canvas-shell";
import { canvasNoteNodes, NOTE_DEFAULT_SIZE, useCanvasNotes, type CanvasNoteDropPoint } from "@/pages/workflows/builder/canvas-notes";
import type { BuilderSurfaceProps, CanvasClipboardSnapshot, CanvasClipboardVisualSelection, CanvasCommandSelection, CanvasPasteTarget, GraphDeleteSelection, NewContainerDefaults } from "@/pages/workflows/builder/canvas-types";
import { createWorkflowClipboardCommands, type ClipboardCommand as CanvasClipboardCommand, type ClipboardCommit } from "@/pages/workflows/detail/detail-clipboard-commands";
import { useCanvasToastFeed, watchGraphIntent } from "./canvas-toast-feed";
import { canvasNodeName } from "./canvas-edge-labels";
import type { WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import {
  canvasPortKey,
  loadManualLayout,
  saveManualLayout,
  suppressCanvasNodeClick,
  type CanvasPosition,
  type GraphEditNotice,
} from "@/pages/workflows/builder/canvas-core";
import { canvasMotionDuration, fitCanvasToNodes } from "./canvas-viewport";
import type { WorkflowNodeRef, WorkflowOutcome } from "@/pages/workflows/shared/api";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { hookGroupId, isHookMemberRef, parseHookNodeId, withHookFrames } from "@/pages/workflows/builder/hook-frames";

function placementDelta(
  canvasWorkflowId: string,
  subjectIds: string[],
  before: Record<string, CanvasPosition>,
  after: Record<string, CanvasPosition>,
): CanvasPlacementDelta | null {
  const earlier: CanvasPlacementValues = {};
  const later: CanvasPlacementValues = {};
  for (const id of new Set([...Object.keys(before), ...Object.keys(after)])) {
    const oldPosition = before[id] ?? null;
    const newPosition = after[id] ?? null;
    if (oldPosition?.x === newPosition?.x && oldPosition?.y === newPosition?.y) continue;
    earlier[id] = oldPosition;
    later[id] = newPosition;
  }
  const canonicalStepSubjectIds = subjectIds.flatMap((id) => {
    if (id.startsWith("group:") || id.startsWith("for_each:")) return [];
    const member = parseHookNodeId(id);
    return member ? (member.memberStepId ? [member.memberStepId] : []) : [id];
  });
  return Object.keys(later).length ? { canvasWorkflowId, subjectIds, canonicalStepSubjectIds, before: earlier, after: later } : null;
}

function replayPlacementValues(current: Record<string, CanvasPosition>, values: CanvasPlacementValues) {
  const next = { ...current };
  for (const [id, position] of Object.entries(values)) {
    if (position === null) delete next[id];
    else next[id] = position;
  }
  return next;
}

export function WorkflowCanvasBuilderSurface({
  advancedMode,
  onCanvasReady,
  availabilityNotice = "",
  missingExtensions = null,
  headerContext,
  backHref,
  canvasGraph: definitionCanvasGraph,
  changeSet,
  resolvePlacementOwner,
  placementHistoryOwners,
  creationSelection,
  creationIntentRetireNonce = 0,
  focusNodeId,
  focusNodeNonce = 0,
  focusNodeCreation = null,
  focusNodePlace = false,
  runtimeActiveNodeId = null,
  exportYamlHref,
  exportYamlLabel,
  onCloseInspector,
  onConnectPort,
  onDisconnectPort,
  onInsertStep,
  onAppendStep,
  onOpenGroup,
  onOpenGroupAutomation,
  onOpenNewGroup,
  onOpenNewStep,
  onOpenStep,
  stepTestRunAllowed,
  onRequestGraphDelete,
  onDeleteSelectionInvalidated,
  deleteGuardNotice = null,
  onRetryGraphDelete,
  onOpenDeleteAutomation,
  onOpenStepAutomation,
  onOpenWorkflowAutomation,
  onOpenWorkflowSettings,
  onOpenWorkflowInterface,
  onOpenWorkflowHistory,
  onCompareVersions,
  onOpenStepPlacement,
  onConvertGroup,
  convertGroupBlockedReason = "",
  inspectorOpen = false,
  inspectorWidth = 0,
  canvasRunBar,
  canvasTestRunAction,
  onToggleAdvancedMode,
  onValidationIssue,
  projectId,
  workflowId,
  commandViewIdentity,
  onReadClipboardAuthority,
  clipboardGuard,
  scheduleProjectHint,
  workflow,
  onAnnotationsSaved,
  onAnnotationsReloaded,
  structureLocked = false,
  hookFrames,
  runtimeMarks,
  runtimeEdges,
  containerRuntimeMarks,
  canvasPlacementCommand,
}: Readonly<BuilderSurfaceProps>) {
  const { t } = useI18n();
  // B14: each root-level hook step is drawn as a frame (a Group) holding its
  // connected workflow's steps; everything below reads the framed graph.
  const canvasGraph = useMemo(
    () => (hookFrames?.size ? withHookFrames(definitionCanvasGraph, hookFrames) : definitionCanvasGraph),
    [definitionCanvasGraph, hookFrames],
  );
  const connectionDefinition = useMemo(() => canvasConnectionDefinition(canvasGraph), [canvasGraph]);
  const [creationIntent, setCreationIntent] = useState<CanvasCreationIntent | null>(null);
  const handledIntentRetireNonceRef = useRef(0);
  useEffect(() => {
    if (!creationIntentRetireNonce || handledIntentRetireNonceRef.current === creationIntentRetireNonce) return;
    handledIntentRetireNonceRef.current = creationIntentRetireNonce;
    setCreationIntent(null);
  }, [creationIntentRetireNonce]);
  const pendingConnectionDropClickRef = useRef<{ x: number; y: number } | null>(null);
  const [connectionPointedTarget, setConnectionPointedTarget] = useState<{ id: string; valid: boolean } | null>(null);
  const [connectingPort, setConnectingPort] = useState<{ source: WorkflowNodeRef; outcome: WorkflowOutcome } | null>(null);
  const connectingRef = useRef<{ source: WorkflowNodeRef; outcome: WorkflowOutcome; clientPoint?: CanvasPosition } | null>(null);
  const runtimeFrame = runtimeActiveNodeId ? hookFrames?.get(runtimeActiveNodeId) : undefined;
  const runtimeNodeId = runtimeFrame ? `group:${hookGroupId(runtimeFrame.hookId)}` : runtimeActiveNodeId;
  const availabilityStatus = firstStringFromUnknownValue([(canvasGraph as Record<string, unknown>).availability_status, ""])
    .trim()
    .toLowerCase();
  const effectiveAvailabilityNotice = availabilityNotice
    || (
      availabilityStatus && availabilityStatus !== "available"
        ? firstStringFromUnknownValue([(canvasGraph as Record<string, unknown>).availability_reason_text, ""]).trim()
        : ""
    );
  const [showFailurePaths, setShowFailurePaths] = useState(true);
  const [showLegend, setShowLegend] = useState(false);
  const [showValidationPanel, setShowValidationPanel] = useState(false);
  const [showMoreMenu, setShowMoreMenu] = useState(false);
  const [showAddMenu, setShowAddMenu] = useState(false);
  const [showMiniMap, setShowMiniMap] = useState(true);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [graphEditNotice, setGraphEditNotice] = useState<GraphEditNotice | null>(null);
  // The step and group editors announce their deletions on this canvas.
  useCanvasToastFeed(setGraphEditNotice);
  const canvasNodesRef = useRef<ReadonlyMap<string, Node>>(new Map());
  const pasteTargetRef = useRef<CanvasPasteTarget | null>(null);
  const [commandClearNonce, setCommandClearNonce] = useState(0);
  const commandViewKey = JSON.stringify([projectId || "", workflowId, commandViewIdentity || ""]);
  const clipboardSnapshotRef = useRef<Omit<CanvasClipboardSnapshot, "generation"> | null>(null);
  const clipboardOwnerReaderRef = useRef(onReadClipboardAuthority);
  const clipboardGuardRef = useRef(clipboardGuard);
  const clipboardViewGenerationRef = useRef(0);
  const clipboardViewportGenerationRef = useRef(0);
  const clipboardManualInteractionsRef = useRef({ serial: 0, subjects: new Map<string, number>() });
  const onUserViewportInteraction = useCallback(() => { clipboardViewportGenerationRef.current += 1; }, []);
  const [clipboardBusy, setClipboardBusy] = useState(false);
  const [clipboardAvailability, setClipboardAvailability] = useState({ copy: false, duplicate: false });
  const [clipboardSelection, setClipboardSelection] = useState<CanvasClipboardVisualSelection | null>(null);
  const [pendingClipboardCommit, setPendingClipboardCommit] = useState<PendingClipboardCommit | null>(null);
  const pendingClipboardCommitRef = useRef<PendingClipboardCommit | null>(null);
  const clipboardCommandSequenceRef = useRef(0);
  const clipboardTranslateRef = useRef(t);
  useLayoutEffect(() => { clipboardOwnerReaderRef.current = onReadClipboardAuthority; clipboardGuardRef.current = clipboardGuard; clipboardTranslateRef.current = t; });
  const commandViewRef = useRef(commandViewKey);
  const lastCommandOwnerRef = useRef<{ viewKey: string; workflowId: string; placementViewKey: string } | null>(null);
  const commandSelectionRef = useRef<CanvasCommandSelection>({ ids: [], generation: 0 });
  /** How the toast names nodes: one by name, else how many (Canvas › Copy and paste). */
  const clipboardSelectionLabel = useCallback((ids: readonly string[]) => (ids.length === 1
    ? canvasNodeName(canvasNodesRef.current.get(ids[0]), ids[0].replace(/^(group|for_each):/, ""))
    : t("react.workflows.items_count", "{count} items", { count: ids.length })), [t]);
  const deletedLabel = useCallback((ids: readonly string[]) => ids.length === 1
    ? t("react.workflows.node_deleted", "{name} deleted.", { name: clipboardSelectionLabel(ids) })
    : t("react.workflows.items_deleted", "{count} items deleted.", { count: ids.length }), [clipboardSelectionLabel, t]);
  const clipboardCommitListenerRef = useRef<(event: ClipboardCommit) => void>(() => undefined);
  useLayoutEffect(() => {
    clipboardCommitListenerRef.current = (event) => {
      if (event.command === "cut") {
        // A cut is a deletion: the selection goes, and Undo brings exactly it back.
        const { changeSet: owner } = event.authority; const token = event.intentToken;
        setCommandClearNonce((current) => current + 1);
        setGraphEditNotice({
          label: t("react.workflows.clipboard_cut", "{name} cut. Paste it anywhere; the deletion joins the unapplied structure.", { name: clipboardSelectionLabel(event.authority.targets.map((target) => target.canvasId)) }),
          tone: "success",
          action: typeof owner.undoGraphIntentIfLatest === "function" ? { label: t("react.workflows.undo", "Undo"), onAction: () => { owner.undoGraphIntentIfLatest(token); } } : undefined,
        });
      } else if (event.command === "paste") {
        const rootCount = event.roots.length;
        const rawName = String(event.rootLabels[0] || event.roots[0]?.id || "").trim();
        const nameCharacters = Array.from(rawName);
        const name = nameCharacters.length > 80 ? `${nameCharacters.slice(0, 79).join("")}…` : nameCharacters.join("");
        const owner = event.authority.changeSet;
        setGraphEditNotice({
          label: rootCount === 1
            ? t("react.workflows.clipboard_pasted_named", "Pasted {name} to the unapplied structure.", { name })
            : t("react.workflows.clipboard_pasted_count", "{count} items pasted to the unapplied structure.", { count: rootCount }),
          tone: "success",
          action: typeof owner.undoGraphIntentIfLatest === "function"
            ? { label: t("react.workflows.undo", "Undo"), onAction: () => { owner.undoGraphIntentIfLatest(event.intentToken); } }
            : undefined,
        });
      }
    };
  });
  const readClipboardCurrent = useCallback((command: CanvasClipboardCommand) => {
    const snapshot = clipboardSnapshotRef.current;
    const selection = commandSelectionRef.current;
    if (command === "paste") {
      // A paste needs no selection: it lands in the workflow root, at the pointer or one grid step on.
      const target = pasteTargetRef.current;
      return target ? clipboardOwnerReaderRef.current?.({ ids: [], worldPositions: new Map(), duplicateOrigin: target.origin,
        pasteOrigin: target.origin, pasteMode: target.mode, generation: selection.generation }, command) ?? null : null;
    }
    if (snapshot?.ids.length !== selection.ids.length ||
      snapshot.ids.some((id, index) => id !== selection.ids[index])) return null;
    return clipboardOwnerReaderRef.current?.({ ...snapshot, generation: selection.generation }, command) ?? null;
  }, []);
  const refreshClipboardAvailability = useCallback(() => {
    const copy = Boolean(readClipboardCurrent("copy"));
    const duplicate = Boolean(readClipboardCurrent("duplicate"));
    setClipboardAvailability((current) => current.copy === copy && current.duplicate === duplicate ? current : { copy, duplicate });
  }, [readClipboardCurrent]);
  const [clipboardCommands] = useState(() => createWorkflowClipboardCommands({
    readCurrent: (command) => readClipboardCurrent(command),
    clipboard: typeof navigator === "undefined" ? null : {
      readText: () => navigator.clipboard.readText(), writeText: (value) => navigator.clipboard.writeText(value),
    },
    guard: {
      requestDelete: (...args) => clipboardGuardRef.current?.requestDelete(...args) ?? Promise.resolve(false),
      requestDeleteBatch: (...args) => clipboardGuardRef.current?.requestDeleteBatch?.(...args) ?? Promise.resolve(false),
    },
    openProtectedScope: async () => false, // A protected cut says why in the delete notice; Open Automation lives there.
    onCommitted: (event) => {
      clipboardCommitListenerRef.current(event);
      if (event.command === "duplicate" || event.command === "paste") {
        const pending = { event, viewKey: commandViewRef.current, generation: clipboardViewGenerationRef.current, viewportGeneration: clipboardViewportGenerationRef.current, manualGeneration: clipboardManualInteractionsRef.current.serial };
        pendingClipboardCommitRef.current = pending;
        setPendingClipboardCommit(pending);
      }
      return undefined;
    },
    copyLabel: (label, ordinal) => clipboardTranslateRef.current("react.workflows.clipboard_copy_label", "{label} copy", { label }) + (ordinal > 1 ? ` ${ordinal}` : ""),
  }));
  const onClipboardSnapshotChange = useCallback((snapshot: Omit<CanvasClipboardSnapshot, "generation"> | null) => {
    clipboardSnapshotRef.current = snapshot;
    refreshClipboardAvailability();
  }, [refreshClipboardAvailability]);
  useEffect(() => { refreshClipboardAvailability(); }, [changeSet, commandViewKey, onReadClipboardAuthority, refreshClipboardAvailability]);
  const requestClipboardCommand = useCallback(async (command: CanvasClipboardCommand, pasteTarget?: CanvasPasteTarget | null) => {
    if (command === "paste") pasteTargetRef.current = pasteTarget ?? null;
    if (!clipboardOwnerReaderRef.current || pendingClipboardCommitRef.current || !readClipboardCurrent(command)) return;
    const token = ++clipboardCommandSequenceRef.current;
    const viewKey = commandViewRef.current;
    const viewGeneration = clipboardViewGenerationRef.current;
    const named = clipboardSelectionLabel(commandSelectionRef.current.ids);
    setClipboardBusy(true);
    const result = await clipboardCommands[command]();
    if (token !== clipboardCommandSequenceRef.current || viewKey !== commandViewRef.current || viewGeneration !== clipboardViewGenerationRef.current) return;
    if (!pendingClipboardCommitRef.current) setClipboardBusy(false);
    if (result.ok) {
      if (command === "copy") setGraphEditNotice({ label: t("react.workflows.clipboard_copied", "{name} copied.", { name: named }), tone: "success" });
      return;
    }
    if (command === "paste" && (result.reason === "unsupported_format" || result.reason === "invalid_fragment")) {
      setGraphEditNotice({ label: t("react.workflows.clipboard_nothing_to_paste", "Nothing to paste: copy steps on a workflow canvas first."), tone: "success" });
      return;
    }
    const title = result.reason === "clipboard_failed"
      ? command === "paste"
        ? t("react.workflows.clipboard_read_failed", "Could not read from clipboard.")
        : t("react.workflows.clipboard_write_failed", "Could not copy to clipboard.")
      : result.reason === "visual_commit_failed"
        ? t("react.workflows.clipboard_visual_failed", "The copy was added, but its canvas placement needs attention.")
        : result.reason === "busy"
          ? t("react.workflows.clipboard_wait", "Wait for the structure to update before copying again.")
          : t("react.workflows.clipboard_unavailable", "This selection cannot be copied here.");
    setGraphEditNotice({ label: title, tone: result.reason === "busy" ? "success" : "error" });
  }, [clipboardCommands, clipboardSelectionLabel, readClipboardCurrent, t]);
  useLayoutEffect(() => {
    if (commandViewRef.current === commandViewKey) return;
    commandViewRef.current = commandViewKey;
    clipboardViewGenerationRef.current += 1;
    clipboardCommands.invalidate();
    clipboardSnapshotRef.current = null;
    pendingClipboardCommitRef.current = null;
    setPendingClipboardCommit(null);
    setClipboardAvailability({ copy: false, duplicate: false });
    setClipboardBusy(false);
    lastCommandOwnerRef.current = null;
    commandSelectionRef.current = { ids: [], generation: commandSelectionRef.current.generation + 1 };
    onDeleteSelectionInvalidated?.();
  }, [clipboardCommands, commandViewKey, onDeleteSelectionInvalidated]);
  const currentCanvasSelection = useCallback(() => commandSelectionRef.current, []);
  const onCommandSelectionChange = useCallback((ids: readonly string[]) => {
    const previous = commandSelectionRef.current.ids;
    if (previous.length === ids.length && previous.every((id, index) => id === ids[index])) return;
    if (ids.length) lastCommandOwnerRef.current = null;
    clipboardCommands.invalidate();
    clipboardCommandSequenceRef.current += 1;
    setClipboardBusy(Boolean(pendingClipboardCommitRef.current));
    setClipboardAvailability({ copy: false, duplicate: false });
    commandSelectionRef.current = { ids: [...ids], generation: commandSelectionRef.current.generation + 1 };
    onDeleteSelectionInvalidated?.();
  }, [clipboardCommands, onDeleteSelectionInvalidated]);
  const requestCanvasDelete = useCallback(async (ids: readonly string[], source: "step-more" | "canvas-key") => {
    const selection: GraphDeleteSelection[] = ids.map((canvasId) => ({ kind: nodeRefFromNodeId(canvasId).kind, canvasId }));
    if (!onRequestGraphDelete) return false;
    const commandOwner = resolvePlacementOwner ? resolvePlacementOwner(ids) : changeSet;
    const requestViewKey = commandViewKey;
    // Delete acts at once; the toast names what went and offers Undo of exactly that change.
    const label = deletedLabel(ids);
    const undo = watchGraphIntent(commandOwner);
    const accepted = await onRequestGraphDelete(selection, source, currentCanvasSelection);
    if (accepted && commandViewRef.current === requestViewKey) {
      if (commandOwner) lastCommandOwnerRef.current = {
        viewKey: requestViewKey, workflowId: commandOwner.workflowId,
        placementViewKey: commandOwner.placementViewKey,
      };
      setCommandClearNonce((current) => current + 1);
      setGraphEditNotice({ label, tone: "success", action: undo(t("react.workflows.undo", "Undo")) });
    }
    return accepted;
  }, [changeSet, commandViewKey, currentCanvasSelection, deletedLabel, onRequestGraphDelete, resolvePlacementOwner, t]);
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null);
  const [highlightedNodeId, setHighlightedNodeId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [showSearchResults, setShowSearchResults] = useState(false);
  const [searchIndex, setSearchIndex] = useState(0);
  const [connectionHelpActive, setConnectionHelpActive] = useState(false);
  const connectCompletedRef = useRef(false);
  const [reactFlowInstance, setReactFlowInstance] = useState<ReactFlowInstance | null>(null);
  useEffect(() => { onCanvasReady?.(reactFlowInstance); }, [onCanvasReady, reactFlowInstance]);
  const afterNotesSaved = useCallback(async (revision: number) => {
    await onAnnotationsSaved?.(revision);
  }, [onAnnotationsSaved]);
  const annotations = useCanvasNotes({
    workflowId, revision: workflow?.revision, settings: workflow?.settings,
    onSaved: afterNotesSaved, disabled: changeSet.status === "applying" || changeSet.status === "conflict",
    onReload: onAnnotationsReloaded, notify: setGraphEditNotice,
  });
  const { onNodesChange: changeAnnotationNodes, save: saveAnnotation, notes: annotationNotes, draft: annotationDraft, selectedId: selectedAnnotationId, busy: annotationsBusy, disabled: annotationsDisabled, nodeActions: annotationActions, add: addAnnotation, removeFromSelection: removeAnnotationsFromSelection } = annotations;
  const annotationNodes = useMemo(() => canvasNoteNodes({
    notes: annotationNotes, draft: annotationDraft, selectedId: selectedAnnotationId, busy: annotationsBusy, disabled: annotationsDisabled, nodeActions: annotationActions,
  }, workflowId), [annotationNotes, annotationDraft, selectedAnnotationId, annotationsBusy, annotationsDisabled, annotationActions, workflowId]);
  const handledFocusNonceRef = useRef(0);
  const [manualState, setManualState] = useState<{ owner: string; positions: Record<string, CanvasPosition> }>(
    () => ({ owner: workflowId, positions: loadManualLayout(workflowId) }),
  );
  const manualPositions = manualState.owner === workflowId ? manualState.positions : loadManualLayout(workflowId);
  const setManualPositions = useCallback((next: Record<string, CanvasPosition> | ((current: Record<string, CanvasPosition>) => Record<string, CanvasPosition>)) => {
    setManualState((current) => {
      const positions = current.owner === workflowId ? current.positions : loadManualLayout(workflowId);
      return { owner: workflowId, positions: typeof next === "function" ? next(positions) : next };
    });
  }, [workflowId]);
  const placementGestureGenerationRef = useRef(new Map<string, number>());
  const placementGestureGenerationFor = useCallback((id: string) => placementGestureGenerationRef.current.get(id) ?? 0, []);
  const placementGestureRef = useRef<{
    canvasWorkflowId: string;
    ownerWorkflowId: string;
    subjectIds: string[];
    before: Record<string, CanvasPosition>;
    dragging: boolean;
    complete: boolean;
  } | null>(null);
  const handledPlacementReplayRef = useRef(new Map<string, number>());
  const [measuredStepHeights, setMeasuredStepHeights] = useState<Record<string, number>>({});
  const canvasMeasureRootRef = useRef<HTMLDivElement | null>(null);
  // A dropped Note lands centred under the pointer; a click, N or the menu puts it in the middle of the view.
  const addNoteAt = useCallback((point?: CanvasNoteDropPoint) => {
    const bounds = canvasMeasureRootRef.current?.getBoundingClientRect();
    const screen = point ? { x: point.clientX, y: point.clientY } : bounds && { x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 };
    const center = screen && reactFlowInstance ? reactFlowInstance.screenToFlowPosition(screen) : { x: 120 + NOTE_DEFAULT_SIZE.width / 2, y: 120 + NOTE_DEFAULT_SIZE.height / 2 };
    addAnnotation({ x: center.x - NOTE_DEFAULT_SIZE.width / 2, y: center.y - NOTE_DEFAULT_SIZE.height / 2 });
  }, [addAnnotation, reactFlowInstance]);
  // Notes in a canvas delete go at once with Undo; steps and groups take the graph delete.
  const requestCanvasDeleteWithNotes = useCallback(async (ids: readonly string[], source: "step-more" | "canvas-key") => {
    const graphIds = removeAnnotationsFromSelection(ids);
    return graphIds.length ? requestCanvasDelete(graphIds, source) : graphIds.length !== ids.length;
  }, [removeAnnotationsFromSelection, requestCanvasDelete]);
  const previousInspectorOpenRef = useRef(inspectorOpen);
  const closeTransientSurfaces = useCallback(() => {
    setShowLegend(false);
    setShowValidationPanel(false);
    setShowMoreMenu(false);
    setShowAddMenu(false);
    setShowSearchResults(false);
  }, []);
  const openGroupFromCanvas = useCallback(
    (groupId: string, defaultTab?: string) => {
      closeTransientSurfaces();
      onOpenGroup(groupId, defaultTab);
    },
    [closeTransientSurfaces, onOpenGroup],
  );
  const editStepFromCanvas = useCallback(
    (stepId: string) => {
      closeTransientSurfaces();
      ignoreAsyncResult(onOpenStep(stepId));
    },
    [closeTransientSurfaces, onOpenStep],
  );
  const testStepFromCanvas = useCallback(
    (stepId: string) => {
      closeTransientSurfaces();
      return onOpenStep(stepId, "test-run");
    },
    [closeTransientSurfaces, onOpenStep],
  );
  const openStepFromCanvas = useCallback(
    (stepId: string) => {
      closeTransientSurfaces();
      if (!parseHookNodeId(stepId) && canvasGraph.steps.find((step) => (step.step_id || step.id) === stepId)?.kind === "stop_success" && onOpenWorkflowInterface) {
        onOpenWorkflowInterface("outputs");
        return;
      }
      ignoreAsyncResult(onOpenStep(stepId));
    },
    [canvasGraph.steps, closeTransientSurfaces, onOpenStep, onOpenWorkflowInterface],
  );
  const openNewStepFromCanvas = useCallback(
    (context?: { groupId?: string; forEachId?: string; laneId?: string }, at?: CanvasPlacementPoint) => {
      closeTransientSurfaces();
      suppressCanvasNodeClick();
      onOpenNewStep(context, at);
    },
    [closeTransientSurfaces, onOpenNewStep],
  );
  const consumeConnectionDropClick = useCallback((event?: Pick<MouseEvent, "clientX" | "clientY" | "detail">) => {
    const pending = pendingConnectionDropClickRef.current;
    pendingConnectionDropClickRef.current = null;
    // This belongs to one release sequence, never to a timer or another gesture.
    // Native mouse/touch client coordinates can round by one physical pixel.
    return Boolean(pending && event && event.detail > 0 && Math.abs(event.clientX - pending.x) <= 1 && Math.abs(event.clientY - pending.y) <= 1);
  }, []);
  const appendStepFromCanvas = useCallback(
    (source: WorkflowNodeRef, outcome: WorkflowOutcome, intent?: CanvasCreationIntent) => {
      const scope = connectionPlacement(connectionDefinition, source);
      if (!scope || !onAppendStep || changeSet.status === "applying" || changeSet.status === "conflict" || (structureLocked && !isHookMemberRef(source))) return false;
      closeTransientSurfaces();
      suppressCanvasNodeClick();
      const nextIntent = { ...(intent || { source, outcome, scope }), selectionGeneration: commandSelectionRef.current.generation };
      setCreationIntent(nextIntent);
      onAppendStep(source, outcome, nextIntent);
      return true;
    },
    [closeTransientSurfaces, onAppendStep, connectionDefinition, changeSet.status, structureLocked],
  );
  const openNewGroupFromCanvas = useCallback(
    (defaults: NewContainerDefaults) => {
      closeTransientSurfaces();
      onOpenNewGroup(defaults);
    },
    [closeTransientSurfaces, onOpenNewGroup],
  );
  const openWorkflowSettingsFromCanvas = useCallback(() => {
    closeTransientSurfaces();
    onOpenWorkflowSettings();
  }, [closeTransientSurfaces, onOpenWorkflowSettings]);
  const openWorkflowInterfaceFromCanvas = useCallback(() => {
    closeTransientSurfaces();
    onOpenWorkflowInterface?.("inputs");
  }, [closeTransientSurfaces, onOpenWorkflowInterface]);
  const openStopInterfaceSectionFromCanvas = useCallback((tab: "outputs") => {
    closeTransientSurfaces();
    onOpenWorkflowInterface?.(tab);
  }, [closeTransientSurfaces, onOpenWorkflowInterface]);
  const openWorkflowHistoryFromCanvas = useCallback(() => {
    closeTransientSurfaces();
    onOpenWorkflowHistory?.();
  }, [closeTransientSurfaces, onOpenWorkflowHistory]);
  const openWorkflowAutomationFromCanvas = useCallback(() => {
    closeTransientSurfaces();
    onOpenWorkflowAutomation();
  }, [closeTransientSurfaces, onOpenWorkflowAutomation]);
  const openGroupAutomationFromCanvas = useCallback(
    (groupId: string) => {
      closeTransientSurfaces();
      onOpenGroupAutomation(groupId);
    },
    [closeTransientSurfaces, onOpenGroupAutomation],
  );
  const openStepAutomationFromCanvas = useCallback(
    (stepId: string) => {
      closeTransientSurfaces();
      onOpenStepAutomation(stepId);
    },
    [closeTransientSurfaces, onOpenStepAutomation],
  );

  // Reuse the real action-card layout and static handles for the empty slot.
  const nextSlotTemplate = useMemo(() => buildCanvasLayout({
    canvasGraph: { ...canvasGraph, entry: { kind: "step", id: "visual:next-slot-template" }, groups: [], for_each: [], edges: [], automation_rules: [], steps: [{ id: "visual:next-slot-template", step_id: "visual:next-slot-template", kind: "wait", display_name: "", config_summary: "", ports: [{ outcome: { name: "success" }, label: "" }] }] },
    onOpenGroup: () => {}, onOpenGroupAutomation: () => {}, onOpenStep: () => {}, onOpenStepAutomation: () => {},
  }, t).nodes.find((node) => node.id === "visual:next-slot-template")!, [canvasGraph, t]);

  const draftEdgeKeys = useMemo(
    () => draftEdgeKeysFromOperations(changeSet.operations),
    [changeSet.operations],
  );
  const localNodeIds = useMemo(
    () => localNodeIdsFromOperations(changeSet.operations),
    [changeSet.operations],
  );

  const inlineLoopIds = useInlineLoopIds(workflowId);
  const { nodes, edges, totalWidth, rootRows } = useMemo(
    () =>
      buildCanvasLayout({
        canvasGraph,
        actionScopeKey: commandViewKey,
        structureLocked,
        draftEdgeKeys,
        localNodeIds,
        measuredStepHeights,
        runtimeMarks,
        runtimeEdges,
        containerRuntimeMarks,
        onOpenGroup: openGroupFromCanvas,
        onOpenGroupAutomation: openGroupAutomationFromCanvas,
        onOpenNewStep: openNewStepFromCanvas,
        onOpenNewGroup: openNewGroupFromCanvas,
        onAppendStep: onAppendStep ? appendStepFromCanvas : undefined,
        onOpenStep: openStepFromCanvas,
        onEditStep: editStepFromCanvas,
        onPlaceStep: onOpenStepPlacement ? (stepId: string) => { closeTransientSurfaces(); onOpenStepPlacement(stepId); } : undefined,
        onOpenStepTestRun: testStepFromCanvas,
        stepTestRunAllowed,
        onOpenStopInterfaceSection: onOpenWorkflowInterface ? openStopInterfaceSectionFromCanvas : undefined,
        onDeleteStep: onRequestGraphDelete ? (canvasStepId) => { ignoreAsyncResult(requestCanvasDelete([canvasStepId], "step-more")); } : undefined,
        onOpenStepAutomation: openStepAutomationFromCanvas,
        onOpenInputs: openWorkflowInterfaceFromCanvas,
        projectHint: scheduleProjectHint,
        inlineLoopIds,
      }, t),
    [
      closeTransientSurfaces,
      onOpenStepPlacement,
      canvasGraph,
      commandViewKey,
      structureLocked,
      draftEdgeKeys,
      localNodeIds,
      measuredStepHeights,
      runtimeMarks,
      runtimeEdges,
      containerRuntimeMarks,
      openGroupAutomationFromCanvas,
      openGroupFromCanvas,
      openNewStepFromCanvas,
      openNewGroupFromCanvas,
      appendStepFromCanvas,
      onAppendStep,
      openStepAutomationFromCanvas,
      openStepFromCanvas,
      editStepFromCanvas,
      testStepFromCanvas,
      stepTestRunAllowed,
      openStopInterfaceSectionFromCanvas,
      requestCanvasDelete,
      onRequestGraphDelete,
      openWorkflowInterfaceFromCanvas,
      onOpenWorkflowInterface,
      scheduleProjectHint,
      inlineLoopIds,
      t,
    ],
  );

  // Layout › Arrange, kept per workflow (Grouping › Layout).
  const undoLastChangeRef = useRef<() => void>(() => {});
  const arrange = useCanvasArrangeMode({
    workflowId, t, manualPositions, setManualPositions, layoutNodes: nodes, setGraphEditNotice,
    undoLastChange: useCallback(() => undoLastChangeRef.current(), []),
  });

  useEffect(() => {
    setMeasuredStepHeights({});
  }, [canvasGraph]);

  useEffect(() => {
    if (inspectorOpen && !previousInspectorOpenRef.current) {
      closeTransientSurfaces();
    }
    if (!inspectorOpen && previousInspectorOpenRef.current) setCreationIntent(null);
    previousInspectorOpenRef.current = inspectorOpen;
  }, [closeTransientSurfaces, inspectorOpen]);

  useEffect(() => {
    if (manualState.owner !== workflowId) setManualState({ owner: workflowId, positions: loadManualLayout(workflowId) });
    if (placementGestureRef.current?.canvasWorkflowId !== workflowId) placementGestureRef.current = null;
  }, [manualState.owner, workflowId]);

  useEffect(() => {
    // The old author's positions must never be saved under a new workflow key.
    if (manualState.owner === workflowId) saveManualLayout(workflowId, manualState.positions);
  }, [manualState, workflowId]);

  const placementOwnerFor = useCallback((nodeIds: readonly string[]): WorkflowChangeSetController | null => (
    resolvePlacementOwner ? resolvePlacementOwner(nodeIds) : changeSet
  ), [changeSet, resolvePlacementOwner]);
  const commandOwnerFor = useCallback((nodeIds: readonly string[]): WorkflowChangeSetController | null => {
    if (nodeIds.length) return placementOwnerFor(nodeIds);
    const recent = lastCommandOwnerRef.current;
    if (recent?.viewKey === commandViewKey) {
      const current = (placementHistoryOwners ?? [changeSet]).find((owner) =>
        owner.workflowId === recent.workflowId && owner.placementViewKey === recent.placementViewKey);
      if (current) return current;
    }
    return placementOwnerFor([]);
  }, [changeSet, commandViewKey, placementHistoryOwners, placementOwnerFor]);
  // Undo and Redo follow the latest action across the histories on this canvas
  // (the workflow's own and an open hook's), whatever is selected or focused:
  // Undo never skips a later edit to reach an older one (Canvas › Undo).
  const runHistory = useCallback((redo: boolean) => {
    const owners = placementHistoryOwners ?? [changeSet];
    const order = (owner: WorkflowChangeSetController) => (redo ? owner.redoSeq : owner.undoSeq) ?? Number.NEGATIVE_INFINITY;
    const owner = owners.filter((candidate) => (redo ? candidate.canRedo : candidate.canUndo))
      .sort((left, right) => order(right) - order(left))[0];
    if (!owner) return false;
    if (redo) owner.redoGraphIntent(); else owner.undoGraphIntent();
    return true;
  }, [changeSet, placementHistoryOwners]);
  const beginPlacementGesture = useCallback((nodeIds: string[], dragging: boolean) => {
    const eligible = nodeIds.filter((id) => {
      const node = nodes.find((candidate) => candidate.id === id);
      const hook = parseHookNodeId(id);
      return node && node.type !== "canvasNote" && node.type !== "workflowStart" &&
        !id.startsWith("visual:") && !(hook && !hook.memberStepId);
    });
    if (eligible.length !== nodeIds.length || !eligible.length) return null;
    const owner = placementOwnerFor(eligible);
    const ownerWorkflowId = owner?.workflowId || (!resolvePlacementOwner ? workflowId : "");
    if (!owner || !ownerWorkflowId || owner.status === "applying" || owner.status === "conflict") return null;
    const serial = ++clipboardManualInteractionsRef.current.serial;
    for (const id of eligible) clipboardManualInteractionsRef.current.subjects.set(id, serial);
    for (const id of eligible) placementGestureGenerationRef.current.set(id, placementGestureGenerationFor(id) + 1);
    const current = placementGestureRef.current;
    if (current && !current.complete && current.canvasWorkflowId === workflowId &&
      current.ownerWorkflowId === ownerWorkflowId) {
      current.subjectIds = [...new Set([...current.subjectIds, ...eligible])];
      current.dragging ||= dragging;
      return current;
    }
    const gesture = {
      canvasWorkflowId: workflowId,
      ownerWorkflowId,
      subjectIds: eligible,
      before: { ...manualPositions },
      dragging,
      complete: false,
    };
    placementGestureRef.current = gesture;
    return gesture;
  }, [manualPositions, nodes, placementGestureGenerationFor, placementOwnerFor, resolvePlacementOwner, workflowId]);

  useEffect(() => {
    const gesture = placementGestureRef.current;
    if (!gesture?.complete) return;
    placementGestureRef.current = null;
    if (gesture.canvasWorkflowId !== workflowId) return;
    const owner = placementOwnerFor(gesture.subjectIds);
    const ownerWorkflowId = owner?.workflowId || (!resolvePlacementOwner ? workflowId : "");
    if (!owner || ownerWorkflowId !== gesture.ownerWorkflowId) return;
    const delta = placementDelta(workflowId, gesture.subjectIds, gesture.before, manualPositions);
    if (delta) owner.recordPlacementIntent?.(delta);
  }, [manualPositions, placementOwnerFor, resolvePlacementOwner, workflowId]);

  useEffect(() => {
    for (const owner of placementHistoryOwners ?? [changeSet]) {
      const replay = owner.placementReplay;
      if (replay?.canvasWorkflowId !== workflowId || replay.ownerWorkflowId !== owner.workflowId) continue;
      const previous = handledPlacementReplayRef.current.get(replay.ownerWorkflowId) ?? 0;
      if (replay.serial <= previous) continue;
      // Consume even a stale or unavailable view: never replay an old token on return.
      handledPlacementReplayRef.current.set(replay.ownerWorkflowId, replay.serial);
      const resolved = placementOwnerFor(replay.subjectIds);
      if (resolved?.workflowId !== replay.ownerWorkflowId ||
        replay.viewKey !== resolved.placementViewKey) continue;
      setManualPositions((current) => replayPlacementValues(current, replay.values));
    }
  }, [changeSet, placementHistoryOwners, placementOwnerFor, setManualPositions, workflowId]);

  useEffect(() => {
    if (!highlightedNodeId) {
      return undefined;
    }
    const timeoutId = globalThis.setTimeout(() => setHighlightedNodeId(null), 2400);
    return () => globalThis.clearTimeout(timeoutId);
  }, [highlightedNodeId]);

  const searchResults = useMemo(
    () => buildCanvasSearchResults(canvasGraph, searchQuery),
    [canvasGraph, searchQuery],
  );

  useEffect(() => {
    setSearchIndex(0);
  }, [searchResults.length]);

  const nodesWithCanvasState = useMemo(
    () =>
      applyCanvasNodeState({
        connectionHelpActive,
        connectionPointedTarget,
        connectionTargetStates: connectingPort ? new Map(nodes.map((node) => {
          const target = nodeRefFromNodeId(node.id);
          const normal = node.id !== "visual:start" && !connectionRefusal(connectionDefinition, connectingPort.source, connectingPort.outcome, target, false, t);
          const sourceScope = connectionPlacement(connectionDefinition, connectingPort.source);
          const exit = target.kind === "group" ? { kind: "group_exit" as const, group_id: target.id, lane_id: sourceScope?.laneId || null } : target.kind === "for_each" ? { kind: "for_each_exit" as const, for_each_id: target.id, lane_id: sourceScope?.laneId || null } : null;
          const ownExit = exit && !connectionRefusal(connectionDefinition, connectingPort.source, connectingPort.outcome, exit, false, t);
          return [node.id, normal || ownExit ? "valid" as const : "invalid" as const];
        })) : undefined,
        highlightedNodeId: highlightedNodeId || runtimeNodeId,
        manualPositions,
        nodes,
      }).concat(annotationNodes),
    [connectionHelpActive, highlightedNodeId, manualPositions, nodes, runtimeNodeId, annotationNodes, connectionDefinition, connectingPort, connectionPointedTarget, t],
  );

  const currentNodesById = useMemo(
    () => new Map(nodesWithCanvasState.map((node) => [node.id, node])),
    [nodesWithCanvasState],
  );
  useLayoutEffect(() => { canvasNodesRef.current = currentNodesById; }, [currentNodesById]);
  useCanvasClipboardPublication({
    arrangeMode: arrange.mode,
    changeSet,
    commandViewKey,
    currentNodesById,
    manualPositions,
    inspectorWidth,
    pendingClipboardCommit,
    pendingClipboardCommitRef,
    placementHistoryOwners,
    reactFlowInstance,
    canvasMeasureRootRef,
    commandViewRef,
    clipboardViewGenerationRef,
    clipboardViewportGenerationRef,
    clipboardManualInteractionsRef,
    commandSelectionRef,
    clipboardTranslateRef,
    setManualPositions,
    setPendingClipboardCommit,
    setClipboardBusy,
    setClipboardSelection,
    setGraphEditNotice,
  });

  useCanvasRenderedStepHeights({
    canvasMeasureRootRef, setMeasuredStepHeights, nodesWithCanvasState
  });

  const { deleteEdgeData, visibleEdges, selectedEdge } = useCanvasInteractiveEdges({
    t, onDisconnectPort, setSelectedEdgeId, setGraphEditNotice, nodes, nodesWithCanvasState, rootRows, edges, showFailurePaths, structureLocked, hoveredEdgeId, selectedEdgeId, closeTransientSurfaces, onInsertStep, resolveCommandOwner: commandOwnerFor
  });

  const recordAutoLayout = useCallback((before: Record<string, CanvasPosition>) => {
    const ids = Object.keys(before);
    const owner = ids.length ? placementOwnerFor(ids) : null;
    const delta = owner && owner.status !== "applying" && owner.status !== "conflict" ? placementDelta(workflowId, ids, before, {}) : null;
    if (!owner || !delta) return undefined;
    const undo = watchGraphIntent(owner);
    owner.recordPlacementIntent?.(delta);
    return undo(t("react.workflows.undo", "Undo"));
  }, [placementOwnerFor, t, workflowId]);

  const { handleNodesChange, handleNodeDragStop, handleEdgesChange, handleAutoLayout } = useCanvasGraphEditing({
    t, beginPlacementGesture, changeAnnotationNodes, nodes, setManualPositions, saveAnnotation, visibleEdges, deleteEdgeData, setSelectedEdgeId, manualPositions, setGraphEditNotice, reactFlowInstance, canvasMeasureRootRef, recordAutoLayout
  });

  const { focusSearchResult, handleSearchKeyDown } = useCanvasSearchNavigation({
    autoArrange: arrange.mode === "auto",
    currentNodesById, reactFlowInstance, setHighlightedNodeId, setShowSearchResults, openGroupFromCanvas, openStepFromCanvas,
    focusNodeId, focusNodeNonce, focusNodeCreation, focusNodePlace, handledFocusNonceRef, canvasMeasureRootRef,
    manualPositions, edges, nodes, workflowId, placementOwnerFor, placementGestureGenerationFor,
    currentSelection: () => commandSelectionRef.current,
    setManualPositions, setCreationIntent, searchIndex, searchResults, setSearchIndex
  });

  undoLastChangeRef.current = () => { commandOwnerFor([])?.undoGraphIntent(); };
  // A palette request: the next created node lands at a point, or a note is added.
  const handledPlacementCommandRef = useRef(0);
  useEffect(() => {
    if (!canvasPlacementCommand || handledPlacementCommandRef.current === canvasPlacementCommand.nonce) return;
    handledPlacementCommandRef.current = canvasPlacementCommand.nonce;
    if (canvasPlacementCommand.kind === "place-next") {
      setCreationIntent({ source: { kind: "step", id: "visual:placement" }, outcome: { name: "success" }, scope: { groupId: "", forEachId: "", laneId: "" }, point: canvasPlacementCommand.point });
      return;
    }
    const bounds = canvasMeasureRootRef.current?.getBoundingClientRect();
    const centre = bounds && reactFlowInstance ? reactFlowInstance.screenToFlowPosition({ x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }) : { x: 120, y: 120 };
    annotations.add(canvasPlacementCommand.point ?? centre);
  }, [annotations, canvasPlacementCommand, reactFlowInstance]);
  const handleArrangedAutoLayout = useCallback(() => {
    // Manually: Auto layout arranges every step once and keeps it there.
    if (arrange.mode === "manual") {
      setManualPositions(arrangeSnapshot(nodes));
      setGraphEditNotice({ label: t("react.workflows.auto_layout_arranged", "Arranged. Steps stay where the layout put them."), tone: "success" });
      globalThis.requestAnimationFrame(() => { ignoreAsyncResult(fitCanvasToNodes(reactFlowInstance, canvasMotionDuration(), canvasMeasureRootRef.current?.getBoundingClientRect())); });
      return;
    }
    handleAutoLayout();
  }, [arrange.mode, handleAutoLayout, nodes, reactFlowInstance, setManualPositions, t]);
  const shellExtras: CanvasShellExtras = {
    arrangeMode: arrange.mode,
    onArrangeChange: arrange.choose,
    addNoteAt: (point) => {
      const bounds = canvasMeasureRootRef.current?.getBoundingClientRect();
      const centre = bounds && reactFlowInstance ? reactFlowInstance.screenToFlowPosition({ x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }) : { x: 120, y: 120 };
      annotations.add(point ?? centre);
    },
    forgetPlacements: (ids) => setManualPositions((current) => {
      if (!ids.some((id) => current[id])) return current;
      const next = { ...current };
      ids.forEach((id) => { delete next[id]; });
      return next;
    }),
    openNewStepAt: (context, at) => openNewStepFromCanvas(context, at),
    ...(onConvertGroup ? { convertGroup: onConvertGroup, convertGroupBlockedReason } : {}),
  };

  const failurePathCount = canvasGraph.edges.filter((edge) => edge.outcome.name === "error").length;
  const hasManualLayout = Boolean(Object.keys(manualPositions).length);

  const toggleAddMenu = useCallback(() => {
    onCloseInspector();
    setShowLegend(false);
    setShowValidationPanel(false);
    setShowMoreMenu(false);
    setShowSearchResults(false);
    setShowAddMenu((current) => !current);
  }, [onCloseInspector]);

  const toggleMoreMenu = useCallback(() => {
    setShowLegend(false);
    setShowValidationPanel(false);
    setShowAddMenu(false);
    setShowSearchResults(false);
    setShowMoreMenu((current) => !current);
  }, []);

  const { stageConnection, handleConnectionTargetChange, isValidConnection, handleConnect, handleConnectStart, handleConnectEnd, handleDeleteSelectedEdge, handleInsertOnSelectedEdge } = useCanvasConnectionGestures({
    connectionDefinition, changeSet, structureLocked, t, onConnectPort, setGraphEditNotice, connectingPort, setConnectionPointedTarget, connectCompletedRef, pendingConnectionDropClickRef, connectingRef, setConnectingPort, setConnectionHelpActive, reactFlowInstance, closeTransientSurfaces, setSelectedEdgeId, setCreationIntent, onInsertStep, appendStepFromCanvas, deleteEdgeData, selectedEdge
  });

  const structureStatus = structureStatusView(changeSet.status, changeSet.operations.length, t);

  return (
    <CanvasPortProvider value={{ definition: connectionDefinition, dragging: connectionHelpActive, creationPortKey: creationIntent ? canvasPortKey(creationIntent.source, creationIntent.outcome) : null, append: appendStepFromCanvas, insert: (edge) => { setSelectedEdgeId(null); closeTransientSurfaces(); onInsertStep(edge); }, connect: stageConnection, disconnect: (source, outcome) => { if (changeSet.status !== "applying" && changeSet.status !== "conflict" && (!structureLocked || isHookMemberRef(source))) onDisconnectPort(source, outcome); }, notice: (label) => setGraphEditNotice({ label, tone: "error" }), disabled: changeSet.status === "applying" || changeSet.status === "conflict" }}>
    <WorkflowCanvasShell
      annotationError={annotations.error}
      annotationsBusy={annotations.busy || annotations.disabled}
      onReloadNotes={() => ignoreAsyncResult(annotations.reload())}
      onAddNote={addNoteAt}
      handleNodeDragStop={handleNodeDragStop}
      advancedMode={advancedMode}
      backHref={backHref}
      canvasGraph={canvasGraph}
      canvasMeasureRootRef={canvasMeasureRootRef}
      onUserViewportInteraction={onUserViewportInteraction}
      changeSet={changeSet}
      closeTransientSurfaces={closeTransientSurfaces}
      effectiveAvailabilityNotice={effectiveAvailabilityNotice}
      missingExtensions={missingExtensions}
      headerContext={headerContext}
      exportYamlHref={exportYamlHref}
      exportYamlLabel={exportYamlLabel || t("react.workflows.export_yaml", "Export YAML")}
      failurePathCount={failurePathCount}
      focusSearchResult={focusSearchResult}
      graphEditNotice={graphEditNotice}
      toastHeld={connectionHelpActive}
      inspectorOpen={inspectorOpen}
      deleteGuardNotice={deleteGuardNotice}
      onRetryGraphDelete={onRetryGraphDelete}
      onOpenDeleteAutomation={onOpenDeleteAutomation}
      handleAutoLayout={handleArrangedAutoLayout}
      extras={shellExtras}
      handleConnect={handleConnect}
      isValidConnection={isValidConnection}
      handleConnectionTargetChange={handleConnectionTargetChange}
      handleConnectEnd={handleConnectEnd}
      handleConnectStart={handleConnectStart}
      consumeConnectionDropClick={consumeConnectionDropClick}
      handleDeleteSelectedEdge={handleDeleteSelectedEdge}
      onCommandSelectionChange={onCommandSelectionChange}
      onClipboardSnapshotChange={onClipboardSnapshotChange}
      requestClipboardCommand={onReadClipboardAuthority ? requestClipboardCommand : undefined}
      clipboardBusy={clipboardBusy}
      clipboardAvailability={clipboardAvailability}
      clipboardSelection={clipboardSelection}
      commandClearNonce={commandClearNonce}
      requestCanvasDelete={requestCanvasDeleteWithNotes}
      resolveCommandOwner={commandOwnerFor}
      runHistory={runHistory}
      commandViewKey={commandViewKey}
      handleEdgesChange={handleEdgesChange}
      handleInsertOnSelectedEdge={handleInsertOnSelectedEdge}
      handleNodesChange={handleNodesChange}
      handleSearchKeyDown={handleSearchKeyDown}
      hasManualLayout={hasManualLayout}
      nodesWithCanvasState={nodesWithCanvasState}
      creationSelection={creationSelection && (creationSelection.selectionGeneration === undefined ||
        creationSelection.selectionGeneration === commandSelectionRef.current.generation) ? creationSelection : null}
      nextSlotTemplate={nextSlotTemplate}
      onAppendAtSlot={onAppendStep ? (source, slot) => {
        const ref = nodeRefFromNodeId(source.id);
        const scope = connectionPlacement(connectionDefinition, ref);
        if (scope) appendStepFromCanvas(ref, slot.outcome, { source: ref, outcome: slot.outcome, scope, point: { x: slot.rect.x, y: slot.rect.y } });
      } : undefined}
      onCloseInspector={onCloseInspector}
      inspectorWidth={inspectorWidth}
      canvasRunBar={canvasRunBar}
      canvasTestRunAction={canvasTestRunAction}
      onToggleAdvancedMode={onToggleAdvancedMode}
      onValidationIssue={onValidationIssue}
      openNewGroupFromCanvas={openNewGroupFromCanvas}
      openNewStepFromCanvas={openNewStepFromCanvas}
      openWorkflowAutomationFromCanvas={openWorkflowAutomationFromCanvas}
      openWorkflowSettingsFromCanvas={openWorkflowSettingsFromCanvas}
      openWorkflowHistoryFromCanvas={onOpenWorkflowHistory ? openWorkflowHistoryFromCanvas : undefined}
      compareVersions={onCompareVersions}
      openWorkflowInterfaceFromCanvas={openWorkflowInterfaceFromCanvas}
      searchIndex={searchIndex}
      searchQuery={searchQuery}
      searchResults={searchResults}
      selectedEdge={selectedEdge}
      setGraphEditNotice={setGraphEditNotice}
      setHoveredEdgeId={setHoveredEdgeId}
      setReactFlowInstance={setReactFlowInstance}
      setSearchIndex={setSearchIndex}
      setSearchQuery={setSearchQuery}
      setSelectedEdgeId={setSelectedEdgeId}
      setShowFailurePaths={setShowFailurePaths}
      setShowLegend={setShowLegend}
      setShowMiniMap={setShowMiniMap}
      setShowMoreMenu={setShowMoreMenu}
      setShowAddMenu={setShowAddMenu}
      setShowSearchResults={setShowSearchResults}
      setShowValidationPanel={setShowValidationPanel}
      showAddMenu={showAddMenu}
      showFailurePaths={showFailurePaths}
      showLegend={showLegend}
      showMiniMap={showMiniMap}
      showMoreMenu={showMoreMenu}
      showSearchResults={showSearchResults}
      showValidationPanel={showValidationPanel}
      structureStatus={structureStatus}
      toggleAddMenu={toggleAddMenu}
      toggleMoreMenu={toggleMoreMenu}
      totalWidth={totalWidth}
      visibleEdges={visibleEdges}
      workflowId={workflowId}
      structureLocked={structureLocked}
    />
    </CanvasPortProvider>
  );
}
