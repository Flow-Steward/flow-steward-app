/** Where an outcome label ("True", "False", "Approved") sits on its connection.
 *  Labels draw in xyflow's edge-label layer, under the cards, so a label at the
 *  line's midpoint vanished whenever that midpoint ran along a card (a band over
 *  a step's top edge). The label keeps to its own line and moves from the middle
 *  towards the nearest stretch where it covers no card. */
import type { CanvasEdgeRoute, CanvasPoint, CanvasRect } from "./canvas-edge-routing";

/** `.workflow-edge-label`: 11px/16px text, 2px+6px padding, 1px border, 14px below the line. */
const LABEL_OFFSET_Y = 14;
const LABEL_HEIGHT = 22;
const LABEL_CHARACTER_WIDTH = 6.5;
const LABEL_FRAME_WIDTH = 14;
const CARD_CLEARANCE = 4;
const STEP = 8;
const CURVE_SAMPLES = 32;

export function edgeLabelRect(at: CanvasPoint, text: string): CanvasRect {
  const width = text.length * LABEL_CHARACTER_WIDTH + LABEL_FRAME_WIDTH;
  return { x: at.x - width / 2, y: at.y + LABEL_OFFSET_Y, width, height: LABEL_HEIGHT };
}

function overlaps(a: CanvasRect, b: CanvasRect): boolean {
  return a.x < b.x + b.width + CARD_CLEARANCE && a.x + a.width + CARD_CLEARANCE > b.x
    && a.y < b.y + b.height + CARD_CLEARANCE && a.y + a.height + CARD_CLEARANCE > b.y;
}

function cubicAt(p: readonly CanvasPoint[], t: number): CanvasPoint {
  const u = 1 - t;
  const a = u * u * u; const b = 3 * u * u * t; const c = 3 * u * t * t; const d = t * t * t;
  return { x: a * p[0].x + b * p[1].x + c * p[2].x + d * p[3].x, y: a * p[0].y + b * p[1].y + c * p[2].y + d * p[3].y };
}

/** The drawn line as a polyline: band routes carry their points; curves are `M L C L` or `M L`. */
function trace(route: CanvasEdgeRoute): CanvasPoint[] {
  if (route.points?.length) return route.points;
  const numbers = (route.path.match(/-?\d+(?:\.\d+)?/g) || []).map(Number);
  const pairs: CanvasPoint[] = [];
  for (let i = 0; i + 1 < numbers.length; i += 2) pairs.push({ x: numbers[i], y: numbers[i + 1] });
  if (pairs.length !== 6) return pairs;
  const [source, start, c1, c2, end, last] = pairs;
  const curve = Array.from({ length: CURVE_SAMPLES + 1 }, (_, i) => cubicAt([start, c1, c2, end], i / CURVE_SAMPLES));
  return [source, ...curve, last];
}

function pointAt(points: readonly CanvasPoint[], lengths: readonly number[], distance: number): CanvasPoint {
  let left = distance;
  for (let i = 0; i < lengths.length; i++) {
    if (left <= lengths[i]) {
      const ratio = lengths[i] ? left / lengths[i] : 0;
      return { x: points[i].x + (points[i + 1].x - points[i].x) * ratio, y: points[i].y + (points[i + 1].y - points[i].y) * ratio };
    }
    left -= lengths[i];
  }
  return points.at(-1)!;
}

/** The point nearest the route's midpoint whose label covers no card; the midpoint when none is clear. */
export function canvasEdgeLabelPoint(route: CanvasEdgeRoute, text: string, cards: readonly CanvasRect[]): CanvasPoint {
  const clear = (at: CanvasPoint) => !cards.some((card) => overlaps(edgeLabelRect(at, text), card));
  if (!text || clear(route.midpoint)) return route.midpoint;
  const points = trace(route);
  if (points.length < 2) return route.midpoint;
  const lengths = points.slice(1).map((p, i) => Math.hypot(p.x - points[i].x, p.y - points[i].y));
  const middle = lengths.reduce((sum, n) => sum + n, 0) / 2;
  for (let offset = STEP; offset <= middle; offset += STEP) {
    for (const distance of [middle - offset, middle + offset]) {
      const at = pointAt(points, lengths, distance);
      if (clear(at)) return at;
    }
  }
  return route.midpoint;
}
