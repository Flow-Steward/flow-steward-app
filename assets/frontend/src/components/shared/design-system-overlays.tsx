import { useI18n } from "@/lib/i18n/useI18n";
import { MoreVertical, X } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type PropsWithChildren, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { focusAfterRequestLoading, resolveRequestLoadingOpener } from "@/components/shared/request-loading-focus";

/* Drawer — non-blocking contextual detail, per
   03_components/overlays-feedback/drawer.md. Deliberately not `aria-modal` and
   deliberately without a scrim: the page beneath stays interactive. Reach for
   DsDialog instead when the user must respond before continuing. */
export type DrawerGeometry = Readonly<{ top: number; right: number; width: number; height: number }>;

export function DsDrawer({
  open,
  onClose,
  title,
  subtitle,
  children,
  closeLabel: suppliedCloseLabel,
  testId,
  className,
  style,
  titleId: suppliedTitleId,
  dataAttributes,
  focusContext,
  returnFocusOwner,
  focusFallback,
  onGeometryChange,
}: PropsWithChildren<{
  open: boolean;
  onClose: () => void;
  title: string;
  /** One identifying line under the title, such as the ID of the object the drawer edits. */
  subtitle?: string;
  closeLabel?: string;
  testId?: string;
  /** Consumer geometry stays outside the shared default drawer contract. */
  className?: string;
  style?: CSSProperties;
  titleId?: string;
  dataAttributes?: Record<`data-${string}`, string | number | undefined>;
  /** Opt in to contextual focus ownership without remounting a live editor. */
  focusContext?: string;
  /** Stable contextual owner across close-only state normalization; defaults to focusContext.
   * A different owner retires return focus, including a simultaneous close. */
  returnFocusOwner?: string;
  focusFallback?: () => HTMLElement | null;
  /** Actual owner geometry for contextual nested panels; absent for standalone drawers. */
  onGeometryChange?: (geometry: DrawerGeometry | null) => void;
}>) {
  const { t } = useI18n();
  const closeLabel = suppliedCloseLabel ?? t("registry.common.close", "Close");
  const generatedTitleId = useId();
  const titleId = suppliedTitleId ?? generatedTitleId;
  const panelRef = useRef<HTMLDialogElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const returnOwner = returnFocusOwner ?? focusContext;
  const optionsRef = useRef({ onClose, focusContext, returnOwner, focusFallback });
  const openReturnOwnerRef = useRef(returnOwner);
  const previousContextRef = useRef(focusContext);
  const returnFocusEpochRef = useRef(0);
  const returnFocusMountedRef = useRef(false);
  const cancelReturnFocusRef = useRef<(() => void) | null>(null);
  useLayoutEffect(() => {
    optionsRef.current = { onClose, focusContext, returnOwner, focusFallback };
  }, [onClose, focusContext, returnOwner, focusFallback]);
  useLayoutEffect(() => {
    returnFocusMountedRef.current = true;
    if (open) {
      if (openReturnOwnerRef.current !== returnOwner) triggerRef.current = null;
      openReturnOwnerRef.current = returnOwner;
    }
    returnFocusEpochRef.current += 1;
    cancelReturnFocusRef.current?.();
    cancelReturnFocusRef.current = null;
    return () => {
      returnFocusMountedRef.current = false;
      returnFocusEpochRef.current += 1;
      cancelReturnFocusRef.current?.();
      cancelReturnFocusRef.current = null;
    };
  }, [focusContext, open, returnOwner]);
  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    const contextual = optionsRef.current.focusContext !== undefined || optionsRef.current.returnOwner !== undefined || optionsRef.current.focusFallback !== undefined;
    triggerRef.current = contextual ? deepestActiveElement() : document.activeElement as HTMLElement;
    // Keep the initiating identity through transient busy/disabled rendering.
    // Close-time eligibility and context retirement still decide restoration.
    if (contextual && (triggerRef.current === document.body || panel?.contains(triggerRef.current))) triggerRef.current = null;
    previousContextRef.current = optionsRef.current.focusContext;
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      // Contextual children may already own initial field/navigation focus.
      const focused = document.activeElement;
      if (!contextual || focused === document.body || focused === triggerRef.current) {
        panel?.querySelector<HTMLElement>(".fs-drawer-close")?.focus();
      }
    });
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (contextual) {
        if (event.defaultPrevented) return;
        // Popups handle Escape at their own target/document listeners first.
        // A confirmation can be open before it receives focus, so also yield
        // to the actual modal while the canonical async guard is pending.
        if (document.querySelector('dialog[open][aria-modal="true"], [role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]')) return;
        const owner = event.target instanceof Element
          ? event.target.closest('dialog, [role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]')
          : null;
        if (owner && owner !== panel) return;
      }
      optionsRef.current.onClose();
    };
    if (contextual) window.addEventListener("keydown", onKeyDown);
    else document.addEventListener("keydown", onKeyDown);
    return () => {
      active = false;
      if (contextual) window.removeEventListener("keydown", onKeyDown);
      else document.removeEventListener("keydown", onKeyDown);
      if (!contextual) {
        triggerRef.current?.focus();
        return;
      }
      const opener = triggerRef.current;
      const context = optionsRef.current.focusContext;
      // Layout has already published the closing render. Retain the owner of
      // the open panel rather than borrowing a replacement owner on close.
      const owner = openReturnOwnerRef.current;
      const epoch = returnFocusEpochRef.current;
      const ownsReturn = () => returnFocusMountedRef.current
        && returnFocusEpochRef.current === epoch && optionsRef.current.focusContext === context
        && optionsRef.current.returnOwner === owner;
      queueMicrotask(() => {
        if (!ownsReturn()) return;
        const focused = deepestActiveElement();
        // A page action or child navigation that already moved focus owns it.
        if (focused && focused !== document.body && !panel?.contains(focused)) return;
        const raw = document.activeElement;
        const loader = raw instanceof HTMLElement && resolveRequestLoadingOpener(raw) !== raw ? raw : null;
        const resolveTarget = () => {
          if (canReceiveDrawerFocus(opener)) return opener;
          const fallback = optionsRef.current.focusFallback?.();
          return canReceiveDrawerFocus(fallback) ? fallback : null;
        };
        cancelReturnFocusRef.current = focusAfterRequestLoading(opener || optionsRef.current.focusFallback?.() || null, () => {}, {
          resolveTarget,
          focusOptions: { preventScroll: true },
          canFocus: (target) => {
            if (!ownsReturn() || !canReceiveDrawerFocus(target)) return false;
            const actual = document.activeElement;
            const resolved = deepestActiveElement();
            // Loader cleanup unregisters before delivery and DOM removal. Only
            // this captured, now-closed loader may still represent the old panel.
            const closingLoader = actual === loader && loader instanceof HTMLDialogElement && !loader.open;
            if (actual instanceof HTMLElement && actual !== document.body && !panel?.contains(actual)
              && resolveRequestLoadingOpener(actual) === actual && !closingLoader) return false;
            if (resolved && resolved !== document.body && !panel?.contains(resolved) && !closingLoader) return false;
            const modal = Array.from(document.querySelectorAll<HTMLElement>(
              'dialog[open][aria-modal="true"], [role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]',
            )).findLast((node) => !node.closest('[hidden], [aria-hidden="true"]'));
            return !modal || modal.contains(target);
          },
        });
      });
    };
  }, [open]);
  useEffect(() => {
    if (!open || focusContext === undefined || previousContextRef.current === focusContext) return;
    previousContextRef.current = focusContext;
    const focused = deepestActiveElement();
    // A new external control can own the new context; an editor control or
    // the unchanged old trigger cannot become a stale return target.
    triggerRef.current = focused !== triggerRef.current && !panelRef.current?.contains(focused)
      ? focused
      : null;
  }, [focusContext, open]);
  useEffect(() => {
    if (!open || !onGeometryChange) return;
    const panel = panelRef.current;
    if (!panel) return;
    let active = true;
    const publish = () => {
      if (!active || !panel.isConnected) return;
      const rect = panel.getBoundingClientRect();
      if (![rect.top, rect.right, rect.width, rect.height].every(Number.isFinite) || rect.width <= 0 || rect.height <= 0) return;
      onGeometryChange({ top: rect.top, right: globalThis.innerWidth - rect.right, width: rect.width, height: rect.height });
    };
    // ResizeObserver tracks layout size, not the entry transform. Read the
    // panel again when its own CSS motion settles (or is cancelled).
    const publishSettled = (event: Event) => { if (event.target === panel) publish(); };
    const motionEvents = ["animationend", "animationcancel", "transitionend", "transitioncancel"] as const;
    for (const name of motionEvents) panel.addEventListener(name, publishSettled);
    publish();
    const observer = globalThis.ResizeObserver ? new ResizeObserver(publish) : null;
    observer?.observe(panel);
    globalThis.addEventListener("resize", publish);
    return () => {
      active = false;
      for (const name of motionEvents) panel.removeEventListener(name, publishSettled);
      observer?.disconnect();
      globalThis.removeEventListener("resize", publish);
      onGeometryChange(null);
    };
  }, [open, onGeometryChange]);
  if (!open || globalThis.document === undefined) return null;
  return createPortal(
    <dialog {...dataAttributes} open aria-labelledby={titleId} className={["fs-drawer", className].filter(Boolean).join(" ")} data-testid={testId} ref={panelRef} style={{ height: "auto", ...style }}>
      <div className="fs-drawer-title-row">
        {subtitle ? <div className="fs-drawer-heading">
          <h2 className="fs-drawer-title" id={titleId}>{title}</h2>
          <p className="fs-drawer-subtitle">{subtitle}</p>
        </div> : <h2 className="fs-drawer-title" id={titleId}>{title}</h2>}
        <button aria-label={closeLabel} className="fs-drawer-close" onClick={onClose} type="button">
          <X aria-hidden="true" />
        </button>
      </div>
      {children}
    </dialog>,
    document.body,
  );
}

