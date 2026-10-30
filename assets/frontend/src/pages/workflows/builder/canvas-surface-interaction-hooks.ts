import { useCallback, useEffect, useMemo, useRef, type Dispatch, type SetStateAction, type RefObject, type KeyboardEvent as ReactKeyboardEvent } from "react";
import type { Connection, OnConnectStart, OnConnectEnd, IsValidConnection, Edge, EdgeChange, NodeChange, Node, ReactFlowInstance } from "@xyflow/react";
import type { I18nContextValue } from "@/lib/i18n/context";
import type { BuilderSurfaceProps, CanvasCreationIntent, CanvasCommandSelection, CanvasClipboardVisualSelection } from "./canvas-types";
import type { CanvasPlacementDelta } from "./canvas-history";
import type { WorkflowChangeSetController } from "../detail/detail-change-set";
import { placeCanvasNode } from "./canvas-placement";
import { portEdge } from "./change-set";
import { type canvasConnectionDefinition, connectionPlacement, connectionRefusal } from "./canvas-connection-policy";
import { routeCanvasEdges, type CanvasEdgeRouteInput } from "./canvas-edge-routing";
import { createWrappedRootRouteHints } from "./canvas-wrap-routing";
import {
  canvasMotionDuration,
  canvasPasteBoundsVisible,
  canvasPasteObstructions,
  canvasPastePane,
  canvasPasteRectsVisible,
  canvasPasteTranslationToBounds,
  canvasPasteViewport,
  fitCanvasToNodes,
  positionAtViewportCentre,
  positionRightOfManualSource,
} from "./canvas-viewport";
import { type buildCanvasLayout, nodeIdForRef, type CanvasEdgeData } from "./layout";
import { applyCanvasNodeState, reconcileCanvasGeometry, manualPositionsFromNodes } from "./canvas-state-helpers";
import type { useCanvasNotes } from "./canvas-notes";
import type { CanvasNoteData } from "./canvas-note-node";
import { absoluteNodeCenter, measuredHeightsEqual, nodeSize, suppressCanvasNodeClick, type CanvasPosition, type GraphEditNotice, type InteractiveCanvasEdgeData, type SearchResult } from "./canvas-core";
import { nodeRef, outcomeFromHandleId, outcomeHandleId } from "./api";
import type { WorkflowGraphEdge, WorkflowNodeRef, WorkflowOutcome, WorkflowTargetRef } from "../shared/api";
import { hookMemberId, isHookMemberRef } from "./hook-frames";
import { watchGraphIntent } from "./canvas-toast-feed";
import { ignoreAsyncResult } from "@/lib/utils/async";
import type { ClipboardCommit } from "@/pages/workflows/detail/detail-clipboard-commands";
import type { CanvasArrangeMode } from "./canvas-layout-menu";

type StateSetter<T> = Dispatch<SetStateAction<T>>;
type CanvasLayout = ReturnType<typeof buildCanvasLayout>;
type ConnectingPort = { source: WorkflowNodeRef; outcome: WorkflowOutcome };
type PlacementGesture = {
  canvasWorkflowId: string;
  ownerWorkflowId: string;
  subjectIds: string[];
  before: Record<string, CanvasPosition>;
  dragging: boolean;
  complete: boolean;
};

type CanvasInteractionContext = {
  t: I18nContextValue["t"];
  canvasMeasureRootRef: RefObject<HTMLDivElement | null>;
  setMeasuredStepHeights: StateSetter<Record<string, number>>;
  nodesWithCanvasState: Node[];
  onDisconnectPort: BuilderSurfaceProps["onDisconnectPort"];
  setSelectedEdgeId: StateSetter<string | null>;
  setGraphEditNotice: StateSetter<GraphEditNotice | null>;
  nodes: CanvasLayout["nodes"];
  rootRows: CanvasLayout["rootRows"];
  edges: CanvasLayout["edges"];
  showFailurePaths: boolean;
  structureLocked: boolean;
  hoveredEdgeId: string | null;
  selectedEdgeId: string | null;
  closeTransientSurfaces: () => void;
  onInsertStep: BuilderSurfaceProps["onInsertStep"];
  beginPlacementGesture: (nodeIds: string[], dragging: boolean) => PlacementGesture | null;
  changeAnnotationNodes: ReturnType<typeof useCanvasNotes>["onNodesChange"];
  setManualPositions: StateSetter<Record<string, CanvasPosition>>;
  saveAnnotation: ReturnType<typeof useCanvasNotes>["save"];
  visibleEdges: Edge<InteractiveCanvasEdgeData>[];
  deleteEdgeData: (data?: CanvasEdgeData) => void;
  manualPositions: Record<string, CanvasPosition>;
  reactFlowInstance: ReactFlowInstance | null;
  currentNodesById: Map<string, Node>;
  setHighlightedNodeId: StateSetter<string | null>;
  setShowSearchResults: StateSetter<boolean>;
  openGroupFromCanvas: (id: string) => void;
  openStepFromCanvas: (id: string) => void;
  focusNodeId: BuilderSurfaceProps["focusNodeId"];
  focusNodeNonce: number;
  focusNodePlace: boolean;
  handledFocusNonceRef: RefObject<number>;
  creationIntent: CanvasCreationIntent | null;
  setCreationIntent: StateSetter<CanvasCreationIntent | null>;
  searchIndex: number;
  searchResults: SearchResult[];
  setSearchIndex: StateSetter<number>;
  connectionDefinition: ReturnType<typeof canvasConnectionDefinition>;
  changeSet: BuilderSurfaceProps["changeSet"];
  onConnectPort: BuilderSurfaceProps["onConnectPort"];
  connectingPort: ConnectingPort | null;
  setConnectionPointedTarget: StateSetter<{ id: string; valid: boolean } | null>;
  connectCompletedRef: RefObject<boolean>;
  pendingConnectionDropClickRef: RefObject<CanvasPosition | null>;
  connectingRef: RefObject<(ConnectingPort & { clientPoint?: CanvasPosition }) | null>;
  setConnectingPort: StateSetter<ConnectingPort | null>;
  setConnectionHelpActive: StateSetter<boolean>;
  appendStepFromCanvas: (source: WorkflowNodeRef, outcome: WorkflowOutcome, intent?: CanvasCreationIntent) => boolean;
  selectedEdge: Edge<InteractiveCanvasEdgeData> | undefined;
  /** The change set that owns these canvas nodes; its history backs a toast's Undo. */
  resolveCommandOwner: (nodeIds: readonly string[]) => Parameters<typeof watchGraphIntent>[0];
  /** Record clearing these manual places as one placement intent; returns the toast's Undo. */
  recordAutoLayout: (before: Record<string, CanvasPosition>) => GraphEditNotice["action"] | undefined;
};

function isHookMemberEdge(edge: WorkflowGraphEdge | undefined): boolean {
  return Boolean(edge && isHookMemberRef(edge.source));
}

