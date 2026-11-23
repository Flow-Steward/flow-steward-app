import { useEffect, useRef, type PropsWithChildren, type ReactNode } from "react";
import { X } from "lucide-react";

import type { SidebarRailState } from "@/components/layout/app-shell-rail-state";
import { IconButton } from "@/components/ui/icon-button";

export const SHELL_NAVIGATION_ID = "app-shell-navigation";

type ShellLayoutProps = PropsWithChildren<{
  header: ReactNode;
  maintenance?: ReactNode;
  railState: SidebarRailState;
  sidebar: ReactNode;
  /** Below the md breakpoint the sidebar is a full-width drawer, open only on demand. */
  mobileNavOpen?: boolean;
  onCloseMobileNav?: () => void;
  closeMobileNavLabel?: string;
}>;

export function ShellLayout({
  children,
  header,
  maintenance,
  railState,
  sidebar,
  mobileNavOpen = false,
  onCloseMobileNav,
  closeMobileNavLabel = "Close menu",
}: Readonly<ShellLayoutProps>) {
  const closeRef = useRef<HTMLDivElement | null>(null);

  // The open drawer takes focus, gives it back when it closes and closes on Escape.
  useEffect(() => {
    if (!mobileNavOpen) {
      return undefined;
    }
    const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.querySelector("button")?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onCloseMobileNav?.();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (returnFocus?.isConnected) returnFocus.focus();
    };
  }, [mobileNavOpen, onCloseMobileNav]);

  return (
    <div
      className="fs-shell-layout"
      data-mobile-nav={mobileNavOpen ? "open" : "closed"}
      data-rail-state={railState}
      data-testid="app-shell-layout"
    >
      <div className="fs-shell-sidebar-frame" data-testid="app-shell-sidebar-frame" id={SHELL_NAVIGATION_ID}>
        {onCloseMobileNav ? (
          <div className="fs-shell-sidebar-close" ref={closeRef}>
            <IconButton
              className="fs-app-header__trigger"
              icon={X}
              iconSize={24}
              label={closeMobileNavLabel}
              onClick={onCloseMobileNav}
              showTooltip={false}
              size="small"
            />
          </div>
        ) : null}
        {sidebar}
      </div>
      <div className="fs-shell-inset">
        {maintenance ? (
          <div
            className="fs-shell-maintenance-frame"
            data-testid="app-shell-maintenance-frame"
          >
            {maintenance}
          </div>
        ) : null}
        <div className="fs-shell-header-frame" data-testid="app-shell-header-frame">
          {header}
        </div>
        <main className="fs-shell-main">{children}</main>
      </div>
    </div>
  );
}
