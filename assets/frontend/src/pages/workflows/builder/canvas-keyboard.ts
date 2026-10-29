/**
 * Design-tool keys on the canvas only (Canvas › Keys). Codes are layout-independent,
 * so they work with any keyboard layout. Text fields keep their own keys.
 *
 * The canvas keydown handler (canvas-shell) runs `runCanvasKey`; each binding below is
 * one key. EXTENSION POINT: another area adds its key as one more row in
 * `CANVAS_KEY_BINDINGS` and one optional callback in `CanvasKeyContext`.
 */
import type { Edge, Node } from "@xyflow/react";
import type { WorkflowOutcome } from "@/pages/workflows/shared/api";
import { outcomeHandleId } from "./api";

export type CanvasWalkDirection = "right" | "left" | "up" | "down";

export type CanvasKeyContext = {
  structureLocked: boolean;
  /** Fit everything (Shift+1), the selection (Shift+2). */
  fit: (mode: "fit" | "selection") => void;
  zoomTo100: () => void;
  zoomStep: (direction: 1 | -1) => void;
  openSearch: () => void;
  addStep: () => void;
  autoLayout: () => void;
  selectAll: () => void;
  /** Esc: close the connection bar first, else clear the selection. False when there is nothing to close. */
  escape: () => boolean;
  /** Delete, Backspace: the selection, the selected node or the selected connection. */
  deleteSelection: () => boolean;
  walk: (direction: CanvasWalkDirection) => boolean;
  nudge: (dx: number, dy: number) => boolean;
  copy: () => boolean;
  cut: () => boolean;
  paste: () => boolean;
  duplicate: () => boolean;
  undo: () => boolean;
  redo: () => boolean;
  /** N — NOTES: add a note in the middle of the view. */
  addNote?: () => void;
  /** ⌘⇧G — CANVAS-B: group the selection (For each or Parallel lanes). */
  groupSelection?: () => boolean;
};

type KeyLike = Pick<KeyboardEvent, "code" | "key" | "shiftKey" | "altKey" | "metaKey" | "ctrlKey">;
type CanvasKeyBinding = {
  match: (event: KeyLike) => boolean;
  run: (context: CanvasKeyContext) => boolean | void;
  /** Runs on a protected (read-only) structure too. */
  readOnly?: boolean;
  /** Also runs while a button or link inside the canvas has the focus. */
  onControls?: boolean;
};

const mod = (event: KeyLike) => event.metaKey || event.ctrlKey;
const plain = (event: KeyLike) => !mod(event) && !event.altKey && !event.shiftKey;
const shifted = (event: KeyLike) => !mod(event) && !event.altKey && event.shiftKey;
const command = (event: KeyLike, code: string, shift = false) => mod(event) && !event.altKey && event.shiftKey === shift && event.code === code;
const ARROWS: Record<string, CanvasWalkDirection> = { ArrowRight: "right", ArrowLeft: "left", ArrowUp: "up", ArrowDown: "down" };
const NUDGE: Record<CanvasWalkDirection, [number, number]> = { right: [1, 0], left: [-1, 0], up: [0, -1], down: [0, 1] };
export const CANVAS_GRID_STEP = 16;

/**
 * Undo and Redo (Canvas › Undo): ⌘/Ctrl+Z, ⌘/Ctrl+Shift+Z and ⌘/Ctrl+Y, by the
 * physical key, so a Russian or any other layout works the same. The canvas
 * runs them as its own keys; the builder page runs them everywhere else except
 * while typing in a text field, which keeps its own Undo.
 */
export function historyKey(event: KeyLike): "undo" | "redo" | null {
  if (command(event, "KeyZ")) return "undo";
  if (command(event, "KeyZ", true) || command(event, "KeyY")) return "redo";
  return null;
}

const TEXT_INPUT_TYPES = new Set(["text", "search", "email", "url", "tel", "password", "number", "date", "datetime-local", "month", "time", "week"]);

/** A field where ⌘Z undoes typing (an input, a text area, a code editor): it keeps its own Undo. */
export function isTextEntryTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  if (target instanceof HTMLElement && target.isContentEditable) return true;
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) return TEXT_INPUT_TYPES.has(target.type.toLowerCase());
  return Boolean(target.closest("[contenteditable]:not([contenteditable='false']), [role='textbox'], [role='combobox'], [role='searchbox'], .cm-editor, .monaco-editor"));
}

/**
 * An open modal dialog keeps its keys: the builder behind it does not react. The
 * builder's side drawer is a non-modal dialog, so the keys work while it is open.
 */
export function modalDialogOpen(root: ParentNode = document): boolean {
  return Boolean(root.querySelector('dialog[open][aria-modal="true"], [role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]'));
}

