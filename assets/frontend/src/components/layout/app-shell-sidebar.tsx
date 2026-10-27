import { useI18n } from "@/lib/i18n/useI18n";
import { useId, useState } from "react";

import {
  Check,
  ChevronsUpDown,
  GalleryVerticalEnd,
  LogOut,
  Moon,
  RotateCcw,
  Sun,
  UserRound,
  Users,
} from "lucide-react";
import { NavLink } from "react-router";

import { AppShellMenu } from "@/components/layout/app-shell-menu";
import { FlowStewardMark } from "@/components/layout/flow-steward-mark";
import { workspaceInitial } from "@/components/layout/workspace-initial";
import type {
  NavEntry,
  NavSection,
  WorkspaceOption,
} from "@/components/layout/app-shell-helpers";
import type { SidebarRailState } from "@/components/layout/app-shell-rail-state";
import { ignoreAsyncResult } from "@/lib/utils/async";

function useSidebarTooltip(enabled: boolean) {
  const tooltipId = useId();
  const [focused, setFocused] = useState(false);
  const [hovered, setHovered] = useState(false);
  const visible = enabled && (focused || hovered);
  return {
    tooltipId,
    triggerInteractionProps: {
      "aria-describedby": visible ? tooltipId : undefined,
      onBlur: () => setFocused(false),
      onFocus: () => setFocused(true),
      onMouseEnter: () => setHovered(true),
      onMouseLeave: () => setHovered(false),
    },
    visible,
  };
}

function SidebarTooltip({
  id,
  label,
  visible,
}: Readonly<{ id: string; label: string; visible: boolean }>) {
  return visible ? (
    <span
      className="fs-shell-tooltip fs-app-sidebar__tooltip"
      id={id}
      role="tooltip"
    >
      {label}
    </span>
  ) : null;
}

export type AppShellSidebarProps = {
  loggingOut: boolean;
  onExpandRail: () => void;
  onLogout: () => Promise<void>;
  onRetryWorkspaces: () => void;
  onThemeChange: (theme: "light" | "dark") => void;
  onToggleRail: () => void;
  onWorkspaceChange: (workspaceId: string) => void;
  pathname: string;
  railState: SidebarRailState;
  sections: NavSection[];
  selectedWorkspaceId: string;
  themeMode: "light" | "dark";
  userDisplayName: string;
  workspaceError: string;
  workspaceLoading: boolean;
  workspaceOptions: WorkspaceOption[];
};

// eslint-disable-next-line react-refresh/only-export-components -- The approved adapter contract exports this route helper from the component module.
export function isActiveSidebarPath(
  pathname: string,
  target: string,
  sections: NavSection[],
) {
  const targets = sections.flatMap((section) =>
    section.items.map((item) => item.to),
  );
  const matches = targets
    .filter(
      (candidate) =>
        pathname === candidate || pathname.startsWith(`${candidate}/`),
    )
    .sort((left, right) => right.length - left.length);
  return matches[0] === target;
}

function dedupeSidebarSections(sections: NavSection[]): NavSection[] {
  const seenPaths = new Set<string>();
  return sections
    .map((section) => ({
      ...section,
      items: section.items.filter((item) => {
        if (seenPaths.has(item.to)) return false;
        seenPaths.add(item.to);
        return true;
      }),
    }))
    .filter((section) => section.items.length > 0);
}

function getInitials(value: string): string {
  const parts = value
    .trim()
    .split(/[\s._@-]+/)
    .filter(Boolean);
  if (parts.length > 1) {
    return `${parts[0]?.[0] || ""}${parts[1]?.[0] || ""}`.toUpperCase();
  }
  return (parts[0] || "FS").slice(0, 2).toUpperCase();
}

function FlowStewardBrand({
  onExpandRail,
  railState,
}: Readonly<{ onExpandRail: () => void; railState: SidebarRailState }>) {
  const { t } = useI18n();
  const mark = (
    <FlowStewardMark
      className="fs-app-sidebar__brand-mark"
      testId="flow-steward-brand-mark"
    />
  );
  // Collapsed, the mark is also the way back: it expands the rail and never
  // collapses it, so a click on the brand cannot take the navigation away
  // (canon navigation/sidebar.md, 2026-10-06). No tooltip; the name is enough.
  if (railState === "collapsed") {
    return (
      <div className="fs-app-sidebar__brand">
        <button
          aria-label={t("react.shared.expand_sidebar", "Expand sidebar")}
          className="fs-app-sidebar__brand-expand"
          onClick={onExpandRail}
          type="button"
        >
          {mark}
        </button>
      </div>
    );
  }
  return (
    <div
      aria-label={t("shell.brand.orchestrator", "Flow Steward")}
      className="fs-app-sidebar__brand"
      role="img"
    >
      {mark}
      <span aria-hidden="true" className="fs-app-sidebar__brand-wordmark">
        Flow Steward
      </span>
    </div>
  );
}

