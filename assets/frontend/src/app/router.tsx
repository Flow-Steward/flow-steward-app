import { createElement, lazy, Suspense, useState, type ComponentType } from "react";
import { createBrowserRouter, RouterProvider, Navigate, Outlet, Route, Routes, useLocation } from "react-router";

import { AppShell } from "@/components/layout/AppShell";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { PagePlaceholder } from "@/components/shared/page-placeholder";
import { registeredPageComponents } from "@/app/extensions";
import { RequireAdmin, RequireAuth, RequireCapability } from "@/lib/auth/guards";
import { useAuth } from "@/lib/auth/useAuth";
import { useI18n } from "@/lib/i18n/useI18n";
import { BootstrapPage } from "@/pages/auth/bootstrap";
import { LoginPage } from "@/pages/auth/login";
import { PublicAuthMotionLayout } from "@/pages/auth/public-auth-motion";
import { ResetPasswordPage } from "@/pages/auth/reset-password";
import { DashboardPage } from "@/pages/dashboard";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { trimRepeatedEdges } from "@/lib/utils/string-normalization";

function lazyNamed<T extends Record<string, unknown>, K extends keyof T>(
  loader: () => Promise<T>,
  exportName: K,
) {
  return lazy(async () => ({ default: (await loader())[exportName] as ComponentType }));
}

const ExtensionsHubPage = lazyNamed(() => import("@/pages/extensions"), "ExtensionsHubPage");
const JobsDetailPage = lazyNamed(() => import("@/pages/jobs/detail"), "JobsDetailPage");
const JobsListPage = lazyNamed(() => import("@/pages/jobs/list"), "JobsListPage");
const MonitoringPage = lazyNamed(() => import("@/pages/monitoring"), "MonitoringPage");
const ProjectsDetailPage = lazyNamed(() => import("@/pages/projects/detail"), "ProjectsDetailPage");
const ProjectsListPage = lazyNamed(() => import("@/pages/projects/list"), "ProjectsListPage");
const RegistryAgentsPage = lazyNamed(() => import("@/pages/registry/agents"), "RegistryAgentsPage");
const RegistryAgentsDetailPage = lazyNamed(
  () => import("@/pages/registry/agents/detail"),
  "RegistryAgentsDetailPage",
);
const RegistryChangelogPage = lazyNamed(
  () => import("@/pages/registry/changelog"),
  "RegistryChangelogPage",
);
const RegistryDataContractsPage = lazyNamed(
  () => import("@/pages/registry/data-contracts"),
  "RegistryDataContractsPage",
);
const RegistryDataContractsDetailPage = lazyNamed(
  () => import("@/pages/registry/data-contracts/detail"),
  "RegistryDataContractsDetailPage",
);
const RegistryDataConnectionsPage = lazyNamed(
  () => import("@/pages/registry/data-connections"),
  "RegistryDataConnectionsPage",
);
const RegistryDatasetsPage = lazyNamed(
  () => import("@/pages/registry/datasets"),
  "RegistryDatasetsPage",
);
const RegistryDatasetsDetailPage = lazyNamed(
  () => import("@/pages/registry/datasets/detail"),
  "RegistryDatasetsDetailPage",
);
const RegistryFieldsPage = lazyNamed(() => import("@/pages/registry/fields"), "RegistryFieldsPage");
const RegistryFieldsDetailPage = lazyNamed(
  () => import("@/pages/registry/fields/detail"),
  "RegistryFieldsDetailPage",
);
const RegistryGuardrailsPage = lazyNamed(
  () => import("@/pages/registry/guardrails"),
  "RegistryGuardrailsPage",
);
const RegistryGuardrailsDetailPage = lazyNamed(
  () => import("@/pages/registry/guardrails/detail"),
  "RegistryGuardrailsDetailPage",
);
const RegistryMcpDetailPage = lazyNamed(
  () => import("@/pages/registry/mcp/detail"),
  "RegistryMcpDetailPage",
);
const RegistryMcpPage = lazyNamed(() => import("@/pages/registry/mcp"), "RegistryMcpPage");
const RegistryPublishedBlocksPage = lazyNamed(
  () => import("@/pages/registry/published-blocks"),
  "RegistryPublishedBlocksPage",
);
const RegistryPublishedBlocksDetailPage = lazyNamed(
  () => import("@/pages/registry/published-blocks/detail"),
  "RegistryPublishedBlocksDetailPage",
);
const RegistrySkillsPage = lazyNamed(() => import("@/pages/registry/skills"), "RegistrySkillsPage");
const RegistrySkillsDetailPage = lazyNamed(
  () => import("@/pages/registry/skills/detail"),
  "RegistrySkillsDetailPage",
);
const RegistryToolsPage = lazyNamed(() => import("@/pages/registry/tools"), "RegistryToolsPage");
const RegistryToolsDetailPage = lazyNamed(
  () => import("@/pages/registry/tools/detail"),
  "RegistryToolsDetailPage",
);
const RegistryWorkspacesPage = lazyNamed(
  () => import("@/pages/registry/workspaces"),
  "RegistryWorkspacesPage",
);
const RegistryWorkspacesDetailPage = lazyNamed(
  () => import("@/pages/registry/workspaces/detail"),
  "RegistryWorkspacesDetailPage",
);
const SettingsAccountPage = lazyNamed(() => import("@/pages/settings/account"), "SettingsAccountPage");
const SettingsHubPage = lazyNamed(() => import("@/pages/settings/hub"), "SettingsHubPage");
const SettingsSecurityPage = lazyNamed(() => import("@/pages/settings/security"), "SettingsSecurityPage");
const SettingsUsersPage = lazyNamed(() => import("@/pages/settings/users"), "SettingsUsersPage");
const TrashPage = lazyNamed(() => import("@/pages/trash"), "TrashPage");
const WorkflowsImportPage = lazyNamed(() => import("@/pages/workflows/import"), "WorkflowsImportPage");
const WorkflowsListPage = lazyNamed(() => import("@/pages/workflows/list"), "WorkflowsListPage");
const WorkflowsOverviewPage = lazyNamed(
  () => import("@/pages/workflows/overview"),
  "WorkflowsOverviewPage",
);
const WorkflowsProjectsPage = lazyNamed(
  () => import("@/pages/workflows/projects"),
  "WorkflowsProjectsPage",
);

