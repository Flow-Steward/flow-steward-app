/** Routes every canvas connection, reusing what has not moved. Page-local geometry only. */
import type { Node } from "@xyflow/react";
import type { CanvasPosition } from "./canvas-core";
import { canvasEdgeLabelPoint } from "./canvas-edge-label-placement";
import { routeCanvasEdges, type CanvasEdgeRoute, type CanvasEdgeRouteInput, type CanvasRect } from "./canvas-edge-routing";
import { createWrappedRootRouteHints } from "./canvas-wrap-routing";
import type { buildCanvasLayout, CanvasEdgeData } from "./layout";

type CanvasLayout = ReturnType<typeof buildCanvasLayout>;
type RoutedEdge = CanvasLayout["edges"][number];

/** What the last pass saw and drew; the next pass reuses it where nothing changed. */
export type CanvasEdgeRouteMemo = {
  layoutNodes: CanvasLayout["nodes"];
  layoutEdges: CanvasLayout["edges"];
  rootRows: CanvasLayout["rootRows"];
  /** Every card's place and size, as drawn. */
  geometry: string;
  /** A drag pass leaves the connections it did not move where they were; the next settled pass routes all. */
  settled: boolean;
  sources: ReadonlyMap<string, RoutedEdge>;
  routed: ReadonlyMap<string, RoutedEdge>;
  edges: RoutedEdge[];
};

type RouteSetInput = {
  /** The layout's own cards: their handles and wrapped rows. */
  nodes: CanvasLayout["nodes"];
  /** The same cards where they are drawn now (manual places, frames fitted). */
  positioned: readonly Node[];
  rootRows: CanvasLayout["rootRows"];
  edges: CanvasLayout["edges"];
  /** Cards the author is dragging right now; empty when nothing moves. */
  draggingIds: ReadonlySet<string>;
  previous: CanvasEdgeRouteMemo | null;
};

/**
 * The cards a drag can move on screen: each dragged card's whole top-level card or frame, with
 * everything inside it (a frame fits itself round its members, so a member's drag can shift them all).
 */
export function canvasDragScope(positioned: readonly Node[], draggingIds: ReadonlySet<string>): Set<string> {
  const byId = new Map(positioned.map((node) => [node.id, node]));
  const rootOf = (id: string) => {
    let current = byId.get(id); const seen = new Set<string>();
    while (current?.parentId && !seen.has(current.id) && byId.has(current.parentId)) { seen.add(current.id); current = byId.get(current.parentId); }
    return current?.id ?? id;
  };
  const roots = new Set([...draggingIds].map(rootOf));
  return new Set(positioned.filter((node) => roots.has(rootOf(node.id))).map((node) => node.id));
}

/**
 * The top of the row a connection drops into: the target card and every card of its own scope that
 * stands beside it below the source. A connection that wraps down to an earlier column comes in through
 * the gap above that row, the same way an automatic row wrap does, whatever moved the cards there.
 */
export function droppedRowTop(source: CanvasRect, target: CanvasRect, siblings: readonly CanvasRect[]): number {
  const sourceBottom = source.y + source.height;
  const targetBottom = target.y + target.height;
  return siblings.reduce((top, card) => (
    card.y >= sourceBottom && card.y < targetBottom && card.y + card.height > target.y ? Math.min(top, card.y) : top
  ), target.y);
}

