import { useCallback, useEffect, useRef, useState } from "react";

/**
 * A promise-based replacement for `globalThis.confirm`.
 *
 * AUDIT-041 / AUDIT-028: the app used native `confirm()` for destructive
 * actions. A native dialog blocks the page, cannot be driven by browser
 * automation, and dismissing it resets the automation session — which is why
 * the reviewer could not complete either the registry or the workflow authoring
 * scenarios.
 *
 * The hook lives apart from the component so controllers written as plain `.ts`
 * can await a decision without rendering anything themselves:
 *
 *     const { requestConfirmation, confirmDialog } = useConfirmDialog();
 *     if (!(await requestConfirmation({ title: "…", confirmLabel: "Delete" }))) return;
 *
 * and the surface renders `<ConfirmDialog {...confirmDialog} />`.
 *
 * A pending request resolves `false` if a second one arrives, so a stray double
 * click can never leave a promise hanging forever.
 */

export type ConfirmDialogRequest = {
  body?: string;
  cancelLabel?: string;
  confirmLabel?: string;
  destructive?: boolean;
  details?: string[];
  returnFocus?: HTMLElement | null;
  title: string;
};

type ConfirmDialogPresentationRequest = Omit<ConfirmDialogRequest, "returnFocus">;

export type ConfirmDialogState = ConfirmDialogPresentationRequest & {
  onCancel: () => void;
  onConfirm: () => void;
  open: boolean;
  /** Direct consumers restore via the modal; request hooks may own restoration. */
  restoreFocus?: boolean;
};

export function useConfirmDialog() {
  const [request, setRequest] = useState<ConfirmDialogPresentationRequest | null>(null);
  const resolverRef = useRef<((confirmed: boolean) => void) | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const returnFocusFrameRef = useRef<number | null>(null);
  const mountedRef = useRef(false);
  const cancelReturnFocus = useCallback(() => {
    if (returnFocusFrameRef.current !== null) cancelAnimationFrame(returnFocusFrameRef.current);
    returnFocusFrameRef.current = null;
  }, []);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cancelReturnFocus();
    };
  }, [cancelReturnFocus]);

  const settle = useCallback((confirmed: boolean) => {
    const resolve = resolverRef.current;
    if (!resolve) return;
    cancelReturnFocus();
    const trigger = triggerRef.current;
    resolverRef.current = null;
    triggerRef.current = null;
    setRequest(null);
    resolve(confirmed);
    // Awaiting callers re-enable their trigger in finally. Let that render
    // commit before focusing; a microtask still sees the disabled control.
    returnFocusFrameRef.current = requestAnimationFrame(() => {
      returnFocusFrameRef.current = null;
      if (mountedRef.current && trigger?.isConnected) {
        trigger.focus();
      }
    });
  }, [cancelReturnFocus]);

  const requestConfirmation = useCallback(
    (next: ConfirmDialogRequest) => {
      cancelReturnFocus();
      // Never leave an earlier promise unresolved.
      resolverRef.current?.(false);
      resolverRef.current = null;
      const { returnFocus, ...presentationRequest } = next;
      triggerRef.current = returnFocus ?? (
        document.activeElement instanceof HTMLElement ? document.activeElement : null
      );
      setRequest(presentationRequest);
      return new Promise<boolean>((resolve) => {
        resolverRef.current = resolve;
      });
    },
    [cancelReturnFocus],
  );

  const confirmDialog: ConfirmDialogState = {
    ...(request || { title: "" }),
    onCancel: () => settle(false),
    onConfirm: () => settle(true),
    open: Boolean(request),
    restoreFocus: false,
  };

  return { confirmDialog, requestConfirmation };
}
