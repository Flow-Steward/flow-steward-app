
import type { PropsWithChildren } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  Activity,
  Bot,
  Database,
  FileCode2,
  FolderKanban,
  FolderTree,
  LayoutDashboard,
  Network,
  Settings2,
  ShieldCheck,
  Trash2,
  Waypoints,
  Workflow,
  Wrench,
} from "lucide-react";
import { useLocation, useNavigate } from "react-router";

import { useAuth } from "@/lib/auth/useAuth";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { fetchJson } from "@/lib/http/client";
import { useI18n } from "@/lib/i18n/useI18n";
import { useEventStream } from "@/lib/realtime/useEventStream";
import {
  normalizeWorkspaceId,
  persistWorkspaceSelection,
  readStoredWorkspaceId,
  resolveWorkspaceSelection,
  WORKSPACE_SELECTED_EVENT,
  WORKSPACES_CHANGED_EVENT,
} from "@/lib/utils/workspace-selection";
import { breadcrumbsForPath } from "@/components/layout/app-shell-breadcrumbs";
import { AppShellHeader } from "@/components/layout/app-shell-header";
import { ShellLayout } from "@/components/layout/app-shell-layout";
import { MaintenanceBanner } from "@/components/layout/maintenance-banner";
import {
  readSidebarRailState,
  toggleSidebarRailState,
  type SidebarRailState,
  writeSidebarRailState,
} from "@/components/layout/app-shell-rail-state";
import { AppShellSidebar } from "@/components/layout/app-shell-sidebar";
import { AdvancedSearchDialog } from "@/components/search/advanced-search-dialog";
import {
  APPROVALS_REFRESH_EVENT,
  approvalJobId,
  dedupeEntries,
  defaultSearchRoute,
  normalizeNavigationEntries,
  normalizeThemeMode,
  normalizeWorkspaceOptions,
  persistThemeMode,
  readStoredThemeMode,
  requestAwaitingApprovalJobs,
  shellHumanizeLabel,
  shouldRefreshApprovalsFromEvent,
  type AwaitingApprovalJob,
  type GlobalSearchItem,
  type NavSection,
  type WorkspaceOption,
} from "@/components/layout/app-shell-helpers";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

