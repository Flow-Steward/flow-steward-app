import { useFilterSearchParams } from "@/lib/filters/use-filter-search-params";
import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/* eslint-disable react-hooks/exhaustive-deps */
import { useDeferredValue, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { RefreshCw, Search } from "lucide-react";
import { useNavigate } from "react-router";
import { appHref } from "@/lib/routing/app-href";

import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import { readStoredWorkspaceId } from "@/lib/utils/workspace-selection";
import { fetchJobsList, jobStatusParams, trashJob, type JobListItem, type JobsListResponse } from "@/pages/jobs/list/api";
import { PeriodFilter } from "@/components/shared/period-filter";
import { expandPeriod, type PeriodValue } from "@/lib/time/period";
import { todayIn } from "@/lib/time/calendar-days";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { fetchWorkflows } from "@/pages/workflows/shared/api";
import { fetchExtensions } from "@/pages/extensions/api";
import {
  JOB_STATUS_GROUPS,
  jobStatusesOfGroups,
  jobStatusGroup,
  jobStatusGroupLabel,
  jobStatusGroupTone,
} from "@/pages/jobs/status-groups";
import {
  findWorkflowSource,
  workflowFilterOptions as buildWorkflowFilterOptions,
  workflowSourceFromRow,
  type WorkflowSource,
} from "@/pages/jobs/list/workflow-options";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { JOB_SKELETON_ROWS, JOB_SKELETON_WIDTHS } from "@/pages/jobs/list/job-skeleton";
import { jobFiltersFromUrl, normalizeStatusList } from "@/pages/jobs/list/job-filters";
import {
  AdvancedTableFilters,
  BulkActionBar,
  DsAlert,
  DsBadge,
  DsCheckbox,
  DsPageHeader,
  DsTable,
  DsTableLink,
  Pagination,
  PageContainer,
  RowActionMenu,
  type DsBulkAction,
  type DsOption,
} from "@/components/shared/design-system";
import { columnsFromGridConfig, visibleRowActions } from "@/components/shared/data-grid-adapter";


type JobsWindow = Window & {
  _allJobs?: JobListItem[];
  _workflowDisplayNames?: Record<string, string>;
  renderJobs?: () => void;
  _rebuildStatusFilter?: () => void;
  _syncJobFiltersAndGrid?: () => void;
  refreshJobs?: () => Promise<void>;
};

const FALLBACK_BOOTSTRAP_ROWS: JobListItem[] = [
  {
    id: "1",
    job_id: "1",
    project_id: "0",
    project_name: "System",
    task_summary: "Cached jobs snapshot",
    display_summary: "Cached jobs snapshot",
    status: "pending",
    current_step_id: "bootstrap",
    current_agent: "system",
  },
];

function normalizeEntityId(value: unknown): string {
  return firstStringFromUnknownValue([value, ""]).trim();
}

function selectedWorkspaceIdFromStorage(): string {
  return normalizeEntityId(readStoredWorkspaceId());
}

function normalizeJobRows(payload: unknown): JobListItem[] {
  if (!payload || typeof payload !== "object") {
    return [];
  }
  const asObject = payload as Record<string, unknown>;
  const rows: Record<string, unknown>[] = [];
  if (Array.isArray(asObject.jobs)) {
    asObject.jobs.forEach((row) => {
      if (row && typeof row === "object") {
        rows.push(row as Record<string, unknown>);
      }
    });
  }
  if (asObject.groups && typeof asObject.groups === "object") {
    Object.values(asObject.groups as Record<string, unknown>).forEach((value) => {
      if (Array.isArray(value)) {
        value.forEach((row) => {
          if (row && typeof row === "object") {
            rows.push(row as Record<string, unknown>);
          }
        });
      }
    });
  }
  return rows.map((row) => {
    const jobId = normalizeEntityId(row.job_id || row.id);
    return {
      ...(row as JobListItem),
      id: jobId,
      job_id: jobId,
      project_id:
        row.project_id === undefined || row.project_id === null
          ? undefined
          : normalizeEntityId(row.project_id),
    };
  });
}

function normalizeStatus(value: unknown): string {
  return firstStringFromUnknownValue([value, ""]).trim().toLowerCase();
}

function periodFromUrl(params: URLSearchParams): PeriodValue {
  const preset = params.get("period");
  if (preset === "today" || preset === "last7" || preset === "last30") return { kind: "preset", preset };
  const from = params.get("from_date") || "";
  const to = params.get("to_date") || "";
  const validDay = (day: string) => /^\d{4}-\d{2}-\d{2}$/.test(day)
    && !Number.isNaN(Date.parse(day)) && new Date(day).toISOString().slice(0, 10) === day;
  return validDay(from) && validDay(to) && from <= to ? { kind: "custom", from, to } : { kind: "all" };
}

function normalizeWorkflowId(job: Record<string, unknown>): string {
  return firstStringFromUnknownValue([job.selected_workflow, job.workflow_id, ""]).trim();
}

function workflowLabel(workflowId: string, displayNames: Record<string, string>): string {
  if (!workflowId) {
    return "";
  }
  const display = firstStringFromUnknownValue([displayNames[workflowId], ""]).trim();
  if (!display || display === workflowId) {
    return workflowId;
  }
  return display;
}

function workflowDisplayName(
  job: Record<string, unknown>,
  displayNames: Record<string, string>,
  sources: ReadonlyMap<string, WorkflowSource> = new Map(),
): string {
  const workflowId = normalizeWorkflowId(job);
  return firstStringFromUnknownValue([
    job.selected_workflow_display_name,
    job.workflow_display_name,
    job.workflow_name,
    // An extension workflow is not in the registry the job list joins, so its
    // name comes from the Workflows list, as on the Workflows page.
    workflowId ? findWorkflowSource(workflowId, sources)?.name : "",
    displayNames[workflowId],
    workflowId,
    "—",
  ]).trim();
}

function durationLabel(job: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t): string {
  const startedAt = firstStringFromUnknownValue([job.started_at, job.queued_at, ""]).trim();
  if (!startedAt) {
    return "—";
  }
  const startMs = new Date(startedAt).getTime();
  if (!Number.isFinite(startMs)) {
    return "—";
  }
  const endRaw = firstStringFromUnknownValue([job.finished_at, ""]).trim();
  const endMs = endRaw ? new Date(endRaw).getTime() : Date.now();
  if (!Number.isFinite(endMs)) {
    return "—";
  }
  const seconds = Math.max(0, Math.floor((endMs - startMs) / 1000));
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return t("react.jobs.value1_m_value2_s", "{value1}m {value2}s", { value1: String(minutes), value2: String(seconds % 60) });
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return t("react.jobs.value1_h_value2_m", "{value1}h {value2}m", { value1: String(hours), value2: String(minutes % 60) });
  }
  return t("react.jobs.value1_d_value2_h", "{value1}d {value2}h", { value1: String(Math.floor(hours / 24)), value2: String(hours % 24) });
}

