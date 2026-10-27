import { useEffect, useLayoutEffect, useRef } from "react";

import { historyKey, isTextEntryTarget, modalDialogOpen } from "./canvas-keyboard";

/**
 * Undo and Redo anywhere on the builder page (Canvas › Undo): after a drawer's
 * button, on the page body, on a checkbox. A text field, a code editor, an IME
 * composition and an open modal dialog keep their own keys. The canvas runs the
 * same keys first while it has the focus, and a key it used is not run again.
 */
export function useBuilderHistoryKeys(runHistory: ((redo: boolean) => boolean) | undefined): void {
  const runHistoryRef = useRef(runHistory);
  useLayoutEffect(() => { runHistoryRef.current = runHistory; });
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const key = historyKey(event);
      const run = runHistoryRef.current;
      if (!key || !run || event.defaultPrevented || event.isComposing) return;
      if (isTextEntryTarget(event.target) || isTextEntryTarget(document.activeElement) || modalDialogOpen()) return;
      if (run(key === "redo")) event.preventDefault();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);
}
