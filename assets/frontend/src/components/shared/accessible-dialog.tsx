import { useEffect, useRef, type PropsWithChildren, type RefObject } from "react";
import { createPortal } from "react-dom";

import { resolveRequestLoadingOpener } from "@/components/shared/request-loading-focus";

const FOCUSABLE_SELECTOR = [
  "button:not(:disabled)",
  "input:not(:disabled)",
  "select:not(:disabled)",
  "textarea:not(:disabled)",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

type AccessibleName =
  | { ariaLabel: string; ariaLabelledBy?: never }
  | { ariaLabel?: never; ariaLabelledBy: string };

type AccessibleDialogProps = PropsWithChildren<
  AccessibleName & {
    ariaDescribedBy?: string;
    initialFocusRef: RefObject<HTMLElement | null>;
    onClose: () => void;
    panelClassName: string;
    role?: "alertdialog" | "dialog";
  }
>;

function deepestActiveElement(): HTMLElement | null {
  let activeElement: Element | null = document.activeElement;
  while (activeElement?.shadowRoot?.activeElement) {
    activeElement = activeElement.shadowRoot.activeElement;
  }
  return resolveRequestLoadingOpener(activeElement instanceof HTMLElement ? activeElement : null);
}

export function AccessibleDialog({
  ariaDescribedBy,
  ariaLabel,
  ariaLabelledBy,
  children,
  initialFocusRef,
  onClose,
  panelClassName,
  role = "dialog",
}: Readonly<AccessibleDialogProps>) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    openerRef.current = deepestActiveElement();
    const appRoot = document.getElementById("root");
    const previousAriaHidden = appRoot?.getAttribute("aria-hidden") ?? null;
    const previousInert = appRoot?.inert ?? false;
    const previousOverflow = document.body.style.overflow;
    if (appRoot) {
      appRoot.setAttribute("aria-hidden", "true");
      appRoot.inert = true;
    }
    document.body.style.overflow = "hidden";

    queueMicrotask(() => {
      (initialFocusRef.current ?? dialogRef.current)?.focus();
    });

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) {
        return;
      }
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      );
      if (!focusable.length) {
        event.preventDefault();
        dialogRef.current.focus();
        return;
      }
      const currentIndex = focusable.indexOf(document.activeElement as HTMLElement);
      const nextIndex = event.shiftKey
        ? (currentIndex - 1 + focusable.length) % focusable.length
        : (currentIndex + 1) % focusable.length;
      event.preventDefault();
      focusable[nextIndex]?.focus();
    };
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      if (appRoot) {
        if (previousAriaHidden === null) {
          appRoot.removeAttribute("aria-hidden");
        } else {
          appRoot.setAttribute("aria-hidden", previousAriaHidden);
        }
        appRoot.inert = previousInert;
      }
      document.body.style.overflow = previousOverflow;
      const opener = openerRef.current;
      queueMicrotask(() => {
        if (opener?.isConnected) {
          opener.focus();
        }
      });
    };
  }, [initialFocusRef]);

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 px-4">
      <div
        aria-describedby={ariaDescribedBy}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        aria-modal="true"
        className={panelClassName}
        ref={dialogRef}
        role={role}
        tabIndex={-1}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