type WorkspaceMenuProps = {
  error: string;
  loading: boolean;
  onOpenChange: (open: boolean) => void;
  onRetry: () => void;
  onSelect: (workspaceId: string) => void;
  open: boolean;
  options: WorkspaceOption[];
  railState: SidebarRailState;
  selectedId: string;
};

function WorkspaceMenu({
  error,
  loading,
  onOpenChange,
  onRetry,
  onSelect,
  open,
  options,
  railState,
  selectedId,
}: Readonly<WorkspaceMenuProps>) {
  const { t } = useI18n();
  const selected = options.find((option) => option.id === selectedId);
  // A session is always pointed at one workspace, so there is no aggregate name
  // to fall back to — an empty name here means the list has not arrived yet.
  const selectedName = selected?.displayName || t("project_mode.workspace", "Workspace");
  const selectedKey = selected?.workspaceKey || t("react.shared.workspace_scope", "Workspace scope");
  const tooltip = useSidebarTooltip(railState === "collapsed" && !open);

  return (
    <AppShellMenu
      label={t("project_mode.workspace", "Workspace")}
      menuLabel={t("react.shared.workspace_menu", "Workspace menu")}
      onOpenChange={onOpenChange}
      open={open}
      panelClassName="fs-app-sidebar__workspace-panel"
      triggerClassName="fs-app-sidebar__workspace-trigger"
      triggerInteractionProps={tooltip.triggerInteractionProps}
      triggerContent={
        <>
          <span aria-hidden="true" className="fs-app-sidebar__workspace-logo">
            {/* Until the workspace list resolves there is no name to take an
                initial from, so the generic mark stands in for it. */}
            {selected ? (
              workspaceInitial(selected.displayName || selected.workspaceKey)
            ) : (
              <GalleryVerticalEnd size={16} strokeWidth={1.5} />
            )}
          </span>
          <span className="fs-app-sidebar__workspace-meta">
            <span className="fs-app-sidebar__workspace-name">{selectedName}</span>
            <span className="fs-app-sidebar__workspace-key">{selectedKey}</span>
          </span>
          <ChevronsUpDown
            aria-hidden="true"
            className="fs-app-sidebar__workspace-chevron"
            size={16}
            strokeWidth={1.5}
          />
          <SidebarTooltip
            id={tooltip.tooltipId}
            label={selectedName}
            visible={tooltip.visible}
          />
        </>
      }
    >
      <p className="fs-shell-menu__label">{t("react.shared.workspaces", "Workspaces")}</p>
      {loading ? <p className="fs-shell-menu__status">{t("react.shared.loading_workspaces", "Loading workspaces…")}</p> : null}
      {error ? (
        <div className="fs-shell-menu__status">
          <p>{error}</p>
          <button
            className="fs-shell-menu__action"
            onClick={onRetry}
            type="button"
          >
            <RotateCcw aria-hidden="true" size={14} strokeWidth={1.5} />
            {t("react.shared.retry_workspaces", "Retry workspaces")} </button>
        </div>
      ) : null}
      {options.map((option) => (
        <button
          aria-label={option.displayName}
          aria-checked={option.id === selectedId}
          className="fs-shell-menu__item fs-app-sidebar__workspace-option"
          key={option.id}
          onClick={() => onSelect(option.id)}
          role="menuitemradio"
          type="button"
        >
          <span className="fs-shell-menu__workspace-initial" aria-hidden="true">
            {workspaceInitial(option.displayName || option.workspaceKey)}
          </span>
          <span className="fs-shell-menu__item-label">{option.displayName}</span>
          {option.id === selectedId ? (
            <Check aria-hidden="true" size={14} strokeWidth={1.5} />
          ) : (
            <span className="fs-shell-menu__item-shortcut">{option.workspaceKey}</span>
          )}
        </button>
      ))}
    </AppShellMenu>
  );
}

type AccountMenuProps = {
  loggingOut: boolean;
  onLogout: () => Promise<void>;
  onOpenChange: (open: boolean) => void;
  onThemeChange: (theme: "light" | "dark") => void;
  open: boolean;
  railState: SidebarRailState;
  themeMode: "light" | "dark";
  userDisplayName: string;
};

