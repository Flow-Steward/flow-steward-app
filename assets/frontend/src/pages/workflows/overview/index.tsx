import { useI18n } from "@/lib/i18n/useI18n";
import { lazy, Suspense, useCallback, useMemo, useState, type ReactNode } from "react";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router";

import { DsAlert, DsBadge, DsMetricCard, DsSelect, DsTable, PageContainer } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { useCanReadWorkflowRegistry, useIsAdmin } from "@/lib/auth/use-is-admin";
import { isHttpError } from "@/lib/http/errors";
import { queryKeys } from "@/lib/query/keys";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import {
  fetchWorkflowDetail,
  fetchWorkflowStats,
  fetchWorkflowValidation,
  type WorkflowStats,
} from "@/pages/workflows/shared/api";
import { useWorkflowHeaderMutations } from "@/pages/workflows/shared/header-actions";
import { WorkflowHeaderMutationDialogs } from "@/pages/workflows/shared/header-mutation-dialogs";
import { WorkflowSummaryHeader } from "@/pages/workflows/shared/header";
import type { ProjectListItem } from "@/pages/projects/list/api";
import { fetchWorkflowLinkedProjects } from "@/pages/workflows/shared/project-context";
import { declaredWorkflowHooks, useWorkflowHookStates, type WorkflowHookState } from "@/pages/workflows/shared/workflow-hooks";
import {
  isWorkflowSettingsTab,
  savedWorkflowChannelState,
  useChannelTabFallback,
  workflowChannelTabHiddenFor,
  workflowPageTabsForDetail,
  type WorkflowChannelState,
} from "@/pages/workflows/shared/workflow-page-tabs";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

/** The formatters answer `—` when there is no number; the card takes that as
 *  `unavailable` rather than printing a dash of its own. */
const NO_VALUE = "—";
const WorkflowSettingsSurface = lazy(() => import("@/pages/workflows/detail/workflow-settings-surface"));

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return `${(value * 100).toFixed(1)}%`;
}

function formatCurrency(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  if (value === 0) {
    return "$0.00";
  }
  if (value < 0.01) {
    return `$${value.toFixed(4)}`;
  }
  return `$${value.toFixed(2)}`;
}

