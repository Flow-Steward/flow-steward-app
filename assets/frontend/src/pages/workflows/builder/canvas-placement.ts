import type { Node } from "@xyflow/react";
import { nodeSize, type CanvasPosition } from "./canvas-core";
import { CANVAS_GRID } from "./layout-constants";

export type CanvasPlacementRect = CanvasPosition & { width: number; height: number };
export type CanvasPlacementBounds = { minX?: number; minY?: number; maxX?: number; maxY?: number };

/**
 * Exact nearest free grid point for axis-aligned rectangles, with touching allowed.
 * A minimum can occur only at the requested coordinate or an obstacle/bounds edge
 * on each axis. Enumerating those candidates avoids an unbounded grid-ring walk.
 */
export function nearestFreeCanvasPosition({ requested, size, obstacles, bounds = {} }: {
  requested: CanvasPosition;
  size: { width: number; height: number };
  obstacles: readonly CanvasPlacementRect[];
  bounds?: CanvasPlacementBounds;
}): CanvasPosition | null {
  const floor = (n: number) => Math.floor(n / CANVAS_GRID) * CANVAS_GRID;
  const ceil = (n: number) => Math.ceil(n / CANVAS_GRID) * CANVAS_GRID;
  const origin = { x: Math.round(requested.x / CANVAS_GRID) * CANVAS_GRID, y: Math.round(requested.y / CANVAS_GRID) * CANVAS_GRID };
  const xs = new Set([origin.x]); const ys = new Set([origin.y]);
  for (const box of obstacles) {
    xs.add(floor(box.x - size.width)); xs.add(ceil(box.x + box.width));
    ys.add(floor(box.y - size.height)); ys.add(ceil(box.y + box.height));
  }
  if (bounds.minX !== undefined) xs.add(ceil(bounds.minX));
  if (bounds.maxX !== undefined) xs.add(floor(bounds.maxX));
  if (bounds.minY !== undefined) ys.add(ceil(bounds.minY));
  if (bounds.maxY !== undefined) ys.add(floor(bounds.maxY));
  const key = (p: CanvasPosition) => { const dx = p.x - origin.x; const dy = p.y - origin.y; return [dx * dx + dy * dy, Math.abs(dx), dy < 0 ? 1 : 0, dx < 0 ? 1 : 0, p.y, p.x]; };
  const less = (a: number[], b: number[]) => {
    for (let i = 0; i < a.length; i += 1) {
      if (a[i] !== b[i]) return a[i] < b[i];
    }
    return false;
  };
  let best: CanvasPosition | null = null;
  for (const x of xs) for (const y of ys) {
    if ((bounds.minX !== undefined && x < bounds.minX) || (bounds.maxX !== undefined && x > bounds.maxX) || (bounds.minY !== undefined && y < bounds.minY) || (bounds.maxY !== undefined && y > bounds.maxY)) continue;
    const candidate = { x, y };
    if (best && !less(key(candidate), key(best))) continue;
    if (obstacles.some((b) => x < b.x + b.width && x + size.width > b.x && y < b.y + b.height && y + size.height > b.y)) continue;
    if (x === origin.x && y === origin.y) return candidate;
    best = candidate;
  }
  return best;
}

export function canvasNodeLane(node: Pick<Node, "data">): string {
  const member = (node.data?.step ?? node.data?.group ?? node.data?.forEach) as { lane_id?: string } | undefined;
  return member?.lane_id ?? "";
}

/** Same parent and lane only; notes stay outside graph placement. Parent growth is retained. */
export function placeCanvasNode(node: Node, requested: CanvasPosition, nodes: readonly Node[], bounds: CanvasPlacementBounds = {}): CanvasPosition {
  const obstacles = nodes.filter((other) => other.id !== node.id && other.type !== "canvasNote" && other.parentId === node.parentId && canvasNodeLane(other) === canvasNodeLane(node)).map((other) => ({ ...other.position, ...nodeSize(other) }));
  return nearestFreeCanvasPosition({ requested, size: nodeSize(node), obstacles, bounds }) ?? requested;
}
