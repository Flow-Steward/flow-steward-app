import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";

import { createPortal } from "react-dom";

import "@/components/shared/popover.css";

const FOCUSABLE =
  'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

export type PopoverTriggerProps = {
  ref: RefObject<HTMLButtonElement | null>;
  "aria-controls": string | undefined;
  "aria-expanded": boolean;
  "aria-haspopup": "dialog";
  onClick: () => void;
};

/**
 * A non-blocking floating surface anchored to a trigger, built to the canon's
 * Popover (03_components/overlays-feedback/popover.md):
 * - it opens 6px below the trigger, its left edge on the trigger's left edge;
 * - on open, focus moves to the element marked `data-popover-initial-focus`,
 *   or else to the first interactive element;
 * - Escape and any close the caller makes (Apply, a preset) return focus to the
 *   trigger. An outside pointer press closes it and leaves focus where it went.
 *
 * The caller renders the trigger through `trigger`, so it keeps its own look.
 */
export function DsPopover({
  children,
  label,
  onOpenChange,
  open,
  trigger,
  wide = false,
  portal = false,
}: Readonly<{
  children: ReactNode;
  label: string;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  trigger: (props: PopoverTriggerProps) => ReactNode;
  wide?: boolean;
  /** Escape transformed/overflowing ancestors while retaining viewport-size chrome. */
  portal?: boolean;
}>) {
  const id = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closedByOutsidePress = useRef(false);
  const wasOpen = useRef(open);
  const focusPending = useRef(open);
  const [placement, setPlacement] = useState<{ left: number; top: number; maxHeight: number } | null>(null);

  useLayoutEffect(() => {
    if (!open || !portal) return undefined;
    let frame = 0;
    const position = () => {
      const anchor = triggerRef.current?.getBoundingClientRect();
      const surface = surfaceRef.current;
      if (!anchor || !surface) return;
      const size = surface.getBoundingClientRect();
      const gap = Number.parseFloat(getComputedStyle(surface).getPropertyValue("--fs-popup-offset")) || 6;
      const margin = 8;
      const maxHeight = Math.max(0, window.innerHeight - margin * 2);
      const height = Math.min(size.height, maxHeight);
      const below = anchor.bottom + gap;
      const above = anchor.top - gap - height;
      const top = Math.max(margin, Math.min(below + height > window.innerHeight - margin && above >= margin ? above : below, window.innerHeight - margin - height));
      const left = Math.max(margin, Math.min(anchor.left, window.innerWidth - margin - size.width));
      setPlacement((previous) => previous?.left === left && previous.top === top && previous.maxHeight === maxHeight ? previous : { left, top, maxHeight });
    };
    // Canvas transforms change the trigger's viewport rect without a resize or
    // scroll event. Only the open portaled layer tracks those changes.
    const follow = () => { position(); frame = requestAnimationFrame(follow); };
    position(); frame = requestAnimationFrame(follow);
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    return () => { cancelAnimationFrame(frame); window.removeEventListener("resize", position); window.removeEventListener("scroll", position, true); };
  }, [open, portal]);

  useEffect(() => {
    if (open && !wasOpen.current) {
      closedByOutsidePress.current = false;
      focusPending.current = true;
    }
    if (!open && wasOpen.current && !closedByOutsidePress.current) {
      triggerRef.current?.focus();
    }
    if (!open) focusPending.current = false;
    wasOpen.current = open;
  }, [open]);

  // Initial focus is owed once per opening, an opening at mount included, and is
  // moved only when the surface can take it: a portaled surface stays hidden until
  // it is placed, and focus() on a hidden element does nothing.
  const placed = !portal || placement !== null;
  useEffect(() => {
    if (!open || !placed || !focusPending.current) return;
    focusPending.current = false;
    const surface = surfaceRef.current;
    const target =
      surface?.querySelector<HTMLElement>("[data-popover-initial-focus]") ??
      // Sorted by document position: jsdom groups a selector list by
      // sub-selector, so the first match is not always the first element.
      Array.from(surface?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []).sort((a, b) =>
        a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1,
      )[0];
    target?.focus();
  }, [open, placed]);

  useEffect(() => {
    if (!open) return undefined;
    const pressOutside = (event: MouseEvent) => {
      if (rootRef.current?.contains(event.target as Node) || surfaceRef.current?.contains(event.target as Node)) return;
      closedByOutsidePress.current = true;
      onOpenChange(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // Only this layer closes; a drawer or dialog behind it stays open.
      event.stopPropagation();
      onOpenChange(false);
    };
    document.addEventListener("mousedown", pressOutside);
    const root = rootRef.current;
    root?.addEventListener("keydown", escape);
    const surface = portal ? surfaceRef.current : null;
    surface?.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", pressOutside);
      root?.removeEventListener("keydown", escape);
      surface?.removeEventListener("keydown", escape);
    };
  }, [onOpenChange, open, portal]);

  const surface = open ? (
    <div
      aria-label={label}
      className={wide ? "fs-popover wide" : "fs-popover"}
      data-popover-portal={portal || undefined}
      id={id}
      ref={surfaceRef}
      role="dialog"
      style={portal ? { position: "fixed", left: placement?.left ?? 0, top: placement?.top ?? 0, maxHeight: placement?.maxHeight, overflowY: "auto", visibility: placement ? undefined : "hidden" } : undefined}
    >
      {children}
    </div>
  ) : null;
  return (
    <div className="fs-popover-anchor" ref={rootRef}>
      {trigger({
        ref: triggerRef,
        "aria-controls": open ? id : undefined,
        "aria-expanded": open,
        "aria-haspopup": "dialog",
        onClick: () => onOpenChange(!open),
      })}
      {portal && surface ? createPortal(surface, document.body) : surface}
    </div>
  );
}
