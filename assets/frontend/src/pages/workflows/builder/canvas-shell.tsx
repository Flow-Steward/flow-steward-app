import { WorkflowCanvasGrid, CanvasAutoLayoutIcon, CanvasInterfaceCounts, CanvasMiniMap, CanvasToolbarHint, CanvasControls, type CanvasShellProps } from "./canvas-shell-support";
import { CanvasRunBar } from "./canvas-run-bar";
import { useCanvasRunFollow } from "./canvas-run-follow";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { DsPopover, type PopoverTriggerProps } from "@/components/shared/popover";
import { canvasMotionDuration, canvasPastePane, fitCanvasToNodes, canvasNextStepSlots } from "./canvas-viewport";
import { CanvasConnectionPreview } from "./canvas-step-nodes";
import { CANVAS_GRID } from "./layout-constants";
import { useI18n } from "@/lib/i18n/useI18n";
import { isHookMemberRef } from "@/pages/workflows/builder/hook-frames";
import { useState, useRef, useEffect, useCallback, useLayoutEffect, useMemo, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";

import {
  type Edge,
  ViewportPortal,
  type Node,
  type NodeChange,
  Panel,
  ReactFlow,
  type ReactFlowInstance,
  SelectionMode,
} from "@xyflow/react";
import { CanvasToast } from "./canvas-toast";
import { CanvasSelectionBar } from "./canvas-selection-bar";
import { CanvasConnectionBar } from "./canvas-edge-actions";
import { canvasEdgeNames } from "./canvas-edge-labels";
import { nextZoomStop, runCanvasKey, walkCanvasGraph, type CanvasKeyContext, type CanvasWalkMemory } from "./canvas-keyboard";
import { useBuilderHistoryKeys } from "./canvas-history-keys";
import type { CanvasPasteTarget } from "./canvas-types";
import {
  ArrowLeft,
  BadgeAlert,
  ChevronDown,
  Search,
  ListChecks,
  Play,
  Plus,
  SquarePlus,
  Settings2,
  Workflow,
  X,
} from "lucide-react";
import { IconButton } from "@/components/ui/icon-button";
import "./canvas-builder.css";
import { Button } from "@/components/ui/button";
import { DsAlert } from "@/components/shared/design-system";
import { WorkflowMissingExtensionNotice } from "@/pages/workflows/shared/header";
import { Link, useSearchParams } from "react-router";

import type { WorkflowCanvasPort } from "@/pages/workflows/shared/api";
import type { ForEachNodeData, GroupNodeData, StartNodeData, StepNodeData } from "@/pages/workflows/builder/layout";
import {
  absoluteNodeCenter,
  consumeCanvasNodeClickSuppression,
  isSecondaryNodeAction,
  isStepNode,
  suppressCanvasNodeClick,
  type CanvasPosition,
} from "@/pages/workflows/builder/canvas-core";
import {
  CanvasViewportControls,
  canvasEdgeTypes,
  stepNodeTypes,
} from "@/pages/workflows/builder/canvas-group-controls";
import {
  LegendPanel,
  ValidationPanel,
} from "@/pages/workflows/builder/canvas-panels";
import { outcomeHandleId } from "@/pages/workflows/builder/api";
import type { CanvasCreationSelection } from "@/pages/workflows/builder/canvas-types";
import { CanvasNoteNode } from "@/pages/workflows/builder/canvas-note-node";
import { CanvasNoteToolbarButton } from "@/pages/workflows/builder/canvas-note-placement";
import { CanvasCardCommandsProvider, useCanvasMenus, type CanvasMenuHost } from "@/pages/workflows/builder/canvas-context-menus";
import { CanvasLayoutControl } from "@/pages/workflows/builder/canvas-layout-menu";
import { applyRefusalMessage, changeIssueMessage } from "@/pages/workflows/detail/detail-change-set";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

function compareCanvasIds(left: string, right: string): number {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

const canvasNodeTypes = { ...stepNodeTypes, canvasNote: CanvasNoteNode };

type Translate = ReturnType<typeof useI18n>["t"];

function failurePathButtonLabel(count: number, visible: boolean, t: Translate): string {
  if (!count) return t("react.workflows.no_failure_paths_available", "No failure paths available");
  return visible
    ? t("react.workflows.hide_count_failure_paths", "Hide {count} failure paths", { count })
    : t("react.workflows.show_count_failure_paths", "Show {count} failure paths", { count });
}

function choose<T>(condition: boolean, yes: T, no: T): T {
  return condition ? yes : no;
}

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

function renderMoreButton(props: PopoverTriggerProps, label: string): ReactNode {
  return <Button {...props} variant="secondary" size="md" type="button">
    <Settings2 aria-hidden="true" />
    {label}<ChevronDown aria-hidden="true" />
  </Button>;
}

function joinClasses(...values: unknown[]): string {
  return values.map((value) => stringFromUnknownValue(value)).filter(Boolean).join(" ");
}

/** "Add next step" after Next; "True: add step" for a branch's own outcome. */
function nextSlotLabel(slot: { outcome: { name: string }; label: string }, t: Translate): string {
  return slot.outcome.name === "success"
    ? t("react.workflows.add_next_step", "Add next step")
    : t("react.workflows.outcome_add_step", "{outcome}: add step", { outcome: slot.label });
}

export function WorkflowCanvasShell({
  annotationError,
  annotationsBusy,
  onAddNote,
  onReloadNotes,
  handleNodeDragStop,
  advancedMode,
  backHref,
  canvasGraph,
  canvasMeasureRootRef,
  onUserViewportInteraction,
  changeSet,
  closeTransientSurfaces,
  effectiveAvailabilityNotice,
  missingExtensions = null,
  headerContext,
  exportYamlHref,
  exportYamlLabel,
  failurePathCount,
  focusSearchResult,
  graphEditNotice,
  toastHeld = false,
  inspectorOpen = false,
  deleteGuardNotice,
  onRetryGraphDelete,
  onOpenDeleteAutomation,
  handleAutoLayout,
  handleConnect,
  isValidConnection,
  handleConnectionTargetChange,
  handleConnectEnd,
  handleConnectStart,
  consumeConnectionDropClick,
  handleDeleteSelectedEdge,
  onCommandSelectionChange,
  onClipboardSnapshotChange,
  requestClipboardCommand,
  clipboardBusy = false,
  clipboardAvailability = { copy: false, duplicate: false },
  clipboardNotice,
  clipboardSelection,
  commandViewKey,
  commandClearNonce,
  requestCanvasDelete,
  resolveCommandOwner,
  runHistory,
  handleEdgesChange,
  handleInsertOnSelectedEdge,
  handleNodesChange,
  handleSearchKeyDown,
  hasManualLayout,
  nodesWithCanvasState,
  creationSelection,
  nextSlotTemplate,
  onAppendAtSlot,
  onCloseInspector,
  inspectorWidth,
  canvasRunBar,
  canvasTestRunAction,
  onToggleAdvancedMode,
  onValidationIssue,
  openNewGroupFromCanvas,
  openNewStepFromCanvas,
  openWorkflowAutomationFromCanvas,
  openWorkflowSettingsFromCanvas,
  openWorkflowInterfaceFromCanvas,
  openWorkflowHistoryFromCanvas,
  compareVersions,
  searchIndex,
  searchQuery,
  searchResults,
  selectedEdge,
  setGraphEditNotice,
  setHoveredEdgeId,
  setReactFlowInstance,
  setSearchIndex,
  setSearchQuery,
  setSelectedEdgeId,
  setShowFailurePaths,
  setShowLegend,
  setShowMiniMap,
  setShowMoreMenu,
  setShowSearchResults,
  setShowValidationPanel,
  showFailurePaths,
  showLegend,
  showMiniMap,
  showMoreMenu,
  showSearchResults,
  showValidationPanel,
  structureStatus,
  toggleMoreMenu,
  totalWidth,
  visibleEdges,
  workflowId,
  structureLocked = false,
  extras,
}: Readonly<CanvasShellProps>) {
  const { t } = useI18n();
  useBuilderHistoryKeys(runHistory);
  const [searchParams] = useSearchParams();
  const [gridGap, setGridGap] = useState(totalWidth > 1800 ? CANVAS_GRID * 2 : CANVAS_GRID);
  const [searchOpen, setSearchOpen] = useState(false);
  const [selectionState, setSelectionState] = useState<{ viewKey: string; ids: Set<string> }>(
    () => ({ viewKey: commandViewKey, ids: new Set() }),
  );
  // The same display ID may exist in another workflow or Hook view. It must
  // never inherit the previous view's selected appearance or command target.
  const selectedNodeIds = useMemo(() => {
    if (selectionState.viewKey !== commandViewKey) return new Set<string>();
    const liveIds = new Set(nodesWithCanvasState.map((node) => node.id));
    const remaining = new Set([...selectionState.ids].filter((id) => liveIds.has(id)));
    return remaining.size === selectionState.ids.size ? selectionState.ids : remaining;
  }, [commandViewKey, nodesWithCanvasState, selectionState]);
  // Retire removed identities before publishing this view's selection. A later
  // node with the same display ID requires a fresh explicit selection nonce.
  if (selectionState.viewKey === commandViewKey && selectedNodeIds !== selectionState.ids) {
    setSelectionState({ viewKey: commandViewKey, ids: selectedNodeIds });
  }
  const setSelectedNodeIds = useCallback((next: Set<string> | ((current: Set<string>) => Set<string>)) => {
    setSelectionState((current) => {
      const ids = current.viewKey === commandViewKey ? current.ids : new Set<string>();
      return { viewKey: commandViewKey, ids: typeof next === "function" ? next(ids) : next };
    });
  }, [commandViewKey]);
  useLayoutEffect(() => {
    if (selectionState.viewKey !== commandViewKey) setSelectionState({ viewKey: commandViewKey, ids: new Set() });
  }, [commandViewKey, selectionState.viewKey]);
  useLayoutEffect(() => {
    onCommandSelectionChange([...selectedNodeIds].sort(compareCanvasIds));
  }, [onCommandSelectionChange, selectedNodeIds]);
  useLayoutEffect(() => {
    if (!onClipboardSnapshotChange) return;
    const byId = new Map(nodesWithCanvasState.map((node) => [node.id, node]));
    const world = new Map<string, CanvasPosition>();
    const origin = (id: string, visiting = new Set<string>()): CanvasPosition | null => {
      const cached = world.get(id); if (cached) return cached;
      const node = byId.get(id);
      if (!node || visiting.has(id) || !Number.isFinite(node.position.x) || !Number.isFinite(node.position.y)) return null;
      visiting.add(id);
      const parent = node.parentId ? origin(node.parentId, visiting) : { x: 0, y: 0 };
      visiting.delete(id);
      if (!parent) return null;
      const point = { x: parent.x + node.position.x, y: parent.y + node.position.y };
      world.set(id, point); return point;
    };
    for (const node of nodesWithCanvasState) if ((isStepNode(node) || node.type === "groupContainer" || node.type === "forEachContainer" || node.type === "inlineLoop") && !origin(node.id)) {
      onClipboardSnapshotChange(null); return;
    }
    const ids = [...selectedNodeIds].sort(compareCanvasIds);
    if (!ids.length || ids.some((id) => !world.has(id))) { onClipboardSnapshotChange(null); return; }
    const selected = ids.map((id) => byId.get(id)!);
    const x = Math.min(...ids.map((id) => world.get(id)!.x));
    const bottom = Math.max(...selected.map((node) => world.get(node.id)!.y + Number(node.height || node.style?.height || 0)));
    onClipboardSnapshotChange(Number.isFinite(x) && Number.isFinite(bottom)
      ? { ids, worldPositions: world, duplicateOrigin: { x, y: bottom + CANVAS_GRID * 2 } } : null);
  }, [nodesWithCanvasState, onClipboardSnapshotChange, selectedNodeIds]);
  const observedCommandClearNonce = useRef<number | null>(null);
  useEffect(() => {
    // A view-only setter change must not replay a previously handled clear.
    if (observedCommandClearNonce.current === commandClearNonce) return;
    observedCommandClearNonce.current = commandClearNonce;
    if (commandClearNonce) setSelectedNodeIds(new Set());
  }, [commandClearNonce, setSelectedNodeIds]);
  const [appliedCreationSelection, setAppliedCreationSelection] = useState<CanvasCreationSelection | null>(null);
  if (creationSelection && creationSelection.nonce !== appliedCreationSelection?.nonce && nodesWithCanvasState.some((node) => node.id === creationSelection.nodeId)) {
    setAppliedCreationSelection(creationSelection);
    setSelectedNodeIds(new Set([creationSelection.nodeId]));
  }
  const [appliedClipboardSelection, setAppliedClipboardSelection] = useState<number | null>(null);
  if (clipboardSelection && clipboardSelection.nonce !== appliedClipboardSelection &&
    clipboardSelection.ids.every((id) => nodesWithCanvasState.some((node) => node.id === id))) {
    setAppliedClipboardSelection(clipboardSelection.nonce);
    setSelectedNodeIds(new Set(clipboardSelection.ids));
  }
  const [autoTooltip, setAutoTooltip] = useState(false);
  const [groupMenuOpen, setGroupMenuOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const [viewportInstance, setViewportInstance] = useState<ReactFlowInstance | null>(null);
  const runFollow = useCanvasRunFollow({ instance: viewportInstance, nodes: nodesWithCanvasState,
    runKey: canvasRunBar?.selection.kind === "verified" ? canvasRunBar.selection.run.jobId : "", active: Boolean(canvasTestRunAction) });
  const initialViewportWorkflow = useRef("");
  useEffect(() => {
    const root = canvasMeasureRootRef.current;
    if (!root || !viewportInstance || !nodesWithCanvasState.length || initialViewportWorkflow.current === workflowId) return;
    const fitInitial = () => {
      const bounds = root.getBoundingClientRect();
      if (bounds.width <= 0 || bounds.height <= 0 || initialViewportWorkflow.current === workflowId) return;
      initialViewportWorkflow.current = workflowId;
      ignoreAsyncResult(fitCanvasToNodes(viewportInstance, 0, bounds, "initial"));
    };
    fitInitial();
    const observer = new ResizeObserver(fitInitial);
    observer.observe(root);
    return () => observer.disconnect();
  }, [canvasMeasureRootRef, nodesWithCanvasState.length, viewportInstance, workflowId]);
  const requestStep = useCallback(() => {
    const selected = nodesWithCanvasState.find((node) => selectedNodeIds.has(node.id) && isStepNode(node));
    const data = selected?.data as StepNodeData | undefined;
    if (data?.freeOutcomes?.includes("success") && data.onAppendStep) {
      data.onAppendStep({ kind: "step", id: data.step.step_id || data.step.id }, { name: "success" });
    } else openNewStepFromCanvas({});
  }, [nodesWithCanvasState, openNewStepFromCanvas, selectedNodeIds]);
  const selectedSource = onAppendAtSlot && selectedNodeIds.size === 1 ? nodesWithCanvasState.find((node) => selectedNodeIds.has(node.id) && (isStepNode(node) || node.type === "groupContainer" || node.type === "forEachContainer" || node.type === "inlineLoop")) : undefined;
  const selectedIds = [...selectedNodeIds].sort(compareCanvasIds);
  // The selected step's connections stand out (Connections › Kinds "Selected step").
  const shownEdges = useMemo(() => selectedNodeIds.size ? visibleEdges.map((edge) => edge.data && (selectedNodeIds.has(edge.source) || selectedNodeIds.has(edge.target))
    ? { ...edge, data: { ...edge.data, stepSelected: true } } : edge) : visibleEdges, [selectedNodeIds, visibleEdges]);
  const dismissGraphEditNotice = useCallback(() => setGraphEditNotice(null), [setGraphEditNotice]);
  const selectedNodes = nodesWithCanvasState.filter((node) => selectedNodeIds.has(node.id));
  const selectedCommandOwner = resolveCommandOwner(selectedIds);
  const selectedCanDelete = selectedNodes.length === selectedNodeIds.size && selectedNodes.every((node) => {
    if (node.hidden || node.id.startsWith("visual:") || !isStepNode(node) && node.type !== "groupContainer" && node.type !== "forEachContainer" && node.type !== "inlineLoop") return false;
    const data = node.data as { readOnly?: boolean; group?: { hook?: unknown } };
    return !data.readOnly && !data.group?.hook;
  }) && Boolean(selectedCommandOwner) && selectedCommandOwner?.status !== "applying" && selectedCommandOwner?.status !== "conflict";
  const selectAllNodes = () => {
    setSelectedEdgeId(null);
    setSelectedNodeIds(new Set(nodesWithCanvasState.filter((node) => !node.hidden && (isStepNode(node) || node.type === "groupContainer" || node.type === "forEachContainer" || node.type === "inlineLoop")).filter((node) => !node.id.startsWith("visual:") && !(node.data as { group?: { hook?: unknown } }).group?.hook).map((node) => node.id)));
  };
  const fitView = () => { onUserViewportInteraction?.(); if (viewportInstance) ignoreAsyncResult(fitCanvasToNodes(viewportInstance, canvasMotionDuration(), canvasMeasureRootRef.current?.getBoundingClientRect(), "fit")); };
  const menuHost: CanvasMenuHost = {
    t, workflowId, nodes: nodesWithCanvasState, canvasGraph, structureLocked,
    selectedIds,
    select: (ids) => setSelectedNodeIds(new Set(ids)),
    owner: resolveCommandOwner,
    ...(requestClipboardCommand ? { clipboard: requestClipboardCommand } : {}),
    // "Paste here" pastes like ⌘V, at the point that was right-clicked.
    ...(requestClipboardCommand && viewportInstance ? {
      paste: (clientPoint: { x: number; y: number }) => {
        const at = viewportInstance.screenToFlowPosition(clientPoint);
        ignoreAsyncResult(requestClipboardCommand("paste", { origin: { x: Math.round(at.x / CANVAS_GRID) * CANVAS_GRID, y: Math.round(at.y / CANVAS_GRID) * CANVAS_GRID }, mode: "pointer" }));
      },
    } : {}),
    canPaste: !structureLocked && !clipboardBusy,
    remove: (ids) => { ignoreAsyncResult(requestCanvasDelete([...ids].sort(compareCanvasIds), "canvas-key")); },
    openNewStepAt: (context, at) => { if (extras) extras.openNewStepAt(context, at); else openNewStepFromCanvas(context); },
    addNoteAt: (point) => { if (extras) extras.addNoteAt(point); else onAddNote(); },
    notesDisabled: annotationsBusy,
    selectAll: selectAllNodes,
    fitView,
    screenToFlow: (point) => viewportInstance ? viewportInstance.screenToFlowPosition(point) : point,
    notice: (notice) => setGraphEditNotice(notice),
    forgetPlacements: (ids) => extras?.forgetPlacements(ids),
    ...(extras?.convertGroup ? { convertGroup: extras.convertGroup, convertGroupBlockedReason: extras.convertGroupBlockedReason } : {}),
  };
  const menus = useCanvasMenus(menuHost);
  const openGroupSelection = (anchor: HTMLElement | { x: number; y: number }) => menus.openGroupMenu(selectedIds, anchor);
  const slotSource = (() => {
    if (!selectedSource) return undefined;
    const data = selectedSource.data as { readOnly?: boolean; step?: StepNodeData["step"]; group?: GroupNodeData["group"]; forEach?: { id: string }; ports?: WorkflowCanvasPort[] };
    if (data.readOnly || data.group?.hook) return undefined;
    const kind = data.step ? "step" : data.group ? "group" : "for_each";
    const id = data.step?.step_id || data.step?.id || data.group?.id || data.forEach?.id;
    if (!id) return undefined;
    const ports = data.step?.ports ?? data.ports ?? [];
    const freeOutcomes = ports.filter((port) => !canvasGraph.edges.some((edge) => edge.source.kind === kind && edge.source.id === id && outcomeHandleId(edge.outcome) === outcomeHandleId(port.outcome))).map((port) => outcomeHandleId(port.outcome));
    return { ...selectedSource, data: { ...selectedSource.data, freeOutcomes } };
  })();
  const nextSlots = slotSource && changeSet.status !== "applying" && changeSet.status !== "conflict" ? canvasNextStepSlots(slotSource, nodesWithCanvasState, nextSlotTemplate, visibleEdges) : [];
  const closeLocalPopovers = () => { setGroupMenuOpen(false); setSearchOpen(false); };
  const closeAllPopovers = () => { closeTransientSurfaces(); closeLocalPopovers(); };

  const walkMemoryRef = useRef<CanvasWalkMemory>({ back: {}, out: null });
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  // A box selection selects steps; the connections it crosses are not "the selected connection".
  const boxSelectingRef = useRef(false);
  const openCanvasNode = useCallback((node: Node) => {
    if (node.type === "workflowStart") {
      (node.data as StartNodeData).onOpenInputs?.();
      return;
    }
    if (isStepNode(node)) {
      const data = node.data as StepNodeData;
      data.onOpenStep(data.step.step_id || data.step.id);
      return;
    }
    if (node.type === "groupContainer") {
      const data = node.data as GroupNodeData;
      data.onOpenGroup(data.group.id);
      return;
    }
    if (node.type === "inlineLoop") {
      const data = node.data as ForEachNodeData;
      data.onOpenForEach(data.forEach.id);
    }
  }, []);
  /** A paste lands at the pointer over the canvas, else one grid step on from the middle of the view each time. */
  const pasteTarget = (): CanvasPasteTarget | null => {
    const bounds = canvasMeasureRootRef.current?.getBoundingClientRect();
    if (!viewportInstance || !bounds) return null;
    const pointer = pointerRef.current;
    const pane = canvasPastePane(canvasMeasureRootRef.current, inspectorWidth);
    if (!pane) return null;
    const target = pointer ? document.elementFromPoint?.(pointer.x, pointer.y) : null;
    const usable = pointer && pointer.x >= pane.left && pointer.x <= pane.right && pointer.y >= pane.top && pointer.y <= pane.bottom &&
      !target?.closest(".react-flow__panel, .workflow-toolbar-popover");
    // Leaving the stage keeps the existing offset anchor at its full-stage center.
    // Only an obstructed remembered pointer falls back to the usable pane center.
    const anchor = !pointer ? { x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
      : usable ? pointer : { x: (pane.left + pane.right) / 2, y: (pane.top + pane.bottom) / 2 };
    const at = viewportInstance.screenToFlowPosition(anchor);
    const snap = (value: number) => Math.round(value / CANVAS_GRID) * CANVAS_GRID;
    return { origin: { x: snap(at.x), y: snap(at.y) }, mode: pointer ? "pointer" : "offset" };
  };
  /** The walk's next node: selected, in view, focused for a screen reader, and open when an editor is (Canvas › Walking the graph). */
  const goToNode = (id: string, nodesById: Map<string, Node>) => {
    const node = nodesById.get(id);
    if (!node) return;
    setSelectedEdgeId(null);
    setSelectedNodeIds(new Set([id]));
    if (viewportInstance) {
      onUserViewportInteraction?.();
      const centre = absoluteNodeCenter(node, nodesById);
      ignoreAsyncResult(viewportInstance.setCenter(centre.x, centre.y, { duration: canvasMotionDuration(), zoom: viewportInstance.getZoom() }));
    }
    if (inspectorOpen && node.type !== "workflowStart") openCanvasNode(node);
    globalThis.requestAnimationFrame(() => {
      const element = [...(canvasMeasureRootRef.current?.querySelectorAll<HTMLElement>(".react-flow__node") ?? [])].find((candidate) => candidate.dataset.id === id);
      element?.focus({ preventScroll: true });
    });
  };
  /** The canvas keys (Canvas › Keys); the bindings live in canvas-keyboard.ts. */
  const handleCanvasKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const target = event.target;
    if (event.nativeEvent.isComposing || !(target instanceof Element) || (target instanceof HTMLElement && target.isContentEditable) ||
      target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='textbox'], [role='combobox'], [role='listbox'], [role='dialog'], [role='alertdialog'], [role='menu'], .workflow-toolbar-popover")) return;
    const onControl = Boolean(target.closest("button, a, [role='button'], [data-canvas-secondary-action='true']"));
    const focusedId = target.closest<HTMLElement>(".react-flow__node")?.dataset.id;
    const nodesById = new Map(nodesWithCanvasState.map((node) => [node.id, node]));
    const commandIds = selectedIds.length ? selectedIds : focusedId ? [focusedId] : [];
    const closeAll = () => { closeTransientSurfaces(); setGroupMenuOpen(false); setSearchOpen(false); };
    const history = (redo: boolean) => {
      if (runHistory) return runHistory(redo);
      const owner = resolveCommandOwner(commandIds);
      if (!owner || !(redo ? owner.canRedo : owner.canUndo)) return false;
      if (redo) owner.redoGraphIntent(); else owner.undoGraphIntent();
      return true;
    };
    const clipboard = (command: "copy" | "cut" | "paste" | "duplicate", enabled: boolean) => {
      if (!requestClipboardCommand || !enabled || clipboardBusy) return false;
      ignoreAsyncResult(requestClipboardCommand(command, command === "paste" ? pasteTarget() : undefined));
      return true;
    };
    const context: CanvasKeyContext = {
      structureLocked,
      fit: (mode) => { onUserViewportInteraction?.(); if (viewportInstance) ignoreAsyncResult(fitCanvasToNodes(viewportInstance, canvasMotionDuration(), canvasMeasureRootRef.current?.getBoundingClientRect(), mode)); },
      zoomTo100: () => { onUserViewportInteraction?.(); if (viewportInstance) ignoreAsyncResult(viewportInstance.zoomTo(1, { duration: canvasMotionDuration() })); },
      zoomStep: (direction) => { onUserViewportInteraction?.(); if (viewportInstance) ignoreAsyncResult(viewportInstance.zoomTo(nextZoomStop(viewportInstance.getZoom(), direction), { duration: canvasMotionDuration() })); },
      openSearch: () => { closeTransientSurfaces(); setGroupMenuOpen(false); setSearchOpen(true); },
      addStep: () => { closeAll(); requestStep(); },
      autoLayout: () => { onUserViewportInteraction?.(); closeAll(); handleAutoLayout(); },
      selectAll: () => {
        setSelectedEdgeId(null);
        setSelectedNodeIds(new Set(nodesWithCanvasState.filter((node) => !node.hidden && (isStepNode(node) || node.type === "groupContainer" || node.type === "forEachContainer")).filter((node) => !node.id.startsWith("visual:") && !(node.data as { group?: { hook?: unknown } }).group?.hook).map((node) => node.id)));
      },
      escape: () => {
        if (selectedEdge) { setSelectedEdgeId(null); return true; }
        if (!selectedNodeIds.size) return false;
        setSelectedNodeIds(new Set());
        return true;
      },
      deleteSelection: () => {
        if (selectedEdge) { handleDeleteSelectedEdge(); return true; }
        // A protected author still deletes inside its hooks: the delete authority decides, not the key.
        // A note keeps its own selection (a click on its paper selects it there), so Delete
        // takes the selected note when nothing else is selected.
        const ids = selectedIds.length ? selectedIds : nodesWithCanvasState.filter((node) => node.type === "canvasNote" && node.selected).map((node) => node.id);
        if (!ids.length) return false;
        ignoreAsyncResult(requestCanvasDelete(ids, "canvas-key"));
        return true;
      },
      walk: (direction) => {
        const current = selectedIds.length === 1 ? selectedIds[0] : focusedId ?? null;
        const next = walkCanvasGraph({ current, direction, nodes: nodesWithCanvasState, edges: visibleEdges, memory: walkMemoryRef.current });
        if (next) goToNode(next, nodesById);
        return true;
      },
      nudge: (dx, dy) => {
        const moving = commandIds.filter((id) => !id.startsWith("visual:") && !id.startsWith("annotation:"));
        const insideMoving = (node: Node) => {
          let parent = node.parentId;
          while (parent) {
            if (moving.includes(parent)) return true;
            parent = nodesById.get(parent)?.parentId;
          }
          return false;
        };
        const changes: NodeChange[] = moving.flatMap((id) => {
          const node = nodesById.get(id);
          if (!node || insideMoving(node) || !(isStepNode(node) || node.type === "groupContainer" || node.type === "forEachContainer")) return [];
          return [{ type: "position" as const, id, position: { x: node.position.x + dx, y: node.position.y + dy }, dragging: false }];
        });
        if (!changes.length) return false;
        handleNodesChange(changes);
        return true;
      },
      copy: () => clipboard("copy", selectedNodeIds.size > 0 && clipboardAvailability.copy),
      cut: () => clipboard("cut", selectedNodeIds.size > 0 && clipboardAvailability.copy && selectedCanDelete),
      paste: () => clipboard("paste", !structureLocked),
      duplicate: () => clipboard("duplicate", selectedNodeIds.size > 0 && clipboardAvailability.duplicate),
      undo: () => history(false),
      redo: () => history(true),
      addNote: structureLocked || annotationsBusy ? undefined : () => { closeAll(); onAddNote(); },
      groupSelection: structureLocked || !selectedNodeIds.size ? undefined : () => {
        const box = canvasMeasureRootRef.current?.getBoundingClientRect();
        openGroupSelection({ x: (box?.left ?? 0) + (box?.width ?? 0) / 2 - 120, y: (box?.top ?? 0) + 64 });
        return true;
      },
    };
    if (runCanvasKey(event.nativeEvent, context, onControl)) {
      event.preventDefault();
      event.stopPropagation();
    }
  };
  useEffect(() => { if (searchOpen) searchRef.current?.focus(); }, [searchOpen]);
  // Leaving with unapplied structure loses it; the author confirms first.
  const projectId = stringFromUnknownValue(searchParams.get("project_id")).trim();
  const builderDrawerHref = (drawer: string) => {
    const params = new URLSearchParams({ drawer });
    if (projectId) {
      params.set("project_id", projectId);
    }
    return `/registry/workflows/${encodeURIComponent(workflowId)}/builder?${params.toString()}`;
  };
  const backTarget = backHref || `/registry/workflows/${encodeURIComponent(workflowId)}`;
  // Canon Loader: a wait under 400ms paints no mark, so quick saves never flicker.
  const applyingLoader = useLoaderDelay(changeSet.status === "applying");
  const clipboardBusyShown = useLoaderDelay(clipboardBusy);
  const runningLoader = useLoaderDelay(Boolean(canvasTestRunAction));
  // A refused Apply names its reasons in the error itself; they are not listed twice.
  const issueMessages = changeSet.applyRefused
    ? []
    : changeSet.applyIssues.map((issue) => changeIssueMessage(issue, t)).filter(Boolean);
  const applyErrorMessage = applyRefusalMessage(changeSet.applyError, changeSet.applyRefused, t);
  const blockingIssueMessages = changeSet.blockingIssueMessages;
  const keptIssueMessages = changeSet.keptIssueMessages;
  let changeCheckNote = "";
  if (changeSet.hasChanges && changeSet.checkState === "checking") {
    changeCheckNote = t("react.workflows.change_check_running", "Checking the changes against the server…");
  } else if (changeSet.hasChanges && changeSet.checkState === "unavailable") {
    changeCheckNote = t("react.workflows.change_check_unavailable", "The server check is unavailable; these issues are a local estimate.");
  }
  const applyDisabled = annotationsBusy || !changeSet.hasChanges || changeSet.status === "applying" || changeSet.status === "conflict";
  const autoLayoutHelp = [
    t("react.workflows.auto_layout_shortcut", "Auto layout (Shift+A)"),
    hasManualLayout
      ? t("react.workflows.manual_placements_present", "Manual placements are saved on this canvas. Auto layout resets them.")
      : t("react.workflows.manual_placements_info", "Drag a step to place it manually. Auto layout restores the graph layout."),
  ].join(" · ");
  const selectedEdgeData = selectedEdge?.data;
  const errorCount = canvasGraph.validation_issues.filter((issue) => issue.severity === "error").length;
  const warningCount = canvasGraph.validation_issues.filter((issue) => issue.severity === "warning").length;
  const diagnosticsFacts = [errorCount ? t(errorCount === 1 ? "react.workflows.validation_error_one" : "react.workflows.validation_errors_count", errorCount === 1 ? "{count} error" : "{count} errors", { count: errorCount }) : "", warningCount ? t(warningCount === 1 ? "react.workflows.validation_warning_one" : "react.workflows.validation_warnings_count", warningCount === 1 ? "{count} warning" : "{count} warnings", { count: warningCount }) : ""].filter(Boolean).join(" · ");
  const diagnosticsSummary = changeSet.hasChanges
    ? diagnosticsFacts ? t("react.workflows.saved_validation_issues", "Saved workflow: {issues}", { issues: diagnosticsFacts }) : t("react.workflows.saved_workflow_valid", "Saved workflow valid")
    : diagnosticsFacts;
  // The selection bar shows from two selected nodes (Components2 › Selection bar).
  const selectionBarShown = selectedNodeIds.size >= 2;
  const edgeBar = selectedEdge && selectedEdgeData && (!structureLocked || isHookMemberRef(selectedEdgeData.edge.source))
    ? canvasEdgeNames(selectedEdgeData.edge, new Map(nodesWithCanvasState.map((node) => [node.id, node])), t, { source: selectedEdge.source, target: selectedEdge.target })
    : null;
  return (
    <section className="workflow-builder flex h-screen min-w-0 flex-col overflow-hidden bg-[var(--fs-bg-canvas)] text-[var(--fs-text-primary)]">
      <div className={joinClasses("workflow-canvas-header", changeSet.hasChanges ? "has-changes" : "")}>
        <div className="workflow-header-identity">
        <Button asChild variant="secondary" size="md" className="workflow-canvas-back">
          <Link to={backTarget}><ArrowLeft aria-hidden="true" />{t("react.workflows.back", "Back")}</Link>
        </Button>
        <button
          className="workflow-name"
          onClick={() => { closeAllPopovers(); openWorkflowSettingsFromCanvas(); }}
          type="button"
        >
          <Workflow aria-hidden="true" />
          <span className="workflow-name-label">{canvasGraph.workflow.display_name}</span>
          <Settings2 className="workflow-name-trail" aria-hidden="true" />
        </button>
        <span className="workflow-canvas-beta">
          {t("react.workflows.canvas_beta", "Canvas · beta")}</span>
        {headerContext}
        <div className="min-w-2 grow" />
        </div>
        <div className="workflow-header-actions">
        <Button variant="secondary" size="md" onClick={() => { closeAllPopovers(); openWorkflowInterfaceFromCanvas(); }}><Settings2 aria-hidden="true" />{t("react.workflows.interface", "Interface")}<CanvasInterfaceCounts graph={canvasGraph} /></Button>
        <span className="workflow-header-divider" aria-hidden="true" />
        {/* A protected canvas shows the change set only for edits inside a hook (B14). */}
        {renderWhen(!structureLocked || changeSet.hasChanges, (
          <>
        <div
          className={joinClasses(
            "workflow-structure-status",
            structureStatus.className,
          )}
          data-testid="structure-status"
          role="status"
          aria-live="polite"
          title={t("react.workflows.structure_status_hint", "Structure changes stay local until you apply them.")}
        >
          {choose(applyingLoader, <DsLoader variant="orbit-inline" />, <ListChecks className="h-3.5 w-3.5" />)}
          <span>{structureStatus.label}</span>
        </div>
        {renderWhen(changeSet.hasChanges, (
          <Button
            variant="secondary"
            size="md"
            disabled={changeSet.status === "applying"}
            onClick={() => changeSet.discard()}
            type="button"
          >
            <X aria-hidden="true" />
            {t("react.workflows.discard_changes", "Discard changes")}
          </Button>
        ))}
        <Button
          variant="primary"
          size="md"
          disabled={applyDisabled}
          onClick={() => ignoreAsyncResult(changeSet.apply())}
          type="button"
        >
          {choose(
            changeSet.status === "applying",
            t("react.workflows.applying_changes", "Applying…"),
            t("react.workflows.apply_changes", "Apply changes"),
          )}
        </Button>
          </>
        ))}
        {renderWhen(structureLocked, (
          <span
            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] px-2.5 py-1 text-[11px] text-[var(--fs-status-attention-ink)]"
            data-testid="structure-locked"
            aria-describedby={`workflow-protection-${workflowId}`}
            title={t("react.workflows.structure_locked_hint", "This workflow is protected by its extension. You can read it and connect your own workflows to its hooks.")}
          >
            {t("react.workflows.structure_locked", "Read-only")}
          </span>
        ))}
        <span className="workflow-header-divider" aria-hidden="true" />
        <Button variant="secondary" size="md" className="workflow-canvas-diagnostics" aria-label={`${t("react.workflows.diagnostics", "Diagnostics")}${diagnosticsSummary ? ` · ${diagnosticsSummary}` : ""}`} aria-expanded={showValidationPanel} onClick={() => { const next = !showValidationPanel; closeAllPopovers(); setShowValidationPanel(next); onCloseInspector(); }} title={t("react.workflows.open_canvas_validation_issues", "Open canvas validation issues")}>
          <ListChecks aria-hidden="true" />
          {!changeSet.hasChanges && errorCount ? <span className="workflow-diagnostics-error">{t(errorCount === 1 ? "react.workflows.validation_error_one" : "react.workflows.validation_errors_count", errorCount === 1 ? "{count} error" : "{count} errors", { count: errorCount })}</span> : null}
          {!changeSet.hasChanges && errorCount && warningCount ? <span aria-hidden="true">·</span> : null}
          {!changeSet.hasChanges && warningCount ? <span className="workflow-diagnostics-warning">{t(warningCount === 1 ? "react.workflows.validation_warning_one" : "react.workflows.validation_warnings_count", warningCount === 1 ? "{count} warning" : "{count} warnings", { count: warningCount })}</span> : null}
          {changeSet.hasChanges ? diagnosticsSummary : !errorCount && !warningCount ? t("react.workflows.diagnostics", "Diagnostics") : null}
        </Button>
        <div className="relative">
          <DsPopover
            label={t("react.workflows.more", "More")}
            open={showMoreMenu}
            portal
            onOpenChange={(open) => {
              if (!open) { setShowMoreMenu(false); return; }
              closeLocalPopovers(); setShowLegend(false); setShowValidationPanel(false); setShowSearchResults(false); toggleMoreMenu();
            }}
            trigger={(props) => renderMoreButton(props, t("react.workflows.more", "More"))}
          >
            <div className="grid">
              {openWorkflowHistoryFromCanvas ? <button className="block w-full rounded-xl px-3 py-2 text-left text-sm text-[var(--fs-text-primary)] hover:bg-[var(--fs-bg-surface-muted)]" onClick={() => { closeAllPopovers(); openWorkflowHistoryFromCanvas?.(); }} type="button">
                {t("react.workflows.history_tab", "History")}</button> : null}
              <button
                className="block w-full rounded-xl px-3 py-2 text-left text-sm text-[var(--fs-text-primary)] hover:bg-[var(--fs-bg-surface-muted)]"
                onClick={() => {
                  const next = !showLegend;
                  closeAllPopovers();
                  onCloseInspector();
                  setShowLegend(next);
                }}
                type="button"
              >
                {choose(showLegend, t("react.workflows.display_hide_legend", "Hide legend"), t("react.workflows.display_show_legend", "Show legend"))}
              </button>
              {renderWhen(Boolean(exportYamlHref), (
                <div><a className="flex items-center gap-2 rounded-xl px-3 py-2 text-sm text-[var(--fs-text-primary)] hover:bg-[var(--fs-bg-surface-muted)]" href={exportYamlHref}>
                  {exportYamlLabel}
                </a><p className="px-3 pb-2 text-xs text-[var(--fs-text-tertiary)]">{t("react.workflows.note_images_not_exported", "Note images are not included in exports. Upload them again after importing.")}</p></div>
              ))}
              <button className="block w-full rounded-xl px-3 py-2 text-left text-sm text-[var(--fs-text-primary)] hover:bg-[var(--fs-bg-surface-muted)]" onClick={() => { closeAllPopovers(); openWorkflowAutomationFromCanvas(); }} type="button">
                {t("registry.workflow_detail.builder.workflow_automation", "Workflow automation")}</button>
              <button
                className="block w-full rounded-xl px-3 py-2 text-left text-sm text-[var(--fs-text-primary)] hover:bg-[var(--fs-bg-surface-muted)]"
                onClick={() => {
                  setShowMiniMap((current) => !current);
                  setShowMoreMenu(false);
                }}
                type="button"
              >
                {choose(showMiniMap, t("react.workflows.display_hide_minimap", "Hide minimap"), t("react.workflows.display_show_minimap", "Show minimap"))}
              </button>
              <button
                className="block w-full rounded-xl px-3 py-2 text-left text-sm text-[var(--fs-text-primary)] hover:bg-[var(--fs-bg-surface-muted)]"
                onClick={() => {
                  onToggleAdvancedMode();
                  setShowMoreMenu(false);
                }}
                type="button"
              >
                {choose(advancedMode, t("react.workflows.display_advanced_mode_on", "Advanced mode on"), t("react.workflows.display_advanced_mode_off", "Advanced mode off"))}
              </button>
            </div>
          </DsPopover>
        </div>
        </div>
      </div>

      {/* The builder's one notice for a workflow that cannot run: the published
          warning alert under the toolbar, worded for a person when a required
          extension is missing, else the reason as the backend gives it. */}
      {renderWhen(Boolean(missingExtensions || effectiveAvailabilityNotice), (
        <div className="flex-none border-b border-[var(--fs-border-subtle)] px-4 py-2" data-workflow-availability-notice="true">
          {missingExtensions ? <WorkflowMissingExtensionNotice model={missingExtensions} /> : (
            <DsAlert title={t("react.workflows.workflow_unavailable", "Workflow unavailable")} tone="warning">
              <p className="break-words">{effectiveAvailabilityNotice}</p>
            </DsAlert>
          )}
        </div>
      ))}

      {renderWhen(changeSet.status === "conflict", (
        <div
          className="flex flex-none flex-wrap items-center justify-between gap-2 border-b border-[var(--fs-status-danger-border)] bg-[var(--fs-status-danger-bg)] px-4 py-2 text-sm text-[var(--fs-status-danger)]"
          role="alert"
        >
          <span>
            {changeSet.checkpoint?.held
              ? t("react.workflows.edit_session_conflict", "A newer version of this workflow was saved elsewhere. Your copy and its Undo history are kept; compare the versions and choose which to keep.")
              : t("react.workflows.change_set_conflict", "The workflow changed after it was loaded. Your unapplied changes are kept; reload the workflow to review them against the new version.")}
          </span>
          <span className="flex flex-wrap gap-2">
            {compareVersions ? (
              <button
                className="rounded-lg border border-[var(--fs-status-danger-border)] px-3 py-1 text-xs text-[var(--fs-status-danger)]"
                onClick={compareVersions}
                type="button"
              >
                {t("react.workflows.edit_session_compare_versions", "Compare versions")}
              </button>
            ) : null}
            {/* A held local copy is never replayed onto the newer server version. */}
            {changeSet.checkpoint?.held ? null : (
              <button
                className="rounded-lg border border-[var(--fs-status-danger-border)] px-3 py-1 text-xs text-[var(--fs-status-danger)]"
                onClick={() => ignoreAsyncResult(changeSet.reload())}
                type="button"
              >
                {t("react.workflows.reload_workflow", "Reload workflow")}
              </button>
            )}
          </span>
        </div>
      ))}



      {renderWhen(
        Boolean(
          changeSet.applyError ||
            issueMessages.length ||
            blockingIssueMessages.length ||
            keptIssueMessages.length ||
            changeCheckNote,
        ),
        (
          <div
            className="flex-none space-y-1 border-b border-[var(--fs-status-attention-border)] bg-[var(--fs-bg-surface)] px-4 py-2 text-xs"
            data-testid="structure-issues"
          >
            {renderWhen(Boolean(changeSet.applyError), (
              <p className="text-[var(--fs-status-danger)]" data-testid="change-set-apply-error" role="alert">{applyErrorMessage}</p>
            ))}
            {issueMessages.slice(0, 6).map((message, index) => (
              <p className="text-[var(--fs-status-attention-ink)]" key={`${message}:${index}`}>{message}</p>
            ))}
            {renderWhen(Boolean(changeCheckNote), (
              <p className="text-[var(--fs-text-tertiary)]" data-testid="change-check-status">{changeCheckNote}</p>
            ))}
            {renderWhen(blockingIssueMessages.length > 0, (
              <div data-testid="change-check-blocking">
                <p className="font-medium text-[var(--fs-status-danger)]">{t("react.workflows.change_check_blocking", "Apply will be refused:")}</p>
                {blockingIssueMessages.slice(0, 6).map((message, index) => (
                  <p className="text-[var(--fs-status-danger)]" key={`${message}:${index}`}>{message}</p>
                ))}
              </div>
            ))}
            {renderWhen(keptIssueMessages.length > 0, (
              <div data-testid="change-check-kept">
                <p className="font-medium text-[var(--fs-status-attention-ink)]">{t("react.workflows.change_check_kept", "Issues the saved definition keeps:")}</p>
                {keptIssueMessages.slice(0, 6).map((message, index) => (
                  <p className="text-[var(--fs-status-attention-ink)]" key={`${message}:${index}`}>{message}</p>
                ))}
              </div>
            ))}
          </div>
        ),
      )}

      {annotationError ? <div className="flex items-center gap-3 border-b border-[var(--fs-status-danger-border)] px-4 py-2 text-sm text-[var(--fs-status-danger)]"><p role="alert">{annotationError}</p><button type="button" onClick={onReloadNotes}>{t("react.workflows.reload_notes", "Reload saved notes")}</button></div> : null}
      {deleteGuardNotice ? <div className="flex items-center gap-3 border-b border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] px-4 py-2 text-sm text-[var(--fs-text-primary)]" role={deleteGuardNotice.state === "checking" ? "status" : "alert"}>
        <p>{deleteGuardNotice.message}</p>
        {deleteGuardNotice.state === "unknown" && onRetryGraphDelete ? <Button size="sm" variant="secondary" onClick={onRetryGraphDelete}>{t("react.workflows.retry", "Retry")}</Button> : null}
        {deleteGuardNotice.canOpenAutomation && onOpenDeleteAutomation ? <Button size="sm" variant="secondary" onClick={onOpenDeleteAutomation}>{t("react.workflows.open_automation", "Open Automation")}</Button> : null}
      </div> : null}
      {clipboardBusyShown ? <DsAlert title={t("react.workflows.clipboard_working", "Copying selection…")} /> : null}
      {clipboardNotice ? <DsAlert title={clipboardNotice.title} tone={clipboardNotice.tone} /> : null}

      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- Keyboard entry for this named composite graph surface preserves Tab focus and graph shortcuts. */}
        <div ref={canvasMeasureRootRef} role="region" aria-label={t("react.workflows.workflow_canvas", "Workflow canvas")} tabIndex={0} onPointerDownCapture={(event) => { consumeConnectionDropClick(); if (!event.currentTarget.contains(document.activeElement)) event.currentTarget.focus({ preventScroll: true }); }} onClickCapture={(event) => { if (event.target instanceof Element && event.target.closest(".react-flow__node")) { closeLocalPopovers(); if (event.target.closest(".workflow-container-header")) setSelectedEdgeId(null); } }} onKeyDownCapture={handleCanvasKeyDown} onPointerMove={(event) => { pointerRef.current = { x: event.clientX, y: event.clientY }; }} onPointerLeave={() => { pointerRef.current = null; }} className="workflow-canvas-stage relative min-h-0 flex-1 overflow-hidden bg-[var(--fs-bg-canvas)] focus-visible:outline-2 focus-visible:outline-[var(--fs-focus-ring-color)]">
          <CanvasCardCommandsProvider host={menuHost}>
          <ReactFlow<Node, Edge>
            defaultViewport={{ x: 24, y: 24, zoom: 1 }}
            deleteKeyCode={null}
            edges={shownEdges}
            edgeTypes={canvasEdgeTypes}
            minZoom={0.25}
            maxZoom={2}
            panOnScroll
            zoomOnScroll={false}
            zoomOnPinch
            zoomActivationKeyCode={["Control", "Meta"]}
            panActivationKeyCode="Space"
            selectionOnDrag
            selectionMode={SelectionMode.Partial}
            multiSelectionKeyCode="Shift"
            onSelectionStart={() => { boxSelectingRef.current = true; setSelectedEdgeId(null); }}
            onSelectionEnd={() => { boxSelectingRef.current = false; }}
            snapToGrid
            snapGrid={[CANVAS_GRID, CANVAS_GRID]}
            onMoveStart={(event) => { if (event) { onUserViewportInteraction?.(); runFollow.pause(); } }}
            onMove={(_, viewport) => setGridGap(viewport.zoom < 0.5 ? CANVAS_GRID * 4 : viewport.zoom < 0.8 ? CANVAS_GRID * 2 : CANVAS_GRID)}
            nodeTypes={canvasNodeTypes}
            nodes={nodesWithCanvasState.map((node) => node.type === "canvasNote" ? node : { ...node, selected: selectedNodeIds.has(node.id) })}
            nodesConnectable={changeSet.status !== "applying" && !structureLocked}
            nodesDraggable={!structureLocked}
            onConnect={handleConnect}
            isValidConnection={isValidConnection}
            connectionLineComponent={CanvasConnectionPreview}
            onConnectEnd={handleConnectEnd}
            onConnectStart={handleConnectStart}
            onEdgeClick={(event, edge) => {
              event.stopPropagation();
              if (edge.selectable === false) return;
              setSelectedNodeIds(new Set());
              setSelectedEdgeId(edge.id);
            }}
            onEdgeMouseEnter={(_, edge) => setHoveredEdgeId(edge.id)}
            onEdgeMouseLeave={() => setHoveredEdgeId(null)}
            onEdgesChange={(changes) => handleEdgesChange(boxSelectingRef.current ? changes.filter((change) => change.type !== "select") : changes)}
            onInit={(instance) => { setViewportInstance(instance); setReactFlowInstance(instance); }}
            onNodeContextMenu={(event, node) => {
              if (node.type === "canvasNote" || node.type === "workflowStart" || node.id.startsWith("visual:")) return;
              event.preventDefault(); setSelectedEdgeId(null); closeAllPopovers();
              menus.openNodeMenu(node.id, { x: event.clientX, y: event.clientY });
            }}
            onPaneContextMenu={(event) => {
              event.preventDefault(); setSelectedEdgeId(null); closeAllPopovers();
              menus.openPaneMenu({ x: event.clientX, y: event.clientY });
            }}
            onNodeMouseEnter={(_, node) => handleConnectionTargetChange(node.type === "canvasNote" ? null : node.id)}
            onNodeMouseLeave={() => handleConnectionTargetChange(null)}
            onNodeClick={(event, node) => {
              setSelectedEdgeId(null);
              runFollow.pause();
              if (consumeCanvasNodeClickSuppression()) {
                return;
              }
              if (isSecondaryNodeAction(event.target, event)) {
                return;
              }
              // Shift+click adds the node to the selection or takes it out; no editor opens (Canvas › Selecting).
              if (event.shiftKey) return;
              openCanvasNode(node);
            }}
            onNodeDragStart={(_, node) => {
              if (isStepNode(node) || node.type === "groupContainer" || node.type === "inlineLoop") {
                suppressCanvasNodeClick(1200);
                setSelectedEdgeId(null);
              }
            }}
            onNodesChange={(changes) => {
              if (changes.some((change) => change.type === "select")) setSelectedNodeIds((current) => {
                const next = new Set(current);
                changes.forEach((change) => { if (change.type === "select") { if (change.selected) next.add(change.id); else next.delete(change.id); } });
                return next;
              });
              handleNodesChange(changes);
            }}
            onNodeDragStop={(_, node) => handleNodeDragStop(node)}
            onPaneClick={(event) => {
              if (consumeConnectionDropClick(event)) return;
              setSelectedEdgeId(null);
              closeTransientSurfaces();
              setSearchOpen(false);
              setGroupMenuOpen(false);
              setSelectedNodeIds(new Set());
              onCloseInspector();
            }}
            panOnDrag={[1]}
            proOptions={{ hideAttribution: true }}
          >
            <CanvasControls inspectorWidth={inspectorWidth} showMiniMap={showMiniMap}>{(compact) => <>
              {renderWhen(showMiniMap, <CanvasMiniMap nodes={nodesWithCanvasState} onUserViewportInteraction={onUserViewportInteraction} />)}
              {canvasRunBar && canvasRunBar.selection.kind !== "empty" ? <div className="workflow-canvas-runbar"><CanvasRunBar {...canvasRunBar} followRunAction={runFollow.paused ? { onInvoke: runFollow.resume } : undefined} /></div> : null}
            <Panel position="bottom-center" role="toolbar" className="workflow-canvas-toolbar" data-compact={compact ? "true" : "false"} aria-label={t("react.workflows.canvas_tools", "Canvas tools")}>
              {!structureLocked ? <CanvasToolbarHint compact={compact} label={t("react.workflows.add_step_key", "Add a step (A)")}>{(descriptionId) => <Button variant="ghost" size="md" className="workflow-toolbar-caption-action" aria-label={t("react.workflows.canvas_toolbar_add_step", "Add step")} aria-describedby={descriptionId} aria-keyshortcuts="A" title={compact ? undefined : t("react.workflows.add_step_key", "Add a step (A)")} onClick={() => { closeAllPopovers(); requestStep(); }}><SquarePlus aria-hidden="true" /><span className="workflow-toolbar-caption">{t("react.workflows.canvas_toolbar_step", "Step")}</span></Button>}</CanvasToolbarHint> : null}
              {!structureLocked ? <div className="relative"><CanvasToolbarHint compact={compact} label={t("react.workflows.canvas_toolbar_add_group", "Add group")}>{(descriptionId) => <Button variant="ghost" size="md" className="workflow-toolbar-caption-action" aria-label={t("react.workflows.canvas_toolbar_add_group", "Add group")} aria-describedby={descriptionId} aria-expanded={groupMenuOpen} onClick={() => { const next = !groupMenuOpen; closeAllPopovers(); setGroupMenuOpen(next); }}><Plus aria-hidden="true" /><span className="workflow-toolbar-caption">{t("react.workflows.group", "Group")}</span></Button>}</CanvasToolbarHint>{groupMenuOpen ? <div className="workflow-toolbar-popover workflow-toolbar-group-menu" role="menu" aria-label={t("react.workflows.canvas_toolbar_add_group", "Add group")}>
                <button className="workflow-layout-option" role="menuitem" type="button" aria-label={t("react.workflows.group", "Group")} onClick={() => { closeAllPopovers(); openNewGroupFromCanvas({ containerKind: "group", bodyKind: "sequence" }); }}><span><span className="workflow-layout-option-name">{t("react.workflows.group", "Group")}</span><span className="workflow-layout-option-hint">{t("react.workflows.palette_desc_group", "Steps that belong together. Can become a child workflow.")}</span></span><span /></button>
                <button className="workflow-layout-option" role="menuitem" type="button" aria-label={t("react.workflows.group_kind_parallel", "Parallel lanes")} onClick={() => { closeAllPopovers(); openNewGroupFromCanvas({ containerKind: "group", bodyKind: "lanes" }); }}><span><span className="workflow-layout-option-name">{t("react.workflows.group_kind_parallel", "Parallel lanes")}</span><span className="workflow-layout-option-hint">{t("react.workflows.palette_desc_parallel_lanes", "A group whose lanes run side by side.")}</span></span><span /></button>
              </div> : null}</div> : null}
              {!structureLocked ? <CanvasNoteToolbarButton compact={compact} disabled={annotationsBusy} canvasRootRef={canvasMeasureRootRef} zoom={() => viewportInstance?.getZoom() ?? 1} onBeforePlace={closeAllPopovers} onPlace={onAddNote} /> : null}
              {!structureLocked ? <span className="workflow-toolbar-divider" aria-hidden="true" /> : null}
              <div className="relative"><IconButton className="workflow-toolbar-icon" icon={Search} label={t("react.workflows.search_canvas_shortcut", "Search canvas (⌘/Ctrl+F)")} aria-expanded={searchOpen} aria-keyshortcuts="Meta+F Control+F" onClick={() => { const next = !searchOpen; closeAllPopovers(); setSearchOpen(next); }} />
                {searchOpen ? <div className="workflow-toolbar-popover"><label className="sr-only" htmlFor="workflow-builder-search">{t("react.workflows.search_workflow_canvas", "Search workflow canvas")}</label><input ref={searchRef} id="workflow-builder-search" className="fs-input w-full" value={searchQuery} autoComplete="off" placeholder={t("react.workflows.search_step_or_group", "Search step or group")} onChange={(event) => { setSearchQuery(event.target.value); setShowSearchResults(Boolean(event.target.value.trim())); }} onKeyDown={(event) => { if (event.key === "Escape") { setSearchOpen(false); }
                    if (event.key === "Enter" && searchResults.length) onUserViewportInteraction?.();
                    handleSearchKeyDown(event); }} />
                  {showSearchResults ? searchResults.map((result, index) => <button className={joinClasses("block w-full rounded px-2 py-2 text-left text-xs", index === searchIndex ? "bg-[var(--fs-bg-surface-muted)]" : "")} key={`${result.kind}:${result.id}`} onClick={() => { onUserViewportInteraction?.(); closeAllPopovers(); focusSearchResult(result); }} onMouseEnter={() => setSearchIndex(index)} type="button"><span className="block truncate">{result.label}</span><span className="block truncate text-[var(--fs-text-tertiary)]">{result.secondary}</span></button>) : null}
                </div> : null}
              </div>
        {renderWhen(!structureLocked, extras ? (
          <CanvasLayoutControl mode={extras.arrangeMode} onChoose={extras.onArrangeChange} onAutoLayout={() => { onUserViewportInteraction?.(); closeAllPopovers(); handleAutoLayout(); }} onOpen={closeAllPopovers} help={autoLayoutHelp} />
        ) : (
        <span className="fs-icon-button-wrap" onMouseEnter={() => setAutoTooltip(true)} onMouseLeave={() => setAutoTooltip(false)}><button
          className="fs-button ghost workflow-toolbar-caption-action"
          aria-label={t("react.workflows.auto_layout", "Auto layout")}
          aria-describedby={autoTooltip ? "workflow-auto-layout-tooltip" : undefined}
          onFocus={() => setAutoTooltip(true)} onBlur={() => setAutoTooltip(false)}
          onClick={() => { onUserViewportInteraction?.(); closeAllPopovers(); handleAutoLayout(); }}
          title={autoLayoutHelp}
          aria-keyshortcuts="Shift+A"
          type="button"
        >
          <CanvasAutoLayoutIcon />
          <span className="workflow-toolbar-caption">{t("react.workflows.auto_layout", "Auto layout")}</span>
        </button>{autoTooltip ? <span id="workflow-auto-layout-tooltip" className="fs-shell-tooltip" role="tooltip">{autoLayoutHelp}</span> : null}</span>
        ))}


        <button
          aria-label={failurePathButtonLabel(failurePathCount, showFailurePaths, t)}
          className="fs-icon-button workflow-toolbar-icon workflow-failure-paths"
          aria-pressed={showFailurePaths}
          title={failurePathButtonLabel(failurePathCount, showFailurePaths, t)}
          disabled={!failurePathCount}
          onClick={() => {
            setShowFailurePaths((current) => {
              const next = !current;
              setGraphEditNotice({
                label: next
                  ? t("react.workflows.showing_count_failure_paths", "Showing {count} failure paths.", { count: failurePathCount })
                  : t("react.workflows.failure_paths_hidden", "Failure paths hidden."),
                tone: "success",
              });
              return next;
            });
          }}
          type="button"
        >
          <BadgeAlert width={16} height={16} aria-hidden="true" />
          <span className="workflow-failure-count">{failurePathCount}</span>
        </button>
              <span className="workflow-toolbar-divider" />
              <div className="contents" onClickCapture={() => { onUserViewportInteraction?.(); closeAllPopovers(); }}><CanvasViewportControls /></div>
              <span className="workflow-toolbar-divider" />
              {canvasTestRunAction ? <CanvasToolbarHint compact={compact} label={t("react.workflows.run_bar_running", "Running")}>{(descriptionId) => <Button variant="secondary" size="md" type="button" className="workflow-toolbar-test-run workflow-toolbar-caption-action" aria-label={t("react.workflows.run_bar_running", "Running")} aria-describedby={descriptionId} disabled={canvasTestRunAction.busy || canvasTestRunAction.disabled} onClick={() => { closeAllPopovers(); ignoreAsyncResult(Promise.resolve(canvasTestRunAction.onInvoke())); }}>{runningLoader ? <DsLoader variant="orbit-inline" /> : <Play aria-hidden="true" />}<span className="workflow-toolbar-caption">{t("react.workflows.run_bar_running", "Running")}</span></Button>}</CanvasToolbarHint>
                : <CanvasToolbarHint compact={compact} label={t("registry.workflow_detail.run_lab.tab.test_run", "Test Run")}>{(descriptionId) => <Button asChild variant="secondary" size="md" className="workflow-toolbar-test-run workflow-toolbar-caption-action"><Link to={builderDrawerHref("workflow-test-run")} aria-label={t("registry.workflow_detail.run_lab.tab.test_run", "Test Run")} aria-describedby={descriptionId} onClick={closeAllPopovers}><Play aria-hidden="true" /><span className="workflow-toolbar-caption">{t("registry.workflow_detail.run_lab.tab.test_run", "Test Run")}</span></Link></Button>}</CanvasToolbarHint>}
            </Panel>
            </>}</CanvasControls>
            {slotSource && nextSlots.length ? <ViewportPortal>
              {nextSlots.map((slot) => <div key={`${slot.outcome.name}:${slot.outcome.case_id || ""}`}>
                <svg aria-hidden="true" className="pointer-events-none absolute overflow-visible" width={1} height={1} style={{ left: 0, top: 0 }}>
                  <path d={slot.path} fill="none" stroke="var(--fs-border-default)" strokeWidth={1.5} strokeDasharray="4 4" />
                </svg>
                <button className="workflow-next-slot nodrag nopan nowheel absolute" style={{ left: slot.rect.x, top: slot.rect.y, width: slot.rect.width, height: slot.rect.height, pointerEvents: "auto" }} type="button" aria-label={nextSlotLabel(slot, t)} onPointerDown={(event) => event.stopPropagation()} onClick={(event) => { event.stopPropagation(); closeAllPopovers(); onAppendAtSlot?.(slotSource, slot); }}>
                  <Plus aria-hidden="true" className="size-4" />{nextSlotLabel(slot, t)}
                </button>
              </div>)}
            </ViewportPortal> : null}
            <WorkflowCanvasGrid gap={gridGap} />
          </ReactFlow>
          {selectionBarShown ? <CanvasSelectionBar count={selectedNodeIds.size}
            zoomDisabled={!viewportInstance}
            onZoom={() => { onUserViewportInteraction?.(); if (viewportInstance) ignoreAsyncResult(fitCanvasToNodes(viewportInstance, canvasMotionDuration(), canvasMeasureRootRef.current?.getBoundingClientRect(), "selection")); }}
            onCopy={requestClipboardCommand ? () => { ignoreAsyncResult(requestClipboardCommand("copy")); } : undefined}
            copyDisabled={!clipboardAvailability.copy || clipboardBusy}
            onDuplicate={requestClipboardCommand ? () => { ignoreAsyncResult(requestClipboardCommand("duplicate")); } : undefined}
            duplicateDisabled={!clipboardAvailability.duplicate || clipboardBusy}
            onDelete={() => { ignoreAsyncResult(requestCanvasDelete(selectedIds, "canvas-key")); }}
            deleteDisabled={!selectedCanDelete}
            onClear={() => setSelectedNodeIds(new Set())}>
            {!structureLocked ? <Button size="sm" variant="ghost" title={t("react.workflows.group_selection_key", "Group… (⌘⇧G)")} aria-keyshortcuts="Meta+Shift+G Control+Shift+G" aria-haspopup="menu" onClick={(event) => openGroupSelection(event.currentTarget)}>{t("react.workflows.group_selection", "Group…")}</Button> : null}
          </CanvasSelectionBar> : null}
          {edgeBar ? <CanvasConnectionBar title={edgeBar.title} ids={edgeBar.ids} draft={Boolean(selectedEdgeData?.draft)}
            onInsert={handleInsertOnSelectedEdge} onRemove={handleDeleteSelectedEdge} onClose={() => setSelectedEdgeId(null)} /> : null}
          <CanvasToast notice={graphEditNotice} onDismiss={dismissGraphEditNotice} held={toastHeld} low={selectionBarShown || Boolean(edgeBar)} />
          </CanvasCardCommandsProvider>
          {menus.element}
        </div>
        {renderWhen(showValidationPanel || showLegend, (
          <aside className="flex w-[320px] shrink-0 flex-col border-l border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)]">
            {choose(showValidationPanel, (
              <ValidationPanel
                graph={canvasGraph}
                issues={canvasGraph.validation_issues}
                onIssueClick={(issue) => {
                  setShowValidationPanel(false);
                  onValidationIssue(issue);
                }}
              />
            ), (
              <LegendPanel />
            ))}
          </aside>
        ))}
      </div>
    </section>
  );
}
