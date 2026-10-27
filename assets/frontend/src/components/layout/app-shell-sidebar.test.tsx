import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FolderKanban, Workflow } from "lucide-react";
import { MemoryRouter } from "react-router";

import { AppShellSidebar } from "@/components/layout/app-shell-sidebar";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import type {
  NavSection,
  WorkspaceOption,
} from "@/components/layout/app-shell-helpers";
import type { SidebarRailState } from "@/components/layout/app-shell-rail-state";

const sections: NavSection[] = [
  {
    key: "main",
    label: "Main",
    items: [
      {
        key: "projects",
        label: "Projects",
        to: "/projects",
        icon: FolderKanban,
      },
    ],
  },
  {
    key: "registry",
    label: "Registry",
    items: [
      {
        key: "workflows",
        label: "Workflows",
        to: "/registry/workflows",
        icon: Workflow,
      },
    ],
  },
];

const workspaceOptions: WorkspaceOption[] = [
  { id: "workspace-atlas", displayName: "Atlas", workspaceKey: "atlas" },
  { id: "workspace-ops", displayName: "Operations", workspaceKey: "operations" },
];

function renderSidebar({
  pathname = "/projects",
  railState = "expanded",
  sidebarSections = sections,
  workspaceError = "",
}: {
  pathname?: string;
  railState?: SidebarRailState;
  sidebarSections?: NavSection[];
  workspaceError?: string;
} = {}) {
  const callbacks = {
    onExpandRail: vi.fn(),
    onLogout: vi.fn().mockResolvedValue(undefined),
    onRetryWorkspaces: vi.fn(),
    onThemeChange: vi.fn(),
    onToggleRail: vi.fn(),
    onWorkspaceChange: vi.fn(),
  };

  render(
    <MemoryRouter initialEntries={[pathname]}>
      <AppShellSidebar
        loggingOut={false}
        pathname={pathname}
        railState={railState}
        sections={sidebarSections}
        selectedWorkspaceId="workspace-atlas"
        themeMode="dark"
        userDisplayName="Tatiana"
        workspaceError={workspaceError}
        workspaceLoading={false}
        workspaceOptions={workspaceOptions}
        {...callbacks}
      />
    </MemoryRouter>,
  );

  return callbacks;
}

