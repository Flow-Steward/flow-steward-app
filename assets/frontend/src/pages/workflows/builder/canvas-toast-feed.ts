import { useEffect } from "react";
import type { GraphEditNotice } from "./canvas-core";
import type { GraphIntentToken } from "./canvas-history";

/** A toast leaves after 2.6s, 6s when it offers Undo; an error and a running save stay (Components2 › Toasts). */
export const CANVAS_TOAST_MS = 2600;
export const CANVAS_TOAST_ACTION_MS = 6000;

export function canvasToastDuration(notice: GraphEditNotice): number | null {
  if (notice.tone === "error" || notice.tone === "saving") return null;
  return notice.action ? CANVAS_TOAST_ACTION_MS : CANVAS_TOAST_MS;
}

type Listener = (notice: GraphEditNotice) => void;
const listeners = new Set<Listener>();

/**
 * Say something on the open canvas from outside it (the step or group editor):
 * "Reject order deleted." with Undo. Returns false when no canvas is listening.
 */
export function announceCanvasToast(notice: GraphEditNotice): boolean {
  listeners.forEach((listener) => listener(notice));
  return listeners.size > 0;
}

/** The mounted canvas shows what the editors announce. */
export function useCanvasToastFeed(show: Listener) {
  useEffect(() => {
    listeners.add(show);
    return () => { listeners.delete(show); };
  }, [show]);
}

type UndoOwner = {
  lastStagedGraphIntent: () => GraphIntentToken | null;
  undoGraphIntentIfLatest: (token: GraphIntentToken) => boolean;
};

/**
 * Watch one change: call before it, then ask for the toast's Undo after it.
 * Undo is offered only when the change staged a new graph intent, and reverses
 * exactly that intent while it is still the latest (the change-set history).
 */
export function watchGraphIntent(owner: Partial<UndoOwner> | null | undefined) {
  // A controller without history (a test double, an old owner) never offers Undo.
  const read = () => (typeof owner?.lastStagedGraphIntent === "function" ? owner.lastStagedGraphIntent() : null);
  const before = read();
  return (label: string): GraphEditNotice["action"] | undefined => {
    const token = read();
    if (!owner?.undoGraphIntentIfLatest || !token || token === before) return undefined;
    const undo = owner.undoGraphIntentIfLatest;
    return { label, onAction: () => { undo(token); } };
  };
}
