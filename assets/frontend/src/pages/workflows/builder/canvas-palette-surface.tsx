import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactFlowInstance } from "@xyflow/react";
import { WorkflowCanvasBuilderSurface } from "./canvas";
import { CanvasAddStepPalette } from "./canvas-add-step-palette";
import { canvasConnectionDefinition, connectionPlacement } from "./canvas-connection-policy";
import { hookGroupId, parseHookNodeId, withHookFrames } from "./hook-frames";
import { nodeIdForRef } from "./layout";
import { portEdge } from "./change-set";
import { outcomeHandleId } from "./api";
import { setInlineLoop } from "./canvas-inline-loop-layout";
import { usePaletteDrag, type PaletteDropTarget } from "./canvas-palette-drag";
import type { PaletteEntry } from "./canvas-palette-catalog";
import type { BuilderSurfaceProps, CanvasCreationIntent, CanvasCreationSelection, CanvasPlacementCommand, CanvasPlacementPoint, CreateLocalStepRequest, LocalCanvasChoice, NewContainerDefaults, PaletteChoice } from "./canvas-types";
import type { WorkflowCanvasGraph, WorkflowGraphEdge, WorkflowNodeRef, WorkflowOutcome, WorkflowTargetRef } from "@/pages/workflows/shared/api";
import { useI18n } from "@/lib/i18n/useI18n";

type PaletteRequest = {
  requestId: string;
  context: CreateLocalStepRequest["context"];
  intent?: CanvasCreationIntent;
  expectedEdge?: WorkflowGraphEdge;
  clientPoint: { x: number; y: number };
  /** Asked for at a point of the canvas (right-click › Add step here): the new node lands there. */
  at?: CanvasPlacementPoint;
  returnFocus: HTMLElement | null;
};

/** A node's name for Add step's title: steps, groups and For each by their display names. */
function nodeName(graph: WorkflowCanvasGraph, ref: WorkflowNodeRef | WorkflowTargetRef): string {
  if (ref.kind === "step") { const step = graph.steps.find((item) => (item.step_id || item.id) === ref.id || item.id === ref.id); return step?.display_name || ref.id; }
  if (ref.kind === "group") return graph.groups.find((item) => item.id === ref.id)?.display_name || ref.id;
  if (ref.kind === "for_each") return graph.for_each?.find((item) => item.id === ref.id)?.display_name || ref.id;
  return "";
}

function outcomeName(graph: WorkflowCanvasGraph, source: WorkflowNodeRef, outcome: WorkflowOutcome, fallback: string): string {
  if (source.kind !== "step") return outcome.name === "error" ? "" : fallback;
  const step = graph.steps.find((item) => (item.step_id || item.id) === source.id || item.id === source.id);
  return step?.ports?.find((port) => outcomeHandleId(port.outcome) === outcomeHandleId(outcome))?.label || fallback;
}

function laneIds(body: Record<string, unknown> | undefined): string[] {
  if (body?.kind !== "lanes" || !Array.isArray(body.lanes)) return [];
  return body.lanes.flatMap((lane) => {
    if (!lane || typeof lane !== "object") return [];
    const laneId = (lane as Record<string, unknown>).lane_id;
    return typeof laneId === "string" && laneId ? [laneId] : [];
  });
}

/** Presentation composition only. The raw Surface retains its gesture contract;
 * the detail-owned command is the sole creation authority. */