export const CANVAS_KEY_BINDINGS: readonly CanvasKeyBinding[] = [
  { match: (e) => shifted(e) && e.code === "Digit1", run: (c) => c.fit("fit"), readOnly: true, onControls: true },
  { match: (e) => shifted(e) && e.code === "Digit2", run: (c) => c.fit("selection"), readOnly: true, onControls: true },
  { match: (e) => shifted(e) && e.code === "Digit0", run: (c) => c.zoomTo100(), readOnly: true, onControls: true },
  { match: (e) => !mod(e) && !e.altKey && (e.code === "Equal" || e.code === "NumpadAdd"), run: (c) => c.zoomStep(1), readOnly: true, onControls: true },
  { match: (e) => !mod(e) && !e.altKey && !e.shiftKey && (e.code === "Minus" || e.code === "NumpadSubtract"), run: (c) => c.zoomStep(-1), readOnly: true, onControls: true },
  { match: (e) => command(e, "KeyF"), run: (c) => c.openSearch(), readOnly: true, onControls: true },
  { match: (e) => plain(e) && e.code === "KeyA", run: (c) => c.addStep(), onControls: true },
  { match: (e) => shifted(e) && e.code === "KeyA", run: (c) => c.autoLayout(), onControls: true },
  { match: (e) => command(e, "KeyA"), run: (c) => c.selectAll(), readOnly: true },
  { match: (e) => !mod(e) && !e.altKey && e.key === "Escape", run: (c) => c.escape(), readOnly: true, onControls: true },
  { match: (e) => !mod(e) && !e.altKey && (e.key === "Delete" || e.key === "Backspace"), run: (c) => c.deleteSelection(), readOnly: true },
  { match: (e) => command(e, "KeyC"), run: (c) => c.copy(), readOnly: true },
  { match: (e) => command(e, "KeyX"), run: (c) => c.cut() },
  { match: (e) => command(e, "KeyV"), run: (c) => c.paste(), readOnly: true },
  { match: (e) => command(e, "KeyD"), run: (c) => c.duplicate(), readOnly: true },
  { match: (e) => historyKey(e) === "undo", run: (c) => c.undo(), readOnly: true, onControls: true },
  { match: (e) => historyKey(e) === "redo", run: (c) => c.redo(), readOnly: true, onControls: true },
  // Extension points: one row per key owned by another area.
  { match: (e) => plain(e) && e.code === "KeyN", run: (c) => {
    if (!c.addNote) return false;
    c.addNote();
    return true;
  } },
  { match: (e) => command(e, "KeyG", true), run: (c) => c.groupSelection?.() ?? false },
];

/** Run the key's binding. True when the key was used (the caller prevents its default). */
export function runCanvasKey(event: KeyLike, context: CanvasKeyContext, onControl = false): boolean {
  const arrow = ARROWS[event.code];
  // A focused control keeps its own arrows; the walk starts from the canvas or a node.
  if (arrow && onControl) return false;
  if (arrow && !mod(event) && !event.altKey) {
    if (event.shiftKey) {
      if (context.structureLocked) return false;
      const [dx, dy] = NUDGE[arrow];
      return context.nudge(dx * CANVAS_GRID_STEP, dy * CANVAS_GRID_STEP);
    }
    return context.walk(arrow);
  }
  const binding = CANVAS_KEY_BINDINGS.find((candidate) => candidate.match(event));
  if (!binding || (context.structureLocked && !binding.readOnly) || (onControl && !binding.onControls)) return false;
  return binding.run(context) !== false;
}

/** Zoom by stops, from 25% to 200% (Canvas › View). */
export const CANVAS_ZOOM_STOPS = [0.25, 0.33, 0.5, 0.67, 0.75, 1, 1.25, 1.5, 2] as const;
export function nextZoomStop(zoom: number, direction: 1 | -1): number {
  if (direction > 0) return CANVAS_ZOOM_STOPS.find((stop) => stop > zoom + 0.001) ?? CANVAS_ZOOM_STOPS.at(-1)!;
  return [...CANVAS_ZOOM_STOPS].reverse().find((stop) => stop < zoom - 0.001) ?? CANVAS_ZOOM_STOPS[0];
}

/** What a walk remembers: where each node was entered from, and the group just walked out of. */
export type CanvasWalkMemory = { back: Record<string, string>; out: string | null };

type WalkNodeData = { isEntry?: boolean; entryNodeId?: string; step?: { ports?: { outcome: WorkflowOutcome }[] }; ports?: { outcome: WorkflowOutcome }[] };

const isContainer = (id: string) => id.startsWith("group:") || id.startsWith("for_each:");
const walkable = (node: Node) => node.type !== "canvasNote" && !node.hidden;