/** The group label as the badge, and a small Acknowledged badge beside a triaged run. */
function jobStatusCell(row: Record<string, unknown>) {
  const label = firstStringFromUnknownValue([row._status_label, ""]);
  const tone = jobStatusGroupTone(jobStatusGroup(row.status));
  const acknowledged = firstStringFromUnknownValue([row._acknowledged_label, ""]);
  return (
    <span className="fs-job-status-cell">
      {label ? <DsBadge tone={tone}>{label}</DsBadge> : null}
      {acknowledged ? <DsBadge>{acknowledged}</DsBadge> : null}
    </span>
  );
}

function jobWorkflowCell(row: Record<string, unknown>) {
  const workflowName = firstStringFromUnknownValue([row._workflow_display, "—"]);
  const workflowId = firstStringFromUnknownValue([row._workflow_tooltip, ""]);
  const request = firstStringFromUnknownValue([row._task_display, ""]).trim();
  const fullRequest = firstStringFromUnknownValue([row._task_full, row.task_summary, request]);
  return (
    <span className="fs-job-workflow-copy">
      <DsTableLink href={appHref(`/jobs/${encodeURIComponent(firstStringFromUnknownValue([row.job_id, row.id, ""]))}`)}>
        <span title={workflowId || workflowName}>{workflowName}</span>
      </DsTableLink>
      {request ? <span title={fullRequest}>{request}</span> : null}
    </span>
  );
}

/**
 * ADR 0075: a user section is this same list with its project and workflow fixed
 * in the background. The section brings its own header (and its New button) and
 * the columns an admin chose; Project and Workflow are known, so neither shows.
 */
export type JobsListScope = {
  key: string;
  projectId: string;
  workflowId: string;
  sectionId: string;
  title: string;
  description: string;
  eyebrow: string;
  action?: ReactNode;
  notice?: ReactNode;
  columns: Array<{ key: string; label: string; cell: (row: Record<string, unknown>) => ReactNode }>;
};

const SCOPED_HIDDEN_COLUMNS = new Set(["_workflow_display", "project_name"]);

