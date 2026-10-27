import { getViewportForBounds, type Edge, type Node, type ReactFlowInstance } from "@xyflow/react";

import { nodeSize, type CanvasPosition } from "@/pages/workflows/builder/canvas-core";
import { canvasNodeLane, placeCanvasNode } from "./canvas-placement";
import { routeCanvasEdges, type CanvasEdgeRouteInput } from "./canvas-edge-routing";
import { outcomeHandleId } from "./api";
import type { WorkflowCanvasPort, WorkflowOutcome } from "@/pages/workflows/shared/api";
import { CANVAS_GRID, COLUMN_GAP } from "@/pages/workflows/builder/layout-constants";

type CanvasViewport = Pick<ReactFlowInstance, "getNodes" | "getNodesBounds" | "fitBounds"> & Partial<Pick<ReactFlowInstance, "setViewport">>;

/** Immediate pointer gestures stay native; deliberate controls use the DS duration. */
export function canvasMotionDuration(): number {
  if (globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return 0;
  const token = typeof document === "undefined" ? "" : getComputedStyle(document.documentElement).getPropertyValue("--fs-duration-standard").trim();
  const value = Number.parseFloat(token);
  return Number.isFinite(value) ? value * (token.endsWith("ms") ? 1 : 1000) : 200;
}

/** Fit explicit canvas dimensions even when controlled nodes are awaiting DOM measurement. */
export async function fitCanvasToNodes(
  instance: CanvasViewport | null | undefined,
  duration: number,
  size?: { width: number; height: number },
  mode: "initial" | "fit" | "selection" = "fit",
): Promise<boolean> {
  const visibleNodes = instance?.getNodes().filter((node) => !node.hidden && (mode !== "selection" || node.selected)) || [];
  if (!instance || !visibleNodes.length) return false;
  const bounds = instance.getNodesBounds(visibleNodes);
  if (!size || size.width <= 0 || size.height <= 0 || !instance.setViewport) {
    return instance.fitBounds(bounds, { duration, padding: 0.12 });
  }
  const padding = 24;
  const minimum = mode === "initial" ? 0.5 : 0.25;
  const fitted = Math.min((size.width - padding * 2) / Math.max(bounds.width, 1), (size.height - padding * 2) / Math.max(bounds.height, 1));
  const zoom = Math.max(minimum, Math.min(2, fitted));
  if (fitted < minimum) {
    const start = mode !== "selection" ? visibleNodes.find((node) => node.type === "workflowStart" || node.id === "visual:start") : undefined;
    const anchor = start ? instance.getNodesBounds([start]) : bounds;
    return instance.setViewport({ x: padding - anchor.x * zoom, y: padding - anchor.y * zoom, zoom }, { duration });
  }
  return instance.setViewport({ x: (size.width - bounds.width * zoom) / 2 - bounds.x * zoom, y: (size.height - bounds.height * zoom) / 2 - bounds.y * zoom, zoom }, { duration });
}

type ScreenPoint = { x: number; y: number };

export type CanvasPasteBounds = { left: number; top: number; right: number; bottom: number };
type CanvasPasteRect = { x: number; y: number; width: number; height: number };

/** Actual bottom-control rectangles. Existing graph cards are never collision obstacles for paste. */
export function canvasPasteObstructions(stage: HTMLElement | null): CanvasPasteRect[] {
  return [...(stage?.querySelectorAll<HTMLElement>(".workflow-canvas-toolbar, .workflow-canvas-minimap, .workflow-canvas-runbar") ?? [])]
    .map((control) => control.getBoundingClientRect())
    .filter((rect) => [rect.left, rect.top, rect.width, rect.height].every(Number.isFinite) && rect.width > 0 && rect.height > 0)
    .map((rect) => ({ x: rect.left, y: rect.top, width: rect.width, height: rect.height }));
}

/** Stage/drawer safe bounds; a conservative above-controls rectangle is only a fitting fallback. */
export function canvasPastePane(stage: HTMLElement | null, inspectorWidth: number, aboveControls = true): CanvasPasteBounds | null {
  if (!stage) return null;
  const rect = stage.getBoundingClientRect();
  if (![rect.left, rect.top, rect.right, rect.bottom, inspectorWidth].every(Number.isFinite)) return null;
  const bounds = { left: rect.left + 16, top: rect.top + 16, right: rect.right - Math.max(0, inspectorWidth) - 16, bottom: rect.bottom - 16 };
  if (aboveControls) for (const control of canvasPasteObstructions(stage)) {
    if (control.x + control.width > bounds.left && control.x < bounds.right && control.y > rect.top && control.y < rect.bottom) {
      bounds.bottom = Math.min(bounds.bottom, control.y - 16);
    }
  }
  return bounds.right > bounds.left && bounds.bottom > bounds.top ? bounds : null;
}

/** Preserve an already usable anchor, including intentional source-card overlap and empty gaps between cards. */
export function canvasPasteRectsVisible(rects: readonly CanvasPasteRect[], bounds: CanvasPasteBounds, obstructions: readonly CanvasPasteRect[] = []) {
  const valid = (rect: CanvasPasteRect) => [rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) && rect.width > 0 && rect.height > 0;
  if (!rects.length || ![bounds.left, bounds.top, bounds.right, bounds.bottom].every(Number.isFinite) || bounds.right <= bounds.left || bounds.bottom <= bounds.top || !rects.every(valid) || !obstructions.every(valid)) return false;
  return rects.every((rect) => rect.x >= bounds.left - 0.01 && rect.y >= bounds.top - 0.01 && rect.x + rect.width <= bounds.right + 0.01 && rect.y + rect.height <= bounds.bottom + 0.01 &&
    !obstructions.some((control) => rect.x < control.x + control.width - 1e-7 && rect.x + rect.width > control.x + 1e-7 && rect.y < control.y + control.height - 1e-7 && rect.y + rect.height > control.y + 1e-7));
}

/** Finite shared grid translations against the pane and actual controls; never against existing cards. */
export function canvasPasteTranslationToBounds(
  rects: readonly CanvasPasteRect[],
  bounds: CanvasPasteBounds,
  obstructions: readonly CanvasPasteRect[] = [],
): CanvasPosition | null {
  const valid = (rect: CanvasPasteRect) => [rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) && rect.width > 0 && rect.height > 0;
  if (!rects.length || ![bounds.left, bounds.top, bounds.right, bounds.bottom].every(Number.isFinite) || bounds.right <= bounds.left || bounds.bottom <= bounds.top || !rects.every(valid) || !obstructions.every(valid)) return null;
  const left = Math.min(...rects.map((rect) => rect.x));
  const right = Math.max(...rects.map((rect) => rect.x + rect.width));
  const top = Math.min(...rects.map((rect) => rect.y));
  const bottom = Math.max(...rects.map((rect) => rect.y + rect.height));
  const ceil = (value: number) => Math.ceil(value / CANVAS_GRID - 1e-9) * CANVAS_GRID;
  const floor = (value: number) => Math.floor(value / CANVAS_GRID + 1e-9) * CANVAS_GRID;
  const minX = ceil(bounds.left - left); const maxX = floor(bounds.right - right);
  const minY = ceil(bounds.top - top); const maxY = floor(bounds.bottom - bottom);
  if (minX > maxX || minY > maxY) return null;
  // Each forbidden common-delta region is an open rectangle. Its boundary,
  // pane bounds, or the nearest-to-zero grid point contains a minimum.
  const xs = new Set([minX, maxX, Math.max(minX, Math.min(maxX, 0))]);
  const ys = new Set([minY, maxY, Math.max(minY, Math.min(maxY, 0))]);
  for (const rect of rects) for (const control of obstructions) {
    xs.add(floor(control.x - rect.x - rect.width));
    xs.add(ceil(control.x + control.width - rect.x));
    ys.add(floor(control.y - rect.y - rect.height));
    ys.add(ceil(control.y + control.height - rect.y));
  }
  const key = (point: CanvasPosition) => [point.x * point.x + point.y * point.y, Math.abs(point.x), point.y < 0 ? 1 : 0, point.x < 0 ? 1 : 0, point.y, point.x];
  const less = (a: number[], b: number[]) => {
    for (let index = 0; index < a.length; index += 1) if (a[index] !== b[index]) return a[index] < b[index];
    return false;
  };
  let best: CanvasPosition | null = null;
  for (const x of xs) for (const y of ys) {
    if (x < minX || x > maxX || y < minY || y > maxY) continue;
    const candidate = { x, y };
    if (best && !less(key(candidate), key(best))) continue;
    const translated = rects.map((rect) => ({ ...rect, x: rect.x + x, y: rect.y + y }));
    if (!canvasPasteRectsVisible(translated, bounds, obstructions)) continue;
    if (x === 0 && y === 0) return candidate;
    best = candidate;
  }
  return best;
}

/** Validate the actual published batch rectangle in client pixels, not its intended anchor. */
export function canvasPasteBoundsVisible(bounds: CanvasPasteRect, pane: CanvasPasteBounds, stage: Pick<DOMRect, "left" | "top">, viewport: { x: number; y: number; zoom: number }) {
  if (![bounds.x, bounds.y, bounds.width, bounds.height, pane.left, pane.top, pane.right, pane.bottom, stage.left, stage.top, viewport.x, viewport.y, viewport.zoom].every(Number.isFinite) ||
    bounds.width <= 0 || bounds.height <= 0 || viewport.zoom <= 0 || pane.right <= pane.left || pane.bottom <= pane.top) return false;
  return bounds.x * viewport.zoom + viewport.x + stage.left >= pane.left - 0.01 && bounds.y * viewport.zoom + viewport.y + stage.top >= pane.top - 0.01 &&
    (bounds.x + bounds.width) * viewport.zoom + viewport.x + stage.left <= pane.right + 0.01 && (bounds.y + bounds.height) * viewport.zoom + viewport.y + stage.top <= pane.bottom + 0.01;
}

/** ReactFlow's own bounds/fit math, with the usable pane's stage-local offset. */
export function canvasPasteViewport(bounds: CanvasPasteRect, pane: CanvasPasteBounds, stage: Pick<DOMRect, "left" | "top">) {
  if (![bounds.x, bounds.y, bounds.width, bounds.height, pane.left, pane.top, pane.right, pane.bottom, stage.left, stage.top].every(Number.isFinite) ||
    bounds.width <= 0 || bounds.height <= 0 || pane.right <= pane.left || pane.bottom <= pane.top) return null;
  const viewport = getViewportForBounds(bounds, pane.right - pane.left, pane.bottom - pane.top, 0.25, 2, 0.12);
  const result = { ...viewport, x: viewport.x + pane.left - stage.left, y: viewport.y + pane.top - stage.top };
  if (!canvasPasteBoundsVisible(bounds, pane, stage, result)) return null;
  return result;
}


/**
 * Where a root node goes so it sits in the middle of what the author sees, or
 * null when it cannot be placed there: a node inside a container keeps its
 * container layout, and an unmeasured canvas has no visible area yet.
 */
export function positionAtViewportCentre(
  node: Pick<Node, "height" | "parentId" | "width">,
  bounds: Pick<DOMRect, "height" | "left" | "top" | "width"> | null | undefined,
  screenToFlowPosition: (point: ScreenPoint) => ScreenPoint,
): ScreenPoint | null {
  if (node.parentId || !bounds || bounds.width <= 0 || bounds.height <= 0) return null;
  const centre = screenToFlowPosition({ x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 });
  return { x: centre.x - (Number(node.width) || 0) / 2, y: centre.y - (Number(node.height) || 0) / 2 };
}

/**
 * Where a node connected from a hand-placed source goes: one layout column to
 * the right of that source, on the same row, in the same container. The source
 * no longer sits on the layout grid, so the grid slot of the new node can land
 * on top of it. Null when no incoming source in the same container was placed
 * by hand; the layout keeps the node then.
 */
export function positionRightOfManualSource(
  node: Pick<Node, "id" | "parentId">,
  edges: ReadonlyArray<Pick<Edge, "source" | "target" | "sourceHandle" | "targetHandle">>,
  nodesById: ReadonlyMap<string, Node>,
  manualPositions: Readonly<Record<string, CanvasPosition>>,
): ScreenPoint | null {
  for (const edge of edges) {
    const manual = edge.target === node.id ? manualPositions[edge.source] : undefined;
    const source = manual ? nodesById.get(edge.source) : undefined;
    if (!manual || !source || source.parentId !== node.parentId) continue;
    const sourcePosition = source.position;
    const target = nodesById.get(node.id);
    if (target && canvasNodeLane(source) !== canvasNodeLane(target)) continue;
    const sourceHandle = source.handles?.find((handle) => handle.type === "source" && handle.id === (edge.sourceHandle ?? "success"));
    const targetHandle = target?.handles?.find((handle) => handle.type === "target" && handle.id === (edge.targetHandle ?? "in"));
    const offset = sourceHandle && targetHandle ? sourceHandle.y + (sourceHandle.height ?? 10) / 2 - targetHandle.y - (targetHandle.height ?? 10) / 2 : 0;
    const requested = { x: sourcePosition.x + nodeSize(source).width + COLUMN_GAP, y: sourcePosition.y + offset };
    return target ? placeCanvasNode(target, requested, [...nodesById.values()]) : requested;
  }
  return null;
}


export type CanvasNextStepSlot = {
  outcome: WorkflowOutcome;
  label: string;
  sourcePoint: ScreenPoint;
  inputPoint: ScreenPoint;
  preferredPosition: ScreenPoint;
  rect: ScreenPoint & { width: number; height: number };
  path: string;
};

/** Actual output centers seed the existing nearest-free 16px placement helper. */
export function canvasNextStepSlots(source: Node, nodes: readonly Node[], template: Node, edges: readonly Edge[] = []): CanvasNextStepSlot[] {
  const data = source.data as { freeOutcomes?: string[]; step?: { kind?: string; ports?: WorkflowCanvasPort[] }; ports?: WorkflowCanvasPort[] };
  const ports = data.step?.ports ?? data.ports;
  const input = template.handles?.find((handle) => handle.type === "target" && handle.id === "in");
  if (!input || !ports || !data.freeOutcomes?.length) return [];
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const parentOffset = { x: 0, y: 0 };
  const visited = new Set<string>();
  let parent = source.parentId ? byId.get(source.parentId) : undefined;
  while (parent && !visited.has(parent.id)) {
    visited.add(parent.id); parentOffset.x += parent.position.x; parentOffset.y += parent.position.y;
    parent = parent.parentId ? byId.get(parent.parentId) : undefined;
  }
  const absoluteRect = (node: Node) => {
    const point = { ...node.position }; const seen = new Set<string>();
    let owner = node.parentId ? byId.get(node.parentId) : undefined;
    while (owner && !seen.has(owner.id)) { seen.add(owner.id); point.x += owner.position.x; point.y += owner.position.y; owner = owner.parentId ? byId.get(owner.parentId) : undefined; }
    return { ...point, ...nodeSize(node) };
  };
  const target: Node = { ...template, id: "visual:next-slot", parentId: source.parentId, data: { ...template.data, step: { lane_id: canvasNodeLane(source) } } };
  const size = nodeSize(target);
  const inputOffset = { x: input.x + (input.width ?? 0) / 2, y: input.y + (input.height ?? 0) / 2 };
  const slots: CanvasNextStepSlot[] = [];
  const occupied = [...nodes];
  const routes: CanvasEdgeRouteInput[] = [];
  let previousBranchBottom: number | undefined;
  for (const port of ports) {
    const key = outcomeHandleId(port.outcome);
    const handle = source.handles?.find((item) => item.type === "source" && item.id === key);
    if (!handle) continue;
    const orderedBranch = data.step?.kind === "switch" && handle.position === "right" && (port.outcome.name === "case" || port.outcome.name === "default");
    if (!data.freeOutcomes.includes(key)) {
      if (orderedBranch) for (const edge of edges) {
        if (edge.source !== source.id || edge.sourceHandle !== key) continue;
        const connected = byId.get(edge.target);
        if (!connected || connected.parentId !== source.parentId || canvasNodeLane(connected) !== canvasNodeLane(source)) continue;
        const bottom = connected.position.y + nodeSize(connected).height;
        if (Number.isFinite(bottom)) previousBranchBottom = Math.max(previousBranchBottom ?? -Infinity, bottom);
      }
      continue;
    }
    const localSource = { x: source.position.x + handle.x + (handle.width ?? 0) / 2, y: source.position.y + handle.y + (handle.height ?? 0) / 2 };
    const preferred = { x: source.position.x + nodeSize(source).width + COLUMN_GAP, y: localSource.y - inputOffset.y };
    const columnX = Math.round(preferred.x / CANVAS_GRID) * CANVAS_GRID;
    // A common column lets the shared curve shape preserve branch order.
    // Only the vertical floor grows; finite obstacles cannot exhaust this column.
    const bounds = orderedBranch ? { minX: columnX, maxX: columnX, minY: previousBranchBottom } : undefined;
    const resolved = placeCanvasNode(target, preferred, occupied, bounds);
    const rect = { x: resolved.x + parentOffset.x, y: resolved.y + parentOffset.y, ...size };
    const sourcePoint = { x: localSource.x + parentOffset.x, y: localSource.y + parentOffset.y };
    const inputPoint = { x: rect.x + inputOffset.x, y: rect.y + inputOffset.y };
    routes.push({ source: sourcePoint, target: inputPoint, sourceSide: handle.position === "bottom" ? "bottom" : "right", targetSide: input.position === "bottom" ? "bottom" : "left", obstacles: nodes.filter((node) => node.id !== source.id && node.type !== "canvasNote" && !visited.has(node.id)).map(absoluteRect), endCards: [absoluteRect(source), rect], backward: rect.x <= sourcePoint.x, failure: port.outcome.name === "error", repeat: false, ...(orderedBranch ? { shapeGroupKey: `next-switch:${source.id}` } : {}) });
    slots.push({ outcome: port.outcome, label: port.label, sourcePoint, preferredPosition: { x: preferred.x + parentOffset.x, y: preferred.y + parentOffset.y }, inputPoint, rect, path: "" });
    occupied.push({ ...target, id: `visual:next-slot:${key}`, position: resolved });
    if (orderedBranch) previousBranchBottom = resolved.y + size.height;
  }
  const routed = routeCanvasEdges(routes);
  return slots.map((slot, i) => ({ ...slot, path: routed[i].path }));
}