const WorkflowsBuilderPageLazy = lazy(async () => {
  const module = await import("@/pages/workflows/builder");
  return { default: module.WorkflowsBuilderPage };
});

export function HomeGate() {
  const auth = useAuth();
  const { t } = useI18n();

  if (auth.status === "loading") {
    return <PageLoadingState label={t("react.routing.restoring_session", "Restoring your session")} standalone />;
  }

  if (auth.session?.bootstrap_required) {
    return <Navigate replace to="/bootstrap" />;
  }

  // A signed-in user lands on the Dashboard, the overview built on live data.
  return <Navigate replace to={auth.isAuthenticated ? "/dashboard" : "/login"} />;
}

function ProtectedLayout() {
  const { t } = useI18n();
  return (
    <RequireAuth>
      <AppShell>
        <Suspense fallback={<PageLoadingState label={t("react.routing.preparing_page", "Preparing this page")} />}>
          <Outlet />
        </Suspense>
      </AppShell>
    </RequireAuth>
  );
}

function ShellNotFound() {
  const location = useLocation();
  const { t } = useI18n();

  return (
    <PagePlaceholder
      eyebrow={t("react.routing.page_unavailable", "Page unavailable")}
      title={t("react.routing.page_not_available", "This page is not available.")}
      description={t("react.routing.screen_not_found", 'We could not find a screen for "{path}".', { path: location.pathname })}
      status={t("react.routing.unavailable", "Unavailable")}
    />
  );
}

function normalizeRoutePath(path: string) {
  const normalized = `/${trimRepeatedEdges(firstStringFromUnknownValue([path, ""]).trim(), "/")}`;
  return normalized === "/" ? "/" : normalized;
}

