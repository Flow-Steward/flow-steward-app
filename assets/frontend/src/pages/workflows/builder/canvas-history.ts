import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import type { WorkflowNodeRef } from "@/pages/workflows/shared/api";
import type { CanvasPosition } from "@/pages/workflows/builder/canvas-core";
import {
  applyChangeOperations,
  operationTouchesStep,
  reconcileChangeOperations,
  stepId,
  type WorkflowDefinitionModel,
} from "@/pages/workflows/builder/change-set";
import { definitionRestoreOperations } from "@/pages/workflows/builder/canvas-history-restore";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

/** null means that the node used automatic placement before this gesture. */
export type CanvasPlacementValues = Record<string, CanvasPosition | null>;
export type CanvasPlacementDelta = {
  canvasWorkflowId: string;
  subjectIds: string[];
  /** Canonical child Step ids for dropStagedStep; display ids remain for canvas ownership. */
  canonicalStepSubjectIds: string[];
  viewKey?: string;
  before: CanvasPlacementValues;
  after: CanvasPlacementValues;
};

/** Identity survives parameter edits, Undo/Redo and an owner-local revision reset. */
export type GraphIntentToken = Readonly<{ controller: symbol; serial: number }>;

/** The longest history kept per workflow session; the oldest actions go first. */
export const HISTORY_LIMIT = 200;

/**
 * One author action (Canvas › Undo). `operations` is what it staged; `inverse`,
 * set once the action is on the server (Apply or an own one-step save), is what
 * takes it back there. An item without operations is a canvas placement only,
 * which never reaches the server.
 */
export type JournalItem = {
  id: number;
  operations: WorkflowChangeOperation[];
  inverse?: WorkflowChangeOperation[];
  placement?: CanvasPlacementDelta;
  /** Session-only: lets a toast's Undo name exactly this action. Never stored. */
  intentToken?: GraphIntentToken;
  /** Page-wide order of the action (or of its Redo), and of its latest Undo. */
  seq: number;
  undoneSeq?: number;
};

/** One part of the unapplied set: an action, or the reverse of a saved one. */
export type PendingEntry = {
  item: number;
  direction: "forward" | "inverse";
  operations: WorkflowChangeOperation[];
};

/**
 * One workflow's history. Items before `cursor` are done, the rest can be
 * redone. `pending` is the unapplied set Apply sends, in order; it is the
 * difference between the server checkpoint and the canvas. The history does not
 * end at Apply: undoing a saved action stages its reverse, so the server
 * changes only on the next explicit Apply.
 */
export type GraphJournal = {
  items: JournalItem[];
  cursor: number;
  pending: PendingEntry[];
  nextId: number;
};

let actionSequence = 0;

/** The next page-wide action number: Undo follows the latest action across owners. */
export function nextActionSequence(): number {
  actionSequence += 1;
  return actionSequence;
}

/** A restored history continues after its own newest action. */
export function noteActionSequence(journal: GraphJournal): void {
  const newest = Math.max(0, ...journal.items.flatMap((item) => [item.seq, item.undoneSeq ?? 0]));
  if (newest > actionSequence) actionSequence = newest;
}

export function emptyGraphJournal(): GraphJournal {
  return { items: [], cursor: 0, pending: [], nextId: 1 };
}

export function flattenGraphJournal(journal: GraphJournal): WorkflowChangeOperation[] {
  return journal.pending.flatMap((entry) => entry.operations);
}

/** Whether the journal holds any action, done or undone, or anything unapplied. */
export function graphJournalIsEmpty(journal: GraphJournal): boolean {
  return !journal.items.length && !journal.pending.length;
}

function cap(journal: GraphJournal): GraphJournal {
  const extra = journal.items.length - HISTORY_LIMIT;
  if (extra <= 0) return journal;
  return { ...journal, items: journal.items.slice(extra), cursor: Math.max(0, journal.cursor - extra) };
}