function absoluteBox(node: Node, byId: ReadonlyMap<string, Node>) {
  let x = node.position.x; let y = node.position.y;
  let parent = node.parentId ? byId.get(node.parentId) : undefined;
  const seen = new Set([node.id]);
  while (parent && !seen.has(parent.id)) {
    seen.add(parent.id); x += parent.position.x; y += parent.position.y;
    parent = parent.parentId ? byId.get(parent.parentId) : undefined;
  }
  return { x, y, width: Number(node.width || node.measured?.width || node.style?.width || 0), height: Number(node.height || node.measured?.height || node.style?.height || 0) };
}

function portRank(node: Node | undefined, handle: string | null | undefined) {
  if (!handle) return 0;
  if (handle === "error") return 1000;
  const data = (node?.data || {}) as WalkNodeData;
  const ports = data.step?.ports || data.ports || [];
  const order = ["true", "approved", "success"];
  const index = ports.findIndex((port) => outcomeHandleId(port.outcome) === handle);
  const preferred = order.indexOf(handle);
  return preferred >= 0 ? preferred - order.length : index >= 0 ? index : 500;
}

/**
 * Arrows follow the connections (Canvas › Walking the graph): → along the node's connection
 * (an If along True first), ← back the way it came, ↑ ↓ between the nodes the same step leads
 * to, else the nearest node above or below. → on a group goes into it; out of its last step the
 * walk returns to the group, and the next → follows the group's own connection. With nothing
 * selected, an arrow picks the entry.
 */
export function walkCanvasGraph({ current, direction, nodes, edges, memory }: Readonly<{
  current: string | null;
  direction: CanvasWalkDirection;
  nodes: readonly Node[];
  edges: readonly Edge[];
  memory: CanvasWalkMemory;
}>): string | null {
  const byId = new Map(nodes.filter(walkable).map((node) => [node.id, node]));
  const start = byId.get("visual:start");
  const entry = (start?.data as WalkNodeData | undefined)?.entryNodeId;
  if (!current || !byId.has(current)) return entry && byId.has(entry) ? entry : start?.id ?? null;
  const live = edges.filter((edge) => byId.has(edge.source) && byId.has(edge.target));
  let next: string | null = null;
  if (direction === "right") {
    const children = [...byId.values()].filter((node) => node.parentId === current);
    const inside = children.find((node) => (node.data as WalkNodeData).isEntry) ?? [...children].sort((a, b) => a.position.x - b.position.x || a.position.y - b.position.y)[0];
    if (isContainer(current) && memory.out !== current && inside) next = inside.id;
    else {
      const outgoing = live.filter((edge) => edge.source === current && edge.targetHandle !== "exit")
        .sort((a, b) => portRank(byId.get(current), a.sourceHandle) - portRank(byId.get(current), b.sourceHandle));
      next = outgoing[0]?.target ?? null;
      if (!next) {
        const exit = live.find((edge) => edge.source === current && edge.targetHandle === "exit");
        const parent = byId.get(current)?.parentId;
        next = exit?.target ?? (parent && isContainer(parent) ? parent : null);
        if (next) memory.out = next;
      }
    }
  } else if (direction === "left") {
    const sources = live.filter((edge) => edge.target === current && edge.targetHandle !== "exit").map((edge) => edge.source);
    const back = memory.back[current];
    next = back && sources.includes(back) ? back : sources[0] ?? byId.get(current)?.parentId ?? null;
  } else {
    const from = memory.back[current] ?? live.find((edge) => edge.target === current)?.source;
    const box = (id: string) => absoluteBox(byId.get(id)!, byId);
    const siblings = [...new Set(from ? live.filter((edge) => edge.source === from && edge.targetHandle !== "exit").map((edge) => edge.target) : [])]
      .sort((a, b) => box(a).y - box(b).y);
    if (siblings.length > 1 && siblings.includes(current)) {
      const index = siblings.indexOf(current);
      next = siblings[(index + (direction === "down" ? 1 : siblings.length - 1)) % siblings.length];
    } else {
      const here = box(current); const centre = here.x + here.width / 2;
      const candidates = [...byId.keys()].filter((id) => id !== current).map((id) => ({ id, rect: box(id) }))
        .filter(({ rect }) => direction === "down" ? rect.y >= here.y + here.height : rect.y + rect.height <= here.y);
      candidates.sort((a, b) => (Math.abs(a.rect.x + a.rect.width / 2 - centre) + Math.abs(a.rect.y - here.y)) -
        (Math.abs(b.rect.x + b.rect.width / 2 - centre) + Math.abs(b.rect.y - here.y)));
      next = candidates[0]?.id ?? null;
    }
  }
  if (!next || next === current) return null;
  if (memory.out && memory.out !== next) memory.out = null;
  if (direction === "right" && memory.out !== next) memory.back = { ...memory.back, [next]: current };
  return next;
}
