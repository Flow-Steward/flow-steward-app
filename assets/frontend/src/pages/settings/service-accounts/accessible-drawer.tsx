import { useEffect, useRef, type PropsWithChildren } from "react";
import { createPortal } from "react-dom";

const FOCUSABLE_SELECTOR = [
  "button:not(:disabled)",
  "input:not(:disabled)",
  "select:not(:disabled)",
  "textarea:not(:disabled)",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

type AccessibleDrawerProps = PropsWithChildren<{
  active?: boolean;
  ariaLabel: string;
  closeLabel: string;
  onClose: () => void;
  panelClassName: string;
  /** "wide" for the identity drawer, which holds the full account form. */
  size?: "default" | "wide";
  /**
   * Stays visible but hands the keyboard over: a confirmation dialog open on
   * top owns Tab and Escape. Without this the drawer's document-level trap
   * pulled focus back out of the dialog and Escape closed both surfaces.
   */
  suspended?: boolean;
}>;

export function AccessibleDrawer({
  active = true,
  ariaLabel,
  children,
  closeLabel,
  onClose,
  panelClassName,
  size = "default",
  suspended = false,
}: Readonly<AccessibleDrawerProps>) {
  const drawerRef = useRef<HTMLDialogElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const hasFocusedRef = useRef(false);

  useEffect(() => {
    openerRef.current = document.activeElement as HTMLElement | null;
    const appRoot = document.getElementById("root");
    const previousAriaHidden = appRoot?.getAttribute("aria-hidden") ?? null;
    const previousInert = appRoot?.inert ?? false;
    const previousOverflow = document.body.style.overflow;
    if (appRoot) {
      appRoot.setAttribute("aria-hidden", "true");
      appRoot.inert = true;
    }
    document.body.style.overflow = "hidden";

    return () => {
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
  }, []);

  useEffect(() => {
    if (!active || suspended) {
      return;
    }
    if (!hasFocusedRef.current) {
      hasFocusedRef.current = true;
      queueMicrotask(() => {
        const first = drawerRef.current?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
        (first ?? drawerRef.current)?.focus();
      });
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !drawerRef.current) {
        return;
      }
      const focusable = Array.from(
        drawerRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      );
      if (!focusable.length) {
        event.preventDefault();
        drawerRef.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable.at(-1);
      const current = document.activeElement;
      if (!drawerRef.current.contains(current)) {
        event.preventDefault();
        (event.shiftKey ? last : first)?.focus();
        return;
      }
      const currentIndex = focusable.indexOf(current as HTMLElement);
      const nextIndex = event.shiftKey
        ? (currentIndex - 1 + focusable.length) % focusable.length
        : (currentIndex + 1) % focusable.length;
      event.preventDefault();
      focusable[nextIndex]?.focus();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [active, onClose, suspended]);

  return createPortal(
    <div
      aria-hidden={active && !suspended ? undefined : true}
      className="fs-settings-drawer-overlay"
      inert={active && !suspended ? undefined : true}
    >
      <button
        aria-label={closeLabel}
        className="absolute inset-0 h-full w-full cursor-default"
        onClick={onClose}
        tabIndex={-1}
        type="button"
      />
      <dialog
        aria-label={ariaLabel}
        aria-modal={active ? true : undefined}
        className={panelClassName}
        data-size={size}
        open={active}
        ref={drawerRef}
        tabIndex={-1}
      >
        {children}
      </dialog>
    </div>,
    document.body,
  );
}
