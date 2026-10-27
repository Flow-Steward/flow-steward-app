/** Page-local drawing geometry. No graph changes or transport belong here. */
export type CanvasPoint = { x: number; y: number };
export type CanvasRect = CanvasPoint & { width: number; height: number };
type Shape = readonly [number, number];
export const CANVAS_EDGE_SHAPES: readonly Shape[] = [[0.5, 0.5], [0.25, 0.5], [0.1, 0.5], [0.5, 0.25], [0.5, 0.1], [0.1, 0.1]];
const LEAD = 8;
/** The input's 10px circle: a line ends on its rim, never under an arrowhead (Connections › Kinds). */
const INPUT_RIM = 5;
const CLEARANCE = 4;
/** A path round a node keeps 12px clear of it (Canvas › Routing); lanes sit on that line. */
const DETOUR_CLEARANCE = 12;
const RADIUS = 8;
/** A turn costs more than the gap between two columns, so the detour with the fewest turns wins
 * over any comparable length; a bounded cost keeps the search focused on the way between the ends. */
const TURN_COST = 160;
/** The short way through the gap wins when the row's band is this much longer. */
const BAND_DETOUR_RATIO = 1.25;
export type CanvasEdgeRouteInput = {
  source: CanvasPoint;
  target: CanvasPoint;
  obstacles: readonly CanvasRect[];
  backward: boolean;
  failure: boolean;
  repeat: boolean;
  shapeGroupKey?: string;
  /** Produced from same-scope logical columns, independent of manual positions. */
  longJump?: boolean;
  sourceSide?: "right" | "bottom";
  targetSide?: "left" | "bottom";
  /** End cards constrain band routes, but are not foreign curve obstacles. */
  endCards?: readonly CanvasRect[];
  /** A layout owner can explicitly request the gap above a wrapped target row. */
  targetRowTop?: number;
};
export type CanvasEdgeRoute = {
  path: string;
  midpoint: CanvasPoint;
  bounds: CanvasRect;
  kind: "curve" | "upper-band" | "lower-band";
  shape?: Shape;
  points?: CanvasPoint[];
};
const pointText = (point: CanvasPoint) => `${point.x},${point.y}`;
const samePoint = (a: CanvasPoint, b: CanvasPoint) => a.x === b.x && a.y === b.y;
function curvePoints(source: CanvasPoint, target: CanvasPoint, f1: number, f2: number) {
  const start = { x: source.x + LEAD, y: source.y };
  const end = { x: target.x - INPUT_RIM - LEAD, y: target.y };
  const run = end.x - start.x;
  const pull = (f: number) => Math.max(f === 0.5 ? 24 : 12, run * f);
  return [start, { x: start.x + pull(f1), y: source.y }, { x: end.x - pull(f2), y: target.y }, end] as const;
}
/** The line ends on the rim of the input's 10px circle; no arrowhead. */
export function curveEdgePath(source: CanvasPoint, target: CanvasPoint, f1 = 0.5, f2 = 0.5): string {
  const last = { x: target.x - INPUT_RIM, y: target.y };
  if (Math.abs(source.y - target.y) < 2) return `M ${pointText(source)} L ${pointText(last)}`;
  const [start, c1, c2, end] = curvePoints(source, target, f1, f2);
  return `M ${pointText(source)} L ${pointText(start)} C ${pointText(c1)} ${pointText(c2)} ${pointText(end)} L ${pointText(last)}`;
}
function boundsOf(points: readonly CanvasPoint[]): CanvasRect {
  const xs = points.map((p) => p.x); const ys = points.map((p) => p.y);
  const x = Math.min(...xs); const y = Math.min(...ys);
  return { x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y };
}
function expanded(rect: CanvasRect, clearance = CLEARANCE): CanvasRect {
  return { x: rect.x - clearance, y: rect.y - clearance, width: rect.width + 2 * clearance, height: rect.height + 2 * clearance };
}
function overlaps(a: CanvasRect, b: CanvasRect) {
  return a.x <= b.x + b.width && a.x + a.width >= b.x && a.y <= b.y + b.height && a.y + a.height >= b.y;
}
const halfway = (a: CanvasPoint, b: CanvasPoint): CanvasPoint => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
/** Recursive control hulls bound the whole curve, including narrow obstacles between samples. */
function cubicHits(points: readonly CanvasPoint[], rect: CanvasRect, depth = 0): boolean {
  if (!overlaps(boundsOf(points), rect)) return false;
  if (depth === 12) return true;
  const [a, b, c, d] = points; const ab = halfway(a, b); const bc = halfway(b, c); const cd = halfway(c, d);
  const abc = halfway(ab, bc); const bcd = halfway(bc, cd); const middle = halfway(abc, bcd);
  return cubicHits([a, ab, abc, middle], rect, depth + 1) || cubicHits([middle, bcd, cd, d], rect, depth + 1);
}
function lineHits(a: CanvasPoint, b: CanvasPoint, rect: CanvasRect) {
  if (!overlaps(boundsOf([a, b]), rect)) return false;
  if (a.x === b.x || a.y === b.y) return true;
  let low = 0; let high = 1;
  for (const [start, delta, min, max] of [[a.x, b.x - a.x, rect.x, rect.x + rect.width], [a.y, b.y - a.y, rect.y, rect.y + rect.height]]) {
    if (delta === 0) {
      if (start < min || start > max) return false;
      continue;
    }
    const t1 = (min - start) / delta; const t2 = (max - start) / delta;
    low = Math.max(low, Math.min(t1, t2)); high = Math.min(high, Math.max(t1, t2));
  }
  return low <= high;
}
function curveClear(input: CanvasEdgeRouteInput, shape: Shape) {
  const points = curvePoints(input.source, input.target, ...shape);
  return input.obstacles.every((obstacle) => {
    const rect = expanded(obstacle);
    if (Math.abs(input.source.y - input.target.y) < 2) return !lineHits(input.source, { x: input.target.x - INPUT_RIM, y: input.target.y }, rect);
    return !lineHits(input.source, points[0], rect) && !cubicHits(points, rect) && !lineHits(points[3], { x: input.target.x - INPUT_RIM, y: input.target.y }, rect);
  });
}
/** Deduplicate and remove straight-through vertices before rounding. */
export function compactRoutePoints(points: readonly CanvasPoint[]): CanvasPoint[] {
  const result: CanvasPoint[] = [];
  for (const point of points) {
    if (result.length && samePoint(result.at(-1)!, point)) continue;
    const a = result.at(-2); const b = result.at(-1)!;
    if (a && b && ((a.x === b.x && b.x === point.x) || (a.y === b.y && b.y === point.y)) && (b.x - a.x) * (point.x - b.x) + (b.y - a.y) * (point.y - b.y) > 0) result.pop();
    result.push(point);
  }
  return result;
}
function squarePath(points: readonly CanvasPoint[]) {
  return points.map((point, index) => `${index ? "L" : "M"} ${pointText(point)}`).join(" ");
}
function polylineLength(points: readonly CanvasPoint[]) {
  return points.slice(1).reduce((sum, point, index) => sum + Math.abs(point.x - points[index].x) + Math.abs(point.y - points[index].y), 0);
}
function roundedPath(points: readonly CanvasPoint[]) {
  let path = `M ${pointText(points[0])}`;
  for (let i = 1; i < points.length - 1; i++) {
    const prev = points[i - 1]; const here = points[i]; const next = points[i + 1];
    const beforeLength = Math.hypot(here.x - prev.x, here.y - prev.y); const afterLength = Math.hypot(next.x - here.x, next.y - here.y);
    const radius = Math.min(RADIUS, beforeLength / 2, afterLength / 2);
    const before = { x: here.x + (prev.x - here.x) * radius / beforeLength, y: here.y + (prev.y - here.y) * radius / beforeLength };
    const after = { x: here.x + (next.x - here.x) * radius / afterLength, y: here.y + (next.y - here.y) * radius / afterLength };
    path += ` L ${pointText(before)} Q ${pointText(here)} ${pointText(after)}`;
  }
  return path + ` L ${pointText(points.at(-1)!)}`;
}
function polylineMidpoint(points: readonly CanvasPoint[]): CanvasPoint {
  const lengths = points.slice(1).map((p, i) => Math.hypot(p.x - points[i].x, p.y - points[i].y));
  let left = lengths.reduce((sum, n) => sum + n, 0) / 2;
  for (let i = 0; i < lengths.length; i++) {
    if (left <= lengths[i]) { const ratio = lengths[i] ? left / lengths[i] : 0; return { x: points[i].x + (points[i + 1].x - points[i].x) * ratio, y: points[i].y + (points[i + 1].y - points[i].y) * ratio }; }
    left -= lengths[i];
  }
  return points[0];
}
type DetourSearch = {
  start: CanvasPoint;
  end: CanvasPoint;
  obstacles: readonly CanvasRect[];
  endCards: readonly CanvasRect[];
  /** How the path comes into the input: from the left, or up into a bottom port. */
  approach: "left" | "bottom";
  /** The band line the path must reach, on one side of its ends; absent for the short way through the gap. */
  band?: { y: number; below: boolean };
};
type Visit = { xi: number; yi: number; band: boolean; dir: number; lead: boolean; g: number; h: number; key: string };
function createVisitQueue() {
  const open: Visit[] = [];
  const precedes = (a: Visit, b: Visit) => a.g + a.h < b.g + b.h || (a.g + a.h === b.g + b.h && (a.h < b.h || (a.h === b.h && a.key < b.key)));
  return {
    get size() { return open.length; },
    push(visit: Visit) {
      open.push(visit);
      let i = open.length - 1;
      while (i > 0) {
        const parent = Math.floor((i - 1) / 2);
        if (!precedes(open[i], open[parent])) break;
        [open[i], open[parent]] = [open[parent], open[i]];
        i = parent;
      }
    },
    pop() {
      const result = open[0];
      const lastVisit = open.pop()!;
      if (open.length) {
        open[0] = lastVisit;
        let i = 0;
        while (true) {
          const left = i * 2 + 1;
          const right = left + 1;
          let next = i;
          if (left < open.length && precedes(open[left], open[next])) next = left;
          if (right < open.length && precedes(open[right], open[next])) next = right;
          if (next === i) break;
          [open[i], open[next]] = [open[next], open[i]];
          i = next;
        }
      }
      return result;
    },
  };
}
/** Square-cornered detour on the lazily visited Hanan grid: fewest turns first, then the shortest. */
function searchDetour({ start, end, obstacles, endCards, approach, band }: DetourSearch): CanvasPoint[] | null {
  // The straight leads out of and into the ports may pass close to what sits beside them; the rest keeps 12px clear.
  const near = [...obstacles, ...endCards].map((rect) => expanded(rect));
  const far = [...obstacles.map((rect) => expanded(rect, DETOUR_CLEARANCE - 0.5)), ...endCards.map((rect) => expanded(rect))];
  const segmentClear = (a: CanvasPoint, b: CanvasPoint, lead: boolean) => (lead ? near : far).every((rect) => !lineHits(a, b, rect));
  const intoEnd = (a: CanvasPoint, b: CanvasPoint) => approach === "left"
    ? a.y === end.y && b.y === end.y && b.x > a.x && b.x <= end.x
    : a.x === end.x && b.x === end.x && b.y < a.y && b.y >= end.y;
  const lanes = (rect: CanvasRect, clearance: number) => [rect.x - clearance, rect.x + rect.width + clearance];
  const rows = (rect: CanvasRect, clearance: number) => [rect.y - clearance, rect.y + rect.height + clearance];
  const xs = [...new Set([start.x, end.x, ...obstacles.flatMap((r) => lanes(r, DETOUR_CLEARANCE)), ...endCards.flatMap((r) => lanes(r, DETOUR_CLEARANCE))])].sort((a, b) => a - b);
  const ys = [...new Set([start.y, end.y, ...(band ? [band.y] : []), ...obstacles.flatMap((r) => rows(r, DETOUR_CLEARANCE)), ...endCards.flatMap((r) => rows(r, DETOUR_CLEARANCE))])].sort((a, b) => a - b);
  const pointOf = (visit: Visit) => ({ x: xs[visit.xi], y: ys[visit.yi] });
  const heuristic = (point: CanvasPoint, reached: boolean) => Math.abs(point.x - end.x) +
    (!band || reached ? Math.abs(point.y - end.y) : Math.abs(point.y - band.y) + Math.abs(end.y - band.y));
  const queue = createVisitQueue();
  const distances = new Map<string, number>(); const previous = new Map<string, string>(); const visits = new Map<string, Visit>();
  const first: Visit = { xi: xs.indexOf(start.x), yi: ys.indexOf(start.y), band: !band, dir: -1, lead: true, g: 0, h: heuristic(start, !band), key: "" };
  first.key = `${first.xi}:${first.yi}:${first.band ? 1 : 0}:-1:1`; distances.set(first.key, 0); visits.set(first.key, first); queue.push(first);
  const steps = [[-1, 0], [1, 0], [0, -1], [0, 1]] as const;
  while (queue.size) {
    const current = queue.pop(); if (current.g !== distances.get(current.key)) continue;
    const point = pointOf(current);
    if (current.band && samePoint(point, end)) {
      const walked = [point]; let key = current.key;
      while (previous.has(key)) { key = previous.get(key)!; walked.unshift(pointOf(visits.get(key)!)); }
      return walked;
    }
    steps.forEach(([dx, dy], dir) => {
      const xi = current.xi + dx; const yi = current.yi + dy;
      if (xi < 0 || yi < 0 || xi >= xs.length || yi >= ys.length) return;
      const nextPoint = { x: xs[xi], y: ys[yi] };
      if (band && (band.below ? nextPoint.y < Math.min(start.y, end.y) : nextPoint.y > Math.max(start.y, end.y))) return;
      const lead = current.lead && (current.dir < 0 || current.dir === dir);
      if (!segmentClear(point, nextPoint, lead || intoEnd(point, nextPoint))) return;
      const reached = current.band || Boolean(band && (band.below ? nextPoint.y >= band.y : nextPoint.y <= band.y));
      const key = `${xi}:${yi}:${reached ? 1 : 0}:${dir}:${lead ? 1 : 0}`;
      const turn = current.dir >= 0 && current.dir !== dir ? TURN_COST : 0;
      const g = current.g + turn + Math.abs(point.x - nextPoint.x) + Math.abs(point.y - nextPoint.y);
      if (g >= (distances.get(key) ?? Infinity)) return;
      const next = { xi, yi, band: reached, dir, lead, g, h: heuristic(nextPoint, reached), key };
      distances.set(key, g); previous.set(key, current.key); visits.set(key, next); queue.push(next);
    });
  }
  return null;
}
/** Nodes far from both ends never shape the short way between them. */
const GAP_WINDOW = 240;
function windowAround(a: CanvasPoint, b: CanvasPoint): CanvasRect {
  const x = Math.min(a.x, b.x) - GAP_WINDOW; const y = Math.min(a.y, b.y) - GAP_WINDOW;
  return { x, y, width: Math.abs(a.x - b.x) + 2 * GAP_WINDOW, height: Math.abs(a.y - b.y) + 2 * GAP_WINDOW };
}
function bandRoute(input: CanvasEdgeRouteInput): CanvasEdgeRoute {
  const { source, target } = input;
  const start = input.sourceSide === "bottom" ? { x: source.x, y: source.y + LEAD } : { x: source.x + LEAD, y: source.y };
  const end = input.targetSide === "bottom" ? { x: target.x, y: target.y + INPUT_RIM + LEAD } : { x: target.x - INPUT_RIM - LEAD, y: target.y };
  const last = input.targetSide === "bottom" ? { x: target.x, y: target.y + INPUT_RIM } : { x: target.x - INPUT_RIM, y: target.y };
  const endCards = input.endCards || [];
  const obstacles = [...input.obstacles, ...endCards];
  const corridorLeft = Math.min(start.x, end.x) - 32; const corridorRight = Math.max(start.x, end.x) + 32;
  const bandObstacles = obstacles.filter((rect) => rect.x <= corridorRight && rect.x + rect.width >= corridorLeft);
  const minY = Math.min(source.y, target.y, ...bandObstacles.map((r) => r.y));
  const maxY = Math.max(source.y, target.y, ...bandObstacles.map((r) => r.y + r.height));
  const below = input.backward || input.failure || input.repeat || input.sourceSide === "bottom" || input.targetSide === "bottom";
  const bandY = below ? maxY + 24 : input.targetRowTop === undefined ? minY - 24 : input.targetRowTop - 24;
  const clear = (points: readonly CanvasPoint[]) => points.slice(1).every((p, i) => obstacles.every((r) => !lineHits(points[i], p, expanded(r))));
  const direct = [start, { x: start.x, y: bandY }, { x: end.x, y: bandY }, end];
  const finish = (middle: readonly CanvasPoint[], square: boolean): CanvasEdgeRoute => {
    const points = compactRoutePoints([source, ...middle, last]);
    return { path: square ? squarePath(points) : roundedPath(points), midpoint: polylineMidpoint(points), bounds: boundsOf(points), points, kind: below ? "lower-band" : "upper-band" };
  };
  const directClear = clear(direct);
  // A path over the top or under the cards keeps its rounded band; round a node in the way it turns square.
  const approach = input.targetSide === "bottom" ? "bottom" : "left";
  const banded = directClear ? direct : searchDetour({ start, end, obstacles: input.obstacles, endCards, approach, band: { y: bandY, below } });
  // The short way through the gap, when the band is much longer (a step dropped under its source).
  // No gap path is shorter than the straight distance, so a band within the ratio of it keeps its place.
  const reach = Math.abs(start.x - end.x) + Math.abs(start.y - end.y);
  const requested = input.longJump || input.targetRowTop !== undefined;
  const gap = requested || (banded && polylineLength(banded) <= BAND_DETOUR_RATIO * reach) ? null
    : searchDetour({ start, end, obstacles: input.obstacles.filter((rect) => overlaps(rect, windowAround(start, end))), endCards, approach });
  if (gap && (!banded || polylineLength(banded) > BAND_DETOUR_RATIO * polylineLength(gap))) return finish(gap, true);
  if (banded) return finish(banded, !directClear);
  // Overlapping end cards can box in the handle itself; its placement owner
  // must resolve that overlap. Never silently turn an error/repeat into an upper route.
  return finish(direct, false);
}
function curveRoute(input: CanvasEdgeRouteInput, shape: Shape): CanvasEdgeRoute {
  const controls = curvePoints(input.source, input.target, ...shape);
  const midpoint = Math.abs(input.source.y - input.target.y) < 2 ? { x: (input.source.x + input.target.x - INPUT_RIM) / 2, y: input.target.y } : { x: (controls[0].x + 3 * controls[1].x + 3 * controls[2].x + controls[3].x) / 8, y: (input.source.y + input.target.y) / 2 };
  return { path: curveEdgePath(input.source, input.target, ...shape), midpoint, bounds: boundsOf([input.source, ...controls, input.target]), shape, kind: "curve" };
}
const bandRequired = (input: CanvasEdgeRouteInput) => input.backward || input.failure || input.repeat || input.sourceSide === "bottom" || input.targetSide === "bottom" || input.targetRowTop !== undefined || input.longJump || input.target.x - input.source.x < 40;
/** A group selects one shape only if it clears every sibling outcome. */
export function routeCanvasEdges(inputs: readonly CanvasEdgeRouteInput[]): CanvasEdgeRoute[] {
  const groups = new Map<string, CanvasEdgeRouteInput[]>();
  inputs.forEach((input, i) => { const key = input.shapeGroupKey || `single:${i}`; const list = groups.get(key) || []; list.push(input); groups.set(key, list); });
  return inputs.map((input, i) => {
    if (bandRequired(input)) return bandRoute(input);
    const peers = groups.get(input.shapeGroupKey || `single:${i}`)!.filter((peer) => !bandRequired(peer));
    const shape = CANVAS_EDGE_SHAPES.find((candidate) => peers.every((peer) => !bandRequired(peer) && curveClear(peer, candidate)));
    return shape ? curveRoute(input, shape) : bandRoute(input);
  });
}
export function routeCanvasEdge(input: CanvasEdgeRouteInput): CanvasEdgeRoute { return routeCanvasEdges([input])[0]; }