export function routeCanvasEdgeSet({ nodes, positioned, rootRows, edges, draggingIds, previous }: RouteSetInput): CanvasEdgeRouteMemo {
  const byId = new Map(positioned.map((node) => [node.id, node]));
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
  const rectangles = new Map(positioned.map((node) => [node.id, rectangle(node)]));
  const geometry = positioned.map((node) => { const rect = rectangles.get(node.id)!; return `${node.id}|${node.type}|${node.parentId ?? ""}|${rect.x},${rect.y},${rect.width},${rect.height}`; }).join(";");
  const sameLayout = Boolean(previous && previous.layoutNodes === nodes && previous.layoutEdges === edges && previous.rootRows === rootRows);
  // Hover, highlight and connection hints redraw the cards but move nothing: the drawn connections stand.
  if (previous && sameLayout && previous.settled && previous.geometry === geometry && !draggingIds.size) return previous;
  // While a card is dragged, only its own connections follow it, drawn without detours; the rest stay put.
  const moving = draggingIds.size && previous && sameLayout ? canvasDragScope(positioned, draggingIds) : null;
  const wrappedRouteHints = createWrappedRootRouteHints(rootRows, nodes);
  const inputs: CanvasEdgeRouteInput[] = []; const routedIds: string[] = [];
  for (const edge of edges) {
    const source = byId.get(edge.source); const target = byId.get(edge.target);
    if (!source || !target || !edge.data) continue;
    if (moving && !moving.has(source.id) && !moving.has(target.id) && previous!.routed.has(edge.id)) continue;
    const sourceHandle = source.handles?.find((handle) => handle.type === "source" && handle.id === edge.sourceHandle);
    const targetHandle = target.handles?.find((handle) => handle.type === "target" && handle.id === edge.targetHandle);
    if (!sourceHandle || !targetHandle) continue;
    const sourceRect = rectangles.get(source.id)!; const targetRect = rectangles.get(target.id)!;
    const obstacles = moving ? [] : positioned.filter((node) => node.type !== "canvasNote" && node.id !== source.id && node.id !== target.id && !isAncestor(node, source) && !isAncestor(node, target)).map((node) => rectangles.get(node.id)!);
    const sourceSide = sourceHandle.position === "bottom" ? "bottom" : "right";
    const targetSide = targetHandle.position === "bottom" ? "bottom" : "left";
    let rowHint = wrappedRouteHints({
      source: { node: source, rect: sourceRect }, target: { node: target, rect: targetRect },
      sameScope: Boolean(edge.data.routing?.sameScope),
      failure: Boolean(edge.data.failurePath), repeat: Boolean(edge.data.rework),
      sourceSide, targetSide,
    });
    // Without the automatic row (a frame fitted round its members, a card placed by hand), a connection that
    // drops to a later row still comes in from above: never round under the row it goes to.
    const dropsToLaterRow = rowHint.backward === undefined && Boolean(edge.data.routing?.sameScope) &&
      !edge.data.failurePath && !edge.data.rework && sourceSide === "right" && targetSide === "left" &&
      targetRect.x <= sourceRect.x && targetRect.y >= sourceRect.y + sourceRect.height;
    if (dropsToLaterRow) {
      const siblings = positioned.filter((node) => node.type !== "canvasNote" && node.parentId === target.parentId).map((node) => rectangles.get(node.id)!);
      rowHint = { backward: false, targetRowTop: droppedRowTop(sourceRect, targetRect, siblings) };
    }
    inputs.push({ source: { x: sourceRect.x + sourceHandle.x + (sourceHandle.width ?? 10) / 2, y: sourceRect.y + sourceHandle.y + (sourceHandle.height ?? 10) / 2 }, target: { x: targetRect.x + targetHandle.x + (targetHandle.width ?? 10) / 2, y: targetRect.y + targetHandle.y + (targetHandle.height ?? 10) / 2 }, obstacles, endCards: moving ? [] : [source, target].filter((node) => !isAncestor(node, source) && !isAncestor(node, target)).map((node) => rectangles.get(node.id)!), backward: rowHint.backward ?? (targetRect.x <= sourceRect.x), failure: edge.data.failurePath, repeat: edge.data.rework, shapeGroupKey: `${source.id}:${target.id}:${edge.targetHandle}`, longJump: edge.data.routing?.longJump, sourceSide, targetSide, ...(rowHint.targetRowTop === undefined ? {} : { targetRowTop: rowHint.targetRowTop }) });
    routedIds.push(edge.id);
  }
  const routes = new Map<string, CanvasEdgeRoute>(routeCanvasEdges(inputs).map((route, index) => [routedIds[index], route]));
  // Every card, the connection's own ends included: a label must not hide under any of them.
  // Group and For each frames enclose their connections, so a label inside one is fine.
  const cards = positioned.filter((node) => node.type !== "canvasNote" && node.type !== "groupContainer" && node.type !== "forEachContainer").map((node) => rectangles.get(node.id)!);
  const sources = new Map<string, RoutedEdge>(); const routed = new Map<string, RoutedEdge>();
  const result = edges.map((edge) => {
    sources.set(edge.id, edge);
    if (!edge.data) return edge;
    const route = routes.get(edge.id);
    const earlier = previous && sameLayout && previous.sources.get(edge.id) === edge ? previous.routed.get(edge.id) : undefined;
    if (!route) {
      // Not routed this pass: a connection the drag did not touch keeps its drawn route, and one
      // that cannot be routed (an end without its handle) keeps its unrouted object.
      const kept = earlier && (moving || !(earlier.data as CanvasEdgeData | undefined)?.route) ? earlier : undefined;
      const next = kept ?? { ...edge, data: { ...edge.data, route: undefined, labelAt: undefined } };
      routed.set(edge.id, next);
      return next;
    }
    const labelAt = canvasEdgeLabelPoint(route, typeof edge.label === "string" ? edge.label : "", cards);
    const drawn = earlier?.data as CanvasEdgeData | undefined;
    // An unchanged route keeps its object, so the connection is not drawn again.
    const next = earlier && drawn?.route?.path === route.path && drawn.labelAt?.x === labelAt.x && drawn.labelAt?.y === labelAt.y
      ? earlier : { ...edge, data: { ...edge.data, route, labelAt } };
    routed.set(edge.id, next);
    return next;
  });
  return { layoutNodes: nodes, layoutEdges: edges, rootRows, geometry, settled: !moving, sources, routed, edges: result };
}

/** Holds the last pass for the next one. One per canvas view; a pass made for a dropped render is still a valid earlier pass. */
export class CanvasEdgeRouteCache {
  private memo: CanvasEdgeRouteMemo | null = null;

  route(input: Omit<RouteSetInput, "previous">): RoutedEdge[] {
    this.memo = routeCanvasEdgeSet({ ...input, previous: this.memo });
    return this.memo.edges;
  }
}