export function nodeRefFromNodeId(nodeId: string): WorkflowNodeRef {
  return nodeId.startsWith("group:") ? nodeRef("group", nodeId.slice(6)) : nodeId.startsWith("for_each:") ? nodeRef("for_each", nodeId.slice(9)) : nodeRef("step", nodeId);
}

export function useCanvasRenderedStepHeights({ canvasMeasureRootRef, setMeasuredStepHeights, nodesWithCanvasState }: Readonly<Pick<CanvasInteractionContext, "canvasMeasureRootRef" | "setMeasuredStepHeights" | "nodesWithCanvasState">>) {
  const measureRenderedStepHeights = useCallback(() => {
    const root = canvasMeasureRootRef.current;
    if (!root) {
      return;
    }
    const next: Record<string, number> = {};
    root
      .querySelectorAll<HTMLElement>("[data-workflow-step-measure-id]")
      .forEach((element) => {
        const stepId = element.dataset.workflowStepMeasureId;
        if (!stepId) {
          return;
        }
        const frame = element.closest<HTMLElement>("[data-workflow-step-frame='true']");
        const frameStyle = frame ? globalThis.getComputedStyle(frame) : null;
        const verticalChrome = frameStyle
          ? [
              frameStyle.paddingTop,
              frameStyle.paddingBottom,
              frameStyle.borderTopWidth,
              frameStyle.borderBottomWidth,
            ].reduce((total, value) => total + (Number.parseFloat(value) || 0), 0)
          : 0;
        const measuredHeight = Math.ceil(Math.max(element.scrollHeight, element.offsetHeight) + verticalChrome);
        if (measuredHeight > 0) {
          next[stepId] = measuredHeight;
        }
      });
    setMeasuredStepHeights((current) =>
      measuredHeightsEqual(current, next) ? current : next,
    );
  }, [canvasMeasureRootRef, setMeasuredStepHeights]);

  useEffect(() => {
    const root = canvasMeasureRootRef.current;
    if (!root) {
      return undefined;
    }
    const requestFrame =
      typeof globalThis.requestAnimationFrame === "function"
        ? globalThis.requestAnimationFrame.bind(globalThis)
        : (callback: FrameRequestCallback) => globalThis.setTimeout(() => callback(Date.now()), 0);
    type FrameHandle = ReturnType<typeof requestFrame>;
    const cancelFrame = (handle: FrameHandle) => {
      if (
        typeof globalThis.cancelAnimationFrame === "function" &&
        typeof handle === "number"
      ) {
        globalThis.cancelAnimationFrame(handle);
        return;
      }
      globalThis.clearTimeout(handle);
    };
    let frameId: FrameHandle | null = null;
    const scheduleMeasure = () => {
      if (frameId !== null) {
        cancelFrame(frameId);
      }
      frameId = requestFrame(() => {
        frameId = null;
        measureRenderedStepHeights();
      });
    };
    scheduleMeasure();
    if (globalThis.ResizeObserver === undefined) {
      return () => {
        if (frameId !== null) {
          cancelFrame(frameId);
        }
      };
    }
    const observer = new ResizeObserver(scheduleMeasure);
    root
      .querySelectorAll<HTMLElement>("[data-workflow-step-measure-id]")
      .forEach((element) => observer.observe(element));
    return () => {
      if (frameId !== null) {
        cancelFrame(frameId);
      }
      observer.disconnect();
    };
  }, [canvasMeasureRootRef, measureRenderedStepHeights, nodesWithCanvasState]);

}

export function useCanvasInteractiveEdges({ t, onDisconnectPort, setSelectedEdgeId, setGraphEditNotice, nodes, nodesWithCanvasState, rootRows, edges, showFailurePaths, structureLocked, hoveredEdgeId, selectedEdgeId, closeTransientSurfaces, onInsertStep, resolveCommandOwner }: Readonly<Pick<CanvasInteractionContext, "t" | "onDisconnectPort" | "setSelectedEdgeId" | "setGraphEditNotice" | "nodes" | "nodesWithCanvasState" | "rootRows" | "edges" | "showFailurePaths" | "structureLocked" | "hoveredEdgeId" | "selectedEdgeId" | "closeTransientSurfaces" | "onInsertStep" | "resolveCommandOwner">>) {
  /** Remove connection acts at once; the toast offers Undo (Components2 › Connection actions). */
  const deleteEdgeData = useCallback(
    (data?: CanvasEdgeData) => {
      if (!data?.edge) {
        return;
      }
      setSelectedEdgeId(null);
      const undo = watchGraphIntent(resolveCommandOwner([nodeIdForRef(data.edge.source)]));
      try { onDisconnectPort(data.edge.source, data.edge.outcome); }
      catch (error) {
        setGraphEditNotice({ label: error instanceof Error ? error.message : t("react.workflows.connection_change_failed", "The connection could not be changed."), tone: "error" });
        return;
      }
      setGraphEditNotice({ label: t("react.workflows.disconnect_staged", "Connection removal added to the unapplied structure."), tone: "success", action: undo(t("react.workflows.undo", "Undo")) });
    },
    [onDisconnectPort, resolveCommandOwner, setGraphEditNotice, setSelectedEdgeId, t],
  );

  const routedEdges = useMemo(() => {
    const byId = new Map(nodesWithCanvasState.map((node) => [node.id, node]));
    const absolute = (node: Node): CanvasPosition => { let point = { ...node.position }; let parent = node.parentId ? byId.get(node.parentId) : undefined; const visited = new Set([node.id]); while (parent && !visited.has(parent.id)) { visited.add(parent.id); point = { x: point.x + parent.position.x, y: point.y + parent.position.y }; parent = parent.parentId ? byId.get(parent.parentId) : undefined; } return point; };
    const rectangle = (node: Node) => ({ ...absolute(node), width: Number(node.width || node.style?.width || 0), height: Number(node.height || node.style?.height || 0) });
    const isAncestor = (ancestor: Node, node: Node) => {
      let parent = node.parentId;
      const seen = new Set<string>();
      while (parent && !seen.has(parent)) {
        if (parent === ancestor.id) return true;
        seen.add(parent);
        parent = byId.get(parent)?.parentId;
      }
      return false;
    };
    const wrappedRouteHints = createWrappedRootRouteHints(rootRows, nodes);
    const inputs: CanvasEdgeRouteInput[] = []; const routedIds: string[] = [];
    for (const edge of edges) {
      const source = byId.get(edge.source); const target = byId.get(edge.target);
      if (!source || !target || !edge.data) continue;
      const sourceHandle = source.handles?.find((handle) => handle.type === "source" && handle.id === edge.sourceHandle);
      const targetHandle = target.handles?.find((handle) => handle.type === "target" && handle.id === edge.targetHandle);
      if (!sourceHandle || !targetHandle) continue;
      const sourceRect = rectangle(source); const targetRect = rectangle(target);
      const obstacles = nodesWithCanvasState.filter((node) => node.type !== "canvasNote" && node.id !== source.id && node.id !== target.id && !isAncestor(node, source) && !isAncestor(node, target)).map(rectangle);
      const sourceSide = sourceHandle.position === "bottom" ? "bottom" : "right";
      const targetSide = targetHandle.position === "bottom" ? "bottom" : "left";
      const rowHint = wrappedRouteHints({
        source: { node: source, rect: sourceRect }, target: { node: target, rect: targetRect },
        sameScope: Boolean(edge.data.routing?.sameScope),
        failure: Boolean(edge.data.failurePath), repeat: Boolean(edge.data.rework),
        sourceSide, targetSide,
      });
      inputs.push({ source: { x: sourceRect.x + sourceHandle.x + (sourceHandle.width ?? 10) / 2, y: sourceRect.y + sourceHandle.y + (sourceHandle.height ?? 10) / 2 }, target: { x: targetRect.x + targetHandle.x + (targetHandle.width ?? 10) / 2, y: targetRect.y + targetHandle.y + (targetHandle.height ?? 10) / 2 }, obstacles, endCards: [source, target].filter((node) => !isAncestor(node, source) && !isAncestor(node, target)).map(rectangle), backward: rowHint.backward ?? (targetRect.x <= sourceRect.x), failure: edge.data.failurePath, repeat: edge.data.rework, shapeGroupKey: `${source.id}:${target.id}:${edge.targetHandle}`, longJump: edge.data.routing?.longJump, sourceSide, targetSide, ...(rowHint.targetRowTop === undefined ? {} : { targetRowTop: rowHint.targetRowTop }) });
      routedIds.push(edge.id);
    }
    const routes = new Map(routeCanvasEdges(inputs).map((route, index) => [routedIds[index], route]));
    return edges.map((edge) => edge.data ? { ...edge, data: { ...edge.data, route: routes.get(edge.id) } } : edge);
  }, [edges, nodes, nodesWithCanvasState, rootRows]);

  const visibleEdges = useMemo(() => routedEdges.filter((edge) => showFailurePaths || !edge.data?.failurePath).map((edge) => {
    if (!edge.data) return edge;
    const editable = !structureLocked || isHookMemberEdge(edge.data.edge);
    return { ...edge, data: { ...edge.data, hovered: edge.id === hoveredEdgeId, selected: edge.id === selectedEdgeId, ...(editable ? { onDelete: deleteEdgeData, onInsert: (data: CanvasEdgeData) => { setSelectedEdgeId(null); closeTransientSurfaces(); onInsertStep(data.edge); } } : {}) } };
  }), [closeTransientSurfaces, deleteEdgeData, hoveredEdgeId, onInsertStep, routedEdges, selectedEdgeId, showFailurePaths, structureLocked, setSelectedEdgeId]);
  const selectedEdge = useMemo(
    () =>
      visibleEdges.find((edge) => edge.id === selectedEdgeId) as
        | Edge<InteractiveCanvasEdgeData>
        | undefined,
    [selectedEdgeId, visibleEdges],
  );

  useEffect(() => {
    if (selectedEdgeId && !selectedEdge) {
      setSelectedEdgeId(null);
    }
  }, [selectedEdge, selectedEdgeId, setSelectedEdgeId]);

  return { deleteEdgeData, visibleEdges, selectedEdge };
}