describe("AppShellSidebar", () => {
  it("localizes empty workspace fallbacks without translating authored workspace names", () => {
    const messages: Record<string, string> = {
      "project_mode.workspace": "Darbo erdvė",
      "react.shared.workspace_scope": "Darbo erdvės sritis",
    };
    const props = {
      loggingOut: false,
      pathname: "/projects",
      railState: "expanded" as const,
      sections,
      selectedWorkspaceId: "workspace-authored",
      themeMode: "dark" as const,
      userDisplayName: "Tatiana",
      workspaceError: "",
      workspaceLoading: false,
      onExpandRail: () => undefined,
      onLogout: async () => undefined,
      onRetryWorkspaces: () => undefined,
      onThemeChange: () => undefined,
      onToggleRail: () => undefined,
      onWorkspaceChange: () => undefined,
    };
    const localizedSidebar = (options: WorkspaceOption[]) => (
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, fallback = "") => messages[key] || fallback || key }}>
        <MemoryRouter><AppShellSidebar {...props} workspaceOptions={options} /></MemoryRouter>
      </I18nContext.Provider>
    );
    const { rerender } = render(localizedSidebar([]));
    const trigger = screen.getByRole("button", { name: "Darbo erdvė" });
    expect(within(trigger).getByText("Darbo erdvė")).toBeInTheDocument();
    expect(within(trigger).getByText("Darbo erdvės sritis")).toBeInTheDocument();

    rerender(localizedSidebar([{ id: "workspace-authored", displayName: "Workspace", workspaceKey: "Workspace scope" }]));
    expect(within(trigger).getByText("Workspace")).toBeInTheDocument();
    expect(within(trigger).getByText("Workspace scope")).toBeInTheDocument();
  });

  it("renders the canonical brand before the fixed workspace selector", () => {
    renderSidebar();

    const brand = screen.getByRole("img", { name: "Flow Steward" });
    const workspaceTrigger = screen.getByRole("button", { name: "Workspace" });
    expect(within(brand).getByText("Flow Steward")).toHaveClass(
      "fs-app-sidebar__brand-wordmark",
    );
    expect(
      brand.compareDocumentPosition(workspaceTrigger) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("keeps only the canonical mark in the collapsed brand header, as the expand control", () => {
    const view = renderSidebar({ railState: "collapsed" });

    const expand = screen.getByRole("button", { name: "Expand sidebar" });
    expect(within(expand).queryByText("Flow Steward")).not.toBeInTheDocument();
    expect(within(expand).getByTestId("flow-steward-brand-mark")).toHaveClass(
      "fs-app-sidebar__brand-mark",
    );
    expect(expand).not.toHaveAttribute("title");

    fireEvent.click(expand);
    expect(view.onExpandRail).toHaveBeenCalledTimes(1);
  });

  it("toggles the rail from its edge, which names itself without a title or a tab stop", () => {
    const view = renderSidebar();

    const edge = screen.getByTestId("sidebar-rail-edge");
    expect(edge).toHaveAccessibleName("Toggle sidebar");
    expect(edge).toHaveAttribute("tabindex", "-1");
    expect(edge).not.toHaveAttribute("title");

    fireEvent.click(edge);
    expect(view.onToggleRail).toHaveBeenCalledTimes(1);
  });

  it("never offers the expanded brand as a control", () => {
    renderSidebar();

    expect(screen.getByRole("img", { name: "Flow Steward" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Expand sidebar" })).not.toBeInTheDocument();
  });

  it("renders one active leaf when an injected section duplicates a built-in route", () => {
    renderSidebar({
      pathname: "/projects",
      sidebarSections: [
        ...sections,
        {
          key: "injected",
          label: "Injected",
          items: [
            {
              key: "injected-projects",
              label: "Injected Projects",
              to: "/projects",
              icon: FolderKanban,
            },
          ],
        },
      ],
    });

    expect(screen.getAllByRole("link", { name: /Projects/ })).toHaveLength(1);
    expect(
      screen
        .getAllByRole("link")
        .filter((link) => link.getAttribute("aria-current") === "page"),
    ).toHaveLength(1);
  });

  it("renders exactly one active leaf and hides labels in collapsed mode", () => {
    renderSidebar({ pathname: "/registry/workflows", railState: "collapsed" });
    expect(
      screen
        .getAllByRole("link")
        .filter((link) => link.getAttribute("aria-current") === "page"),
    ).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Workflows" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByTestId("app-shell-sidebar")).toHaveAttribute(
      "data-rail-state",
      "collapsed",
    );
  });

  it("keeps workspace and profile menus mutually exclusive", () => {
    renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    expect(screen.getByRole("menu", { name: "Workspace menu" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));
    expect(
      screen.queryByRole("menu", { name: "Workspace menu" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("menu", { name: "Account menu" })).toBeVisible();
  });

  it("uses the canonical workspace and account compositions", () => {
    renderSidebar();

    const workspaceTrigger = screen.getByRole("button", { name: "Workspace" });
    expect(
      within(workspaceTrigger).getByText("Atlas"),
    ).toHaveClass("fs-app-sidebar__workspace-name");
    expect(
      within(workspaceTrigger).getByText("atlas"),
    ).toHaveClass("fs-app-sidebar__workspace-key");

    fireEvent.click(workspaceTrigger);
    const workspaceMenu = screen.getByRole("menu", { name: "Workspace menu" });
    expect(within(workspaceMenu).getByText("Workspaces")).toBeVisible();
    expect(
      within(workspaceMenu).getByRole("menuitemradio", { name: "Atlas" }),
    ).toBeChecked();

    const accountTrigger = screen.getByRole("button", { name: "Account menu" });
    expect(
      within(accountTrigger).getByText("TA"),
    ).toHaveClass("fs-app-sidebar__account-avatar");
    expect(within(accountTrigger).getByText("Tatiana")).toBeVisible();
    expect(within(accountTrigger).queryByText("Current account")).not.toBeInTheDocument();

    expect(screen.queryByRole("group", { name: "Theme" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitemradio", { name: "Light theme" }),
    ).not.toBeInTheDocument();

    fireEvent.click(accountTrigger);
    const accountMenu = screen.getByRole("menu", { name: "Account menu" });
    expect(
      within(accountMenu).getByRole("group", { name: "Appearance" }),
    ).toBeVisible();
    expect(
      within(accountMenu).getByRole("menuitemradio", { name: "Dark theme" }),
    ).toBeChecked();
  });

  it("exposes unique associated collapsed tooltips only during focus or hover", () => {
    renderSidebar({ railState: "collapsed" });
    const workspaceTrigger = screen.getByRole("button", { name: "Workspace" });
    const workflowsLink = screen.getByRole("link", { name: "Workflows" });
    const accountTrigger = screen.getByRole("button", { name: "Account menu" });

    expect(screen.queryAllByRole("tooltip")).toHaveLength(0);
    expect(workspaceTrigger).not.toHaveAttribute("aria-describedby");

    fireEvent.focus(workspaceTrigger);
    const workspaceTooltip = within(workspaceTrigger).getByRole("tooltip");
    expect(workspaceTooltip).toHaveTextContent("Atlas");
    expect(workspaceTooltip).toHaveClass("fs-app-sidebar__tooltip");
    expect(workspaceTooltip).toHaveAttribute("id");
    expect(workspaceTrigger).toHaveAttribute(
      "aria-describedby",
      workspaceTooltip.id,
    );
    fireEvent.blur(workspaceTrigger);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(workspaceTrigger).not.toHaveAttribute("aria-describedby");

    fireEvent.mouseEnter(workspaceTrigger);
    expect(within(workspaceTrigger).getByRole("tooltip")).toHaveAttribute(
      "id",
      workspaceTooltip.id,
    );
    fireEvent.mouseLeave(workspaceTrigger);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    fireEvent.focus(workflowsLink);
    const workflowsTooltip = within(workflowsLink).getByRole("tooltip");
    expect(workflowsLink).toHaveAttribute("aria-describedby", workflowsTooltip.id);
    expect(workflowsTooltip.id).not.toBe(workspaceTooltip.id);
    fireEvent.blur(workflowsLink);

    fireEvent.focus(accountTrigger);
    const accountTooltip = within(accountTrigger).getByRole("tooltip");
    expect(accountTrigger).toHaveAttribute("aria-describedby", accountTooltip.id);
    expect(accountTooltip.id).not.toBe(workflowsTooltip.id);
  });

  it("suppresses a focused collapsed trigger tooltip while its menu is open", () => {
    renderSidebar({ railState: "collapsed" });
    const workspaceTrigger = screen.getByRole("button", { name: "Workspace" });
    fireEvent.focus(workspaceTrigger);
    expect(within(workspaceTrigger).getByRole("tooltip")).toHaveTextContent("Atlas");
    fireEvent.click(workspaceTrigger);
    expect(within(workspaceTrigger).queryByRole("tooltip")).not.toBeInTheDocument();
    expect(workspaceTrigger).not.toHaveAttribute("aria-describedby");

    const accountTrigger = screen.getByRole("button", { name: "Account menu" });
    fireEvent.focus(accountTrigger);
    expect(within(accountTrigger).getByRole("tooltip")).toHaveTextContent("Tatiana");
    fireEvent.click(accountTrigger);
    expect(within(accountTrigger).queryByRole("tooltip")).not.toBeInTheDocument();
    expect(accountTrigger).not.toHaveAttribute("aria-describedby");
  });

  it("dispatches workspace retry, theme, and logout callbacks", async () => {
    const user = userEvent.setup();
    const {
      onLogout,
      onRetryWorkspaces,
      onThemeChange,
    } = renderSidebar({ workspaceError: "Workspace load failed" });
    await user.click(screen.getByRole("button", { name: "Workspace" }));
    await user.click(screen.getByRole("button", { name: "Retry workspaces" }));
    expect(onRetryWorkspaces).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Account menu" }));
    await user.click(screen.getByRole("menuitemradio", { name: "Light theme" }));
    expect(onThemeChange).toHaveBeenCalledWith("light");
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Account menu" }));
    await user.click(screen.getByRole("menuitem", { name: "Log out" }));
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it("returns focus after workspace, theme, and logout selections", async () => {
    const user = userEvent.setup();
    const { onLogout, onThemeChange, onWorkspaceChange } = renderSidebar();
    const workspaceTrigger = screen.getByRole("button", { name: "Workspace" });
    const accountTrigger = screen.getByRole("button", { name: "Account menu" });

    await user.click(workspaceTrigger);
    await user.click(screen.getByRole("menuitemradio", { name: "Operations" }));
    expect(onWorkspaceChange).toHaveBeenCalledWith("workspace-ops");
    expect(screen.queryByRole("menu", { name: "Workspace menu" })).not.toBeInTheDocument();
    expect(workspaceTrigger).toHaveFocus();

    await user.click(accountTrigger);
    await user.click(screen.getByRole("menuitemradio", { name: "Light theme" }));
    expect(onThemeChange).toHaveBeenCalledWith("light");
    expect(screen.queryByRole("menu", { name: "Account menu" })).not.toBeInTheDocument();
    expect(accountTrigger).toHaveFocus();

    await user.click(accountTrigger);
    await user.click(screen.getByRole("menuitem", { name: "Log out" }));
    expect(onLogout).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu", { name: "Account menu" })).not.toBeInTheDocument();
    expect(accountTrigger).toHaveFocus();
  });
});
