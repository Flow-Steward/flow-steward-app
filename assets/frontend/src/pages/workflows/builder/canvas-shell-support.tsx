import type { MissingExtensionNoticeModel } from "@/pages/workflows/shared/missing-extension";
import { Background, MiniMap, Panel, useReactFlow, useViewport, type Connection, type OnConnectStart, type OnConnectEnd, type IsValidConnection, type Edge, type EdgeChange, type Node, type NodeChange, type ReactFlowInstance } from "@xyflow/react";
import { useState, useRef, useCallback, useLayoutEffect, useId, type ReactNode, type RefObject } from "react";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { chooseCanvasMinimapRect } from "./canvas-minimap-placement";
import type { CanvasRunBarAction, CanvasRunBarProps } from "./canvas-run-bar";
import type { CanvasNextStepSlot } from "./canvas-viewport";
import type { CanvasNoteDropPoint } from "./canvas-notes";
import type { WorkflowCanvasGraph, WorkflowCanvasIssue } from "@/pages/workflows/shared/api";
import type { GraphEditNotice, InteractiveCanvasEdgeData, SearchResult, CanvasPosition } from "./canvas-core";
import type { CanvasCreationSelection, CanvasClipboardVisualSelection, CanvasDeleteNotice, CanvasPasteTarget, NewContainerDefaults } from "./canvas-types";
import type { CanvasShellExtras } from "./canvas-context-menus";
import type { WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";

type StateSetter<T> = (value: T | ((current: T) => T)) => void;

/** Decorative only: keep the existing unzoomed dot diameter when zooming out. */
export function WorkflowCanvasGrid({ gap }: Readonly<{ gap: number }>) {
  const { zoom } = useViewport();
  const scale = Number.isFinite(zoom) && zoom > 0 ? zoom : 1;
  const size = Math.max(1.2, 1.2 / scale);
  return <Background color="var(--fs-workflow-grid-dot-color)" gap={gap} size={size} />;
}

export type CanvasShellProps = {
  annotationError: string;
  annotationsBusy: boolean;
  /** Adds a note in the middle of the view, or where a dragged Note was dropped. */
  onAddNote: (point?: CanvasNoteDropPoint) => void;
  onReloadNotes: () => void;
  handleNodeDragStop: (node: Node) => void;
  advancedMode: boolean;
  backHref?: string;
  canvasGraph: WorkflowCanvasGraph;
  canvasMeasureRootRef: RefObject<HTMLDivElement | null>;
  /** Actual user camera gestures invalidate an older clipboard reveal. */
  onUserViewportInteraction?: () => void;
  changeSet: WorkflowChangeSetController;
  closeTransientSurfaces: () => void;
  effectiveAvailabilityNotice: string;
  missingExtensions?: MissingExtensionNoticeModel | null;
  /** Context shown in the header after the workflow name (the hooks' project, R2-10). */
  headerContext?: ReactNode;
  exportYamlHref?: string;
  exportYamlLabel: string;
  failurePathCount: number;
  focusSearchResult: (result: SearchResult) => void;
  graphEditNotice: GraphEditNotice | null;
  /** A connection drag keeps its guidance toast on screen. */
  toastHeld?: boolean;
  /** An open step or group editor follows the keyboard walk. */
  inspectorOpen?: boolean;
  deleteGuardNotice: CanvasDeleteNotice | null;
  onRetryGraphDelete?: () => void;
  onOpenDeleteAutomation?: () => void;
  handleAutoLayout: () => void;
  handleConnect: (connection: Connection) => void;
  handleConnectEnd: OnConnectEnd;
  handleConnectStart: OnConnectStart;
  consumeConnectionDropClick: (event?: Pick<MouseEvent, "clientX" | "clientY" | "detail">) => boolean;
  isValidConnection: IsValidConnection;
  handleConnectionTargetChange: (nodeId: string | null) => void;
  handleDeleteSelectedEdge: () => void;
  onCommandSelectionChange: (ids: readonly string[]) => void;
  onClipboardSnapshotChange?: (snapshot: { ids: readonly string[]; worldPositions: ReadonlyMap<string, CanvasPosition>; duplicateOrigin: CanvasPosition } | null) => void;
  requestClipboardCommand?: (command: "copy" | "cut" | "paste" | "duplicate", pasteTarget?: CanvasPasteTarget | null) => Promise<void>;
  clipboardBusy?: boolean;
  clipboardAvailability?: { copy: boolean; duplicate: boolean };
  clipboardNotice?: { title: string; tone: "danger" | "neutral" } | null;
  clipboardSelection?: CanvasClipboardVisualSelection | null;
  commandViewKey: string;
  commandClearNonce: number;
  requestCanvasDelete: (ids: readonly string[], source: "canvas-key") => Promise<boolean>;
  resolveCommandOwner: (ids: readonly string[]) => WorkflowChangeSetController | null;
  /**
   * Undo (redo=false) or Redo of the latest action across the canvas's history
   * owners (Canvas › Undo). False when there is nothing to do.
   */
  runHistory?: (redo: boolean) => boolean;
  handleEdgesChange: (changes: EdgeChange[]) => void;
  handleInsertOnSelectedEdge: () => void;
  handleNodesChange: (changes: NodeChange[]) => void;
  handleSearchKeyDown: (event: React.KeyboardEvent<HTMLInputElement>) => void;
  hasManualLayout: boolean;
  nodesWithCanvasState: Node[];
  creationSelection?: CanvasCreationSelection | null;
  nextSlotTemplate: Node;
  onAppendAtSlot?: (source: Node, slot: CanvasNextStepSlot) => void;
  onCloseInspector: () => void;
  inspectorWidth: number;
  canvasRunBar?: CanvasRunBarProps;
  canvasTestRunAction?: CanvasRunBarAction;
  onToggleAdvancedMode: () => void;
  onValidationIssue: (issue: WorkflowCanvasIssue) => void;
  openNewGroupFromCanvas: (defaults: NewContainerDefaults) => void;
  openNewStepFromCanvas: (context?: { groupId?: string; laneId?: string }) => void;
  openWorkflowAutomationFromCanvas: () => void;
  openWorkflowSettingsFromCanvas: () => void;
  openWorkflowInterfaceFromCanvas: () => void;
  openWorkflowHistoryFromCanvas?: () => void;
  /** The conflict bar's "Compare versions" (edit session). */
  compareVersions?: () => void;
  searchIndex: number;
  searchQuery: string;
  searchResults: SearchResult[];
  selectedEdge: Edge<InteractiveCanvasEdgeData> | undefined;
  setGraphEditNotice: StateSetter<GraphEditNotice | null>;
  setHoveredEdgeId: StateSetter<string | null>;
  setReactFlowInstance: StateSetter<ReactFlowInstance | null>;
  setSearchIndex: StateSetter<number>;
  setSearchQuery: StateSetter<string>;
  setSelectedEdgeId: StateSetter<string | null>;
  setShowAddMenu: StateSetter<boolean>;
  setShowFailurePaths: StateSetter<boolean>;
  setShowLegend: StateSetter<boolean>;
  setShowMiniMap: StateSetter<boolean>;
  setShowMoreMenu: StateSetter<boolean>;
  setShowSearchResults: StateSetter<boolean>;
  setShowValidationPanel: StateSetter<boolean>;
  showAddMenu: boolean;
  showFailurePaths: boolean;
  showLegend: boolean;
  showMiniMap: boolean;
  showMoreMenu: boolean;
  showSearchResults: boolean;
  showValidationPanel: boolean;
  structureStatus: { className: string; label: string };
  toggleAddMenu: () => void;
  /** A protected author graph: shown read-only, without Add, connect or apply. */
  structureLocked?: boolean;
  toggleMoreMenu: () => void;
  totalWidth: number;
  visibleEdges: Edge<InteractiveCanvasEdgeData>[];
  workflowId: string;
  /** Menus, Layout › Arrange and placement at a point (CANVAS-B). */
  extras?: CanvasShellExtras;
};

/** "3 inputs · 1 output": what the Interface button says beside its name. */
export function CanvasInterfaceCounts({ graph }: Readonly<{ graph: CanvasShellProps["canvasGraph"] }>) {
  const { t } = useI18n();
  const contract = graph.interface_contract;
  const schemaInputs = Object.keys((contract.input_schema?.properties as Record<string, unknown> | undefined) || {}).length;
  const inputs = contract.input_mode === "json_schema" ? schemaInputs : (contract.expected_inputs || []).length;
  // Returned files are outputs too.
  const outputs = (graph.output_contract.expected_outputs || []).length + (graph.output_contract.returned_files || []).length;
  const counts = [
    inputs ? t(inputs === 1 ? "react.workflows.interface_count_input_one" : "react.workflows.interface_count_inputs", inputs === 1 ? "{count} input" : "{count} inputs", { count: inputs }) : "",
    outputs ? t(outputs === 1 ? "react.workflows.interface_count_output_one" : "react.workflows.interface_count_outputs", outputs === 1 ? "{count} output" : "{count} outputs", { count: outputs }) : "",
  ].filter(Boolean).join(" · ");
  return counts ? <span className="text-[var(--fs-text-tertiary)]">{counts}</span> : null;
}

/** Reference geometry is local anatomy; all interaction roles come from the DS. */
export function CanvasAutoLayoutIcon() {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect width="18" height="7" x="3" y="3" rx="1" /><rect width="9" height="7" x="3" y="14" rx="1" /><rect width="5" height="7" x="16" y="14" rx="1" /></svg>;
}

/** Use ReactFlow's real world bounds and pannable viewport, including parent frames. */
export function CanvasMiniMap({ nodes, onUserViewportInteraction }: Readonly<{ nodes: Node[]; onUserViewportInteraction?: () => void }>) {
  const { t } = useI18n();
  const instance = useReactFlow();
  const bounds = instance.getNodesBounds(nodes.filter((node) => !node.hidden));
  const height = bounds.width > 0 ? Math.min(168, Math.max(1, Math.round(168 * bounds.height / bounds.width))) : 168;
  return <Panel position="bottom-right" className="workflow-canvas-minimap" onPointerDownCapture={onUserViewportInteraction}>
    <MiniMap
      ariaLabel={t("react.workflows.canvas_minimap", "Canvas minimap")}
      onClick={(_, point) => { onUserViewportInteraction?.(); ignoreAsyncResult(instance.setCenter(point.x, point.y, { zoom: instance.getZoom(), duration: 0 })); }}
      maskColor="color-mix(in srgb, var(--fs-bg-canvas) 60%, transparent)"
      nodeBorderRadius={2}
      nodeColor="var(--fs-border-default)"
      pannable
      style={{ height, width: 168 }}
    />
  </Panel>;
}

/** Same native action and label in either presentation; only the caption yields. */
export function CanvasToolbarHint({ label, compact, children }: Readonly<{
  label: string;
  compact: boolean;
  children: (descriptionId: string | undefined) => ReactNode;
}>) {
  const id = useId();
  const [focused, setFocused] = useState(false);
  const [hovered, setHovered] = useState(false);
  const visible = compact && (focused || hovered);
  return <span className="fs-icon-button-wrap" onFocusCapture={() => setFocused(true)} onBlurCapture={() => setFocused(false)} onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}>
    {children(visible ? id : undefined)}
    {visible ? <span id={id} className="fs-shell-tooltip" role="tooltip">{label}</span> : null}
  </span>;
}