function RegisteredPageRoute() {
  const auth = useAuth();
  const { t } = useI18n();
  const location = useLocation();
  const currentPath = normalizeRoutePath(location.pathname);
  const page = (auth.session?.registered_pages || []).find(
    (candidate) => normalizeRoutePath(candidate.path) === currentPath,
  );

  if (!page) {
    return <ShellNotFound />;
  }

  const componentKey = firstStringFromUnknownValue([page.component_key, ""]).trim();
  const Component = componentKey ? registeredPageComponents[componentKey] : undefined;
  if (!Component) {
    return (
      <PagePlaceholder
        eyebrow={t("react.routing.page_unavailable", "Page unavailable")}
        title={t("react.routing.page_not_available", "This page is not available.")}
        description={t("react.routing.registered_screen_missing", "The registered screen is not included in this frontend build.")}
        status={t("react.routing.unavailable", "Unavailable")}
      />
    );
  }

  return createElement(Component, { page });
}

function SettingsDynamicRoute() {
  const auth = useAuth();
  const location = useLocation();
  const currentPath = normalizeRoutePath(location.pathname);
  const hasRegisteredPage = (auth.session?.registered_pages || []).some(
    (candidate) => normalizeRoutePath(candidate.path) === currentPath,
  );

  if (hasRegisteredPage) {
    return <RegisteredPageRoute />;
  }
  const hasSettingsPage = (auth.session?.settings_pages || []).some(
    (candidate) => normalizeRoutePath(candidate.path) === currentPath,
  );
  return hasSettingsPage ? <SettingsHubPage /> : <ShellNotFound />;
}

export function AppRouter() {
  const configuredBase = (import.meta.env.BASE_URL || "/app/").replace(/\/$/, "") || "/app";
  const pathname = globalThis.window === undefined ? "/app" : globalThis.location.pathname;
  const basename = pathname === "/app" || pathname.startsWith("/app/") ? configuredBase : "";

  const [router] = useState(() => createBrowserRouter(
    [{ path: "*", element: <AppRoutes /> }],
    { basename },
  ));
  return <RouterProvider router={router} />;
}