export function useCanvasGraphEditing({ t, beginPlacementGesture, changeAnnotationNodes, nodes, setManualPositions, saveAnnotation, visibleEdges, deleteEdgeData, setSelectedEdgeId, manualPositions, setGraphEditNotice, reactFlowInstance, canvasMeasureRootRef, recordAutoLayout }: Readonly<Pick<CanvasInteractionContext, "t" | "beginPlacementGesture" | "changeAnnotationNodes" | "nodes" | "setManualPositions" | "saveAnnotation" | "visibleEdges" | "deleteEdgeData" | "setSelectedEdgeId" | "manualPositions" | "setGraphEditNotice" | "reactFlowInstance" | "canvasMeasureRootRef" | "recordAutoLayout">>) {
  /** Dragging a node only moves it on screen; the execution graph is unchanged. */
  const handleNodesChange = useCallback((changes: NodeChange[]) => {
    changeAnnotationNodes(changes);
    const positions = new Map<string, CanvasPosition>();
    for (const change of changes) if (change.type === "position" && change.position && !change.id.startsWith("annotation:") && change.id !== "visual:start") positions.set(change.id, change.position);
    if (!positions.size) return;
    const positional = changes.filter((change) => change.type === "position" && positions.has(change.id));
    const gesture = beginPlacementGesture([...positions.keys()], positional.some((change) => change.type === "position" && change.dragging === true));
    if (!gesture) return;
    // React Flow keyboard nudges have a final position batch but no drag-stop.
    if (!gesture.dragging && positional.every((change) => change.type === "position" && change.dragging === false)) gesture.complete = true;
    setManualPositions((current) => {
      const displayed = applyCanvasNodeState({ nodes, manualPositions: current, connectionHelpActive: false, highlightedNodeId: null });
      const changed = displayed.map((node) => {
        const position = positions.get(node.id);
        return position ? { ...node, position } : node;
      });
      return manualPositionsFromNodes(reconcileCanvasGeometry(changed), nodes, current);
    });
  }, [beginPlacementGesture, changeAnnotationNodes, nodes, setManualPositions]);

  const handleNodeDragStop = useCallback((node: Node) => {
    if (node.type === "canvasNote") {
      const { note } = node.data as CanvasNoteData;
      ignoreAsyncResult(saveAnnotation({ ...note, ...node.position }));
      return;
    }
    if (node.type === "workflowStart") return;
    const gesture = beginPlacementGesture([node.id], true);
    if (!gesture) return;
    gesture.complete = true;
    setManualPositions((current) => {
      const displayed = applyCanvasNodeState({ nodes, manualPositions: current, connectionHelpActive: false, highlightedNodeId: null });
      const currentNode = displayed.find((candidate) => candidate.id === node.id) ?? node;
      const position = placeCanvasNode(currentNode, node.position, displayed);
      const placed = displayed.map((candidate) => candidate.id === node.id ? { ...candidate, position } : candidate);
      return manualPositionsFromNodes(reconcileCanvasGeometry(placed), nodes, current);
    });
  }, [beginPlacementGesture, nodes, saveAnnotation, setManualPositions]);

  const handleEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      const removedChange = changes.find((change) => change.type === "remove" && "id" in change);
      if (removedChange && "id" in removedChange) {
        const removedEdge = visibleEdges.find((edge) => edge.id === removedChange.id);
        deleteEdgeData(removedEdge?.data);
        return;
      }
      const selectedChange = changes.find((change) => change.type === "select" && change.selected);
      if (selectedChange && "id" in selectedChange) {
        setSelectedEdgeId(selectedChange.id);
        return;
      }
      if (changes.some((change) => change.type === "select" && !change.selected)) {
        setSelectedEdgeId(null);
      }
    },
    [deleteEdgeData, setSelectedEdgeId, visibleEdges],
  );

  const handleAutoLayout = useCallback(() => {
    const hadManualLayout = Object.keys(manualPositions).length;
    // The cleared places are one placement intent: the toast's Undo (and ⌘Z) puts them back.
    const undo = hadManualLayout ? recordAutoLayout(manualPositions) : undefined;
    setManualPositions({});
    setGraphEditNotice({
      label: hadManualLayout ? t("react.workflows.auto_layout_reset", "Auto layout reset.") : t("react.workflows.already_using_auto_layout_view_fitted", "Already using auto layout. View fitted."),
      tone: "success",
      ...(undo ? { action: undo } : {}),
    });
    globalThis.requestAnimationFrame(() => {
      ignoreAsyncResult(fitCanvasToNodes(reactFlowInstance, canvasMotionDuration(), canvasMeasureRootRef.current?.getBoundingClientRect()));
    });
  }, [canvasMeasureRootRef, manualPositions, reactFlowInstance, recordAutoLayout, setGraphEditNotice, setManualPositions, t]);

  return { handleNodesChange, handleNodeDragStop, handleEdgesChange, handleAutoLayout };
}