function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return new Intl.NumberFormat().format(value);
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return "—";
  }
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) {
    return `${total}s`;
  }
  const minutes = Math.floor(total / 60);
  if (minutes < 60) {
    return `${minutes}m ${total % 60}s`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours}h ${minutes % 60}m`;
  }
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function workflowStepCount(workflow: unknown): number {
  if (!workflow || typeof workflow !== "object") return 0;
  const steps = (workflow as { steps?: unknown }).steps;
  return Array.isArray(steps) ? steps.length : 0;
}

function WorkflowAccessDenied() {
  const { t } = useI18n();
  return (
    <section className="fs-page-sections">
      <DsAlert title={t("react.workflows.workflow_catalog_is_restricted", "Workflow catalog is restricted")} tone="warning">
        {t("react.workflows.viewing_workflow_definitions_requires_an_operator_or_admin_role_ask_an_administrator_if_you_need_access", "Viewing workflow definitions requires an operator or admin role. Ask an administrator if you need access.")}</DsAlert>
      <Link className="fs-button" to="/jobs">
        {t("react.workflows.go_to_jobs", "Go to jobs")}<ArrowRight aria-hidden="true" />
      </Link>
    </section>
  );
}

function WorkflowLoadError({ error }: Readonly<{ error: unknown }>) {
  const { t } = useI18n();
  const workflowMissing = isHttpError(error) && error.status === 404;
  const errorMessage = error instanceof Error
    ? error.message
    : t("react.workflows.failed_to_load_workflow_summary", "Failed to load workflow summary.");
  return (
    <section className="fs-page-sections">
      <DsAlert
        title={workflowMissing ? t("react.workflows.workflow_not_found", "Workflow not found") : t("react.workflows.workflow_unavailable", "Workflow unavailable")}
        tone="danger"
      >
        <p>
          {workflowMissing
            ? t("react.workflows.this_workflow_was_deleted_or_is_no_longer_available_in_this_workspace", "This workflow was deleted or is no longer available in this workspace.")
            : t("react.workflows.workflow_summary_could_not_be_loaded_right_now", "Workflow summary could not be loaded right now.")}
        </p>
        <p>{errorMessage}</p>
      </DsAlert>
      <Link className="fs-button" to="/registry/workflows">
        {t("react.workflows.back_to_workflow_catalog", "Back to workflow catalog")}<ArrowRight aria-hidden="true" />
      </Link>
    </section>
  );
}

function WorkflowStatsPanel({
  error,
  isError,
  isLoading,
  jobsLink,
  stats,
  userTimezone,
}: Readonly<{
  error: unknown;
  isError: boolean;
  isLoading: boolean;
  jobsLink: string;
  stats: WorkflowStats | undefined;
  userTimezone: string;
}>) {
  const { t } = useI18n();
  const lastRunDisplay = stats?.last_run_at
    ? formatUserDateTime(stats.last_run_at, userTimezone)
    : "—";
  return (
    <article className="fs-card">
      <header className="fs-card-header flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="fs-card-title">{t("react.workflows.run_statistics", "Run statistics")}</h2>
          <p className="fs-card-description">
            {t("react.workflows.aggregated_job_activity_and_cost_across_all_projects_using_this_workflow", "Aggregated job activity and cost across all projects using this workflow.")}</p>
        </div>
        <Link className="fs-button" data-workflow-overview-action="view-jobs" to={jobsLink}>
          {t("react.workflows.view_jobs", "View jobs")}<ArrowRight aria-hidden="true" />
        </Link>
      </header>
      <div className="fs-card-body">
        {isLoading ? (
          <div className="fs-table-state">
            <strong>{t("react.workflows.loading_stats", "Loading stats")}</strong>
            <span>{t("react.workflows.the_statistics_will_remain_in_this_layout", "The statistics will remain in this layout.")}</span>
          </div>
        ) : null}
        {isError ? (
          <DsAlert title={t("react.workflows.failed_to_load_workflow_stats_variant_fe059c63", "Failed to load workflow stats")} tone="danger">
            {error instanceof Error ? error.message : t("react.workflows.failed_to_load_workflow_stats", "Failed to load workflow stats.")}
          </DsAlert>
        ) : null}
        {!isLoading && !isError && stats ? (
          <div className="fs-metric-grid" data-columns="4">
            <DsMetricCard density="standard" figure={formatNumber(stats.total_runs)} label={t("react.workflows.total_runs", "Total runs")} meta={stats.active_count ? t("react.workflows.active_now_count", "{count} active now", { count: formatNumber(stats.active_count) }) : null} unavailable={formatNumber(stats.total_runs) === NO_VALUE} wave={false} />
            <DsMetricCard attention={Number(stats.total_runs) > 0 && stats.success_rate !== null && stats.success_rate !== undefined && stats.success_rate < 0.6} density="standard" figure={formatPercent(stats.success_rate)} label={t("react.workflows.success_rate", "Success rate")} meta={t("react.workflows.completed_failed_counts", "{completed} completed / {failed} failed", { completed: formatNumber(stats.completed_count), failed: formatNumber(stats.failed_count) })} unavailable={formatPercent(stats.success_rate) === NO_VALUE} wave={false} />
            <DsMetricCard density="standard" figure={formatCurrency(stats.total_cost_usd)} label={t("react.workflows.total_cost", "Total cost")} meta={stats.jobs_with_cost ? t("react.workflows.jobs_with_cost_data_count", "{count} jobs with cost data", { count: formatNumber(stats.jobs_with_cost) }) : t("react.workflows.no_cost_data_recorded", "No cost data recorded")} unavailable={formatCurrency(stats.total_cost_usd) === NO_VALUE} wave={false} />
            <DsMetricCard density="standard" figure={formatCurrency(stats.avg_cost_per_run_usd)} label={t("react.workflows.avg_cost_run", "Avg cost / run")} meta={stats.total_runs ? t("react.workflows.across_runs_count", "Across {count} runs", { count: formatNumber(stats.total_runs) }) : null} unavailable={formatCurrency(stats.avg_cost_per_run_usd) === NO_VALUE} wave={false} />
            <DsMetricCard density="standard" figure={formatDuration(stats.avg_duration_seconds)} label={t("react.workflows.avg_duration", "Avg duration")} meta={t("react.workflows.finished_runs_only", "Finished runs only")} unavailable={formatDuration(stats.avg_duration_seconds) === NO_VALUE} wave={false} />
            <DsMetricCard density="standard" figure={lastRunDisplay} label={t("react.workflows.last_run", "Last run")} unavailable={lastRunDisplay === NO_VALUE} wave={false} />
            <DsMetricCard density="standard" figure={formatNumber(stats.active_count)} label={t("users.status.active", "Active")} meta={t("react.workflows.active_statuses_hint", "Pending, in progress, or awaiting approval")} unavailable={formatNumber(stats.active_count) === NO_VALUE} wave={false} />
            <DsMetricCard density="standard" figure={formatNumber(stats.cancelled_count)} label={t("processes.bulk.jobs_cancelled", "Cancelled")} unavailable={formatNumber(stats.cancelled_count) === NO_VALUE} wave={false} />
          </div>
        ) : null}
      </div>
    </article>
  );
}

function WorkflowQuickLinks({
  canAuthor,
  jobsLink,
  projectId,
  workflowId,
}: Readonly<{ canAuthor: boolean; jobsLink: string; projectId: string; workflowId: string }>) {
  const { t } = useI18n();
  const encodedWorkflowId = encodeURIComponent(workflowId);
  const context = new URLSearchParams();
  if (projectId) context.set("project_id", projectId);
  const path = (suffix: string, drawer = "") => {
    const params = new URLSearchParams(context);
    if (drawer) params.set("drawer", drawer);
    return `/registry/workflows/${encodedWorkflowId}${suffix}${params.size ? `?${params.toString()}` : ""}`;
  };
  return (
    <article className="fs-card">
      <header className="fs-card-header">
        <h2 className="fs-card-title">{t("react.workflows.quick_links", "Quick links")}</h2>
        <p className="fs-card-description">{t("react.workflows.jump_to_related_views_for_this_workflow", "Jump to related views for this workflow.")}</p>
      </header>
      <div className="fs-card-body">
        <div className="flex flex-wrap gap-2">
          <Link className="fs-button" to={jobsLink}>{t("react.workflows.jobs_filtered_by_this_workflow", "Jobs filtered by this workflow")}<ArrowRight aria-hidden="true" /></Link>
          {canAuthor ? <Link className="fs-button" to={path("/builder")}>{t("react.workflows.open_builder", "Open builder")}<ArrowRight aria-hidden="true" /></Link> : null}
          <Link className="fs-button" to={path("/projects")}>{t("react.workflows.linked_projects", "Linked projects")}<ArrowRight aria-hidden="true" /></Link>
          {canAuthor ? <Link className="fs-button" to={path("/builder", "workflow-test-run")}>{t("registry.workflow_detail.run_lab.tab.test_run", "Test Run")}<ArrowRight aria-hidden="true" /></Link> : null}
        </div>
      </div>
    </article>
  );
}

function HookStateBadge({ hook }: Readonly<{ hook: WorkflowHookState }>) {
  const { t } = useI18n();
  // R2-11: a hook is a place for steps; it says how many it holds, never a workflow's name.
  if (hook.connected === null || hook.stepCount === null) return null;
  const count = hook.stepCount || 0;
  if (!count) return <DsBadge>{t("react.workflows.hook_empty", "Empty")}</DsBadge>;
  return <DsBadge tone="green">{count === 1
    ? t("react.workflows.hook_steps_one", "1 step")
    : t("react.workflows.hook_steps_count", "{count} steps", { count: String(count) })}</DsBadge>;
}

function renderHookNameCell(hook: WorkflowHookState) {
  return (
    <div className="min-w-0">
      <p className="fs-ink-primary">{hook.displayName}</p>
      {hook.description ? <p className="text-xs fs-ink-secondary">{hook.description}</p> : null}
    </div>
  );
}

function renderHookStateCell(hook: WorkflowHookState) {
  return <HookStateBadge hook={hook} />;
}

function renderHookConnectCell(
  { canAuthor, projectId, t, workflowId }: Readonly<{
    canAuthor: boolean;
    projectId: string;
    t: ReturnType<typeof useI18n>["t"];
    workflowId: string;
  }>,
  hook: WorkflowHookState,
) {
  if (!canAuthor || !projectId) return null;
  const params = new URLSearchParams({ drawer: "step", step_id: hook.stepId });
  params.set("project_id", projectId);
  const connectPath = `/registry/workflows/${encodeURIComponent(workflowId)}/builder?${params.toString()}`;
  return (
    <Link
      aria-label={t("react.workflows.hook_edit_steps_name", "Edit the steps of hook {name}", { name: hook.displayName })}
      className="fs-button"
      data-workflow-hook-connect={hook.hookId}
      to={connectPath}
    >
      {t("react.workflows.hook_edit_steps", "Edit steps")}<ArrowRight aria-hidden="true" />
    </Link>
  );
}

/**
 * The places this workflow lets a project run a workflow of its own, and what
 * each project connects there. Shown only when the workflow declares hooks; a
 * binding is per project, so reads and navigation share the validated explicit choice.
 */
function WorkflowHooksPanel({
  canAuthor,
  projectId,
  projects,
  projectsPending,
  projectsError,
  onProjectChange,
  steps,
  workflowId,
}: Readonly<{ canAuthor: boolean; projectId: string; projects: ProjectListItem[]; projectsPending: boolean; projectsError: Error | null; onProjectChange: (id: string) => void; steps: unknown; workflowId: string }>) {
  const { t } = useI18n();
  const { hooks, error } = useWorkflowHookStates({ enabled: canAuthor, options: { presentation: "background" }, projectId, steps, workflowId });
  if (!hooks.length) return null;
  const projectName = firstStringFromUnknownValue([
    projects.find((project) => project.id === projectId)?.name,
    projectId,
  ]);
  let context = t("react.workflows.hooks_description", "Places in this workflow where each project adds steps of its own.");
  if (projectId) {
    context = t("react.workflows.hooks_in_project", "Steps of project {project}.", { project: projectName });
  } else if (canAuthor && !projectsPending && !projectsError && !projects.length) {
    context = t("react.workflows.hooks_no_project", "Link this workflow to a project to connect its hooks.");
  }
  return (
    <article className="fs-card" id="workflow-hooks">
      <header className="fs-card-header">
        <h2 className="fs-card-title">{t("react.workflows.hooks", "Hooks")}</h2>
        <p className="fs-card-description">{context}</p>
      </header>
      <div className="fs-card-body">
        {canAuthor ? projectsError ? (
          <DsAlert title={t("react.workflows.linked_projects_could_not_be_loaded", "Linked projects could not be loaded")} tone="danger">{projectsError.message}</DsAlert>
        ) : projectsPending ? (
          <p className="fs-empty-copy">{t("react.workflows.loading_linked_projects", "Loading linked projects…")}</p>
        ) : projects.length ? (
          <DsSelect ariaLabel={t("react.workflows.hook_project", "Project for hooks")} value={projectId}
            onChange={onProjectChange} placeholder={t("react.workflows.choose_a_linked_project", "Choose a linked project")}
            options={projects.map((project) => ({ value: project.id, label: project.name || project.id }))} />
        ) : (
          <Link className="fs-button secondary" to={`/registry/workflows/${encodeURIComponent(workflowId)}/projects`}>{t("react.workflows.link_a_project", "Link a project")}</Link>
        ) : null}
        {error ? (
          <DsAlert title={t("react.workflows.hook_load_failed", "The hook could not be loaded")} tone="danger">
            {error instanceof Error ? error.message : String(error)}
          </DsAlert>
        ) : null}
        <DsTable
          ariaLabel={t("react.workflows.hooks", "Hooks")}
          columns={[
            {
              key: "hook",
              header: t("react.workflows.hook", "Hook"),
              cell: renderHookNameCell,
            },
            { key: "state", header: t("extensions.runtime.status", "Status"), cell: renderHookStateCell },
            {
              key: "connect",
              header: "",
              ariaLabel: t("extensions.table.actions", "Actions"),
              align: "right",
              cell: renderHookConnectCell.bind(null, { canAuthor, projectId, t, workflowId }),
            },
          ]}
          embedded
          rowKey={(hook) => hook.hookId}
          rows={hooks}
        />
      </div>
    </article>
  );
}

export function WorkflowsOverviewPage() {
  const { t } = useI18n();
  const { workflowId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const projectId = searchParams.get("project_id")?.trim() || "";
  const userTimezone = useUserTimezone();
  // Workflow authoring is admin-only; hide authoring/destructive affordances on the
  // read-only overview page for non-admins (Open builder, Clone,
  // Delete, Settings, Toggle) to match backend RBAC.
  const canAuthor = useIsAdmin();
  // The unsaved channel draft of the settings form, so Webhook and Schedules follow a ticked channel at once.
  const [draftChannels, setDraftChannels] = useState<WorkflowChannelState | null>(null);
  // The workflow overview reads the global registry (GET /api/workflows/*), which the
  // backend allows only for operator/admin. Viewers are denied, so gate every query on
  // read access to avoid firing retried 403s after a denied navigation.
  const canReadRegistry = useCanReadWorkflowRegistry();

  const detailQuery = useQuery({
    queryKey: queryKeys.workflowDetail(workflowId),
    queryFn: () => fetchWorkflowDetail(workflowId),
    enabled: canReadRegistry && Boolean(workflowId),
  });
  const validationQuery = useQuery({
    queryKey: queryKeys.workflowValidation(workflowId),
    queryFn: () => fetchWorkflowValidation(workflowId),
    enabled: canReadRegistry && Boolean(workflowId),
  });
  const statsQuery = useQuery({
    queryKey: queryKeys.workflowStats(workflowId),
    queryFn: () => fetchWorkflowStats(workflowId, { presentation: "background" }),
    enabled: canReadRegistry && Boolean(workflowId),
  });

  const hasHooks = declaredWorkflowHooks(detailQuery.data?.steps).length > 0;
  const requestedTab = searchParams.get("tab") || "";
  let activeTab = "";
  if (requestedTab === "hooks" && hasHooks) activeTab = "hooks";
  else if (canAuthor && isWorkflowSettingsTab(requestedTab)) activeTab = requestedTab;
  const hookProjectsQuery = useQuery({
    queryKey: ["workflows", workflowId, "hook-projects"],
    queryFn: () => fetchWorkflowLinkedProjects(workflowId, { presentation: "background" }),
    enabled: canReadRegistry && canAuthor && activeTab === "hooks" && Boolean(workflowId),
    retry: false,
  });
  // A failed refresh may retain old data; it cannot authorize a Hook read or link.
  const hookProjects = hookProjectsQuery.isSuccess ? hookProjectsQuery.data : [];
  const hookProjectId = canAuthor && hookProjects.some((project) => project.id === projectId) ? projectId : "";
  const selectHookProject = (id: string) => {
    if (!hookProjects.some((project) => project.id === id)) return;
    const next = new URLSearchParams(searchParams);
    next.set("project_id", id);
    setSearchParams(next, { replace: true });
  };
  const selectTab = useCallback((tab: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", tab);
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);
  const settingsTabOpen = canAuthor && isWorkflowSettingsTab(activeTab);
  const [settingsLifetime, setSettingsLifetime] = useState({ workflowId, opened: settingsTabOpen });
  if (settingsLifetime.workflowId !== workflowId || (settingsTabOpen && !settingsLifetime.opened)) {
    if (settingsLifetime.workflowId !== workflowId) setDraftChannels(null);
    setSettingsLifetime({ workflowId, opened: settingsTabOpen || (settingsLifetime.workflowId === workflowId && settingsLifetime.opened) });
  }
  const settingsMounted = canAuthor && (settingsTabOpen || (settingsLifetime.workflowId === workflowId && settingsLifetime.opened));
  // Only a mounted settings form has a draft; it replaces the saved channels while unsaved.
  const channelState = (settingsMounted ? draftChannels : null) ?? savedWorkflowChannelState(detailQuery.data);
  useChannelTabFallback({
    active: canAuthor && Boolean(detailQuery.data),
    hidden: workflowChannelTabHiddenFor(activeTab, channelState),
    onFallback: () => selectTab("smart-setup"),
    tab: activeTab,
  });
  const stats: WorkflowStats | undefined = statsQuery.data;
  const workflowTitle = firstStringFromUnknownValue([detailQuery.data?.display_name, workflowId, "Workflow"]);
  const workflowDescription = firstStringFromUnknownValue([detailQuery.data?.description, t("react.workflows.overview_description", "Stats and recent activity for this workflow.")]);
  const workflowEnabled = Boolean(detailQuery.data?.enabled);
  const stepCount = useMemo(() => workflowStepCount(detailQuery.data), [detailQuery.data]);
  const validation = validationQuery.data?.validation;
  const isValid = Boolean(validation?.valid);
  const errorCount = Array.isArray(validation?.errors) ? validation.errors.length : 0;
  const warningCount = Array.isArray(validation?.warnings) ? validation.warnings.length : 0;
  const headerMutations = useWorkflowHeaderMutations({
    workflowId,
    onChanged: async () => {
      await Promise.allSettled([
        detailQuery.refetch(),
        validationQuery.refetch(),
        statsQuery.refetch(),
      ]);
    },
  });

  const jobsLink = `/jobs?workflow_id=${encodeURIComponent(workflowId)}`;

  let pageBody: ReactNode;
  if (!canReadRegistry) {
    pageBody = <WorkflowAccessDenied />;
  } else if (detailQuery.isError && !detailQuery.data) {
    pageBody = <WorkflowLoadError error={detailQuery.error} />;
  } else if (detailQuery.isLoading || validationQuery.isLoading) {
    // Wait for the definition and validation instead of printing placeholder state.
    pageBody = <PageLoadingState label={t("react.workflows.preparing_workflow_overview", "Preparing the workflow overview")} />;
  } else {
    pageBody = (
      <section className="fs-page-sections">
        <WorkflowSummaryHeader
          activePage="overview"
          activeTab={activeTab}
          canAuthor={canAuthor}
          description={workflowDescription}
          enabled={workflowEnabled}
          errorCount={errorCount}
          extraTabs={workflowPageTabsForDetail(detailQuery.data, canAuthor, t, settingsMounted ? draftChannels : null)}
          isValid={isValid}
          onCloneWorkflow={headerMutations.cloneWorkflow}
          onDeleteWorkflow={headerMutations.deleteWorkflow}
          onToggleEnabled={() => ignoreAsyncResult(headerMutations.toggleEnabled())}
          stepCount={stepCount}
          projectId={activeTab === "hooks" ? hookProjectId : projectId}
          discoverProject={activeTab !== "hooks"}
          title={workflowTitle}
          togglingEnabled={headerMutations.togglingEnabled}
          warningCount={warningCount}
          portability={detailQuery.data?.portability}
          workflowId={workflowId}
        >
          <WorkflowHeaderMutationDialogs controller={headerMutations} workflowId={workflowId} />

          {activeTab === "hooks" ? <WorkflowHooksPanel canAuthor={canAuthor} projectId={hookProjectId} projects={hookProjects} projectsPending={hookProjectsQuery.isPending} projectsError={hookProjectsQuery.error} onProjectChange={selectHookProject} steps={detailQuery.data?.steps} workflowId={workflowId} /> : null}
          {activeTab ? null : <>
            <WorkflowStatsPanel error={statsQuery.error} isError={statsQuery.isError} isLoading={statsQuery.isLoading} jobsLink={jobsLink} stats={stats} userTimezone={userTimezone} />
            <WorkflowQuickLinks canAuthor={canAuthor} jobsLink={jobsLink} projectId={projectId} workflowId={workflowId} />
          </>}
          {settingsMounted ? <Suspense fallback={null}>
            <WorkflowSettingsSurface key={workflowId} workflowId={workflowId} projectId={projectId} tab={activeTab}
              onSelectTab={selectTab} onChannelDraft={setDraftChannels}
              onSaved={async () => { await Promise.all([validationQuery.refetch({ throwOnError: true }), statsQuery.refetch({ throwOnError: true })]); }} />
          </Suspense> : null}
        </WorkflowSummaryHeader>
      </section>
    );
  }

  return <PageContainer width="standard">{pageBody}</PageContainer>;
}
