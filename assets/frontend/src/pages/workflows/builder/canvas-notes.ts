import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Node, NodeChange } from "@xyflow/react";

import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { isWorkflowRevisionConflict } from "@/pages/workflows/builder/api";
import type { GraphEditNotice } from "@/pages/workflows/builder/canvas-core";
import type { CanvasNoteData } from "@/pages/workflows/builder/canvas-note-node";
import {
  clearCanvasNoteImage, deleteCanvasNote, fetchCanvasNotes, isCanvasNotesRevisionConflict, saveCanvasNote, uploadCanvasNoteImage,
  type CanvasNote, type CanvasNotesResult,
} from "@/pages/workflows/builder/canvas-notes-api";
import { CANVAS_GRID } from "@/pages/workflows/builder/layout-constants";

/** A new note opens as a landscape sheet; it never shrinks below the minimum or its text. */
export const NOTE_DEFAULT_SIZE = { width: 256, height: 160 } as const;
export const NOTE_MIN_SIZE = { width: 192, height: 96 } as const;
/** How long a removed note waits for Undo before the server forgets it (image included). */
export const NOTE_UNDO_WINDOW_MS = 6000;
const NOTE_NODE_PREFIX = "annotation:";

/** A screen point where a dragged note lands; without one the note goes to the middle of the view. */
export type CanvasNoteDropPoint = { clientX: number; clientY: number };

/**
 * N puts a note in the middle of the view, as a plain click on Note does. The canvas
 * keydown handler calls this with the event; it says whether the key was taken.
 */
export function handleCanvasNoteShortcut(event: KeyboardEvent, addNote: (() => void) | null): boolean {
  if (!addNote || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey || event.code !== "KeyN") return false;
  event.preventDefault();
  addNote();
  return true;
}

export function snapToCanvasGrid(value: number): number {
  return Math.round(value / CANVAS_GRID) * CANVAS_GRID;
}

export function noteIdFromCanvasNodeId(id: string): string | null {
  return id.startsWith(NOTE_NODE_PREFIX) ? id.slice(NOTE_NODE_PREFIX.length) : null;
}

/** Nothing written and no image: the note says nothing and does not stay on the canvas. */
export function isBlankCanvasNote(note: Pick<CanvasNote, "text" | "image">): boolean {
  return !note.text.trim() && !note.image;
}

export function canvasNotesFromSettings(settings: Record<string, unknown> | undefined): CanvasNote[] {
  const value = settings?.canvas_annotations;
  if (!Array.isArray(value)) return [];
  return value.filter((note): note is CanvasNote =>
    Boolean(note) && typeof note === "object" && typeof note.id === "string" && typeof note.text === "string"
    && [note.x, note.y, note.width, note.height].every(Number.isFinite),
  );
}

function annotationsRevisionFromSettings(settings: Record<string, unknown> | undefined): number {
  const value = settings?.canvas_annotations_revision;
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}

type PendingRemoval = {
  note: CanvasNote;
  state: "waiting" | "deleting" | "deleted";
  restore: boolean;
  timer?: ReturnType<typeof globalThis.setTimeout>;
};

/** Removals still waiting for Undo are sent when the canvas goes away, in CAS order. */
function flushRemovals(pendingRemovals: Map<string, PendingRemoval>, workflowId: string, revision: number, annotationsRevision: number) {
  const waiting = [...pendingRemovals.entries()].filter(([, entry]) => entry.state === "waiting");
  waiting.forEach(([, entry]) => { if (entry.timer) globalThis.clearTimeout(entry.timer); });
  pendingRemovals.clear();
  if (!waiting.length || !revision) return;
  ignoreAsyncResult((async () => {
    let expected = { revision, annotationsRevision };
    for (const [noteId] of waiting) {
      const result = await deleteCanvasNote(workflowId, noteId, expected.revision, expected.annotationsRevision);
      expected = { revision: result.revision, annotationsRevision: result.annotations_revision };
    }
  })().catch(() => undefined));
}

