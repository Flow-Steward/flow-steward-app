import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import type { Node, ReactFlowInstance } from "@xyflow/react";
import type { I18nContextValue } from "@/lib/i18n/context";
import { WorkflowStepIcon } from "@/pages/workflows/shared/workflow-step-icon";
import { ACTION_STEP_WIDTH, CANVAS_GRID } from "./layout-constants";
import type { LaneFrame } from "./layout";
import type { PaletteEntry } from "./canvas-palette-catalog";
import "./canvas-menus.css";

type Point = { x: number; y: number };
/** Where a dragged kind lands: the flow point, and the group and lane under the pointer. */
export type PaletteDropTarget = { point: Point; clientPoint: Point; groupId: string; forEachId: string; laneId: string; label: string };

const SLOT_HEIGHT = 96;

function absolutePosition(instance: ReactFlowInstance, node: Node): Point {
  const internal = instance.getInternalNode(node.id);
  return internal?.internals.positionAbsolute ?? node.position;
}

/** The group frame and lane under a flow point; a group kind never joins one (groups don't nest). */
export function paletteDropScope(instance: ReactFlowInstance, point: Point, container: boolean): Pick<PaletteDropTarget, "groupId" | "forEachId" | "laneId"> & { name: string } {
  const empty = { groupId: "", forEachId: "", laneId: "", name: "" };
  if (container) return empty;
  const frames = instance.getNodes().filter((node) => !node.hidden && (node.type === "groupContainer" || node.type === "forEachContainer"));
  let best: { node: Node; origin: Point; depth: number } | null = null;
  for (const node of frames) {
    const origin = absolutePosition(instance, node);
    const width = Number(node.width ?? node.measured?.width ?? 0); const height = Number(node.height ?? node.measured?.height ?? 0);
    if (point.x < origin.x || point.y < origin.y || point.x > origin.x + width || point.y > origin.y + height) continue;
    let depth = 0; let parent = node.parentId;
    while (parent) { depth += 1; parent = instance.getNode(parent)?.parentId; }
    if (!best || depth > best.depth) best = { node, origin, depth };
  }
  if (!best) return empty;
  const data = best.node.data as { group?: { id: string; display_name: string }; forEach?: { id: string; display_name: string }; lanes?: string[]; laneFrames?: LaneFrame[] };
  const local = { x: point.x - best.origin.x, y: point.y - best.origin.y };
  const lanes = data.lanes ?? [];
  const frame = data.laneFrames?.find((lane) => local.y >= lane.y && local.y <= lane.y + lane.height) ?? data.laneFrames?.find((lane) => local.y < lane.y);
  const laneId = lanes.length ? frame?.id || lanes.at(-1) || "" : "";
  if (best.node.type === "forEachContainer" && data.forEach) return { groupId: "", forEachId: data.forEach.id, laneId, name: data.forEach.display_name };
  if (data.group) return { groupId: data.group.id, forEachId: "", laneId, name: data.group.display_name };
  return empty;
}

/**
 * Drag a kind out of Add step onto the canvas: the palette fades, a dashed slot
 * shows where it lands and a chip names it and where it goes; released over the
 * canvas it is added there, released elsewhere or Escape adds nothing. A plain
 * click still adds the kind where the next step goes. Touch keeps the list scrolling.
 */