export function JobsListPage({ scope }: Readonly<{ scope?: JobsListScope }> = {}) {
  const { t } = useI18n();
  const auth = useAuth();
  const userTimezone = useUserTimezone();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useFilterSearchParams(scope ? `section-jobs:${scope.key}` : "jobs", ["search", "project_id", "workflow_id", "status", "hide_acknowledged", "period", "from_date", "to_date"]);
  const period = periodFromUrl(searchParams);
  const { fromDate, toDate } = expandPeriod(period, todayIn(userTimezone));
  // Filter state lives in the URL (job-filters.ts): the published filter and
  // search controls are controlled, and the option lists never come from the
  // loaded jobs alone, so a filter never shrinks its own menu.
  const {
    urlWorkflowFilter, search, setSearch, projectFilter: urlProjectFilter, setProjectFilter, statusFilter,
    setStatusFilter, hideAcknowledged, setHideAcknowledged, workflowFilter: chosenWorkflowFilter, setWorkflowFilter,
  } = jobFiltersFromUrl(searchParams, setSearchParams);
  // A section fixes its project and workflow; the person never picks them.
  const projectFilter = scope ? scope.projectId : urlProjectFilter;
  const workflowFilter = scope ? scope.workflowId : chosenWorkflowFilter;
  const deferredSearch = useDeferredValue(search);
  const [filterOptions, setFilterOptions] = useState<{
    projects: DsOption[];
    statuses: DsOption[];
    workflows: DsOption[];
  }>({ projects: [], statuses: [], workflows: [] });
  const [displayRows, setDisplayRows] = useState<Record<string, unknown>[]>([]);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(25);
  const [totalRows, setTotalRows] = useState(0);
  // False until the first answer (or its fallback) lands: until then an empty
  // body means "not yet", never "no jobs".
  const [jobsLoaded, setJobsLoaded] = useState(false);
  const [selectedJobs, setSelectedJobs] = useState<ReadonlySet<string>>(new Set());
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const allJobsRef = useRef<JobListItem[]>([]);
  // Projects and workflows the user can see, from their own lists; null until
  // loaded (or when the list is not readable), then the loaded jobs fill in.
  const projectSourceRef = useRef<DsOption[] | null>(null);
  const workflowSourceRef = useRef<Map<string, WorkflowSource>>(new Map());
  const extensionNamesRef = useRef<Map<string, string>>(new Map());
  const workflowDisplayNamesRef = useRef<Record<string, string>>({});
  const renderJobsFnRef = useRef<() => void>(() => undefined);
  const rebuildStatusFilterRef = useRef<() => void>(() => undefined);
  const syncFiltersAndGridRef = useRef<() => void>(() => undefined);
  const refreshJobsRef = useRef<() => Promise<void>>(async () => undefined);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);
  const bulkBusyRef = useRef(false);
  const [workspaceSelection, setSelectedWorkspaceId] = useState(selectedWorkspaceIdFromStorage);
  // The section's project decides; the shell's workspace choice does not narrow it.
  const selectedWorkspaceId = scope ? "" : workspaceSelection;
  const filterScope = JSON.stringify([deferredSearch, hideAcknowledged, projectFilter, selectedWorkspaceId, statusFilter, workflowFilter, fromDate, toDate]);
  const selectionScopeRef = useRef({ key: filterScope, generation: 0 });
  if (selectionScopeRef.current.key !== filterScope) {
    selectionScopeRef.current = { key: filterScope, generation: selectionScopeRef.current.generation + 1 };
  }

  const roleFlags = useMemo(() => {
    const roles = new Set((auth.session?.user?.roles || auth.session?.roles || [])
      .map((role) => firstStringFromUnknownValue([role, ""]).trim().toLowerCase())
      .filter(isTruthy));
    const isAdmin = roles.has("admin");
    const isOperator = roles.has("operator");
    return {
      isAdmin,
      canMutate: isAdmin || isOperator,
    };
  }, [auth.session]);

  function syncFromWindowGlobals() {
    const jobsWindow = globalThis as unknown as JobsWindow;
    if (Array.isArray(jobsWindow._allJobs)) {
      allJobsRef.current = normalizeJobRows({ jobs: jobsWindow._allJobs });
    }
    if (jobsWindow._workflowDisplayNames && typeof jobsWindow._workflowDisplayNames === "object") {
      workflowDisplayNamesRef.current = { ...jobsWindow._workflowDisplayNames };
    }
  }

  function publishWindowGlobals() {
    const jobsWindow = globalThis as unknown as JobsWindow;
    jobsWindow._allJobs = allJobsRef.current.map((row) => ({ ...row }));
    jobsWindow._workflowDisplayNames = { ...workflowDisplayNamesRef.current };
  }

  function matchesStatusFilter(job: JobListItem, filterValue: string): boolean {
    const groups = jobStatusParams(filterValue);
    if (!groups.length) {
      return true;
    }
    const group = jobStatusGroup(normalizeStatus(job.status));
    return Boolean(group && groups.includes(group));
  }

  function buildJobsGridConfig(): Record<string, unknown> {
    const rowActions: Record<string, unknown>[] = [{ action_id: "view", label: t("jobs.actions.view", "View"), variant: "ghost" }];
    if (roleFlags.canMutate) {
      rowActions.push({
        action_id: "cancel",
        label: t("common.cancel", "Cancel"),
        variant: "danger",
        visible_if: { field: "status", in: ["in_progress", "resume_pending", "pending", "awaiting_approval", "awaiting_timer", "awaiting_child", "awaiting_event"] },
      });
    }
    const bulkActions: Record<string, unknown>[] = [];
    if (roleFlags.canMutate) {
      bulkActions.push({ action_id: "bulk_cancel", label: t("jobs.actions.cancel_selected", "Cancel Selected"), variant: "danger" });
    }
    if (roleFlags.isAdmin) {
      bulkActions.push({ action_id: "bulk_delete", label: t("project_detail.jobs.delete_selected", "Delete Selected"), variant: "ghost" });
    }
    return {
      columns: [
        {
          key: "_workflow_display",
          label: t("jobs.columns.workflow", "Workflow"),
          type: "text",
          width: "320px",
        },
        {
          key: "project_name",
          label: t("events.columns.project", "Project"),
          type: "text",
          width: "130px",
          truncate: true,
          truncate_width: "clamp(9rem, 14vw, 12rem)",
          tooltip_key: "project_name",
        },
        {
          key: "status",
          label: t("account_settings.card.status", "Status"),
          type: "badge",
          width: "140px",
          badge_map: {
            failed: "red",
            stuck: "red",
            orphaned: "amber",
            max_iterations: "red",
            triage_needed: "red",
            awaiting_approval: "red",
            blocked: "amber",
            retryable_interrupted_step: "amber",
            external_wait: "amber",
            in_progress: "accent",
            awaiting_timer: "accent",
            awaiting_child: "accent",
            awaiting_event: "accent",
            resume_pending: "accent",
            pending: "accent",
            completed: "green",
            cancelled: "neutral",
          },
        },
        {
          key: "_started_display",
          label: t("project_detail.jobs.grid.started", "Started"),
          type: "text",
          width: "180px",
          tooltip_key: "_started_tooltip",
        },
        {
          key: "_completed_display",
          label: t("jobs.detail.step.completed", "Completed"),
          type: "text",
          width: "180px",
          tooltip_key: "_completed_tooltip",
        },
        { key: "_duration", label: t("jobs.columns.duration", "Duration"), type: "text", width: "88px" },
      ],
      row_actions: rowActions,
      bulk_actions: bulkActions,
      id_field: "job_id",
      detail: { navigate: true },
      pagination: {
        page_size_default: 25,
        page_size_options: [10, 25, 50],
      },
      empty_state: {
        title: t("jobs.empty.title", "No jobs found"),
        description: t("jobs.empty.description", "Try adjusting your search or filters."),
      },
    };
  }

  function getFilteredRows(): JobListItem[] {
    const searchQuery = deferredSearch.trim().toLowerCase();
    const projectValue = projectFilter.trim();
    const statusValue = statusFilter.trim();
    const workflowValue = workflowFilter.trim();
    return allJobsRef.current.filter((job) => {
      const workflowId = normalizeWorkflowId(job);
      if (searchQuery) {
        const haystack = [
          job.run_title || "",
          job.display_summary || job.task_summary || "",
          job.task_summary || "",
          job.project_name || "",
          workflowId,
          workflowDisplayName(job, workflowDisplayNamesRef.current, workflowSourceRef.current),
          job.current_step_id || "",
          job.current_agent || "",
          job.status || "",
          job.task_id || "",
          job.job_id || "",
        ].map((value) => stringFromUnknownValue(value)).join(" ")
          .toLowerCase();
        if (!haystack.includes(searchQuery)) {
          return false;
        }
      }
      if (projectValue && firstStringFromUnknownValue([job.project_id, ""]) !== projectValue) {
        return false;
      }
      if (statusValue && !matchesStatusFilter(job, statusValue)) {
        return false;
      }
      if (hideAcknowledged && job.acknowledged_at) {
        return false;
      }
      if (workflowValue && workflowId !== workflowValue) {
        return false;
      }
      return true;
    });
  }

  function renderJobs() {
    syncFromWindowGlobals();
    const rows = getFilteredRows().map((job) => {
      const workflowId = normalizeWorkflowId(job);
      const startedAt = firstStringFromUnknownValue([job.started_at, job.queued_at, ""]).trim();
      const finishedAt = firstStringFromUnknownValue([job.finished_at, ""]).trim();
      const summaryParams = job.task_summary_params;
      const generatedWorkflowId = summaryParams && typeof summaryParams === "object"
        ? summaryParams.workflow_id
        : undefined;
      const generatedWorkflowRun = job.task_summary_key === "react.jobs.generated_workflow_run_summary"
        && typeof generatedWorkflowId === "string" && generatedWorkflowId.length > 0;
      // A run title (schedule or webhook name, the workflow's run title) says what this run was for.
      const runTitle = firstStringFromUnknownValue([job.run_title, ""]).trim();
      let taskDisplay = firstStringFromUnknownValue([job.display_summary, job.task_summary, ""]);
      let taskFull = firstStringFromUnknownValue([job.task_summary, job.display_summary, ""]);
      if (runTitle) {
        taskDisplay = runTitle;
        taskFull = runTitle;
      } else if (generatedWorkflowRun) {
        taskDisplay = t("react.jobs.generated_workflow_run_label", "Workflow run");
        taskFull = t("react.jobs.generated_workflow_run_summary", "Workflow run: {workflow_id}", { workflow_id: generatedWorkflowId });
      }
      return {
        ...job,
        _duration: durationLabel(job, t),
        _task_display: taskDisplay,
        _task_full: taskFull,
        _workflow_display: workflowDisplayName(job, workflowDisplayNamesRef.current, workflowSourceRef.current),
        _workflow_tooltip: workflowId,
        // Unknown or legacy codes have no group; only Job details shows them raw.
        _status_label: (() => {
          const group = jobStatusGroup(normalizeStatus(job.status));
          return group ? jobStatusGroupLabel(group, t) : t("react.jobs.status_group_unknown", "Unknown status");
        })(),
        _acknowledged_label: job.acknowledged_at ? t("react.jobs.acknowledged", "Acknowledged") : "",
        _started_display: formatUserDateTime(startedAt, userTimezone),
        _started_tooltip: startedAt,
        _completed_display: formatUserDateTime(finishedAt, userTimezone),
        _completed_tooltip: finishedAt,
      };
    });
    setDisplayRows(rows);
  }

  function projectFilterOptions(): DsOption[] {
    const projects = new Map<string, string>();
    projectSourceRef.current?.forEach((option) => projects.set(option.value, option.label));
    allJobsRef.current.forEach((job) => {
      const id = firstStringFromUnknownValue([job.project_id, ""]).trim();
      const name = firstStringFromUnknownValue([job.project_name, ""]).trim();
      if (id && name && !projects.has(id)) {
        projects.set(id, name);
      }
    });
    return Array.from(projects.entries())
      .sort((left, right) => left[1].localeCompare(right[1]))
      .map(([value, label]) => ({ value, label }));
  }

  function statusFilterOptions(): DsOption[] {
    // Exactly the eight status groups, in their order, whatever the page holds.
    return JOB_STATUS_GROUPS.map((group) => ({ value: group.key, label: jobStatusGroupLabel(group.key, t) }));
  }

  function workflowFilterOptions(): DsOption[] {
    // Every workflow id worth offering, with the best name the page has for it;
    // the Workflows list's own name wins inside buildWorkflowFilterOptions.
    const candidates = new Map<string, string>();
    workflowSourceRef.current.forEach((source) => candidates.set(source.id, source.name));
    Object.keys(workflowDisplayNamesRef.current).forEach((workflowId) => {
      const id = firstStringFromUnknownValue([workflowId, ""]).trim();
      if (id && !candidates.has(id)) {
        candidates.set(id, workflowLabel(id, workflowDisplayNamesRef.current) || id);
      }
    });
    allJobsRef.current.forEach((job) => {
      const workflowId = normalizeWorkflowId(job);
      if (workflowId && !candidates.has(workflowId)) {
        candidates.set(workflowId, workflowDisplayName(job, workflowDisplayNamesRef.current, workflowSourceRef.current));
      }
    });
    if (urlWorkflowFilter && !candidates.has(urlWorkflowFilter)) {
      candidates.set(
        urlWorkflowFilter,
        workflowLabel(urlWorkflowFilter, workflowDisplayNamesRef.current) || urlWorkflowFilter,
      );
    }
    const projects = new Map<string, string>();
    projectFilterOptions().forEach((option) => projects.set(option.value, option.label));
    return buildWorkflowFilterOptions(
      candidates,
      workflowSourceRef.current,
      { extensions: extensionNamesRef.current, projects },
    );
  }

  // Rebuild the option lists from their sources plus the current server page.
  // Active filters stay authoritative even when a valid filter produces an empty page.
  function rebuildFilterOptions() {
    syncFromWindowGlobals();
    const projects = projectFilterOptions();
    const statuses = statusFilterOptions();
    const workflows = workflowFilterOptions();
    setFilterOptions({ projects, statuses, workflows });
  }

  function syncJobFiltersAndGrid() {
    syncFromWindowGlobals();
    rebuildFilterOptions();
    renderJobs();
  }

  function applyJobsData(payload: JobsListResponse) {
    const rows = normalizeJobRows(payload);
    allJobsRef.current = rows;
    const responsePerPage = Math.max(1, Number(payload.per_page || perPage));
    const responseTotal = Math.max(0, Number(payload.total ?? rows.length));
    const responseTotalPages = Math.max(1, Number(payload.total_pages || Math.ceil(responseTotal / responsePerPage)));
    const responsePage = Math.min(responseTotalPages, Math.max(1, Number(payload.page || page)));
    setPerPage(responsePerPage);
    setTotalRows(responseTotal);
    setPage(responsePage);
    setJobsLoaded(true);
    publishWindowGlobals();
    syncJobFiltersAndGrid();
  }

  function clearGridSelection() {
    setSelectedJobs(new Set());
  }

  async function bulkJobAction(actionId: string, itemIds: string[]) {
    if (bulkBusyRef.current) return;
    const ids = itemIds.map((itemId) => normalizeEntityId(itemId)).filter(isTruthy);
    if (!ids.length) {
      return;
    }
    if (actionId === "bulk_cancel") {
      if (!globalThis.confirm(t("react.jobs.confirm_cancel_selected", "Cancel {count} selected jobs?", { count: ids.length }))) {
        return;
      }
      for (const jobId of ids) {
        await fetchJson<unknown>(`/api/jobs/${jobId}/cancel`, {
          method: "POST",
        });
      }
      clearGridSelection();
      await refreshJobs();
      return;
    }
    if (actionId === "bulk_delete") {
      if (!globalThis.confirm(t("react.jobs.confirm_delete_selected", "Delete {count} selected jobs?", { count: ids.length }))) {
        return;
      }
      const selectionGeneration = selectionScopeRef.current.generation;
      bulkBusyRef.current = true;
      setBulkBusy(true);
      setActionError("");
      const failed: string[] = [];
      const errors: string[] = [];
      try {
        for (const jobId of ids) {
          try {
            await trashJob(jobId);
          } catch (error) {
            failed.push(jobId);
            errors.push(`${jobId}: ${error instanceof Error ? error.message : t("react.jobs.trash_failed", "Could not move job to Trash.")}`);
          }
        }
        if (selectionScopeRef.current.generation === selectionGeneration) setSelectedJobs(new Set(failed));
        if (failed.length < ids.length) {
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: ["workflows", "stats"] }),
            queryClient.invalidateQueries({ queryKey: ["jobs"] }),
            queryClient.invalidateQueries({ queryKey: ["trash"] }),
            queryClient.invalidateQueries({ queryKey: ["dashboard", "summary"] }),
          ]);
        }
        await refreshJobsRef.current();
        setActionError(errors.join("; "));
      } finally {
        bulkBusyRef.current = false;
        setBulkBusy(false);
      }
    }
  }

  async function refreshJobsFromLegacy(): Promise<boolean> {
    if (scope) {
      // The old list has no section filter; showing other runs would mislead.
      return false;
    }
    try {
      const params = new URLSearchParams({
        page: String(page),
        per_page: String(perPage),
      });
      if (selectedWorkspaceId) {
        params.set("workspace_id", selectedWorkspaceId);
      }
      if (projectFilter) {
        params.set("project_id", projectFilter);
      }
      jobStatusesOfGroups(jobStatusParams(statusFilter)).forEach((status) => params.append("status", status));
      if (hideAcknowledged) {
        params.set("hide_acknowledged", "true");
      }
      if (workflowFilter) {
        params.set("workflow_id", workflowFilter);
      }
      if (deferredSearch.trim()) {
        params.set("search", deferredSearch.trim());
      }
      if (fromDate) params.set("from_date", fromDate);
      if (toDate) params.set("to_date", toDate);
      const payload = await fetchJson<JobsListResponse>(`/api/jobs?${params.toString()}`);
      applyJobsData(payload);
      return true;
    } catch {
      return false;
    }
  }

  async function refreshJobs() {
    let errorMessage = "";
    try {
      const payload = await fetchJobsList({
        page,
        perPage,
        status: jobStatusesOfGroups(jobStatusParams(statusFilter)).join(","),
        hideAcknowledged,
        projectId: projectFilter || null,
        workflowId: workflowFilter || null,
        sectionId: scope?.sectionId ?? null,
        search: deferredSearch,
        workspaceId: selectedWorkspaceId || null,
        fromDate,
        toDate,
      });
      applyJobsData(payload);
      setLoadError("");
      return;
    } catch (error) {
      const message = error instanceof Error ? error.message : "request_failed";
      errorMessage = `Could not refresh jobs. Showing last available data. (${message})`;
    }
    if (allJobsRef.current.length) {
      syncJobFiltersAndGrid();
    } else {
      const restoredFromLegacy = await refreshJobsFromLegacy();
      if (!restoredFromLegacy) {
        applyJobsData({
          jobs: FALLBACK_BOOTSTRAP_ROWS,
          page: 1,
          per_page: perPage,
          total: FALLBACK_BOOTSTRAP_ROWS.length,
          total_pages: 1,
        });
      }
    }
    setLoadError(errorMessage);
  }

  async function rowJobAction(actionId: string, jobId: string) {
    if (!jobId) {
      return;
    }
    if (actionId === "view") {
      ignoreAsyncResult(navigate(`/jobs/${jobId}`));
      return;
    }
    if (actionId === "cancel") {
      if (!globalThis.confirm(t("react.jobs.confirm_cancel_job", "Cancel job #{id}?", { id: jobId }))) {
        return;
      }
      await fetchJson<unknown>(`/api/jobs/${jobId}/cancel`, {
        method: "POST",
      });
      await refreshJobs();
    }
  }

  renderJobsFnRef.current = renderJobs;
  rebuildStatusFilterRef.current = rebuildFilterOptions;
  syncFiltersAndGridRef.current = syncJobFiltersAndGrid;
  refreshJobsRef.current = refreshJobs;

  useEffect(() => {
    const jobsWindow = globalThis as unknown as JobsWindow;
    jobsWindow.renderJobs = () => renderJobsFnRef.current();
    jobsWindow._rebuildStatusFilter = () => rebuildStatusFilterRef.current();
    jobsWindow._syncJobFiltersAndGrid = () => syncFiltersAndGridRef.current();
    jobsWindow.refreshJobs = async () => refreshJobsRef.current();
    publishWindowGlobals();
    return () => {
      delete jobsWindow.renderJobs;
      delete jobsWindow._rebuildStatusFilter;
      delete jobsWindow._syncJobFiltersAndGrid;
      delete jobsWindow.refreshJobs;
    };
  }, []);

  useEffect(() => {
    const onWorkspaceSelected = (event: Event) => {
      const detail = (event as CustomEvent<{ workspaceId?: string }>).detail || {};
      setPage(1);
      setSelectedWorkspaceId(normalizeEntityId(detail.workspaceId));
    };
    globalThis.addEventListener("platform:workspace-selected", onWorkspaceSelected as EventListener);
    return () => {
      globalThis.removeEventListener("platform:workspace-selected", onWorkspaceSelected as EventListener);
    };
  }, []);

  useEffect(() => {
    ignoreAsyncResult(refreshJobsRef.current());
  }, [deferredSearch, hideAcknowledged, page, perPage, projectFilter, selectedWorkspaceId, statusFilter, workflowFilter, fromDate, toDate]);

  useEffect(() => {
    setSelectedJobs(new Set());
  }, [deferredSearch, hideAcknowledged, projectFilter, selectedWorkspaceId, statusFilter, workflowFilter, fromDate, toDate]);

  // Every chosen status group stays in the URL, so a reload or a shared link
  // keeps it; a link that named stored statuses is rewritten to their groups.
  useEffect(() => {
    const desiredHide = hideAcknowledged ? "1" : null;
    if (
      searchParams.getAll("status").join(",") === jobStatusParams(statusFilter).join(",")
      && searchParams.get("hide_acknowledged") === desiredHide
    ) {
      return;
    }
    const next = new URLSearchParams(searchParams);
    next.delete("status");
    jobStatusParams(statusFilter).forEach((status) => next.append("status", status));
    next.delete("hide_acknowledged");
    if (hideAcknowledged) {
      next.set("hide_acknowledged", "1");
    }
    setSearchParams(next, { replace: true });
  }, [hideAcknowledged, statusFilter]);

  useEffect(() => {
    let active = true;
    fetchProjectsList({ page: 1, perPage: 200, search: "", workspaceId: selectedWorkspaceId })
      .then((payload) => {
        if (!active || !Array.isArray(payload?.projects)) return;
        projectSourceRef.current = payload.projects
          .map((project) => ({ value: normalizeEntityId(project.id), label: firstStringFromUnknownValue([project.name, project.id, ""]).trim() }))
          .filter((option) => option.value && option.label)
          .sort((left, right) => left.label.localeCompare(right.label));
        rebuildStatusFilterRef.current();
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [selectedWorkspaceId]);

  useEffect(() => {
    let active = true;
    // GET /api/workflows is for operators and admins; a viewer's options come from the jobs.
    if (!roleFlags.canMutate) {
      return () => {
        active = false;
      };
    }
    fetchWorkflows({ enabledOnly: false, search: "" })
      .then((workflows) => {
        if (!active || !Array.isArray(workflows)) return;
        workflowSourceRef.current = new Map(
          workflows
            .map((workflow) => workflowSourceFromRow(workflow))
            .filter((source): source is WorkflowSource => Boolean(source))
            .map((source) => [source.id, source]),
        );
        // The grid's Workflow column reads the same names, so it re-renders too.
        syncFiltersAndGridRef.current();
      })
      .catch(() => undefined);
    // Extension names tell same-named extension workflows apart in the filter.
    fetchExtensions("all")
      .then((extensions) => {
        if (!active || !Array.isArray(extensions)) return;
        extensionNamesRef.current = new Map(
          extensions
            .map((extension) => [
              firstStringFromUnknownValue([extension.install_id, ""]).trim(),
              firstStringFromUnknownValue([extension.display_name, extension.extension_id, ""]).trim(),
            ] as const)
            .filter(([installId, name]) => installId && name),
        );
        rebuildStatusFilterRef.current();
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [roleFlags.canMutate]);

  useEffect(() => {
    if (!urlWorkflowFilter) {
      return;
    }
    setPage(1);
  }, [urlWorkflowFilter]);

  function handleJobRowAction(actionId: string, row: Record<string, unknown>) {
    const jobId = normalizeEntityId(row.job_id || row.id);
    if (jobId) {
      ignoreAsyncResult(rowJobAction(actionId, jobId));
    }
  }

  const jobsConfig = useMemo(
    () => buildJobsGridConfig(),
    // Role flags decide which row and bulk actions the config declares.
    [roleFlags.canMutate, roleFlags.isAdmin],
  );

  const jobColumns = useMemo(() => {
    const columns = columnsFromGridConfig(jobsConfig, (row) => {
        const jobId = normalizeEntityId(row.job_id || row.id);
        return (
          <RowActionMenu
            label={t("react.jobs.actions_for_job_value1", "Actions for job {value1}", { value1: String(jobId) })}
            onOpenChange={(open) => setRowMenu(open ? jobId : null)}
            open={rowMenu === jobId}
          >
            {visibleRowActions(jobsConfig, row).map((action) => (
              <div className="fs-row-menu-group" key={action.action_id}>
                <button
                  className={action.variant === "danger" ? "danger" : undefined}
                  onClick={() => {
                    setRowMenu(null);
                    handleJobRowAction(action.action_id, row);
                  }}
                  role="menuitem"
                  type="button"
                >
                  {action.label}
                </button>
              </div>
            ))}
          </RowActionMenu>
        );
      });
    const visible = scope ? columns.filter((column) => !SCOPED_HIDDEN_COLUMNS.has(column.key)) : columns;
    const withSection = scope
      ? [
          ...visible.filter((column) => column.key !== "actions"),
          ...scope.columns.map((column) => ({ key: `section:${column.key}`, header: column.label, cell: column.cell })),
          ...visible.filter((column) => column.key === "actions"),
        ]
      : visible;
    return withSection.map((declared) => {
      const column = { ...declared, skeletonWidth: JOB_SKELETON_WIDTHS[declared.key] };
      if (column.key === "status") {
        return { ...column, cell: jobStatusCell };
      }
      if (column.key !== "_workflow_display") {
        return column;
      }
      return {
        ...column,
        cell: jobWorkflowCell,
      };
    });
  }, [jobsConfig, rowMenu, t, scope]);

  const jobBulkActions = useMemo<DsBulkAction[]>(() => {
    const declared = Array.isArray(jobsConfig.bulk_actions) ? jobsConfig.bulk_actions : [];
    return declared.map((entry) => {
      const action = (entry || {}) as Record<string, unknown>;
      return {
        id: firstStringFromUnknownValue([action.action_id, ""]),
        label: firstStringFromUnknownValue([action.label, ""]),
        variant: action.variant === "danger" ? ("danger" as const) : ("default" as const),
      };
    });
  }, [jobsConfig]);

  // First load only: headers, filters, toolbar and pagination are real, the
  // body stays empty for 400ms, then holds the skeleton rows for at least
  // 500ms; a later refresh keeps the rows it has.
  const firstLoadPending = !jobsLoaded && displayRows.length === 0;
  const skeletonShown = useLoaderDelay(firstLoadPending);
  const showEmptyState = jobsLoaded && !skeletonShown && displayRows.length === 0;

  return (
    <PageContainer className="fs-page-sections" width="wide">
      <DsPageHeader
        action={
          <div className="flex flex-wrap items-center justify-end gap-2">
            <button
              className="fs-button secondary"
              onClick={() => {
                ignoreAsyncResult(refreshJobs());
              }}
              type="button"
            >
              <RefreshCw aria-hidden="true" />
              {t("account.tokens.refresh", "Refresh")} </button>
            {scope?.action}
          </div>
        }
        description={scope ? scope.description : t("react.jobs.review_job_status_filter_active_runs_and_jump_straight_into_the_work_that_needs_attention", "Review job status, filter active runs, and jump straight into the work that needs attention.")}
        eyebrow={scope ? scope.eyebrow : t("jobs.page_title", "Jobs")}
        title={scope ? scope.title : t("react.jobs.jobs_list_and_triage", "Jobs list and triage")}
      />
      {scope?.notice}

      {loadError ? (
        <DsAlert title={t("react.jobs.jobs_could_not_be_loaded", "Jobs could not be loaded")} tone="danger">
          {loadError}
        </DsAlert>
      ) : null}
      {actionError ? <DsAlert title={t("react.jobs.jobs_could_not_be_trashed", "Some jobs could not be moved to Trash")} tone="danger">{actionError}</DsAlert> : null}

      <article className="fs-card fs-table-card" id="jobs-table-view">
        <div className="fs-table-toolbar">
          <PeriodFilter
            portal
            allLabel={t("react.jobs.all_runs", "All runs")}
            ariaLabel={t("react.jobs.job_period", "Job period")}
            value={period}
            onChange={(value) => {
              const next = new URLSearchParams(searchParams);
              next.delete("period");
              next.delete("from_date");
              next.delete("to_date");
              if (value.kind === "preset") next.set("period", value.preset);
              if (value.kind === "custom") {
                next.set("from_date", value.from);
                next.set("to_date", value.to);
              }
              setPage(1);
              setSearchParams(next, { replace: true });
            }}
          />
          <AdvancedTableFilters
            fields={
              scope
                ? [{ key: "status_filter", label: t("account_settings.card.status", "Status"), options: filterOptions.statuses, multiple: true }]
                : [
                    { key: "project_filter", label: t("events.columns.project", "Project"), options: filterOptions.projects },
                    { key: "status_filter", label: t("account_settings.card.status", "Status"), options: filterOptions.statuses, multiple: true },
                    { key: "workflow_filter", label: t("jobs.columns.workflow", "Workflow"), options: filterOptions.workflows },
                  ]
            }
            onChange={(key, value) => {
              setPage(1);
              if (key === "project_filter") {
                setProjectFilter(value);
              } else if (key === "status_filter") {
                setStatusFilter(normalizeStatusList([value]));
              } else {
                setWorkflowFilter(value);
              }
            }}
            onClear={() => {
              setPage(1);
              setProjectFilter("");
              setStatusFilter("");
              setWorkflowFilter("");
            }}
            values={{
              project_filter: projectFilter,
              status_filter: statusFilter,
              workflow_filter: workflowFilter,
            }}
          />
          <DsCheckbox
            checked={hideAcknowledged}
            onChange={(checked) => {
              setPage(1);
              setHideAcknowledged(checked);
            }}
          >
            {t("react.jobs.hide_acknowledged", "Hide acknowledged")}
          </DsCheckbox>
          <label className="fs-search">
            <Search aria-hidden="true" />
            <input
              aria-label={t("react.jobs.search_jobs", "Search jobs")}
              id="search"
              onChange={(event) => {
                setPage(1);
                setSearch(event.target.value);
              }}
              placeholder={t("react.jobs.search_jobs_2", "Search jobs…")}
              type="search"
              value={search}
            />
          </label>
        </div>
        <DsTable
          rowClickFocusable={false}
          ariaLabel={t("jobs.page_title", "Jobs")}
          columns={jobColumns}
          onRowClick={(row) => {
            const jobId = normalizeEntityId(row.job_id || row.id);
            if (jobId) {
              ignoreAsyncResult(navigate(`/jobs/${jobId}`));
            }
          }}
          rowKey={(row) => normalizeEntityId(row.job_id || row.id)}
          rows={displayRows}
          selectable={jobBulkActions.length > 0}
          selectedKeys={selectedJobs}
          onSelectionChange={setSelectedJobs}
          busy={firstLoadPending}
          skeletonRows={skeletonShown ? JOB_SKELETON_ROWS : 0}
          state={
            showEmptyState ? (
              <div className="fs-table-state" data-testid="jobs-grid">
                <strong>{t("jobs.empty.title", "No jobs found")}</strong>
                <span>{t("jobs.empty.description", "Try adjusting your search or filters.")}</span>
              </div>
            ) : null
          }
        />
        <Pagination
          onPageChange={setPage}
          onPerPageChange={(value) => {
            setPage(1);
            setPerPage(value);
          }}
          page={page}
          perPage={perPage}
          perPageOptions={[10, 25, 50]}
          total={totalRows}
        />
        <BulkActionBar
          actions={jobBulkActions}
          busy={bulkBusy}
          count={selectedJobs.size}
          onAction={(actionId) => ignoreAsyncResult(bulkJobAction(actionId, [...selectedJobs]))}
          onClear={() => setSelectedJobs(new Set())}
        />
      </article>

    </PageContainer>
  );
}