export function useCanvasNotes({ workflowId, revision, settings, onSaved, onReload, notify, disabled = false }: {
  workflowId: string;
  revision?: number;
  settings?: Record<string, unknown>;
  onSaved: (revision: number) => Promise<unknown>;
  onReload?: (revision: number) => Promise<unknown>;
  /** The canvas notice; a removal names what went and offers Undo through its action. */
  notify?: (notice: GraphEditNotice | null) => void;
  disabled?: boolean;
}) {
  const { t } = useI18n();
  const [savedList, setSavedList] = useState<CanvasNote[]>(() => canvasNotesFromSettings(settings));
  const [draft, setDraft] = useState<CanvasNote | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hiddenIds, setHiddenIds] = useState<ReadonlySet<string>>(() => new Set());
  const currentRevision = useRef(revision ?? 0);
  const currentAnnotationsRevision = useRef(annotationsRevisionFromSettings(settings));
  const savedNotes = useRef(savedList);
  const pending = useRef(false);
  const activeWorkflow = useRef(workflowId);
  const mounted = useRef(true);
  const pointerMoves = useRef(new Set<string>());
  const draftRef = useRef<CanvasNote | null>(null);
  const removals = useRef(new Map<string, PendingRemoval>());
  const cleanedWorkflow = useRef<string | null>(null);
  const [keyboardPositions, setKeyboardPositions] = useState<Record<string, { x: number; y: number }>>({});

  useEffect(() => { draftRef.current = draft; }, [draft]);

  useEffect(() => {
    mounted.current = true;
    const pendingRemovals = removals.current;
    const workflow = activeWorkflow;
    const revisions = { revision: currentRevision, annotations: currentAnnotationsRevision };
    return () => {
      mounted.current = false;
      flushRemovals(pendingRemovals, workflow.current, revisions.revision.current, revisions.annotations.current);
    };
  }, []);

  useEffect(() => {
    if (activeWorkflow.current !== workflowId) {
      flushRemovals(removals.current, activeWorkflow.current, currentRevision.current, currentAnnotationsRevision.current);
      activeWorkflow.current = workflowId;
      currentRevision.current = 0;
      currentAnnotationsRevision.current = 0;
      setKeyboardPositions({});
      setHiddenIds(new Set());
      pointerMoves.current.clear();
      setDraft(null);
      setError("");
    }
    const incomingAnnotationsRevision = annotationsRevisionFromSettings(settings);
    if ((revision ?? 0) >= currentRevision.current && incomingAnnotationsRevision >= currentAnnotationsRevision.current && !pending.current) {
      const incoming = canvasNotesFromSettings(settings);
      // A background refresh must not bless an open, older draft with a new CAS token.
      if (draft && (incomingAnnotationsRevision !== currentAnnotationsRevision.current || JSON.stringify(incoming) !== JSON.stringify(savedNotes.current))) return;
      currentRevision.current = revision ?? 0;
      currentAnnotationsRevision.current = incomingAnnotationsRevision;
      if (JSON.stringify(incoming) !== JSON.stringify(savedNotes.current)) {
        savedNotes.current = incoming;
        setSavedList(incoming);
      }
    }
  }, [workflowId, revision, settings, draft]);

  const mutate = useCallback(async (action: (revision: number, annotationsRevision: number) => Promise<CanvasNotesResult>) => {
    if (pending.current || disabled) return false;
    if (!currentRevision.current) {
      setError(t("react.workflows.notes_reload", "Reload the workflow before saving a note."));
      return false;
    }
    pending.current = true;
    setBusy(true);
    setError("");
    const targetWorkflow = workflowId;
    try {
      const result = await action(currentRevision.current, currentAnnotationsRevision.current);
      if (!mounted.current || activeWorkflow.current !== targetWorkflow) return false;
      const before = savedNotes.current;
      currentRevision.current = result.revision;
      currentAnnotationsRevision.current = result.annotations_revision;
      savedNotes.current = result.annotations;
      setSavedList(result.annotations);
      // A new sheet still being written is not on the server yet; another note's save must not close it.
      setDraft((current) => {
        if (!current) return null;
        const saved = result.annotations.find((note) => note.id === current.id);
        if (saved) return saved;
        return before.some((note) => note.id === current.id) ? null : current;
      });
      await onSaved(result.revision);
      return true;
    } catch (cause) {
      if (!mounted.current || activeWorkflow.current !== targetWorkflow) return false;
      setKeyboardPositions({});
      setSavedList(savedNotes.current);
      setError(isWorkflowRevisionConflict(cause) || isCanvasNotesRevisionConflict(cause)
        ? t("react.workflows.notes_conflict", "The workflow changed. Keep your note text, reload the workflow, then try again.")
        : cause instanceof Error ? cause.message : t("react.workflows.notes_save_failed", "The note was not saved."));
      return false;
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }, [disabled, onSaved, t, workflowId]);

  /** Waits for the one in-flight write so a queued delete or restore keeps the CAS order. */
  const whenIdle = useCallback(async () => {
    for (let attempt = 0; pending.current && attempt < 100; attempt += 1) {
      await new Promise((resolve) => globalThis.setTimeout(resolve, 100));
    }
  }, []);

  const save = useCallback((note: CanvasNote) => mutate((value, annotationsRevision) => saveCanvasNote(workflowId, note, value, annotationsRevision)), [mutate, workflowId]);
  const reload = useCallback(async () => {
    setKeyboardPositions({});
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    try {
      const result = await fetchCanvasNotes(workflowId);
      if (activeWorkflow.current !== workflowId) return;
      currentRevision.current = result.revision;
      currentAnnotationsRevision.current = result.annotations_revision;
      savedNotes.current = result.annotations;
      setSavedList(result.annotations);
      setDraft((current) => {
        if (!current) return null;
        const saved = result.annotations.find((note) => note.id === current.id);
        return saved ? { ...saved, text: current.text } : current;
      });
      setError("");
      await onReload?.(result.revision);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("react.workflows.notes_reload_failed", "The notes could not be reloaded."));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }, [onReload, t, workflowId]);
  const remove = useCallback((noteId: string) => mutate((value, annotationsRevision) => deleteCanvasNote(workflowId, noteId, value, annotationsRevision)), [mutate, workflowId]);
  const uploadImage = useCallback((noteId: string, file: File) => mutate((value, annotationsRevision) => uploadCanvasNoteImage(workflowId, noteId, file, value, annotationsRevision)), [mutate, workflowId]);
  const clearImage = useCallback((noteId: string) => mutate((value, annotationsRevision) => clearCanvasNoteImage(workflowId, noteId, value, annotationsRevision)), [mutate, workflowId]);
  const edit = useCallback((note: CanvasNote) => {
    setError("");
    setDraft({ ...note });
    setSelectedId(`${NOTE_NODE_PREFIX}${note.id}`);
  }, []);
  /** A new sheet at a flow position (its top-left), open for typing; it is saved once it says something. */
  const add = useCallback((position: { x: number; y: number }) => {
    edit({ id: `note_${crypto.randomUUID()}`, text: "", x: snapToCanvasGrid(position.x), y: snapToCanvasGrid(position.y), ...NOTE_DEFAULT_SIZE });
  }, [edit]);

  const unhide = useCallback((noteIds: readonly string[]) => {
    setHiddenIds((current) => {
      if (!noteIds.some((id) => current.has(id))) return current;
      const next = new Set(current);
      noteIds.forEach((id) => next.delete(id));
      return next;
    });
  }, []);

  const commitRemoval = useCallback(async (noteId: string) => {
    const entry = removals.current.get(noteId);
    if (entry?.state !== "waiting") return;
    entry.timer = undefined;
    entry.state = "deleting";
    await whenIdle();
    const deleted = await mutate((value, annotationsRevision) => deleteCanvasNote(workflowId, noteId, value, annotationsRevision));
    unhide([noteId]);
    if (!deleted) { removals.current.delete(noteId); return; }
    entry.state = "deleted";
    if (entry.restore) {
      removals.current.delete(noteId);
      await whenIdle();
      await save(entry.note);
    }
  }, [mutate, save, unhide, whenIdle, workflowId]);

  const undoRemoval = useCallback((noteIds: readonly string[]) => {
    notify?.(null);
    for (const noteId of noteIds) {
      const entry = removals.current.get(noteId);
      if (!entry) continue;
      if (entry.state === "waiting") {
        if (entry.timer) globalThis.clearTimeout(entry.timer);
        removals.current.delete(noteId);
        unhide([noteId]);
      } else if (entry.state === "deleting") {
        entry.restore = true;
      } else {
        // The server already forgot it: put the text and the sheet back (an image cannot come back).
        removals.current.delete(noteId);
        ignoreAsyncResult(whenIdle().then(() => save(entry.note)));
      }
    }
  }, [notify, save, unhide, whenIdle]);

  /**
   * Delete acts at once: the notes leave the canvas, the notice names what went and
   * offers Undo, and the server delete follows when the Undo window closes.
   */
  const removeWithUndo = useCallback((ids: string | readonly string[], reason: "deleted" | "emptied" = "deleted") => {
    const noteIds = typeof ids === "string" ? [ids] : [...ids];
    setDraft((current) => current && noteIds.includes(current.id) ? null : current);
    const removable = noteIds.filter((id) => savedNotes.current.some((note) => note.id === id) && !removals.current.has(id));
    if (!removable.length || disabled) return;
    for (const noteId of removable) {
      const note = savedNotes.current.find((candidate) => candidate.id === noteId);
      if (!note) continue;
      const entry: PendingRemoval = { note, state: "waiting", restore: false };
      entry.timer = globalThis.setTimeout(() => ignoreAsyncResult(commitRemoval(noteId)), NOTE_UNDO_WINDOW_MS);
      removals.current.set(noteId, entry);
    }
    setHiddenIds((current) => new Set([...current, ...removable]));
    setSelectedId((current) => current && removable.some((id) => current === `${NOTE_NODE_PREFIX}${id}`) ? null : current);
    let label = t("react.workflows.note_deleted", "Note deleted.");
    if (reason === "emptied") label = t("react.workflows.note_removed_empty", "Empty note removed.");
    else if (removable.length > 1) label = t("react.workflows.notes_deleted_count", "{count} notes deleted.", { count: removable.length });
    notify?.({ label, tone: "success", action: { label: t("react.workflows.undo", "Undo"), onAction: () => undoRemoval(removable) } });
  }, [commitRemoval, disabled, notify, t, undoRemoval]);

  /**
   * Leaves writing: Done, Escape, Cmd/Ctrl+Enter or a click elsewhere keep the text.
   * Left with nothing in it the note goes — quietly if nothing was ever there,
   * with Undo when its text was erased.
   */
  const finish = useCallback(async (text?: string) => {
    const current = draftRef.current;
    if (!current) return true;
    const value = text ?? current.text;
    const saved = savedNotes.current.find((note) => note.id === current.id);
    const close = () => setDraft((open) => open?.id === current.id ? null : open);
    if (!value.trim() && !saved?.image) {
      close();
      if (saved?.text.trim()) removeWithUndo(saved.id, "emptied");
      else if (saved) ignoreAsyncResult(remove(saved.id));
      return true;
    }
    if (saved?.text === value) { close(); return true; }
    const written = await save({ ...(saved ?? current), text: value });
    if (written) close();
    return written;
  }, [remove, removeWithUndo, save]);

  /** An image needs the note on the server first, so a new or changed text is saved before the upload. */
  const attachImage = useCallback(async (file: File, text: string) => {
    const current = draftRef.current;
    if (!current) return false;
    const saved = savedNotes.current.find((note) => note.id === current.id);
    if (saved?.text !== text && !await save({ ...(saved ?? current), text })) return false;
    return uploadImage(current.id, file);
  }, [save, uploadImage]);

  const duplicate = useCallback((note: CanvasNote) => save({
    id: `note_${crypto.randomUUID()}`, text: note.text,
    x: note.x + CANVAS_GRID * 2, y: note.y + CANVAS_GRID * 2, width: note.width, height: note.height,
  }), [save]);

  // Notes left empty by an earlier session are removed when the canvas opens.
  useEffect(() => {
    if (disabled || busy || !currentRevision.current || cleanedWorkflow.current === workflowId) return;
    cleanedWorkflow.current = workflowId;
    const blanks = savedList.filter((note) => isBlankCanvasNote(note) && note.id !== draftRef.current?.id);
    if (!blanks.length) return;
    ignoreAsyncResult((async () => {
      for (const note of blanks) {
        if (!await mutate((value, annotationsRevision) => deleteCanvasNote(workflowId, note.id, value, annotationsRevision))) return;
      }
    })());
  }, [busy, disabled, mutate, revision, savedList, workflowId]);

  // Keyboard moves emit a completed position change but no pointer drag-stop.
  // Coalesce repeated arrows, then use the same revision-checked writer. One
  // queued note is saved at a time so multi-selection cannot race its own CAS.
  useEffect(() => {
    const next = Object.entries(keyboardPositions)[0];
    if (!next) return;
    if (disabled) { setKeyboardPositions({}); return; }
    if (busy) return;
    const [id, position] = next;
    const timer = globalThis.setTimeout(() => {
      const note = savedList.find((item) => item.id === id);
      const write = note ? save({ ...note, ...position }) : Promise.resolve(false);
      void write.then(() => {
        if (!mounted.current) return;
        setKeyboardPositions((current) => {
          if (current[id] !== position) return current;
          const remaining = { ...current };
          delete remaining[id];
          return remaining;
        });
      });
    }, 250);
    return () => globalThis.clearTimeout(timer);
  }, [busy, disabled, keyboardPositions, savedList, save]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const annotationChanges = changes.filter((change) => "id" in change && change.id.startsWith(NOTE_NODE_PREFIX));
    if (!annotationChanges.length) return;
    for (const change of annotationChanges) {
      if (change.type === "select") setSelectedId((current) => {
        if (change.selected) return change.id;
        return current === change.id ? null : current;
      });
      if (disabled || change.type !== "position" || !change.position) continue;
      const id = change.id.slice(NOTE_NODE_PREFIX.length);
      if (change.dragging) {
        pointerMoves.current.add(id);
        setKeyboardPositions((current) => {
          if (!(id in current)) return current;
          const remaining = { ...current };
          delete remaining[id];
          return remaining;
        });
      } else if (change.dragging === false && !pointerMoves.current.delete(id)) {
        const position = change.position;
        setKeyboardPositions((current) => ({ ...current, [id]: position }));
      }
    }
    if (disabled) return;
    setSavedList((current) => current.map((note) => {
      let next = note;
      for (const change of annotationChanges) {
        if (!("id" in change) || change.id !== `${NOTE_NODE_PREFIX}${note.id}`) continue;
        if (change.type === "position" && change.position) next = { ...next, ...change.position };
        if (change.type === "dimensions" && change.resizing && change.dimensions) next = { ...next, ...change.dimensions };
      }
      return next;
    }));
  }, [disabled]);

  /**
   * Splits a canvas selection: the notes in it are removed here (with Undo) and
   * the remaining step and group ids are returned for the graph delete.
   */
  const removeFromSelection = useCallback((canvasIds: readonly string[]) => {
    const noteIds = canvasIds.map(noteIdFromCanvasNodeId).filter((id): id is string => Boolean(id));
    if (noteIds.length) removeWithUndo(noteIds);
    return canvasIds.filter((id) => !noteIdFromCanvasNodeId(id));
  }, [removeWithUndo]);

  const notes = useMemo(() => hiddenIds.size ? savedList.filter((note) => !hiddenIds.has(note.id)) : savedList, [hiddenIds, savedList]);
  const nodeActions = useMemo(() => ({ edit, finish, removeWithUndo, duplicate, attachImage, clearImage, save }), [attachImage, clearImage, duplicate, edit, finish, removeWithUndo, save]);

  return {
    notes, draft, setDraft, busy, disabled, error, selectedId, save, reload, remove, removeWithUndo, removeFromSelection,
    uploadImage, attachImage, clearImage, edit, add, finish, duplicate, onNodesChange, nodeActions,
  };
}

export type CanvasNotesController = ReturnType<typeof useCanvasNotes>;
export type CanvasNoteActions = CanvasNotesController["nodeActions"];

/** Notes lie beneath every step and connection; the one being written is never dragged. */
export function canvasNoteNodes(
  { notes, draft, selectedId, busy, disabled, nodeActions }: Pick<CanvasNotesController, "notes" | "draft" | "selectedId" | "busy" | "disabled" | "nodeActions">,
  workflowId: string,
): Node<CanvasNoteData>[] {
  const all = draft && !notes.some((note) => note.id === draft.id) ? [...notes, draft] : notes;
  const locked = busy || disabled;
  return all.map((note) => {
    const id = `${NOTE_NODE_PREFIX}${note.id}`;
    const editing = draft?.id === note.id;
    return {
      id, type: "canvasNote", zIndex: -1, position: { x: note.x, y: note.y },
      width: note.width, height: note.height, selected: selectedId === id || editing,
      measured: { width: note.width, height: note.height },
      connectable: false, deletable: false, draggable: !locked && !editing,
      data: { note, workflowId, busy: locked, editing, actions: nodeActions },
    };
  });
}