export function usePaletteDrag({ instance, canvasRoot, t, onDrop }: Readonly<{
  instance: ReactFlowInstance | null;
  canvasRoot: () => Element | null;
  t: I18nContextValue["t"];
  onDrop: (entry: PaletteEntry, target: PaletteDropTarget) => void;
}>) {
  const [drag, setDrag] = useState<{ entry: PaletteEntry; client: Point; target: PaletteDropTarget | null } | null>(null);
  const pending = useRef<{ entry: PaletteEntry; start: Point; pointerId: number } | null>(null);
  const suppressClick = useRef(false);
  const targetAt = useCallback((client: Point, entry: PaletteEntry): PaletteDropTarget | null => {
    const root = canvasRoot();
    if (!instance || !root || typeof document.elementFromPoint !== "function") return null;
    const hit = document.elementFromPoint(client.x, client.y);
    if (!hit || !root.contains(hit) || hit.closest(".react-flow__panel, .fs-popover, .fs-row-menu")) return null;
    const flow = instance.screenToFlowPosition(client);
    const point = { x: Math.round(flow.x / CANVAS_GRID) * CANVAS_GRID, y: Math.round(flow.y / CANVAS_GRID) * CANVAS_GRID };
    const container = entry.choice.nodeKind === "group" || entry.choice.nodeKind === "for_each";
    const scope = paletteDropScope(instance, flow, container || entry.choice.nodeKind === "note");
    const label = scope.name
      ? scope.laneId ? t("react.workflows.drag_joins_lane", "Joins {group} · {lane}", { group: scope.name, lane: scope.laneId }) : t("react.workflows.drag_joins", "Joins {group}", { group: scope.name })
      : t("react.workflows.drag_root", "In the workflow root");
    return { point, clientPoint: client, groupId: scope.groupId, forEachId: scope.forEachId, laneId: scope.laneId, label };
  }, [canvasRoot, instance, t]);
  const onDragStart = useCallback((entry: PaletteEntry, event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0 || event.pointerType === "touch") return;
    pending.current = { entry, start: { x: event.clientX, y: event.clientY }, pointerId: event.pointerId };
  }, []);
  useEffect(() => {
    const move = (event: PointerEvent) => {
      const press = pending.current;
      if (press?.pointerId !== event.pointerId) return;
      const client = { x: event.clientX, y: event.clientY };
      if (!drag && Math.hypot(client.x - press.start.x, client.y - press.start.y) <= 4) return;
      event.preventDefault();
      setDrag({ entry: press.entry, client, target: targetAt(client, press.entry) });
    };
    const up = (event: PointerEvent) => {
      const press = pending.current;
      if (press?.pointerId !== event.pointerId) return;
      pending.current = null;
      if (!drag) return;
      suppressClick.current = true;
      globalThis.setTimeout(() => { suppressClick.current = false; }, 0);
      const target = targetAt({ x: event.clientX, y: event.clientY }, press.entry);
      setDrag(null);
      if (target) onDrop(press.entry, target);
    };
    const key = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !pending.current) return;
      event.preventDefault(); event.stopPropagation();
      pending.current = null; setDrag(null);
    };
    globalThis.addEventListener("pointermove", move);
    globalThis.addEventListener("pointerup", up);
    globalThis.addEventListener("keydown", key, true);
    return () => { globalThis.removeEventListener("pointermove", move); globalThis.removeEventListener("pointerup", up); globalThis.removeEventListener("keydown", key, true); };
  }, [drag, onDrop, targetAt]);
  const zoom = instance?.getZoom() ?? 1;
  const slot = drag?.target && instance ? instance.flowToScreenPosition(drag.target.point) : null;
  const overlay = drag ? createPortal(<>
    {slot ? <div className="workflow-drag-slot" aria-hidden="true" style={{ left: slot.x, top: slot.y, width: ACTION_STEP_WIDTH * zoom, height: SLOT_HEIGHT * zoom }} /> : null}
    <div className="workflow-drag-chip" role="status" data-refused={drag.target ? undefined : "true"} style={{ left: drag.client.x + 12, top: drag.client.y + 12 }}>
      <WorkflowStepIcon kind={drag.entry.iconKind} toolCategory={drag.entry.toolCategory} />
      <span>{drag.entry.choice.displayName}</span>
      <span className="workflow-drag-chip-place">{drag.target ? drag.target.label : t("react.workflows.drag_off_canvas", "Release over the canvas to add it")}</span>
    </div>
  </>, document.body) : null;
  return { dragging: Boolean(drag), onDragStart, overlay, consumeClick: () => suppressClick.current };
}