export function useCanvasSearchNavigation({ autoArrange = false, currentNodesById, reactFlowInstance, setHighlightedNodeId, setShowSearchResults, openGroupFromCanvas, openStepFromCanvas, focusNodeId, focusNodeNonce, focusNodeCreation, focusNodePlace, handledFocusNonceRef, canvasMeasureRootRef, manualPositions, edges, nodes, workflowId, placementOwnerFor, placementGestureGenerationFor, currentSelection, setManualPositions, setCreationIntent, searchIndex, searchResults, setSearchIndex }: Readonly<{ autoArrange?: boolean; workflowId: string; placementOwnerFor: (ids: readonly string[]) => WorkflowChangeSetController | null; placementGestureGenerationFor: (id: string) => number; currentSelection: () => { ids: readonly string[]; generation: number } } & Pick<CanvasInteractionContext, "currentNodesById" | "reactFlowInstance" | "setHighlightedNodeId" | "setShowSearchResults" | "openGroupFromCanvas" | "openStepFromCanvas" | "focusNodeId" | "focusNodeNonce" | "focusNodePlace" | "handledFocusNonceRef" | "canvasMeasureRootRef" | "manualPositions" | "edges" | "nodes" | "setManualPositions" | "setCreationIntent" | "searchIndex" | "searchResults" | "setSearchIndex"> & { focusNodeCreation: BuilderSurfaceProps["focusNodeCreation"] }>) {
  const creationBaselineRef = useRef<{ nonce: number; token: NonNullable<BuilderSurfaceProps["focusNodeCreation"]>["token"]; nodeId: string; before: CanvasPosition | null; subjectGeneration: number; gestureGeneration: number } | null>(null);
  const focusSearchResult = useCallback(
    (result: SearchResult) => {
      const node = currentNodesById.get(result.id);
      if (node && reactFlowInstance) {
        const center = absoluteNodeCenter(node, currentNodesById);
        ignoreAsyncResult(reactFlowInstance.setCenter(center.x, center.y, {
          duration: canvasMotionDuration(),
          zoom: Math.max(reactFlowInstance.getZoom(), result.kind === "group" ? 0.8 : 0.9),
        }));
      }
      setHighlightedNodeId(result.id);
      setShowSearchResults(false);
      if (result.kind === "group") {
        openGroupFromCanvas(result.id.replace(/^group:/, ""));
        return;
      }
      openStepFromCanvas(result.id);
    },
    [currentNodesById, openGroupFromCanvas, openStepFromCanvas, reactFlowInstance, setHighlightedNodeId, setShowSearchResults],
  );

  useEffect(() => {
    const creation = focusNodeCreation && focusNodeCreation.nodeId === focusNodeId && focusNodeCreation.nonce === focusNodeNonce ? focusNodeCreation : null;
    // Capture the retired ID's XY before the new node has to be measured. If the
    // author drags that new node while publication waits, its newer XY wins.
    if (creation && (creationBaselineRef.current?.nonce !== creation.nonce || creationBaselineRef.current.token !== creation.token)) {
      const owner = placementOwnerFor([creation.nodeId]);
      creationBaselineRef.current = { nonce: creation.nonce, token: creation.token, nodeId: creation.nodeId,
        before: manualPositions[creation.nodeId] ?? null, subjectGeneration: owner?.placementSubjectGeneration(creation.nodeId) ?? -1,
        gestureGeneration: placementGestureGenerationFor(creation.nodeId) };
    }
    if (!focusNodeId || !reactFlowInstance || !focusNodeNonce || handledFocusNonceRef.current === focusNodeNonce) return;
    const node = currentNodesById.get(focusNodeId);
    if (!node) return; // The graph's next publication may contain the new node.
    const bounds = canvasMeasureRootRef.current?.getBoundingClientRect();
    const samePosition = (left: CanvasPosition | null, right: CanvasPosition | null) => left?.x === right?.x && left?.y === right?.y;
    if (creation) {
      const owner = placementOwnerFor([focusNodeId]);
      if (owner?.workflowId !== creation.ownerWorkflowId || owner.placementViewKey !== creation.viewKey ||
        owner.placementViewEpoch !== creation.viewEpoch || !owner.hasGraphIntent(creation.token)) return;
      if (creation.intent && !edges.some((edge) => edge.target === focusNodeId && edge.source === nodeIdForRef(creation.intent!.source) &&
        edge.sourceHandle === outcomeHandleId(creation.intent!.outcome))) return;
      const before = creationBaselineRef.current?.before ?? null;
      const subjectGeneration = creationBaselineRef.current?.subjectGeneration ?? -1;
      if (owner.placementSubjectGeneration(focusNodeId) !== subjectGeneration ||
        placementGestureGenerationFor(focusNodeId) !== creationBaselineRef.current?.gestureGeneration ||
        !samePosition(manualPositions[focusNodeId] ?? null, before)) {
        // A later manual gesture has already claimed this node. Focus is retired,
        // but its older placement must not become a later Undo action.
        handledFocusNonceRef.current = focusNodeNonce;
        setCreationIntent(null);
        return;
      }
      let after: CanvasPosition | null = null;
      if (creation.point) {
        let parent = node.parentId ? currentNodesById.get(node.parentId) : undefined;
        let x = creation.point.x; let y = creation.point.y;
        while (parent) { x -= parent.position.x; y -= parent.position.y; parent = parent.parentId ? currentNodesById.get(parent.parentId) : undefined; }
        after = placeCanvasNode(node, { x, y }, [...currentNodesById.values()]);
      } else if (focusNodePlace && !autoArrange) {
        const centre = positionAtViewportCentre(node, bounds, (point) => reactFlowInstance.screenToFlowPosition(point));
        if (centre) after = placeCanvasNode(node, centre, [...currentNodesById.values()]);
      } else {
        after = positionRightOfManualSource(node, edges, currentNodesById, manualPositions);
      }
      if (!samePosition(before, after)) {
        const id = focusNodeId;
        const placement: CanvasPlacementDelta = {
          canvasWorkflowId: workflowId, subjectIds: [id],
          canonicalStepSubjectIds: creation.target.kind === "step" ? [creation.target.id] : [],
          before: { [id]: before }, after: { [id]: after },
        };
        if (!owner.attachPlacementToCreation(creation.token, creation.target, placement, subjectGeneration)) return;
        setManualPositions((current) => {
          const next = { ...current };
          if (after) next[id] = after; else delete next[id];
          return next;
        });
      }
      handledFocusNonceRef.current = focusNodeNonce;
      setCreationIntent(null);
      const selection = currentSelection();
      const stillFollowing = creation.selectionGeneration === undefined || selection.generation === creation.selectionGeneration || selection.ids.includes(focusNodeId);
      if (stillFollowing) {
        const baseNode = after === null ? nodes.find((candidate) => candidate.id === focusNodeId) ?? node : node;
        const center = absoluteNodeCenter(after ? { ...node, position: after } : baseNode, currentNodesById);
        ignoreAsyncResult(reactFlowInstance.setCenter(center.x, center.y, {
          duration: canvasMotionDuration(),
          zoom: Math.max(reactFlowInstance.getZoom(), focusNodeId.startsWith("group:") ? 0.8 : 0.9),
        }));
        setHighlightedNodeId(focusNodeId);
      }
      return;
    }
    // Ordinary focus has no authority to consume an old palette point.
    handledFocusNonceRef.current = focusNodeNonce;
    const unplaced = !manualPositions[focusNodeId];
    const placed = focusNodePlace && unplaced && !autoArrange
      ? positionAtViewportCentre(node, bounds, (point) => reactFlowInstance.screenToFlowPosition(point))
      : null;
    if (placed) {
      setManualPositions((current) => ({ ...current, [focusNodeId]: placeCanvasNode(node, placed, [...currentNodesById.values()]) }));
    } else {
      const besideSource = unplaced ? positionRightOfManualSource(node, edges, currentNodesById, manualPositions) : null;
      if (besideSource) setManualPositions((current) => ({ ...current, [focusNodeId]: besideSource }));
      const center = absoluteNodeCenter(besideSource ? { ...node, position: besideSource } : node, currentNodesById);
      ignoreAsyncResult(reactFlowInstance.setCenter(center.x, center.y, {
        duration: canvasMotionDuration(),
        zoom: Math.max(reactFlowInstance.getZoom(), focusNodeId.startsWith("group:") ? 0.8 : 0.9),
      }));
    }
    setHighlightedNodeId(focusNodeId);
  }, [autoArrange, currentNodesById, currentSelection, edges, focusNodeCreation, focusNodeId, focusNodeNonce, focusNodePlace, handledFocusNonceRef, manualPositions, nodes, placementGestureGenerationFor, placementOwnerFor, reactFlowInstance, setCreationIntent, setHighlightedNodeId, setManualPositions, canvasMeasureRootRef, workflowId]);

  const handleSearchKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLInputElement>) => {
      if (!searchResults.length) {
        if (event.key === "Escape") {
          setShowSearchResults(false);
        }
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setShowSearchResults(true);
        setSearchIndex((current) => Math.min(current + 1, searchResults.length - 1));
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setShowSearchResults(true);
        setSearchIndex((current) => Math.max(current - 1, 0));
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        focusSearchResult(searchResults[searchIndex] || searchResults[0]);
        return;
      }
      if (event.key === "Escape") {
        setShowSearchResults(false);
      }
    },
    [focusSearchResult, searchIndex, searchResults, setSearchIndex, setShowSearchResults],
  );

  return { focusSearchResult, handleSearchKeyDown };
}

