import { useSyncExternalStore } from "react";
import type { WorkflowCanvasForEach, WorkflowCanvasStep, WorkflowGraphEdge } from "@/pages/workflows/shared/api";
import { CANVAS_GRID, MEASURED_HEIGHT_BUFFER } from "./layout-constants";

/**
 * Inline loop: a For each drawn as one card that lists its body. The workflow
 * model has no separate inline-loop kind, so this is a canvas presentation of
 * an ordinary For each, kept per workflow in this browser (like manual places).
 */
export const INLINE_LOOP_WIDTH = 256;
const INLINE_LOOP_BASE_HEIGHT = 128;
const INLINE_LOOP_ROW_HEIGHT = 22;
/** Kinds an inline card can list: a few simple steps, never a branch or a stop. */
const INLINE_BODY_KINDS = new Set(["agent", "connector", "invoke_workflow", "http_request", "dataset_query", "set_variable", "collect", "transform", "tabular_feed", "file_from_base64", "template", "wait", "batch", "continue", "break"]);
const INLINE_MAX_STEPS = 6;

export type InlineLoopBodyStep = { id: string; name: string; summary: string; kind: string; toolCategory?: string };

export function inlineLoopStorageKey(workflowId: string) {
  return `platform.workflowCanvas.inlineLoops.${workflowId}`;
}

const listeners = new Set<() => void>();
const snapshots = new Map<string, { raw: string | null; ids: ReadonlySet<string> }>();
const EMPTY: ReadonlySet<string> = new Set();

function readRaw(workflowId: string): string | null {
  try { return globalThis.localStorage?.getItem(inlineLoopStorageKey(workflowId)) ?? null; } catch { return null; }
}

/** The For each ids of this workflow drawn as inline loop cards. */
export function inlineLoopIds(workflowId: string): ReadonlySet<string> {
  if (!workflowId) return EMPTY;
  const raw = readRaw(workflowId);
  const cached = snapshots.get(workflowId);
  if (cached?.raw === raw) return cached.ids;
  let ids: ReadonlySet<string> = EMPTY;
  try {
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    if (Array.isArray(parsed)) ids = new Set(parsed.filter((value): value is string => typeof value === "string" && Boolean(value)));
  } catch { ids = EMPTY; }
  snapshots.set(workflowId, { raw, ids });
  return ids;
}

export function setInlineLoop(workflowId: string, forEachId: string, inline: boolean) {
  if (!workflowId || !forEachId) return;
  const next = new Set(inlineLoopIds(workflowId));
  if (inline) next.add(forEachId); else next.delete(forEachId);
  try {
    if (next.size) globalThis.localStorage?.setItem(inlineLoopStorageKey(workflowId), JSON.stringify([...next].sort((left, right) => left.localeCompare(right))));
    else globalThis.localStorage?.removeItem(inlineLoopStorageKey(workflowId));
  } catch { /* The presentation stays for this session only. */ }
  listeners.forEach((listener) => listener());
}

export function useInlineLoopIds(workflowId: string): ReadonlySet<string> {
  return useSyncExternalStore(
    (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    () => inlineLoopIds(workflowId),
    () => EMPTY,
  );
}

/** A For each can show as an inline card only while its body is a short sequence of simple steps. */
export function inlineLoopEligible(forEach: WorkflowCanvasForEach, steps: readonly WorkflowCanvasStep[], forEachItems: readonly WorkflowCanvasForEach[]): boolean {
  const body = forEach.body || {};
  if (body.kind === "lanes" || Array.isArray(body.lanes)) return false;
  if (forEachItems.some((item) => item.parent?.kind === "for_each" && item.parent.id === forEach.id)) return false;
  const members = steps.filter((step) => step.for_each_id === forEach.id);
  return members.length <= INLINE_MAX_STEPS && members.every((step) => INLINE_BODY_KINDS.has(step.kind));
}

/** The body in walk order: from its entry along Next, then anything left. */
export function inlineLoopBody(forEach: WorkflowCanvasForEach, steps: readonly WorkflowCanvasStep[], edges: readonly WorkflowGraphEdge[]): InlineLoopBodyStep[] {
  const members = steps.filter((step) => step.for_each_id === forEach.id);
  const byId = new Map(members.map((step) => [step.step_id || step.id, step]));
  const entry = (forEach.body?.entry as { id?: unknown } | undefined)?.id;
  const ordered: WorkflowCanvasStep[] = [];
  let current = typeof entry === "string" ? byId.get(entry) : undefined;
  while (current && !ordered.includes(current)) {
    ordered.push(current);
    const from = current.step_id || current.id;
    const next = edges.find((edge) => edge.source.kind === "step" && edge.source.id === from && edge.outcome.name === "success" && edge.target.kind === "step");
    current = next?.target.kind === "step" ? byId.get(next.target.id) : undefined;
  }
  members.forEach((step) => { if (!ordered.includes(step)) ordered.push(step); });
  return ordered.map((step) => ({ id: step.id, name: step.display_name, summary: step.config_summary || "", kind: step.kind, ...(step.tool_category ? { toolCategory: step.tool_category } : {}) }));
}

export function inlineLoopHeight(bodyLength: number, measured?: number): number {
  const estimated = INLINE_LOOP_BASE_HEIGHT + Math.max(1, bodyLength) * INLINE_LOOP_ROW_HEIGHT;
  const fromMeasure = Number.isFinite(measured) ? Math.ceil(Number(measured) + MEASURED_HEIGHT_BUFFER) : 0;
  return Math.ceil(Math.max(estimated, fromMeasure) / CANVAS_GRID) * CANVAS_GRID;
}
