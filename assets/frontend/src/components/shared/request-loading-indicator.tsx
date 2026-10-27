import { type SyntheticEvent, useLayoutEffect, useRef, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";

import { BrandLoader } from "@/components/shared/brand-loader";
import { completeRequestLoadingFocus, registerRequestLoadingOpener } from "@/components/shared/request-loading-focus";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { hasPageWait, hasPendingRequests, subscribeToRequestActivity } from "@/lib/http/request-activity";
import { useI18n } from "@/lib/i18n/useI18n";

import "@/components/shared/request-loading-indicator.css";

function DelayedRequestLoadingIndicator() {
  const delayed = useLoaderDelay();
  // A page loader already shows this wait; interaction stays blocked either way.
  const pageOwnsWait = useSyncExternalStore(subscribeToRequestActivity, hasPageWait, () => false);
  const visible = delayed && !pageOwnsWait;
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const label = t("react.shared.request_loading", "Loading requests");

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const opener = document.activeElement;
    const unregisterOpener = registerRequestLoadingOpener(dialog, opener instanceof HTMLElement ? opener : null);
    // The native top layer also makes already-open portal dialogs inert.
    dialog.showModal();
    dialog.focus({ preventScroll: true });
    return () => {
      if (dialog.open) dialog.close();
      unregisterOpener();
      const activeModal = Array.from(document.querySelectorAll<HTMLElement>(
        'dialog[open][aria-modal="true"], [role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]',
      )).findLast((node) => node !== dialog && !node.closest('[hidden], [aria-hidden="true"]'));
      if (activeModal && (!(opener instanceof HTMLElement) || !activeModal.contains(opener))) {
        const firstControl = activeModal.querySelector<HTMLElement>(
          'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
        );
        (firstControl ?? activeModal).focus({ preventScroll: true });
      } else if (opener instanceof HTMLElement && opener.isConnected) {
        opener.focus({ preventScroll: true });
      }
      completeRequestLoadingFocus(activeModal);
    };
  }, []);

  const blockInteraction = (event: SyntheticEvent) => {
    event.preventDefault();
    event.stopPropagation();
  };

  return createPortal(
    <dialog
      ref={dialogRef}
      className={`fs-request-loading${visible ? " fs-request-loading--visible" : ""}`}
      aria-label={label}
      aria-modal="true"
      aria-busy="true"
      tabIndex={-1}
      onCancel={blockInteraction}
      onKeyDownCapture={blockInteraction}
      onClickCapture={blockInteraction}
    >
      {visible ? (
        <div role="status" aria-label={label}>
          <BrandLoader />
          <span className="fs-sr-only">{label}</span>
        </div>
      ) : null}
    </dialog>,
    document.body,
  );
}

/** Block interaction immediately; paint the centered loader only for slower requests. */
export function RequestLoadingIndicator() {
  const pending = useSyncExternalStore(subscribeToRequestActivity, hasPendingRequests, () => false);
  // Unmounting the delayed child gives each new request episode a fresh delay.
  return pending ? <DelayedRequestLoadingIndicator /> : null;
}