/** A new action: it ends the redo branch, as in any editor. */
export function appendGraphIntent(
  journal: GraphJournal,
  operations: WorkflowChangeOperation[],
  options: { placement?: CanvasPlacementDelta; intentToken?: GraphIntentToken; seq?: number } = {},
): GraphJournal {
  if (!operations.length && !options.placement) return journal;
  const item: JournalItem = {
    id: journal.nextId,
    operations,
    seq: options.seq ?? nextActionSequence(),
    ...(options.placement ? { placement: options.placement } : {}),
    ...(options.intentToken ? { intentToken: options.intentToken } : {}),
  };
  return cap({
    items: [...journal.items.slice(0, journal.cursor), item],
    cursor: journal.cursor + 1,
    pending: operations.length ? [...journal.pending, { item: item.id, direction: "forward", operations }] : journal.pending,
    nextId: journal.nextId + 1,
  });
}

/**
 * An action that is already on the server (an own one-step save): it joins the
 * history with its reverse and adds nothing to the unapplied set.
 */
export function appendSavedAction(
  journal: GraphJournal,
  operations: WorkflowChangeOperation[],
  inverse: WorkflowChangeOperation[],
  options: { intentToken?: GraphIntentToken; seq?: number } = {},
): GraphJournal {
  if (!operations.length || !inverse.length) return journal;
  const item: JournalItem = {
    id: journal.nextId,
    operations,
    inverse,
    seq: options.seq ?? nextActionSequence(),
    ...(options.intentToken ? { intentToken: options.intentToken } : {}),
  };
  return cap({
    items: [...journal.items.slice(0, journal.cursor), item],
    cursor: journal.cursor + 1,
    pending: journal.pending,
    nextId: journal.nextId + 1,
  });
}

function lastPendingIs(journal: GraphJournal, item: JournalItem, direction: PendingEntry["direction"]): boolean {
  const last = journal.pending.at(-1);
  return last?.item === item.id && last.direction === direction;
}

/** The action Undo would take back, when it can be taken back. */
export function nextUndoItem(journal: GraphJournal): JournalItem | null {
  const item = journal.items[journal.cursor - 1];
  if (!item) return null;
  if (!item.operations.length || lastPendingIs(journal, item, "forward") || item.inverse) return item;
  return null;
}

/** The action Redo would do again. */
export function nextRedoItem(journal: GraphJournal): JournalItem | null {
  return journal.items[journal.cursor] ?? null;
}

/** Attach one visual before/after to its own active creation, without adding an Undo step. */
export function attachCreationPlacement(
  journal: GraphJournal,
  token: GraphIntentToken,
  target: WorkflowNodeRef,
  displayId: string,
  placement: CanvasPlacementDelta,
): GraphJournal {
  const index = journal.items.findIndex((item, at) => at < journal.cursor && item.intentToken === token);
  const exactKey = (values: CanvasPlacementValues) => Object.keys(values).length === 1 && Object.hasOwn(values, displayId);
  if (index < 0 || journal.items[index].placement ||
    placement.subjectIds.length !== 1 || placement.subjectIds[0] !== displayId ||
    !exactKey(placement.before) || !exactKey(placement.after) ||
    (target.kind === "step"
      ? placement.canonicalStepSubjectIds.length !== 1 || placement.canonicalStepSubjectIds[0] !== target.id
      : placement.canonicalStepSubjectIds.length !== 0)) return journal;
  // A later move stays later even when Undo or another move returns the same XY.
  if (journal.items.slice(index + 1).some((item) => item.placement?.subjectIds.includes(displayId))) return journal;
  const createsTarget = journal.items[index].operations.some((operation) => {
    if (target.kind === "step") return (operation.op === "create_step" || operation.op === "insert_step_between") && stepId(operation.step) === target.id;
    if (target.kind === "group") return operation.op === "create_group" && operation.group.group_id === target.id;
    return operation.op === "create_for_each" && operation.for_each.for_each_id === target.id;
  });
  if (!createsTarget) return journal;
  const items = [...journal.items];
  items[index] = { ...items[index], placement };
  return { ...journal, items };
}

