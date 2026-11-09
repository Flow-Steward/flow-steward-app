import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useRef, type KeyboardEvent, type ReactNode } from "react";

import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { ButtonBusyContent } from "@/components/ui/button";

import "@/components/shared/inline-confirm.css";

/**
 * The canon's Inline confirm (03_components/overlays-feedback/inline-confirm.md),
 * in the two homes it has inside a table: a row replaced in place by a
 * full-width strip, and the bulk action bar with its buttons replaced. Both
 * embed the surface in their container, so it carries no card of its own.
 *
 * - The variant follows the consequence: `danger` when something is removed,
 *   `primary` when the action is reversible and destroys nothing.
 * - No comes first and takes focus on open; the confirming action is last and
 *   named verb plus object, never "Yes".
 * - Escape cancels, unless the request is in flight, and it stops here, so the
 *   bulk bar underneath does not also clear its selection.
 * - While the request runs No disables and the confirming action is busy: it
 *   ignores presses and, after the loader delay, shows the round Loader with
 *   the progress label at one width; a failure restores them with the reason
 *   beside them.
 *
 * Outcome announcements and focus after the decision belong to the page: the
 * row or the selection this surface stood for is gone by then.
 */
export function InlineConfirm({
  question,
  confirmLabel,
  cancelLabel,
  children,
  pendingLabel,
  tone,
  pending = false,
  error = "",
  onCancel,
  onConfirm,
}: Readonly<{
  question: string;
  confirmLabel: string;
  /** The first action's label; a confirmation beside a form control reads Cancel. */
  cancelLabel?: string;
  /** What the decision changes, under the question: a consequence sentence or the affected objects. */
  children?: ReactNode;
  pendingLabel: string;
  tone: "danger" | "primary";
  pending?: boolean;
  error?: string;
  onCancel: () => void;
  onConfirm: () => void;
}>) {
  const { t } = useI18n();
  const noRef = useRef<HTMLButtonElement>(null);
  const busyShown = useLoaderDelay(pending);

  useEffect(() => {
    noRef.current?.focus();
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    if (!pending) onCancel();
  }

  return (
    // The group only scopes Escape to this surface; the buttons stay the focus targets.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div aria-busy={pending} aria-label={question} className="fs-inline-confirm" onKeyDown={handleKeyDown} role="group">
      <span className="fs-inline-confirm-question">{question}</span>
      {children ? <div className="fs-inline-confirm-detail">{children}</div> : null}
      <div className="fs-inline-confirm-actions">
        <button className="fs-inline-confirm-button" disabled={pending} onClick={onCancel} ref={noRef} type="button">
          {cancelLabel ?? t("settings.common.no", "No")} </button>
        <button aria-busy={pending || undefined} className={`fs-inline-confirm-button ${tone}`} onClick={() => { if (!pending) onConfirm(); }} type="button">
          <ButtonBusyContent busyLabel={pendingLabel} shown={busyShown}>{confirmLabel}</ButtonBusyContent>
        </button>
      </div>
      {error ? (
        <p className="fs-inline-confirm-error" role="alert">
          {error}
        </p>
      ) : null}
      <span aria-live="polite" className="fs-sr-only" role="status">
        {pending ? pendingLabel : ""}
      </span>
    </div>
  );
}