function AccountMenu({
  loggingOut,
  onLogout,
  onOpenChange,
  onThemeChange,
  open,
  railState,
  themeMode,
  userDisplayName,
}: Readonly<AccountMenuProps>) {
  const { t } = useI18n();
  const tooltip = useSidebarTooltip(railState === "collapsed" && !open);
  const accountInitials = getInitials(userDisplayName);
  return (
    <AppShellMenu
      label={t("react.shared.account_menu", "Account menu")}
      menuLabel={t("react.shared.account_menu", "Account menu")}
      onOpenChange={onOpenChange}
      open={open}
      panelClassName="fs-app-sidebar__account-panel"
      triggerClassName="fs-app-sidebar__account-trigger"
      triggerInteractionProps={tooltip.triggerInteractionProps}
      triggerContent={
        <>
          <span className="fs-app-sidebar__account-avatar" aria-hidden="true">
            {accountInitials}
          </span>
          <span className="fs-app-sidebar__account-meta">
            <span className="fs-app-sidebar__account-name">{userDisplayName}</span>
          </span>
          <ChevronsUpDown
            aria-hidden="true"
            className="fs-app-sidebar__account-chevron"
            size={16}
            strokeWidth={1.5}
          />
          <SidebarTooltip
            id={tooltip.tooltipId}
            label={userDisplayName}
            visible={tooltip.visible}
          />
        </>
      }
    >
      <div className="fs-shell-menu__identity">
        <span className="fs-app-sidebar__account-avatar" aria-hidden="true">
          {accountInitials}
        </span>
        <span className="fs-app-sidebar__account-meta">
          <span className="fs-app-sidebar__account-name">{userDisplayName}</span>
        </span>
      </div>
      <hr className="fs-shell-menu__separator" />
      <NavLink
        className="fs-shell-menu__item"
        role="menuitem"
        to="/settings/account"
      >
        <span className="fs-shell-menu__icon">
          <UserRound aria-hidden="true" size={16} strokeWidth={1.5} />
        </span>
        <span>{t("react.shared.account_profile", "Account profile")}</span>
      </NavLink>
      <NavLink
        className="fs-shell-menu__item"
        role="menuitem"
        to="/settings/users"
      >
        <span className="fs-shell-menu__icon">
          <Users aria-hidden="true" size={16} strokeWidth={1.5} />
        </span>
        <span>{t("react.shared.users_amp_access", "Users & access")}</span>
      </NavLink>
      <hr className="fs-shell-menu__separator" />
      <fieldset className="fs-shell-menu__section">
        <legend className="fs-shell-menu__label">{t("react.shared.appearance", "Appearance")}</legend>
        <button
          aria-checked={themeMode === "light"}
          aria-label={t("react.shared.light_theme", "Light theme")}
          className="fs-shell-menu__item"
          onClick={() => onThemeChange("light")}
          role="menuitemradio"
          type="button"
        >
          <span className="fs-shell-menu__icon">
            <Sun aria-hidden="true" size={16} strokeWidth={1.5} />
          </span>
          <span className="fs-shell-menu__item-label">{t("react.shared.light", "Light")}</span>
          {themeMode === "light" ? (
            <Check aria-hidden="true" size={14} strokeWidth={1.5} />
          ) : null}
        </button>
        <button
          aria-checked={themeMode === "dark"}
          aria-label={t("react.shared.dark_theme", "Dark theme")}
          className="fs-shell-menu__item"
          onClick={() => onThemeChange("dark")}
          role="menuitemradio"
          type="button"
        >
          <span className="fs-shell-menu__icon">
            <Moon aria-hidden="true" size={16} strokeWidth={1.5} />
          </span>
          <span className="fs-shell-menu__item-label">{t("react.shared.dark", "Dark")}</span>
          {themeMode === "dark" ? (
            <Check aria-hidden="true" size={14} strokeWidth={1.5} />
          ) : null}
        </button>
      </fieldset>
      <hr className="fs-shell-menu__separator" />
      <button
        className="fs-shell-menu__item fs-shell-menu__item--danger"
        disabled={loggingOut}
        onClick={() => {
          ignoreAsyncResult(onLogout());
        }}
        role="menuitem"
        type="button"
      >
        <LogOut aria-hidden="true" size={14} strokeWidth={1.5} />
        {loggingOut ? t("react.shared.logging_out", "Logging out…") : t("react.shared.log_out", "Log out")}
      </button>
    </AppShellMenu>
  );
}