export function hasActiveGraphIntent(journal: GraphJournal, token: GraphIntentToken): boolean {
  return journal.items.slice(0, journal.cursor).some((item) => item.intentToken === token);
}

/** The token of the intent Undo would reverse next, if it has one. */
export function latestGraphIntentToken(journal: GraphJournal): GraphIntentToken | null {
  return nextUndoItem(journal)?.intentToken ?? null;
}

export function canUndoGraphIntent(journal: GraphJournal): boolean {
  return nextUndoItem(journal) !== null;
}

export function canRedoGraphIntent(journal: GraphJournal): boolean {
  return nextRedoItem(journal) !== null;
}

/** When Undo's action happened, and when Redo's action was undone: the keys follow the newest. */
export function undoSequence(journal: GraphJournal): number | null {
  return nextUndoItem(journal)?.seq ?? null;
}

export function redoSequence(journal: GraphJournal): number | null {
  const item = nextRedoItem(journal);
  return item ? item.undoneSeq ?? item.seq : null;
}

export function placementUndoneBy(journal: GraphJournal): CanvasPlacementDelta | undefined {
  return nextUndoItem(journal)?.placement;
}

export function placementRedoneBy(journal: GraphJournal): CanvasPlacementDelta | undefined {
  return nextRedoItem(journal)?.placement;
}

/** A view switch retires only visual placements, retaining structural edits. */
export function retirePlacementIntents(journal: GraphJournal): GraphJournal {
  let cursor = journal.cursor;
  const items = journal.items.flatMap((item, index) => {
    if (!item.placement) return [item];
    if (item.operations.length) return [{ ...item, placement: undefined }];
    if (index < journal.cursor) cursor -= 1;
    return [];
  });
  return { ...journal, items, cursor };
}

/**
 * Take back the latest action: an unapplied one leaves the set; a saved one
 * stages its reverse; a placement only moves the cards back. Nothing is written.
 */
export function undoGraphIntent(journal: GraphJournal, seq = nextActionSequence()): GraphJournal {
  const item = nextUndoItem(journal);
  if (!item) return journal;
  const items = journal.items.map((current) => current === item ? { ...current, undoneSeq: seq } : current);
  let pending = journal.pending;
  if (item.operations.length) {
    pending = lastPendingIs(journal, item, "forward")
      ? journal.pending.slice(0, -1)
      : [...journal.pending, { item: item.id, direction: "inverse", operations: item.inverse ?? [] }];
  }
  return { ...journal, items, cursor: journal.cursor - 1, pending };
}

/** Do the next undone action again, the mirror of Undo. */
export function redoGraphIntent(journal: GraphJournal, seq = nextActionSequence()): GraphJournal {
  const item = nextRedoItem(journal);
  if (!item) return journal;
  const items = journal.items.map((current) => current === item ? { ...current, seq, undoneSeq: undefined } : current);
  let pending = journal.pending;
  if (item.operations.length) {
    pending = lastPendingIs(journal, item, "inverse")
      ? journal.pending.slice(0, -1)
      : [...journal.pending, { item: item.id, direction: "forward", operations: item.operations }];
  }
  return { ...journal, items, cursor: journal.cursor + 1, pending };
}

/**
 * The unapplied set is on the server now (Apply, or an explicitly kept own
 * copy). Each action it carried learns its reverse from the definition before
 * and after it, so a later Undo stages that reverse. The history stays.
 */
