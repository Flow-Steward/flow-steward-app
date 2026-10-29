import { NOTE_DEFAULT_SIZE, type CanvasNoteDropPoint } from "./canvas-notes";
import { canvasPastePane } from "./canvas-viewport";
import { CANVAS_GRID } from "./layout-constants";

type FlowPoint = { x: number; y: number };

/** Find the closest grid-aligned note position inside the visible pane that clears rendered nodes. */
export function findCanvasNoteDropPoint(
  canvasRoot: HTMLElement | null,
  inspectorWidth: number,
  zoom: number,
  screenToFlow: (point: FlowPoint) => FlowPoint,
  flowToScreen: (point: FlowPoint) => FlowPoint,
): CanvasNoteDropPoint | undefined {
  if (!canvasRoot || !Number.isFinite(zoom) || zoom <= 0) return undefined;
  const pane = canvasPastePane(canvasRoot, inspectorWidth);
  if (!pane) return undefined;

  const screenCenter = { x: (pane.left + pane.right) / 2, y: (pane.top + pane.bottom) / 2 };
  const flowCenter = screenToFlow(screenCenter);
  if (!Number.isFinite(flowCenter.x) || !Number.isFinite(flowCenter.y)) return undefined;
  const grid = CANVAS_GRID;
  const startX = Math.round((flowCenter.x - NOTE_DEFAULT_SIZE.width / 2) / grid) * grid;
  const startY = Math.round((flowCenter.y - NOTE_DEFAULT_SIZE.height / 2) / grid) * grid;
  const occupied = [...canvasRoot.querySelectorAll<HTMLElement>(".react-flow__node")]
    .map((node) => node.getBoundingClientRect())
    .filter((rect) => rect.width > 0 && rect.height > 0 && [rect.left, rect.top, rect.right, rect.bottom].every(Number.isFinite));
  const maxRadius = Math.ceil(
    (pane.right - pane.left + pane.bottom - pane.top) / (grid * zoom * 2),
  );

  for (let radius = 0; radius <= maxRadius; radius += 1) {
    for (let dx = -radius; dx <= radius; dx += 1) {
      const dy = radius - Math.abs(dx);
      const offsets = dy === 0 ? [0] : [-dy, dy];
      for (const offsetY of offsets) {
        const topLeft = { x: startX + dx * grid, y: startY + offsetY * grid };
        const center = flowToScreen({
          x: topLeft.x + NOTE_DEFAULT_SIZE.width / 2,
          y: topLeft.y + NOTE_DEFAULT_SIZE.height / 2,
        });
        if (!Number.isFinite(center.x) || !Number.isFinite(center.y)) continue;
        const halfWidth = NOTE_DEFAULT_SIZE.width * zoom / 2;
        const halfHeight = NOTE_DEFAULT_SIZE.height * zoom / 2;
        const rect = {
          left: center.x - halfWidth - 8,
          right: center.x + halfWidth + 8,
          top: center.y - halfHeight - 8,
          bottom: center.y + halfHeight + 8,
        };
        if (rect.left < pane.left || rect.right > pane.right || rect.top < pane.top || rect.bottom > pane.bottom) continue;
        if (occupied.some((node) => rect.left < node.right && rect.right > node.left && rect.top < node.bottom && rect.bottom > node.top)) continue;
        return { clientX: center.x, clientY: center.y };
      }
    }
  }
  return undefined;
}