/** Only the controls yield to the drawer; the graph viewport stays unchanged. */
export function CanvasControls({ inspectorWidth, showMiniMap, children }: Readonly<{
  inspectorWidth: number;
  showMiniMap: boolean;
  children: (compact: boolean) => ReactNode;
}>) {
  const regionRef = useRef<HTMLDivElement>(null);
  const measuring = useRef(false);
  const [compact, setCompact] = useState(false);

  const updatePlacement = useCallback(() => {
    const region = regionRef.current;
    const toolbar = region?.querySelector<HTMLElement>(".workflow-canvas-toolbar");
    if (measuring.current || !region?.isConnected || !toolbar?.isConnected) return;
    const available = region.getBoundingClientRect();
    if (!Number.isFinite(available.width) || !Number.isFinite(available.height) || available.width <= 32 || available.height <= 0) return;
    measuring.current = true;
    try {
      // Read actual localized content in its expanded anatomy, without cloning
      // controls. Restore before any commit/paint, including a failed read.
      const previous = toolbar.dataset.measureExpanded;
      let expandedWidth = 0;
      let expandedHeight = 0;
      try {
        toolbar.dataset.measureExpanded = "true";
        const expanded = toolbar.getBoundingClientRect();
        expandedWidth = expanded.width;
        expandedHeight = expanded.height;
      } finally {
        if (previous === undefined) delete toolbar.dataset.measureExpanded;
        else toolbar.dataset.measureExpanded = previous;
      }
      if (!region.isConnected || !toolbar.isConnected || !Number.isFinite(expandedWidth) || !Number.isFinite(expandedHeight) || expandedWidth <= 0 || expandedHeight <= 0) return;
      // Preserve the existing16px safe inset on both sides of this local region.
      const nextCompact = expandedWidth > available.width - 32;
      setCompact((current) => current === nextCompact ? current : nextCompact);
      const minimap = region.querySelector<HTMLElement>(".workflow-canvas-minimap");
      if (!minimap?.isConnected) return;
      const minimapRect = minimap.getBoundingClientRect();
      const runbarRect = region.querySelector<HTMLElement>(".workflow-canvas-runbar")?.getBoundingClientRect();
      const stageElement = region.closest<HTMLElement>(".workflow-canvas-stage");
      const stage = stageElement?.getBoundingClientRect();
      if (!stage || ![stage.left, stage.top, stage.right, stage.bottom].every(Number.isFinite)) return;
      const laneControls = [...(stageElement?.querySelectorAll<HTMLElement>(".workflow-container-lane-add") || [])]
        .filter((element) => element.getClientRects().length > 0)
        .map((element) => element.getBoundingClientRect())
        .filter((rect) => rect.width > 0 && rect.height > 0 && [rect.left, rect.top, rect.right, rect.bottom].every(Number.isFinite));
      const obstructions = [toolbar.getBoundingClientRect(), ...(runbarRect ? [runbarRect] : []), ...laneControls];
      const candidate = chooseCanvasMinimapRect(stage, available, minimapRect, obstructions);
      minimap.style.left = `${candidate.left - available.left}px`;
      minimap.style.top = `${candidate.top - available.top}px`;
      minimap.style.right = "auto";
      minimap.style.bottom = "auto";
      region.dataset.minimapStacked = candidate.top < available.top ? "true" : "false";
    } finally { measuring.current = false; }
  }, []);

  // Content/locale and compact commits also remeasure before paint. The same
  // local observer handles region/drawer/font geometry; no global listeners.
  useLayoutEffect(() => { updatePlacement(); });
  useLayoutEffect(() => {
    const region = regionRef.current;
    const toolbar = region?.querySelector<HTMLElement>(".workflow-canvas-toolbar");
    if (!region || !toolbar || globalThis.ResizeObserver === undefined) return;
    let active = true;
    let placementFrame: number | null = null;
    const observer = new ResizeObserver(updatePlacement);
    observer.observe(region);
    observer.observe(toolbar);
    region.querySelectorAll<HTMLElement>(".workflow-canvas-minimap, .workflow-toolbar-caption, .workflow-canvas-runbar").forEach((element) => observer.observe(element));
    const stage = region.closest<HTMLElement>(".workflow-canvas-stage");
    stage?.querySelectorAll<HTMLElement>(".workflow-container-lane-add").forEach((element) => observer.observe(element));
    const viewportElement = stage?.querySelector<HTMLElement>(".react-flow__viewport");
    const viewportObserver = viewportElement && typeof globalThis.MutationObserver !== "undefined"
      ? new MutationObserver(() => {
        if (placementFrame !== null || typeof globalThis.requestAnimationFrame !== "function") return;
        placementFrame = requestAnimationFrame(() => {
          placementFrame = null;
          if (active) updatePlacement();
        });
      })
      : null;
    if (viewportElement && viewportObserver) viewportObserver.observe(viewportElement, { attributes: true, attributeFilter: ["style"] });
    // A font becoming ready can change captions hidden by the compact row.
    // This one completion belongs to the same owner, never a global listener.
    if (document.fonts) ignoreAsyncResult(document.fonts.ready.then(() => { if (active) updatePlacement(); }));
    return () => {
      active = false;
      observer.disconnect();
      viewportObserver?.disconnect();
      if (placementFrame !== null && typeof globalThis.cancelAnimationFrame === "function") cancelAnimationFrame(placementFrame);
    };
  }, [children, showMiniMap, updatePlacement]);

  return <Panel
    position="bottom-center"
    ref={regionRef}
    className="workflow-canvas-controls"
    data-testid="workflow-canvas-controls"
    data-minimap-stacked="false"
    style={{ right: inspectorWidth }}
  >{children(compact)}</Panel>;
}