export function applyGraphJournal(journal: GraphJournal, serverDefinition: WorkflowDefinitionModel): GraphJournal {
  let definition = serverDefinition;
  const inverses = new Map<number, WorkflowChangeOperation[]>();
  journal.pending.forEach((entry) => {
    const operations = reconcileChangeOperations(definition, entry.operations);
    const next = applyChangeOperations(definition, operations).definition;
    if (entry.direction === "forward") inverses.set(entry.item, definitionRestoreOperations(next, definition));
    definition = next;
  });
  return {
    ...journal,
    items: journal.items.map((item) => inverses.has(item.id) ? { ...item, inverse: inverses.get(item.id) } : item),
    pending: [],
  };
}

/** Discard returns the canvas to the server checkpoint: only what the server holds stays in the history. */
export function discardGraphJournal(journal: GraphJournal): GraphJournal {
  const forward = new Set(journal.pending.filter((entry) => entry.direction === "forward").map((entry) => entry.item));
  const inverse = new Set(journal.pending.filter((entry) => entry.direction === "inverse").map((entry) => entry.item));
  const items = journal.items.filter((item, index) => (index < journal.cursor
    ? !forward.has(item.id) && (item.operations.length > 0 || Boolean(item.placement))
    : item.operations.length > 0 && inverse.has(item.id)));
  return { ...journal, items: items.map((item) => ({ ...item, undoneSeq: undefined })), cursor: items.length, pending: [] };
}

function updateStep(operation: WorkflowChangeOperation, targetStepId: string, patch: Record<string, unknown>): WorkflowChangeOperation {
  if (
    (operation.op === "create_step" || operation.op === "insert_step_between") &&
    stepId(operation.step) === targetStepId
  ) {
    return { ...operation, step: { ...operation.step, ...patch } };
  }
  if (operation.op === "change_step_kind" && operation.step_id === targetStepId) {
    return {
      ...operation,
      step: { ...operation.step, ...patch, step_kind: stringFromUnknownValue(patch.step_kind) || operation.step.step_kind },
    };
  }
  return operation;
}

/** The staged operations with one creation's (or kind change's) parameters replaced. */
export function withStepParameters(operations: WorkflowChangeOperation[], targetStepId: string, patch: Record<string, unknown>): WorkflowChangeOperation[] {
  return operations.map((operation) => updateStep(operation, targetStepId, patch));
}

/**
 * Remove a step this set created, with every staged operation that mentions it.
 * It matches the existing drop behavior on the reconciled set: an insert may
 * have expanded to create + connect; dropping its source keeps that created
 * step. The redo branch ends, because its actions may name the step.
 */
export function dropStepOperations(
  journal: GraphJournal,
  definition: WorkflowDefinitionModel,
  targetStepId: string,
): GraphJournal {
  const rawPrefix: WorkflowChangeOperation[] = [];
  let emitted = 0;
  const changed = new Map<number, WorkflowChangeOperation[]>();
  const pending = journal.pending.map((entry) => {
    rawPrefix.push(...entry.operations);
    const effectivePrefix = reconcileChangeOperations(definition, rawPrefix);
    const effectiveEntry = effectivePrefix.slice(emitted);
    emitted = effectivePrefix.length;
    const operations = effectiveEntry.filter((operation) => !operationTouchesStep(operation, targetStepId));
    if (entry.direction === "forward") changed.set(entry.item, operations);
    return { ...entry, operations };
  }).filter((entry) => entry.operations.length);
  const items = journal.items.slice(0, journal.cursor).flatMap((item) => {
    const operations = changed.get(item.id) ?? item.operations;
    const placement = item.placement?.canonicalStepSubjectIds.includes(targetStepId) ? undefined : item.placement;
    return operations.length || placement ? [{ ...item, operations, placement }] : [];
  });
  return { ...journal, items, cursor: items.length, pending };
}

/** The journal as stored: without session-only tokens. */
export function storableGraphJournal(journal: GraphJournal): GraphJournal {
  return {
    ...journal,
    items: journal.items.map((item) => {
      const stored = { ...item };
      delete stored.intentToken;
      return stored;
    }),
  };
}