export function useCanvasConnectionGestures({ connectionDefinition, changeSet, structureLocked, t, onConnectPort, setGraphEditNotice, connectingPort, setConnectionPointedTarget, connectCompletedRef, pendingConnectionDropClickRef, connectingRef, setConnectingPort, setConnectionHelpActive, reactFlowInstance, closeTransientSurfaces, setSelectedEdgeId, setCreationIntent, onInsertStep, appendStepFromCanvas, deleteEdgeData, selectedEdge }: Readonly<Pick<CanvasInteractionContext, "connectionDefinition" | "changeSet" | "structureLocked" | "t" | "onConnectPort" | "setGraphEditNotice" | "connectingPort" | "setConnectionPointedTarget" | "connectCompletedRef" | "pendingConnectionDropClickRef" | "connectingRef" | "setConnectingPort" | "setConnectionHelpActive" | "reactFlowInstance" | "closeTransientSurfaces" | "setSelectedEdgeId" | "setCreationIntent" | "onInsertStep" | "appendStepFromCanvas" | "deleteEdgeData" | "selectedEdge">>) {
  const resolveConnection = useCallback((connection: Pick<Connection, "source" | "target"> & Partial<Pick<Connection, "sourceHandle" | "targetHandle">>) => {
    const sourceId = connection.source || ""; const targetId = connection.target || "";
    const outcome = outcomeFromHandleId(connection.sourceHandle || "");
    if (!sourceId || !targetId || !outcome || sourceId.startsWith("visual:")) return null;
    const source = nodeRefFromNodeId(sourceId); const placement = connectionPlacement(connectionDefinition, source);
    let target: WorkflowTargetRef = nodeRefFromNodeId(targetId);
    if (connection.targetHandle === "exit") {
      if (target.kind === "group") target = { kind: "group_exit", group_id: target.id, lane_id: placement?.laneId || null };
      else if (target.kind === "for_each") target = { kind: "for_each_exit", for_each_id: target.id, lane_id: placement?.laneId || null };
      else return null;
    }
    return { source, outcome, target };
  }, [connectionDefinition]);
  const stageConnection = useCallback(async (source: WorkflowNodeRef, outcome: WorkflowOutcome, target: WorkflowTargetRef, rework = false) => {
    if (changeSet.status === "applying" || changeSet.status === "conflict" || (structureLocked && !isHookMemberRef(source))) return false;
    const refusal = connectionRefusal(connectionDefinition, source, outcome, target, rework, t);
    if (refusal) { setGraphEditNotice({ label: refusal.label, tone: "error" }); return false; }
    try {
      const staged = await onConnectPort(source, outcome, target, rework);
      if (staged) setGraphEditNotice({ label: t("react.workflows.connection_staged", "Connection added to the unapplied structure."), tone: "success" });
      return staged;
    } catch (error) { setGraphEditNotice({ label: error instanceof Error ? error.message : t("react.workflows.connection_change_failed", "The connection could not be changed."), tone: "error" }); return false; }
  }, [changeSet.status, connectionDefinition, onConnectPort, structureLocked, t, setGraphEditNotice]);
  const handleConnectionTargetChange = useCallback((id: string | null) => {
    if (!connectingPort) return;
    if (!id) { setConnectionPointedTarget(null); setGraphEditNotice({ label: t("react.workflows.connect_model_named_outcome", "Drag from a named outcome to the step that runs next."), tone: "success" }); return; }
    const refusal = connectionRefusal(connectionDefinition, connectingPort.source, connectingPort.outcome, nodeRefFromNodeId(id), false, t);
    setConnectionPointedTarget((current) => current?.id === id && current.valid === !refusal ? current : { id, valid: !refusal });
    setGraphEditNotice({ label: refusal?.label || t("react.workflows.connect_model_named_outcome", "Drag from a named outcome to the step that runs next."), tone: refusal ? "error" : "success" });
  }, [connectingPort, connectionDefinition, t, setConnectionPointedTarget, setGraphEditNotice]);
  const isValidConnection = useCallback<IsValidConnection>((connection) => {
    const resolved = resolveConnection(connection);
    if (!resolved) return false;
    const refusal = connectionRefusal(connectionDefinition, resolved.source, resolved.outcome, resolved.target, false, t);
    setConnectionPointedTarget((current) => current?.id === connection.target && current.valid === !refusal ? current : { id: connection.target, valid: !refusal });
    const label = refusal?.label || t("react.workflows.connect_model_named_outcome", "Drag from a named outcome to the step that runs next.");
    setGraphEditNotice((current) => current?.label === label ? current : { label, tone: refusal ? "error" : "success" });
    return !(structureLocked && !isHookMemberRef(resolved.source)) && !refusal;
  }, [connectionDefinition, resolveConnection, structureLocked, t, setConnectionPointedTarget, setGraphEditNotice]);
  const handleConnect = useCallback((connection: Connection) => {
    connectCompletedRef.current = true;
    const resolved = resolveConnection(connection);
    if (!resolved) { setGraphEditNotice({ label: t("react.workflows.connect_from_a_named_outcome", "Drag from one of the step's named outcomes to the next step."), tone: "error" }); return; }
    ignoreAsyncResult(stageConnection(resolved.source, resolved.outcome, resolved.target));
  }, [resolveConnection, stageConnection, t, connectCompletedRef, setGraphEditNotice]);
  const handleConnectStart = useCallback<OnConnectStart>((event, params) => {
    pendingConnectionDropClickRef.current = null;
    connectCompletedRef.current = false;
    setConnectionPointedTarget(null);
    const outcome = outcomeFromHandleId(params.handleId || "");
    const source = params.nodeId ? nodeRefFromNodeId(params.nodeId) : null;
    const point = "touches" in event ? event.touches[0] : event;
    connectingRef.current = source && outcome && params.handleType === "source" ? { source, outcome, clientPoint: point ? { x: point.clientX, y: point.clientY } : undefined } : null;
    setConnectingPort(connectingRef.current);
    setConnectionHelpActive(true);
    setGraphEditNotice({ label: t("react.workflows.connect_model_named_outcome", "Drag from a named outcome to the step that runs next."), tone: "success" });
  }, [t, pendingConnectionDropClickRef, connectCompletedRef, setConnectionPointedTarget, connectingRef, setConnectingPort, setConnectionHelpActive, setGraphEditNotice]);
  const handleConnectEnd = useCallback<OnConnectEnd>((event, state) => {
    const connecting = connectingRef.current; connectingRef.current = null;
    setConnectingPort(null);
    setConnectionPointedTarget(null);
    setConnectionHelpActive(false);
    if (connectCompletedRef.current || !connecting) return;
    const point = "changedTouches" in event ? event.changedTouches[0] : event;
    const target = state?.toNode;
    if (target) {
      const resolved = resolveConnection({ source: nodeIdForRef(connecting.source), sourceHandle: outcomeHandleId(connecting.outcome), target: target.id, targetHandle: state.toHandle?.id || (target.id.startsWith("group:") || target.id.startsWith("for_each:") ? "entry" : "in") });
      const refusal = resolved && connectionRefusal(connectionDefinition, resolved.source, resolved.outcome, resolved.target, false, t);
      if (refusal) setGraphEditNotice({ label: refusal.label, tone: "error" });
      return;
    }
    const scope = connectionPlacement(connectionDefinition, connecting.source);
    const moved = point && connecting.clientPoint && Math.hypot(point.clientX - connecting.clientPoint.x, point.clientY - connecting.clientPoint.y) > 4;
    // A native document release can retain the captured source as event.target.
    // Only the visible bare pane under the release point starts creation; cards,
    // toolbars and overlays must never become empty drops through their ancestry.
    const hit = point && typeof document.elementFromPoint === "function" ? document.elementFromPoint(point.clientX, point.clientY) : event.target;
    const pane = hit instanceof Element && hit.classList.contains("react-flow__pane");
    if (scope && point && moved && pane) {
      const clientPoint = { x: point.clientX, y: point.clientY };
      const intent: CanvasCreationIntent = { source: connecting.source, outcome: connecting.outcome, scope, clientPoint, point: reactFlowInstance?.screenToFlowPosition(clientPoint) || state?.to || clientPoint };
      const existing = portEdge(connectionDefinition, connecting.source, connecting.outcome);
      if (existing) {
        if (changeSet.status === "applying" || changeSet.status === "conflict" || (structureLocked && !isHookMemberRef(connecting.source))) return;
        closeTransientSurfaces(); suppressCanvasNodeClick(); setSelectedEdgeId(null); setCreationIntent(intent);
        onInsertStep(existing, intent);
        pendingConnectionDropClickRef.current = { ...clientPoint };
      } else if (appendStepFromCanvas(connecting.source, connecting.outcome, intent)) pendingConnectionDropClickRef.current = { ...clientPoint };
    }
  }, [appendStepFromCanvas, changeSet.status, closeTransientSurfaces, connectionDefinition, onInsertStep, reactFlowInstance, resolveConnection, structureLocked, t, connectingRef, setConnectingPort, setConnectionPointedTarget, setConnectionHelpActive, connectCompletedRef, setGraphEditNotice, setSelectedEdgeId, setCreationIntent, pendingConnectionDropClickRef]);

  const handleDeleteSelectedEdge = useCallback(() => {
    deleteEdgeData(selectedEdge?.data);
  }, [deleteEdgeData, selectedEdge]);

  const handleInsertOnSelectedEdge = useCallback(() => {
    const data = selectedEdge?.data;
    if (!data) {
      return;
    }
    setSelectedEdgeId(null);
    closeTransientSurfaces();
    onInsertStep(data.edge);
  }, [closeTransientSurfaces, onInsertStep, selectedEdge, setSelectedEdgeId]);

  return { stageConnection, handleConnectionTargetChange, isValidConnection, handleConnect, handleConnectStart, handleConnectEnd, handleDeleteSelectedEdge, handleInsertOnSelectedEdge };
}