function canReceiveDrawerFocus(element: HTMLElement | null | undefined): element is HTMLElement {
  if (!element?.isConnected || element === document.body || element.matches(":disabled") || !element.matches('button, a[href], input, select, textarea, [tabindex], [contenteditable="true"]')) return false;
  for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
    if (ancestor.hidden || ancestor.inert || ancestor.getAttribute("aria-hidden") === "true") return false;
    const computed = getComputedStyle(ancestor);
    if (computed.display === "none" || computed.visibility === "hidden" || computed.visibility === "collapse" || computed.opacity === "0" || computed.contentVisibility === "hidden") return false;
  }
  return true;
}

/**
 * `document.activeElement` stops at a shadow host, so a control inside a
 * `platform-grid` shadow root reports as the host and focus never comes back to
 * it. `accessible-dialog.tsx` has walked the tree since it was written; this is
 * the same walk, for the dialog that replaced it.
 */
function deepestActiveElement(): HTMLElement | null {
  let activeElement: Element | null = document.activeElement;
  while (activeElement?.shadowRoot?.activeElement) {
    activeElement = activeElement.shadowRoot.activeElement;
  }
  return resolveRequestLoadingOpener(activeElement instanceof HTMLElement ? activeElement : null);
}

export function DsDialog({
  open,
  onClose,
  title,
  description,
  eyebrow,
  variant = "form",
  initialFocusRef,
  children,
  footer,
  busy = false,
  closeLabel,
  onEscape,
  role,
  restoreFocus = true,
}: PropsWithChildren<{
  open: boolean;
  onClose: () => void;
  title: string;
  description: string;
  eyebrow: string;
  /**
   * `catalog` is the wide, fixed-height browser for picking from a catalog: it
   * does not resize with its content and, beside the shell sidebar, centres in
   * the content area instead of covering the navigation.
   */
  variant?: "form" | "add-user" | "edit-user" | "confirm" | "secret" | "catalog";
  /** Form dialogs open on their first field rather than on the close button. */
  initialFocusRef?: React.RefObject<HTMLElement | null>;
  footer?: ReactNode;
  busy?: boolean;
  closeLabel?: string;
  /**
   * What Escape does when it is not simply "close": a dialog with stages steps
   * back one stage, and closes only from its first. Defaults to `onClose`.
   */
  onEscape?: () => void;
  /** Preserve a consumer's accessible role while sharing modal presentation. */
  role?: "dialog" | "alertdialog";
  /** Request-based consumers may own an explicit return-focus target. */
  restoreFocus?: boolean;
}>) {
  const { t } = useI18n();
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const busyRef = useRef(busy);
  const onCloseRef = useRef(onClose);
  const onEscapeRef = useRef(onEscape);
  useEffect(() => {
    busyRef.current = busy;
    onCloseRef.current = onClose;
    onEscapeRef.current = onEscape;
  }, [busy, onClose, onEscape]);
  useEffect(() => {
    if (!open) return;
    triggerRef.current = deepestActiveElement();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const dialog = dialogRef.current;
    let active = true;
    // Canonical portals share the modal layer; the last live modal paints above
    // earlier ones. Check the DOM, not listener registration or focused control.
    const ownsKeyboard = () => Array.from(document.querySelectorAll<HTMLElement>(
      'dialog[open][aria-modal="true"], [role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]',
    )).findLast((node) => !node.closest('[hidden], [aria-hidden="true"]')) === dialog;
    queueMicrotask(() => {
      if (!active || !ownsKeyboard()) return;
      const requested = initialFocusRef?.current;
      // Same selector as the trap, for the same reason: without it a dialog whose
      // first interactive element is a select or a textarea opens focused on the
      // dialog itself.
      const first = dialogRef.current?.querySelector<HTMLElement>(
        'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
      );
      (requested || first || dialogRef.current)?.focus();
    });
    const onKeyDown = (event: KeyboardEvent) => {
      if (!ownsKeyboard() || event.defaultPrevented) return;
      // A popup inside the dialog (a select, a filter menu) that handled Escape
      // marks it handled; the dialog must not also close or step back.
      if (event.key === "Escape") {
        event.preventDefault();
        if (!busyRef.current) (onEscapeRef.current ?? onCloseRef.current)();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      // Two things are wrong with the obvious version of this, and one of them
      // only bites in a browser.
      //
      // The selector list missed `textarea`, `select` and `a[href]`. A dialog
      // whose last focusable is a textarea loses focus forward for real, in every
      // browser — the trap simply does not see the element it should wrap from.
      //
      // The ordering is a test-environment trap. The spec requires
      // `querySelectorAll` to return document order; jsdom groups matches by
      // sub-selector instead, so `<button><input><button><input>` comes back as
      // button, button, input, input. `first` and `last` are then the wrong
      // elements under test and right in the browser, which is the worst way for a
      // bug to behave. Sorting by document position makes the result independent
      // of how the environment chose to return it.
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
        ),
      ).sort((a, b) =>
        a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1,
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!dialogRef.current.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first)?.focus();
        return;
      }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      active = false;
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
      if (!restoreFocus) return;
      const trigger = triggerRef.current;
      queueMicrotask(() => {
        if (trigger?.isConnected) {
          trigger.focus();
        }
      });
    };
  }, [initialFocusRef, open, restoreFocus]);
  if (!open) return null;
  const className = variant === "form" ? "fs-dialog" : `fs-dialog ${variant}`;
  return createPortal(
    <div className="fs-settings-portal fs-dialog-layer">
      <button aria-label={t("react.shared.dismiss_value1", "Dismiss {value1}", { value1: String(title) })} className="fs-dialog-backdrop" disabled={busy} onClick={onClose} tabIndex={-1} type="button" />
      <dialog
        open
        aria-describedby={descriptionId}
        aria-labelledby={titleId}
        aria-modal="true"
        aria-busy={busy}
        className={className}
        ref={dialogRef}
        role={role ?? (variant === "confirm" ? "alertdialog" : undefined)}
        tabIndex={-1}
      >
        <header className="fs-dialog-header">
          <div><span className="fs-dialog-eyebrow">{eyebrow}</span><h2 className="fs-dialog-title" id={titleId}>{title}</h2><p className="fs-dialog-description" id={descriptionId}>{description}</p></div>
          <button aria-label={closeLabel || t("react.shared.close_value1", "Close {value1}", { value1: String(title) })} className="fs-dialog-close" disabled={busy} onClick={onClose} type="button"><X aria-hidden="true" /></button>
        </header>
        <div className="fs-dialog-body">{children}</div>
        {footer ? <footer className="fs-dialog-footer">{footer}</footer> : null}
      </dialog>
    </div>,
    document.body,
  );
}

