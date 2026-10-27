import { CANVAS_GRID, CANVAS_MARGIN_Y, COLUMN_GAP, START_WIDTH } from "./layout-constants";

type Point = { x: number; y: number };
type RootNode = { id: string; width: number; height: number };
type RootLink = { source: string; target: string; rework: boolean };
type RootColumn = { nodes: readonly RootNode[]; width: number; height: number };

export type CanvasRootRow = {
  rootIds: readonly string[];
  /** World coordinates before the existing manual/reconciliation overlay. */
  top: number;
  bottom: number;
  gapAfter: number;
};

export type CanvasRootRows = {
  rows: readonly CanvasRootRow[];
  rowIndexByRootId: ReadonlyMap<string, number>;
};

type WrappedRootLayout = {
  /** Same origin as gridPositions: later rows may have negative local X. */
  positions: Map<string, Point>;
  /** Actual union width of Start and the root nodes, without canvas padding. */
  width: number;
  rootRows: CanvasRootRows;
};

const WRAP_THRESHOLD = 5600;
const CAPACITY_STEP = 512;
const NOMINAL_WIDTH = 1440;
const NOMINAL_HEIGHT = 836;
const START_PREFIX = START_WIDTH + COLUMN_GAP;

/**
 * Pack existing root columns only. Ranking, membership, member-local Y and all
 * child geometry stay with the existing layout owners. Undefined means use the
 * original map and width verbatim, including a standalone overwide column.
 */