export type PendingClipboardCommit = {
  event: ClipboardCommit;
  viewKey: string;
  generation: number;
  viewportGeneration: number;
  manualGeneration: number;
};

type CanvasClipboardPublicationContext = Pick<CanvasInteractionContext,
  | "changeSet"
  | "currentNodesById"
  | "manualPositions"
  | "reactFlowInstance"
  | "canvasMeasureRootRef"
  | "setManualPositions"
  | "setGraphEditNotice"
> & {
  arrangeMode: CanvasArrangeMode;
  commandViewKey: string;
  inspectorWidth: number;
  pendingClipboardCommit: PendingClipboardCommit | null;
  pendingClipboardCommitRef: RefObject<PendingClipboardCommit | null>;
  placementHistoryOwners: BuilderSurfaceProps["placementHistoryOwners"];
  commandViewRef: RefObject<string>;
  clipboardViewGenerationRef: RefObject<number>;
  clipboardViewportGenerationRef: RefObject<number>;
  clipboardManualInteractionsRef: RefObject<{ serial: number; subjects: Map<string, number> }>;
  commandSelectionRef: RefObject<CanvasCommandSelection>;
  clipboardTranslateRef: RefObject<I18nContextValue["t"]>;
  setPendingClipboardCommit: StateSetter<PendingClipboardCommit | null>;
  setClipboardBusy: StateSetter<boolean>;
  setClipboardSelection: StateSetter<CanvasClipboardVisualSelection | null>;
};