export function RowActionMenu({
  label,
  open,
  onOpenChange,
  children,
}: PropsWithChildren<{
  label: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}>) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const wasOpenRef = useRef(open);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  useEffect(() => {
    if (wasOpenRef.current && !open) {
      queueMicrotask(() => triggerRef.current?.focus());
    }
    wasOpenRef.current = open;
  }, [open]);
  useEffect(() => {
    if (!open || !triggerRef.current) return;
    const update = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const menuRect = menuRef.current?.getBoundingClientRect();
      if (!menuRect) return;
      const footerRect = triggerRef.current?.closest(".fs-card")?.querySelector<HTMLElement>(".fs-table-footer")?.getBoundingClientRect();
      const popupGap = 6;
      const viewportInset = 8;
      const belowTop = rect.bottom + popupGap;
      const aboveTop = rect.top - menuRect.height - popupGap;
      const collidesWithFooter = footerRect ? belowTop + menuRect.height > footerRect.top : false;
      const collidesWithViewport = belowTop + menuRect.height > window.innerHeight - viewportInset;
      const opensAbove = aboveTop >= viewportInset && (collidesWithFooter || collidesWithViewport);
      const top = opensAbove ? aboveTop : Math.min(belowTop, window.innerHeight - menuRect.height - viewportInset);
      const left = Math.min(Math.max(viewportInset, rect.right - menuRect.width), window.innerWidth - menuRect.width - viewportInset);
      setPosition({ top: Math.round(Math.max(viewportInset, top)), left: Math.round(left) });
    };
    const close = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node) && !triggerRef.current?.contains(event.target as Node)) onOpenChange(false);
    };
    const keydown = (event: KeyboardEvent) => {
      const items = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') || []);
      const activeIndex = items.indexOf(document.activeElement as HTMLElement);
      if (event.key === "Escape") {
        event.preventDefault();
        onOpenChange(false);
        triggerRef.current?.focus();
      } else if (event.key === "ArrowDown" && items.length) {
        event.preventDefault();
        items[(activeIndex + 1 + items.length) % items.length]?.focus();
      } else if (event.key === "ArrowUp" && items.length) {
        event.preventDefault();
        items[(activeIndex - 1 + items.length) % items.length]?.focus();
      } else if (event.key === "Home" && items.length) {
        event.preventDefault();
        items[0]?.focus();
      } else if (event.key === "End" && items.length) {
        event.preventDefault();
        items.at(-1)?.focus();
      }
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", keydown);
    const focusFrame = window.requestAnimationFrame(() => menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus());
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", keydown);
    };
  }, [onOpenChange, open]);
  return (
    <>
      <button aria-expanded={open} aria-haspopup="menu" aria-label={label} className="fs-row-action" onClick={() => onOpenChange(!open)} onKeyDown={(event) => { if (event.key === "ArrowDown") { event.preventDefault(); onOpenChange(true); } }} ref={triggerRef} type="button"><MoreVertical aria-hidden="true" /></button>
      {open ? createPortal(<div aria-label={label} className="fs-row-menu" ref={menuRef} role="menu" style={position}>{children}</div>, document.body) : null}
    </>
  );
}