export function wrapRootColumns({
  nodes,
  positions,
  rank,
  links,
  startHeight,
  startY,
}: {
  nodes: readonly RootNode[];
  positions: ReadonlyMap<string, Point>;
  rank: ReadonlyMap<string, number>;
  links: readonly RootLink[];
  startHeight: number;
  startY: number;
}): WrappedRootLayout | undefined {
  if (!nodes.length || !Number.isFinite(startHeight) || startHeight <= 0 || !Number.isFinite(startY)) return undefined;
  const byRank = new Map<number, RootNode[]>();
  let left = Infinity;
  let right = -Infinity;
  const ids = new Set<string>();
  for (const node of nodes) {
    const point = positions.get(node.id);
    const column = rank.get(node.id);
    if (ids.has(node.id) || !point || column === undefined || !Number.isSafeInteger(column) ||
      !Number.isFinite(point.x) || !Number.isFinite(point.y) ||
      !Number.isFinite(node.width) || node.width <= 0 || !Number.isFinite(node.height) || node.height <= 0) return undefined;
    ids.add(node.id);
    left = Math.min(left, point.x);
    right = Math.max(right, point.x + node.width);
    const members = byRank.get(column);
    if (members) members.push(node);
    else byRank.set(column, [node]);
  }
  const realWidth = right - left;
  if (!Number.isFinite(realWidth) || realWidth <= WRAP_THRESHOLD || byRank.size < 2) return undefined;
  const columns: RootColumn[] = [...byRank.keys()].sort((a, b) => a - b).map((column) => {
    const members = byRank.get(column)!;
    let width = 0;
    let height = 0;
    for (const node of members) {
      width = Math.max(width, node.width);
      height = Math.max(height, positions.get(node.id)!.y + node.height);
    }
    return { nodes: members, width, height };
  });
  // Numeric representability is a guard, not a product node-count cap. Never
  // enter a loop whose bucket increment can stop advancing in JS numbers.
  const candidateCount = Math.ceil((START_PREFIX + realWidth) / CAPACITY_STEP);
  if (!Number.isSafeInteger(candidateCount) || candidateCount < 1) return undefined;
  const widthBefore = [0];
  const columnById = new Map<string, number>();
  columns.forEach((column, index) => {
    widthBefore.push(widthBefore[index] + column.width);
    column.nodes.forEach((node) => columnById.set(node.id, index));
  });
  if (!widthBefore.every(Number.isFinite)) return undefined;
  const cutCosts = new Array<number>(columns.length).fill(0);
  const cutLanes = new Array<number>(columns.length).fill(0);
  // One difference/prefix sweep includes actual forward and backward domain
  // edges. Rank-only adjacency omits Repeat/back edges and is not an authority.
  for (const link of links) {
    const source = columnById.get(link.source);
    const target = columnById.get(link.target);
    if (source === undefined || target === undefined || source === target) continue;
    const first = Math.min(source, target);
    const last = Math.max(source, target);
    const weight = link.rework ? 4 : 1;
    cutCosts[first] += weight;
    cutCosts[last] -= weight;
    cutLanes[first] += 1;
    cutLanes[last] -= 1;
  }
  for (let index = 1; index < columns.length; index += 1) {
    cutCosts[index] += cutCosts[index - 1];
    cutLanes[index] += cutLanes[index - 1];
  }
  const occupied = (first: number, end: number, prefix: number) =>
    prefix + widthBefore[end] - widthBefore[first] + (end - first - 1) * COLUMN_GAP;
  let best: { fit: number; layout: WrappedRootLayout } | undefined;
  for (let bucket = 1; bucket <= candidateCount; bucket += 1) {
    const capacity = bucket * CAPACITY_STEP;
    const ranges: Array<{ first: number; end: number }> = [];
    let first = 0;
    while (first < columns.length) {
      const prefix = ranges.length ? 0 : START_PREFIX;
      const tailFitsOrSingleton = occupied(first, columns.length, prefix) <= capacity || first === columns.length - 1;
      // A triggered multi-column candidate must make a proper nonempty cut,
      // even at the largest capacity. Every loop consumes at least one column.
      if (tailFitsOrSingleton && ranges.length > 0) {
        ranges.push({ first, end: columns.length });
        break;
      }
      let furthest = first;
      let preferred = first;
      let preferredCost = Infinity;
      for (let end = first + 1; end < columns.length; end += 1) {
        const width = occupied(first, end, prefix);
        if (width > capacity) break;
        furthest = end;
        if (width >= capacity * 0.75 && cutCosts[end - 1] <= preferredCost) {
          // Ascending ends and <= give the stable furthest-right equal-cost tie.
          preferred = end;
          preferredCost = cutCosts[end - 1];
        }
      }
      const end = preferred > first ? preferred : furthest > first ? furthest : first + 1;
      ranges.push({ first, end });
      first = end;
    }
    const nextPositions = new Map<string, Point>();
    const rows: CanvasRootRow[] = [];
    const rowIndexByRootId = new Map<string, number>();
    let rowTop = 0;
    ranges.forEach((range, rowIndex) => {
      let x = rowIndex ? -START_PREFIX : 0;
      let height = rowIndex ? 0 : startHeight;
      const rootIds: string[] = [];
      for (let index = range.first; index < range.end; index += 1) {
        const column = columns[index];
        height = Math.max(height, column.height);
        for (const node of column.nodes) {
          nextPositions.set(node.id, { x, y: rowTop + positions.get(node.id)!.y });
          rowIndexByRootId.set(node.id, rowIndex);
          rootIds.push(node.id);
        }
        x += column.width + COLUMN_GAP;
      }
      const gapAfter = rowIndex === ranges.length - 1 ? 0 : Math.ceil((128 + 14 * cutLanes[range.end - 1]) / CANVAS_GRID) * CANVAS_GRID;
      rows.push({ rootIds, top: CANVAS_MARGIN_Y + rowTop, bottom: CANVAS_MARGIN_Y + rowTop + height, gapAfter });
      rowTop += height + gapAfter;
    });
    let minX = 0;
    let minY = startY;
    let maxX = START_WIDTH;
    let maxY = startY + startHeight;
    for (const node of nodes) {
      const point = nextPositions.get(node.id)!;
      const x = START_PREFIX + point.x;
      minX = Math.min(minX, x);
      minY = Math.min(minY, point.y);
      maxX = Math.max(maxX, x + node.width);
      maxY = Math.max(maxY, point.y + node.height);
    }
    const width = maxX - minX;
    const height = maxY - minY;
    if (!Number.isFinite(width) || width <= 0 || !Number.isFinite(height) || height <= 0) continue;
    const fit = Math.min(NOMINAL_WIDTH / width, NOMINAL_HEIGHT / height);
    // Capacities are ascending; an exact score tie keeps the smaller capacity.
    // Its cut vector is already deterministic from the boundary tie above.
    if (!best || fit > best.fit) best = { fit, layout: { positions: nextPositions, width, rootRows: { rows, rowIndexByRootId } } };
  }
  return best?.layout;
}