/** Publish accepted clipboard geometry; commands, authority and graph history stay with the Surface. */
export function useCanvasClipboardPublication({
  arrangeMode,
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
}: Readonly<CanvasClipboardPublicationContext>) {
  const phaseRef = useRef<{
    pending: PendingClipboardCommit;
    baseline: Record<string, CanvasPosition>;
    applied?: Map<string, CanvasPosition>;
    previous?: Record<string, CanvasPosition>;
    awaitingState?: boolean;
    writePending?: boolean;
    retired?: boolean;
    manualInput?: string;
    delta?: CanvasPosition | null;
    fitInput?: string;
    viewport?: { x: number; y: number; zoom: number };
    stableInput?: string;
  } | null>(null);
  useEffect(() => {
    const pending = pendingClipboardCommit;
    if (!pending) { phaseRef.current = null; return; }
    const phase: NonNullable<typeof phaseRef.current> = phaseRef.current?.pending === pending ? phaseRef.current : { pending, baseline: manualPositions };
    phaseRef.current = phase;
    let frame = 0; let stopped = false;
    const finish = (roots?: string[]) => {
      if (pending !== pendingClipboardCommitRef.current) return;
      if (roots && commandSelectionRef.current.generation === pending.event.authority.generation) {
        setClipboardSelection((value) => ({ ids: roots, nonce: (value?.nonce || 0) + 1 }));
      }
      phaseRef.current = null; pendingClipboardCommitRef.current = null;
      setPendingClipboardCommit(null); setClipboardBusy(false);
    };
    const current = () => pending === pendingClipboardCommitRef.current && pending.viewKey === commandViewRef.current &&
      pending.generation === clipboardViewGenerationRef.current && pending.event.authority.changeSet.hasGraphIntent(pending.event.intentToken) &&
      (placementHistoryOwners ?? [changeSet]).some((owner) => owner.workflowId === pending.event.authority.owner.workflowId &&
        owner.placementViewKey === pending.event.authority.changeSet.placementViewKey && owner.hasGraphIntent(pending.event.intentToken) &&
        (owner.baseRevision ?? pending.event.authority.owner.serverRevision) === (pending.event.authority.changeSet.baseRevision ?? pending.event.authority.owner.serverRevision));
    const same = (a?: CanvasPosition, b?: CanvasPosition) => a === b || Boolean(a && b && Math.abs(a.x - b.x) < 0.01 && Math.abs(a.y - b.y) < 0.01);
    const attempt = () => {
      if (stopped || pending !== pendingClipboardCommitRef.current) return;
      if (!current()) { finish(); return; }
      const retry = () => { frame = window.requestAnimationFrame(attempt); };
      const hookId = pending.event.authority.viewKey === "author" ? "" : pending.event.authority.viewKey;
      const display = (raw: string) => {
        const member = hookId ? hookMemberId(hookId, raw) : raw;
        const found = [member, `group:${member}`, `for_each:${member}`].filter((id) => currentNodesById.has(id));
        return found.length === 1 ? found[0] : null;
      };
      const world = new Map<string, CanvasPosition>();
      for (const [raw, point] of pending.event.worldPositions) {
        const id = display(raw);
        if (!id || !Number.isFinite(point.x) || !Number.isFinite(point.y)) { retry(); return; }
        world.set(id, point);
      }
      const roots = pending.event.roots.map((ref) => display(ref.id));
      if (roots.some((id) => !id)) { retry(); return; }
      const ids = [...world.keys()];
      const byHand = pending.event.command === "paste" && pending.event.authority.pasteMode === "pointer";
      const manualPaste = byHand || pending.event.command === "paste" && arrangeMode === "manual";
      const autoPaste = pending.event.command === "paste" && !byHand && arrangeMode === "auto";
      const viewportGeneration = clipboardViewportGenerationRef.current;
      const laterCamera = viewportGeneration !== pending.viewportGeneration;
      const newerMove = ids.some((id) => (clipboardManualInteractionsRef.current.subjects.get(id) ?? 0) > pending.manualGeneration);
      if ((manualPaste || autoPaste) && (newerMove || phase.retired)) { finish(); return; }
      if (autoPaste && commandSelectionRef.current.generation !== pending.event.authority.generation) { finish(); return; }
      if (autoPaste && laterCamera) { finish(); return; }
      const matches = (value: Record<string, CanvasPosition>, expected: Record<string, CanvasPosition>) => ids.every((id) => same(value[id], expected[id]));
      if ((manualPaste || autoPaste) && phase.applied) {
        const expected = Object.fromEntries(phase.applied);
        if (matches(manualPositions, expected)) phase.awaitingState = false;
        else if (phase.awaitingState && phase.previous && matches(manualPositions, phase.previous)) { retry(); return; }
        else { finish(); return; }
      } else if ((manualPaste || autoPaste) && !matches(manualPositions, phase.baseline)) { finish(); return; }
      if (phase.writePending) { retry(); return; }
      const rendered = new Map(reactFlowInstance?.getNodes().map((node) => [node.id, node]) ?? []);
      if (manualPaste || autoPaste) {
        if (!reactFlowInstance || !ids.length || ids.some((id) => {
          const node = rendered.get(id); const width = node?.measured?.width ?? (node && nodeSize(node).width); const height = node?.measured?.height ?? (node && nodeSize(node).height);
          return !node || node.hidden || ![node.position.x, node.position.y, width, height].every(Number.isFinite) || !width || !height || width <= 0 || height <= 0;
        })) { retry(); return; }
      }
      const stage = canvasMeasureRootRef.current?.getBoundingClientRect();
      const pane = canvasPastePane(canvasMeasureRootRef.current, inspectorWidth, false);
      const controls = canvasPasteObstructions(canvasMeasureRootRef.current);
      let fits = true; let input = "";
      if (manualPaste && !laterCamera) {
        if (!reactFlowInstance || !stage || !pane) { retry(); return; }
        const toWorld = (rect: { x: number; y: number; width: number; height: number }) => {
          const a = reactFlowInstance.screenToFlowPosition(rect, { snapToGrid: false });
          const b = reactFlowInstance.screenToFlowPosition({ x: rect.x + rect.width, y: rect.y + rect.height }, { snapToGrid: false });
          return { ...a, width: b.x - a.x, height: b.y - a.y };
        };
        const area = toWorld({ x: pane.left, y: pane.top, width: pane.right - pane.left, height: pane.bottom - pane.top });
        const obstacles = controls.map(toWorld);
        const rects = [...world].map(([id, point]) => {
          const node = rendered.get(id)!;
          return { ...point, width: node.measured?.width ?? nodeSize(node).width, height: node.measured?.height ?? nodeSize(node).height };
        });
        input = JSON.stringify([rects, area, obstacles]);
        if (phase.manualInput !== input) {
          phase.manualInput = input;
          phase.delta = canvasPasteTranslationToBounds(rects, { left: area.x, top: area.y, right: area.x + area.width, bottom: area.y + area.height }, obstacles);
          phase.stableInput = undefined;
        }
        fits = Boolean(phase.delta);
        if (phase.delta) for (const [id, point] of world) world.set(id, { x: point.x + phase.delta.x, y: point.y + phase.delta.y });
      } else if (manualPaste && phase.applied) { finish(); return; }
      const absolute = (id: string, seen = new Set<string>()): CanvasPosition | null => {
        const own = world.get(id); if (own) return own;
        const node = currentNodesById.get(id); if (!node || seen.has(id)) return null;
        seen.add(id); const parent = node.parentId ? absolute(node.parentId, seen) : { x: 0, y: 0 }; seen.delete(id);
        return parent ? { x: parent.x + node.position.x, y: parent.y + node.position.y } : null;
      };
      const positions = new Map<string, CanvasPosition>();
      for (const [id, point] of world) {
        const node = currentNodesById.get(id)!; const parent = node.parentId ? absolute(node.parentId) : { x: 0, y: 0 };
        if (!parent) { retry(); return; }
        positions.set(id, { x: point.x - parent.x, y: point.y - parent.y });
      }
      if (pending.event.command === "duplicate") {
        if (arrangeMode !== "auto") setManualPositions((value) => ({ ...value, ...Object.fromEntries(positions) }));
        finish(roots as string[]);
        return;
      }
      if ((arrangeMode !== "auto" || byHand) && (!phase.applied || ids.some((id) => !same(phase.applied?.get(id), positions.get(id))))) {
        const expected = phase.applied ? Object.fromEntries(phase.applied) : phase.baseline;
        phase.previous = expected; phase.awaitingState = true; phase.writePending = true;
        setManualPositions((value) => {
          phase.writePending = false;
          const moved = ids.some((id) => (clipboardManualInteractionsRef.current.subjects.get(id) ?? 0) > pending.manualGeneration);
          if (!current() || moved || clipboardViewportGenerationRef.current !== viewportGeneration || !matches(value, expected)) { phase.retired = true; return value; }
          phase.applied = positions;
          return { ...value, ...Object.fromEntries(positions) };
        });
        retry(); return;
      }
      if (manualPaste) {
        if (laterCamera) { finish(); return; }
        if (!fits || !reactFlowInstance || !pane || !stage || ids.some((id) => !same(rendered.get(id)?.position, phase.applied?.get(id)))) { retry(); return; }
        const view = reactFlowInstance.getViewport();
        const rects = ids.map((id) => {
          const rect = reactFlowInstance.getNodesBounds([id]);
          return { x: stage.left + view.x + rect.x * view.zoom, y: stage.top + view.y + rect.y * view.zoom, width: rect.width * view.zoom, height: rect.height * view.zoom };
        });
        if (!canvasPasteRectsVisible(rects, pane, controls)) { retry(); return; }
      }
      if (autoPaste) {
        if (laterCamera || commandSelectionRef.current.generation !== pending.event.authority.generation) { finish(); return; }
        const fittingPane = canvasPastePane(canvasMeasureRootRef.current, inspectorWidth);
        if (!stage || !fittingPane || !reactFlowInstance) { retry(); return; }
        const bounds = reactFlowInstance.getNodesBounds(ids);
        input = JSON.stringify([bounds, fittingPane, stage.left, stage.top]);
        const target = canvasPasteViewport(bounds, fittingPane, stage);
        if (!target) { retry(); return; }
        if (phase.fitInput !== input) {
          if (!current() || clipboardViewportGenerationRef.current !== pending.viewportGeneration || commandSelectionRef.current.generation !== pending.event.authority.generation) { finish(); return; }
          phase.fitInput = input; phase.viewport = target; phase.stableInput = undefined;
          ignoreAsyncResult(reactFlowInstance.setViewport(target, { duration: 0 }));
          retry(); return;
        }
        const actual = reactFlowInstance.getViewport();
        if (!phase.viewport || !same(actual, phase.viewport) || Math.abs(actual.zoom - phase.viewport.zoom) > 0.0001 || !canvasPasteBoundsVisible(bounds, fittingPane, stage, actual)) { retry(); return; }
      }
      // One verification frame with identical positive geometry prevents the
      // first explicit estimate from completing before its next published size.
      if ((manualPaste || autoPaste) && phase.stableInput !== input) { phase.stableInput = input; retry(); return; }
      finish(roots as string[]);
    };
    attempt();
    return () => { stopped = true; window.cancelAnimationFrame(frame); };
  }, [
    arrangeMode, changeSet, commandViewKey, currentNodesById, manualPositions, inspectorWidth,
    pendingClipboardCommit, pendingClipboardCommitRef, placementHistoryOwners,
    reactFlowInstance, canvasMeasureRootRef, commandViewRef, clipboardViewGenerationRef,
    clipboardViewportGenerationRef, clipboardManualInteractionsRef, commandSelectionRef,
    setManualPositions, setPendingClipboardCommit, setClipboardBusy, setClipboardSelection,
  ]);
  useEffect(() => {
    if (!pendingClipboardCommit) return;
    const timeout = window.setTimeout(() => {
      if (pendingClipboardCommitRef.current !== pendingClipboardCommit) return;
      pendingClipboardCommitRef.current = null; phaseRef.current = null;
      setPendingClipboardCommit(null); setClipboardBusy(false);
      if (pendingClipboardCommit.viewKey === commandViewRef.current && pendingClipboardCommit.generation === clipboardViewGenerationRef.current && pendingClipboardCommit.event.authority.changeSet.hasGraphIntent(pendingClipboardCommit.event.intentToken)) {
        setGraphEditNotice({ label: clipboardTranslateRef.current("react.workflows.clipboard_visual_failed", "The copy was added, but its canvas placement needs attention."), tone: "error" });
      }
    }, 5000);
    return () => window.clearTimeout(timeout);
  }, [pendingClipboardCommit, pendingClipboardCommitRef, clipboardTranslateRef, commandViewRef, clipboardViewGenerationRef, setPendingClipboardCommit, setClipboardBusy, setGraphEditNotice]);
}
