import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Check } from "lucide-react";
import "./canvas-menus.css";

/** One command of a canvas menu (right-click, a step's More, Group…). */
export type CanvasMenuItem =
  | {
      kind?: "item";
      key: string;
      label: string;
      /** The key that does the same, shown on the right: "⌘D", "Shift+1". */
      shortcut?: string;
      /** A command that cannot run here stays listed and says why under it. */
      disabledReason?: string;
      /** An action offered under the reason ("Open Automation"). */
      reasonAction?: { label: string; onSelect: () => void };
      danger?: boolean;
      checked?: boolean;
      /** Opens a second level in place of the item list (Group… → Group / Parallel lanes). */
      submenu?: () => CanvasMenuLevel;
      onSelect?: () => void;
    }
  | { kind: "separator"; key: string };

/** A level of the menu: a list of commands, or content such as a summary with its own buttons. */
export type CanvasMenuLevel = { heading?: string; items?: CanvasMenuItem[]; content?: (close: () => void) => ReactNode };

function MenuRows({ items, onClose, onOpen }: Readonly<{ items: CanvasMenuItem[]; onClose: () => void; onOpen: (level: CanvasMenuLevel) => void }>) {
  return <>{items.map((item) => {
    if (item.kind === "separator") return <div className="workflow-menu-separator" role="separator" key={item.key} />;
    const refused = Boolean(item.disabledReason);
    return <Fragment key={item.key}>
      <button type="button" role={item.checked === undefined ? "menuitem" : "menuitemradio"} aria-checked={item.checked} aria-disabled={refused || undefined} aria-haspopup={item.submenu ? "menu" : undefined} data-danger={item.danger || undefined}
        onClick={(event) => {
          event.stopPropagation();
          if (refused) return;
          if (item.submenu) { onOpen(item.submenu()); return; }
          onClose();
          item.onSelect?.();
        }}>
        <span>{item.label}</span>
        {item.checked ? <Check aria-hidden="true" size={14} className="workflow-menu-check" /> : item.shortcut ? <span className="workflow-menu-shortcut" aria-hidden="true">{item.shortcut}</span> : <span />}
        {item.disabledReason ? <span className="workflow-menu-reason">{item.disabledReason}</span> : null}
      </button>
      {item.disabledReason && item.reasonAction ? <button type="button" role="menuitem" onClick={(event) => { event.stopPropagation(); onClose(); item.reasonAction?.onSelect(); }}><span className="workflow-menu-subitems">{item.reasonAction.label}</span><span /></button> : null}
    </Fragment>;
  })}</>;
}

/**
 * A canvas command menu at a point or under its trigger: the canonical row menu
 * surface (`.fs-row-menu`), portaled, kept inside the viewport, with arrows,
 * Home/End, Escape and an outside press. Escape on a second level returns to the first.
 */
export function CanvasMenuSurface({ label, anchor, level, onClose }: Readonly<{
  label: string;
  /** A client point (right-click) or the element the menu opens under (More). */
  anchor: { x: number; y: number } | HTMLElement;
  level: CanvasMenuLevel;
  onClose: () => void;
}>) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [stack, setStack] = useState<CanvasMenuLevel[]>([level]);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const current = stack.at(-1) ?? level;
  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!menu) return;
    const size = menu.getBoundingClientRect();
    const inset = 8;
    let left: number; let top: number;
    if (anchor instanceof HTMLElement) {
      const rect = anchor.getBoundingClientRect();
      left = rect.right - size.width; top = rect.bottom + 4;
      if (top + size.height > window.innerHeight - inset) top = rect.top - size.height - 4;
    } else { left = anchor.x; top = anchor.y; }
    left = Math.max(inset, Math.min(left, window.innerWidth - size.width - inset));
    top = Math.max(inset, Math.min(top, window.innerHeight - size.height - inset));
    setPosition((previous) => previous?.left === left && previous.top === top ? previous : { left, top });
  }, [anchor, current]);
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => menuRef.current?.querySelector<HTMLElement>('[role^="menuitem"]:not([aria-disabled="true"]), button')?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [current]);
  useEffect(() => {
    const press = (event: MouseEvent) => {
      if (menuRef.current?.contains(event.target as Node)) return;
      if (anchor instanceof HTMLElement && anchor.contains(event.target as Node)) return;
      onClose();
    };
    const keydown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault(); event.stopPropagation();
      if (stack.length > 1) setStack((levels) => levels.slice(0, -1));
      else { onClose(); if (anchor instanceof HTMLElement) anchor.focus(); }
    };
    document.addEventListener("mousedown", press);
    document.addEventListener("keydown", keydown, true);
    return () => { document.removeEventListener("mousedown", press); document.removeEventListener("keydown", keydown, true); };
  }, [anchor, onClose, stack.length]);
  return createPortal(<div
    aria-label={current.heading || label}
    className="fs-row-menu workflow-canvas-menu nodrag nopan nowheel"
    data-canvas-secondary-action="true"
    ref={menuRef}
    role="menu"
    tabIndex={-1}
    style={{ left: position?.left ?? 0, top: position?.top ?? 0, visibility: position ? undefined : "hidden" }}
    onContextMenu={(event) => event.preventDefault()}
    onPointerDown={(event) => event.stopPropagation()}
    onKeyDown={(event) => {
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault(); event.stopPropagation();
      const items = [...event.currentTarget.querySelectorAll<HTMLElement>('[role^="menuitem"], .workflow-menu-actions button')];
      if (!items.length) return;
      const index = items.indexOf(document.activeElement as HTMLElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }}
  >
    {current.heading ? <p className="workflow-menu-heading">{current.heading}</p> : null}
    {current.items ? <MenuRows items={current.items} onClose={onClose} onOpen={(next) => setStack((levels) => [...levels, next])} /> : null}
    {current.content?.(onClose)}
  </div>, document.body);
}