export function WorkflowCanvasPaletteSurface(props: Readonly<BuilderSurfaceProps>) {
  const { t } = useI18n();
  const { onCanvasReady, onCreateStep, workflowId } = props;
  const root = useRef<HTMLDivElement>(null);
  const [instance, setInstance] = useState<ReactFlowInstance | null>(null);
  const [request, setRequest] = useState<PaletteRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [creationSelection, setCreationSelection] = useState<CanvasCreationSelection | null>(null);
  const [creationIntentRetireNonce, setCreationIntentRetireNonce] = useState(0);
  const [placementCommand, setPlacementCommand] = useState<CanvasPlacementCommand | null>(null);
  const placementSerial = useRef(0);
  const creationSerial = useRef(0);
  const serial = useRef(0);
  const activeRequest = useRef("");
  const pending = useRef<AbortController | null>(null);
  const lastControl = useRef<HTMLElement | null>(null);
  const graph = useMemo(() => props.hookFrames?.size ? withHookFrames(props.canvasGraph, props.hookFrames) : props.canvasGraph, [props.canvasGraph, props.hookFrames]);
  const definition = useMemo(() => canvasConnectionDefinition(graph), [graph]);
  useEffect(() => () => { pending.current?.abort(); }, []);
  const ready = useCallback((flow: ReactFlowInstance | null) => { setInstance(flow); onCanvasReady?.(flow); }, [onCanvasReady]);
  const dismiss = useCallback(() => {
    pending.current?.abort(); pending.current = null; activeRequest.current = "";
    setBusy(false); setRequest(null); setError("");
    setCreationIntentRetireNonce((current) => current + 1);
    request?.returnFocus?.focus();
  }, [request]);
  const open = useCallback((context: PaletteRequest["context"] = {}, intent?: CanvasCreationIntent, expectedEdge?: WorkflowGraphEdge, at?: CanvasPlacementPoint) => {
    pending.current?.abort(); pending.current = null; activeRequest.current = "";
    const canvas = root.current?.querySelector(".react-flow");
    const canvasBounds = canvas?.getBoundingClientRect();
    const target = lastControl.current?.isConnected ? lastControl.current : document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const controlBounds = target?.getBoundingClientRect();
    let clientPoint = at?.clientPoint ?? intent?.clientPoint;
    if (!clientPoint && intent?.point && instance) clientPoint = instance.flowToScreenPosition(intent.point);
    if (!clientPoint && intent && instance) {
      const source = instance.getNode(nodeIdForRef(intent.source));
      if (source) {
        const bounds = instance.getNodesBounds([source]);
        const handle = source.handles?.find((item) => item.type === "source" && item.id === outcomeHandleId(intent.outcome));
        clientPoint = instance.flowToScreenPosition({ x: bounds.x + (handle?.x ?? bounds.width) + (handle?.width ?? 0) / 2, y: bounds.y + (handle?.y ?? 0) + (handle?.height ?? 0) / 2 });
      }
    }
    clientPoint ??= controlBounds && (controlBounds.width || controlBounds.height)
      ? { x: controlBounds.left, y: controlBounds.top }
      : { x: canvasBounds ? canvasBounds.left + canvasBounds.width / 2 : innerWidth / 2, y: canvasBounds ? canvasBounds.top + canvasBounds.height / 2 : innerHeight / 2 };
    const requestId = `canvas-choice-${++serial.current}`;
    setError(""); setBusy(false);
    const next = { requestId, context, intent, expectedEdge, clientPoint, at, returnFocus: target };
    setRequest(next);
    return next;
  }, [instance]);
  const append = useCallback((source: WorkflowNodeRef, outcome: WorkflowOutcome, intent?: CanvasCreationIntent) => {
    const scope = connectionPlacement(definition, source);
    if (!scope) return;
    const complete = intent || { source, outcome, scope };
    const existing = portEdge(definition, source, outcome);
    open({ ...scope, insert: { source, outcome, label: `${source.id} · ${outcomeHandleId(outcome)}`, append: !existing } }, complete, existing || undefined);
  }, [definition, open]);
  const insert = useCallback((edge: WorkflowGraphEdge, intent?: CanvasCreationIntent) => {
    const scope = connectionPlacement(definition, edge.source);
    if (!scope) return;
    open({ ...scope, insert: { source: edge.source, outcome: edge.outcome, label: `${edge.source.id} · ${outcomeHandleId(edge.outcome)}` } }, intent || { source: edge.source, outcome: edge.outcome, scope }, edge);
  }, [definition, open]);
  const placeNextAt = useCallback((point: { x: number; y: number }) => {
    setPlacementCommand({ kind: "place-next", point, nonce: ++placementSerial.current });
  }, []);
  const choose = useCallback(async (choice: LocalCanvasChoice, selection: PaletteRequest | null = request) => {
    if (!selection || activeRequest.current) return;
    if (!onCreateStep) { setError(t("react.workflows.palette_creation_unavailable", "Step creation is unavailable. Reopen the workflow and try again.")); return; }
    activeRequest.current = selection.requestId;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError("");
    // A node asked for at a point (and not after a port) lands where it was asked for.
    if (selection.at && !selection.context.insert) placeNextAt(selection.at.point);
    try {
      const result = await onCreateStep({ requestId: selection.requestId, choice, context: selection.context, intent: selection.intent, at: selection.at, expectedEdge: selection.expectedEdge, signal: controller.signal });
      if (controller.signal.aborted || activeRequest.current !== selection.requestId) return;
      if (result.status === "created") {
        const nodeId = "canvasStepId" in result ? result.canvasNodeId || result.canvasStepId : result.canvasNodeId;
        if (choice.nodeKind === "for_each" && choice.presentation === "inline" && "nodeId" in result) setInlineLoop(workflowId, result.nodeId, true);
        setCreationSelection({ nodeId, nonce: ++creationSerial.current, selectionGeneration: selection.intent?.selectionGeneration });
        setRequest(null);
        setCreationIntentRetireNonce((current) => current + 1);
      }
      else if (result.status === "cancelled") dismiss();
      else setError(result.message || t("react.workflows.palette_creation_refused", "The step could not be added. Resolve the current edit and try again."));
    } catch (error_) {
      if (!controller.signal.aborted) setError(error_ instanceof Error ? error_.message : t("react.workflows.palette_creation_unavailable", "Step creation is unavailable. Reopen the workflow and try again."));
    } finally {
      if (activeRequest.current === selection.requestId) { activeRequest.current = ""; pending.current = null; setBusy(false); }
    }
  }, [dismiss, onCreateStep, placeNextAt, request, t, workflowId]);
  const chooseFromPalette = useCallback((choice: PaletteChoice, selection: PaletteRequest | null = request) => {
    if (choice.nodeKind === "note") {
      setPlacementCommand({ kind: "add-note", ...(selection?.at ? { point: selection.at.point } : {}), nonce: ++placementSerial.current });
      setRequest(null);
      return;
    }
    void choose(choice, selection);
  }, [choose, request]);
  const drop = useCallback((entry: PaletteEntry, target: PaletteDropTarget) => {
    if (!request) return;
    const at = { point: target.point, clientPoint: target.clientPoint };
    // Opened from a port or a connection, Add step keeps that connection and only takes the drop's place.
    const selection: PaletteRequest = request.context.insert
      ? { ...request, intent: request.intent ? { ...request.intent, point: target.point, clientPoint: target.clientPoint } : request.intent }
      : { ...request, context: { groupId: target.groupId, forEachId: target.forEachId, laneId: target.laneId }, at };
    chooseFromPalette(entry.choice, selection);
  }, [chooseFromPalette, request]);
  const paletteDrag = usePaletteDrag({ instance, canvasRoot: () => root.current?.querySelector(".react-flow") ?? null, t, onDrop: drop });
  const createContainer = (defaults: NewContainerDefaults = {}) => {
    const nodeKind = defaults.containerKind || "group";
    const selection = open({ groupId: defaults.parentGroupId || (defaults.hookId ? hookGroupId(defaults.hookId) : "") });
    const lanes = nodeKind === "group" && defaults.bodyKind === "lanes";
    const displayName = nodeKind === "for_each" ? t("react.workflows.for_each", "For each") : lanes ? t("react.workflows.group_kind_parallel", "Parallel lanes") : t("react.workflows.group", "Group");
    void choose({ nodeKind, catalogKey: `container:${nodeKind}`, displayName, family: nodeKind === "group" ? "Groups" : "Flow", containerDefaults: { containerKind: nodeKind, bodyKind: defaults.bodyKind } }, selection);
  };
  const scope = request?.context;
  const forEach = graph.for_each?.find((item) => item.id === scope?.forEachId);
  const group = graph.groups.find((item) => item.id === scope?.groupId);
  const hookRoot = Boolean(scope?.groupId && parseHookNodeId(scope.groupId) && !parseHookNodeId(scope.groupId)?.memberStepId);
  const rootScope = !scope?.groupId && !scope?.forEachId && !scope?.laneId;
  const sequenceGroup = Boolean(group && group.body?.kind === "sequence" && !scope?.laneId && !scope?.forEachId);
  let title = t("react.workflows.add_step_title", "Add step");
  let subtitle = t("react.workflows.palette_unconnected", "In view, not connected yet");
  const insertion = scope?.insert;
  if (insertion && request?.expectedEdge) {
    title = t("react.workflows.palette_insert_title", "Insert on {source} · {outcome} → {target}", { source: nodeName(graph, insertion.source), outcome: outcomeName(graph, insertion.source, insertion.outcome, t("react.workflows.outcome_success", "Next")), target: nodeName(graph, request.expectedEdge.target) || t("react.workflows.exit", "Exit") });
    subtitle = "";
  } else if (insertion) {
    title = t("react.workflows.palette_append_title", "Add after {source} · {outcome}", { source: nodeName(graph, insertion.source), outcome: outcomeName(graph, insertion.source, insertion.outcome, t("react.workflows.outcome_success", "Next")) });
    subtitle = "";
  } else if (group || forEach) {
    const owner = group?.display_name || forEach?.display_name || "";
    if (scope?.laneId) {
      const ownerLanes = laneIds(group?.body || forEach?.body);
      const laneIndex = ownerLanes.indexOf(scope.laneId);
      const laneLabel = laneIndex >= 0
        ? t("react.workflows.parallel_lane_label", "Parallel lane {lane}", { lane: laneIndex + 1 })
        : t("react.workflows.parallel_lane", "Parallel lane");
      subtitle = t("react.workflows.palette_in_lane_ordinal", "In {group} · {lane}", { group: owner, lane: laneLabel });
    } else {
      subtitle = t("react.workflows.palette_in_group", "In {group}", { group: owner });
    }
  } else if (request?.at) {
    subtitle = t("react.workflows.palette_here", "Here, not connected yet");
  }
  return <div className="contents" ref={root} onClickCapture={(event) => {
    if (event.target instanceof Element) lastControl.current = event.target.closest<HTMLElement>("button, [role=button], [role=group]");
  }}>
    <WorkflowCanvasBuilderSurface {...props} creationSelection={creationSelection} creationIntentRetireNonce={creationIntentRetireNonce} canvasPlacementCommand={placementCommand} onCanvasReady={ready} onAppendStep={append} onInsertStep={insert} onOpenNewStep={(context, at) => open(context, undefined, undefined, at)} onOpenNewGroup={createContainer} />
    {request ? <CanvasAddStepPalette key={request.requestId} clientPoint={request.clientPoint} title={title} subtitle={subtitle} projectId={props.projectId} laneId={scope?.laneId || undefined} sequenceForEach={Boolean(forEach?.body?.kind === "sequence" && !scope?.laneId && !scope?.groupId)} allowHookDeclaration={Boolean(props.allowHookDeclaration && rootScope && !props.structureLocked && onCreateStep)} allowGroup={rootScope || hookRoot} allowForEach={rootScope || hookRoot || sequenceGroup} inserting={Boolean(request.expectedEdge)} connected={Boolean(request.context.insert)} busy={busy} error={error} dragging={paletteDrag.dragging} onChoose={(choice) => { if (!paletteDrag.consumeClick()) chooseFromPalette(choice); }} onDragStart={paletteDrag.onDragStart} onDismiss={dismiss} /> : null}
    {paletteDrag.overlay}
  </div>;
}
