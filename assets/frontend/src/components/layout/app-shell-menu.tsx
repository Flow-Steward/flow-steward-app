import type {
  ButtonHTMLAttributes,
  PropsWithChildren,
  ReactNode,
} from "react";
import { useCallback, useEffect, useId, useRef } from "react";

function isEnabledMenuItem(item: HTMLElement): boolean {
  return (
    !item.hasAttribute("disabled") &&
    item.getAttribute("aria-disabled") !== "true"
  );
}

function getEnabledMenuItems(root: HTMLElement | null): HTMLElement[] {
  return Array.from(
    root?.querySelectorAll<HTMLElement>('[role^="menuitem"]') || [],
  ).filter(isEnabledMenuItem);
}

type AppShellMenuProps = PropsWithChildren<{
  label: string;
  menuLabel: string;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  panelClassName?: string;
  triggerClassName?: string;
  triggerContent: ReactNode;
  triggerInteractionProps?: Pick<
    ButtonHTMLAttributes<HTMLButtonElement>,
    | "aria-describedby"
    | "onBlur"
    | "onFocus"
    | "onMouseEnter"
    | "onMouseLeave"
  >;
}>;

export function AppShellMenu({
  children,
  label,
  menuLabel,
  onOpenChange,
  open,
  panelClassName = "",
  triggerClassName = "",
  triggerContent,
  triggerInteractionProps,
}: Readonly<AppShellMenuProps>) {
  const menuId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const pendingOpenFocus = useRef<"first" | "last" | null>(null);
  const closeAndReturnFocus = useCallback(() => {
    onOpenChange(false);
    globalThis.queueMicrotask(() => triggerRef.current?.focus());
  }, [onOpenChange]);

  useEffect(() => {
    if (!open || !pendingOpenFocus.current) return;
    const menuItems = getEnabledMenuItems(rootRef.current);
    const target =
      pendingOpenFocus.current === "first"
        ? menuItems[0]
        : menuItems.at(-1);
    pendingOpenFocus.current = null;
    target?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        closeAndReturnFocus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [closeAndReturnFocus, open]);

  const moveMenuFocus = (direction: "ArrowDown" | "ArrowUp") => {
    const menuItems = getEnabledMenuItems(rootRef.current);
    if (!menuItems.length) return;
    const currentIndex = menuItems.indexOf(document.activeElement as HTMLElement);
    let nextIndex = currentIndex - 1;
    if (direction === "ArrowDown") {
      nextIndex = (currentIndex + 1) % menuItems.length;
    } else if (currentIndex <= 0) {
      nextIndex = menuItems.length - 1;
    }
    menuItems[nextIndex]?.focus();
  };

  return (
    <div className="fs-shell-menu" ref={rootRef}>
      <button
        {...triggerInteractionProps}
        aria-controls={open ? menuId : undefined}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={label}
        className={triggerClassName}
        onClick={() => onOpenChange(!open)}
        onKeyDown={(event) => {
          if (event.key === "Escape" && open) {
            event.preventDefault();
            closeAndReturnFocus();
            return;
          }
          if (event.key !== "ArrowDown" && event.key !== "ArrowUp") {
            return;
          }
          event.preventDefault();
          if (open) {
            moveMenuFocus(event.key);
            return;
          }
          pendingOpenFocus.current = event.key === "ArrowDown" ? "first" : "last";
          onOpenChange(true);
        }}
        ref={triggerRef}
        type="button"
      >
        {triggerContent}
      </button>
      {open ? (
        <div
          aria-label={menuLabel}
          className={`fs-shell-menu-panel ${panelClassName}`}
          id={menuId}
          onClick={(event) => {
            const target = event.target;
            const menuItem =
              target instanceof Element &&
              target.closest<HTMLElement>('[role^="menuitem"]');
            if (menuItem && isEnabledMenuItem(menuItem)) {
              closeAndReturnFocus();
            }
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              closeAndReturnFocus();
              return;
            }
            if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
            event.preventDefault();
            moveMenuFocus(event.key);
          }}
          role="menu"
          tabIndex={-1}
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}