function AppRoutes() {
  const { t } = useI18n();
  return (
      <Routes>
        <Route index element={<HomeGate />} />
        <Route path="bootstrap" element={<BootstrapPage />} />
        <Route element={<PublicAuthMotionLayout />}>
          <Route path="login" element={<LoginPage />} />
          <Route path="forgot-password" element={<ResetPasswordPage />} />
          <Route path="reset-password" element={<ResetPasswordPage />} />
        </Route>

        <Route
          path="registry/workflows/:workflowId/builder"
          element={
            <RequireAdmin>
              <Suspense fallback={<PageLoadingState label={t("react.routing.preparing_builder", "Preparing the workflow builder")} standalone />}>
                <WorkflowsBuilderPageLazy />
              </Suspense>
            </RequireAdmin>
          }
        />

        <Route element={<ProtectedLayout />}>
          <Route path="dashboard" element={<DashboardPage />} />
          <Route path="extensions" element={<RequireCapability capability="administer_platform"><ExtensionsHubPage /></RequireCapability>} />
          <Route path="extensions/:extensionId" element={<ExtensionsHubPage />} />
          <Route path="security" element={<RequireCapability capability="administer_platform"><SettingsSecurityPage /></RequireCapability>} />
          <Route path="security/projects/:projectId" element={<RequireCapability capability="administer_platform"><SettingsSecurityPage /></RequireCapability>} />
          <Route path="jobs">
            <Route index element={<JobsListPage />} />
            <Route path=":jobId" element={<JobsDetailPage />} />
          </Route>

          <Route path="projects">
            <Route index element={<ProjectsListPage />} />
            <Route path="new" element={<ShellNotFound />} />
            <Route path=":projectId" element={<ProjectsDetailPage />} />
            {/* The Notifications tab is a read-only overview; subscriptions are edited here. */}
            <Route path=":projectId/kb" element={<ShellNotFound />} />
          </Route>

          <Route path="settings">
            {/* The hub is the frame for every settings tab: it owns the header and
                the tab row and picks the panel from the path, so switching tabs
                keeps the row in place instead of leaving the hub. */}
            <Route element={<RequireCapability capability="administer_platform"><SettingsHubPage /></RequireCapability>}>
              <Route index />
              <Route path="general" />
              <Route path="providers" />
              <Route path="service-accounts" />
              <Route path="security" />
              <Route path="retention" />
              <Route path="notifications" />
            </Route>
            <Route path="account" element={<SettingsAccountPage />} />
            <Route path="users" element={<RequireCapability capability="administer_platform"><SettingsUsersPage /></RequireCapability>} />
            <Route path=":settingsPageKey" element={<RequireCapability capability="administer_platform"><SettingsDynamicRoute /></RequireCapability>} />
          </Route>

          <Route path="monitoring" element={<RequireCapability capability="administer_platform"><MonitoringPage /></RequireCapability>} />
          <Route path="trash" element={<RequireCapability capability="administer_platform"><TrashPage /></RequireCapability>} />

          <Route path="registry" element={<RequireCapability capability="read_registry"><Outlet /></RequireCapability>}>
            <Route index element={<Navigate replace to="agents" />} />
            <Route path="changelog" element={<RegistryChangelogPage />} />
            <Route path="agents" element={<RegistryAgentsPage />} />
            <Route path="agents/:agentId" element={<RegistryAgentsDetailPage />} />
            <Route path="mcp" element={<RegistryMcpPage />} />
            <Route path="mcp/:connectorId" element={<RegistryMcpDetailPage />} />
            <Route path="tools" element={<RegistryToolsPage />} />
            <Route path="tools/:toolId" element={<RegistryToolsDetailPage />} />
            <Route path="skills" element={<RegistrySkillsPage />} />
            <Route path="skills/:skillId" element={<RegistrySkillsDetailPage />} />
            <Route path="guardrails" element={<RegistryGuardrailsPage />} />
            <Route path="guardrails/:guardrailId" element={<RegistryGuardrailsDetailPage />} />
            <Route path="data-connections" element={<RegistryDataConnectionsPage />} />
            <Route path="datasets" element={<RegistryDatasetsPage />} />
            <Route path="datasets/:datasetId" element={<RequireCapability capability="administer_platform"><RegistryDatasetsDetailPage /></RequireCapability>} />
            <Route path="workspaces" element={<RegistryWorkspacesPage />} />
            <Route path="workspaces/:workspaceId" element={<RequireCapability capability="administer_platform"><RegistryWorkspacesDetailPage /></RequireCapability>} />
            <Route path="data-contracts" element={<RegistryDataContractsPage />} />
            <Route path="data-contracts/:contractId" element={<RegistryDataContractsDetailPage />} />
            <Route path="fields" element={<RegistryFieldsPage />} />
            <Route path="fields/:fieldKey" element={<RegistryFieldsDetailPage />} />
            <Route path="published-blocks" element={<RegistryPublishedBlocksPage />} />
            <Route path="published-blocks/:blockKey" element={<RegistryPublishedBlocksDetailPage />} />
          </Route>

          <Route path="registry/workflows" element={<RequireCapability capability="read_registry"><Outlet /></RequireCapability>}>
            <Route index element={<WorkflowsListPage />} />
            <Route
              path="import"
              element={
                <RequireAdmin>
                  <WorkflowsImportPage />
                </RequireAdmin>
              }
            />
            <Route path=":workflowId" element={<WorkflowsOverviewPage />} />
            <Route path=":workflowId/projects" element={<WorkflowsProjectsPage />} />
          </Route>

          <Route path="*" element={<RegisteredPageRoute />} />
        </Route>

        <Route
          path="*"
          element={
            <main className="min-h-screen bg-slate-950 px-6 py-10 text-white">
              <div className="mx-auto flex max-w-6xl flex-col gap-6">
                <ShellNotFound />
              </div>
            </main>
          }
        />
      </Routes>
  );
}
