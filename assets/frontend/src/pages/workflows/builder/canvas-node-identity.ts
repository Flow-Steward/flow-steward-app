/** Keeps a card's object while nothing about it changed, so React Flow redraws only the cards that did. */
import { useMemo } from "react";
import type { Node } from "@xyflow/react";

/** Card fields nest a few levels (data → lane frames → their add control); deeper values compare by identity. */
const COMPARE_DEPTH = 6;

function sameValue(left: unknown, right: unknown, depth: number): boolean {
  if (Object.is(left, right)) return true;
  if (depth <= 0 || !left || !right || typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length &&
      left.every((value, index) => sameValue(value, right[index], depth - 1));
  }
  // Only plain records compare by content; a Map, a Set or a class instance must be the same object.
  if (Object.getPrototypeOf(left) !== Object.prototype || Object.getPrototypeOf(right) !== Object.prototype) return false;
  const leftKeys = Object.keys(left);
  const rightRecord = right as Record<string, unknown>;
  return leftKeys.length === Object.keys(rightRecord).length && leftKeys.every((key) =>
    Object.hasOwn(rightRecord, key) && sameValue((left as Record<string, unknown>)[key], rightRecord[key], depth - 1));
}

/**
 * The next cards, with each card that equals its earlier self replaced by that earlier object. When every
 * card is unchanged and in the same order, the earlier list itself comes back.
 */
export function reuseUnchangedNodes<T extends Node>(previous: readonly T[] | null, next: readonly T[]): T[] {
  if (!previous) return [...next];
  const earlier = new Map(previous.map((node) => [node.id, node]));
  let allKept = previous.length === next.length;
  const result = next.map((node, index) => {
    const kept = earlier.get(node.id);
    const reuse = kept !== undefined && sameValue(kept, node, COMPARE_DEPTH);
    if (!reuse || previous[index] !== kept) allKept = false;
    return reuse ? kept : node;
  });
  return allKept ? previous as T[] : result;
}

/** Holds the last list for the next render; one per canvas view. */
class StableNodeList<T extends Node> {
  private last: T[] | null = null;

  next(nodes: readonly T[]): T[] {
    this.last = reuseUnchangedNodes(this.last, nodes);
    return this.last;
  }
}

/** `reuseUnchangedNodes` across renders: the list keeps the objects of cards that did not change. */
export function useStableCanvasNodes<T extends Node>(next: readonly T[]): T[] {
  const list = useMemo(() => new StableNodeList<T>(), []);
  return useMemo(() => list.next(next), [list, next]);
}

/** Marks cards selected; a card whose object and flag did not change keeps its marked object. */
class SelectedNodeMarks {
  private marks = new Map<string, { from: Node; selected: boolean; marked: Node }>();

  mark(nodes: readonly Node[], selectedIds: ReadonlySet<string>): Node[] {
    const next = new Map<string, { from: Node; selected: boolean; marked: Node }>();
    const marked = nodes.map((node) => {
      if (node.type === "canvasNote") return node;
      const selected = selectedIds.has(node.id);
      const kept = this.marks.get(node.id);
      const entry = kept && kept.from === node && kept.selected === selected ? kept : { from: node, selected, marked: { ...node, selected } };
      next.set(node.id, entry);
      return entry.marked;
    });
    this.marks = next;
    return marked;
  }
}

/** Each card with its `selected` flag, keeping the marked objects of cards that did not change. */
export function useSelectedCanvasNodes(nodes: readonly Node[], selectedIds: ReadonlySet<string>): Node[] {
  const marks = useMemo(() => new SelectedNodeMarks(), []);
  return useMemo(() => marks.mark(nodes, selectedIds), [marks, nodes, selectedIds]);
}