function SidebarNavigationItem({
  active,
  item,
  railState,
}: Readonly<{
  active: boolean;
  item: NavEntry;
  railState: SidebarRailState;
}>) {
  const tooltip = useSidebarTooltip(railState === "collapsed");
  const Icon = item.icon;
  return (
    <NavLink
      {...tooltip.triggerInteractionProps}
      aria-label={railState === "collapsed" ? item.label : undefined}
      className="fs-app-sidebar__item"
      data-active={active ? "true" : "false"}
      end={!active}
      to={item.to}
    >
      <Icon aria-hidden="true" size={16} strokeWidth={1.5} />
      <span className="fs-app-sidebar__label">{item.label}</span>
      <SidebarTooltip
        id={tooltip.tooltipId}
        label={item.label}
        visible={tooltip.visible}
      />
    </NavLink>
  );
}

/**
 * The rail edge (canon navigation/sidebar.md): a 16px strip on the rail's
 * border that toggles it both ways, as Header's PanelLeft does. It is not a tab
 * stop; the keyboard has PanelLeft. It names itself with the collapsed rows'
 * tooltip at the pointer's height, after a 500ms delay, never a native title.
 */
function SidebarRailEdge({ onToggleRail }: Readonly<{ onToggleRail: () => void }>) {
  const { t } = useI18n();
  return (
    <button
      aria-label={t("react.shared.toggle_sidebar", "Toggle sidebar")}
      className="fs-app-sidebar__rail-edge"
      data-testid="sidebar-rail-edge"
      onClick={onToggleRail}
      onPointerMove={(event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        event.currentTarget.style.setProperty("--fs-rail-tip-y", `${event.clientY - rect.top}px`);
      }}
      tabIndex={-1}
      type="button"
    >
      <span aria-hidden="true" className="fs-app-sidebar__rail-tip">
        {t("react.shared.toggle_sidebar", "Toggle sidebar")} </span>
    </button>
  );
}

export function AppShellSidebar({
  loggingOut,
  onExpandRail,
  onLogout,
  onRetryWorkspaces,
  onThemeChange,
  onToggleRail,
  onWorkspaceChange,
  pathname,
  railState,
  sections,
  selectedWorkspaceId,
  themeMode,
  userDisplayName,
  workspaceError,
  workspaceLoading,
  workspaceOptions,
}: Readonly<AppShellSidebarProps>) {
  const { t } = useI18n();
  const [openMenu, setOpenMenu] = useState<"workspace" | "account" | null>(
    null,
  );
  const visibleSections = dedupeSidebarSections(sections);

  return (
    <aside
      aria-label={t("react.shared.application_sidebar", "Application sidebar")}
      className="fs-app-sidebar"
      data-rail-state={railState}
      data-testid="app-shell-sidebar"
    >
      <div className="fs-app-sidebar__brand-header">
        <FlowStewardBrand onExpandRail={onExpandRail} railState={railState} />
      </div>
      <div className="fs-app-sidebar__workspace-section">
        <WorkspaceMenu
          error={workspaceError}
          loading={workspaceLoading}
          onOpenChange={(open) => setOpenMenu(open ? "workspace" : null)}
          onRetry={onRetryWorkspaces}
          onSelect={onWorkspaceChange}
          open={openMenu === "workspace"}
          options={workspaceOptions}
          railState={railState}
          selectedId={selectedWorkspaceId}
        />
      </div>
      <nav aria-label={t("react.shared.primary", "Primary")} className="fs-app-sidebar__content">
        {visibleSections.map((section) => (
          <section className="fs-app-sidebar__group" key={section.key}>
            <p className="fs-app-sidebar__group-label">{section.label}</p>
            {section.items.map((item) => {
              const active = isActiveSidebarPath(
                pathname,
                item.to,
                visibleSections,
              );
              return (
                <SidebarNavigationItem
                  active={active}
                  item={item}
                  key={item.key}
                  railState={railState}
                />
              );
            })}
          </section>
        ))}
      </nav>
      <footer className="fs-app-sidebar__footer">
        <AccountMenu
          loggingOut={loggingOut}
          onLogout={onLogout}
          onOpenChange={(open) => setOpenMenu(open ? "account" : null)}
          onThemeChange={onThemeChange}
          open={openMenu === "account"}
          railState={railState}
          themeMode={themeMode}
          userDisplayName={userDisplayName}
        />
      </footer>
      <SidebarRailEdge onToggleRail={onToggleRail} />
    </aside>
  );
}
