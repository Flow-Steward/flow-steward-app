import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { AppShellHeader } from "@/components/layout/app-shell-header";
import shellStyles from "@/styles/design-system-shell.css?raw";

function renderHeader(initialEntry = "/") {
  const actions = {
    onOpenApprovalJob: vi.fn(),
    onOpenSearch: vi.fn(),
    onRetryApprovals: vi.fn(),
    onToggleApprovals: vi.fn(),
    onToggleRail: vi.fn(),
  };
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <AppShellHeader
        approvalError=""
        approvalJobs={[]}
        approvalLoaded
        approvalLoading={false}
        approvalsOpen={false}
        breadcrumbs={[{ label: "Projects", to: "/projects" }, { label: "Project details" }]}
        onOpenApprovalJob={actions.onOpenApprovalJob}
        onOpenSearch={actions.onOpenSearch}
        onRetryApprovals={actions.onRetryApprovals}
        onToggleApprovals={actions.onToggleApprovals}
        onToggleRail={actions.onToggleRail}
        pendingApprovals={2}
        railExpanded
      />
    </MemoryRouter>,
  );
  return actions;
}

describe("AppShellHeader", () => {
  it("renders the canonical control order and omits sidebar-owned controls", () => {
    renderHeader();
    const header = screen.getByRole("banner");
    const controls = within(header)
      .getAllByTestId(/header-order-/)
      .map((node) => node.dataset.testid);
    expect(controls).toEqual([
      "header-order-trigger",
      "header-order-separator",
      "header-order-breadcrumbs",
      "header-order-search",
      "header-order-approvals",
    ]);
    expect(
      within(header).queryByRole("button", { name: "Workspace" }),
    ).not.toBeInTheDocument();
    expect(
      within(header).queryByRole("button", { name: "Account menu" }),
    ).not.toBeInTheDocument();
    expect(
      within(header).queryByRole("button", { name: "Light theme" }),
    ).not.toBeInTheDocument();
  });

  it("uses the locked Header geometry for trigger and secondary actions", () => {
    renderHeader();
    expect(
      screen.getByRole("button", { name: "Toggle sidebar" }),
    ).toHaveAttribute("data-size", "small");
    expect(
      screen.getByRole("button", { name: "Open approvals, 2 waiting" }),
    ).toHaveAttribute("data-size", "medium");
    expect(screen.getByRole("button", { name: "Global search" })).toHaveAttribute(
      "aria-haspopup",
      "dialog",
    );
    expect(
      screen.queryByRole("searchbox", { name: "Global search" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the familiar Sidebar toggle icon free of a visible tooltip", () => {
    renderHeader();
    const toggle = screen.getByRole("button", { name: "Toggle sidebar" });

    fireEvent.mouseEnter(toggle);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    fireEvent.focus(toggle);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(toggle).not.toHaveAttribute("aria-describedby");
    // Never a native title either: PanelLeft's name is enough (header.md, 2026-10-06).
    expect(toggle).not.toHaveAttribute("title");
  });

  it("carries the waiting count in the bell's name, not in the pill", () => {
    renderHeader();
    expect(screen.getByTestId("approval-badge")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByTestId("approval-bell")).toHaveAccessibleName("Open approvals, 2 waiting");
  });

  it("dispatches rail, search and approvals actions", () => {
    const view = renderHeader();
    fireEvent.click(screen.getByRole("button", { name: "Toggle sidebar" }));
    fireEvent.click(screen.getByRole("button", { name: "Global search" }));
    fireEvent.click(screen.getByRole("button", { name: "Open approvals, 2 waiting" }));
    expect(view.onToggleRail).toHaveBeenCalledTimes(1);
    expect(view.onOpenSearch).toHaveBeenCalledTimes(1);
    expect(view.onToggleApprovals).toHaveBeenCalledTimes(1);
  });

  it("no longer offers an assistant trigger", () => {
    renderHeader();
    expect(screen.queryByRole("button", { name: "Ask AI" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("global-assist-trigger")).not.toBeInTheDocument();
  });

  it("marks only the final breadcrumb current on a project detail route", () => {
    renderHeader("/projects/ce42a80b-9d5f-4f39-8012-513d915bc6af");
    const breadcrumb = screen.getByRole("navigation", { name: "Breadcrumb" });
    const parent = within(breadcrumb).getByRole("link", { name: "Projects" });

    expect(parent).toHaveAttribute("href", "/projects");
    expect(parent).not.toHaveAttribute("aria-current");
    expect(
      within(breadcrumb).getAllByRole("generic", { current: "page" }),
    ).toHaveLength(1);
    expect(
      within(breadcrumb).getByText("Project details"),
    ).toHaveAttribute("aria-current", "page");
  });

  it("keeps the Header one row and collapses only Search at tablet width", () => {
    expect(shellStyles).toMatch(
      /\.fs-app-header\s*\{[^}]*display:\s*flex;[^}]*height:\s*calc\(var\(--fs-header-height\) - 1px\);/s,
    );
    expect(shellStyles).not.toMatch(
      /\.fs-app-header\s*\{[^}]*flex-wrap:/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-icon-button\[data-size="small"\]\s*\{[^}]*var\(--fs-icon-button-size-sm\)/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-header__search-slot\s*\{[^}]*width:\s*var\(--fs-header-search-trigger-width\);[^}]*flex:\s*none;/s,
    );
    expect(shellStyles).toMatch(
      /@media \(width < theme\(--breakpoint-lg\)\)[\s\S]*\.fs-app-header__search-label,[\s\S]*\.fs-app-header__search-shortcut\s*\{[^}]*display:\s*none;/,
    );
    expect(shellStyles).not.toMatch(
      /\.fs-app-header__search input/,
    );
  });
});