export function AppShell({ children }: Readonly<PropsWithChildren>) {
  const auth = useAuth();
  // The workspace registry (/api/workspaces) is admin-only; skip the fetch for
  // non-admins so denied navigation doesn't emit a 403 on every page load.
  const canListWorkspaces = useIsAdmin();
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();
  const [loggingOut, setLoggingOut] = useState(false);
  const [searchPanelOpen, setSearchPanelOpen] = useState(false);
  const [approvalJobs, setApprovalJobs] = useState<AwaitingApprovalJob[]>([]);
  const [approvalLoading, setApprovalLoading] = useState(false);
  const [approvalLoaded, setApprovalLoaded] = useState(false);
  const [approvalError, setApprovalError] = useState("");
  const [approvalsOpen, setApprovalsOpen] = useState(false);
  const [railState, setRailState] = useState<SidebarRailState>(() =>
    readSidebarRailState(
      globalThis.window === undefined ? null : globalThis.localStorage,
      globalThis.window === undefined ? 1024 : globalThis.innerWidth,
    ),
  );
  const [themeMode, setThemeMode] = useState<"dark" | "light">(() => {
    if (globalThis.window === undefined) {
      return "dark";
    }
    const root = document.documentElement;
    const stored = readStoredThemeMode();
    if (stored) {
      return stored;
    }
    const attr = firstStringFromUnknownValue([root.dataset.theme, ""])
      .trim()
      .toLowerCase();
    if (attr === "light" || attr === "dark") {
      return attr;
    }
    return root.classList.contains("platform-light") ? "light" : "dark";
  });
  const [workspaceOptions, setWorkspaceOptions] = useState<WorkspaceOption[]>([]);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [workspaceError, setWorkspaceError] = useState("");
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState(() => readStoredWorkspaceId());
  const approvalsRealtimeRefreshTimer = useRef<ReturnType<typeof globalThis.setTimeout> | null>(null);
  const searchPanelOpenRef = useRef(false);
  const searchReturnFocusRef = useRef<HTMLElement | null>(null);
  const workspaceLoadSequence = useRef(0);
  const toggleRail = useCallback(() => {
    setRailState((current) => {
      const next = toggleSidebarRailState(current);
      writeSidebarRailState(globalThis.localStorage, next);
      return next;
    });
  }, []);
  const expandRail = useCallback(() => {
    writeSidebarRailState(globalThis.localStorage, "expanded");
    setRailState("expanded");
  }, []);

  const openGlobalSearchSurface = useCallback(() => {
    if (!searchPanelOpenRef.current) {
      searchReturnFocusRef.current = document.activeElement as HTMLElement | null;
    }
    searchPanelOpenRef.current = true;
    setApprovalsOpen(false);
    setSearchPanelOpen(true);
  }, []);

  const closeGlobalSearchSurface = useCallback(() => {
    searchPanelOpenRef.current = false;
    setSearchPanelOpen(false);
    // Closing restores focus to the Header trigger the dialog was opened from.
    const returnTarget = searchReturnFocusRef.current;
    searchReturnFocusRef.current = null;
    globalThis.requestAnimationFrame(() => returnTarget?.focus());
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        !(event.metaKey || event.ctrlKey) ||
        event.key.toLowerCase() !== "k"
      ) {
        return;
      }
      event.preventDefault();
      openGlobalSearchSurface();
    };
    globalThis.addEventListener("keydown", onKeyDown);
    return () => globalThis.removeEventListener("keydown", onKeyDown);
  }, [openGlobalSearchSurface]);

  const loadWorkspaces = useCallback(async () => {
    const requestSequence = workspaceLoadSequence.current + 1;
    workspaceLoadSequence.current = requestSequence;
    if (!canListWorkspaces) {
      setWorkspaceOptions([]);
      setWorkspaceError("");
      setWorkspaceLoading(false);
      return;
    }
    setWorkspaceLoading(true);
    try {
      const payload = await fetchJson<unknown>("/api/workspaces");
      if (workspaceLoadSequence.current !== requestSequence) return;
      const rows = normalizeWorkspaceOptions(payload, t);
      setWorkspaceOptions(rows);
      setSelectedWorkspaceId((current) =>
        resolveWorkspaceSelection(current, rows),
      );
      setWorkspaceError("");
    } catch {
      if (workspaceLoadSequence.current !== requestSequence) return;
      setWorkspaceError(t("react.projects.unable_to_load_workspaces", "Unable to load workspaces."));
    } finally {
      if (workspaceLoadSequence.current === requestSequence) {
        setWorkspaceLoading(false);
      }
    }
  }, [canListWorkspaces, t]);
  useEffect(() => {
    ignoreAsyncResult(loadWorkspaces());
  }, [loadWorkspaces]);
  useEffect(() => {
    persistWorkspaceSelection(selectedWorkspaceId);
  }, [selectedWorkspaceId]);
  useEffect(() => {
    const reloadWorkspaceOptions = () => {
      ignoreAsyncResult(loadWorkspaces());
    };
    const onWorkspaceSelected = (event: Event) => {
      const detail = (event as CustomEvent<{ workspaceId?: string }>).detail || {};
      const nextWorkspaceId = normalizeWorkspaceId(detail.workspaceId);
      setSelectedWorkspaceId((current) => (current === nextWorkspaceId ? current : nextWorkspaceId));
    };
    globalThis.addEventListener(WORKSPACES_CHANGED_EVENT, reloadWorkspaceOptions);
    globalThis.addEventListener(WORKSPACE_SELECTED_EVENT, onWorkspaceSelected as EventListener);
    return () => {
      globalThis.removeEventListener(WORKSPACES_CHANGED_EVENT, reloadWorkspaceOptions);
      globalThis.removeEventListener(WORKSPACE_SELECTED_EVENT, onWorkspaceSelected as EventListener);
    };
  }, [loadWorkspaces]);
  useEffect(() => {
    const root = document.documentElement;
    root.dataset.platformTheme = themeMode;
    root.dataset.theme = themeMode;
    root.classList.toggle("dark", themeMode === "dark");
    root.classList.toggle("platform-light", themeMode === "light");
    root.style.colorScheme = themeMode;
    persistThemeMode(themeMode);
  }, [themeMode]);
  useEffect(() => {
    const root = document.documentElement;
    const observer = new MutationObserver(() => {
      const attrTheme = normalizeThemeMode(root.dataset.theme);
      if (attrTheme && attrTheme !== themeMode) {
        setThemeMode(attrTheme);
      }
    });
    observer.observe(root, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => {
      observer.disconnect();
    };
  }, [themeMode]);

  const roles = useMemo(() => {
    const rawRoles = auth.session?.user?.roles || auth.session?.roles || [];
    return rawRoles
      .map((role) =>
        firstStringFromUnknownValue([role, ""])
          .trim()
          .toLowerCase(),
      )
      .filter(isTruthy);
  }, [auth.session]);

  const isAdmin = roles.includes("admin");
  const isOperator = roles.includes("operator");
  const hasRegistryAccess = isAdmin || isOperator || !auth.session?.user;
  const currentAccountId = firstStringFromUnknownValue([auth.session?.user?.account_id, auth.session?.account_memberships?.[0]?.account_id, ""]).trim();
  const canReadApprovals = Boolean(
    auth.session?.authenticated || auth.session?.user,
  );
  const refreshAwaitingApprovals = useCallback(async () => {
    if (!canReadApprovals) {
      setApprovalJobs([]);
      setApprovalLoaded(true);
      setApprovalError("");
      return;
    }
    setApprovalLoading(true);
    setApprovalError("");
    try {
      const jobs = await requestAwaitingApprovalJobs();
      setApprovalJobs(jobs);
      setApprovalLoaded(true);
    } catch (error) {
      setApprovalError(
        error instanceof Error ? error.message : "Failed to load approvals.",
      );
      setApprovalLoaded(true);
    } finally {
      setApprovalLoading(false);
    }
  }, [canReadApprovals]);

  useEffect(() => {
    if (globalThis.window === undefined) {
      return undefined;
    }
    ignoreAsyncResult(refreshAwaitingApprovals());
    const refreshOnSignal = () => {
      ignoreAsyncResult(refreshAwaitingApprovals());
    };
    const interval = globalThis.setInterval(refreshOnSignal, 30_000);
    globalThis.addEventListener("focus", refreshOnSignal);
    globalThis.addEventListener(APPROVALS_REFRESH_EVENT, refreshOnSignal);
    return () => {
      globalThis.clearInterval(interval);
      globalThis.removeEventListener("focus", refreshOnSignal);
      globalThis.removeEventListener(APPROVALS_REFRESH_EVENT, refreshOnSignal);
    };
  }, [currentAccountId, refreshAwaitingApprovals]);

  useEffect(() => {
    if (!approvalsOpen) {
      return;
    }
    ignoreAsyncResult(refreshAwaitingApprovals());
  }, [approvalsOpen, refreshAwaitingApprovals]);

  const scheduleRealtimeApprovalsRefresh = useCallback(() => {
    if (globalThis.window === undefined) {
      return;
    }
    if (approvalsRealtimeRefreshTimer.current !== null) {
      return;
    }
    approvalsRealtimeRefreshTimer.current = globalThis.setTimeout(() => {
      approvalsRealtimeRefreshTimer.current = null;
      ignoreAsyncResult(refreshAwaitingApprovals());
    }, 150);
  }, [refreshAwaitingApprovals]);

  const handleApprovalStreamMessage = useCallback(
    (event: MessageEvent<string>) => {
      try {
        const payload = JSON.parse(event.data || "{}");
        if (shouldRefreshApprovalsFromEvent(payload)) {
          scheduleRealtimeApprovalsRefresh();
        }
      } catch {
        // Ignore malformed keepalive frames; polling remains the fallback.
      }
    },
    [scheduleRealtimeApprovalsRefresh],
  );

  useEventStream("/api/events/stream", {
    enabled:
      canReadApprovals &&
      approvalsOpen &&
      globalThis.window !== undefined &&
      globalThis.EventSource !== undefined,
    onMessage: handleApprovalStreamMessage,
  });

  useEffect(() => {
    return () => {
      if (approvalsRealtimeRefreshTimer.current !== null) {
        globalThis.clearTimeout(approvalsRealtimeRefreshTimer.current);
        approvalsRealtimeRefreshTimer.current = null;
      }
    };
  }, []);

  const injectedNavigation = auth.session?.shell_navigation || {};
  const injectedSystem = normalizeNavigationEntries(
    injectedNavigation.system || [],
  );
  const injectedSections = Object.entries(injectedNavigation)
    .filter(([section]) => !["", "system"].includes(firstStringFromUnknownValue([section, ""]).trim()))
    .map(([section, items]) => {
      const key = firstStringFromUnknownValue([section, ""]).trim();
      return {
        key,
        label: shellHumanizeLabel(key),
        items: dedupeEntries(normalizeNavigationEntries(items || [])),
      } satisfies NavSection;
    })
    .filter((section) => section.items.length > 0);

  const mainSection: NavSection = {
    key: "main",
    label: t("shell.nav.main", "Main"),
    items: [
      {
        key: "dashboard",
        to: "/dashboard",
        label: t("shell.nav.dashboard", "Dashboard"),
        icon: LayoutDashboard,
      },
      {
        key: "projects",
        to: "/projects",
        label: t("shell.nav.projects", "Projects"),
        icon: FolderKanban,
      },
      {
        key: "jobs",
        to: "/jobs",
        label: t("shell.nav.jobs", "Jobs"),
        icon: Network,
      },
    ],
  };

  const registrySection: NavSection | null = hasRegistryAccess
    ? {
        key: "registry",
        label: t("shell.nav.registry", "Registry"),
        items: [
          {
            key: "registry_agents",
            to: "/registry/agents",
            label: t("shell.nav.agents", "Agents"),
            icon: Bot,
          },
          {
            key: "registry_mcp",
            to: "/registry/mcp",
            label: "MCP",
            icon: Network,
          },
          {
            key: "registry_tools",
            to: "/registry/tools",
            label: t("shell.nav.tools", "Tools"),
            icon: Wrench,
          },
          {
            key: "registry_skills",
            to: "/registry/skills",
            label: t("shell.nav.skills", "Skills"),
            icon: Waypoints,
          },
          {
            key: "registry_workflows",
            to: "/registry/workflows",
            label: t("shell.nav.workflows", "Workflows"),
            icon: Workflow,
          },
          {
            key: "registry_guardrails",
            to: "/registry/guardrails",
            label: t("shell.nav.guardrails", "Guardrails"),
            icon: ShieldCheck,
          },
          {
            key: "registry_workspaces",
            to: "/registry/workspaces",
            label: t("shell.nav.workspaces", "Workspaces"),
            icon: FolderTree,
          },
          {
            key: "registry_datasets",
            to: "/registry/datasets",
            label: t("shell.nav.datasets", "Datasets"),
            icon: Database,
          },
          {
            key: "registry_data_connections",
            to: "/registry/data-connections",
            label: t("shell.nav.data_connections", "Data Connections"),
            icon: Network,
          },
        ],
      }
    : null;

  const systemSection: NavSection | null = isAdmin
    ? {
        key: "system",
        label: t("shell.nav.system", "System"),
        items: dedupeEntries([
          {
            key: "monitoring",
            to: "/monitoring",
            label: t("shell.nav.monitoring", "Monitoring"),
            icon: Activity,
          },
          {
            key: "settings_home",
            to: "/settings",
            label: t("shell.nav.settings", "Settings"),
            icon: Settings2,
          },
          {
            key: "extensions",
            to: "/extensions",
            label: t("shell.nav.extensions", "Extensions"),
            icon: FileCode2,
          },
          {
            key: "trash",
            to: "/trash",
            label: t("shell.nav.trash", "Trash"),
            icon: Trash2,
          },
          ...injectedSystem,
        ]),
      }
    : null;

  const sidebarSections = [mainSection, registrySection, systemSection, ...injectedSections]
    .filter((section): section is NavSection => section !== null)
    .filter((section) => section.items.length > 0);
  const routeLabels = useMemo(
    () => {
      const sidebarRouteLabels = sidebarSections.flatMap((section) =>
          section.items.map((item) => [item.to, item.label] as const),
        );
      const settingsRouteLabels = (auth.session?.settings_pages ?? []).map(
        (page) =>
          [
            page.path,
            firstStringFromUnknownValue([
              page.title,
              page.nav_label,
              page.page_key,
            ]),
          ] as const,
      );
      const registeredRouteLabels = (auth.session?.registered_pages ?? []).map(
        (page) =>
          [
            page.path,
            firstStringFromUnknownValue([
              page.title,
              page.label,
              page.nav_label,
              page.page_key,
            ]),
          ] as const,
      );
      return new Map([
        ...sidebarRouteLabels,
        ...settingsRouteLabels,
        ...registeredRouteLabels,
      ]);
    },
    [auth.session?.registered_pages, auth.session?.settings_pages, sidebarSections],
  );
  const breadcrumbs = useMemo(
    () => breadcrumbsForPath(location.pathname, routeLabels, t),
    [location.pathname, routeLabels, t],
  );

  const pendingApprovals = approvalJobs.length;

  function openApprovalJob(job: AwaitingApprovalJob) {
    const jobId = approvalJobId(job);
    if (!jobId) return;
    setApprovalsOpen(false);
    ignoreAsyncResult(navigate(`/jobs/${encodeURIComponent(jobId)}`));
  }

  async function handleLogout() {
    setLoggingOut(true);
    try {
      await auth.logout();
      ignoreAsyncResult(navigate("/login", { replace: true }));
    } finally {
      setLoggingOut(false);
    }
  }

  function openSearchResult(item: GlobalSearchItem) {
    const deepLink = firstStringFromUnknownValue([item.deep_link, ""]).trim();
    const target = deepLink.startsWith("/")
      ? deepLink
      : defaultSearchRoute(item);
    searchPanelOpenRef.current = false;
    searchReturnFocusRef.current = null;
    setSearchPanelOpen(false);
    setApprovalsOpen(false);
    ignoreAsyncResult(navigate(target));
  }

  function handleWorkspaceSelectionChange(workspaceId: string) {
    const nextWorkspaceId = normalizeWorkspaceId(workspaceId);
    setSelectedWorkspaceId((current) => (current === nextWorkspaceId ? current : nextWorkspaceId));
  }

  // The workspace registry is admin-only, so a non-admin gets no selectable option set and
  // the active workspace applies as non-removable request context instead.
  const searchWorkspaceOptions = useMemo(
    () =>
      workspaceOptions.map((workspace) => ({
        value: workspace.id,
        label: workspace.displayName || workspace.workspaceKey || workspace.id,
      })),
    [workspaceOptions],
  );
  const userDisplayName =
    auth.session?.user?.display_name ||
    auth.session?.user?.username ||
    t("shell.header.unknown_user", "Unknown user");
  const header = (
    <AppShellHeader
      approvalError={approvalError}
      approvalJobs={approvalJobs}
      approvalLoaded={approvalLoaded}
      approvalLoading={approvalLoading}
      approvalsOpen={approvalsOpen}
      breadcrumbs={breadcrumbs}
      onOpenApprovalJob={openApprovalJob}
      onOpenSearch={openGlobalSearchSurface}
      onRetryApprovals={() => ignoreAsyncResult(refreshAwaitingApprovals())}
      onToggleApprovals={() => setApprovalsOpen((current) => !current)}
      onToggleRail={toggleRail}
      pendingApprovals={pendingApprovals}
      railExpanded={railState === "expanded"}
    />
  );
  const searchOverlay = searchPanelOpen ? (
    <AdvancedSearchDialog
      canSearchAccountWide={canListWorkspaces}
      onClose={closeGlobalSearchSurface}
      onOpenResult={openSearchResult}
      open={searchPanelOpen}
      selectedWorkspaceId={selectedWorkspaceId}
      workspaceOptions={searchWorkspaceOptions}
    />
  ) : null;
  const sidebar = (
    <AppShellSidebar
      loggingOut={loggingOut}
      onExpandRail={expandRail}
      onLogout={handleLogout}
      onRetryWorkspaces={() => ignoreAsyncResult(loadWorkspaces())}
      onThemeChange={setThemeMode}
      onToggleRail={toggleRail}
      onWorkspaceChange={handleWorkspaceSelectionChange}
      pathname={location.pathname}
      railState={railState}
      sections={sidebarSections}
      selectedWorkspaceId={selectedWorkspaceId}
      themeMode={themeMode}
      userDisplayName={userDisplayName}
      workspaceError={workspaceError}
      workspaceLoading={workspaceLoading}
      workspaceOptions={workspaceOptions}
    />
  );

  return (
    <div className="app-shell-root" data-shell-theme={themeMode}>
      <ShellLayout
        header={header}
        maintenance={<MaintenanceBanner />}
        railState={railState}
        sidebar={sidebar}
      >
        {children}
      </ShellLayout>

      {searchOverlay}
    </div>
  );
}
