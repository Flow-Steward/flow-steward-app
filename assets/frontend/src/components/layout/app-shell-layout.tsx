import type { PropsWithChildren, ReactNode } from "react";

import type { SidebarRailState } from "@/components/layout/app-shell-rail-state";

type ShellLayoutProps = PropsWithChildren<{
  header: ReactNode;
  maintenance?: ReactNode;
  railState: SidebarRailState;
  sidebar: ReactNode;
}>;

export function ShellLayout({
  children,
  header,
  maintenance,
  railState,
  sidebar,
}: Readonly<ShellLayoutProps>) {
  return (
    <div
      className="fs-shell-layout"
      data-rail-state={railState}
      data-testid="app-shell-layout"
    >
      <div className="fs-shell-sidebar-frame" data-testid="app-shell-sidebar-frame">
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
