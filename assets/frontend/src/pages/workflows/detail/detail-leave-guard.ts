import { useCallback, useEffect, useRef } from "react";
import { useBlocker, type BlockerFunction } from "react-router";

import type { ConfirmDialogState } from "@/components/shared/use-confirm-dialog";
import { useI18n } from "@/lib/i18n/useI18n";

/**
 * Block both browser history and SPA route changes before the builder unmounts.
 *
 * The browser's own dialog guards a reload or a closing tab, and only while
 * something is not saved on the server (unapplied structure, unsaved forms, an
 * Apply in flight). It is a warning, not a save: the edit session is already in
 * the browser's database, and nothing is written or removed on unload.
 * In-app navigation asks "Stay" or "Leave and discard"; the session ends only
 * when the page actually leaves (`onDiscard` marks it, the session hook ends it).
 */
export function useWorkflowLeaveGuard(
  hasUnsavedChanges: boolean,
  { onNavigate, onDiscard }: { onNavigate?: (pathname: string) => void; onDiscard?: () => void } = {},
): ConfirmDialogState {
  const { t } = useI18n();
  const onNavigateRef = useRef(onNavigate);
  useEffect(() => { onNavigateRef.current = onNavigate; });
  const blocker = useBlocker(useCallback<BlockerFunction>(
    ({ currentLocation, nextLocation }) => {
      onNavigateRef.current?.(nextLocation.pathname);
      return hasUnsavedChanges && currentLocation.pathname !== nextLocation.pathname;
    },
    [hasUnsavedChanges],
  ));
  useEffect(() => {
    if (!hasUnsavedChanges) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    globalThis.addEventListener("beforeunload", warn);
    return () => globalThis.removeEventListener("beforeunload", warn);
  }, [hasUnsavedChanges]);
  return {
    open: blocker.state === "blocked",
    title: t("react.workflows.leave_builder_title", "Leave the builder?"),
    body: t("react.workflows.leave_builder_unsaved", "Unapplied structure and unsaved parameters will be lost."),
    cancelLabel: t("react.workflows.leave_builder_stay", "Stay"),
    confirmLabel: t("react.workflows.leave_and_discard", "Leave and discard"),
    onCancel: () => {
      if (blocker.state === "blocked") blocker.reset();
    },
    onConfirm: () => {
      if (blocker.state !== "blocked") return;
      onDiscard?.();
      blocker.proceed();
    },
  };
}
