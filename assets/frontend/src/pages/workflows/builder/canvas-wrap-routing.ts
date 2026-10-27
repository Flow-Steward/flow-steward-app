import type { Node } from "@xyflow/react";
import type { CanvasEdgeRouteInput, CanvasRect } from "./canvas-edge-routing";
import type { CanvasRootRows } from "./canvas-root-wrap";

type Endpoint = { node: Node; rect: CanvasRect };
type RouteContext = {
  source: Endpoint;
  target: Endpoint;
  sameScope: boolean;
  failure: boolean;
  repeat: boolean;
  sourceSide?: Exclude<CanvasEdgeRouteInput["sourceSide"], undefined>;
  targetSide?: Exclude<CanvasEdgeRouteInput["targetSide"], undefined>;
};
type RowHint = Partial<Pick<CanvasEdgeRouteInput, "backward" | "targetRowTop">>;

const rectOfAutomaticRoot = (node: Node): CanvasRect => ({
  x: node.position.x,
  y: node.position.y,
  width: Number(node.width || node.style?.width || 0),
  height: Number(node.height || node.style?.height || 0),
});
const validRect = (rect: CanvasRect) =>
  [rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) && rect.width > 0 && rect.height > 0;
const sameRect = (left: CanvasRect, right: CanvasRect) =>
  left.x === right.x && left.y === right.y && left.width === right.width && left.height === right.height;

/**
 * Construct once per routed-edge pass. Automatic rectangles come from the Wrap
 * layout; displayed rectangles are supplied by the canvas after manual overlay
 * and descendant reconciliation. A root's missing manual key proves nothing.
 */
export function createWrappedRootRouteHints(rootRows: CanvasRootRows | undefined, automaticNodes: readonly Node[]) {
  const automaticRoots = new Map(automaticNodes.filter((node) => !node.parentId).map((node) => [node.id, rectOfAutomaticRoot(node)]));
  return ({ source, target, sameScope, failure, repeat, sourceSide, targetSide }: RouteContext): RowHint => {
    if (!rootRows || !sameScope || source.node.parentId || target.node.parentId ||
      failure || repeat || sourceSide === "bottom" || targetSide === "bottom") return {};
    const sourceRow = rootRows.rowIndexByRootId.get(source.node.id);
    const targetRow = rootRows.rowIndexByRootId.get(target.node.id);
    if (sourceRow === undefined || targetRow === undefined || sourceRow === targetRow ||
      !Number.isInteger(sourceRow) || !Number.isInteger(targetRow) ||
      sourceRow < 0 || targetRow < 0 || sourceRow >= rootRows.rows.length || targetRow >= rootRows.rows.length) return {};
    const sourceAutomatic = automaticRoots.get(source.node.id);
    const targetAutomatic = automaticRoots.get(target.node.id);
    if (!sourceAutomatic || !targetAutomatic ||
      !validRect(sourceAutomatic) || !validRect(targetAutomatic) ||
      !validRect(source.rect) || !validRect(target.rect) ||
      !sameRect(sourceAutomatic, source.rect) || !sameRect(targetAutomatic, target.rect)) return {};
    if (sourceRow > targetRow) return { backward: true };
    const targetRowTop = rootRows.rows[targetRow]?.top;
    return targetRowTop !== undefined && Number.isFinite(targetRowTop)
      ? { backward: false, targetRowTop }
      : {};
  };
}
