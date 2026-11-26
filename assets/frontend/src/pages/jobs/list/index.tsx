import { DsSearchInput } from "@/components/ui/search-input";
import { useFilterSearchParams } from "@/lib/filters/use-filter-search-params";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable react-hooks/exhaustive-deps */
import { useDeferredValue, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { RefreshCw } from "lucide-react";
import { useNavigate } from "react-router";
import { appHref } from "@/lib/routing/app-href";

import { sessionPermissions } from "@/lib/auth/permissions";
import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import { fetchJobsList, jobStatusParams, trashJob, type JobListItem, type JobsListResponse } from "@/pages/jobs/list/api";
import { PeriodFilter } from "@/components/shared/period-filter";
import { expandPeriod } from "@/lib/time/period";
import { todayIn } from "@/lib/time/calendar-days";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { fetchWorkflows } from "@/pages/workflows/shared/api";
import { fetchExtensions } from "@/pages/extensions/api";
import {
  JOB_STATUS_GROUPS,
  jobStatusGroup,
  jobStatusGroupLabel,
  jobStatusGroupTone,
} from "@/pages/jobs/status-groups";
import {
  workflowFilterOptions as buildWorkflowFilterOptions,
  workflowSourceFromRow,
  type WorkflowSource,
} from "@/pages/jobs/list/workflow-options";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { useUserTimezone } from "@/lib/time/user-timezone";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { JOB_SKELETON_ROWS, JOB_SKELETON_WIDTHS } from "@/pages/jobs/list/job-skeleton";
import {
  AWAITING_REVIEW_STATUS,
  jobFiltersFromUrl,
  jobStatusRequest,
  normalizeStatusList,
  periodFromUrl,
  RUN_SOURCE_PARAM,
  runSourceFromUrl,
} from "@/pages/jobs/list/job-filters";
import {
  FALLBACK_BOOTSTRAP_ROWS,
  filterJobRows,
  jobDisplayRow,
  normalizeEntityId,
  normalizeJobRows,
  normalizeWorkflowId,
  selectedWorkspaceIdFromStorage,
  workflowDisplayName,
  workflowLabel,
  type JobsWindow,
} from "@/pages/jobs/list/job-rows";
import { buildJobsGridConfig } from "@/pages/jobs/list/jobs-grid-config";
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
  type DsFilterField,
  type DsOption,
} from "@/components/shared/design-system";
import { columnsFromGridConfig, visibleRowActions } from "@/components/shared/data-grid-adapter";


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
 * ADR 0075: a section is this same list with its project and workflow fixed in
 * the background (`operator_view=1`). The section brings its own header (and its
 * New button), the columns and filters an admin chose, and the columns this
 * person hid; Project and Workflow are known, so neither shows.
 */
export type JobsListScope = {
  key: string;
  projectId: string;
  workflowId: string;
  title: string;
  description: string;
  eyebrow: string;
  action?: ReactNode;
  notice?: ReactNode;
  /** Extra controls at the end of the toolbar (the column picker). */
  toolbar?: ReactNode;
  columns: Array<{ key: string; label: string; cell: (row: Record<string, unknown>) => ReactNode }>;
  /** Column keys (built-in or configured) this person hid. */
  hiddenColumns?: ReadonlySet<string>;
  /** Configured filters in the filter panel; their values live in the URL under their keys. */
  filterFields?: DsFilterField[];
  /** The `filters` query value for the current panel values, or null when none applies. */
  filtersParam?: (values: Readonly<Record<string, string>>) => Record<string, unknown> | null;
  /**
   * Where a row opens, with the list it came from (its URL filters and page), so
   * the job page can step through the same list and lead back to it.
   */
  jobPath?: (jobId: string, list: Readonly<{ params: URLSearchParams; page: number; perPage: number }>) => string;
};

const NO_FILTER_FIELDS: DsFilterField[] = [];

const SCOPED_HIDDEN_COLUMNS = new Set(["_workflow_display", "project_name"]);

export function JobsListPage({ scope }: Readonly<{ scope?: JobsListScope }> = {}) {
  const { t } = useI18n();
  const auth = useAuth();
  const userTimezone = useUserTimezone();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const scopeFilterFields = scope?.filterFields ?? NO_FILTER_FIELDS;
  const filterParamKeys = useMemo(
    () => [
      "search",
      "project_id",
      "workflow_id",
      "status",
      "hide_acknowledged",
      "period",
      "from_date",
      "to_date",
      ...(scope ? [RUN_SOURCE_PARAM] : []),
      ...scopeFilterFields.map((field) => field.key),
    ],
    [scope, scopeFilterFields],
  );
  const [searchParams, setSearchParams] = useFilterSearchParams(scope ? `section-jobs:${scope.key}` : "jobs", filterParamKeys);
  // The admin's filters of a section: panel values from the URL, sent as one JSON value.
  const scopeFilterValues: Record<string, string> = Object.fromEntries(
    scopeFilterFields.map((field) => [field.key, searchParams.get(field.key) ?? ""]),
  );
  const operatorFilters = scope?.filtersParam?.(scopeFilterValues) ?? null;
  // A section's list filters by how runs started; the queue carries it too.
  const runSource = scope ? runSourceFromUrl(searchParams) : undefined;
  const operatorFiltersKey = operatorFilters ? JSON.stringify(operatorFilters) : "";
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
  // A section reopens on the page its job page led back to.
  const [page, setPage] = useState(() => (scope ? Math.max(1, Math.trunc(Number(searchParams.get("page"))) || 1) : 1));
  const [perPage, setPerPage] = useState(() => {
    const requested = scope ? Number(searchParams.get("per_page")) : 0;
    return [10, 25, 50].includes(requested) ? requested : 25;
  });
  const jobPathOf = (jobId: string) =>
    scope?.jobPath ? scope.jobPath(jobId, { params: searchParams, page, perPage }) : `/jobs/${jobId}`;
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
  const filterScope = JSON.stringify([deferredSearch, hideAcknowledged, projectFilter, selectedWorkspaceId, statusFilter, workflowFilter, fromDate, toDate, operatorFiltersKey, runSource]);
  const selectionScopeRef = useRef({ key: filterScope, generation: 0 });
  if (selectionScopeRef.current.key !== filterScope) {
    selectionScopeRef.current = { key: filterScope, generation: selectionScopeRef.current.generation + 1 };
  }

  // ADR 0076: the actions follow the permissions of the routes they call.
  const roleFlags = useMemo(() => {
    const permissions = sessionPermissions(auth.session);
    return {
      // Bulk delete: POST /api/jobs/bulk-delete.
      isAdmin: permissions.has("jobs.manage"),
      // Cancel: POST /api/jobs/{id}/cancel.
      canMutate: permissions.has("jobs.write"),
      // The workflow filter's names: GET /api/workflows.
      canReadRegistry: permissions.has("registry.read"),
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

  function getFilteredRows(): JobListItem[] {
    return filterJobRows(allJobsRef.current, {
      search: deferredSearch,
      projectId: projectFilter,
      status: statusFilter,
      workflowId: workflowFilter,
      hideAcknowledged,
      displayNames: workflowDisplayNamesRef.current,
      sources: workflowSourceRef.current,
    });
  }

  function renderJobs() {
    syncFromWindowGlobals();
    const context = {
      t,
      userTimezone,
      displayNames: workflowDisplayNamesRef.current,
      sources: workflowSourceRef.current,
    };
    setDisplayRows(getFilteredRows().map((job) => jobDisplayRow(job, context)));
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
      jobStatusRequest(statusFilter).forEach((status) => params.append("status", status));
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
        status: jobStatusRequest(statusFilter).join(","),
        hideAcknowledged,
        projectId: projectFilter || null,
        workflowId: workflowFilter || null,
        operatorView: Boolean(scope),
        operatorFilters,
        taskSource: runSource,
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
      ignoreAsyncResult(navigate(jobPathOf(jobId)));
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
  }, [deferredSearch, hideAcknowledged, page, perPage, projectFilter, selectedWorkspaceId, statusFilter, workflowFilter, fromDate, toDate, operatorFiltersKey, runSource]);

  useEffect(() => {
    setSelectedJobs(new Set());
  }, [deferredSearch, hideAcknowledged, projectFilter, selectedWorkspaceId, statusFilter, workflowFilter, fromDate, toDate, operatorFiltersKey, runSource]);

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
    // A section knows its project and workflow; it offers neither as a filter.
    if (scope) {
      return () => {
        active = false;
      };
    }
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
    // GET /api/workflows needs registry.read; a viewer's options come from the jobs.
    if (!roleFlags.canReadRegistry || scope) {
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
  }, [roleFlags.canReadRegistry]);

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
    () => buildJobsGridConfig(t, roleFlags),
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
    const hidden = scope?.hiddenColumns;
    const visible = scope
      ? columns.filter((column) => !SCOPED_HIDDEN_COLUMNS.has(column.key) && !hidden?.has(column.key))
      : columns;
    const withSection = scope
      ? [
          ...visible.filter((column) => column.key !== "actions"),
          ...scope.columns
            .filter((column) => !hidden?.has(column.key))
            .map((column) => ({ key: `section:${column.key}`, header: column.label, cell: column.cell })),
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
                ? [
                    {
                      key: "status_filter",
                      label: t("account_settings.card.status", "Status"),
                      // The operator's main queue: built in, no admin setup (ADR 0075).
                      options: [{ value: AWAITING_REVIEW_STATUS, label: t("react.operator.status_awaiting_review", "Awaiting review") }, ...filterOptions.statuses],
                      multiple: true,
                    },
                    {
                      key: "source_filter",
                      label: t("react.operator.filter_source", "Source"),
                      options: [
                        { value: "manual_ui", label: t("react.operator.source_form", "Form") },
                        { value: "mcp_workflow", label: t("react.operator.source_api", "API and MCP") },
                      ],
                    },
                    ...scopeFilterFields,
                  ]
                : [
                    { key: "project_filter", label: t("events.columns.project", "Project"), options: filterOptions.projects },
                    { key: "status_filter", label: t("account_settings.card.status", "Status"), options: filterOptions.statuses, multiple: true },
                    { key: "workflow_filter", label: t("jobs.columns.workflow", "Workflow"), options: filterOptions.workflows },
                  ]
            }
            onChange={(key, value) => {
              setPage(1);
              if (key === "source_filter") {
                setSearchParams((latest) => {
                  const next = new URLSearchParams(latest);
                  if (value) next.set(RUN_SOURCE_PARAM, value);
                  else next.delete(RUN_SOURCE_PARAM);
                  return next;
                }, { replace: true });
              } else if (scopeFilterFields.some((field) => field.key === key)) {
                setSearchParams((latest) => {
                  const next = new URLSearchParams(latest);
                  if (value) next.set(key, value);
                  else next.delete(key);
                  return next;
                }, { replace: true });
              } else if (key === "project_filter") {
                setProjectFilter(value);
              } else if (key === "status_filter") {
                setStatusFilter(normalizeStatusList([value]));
              } else {
                setWorkflowFilter(value);
              }
            }}
            onClear={() => {
              setPage(1);
              if (scope) {
                // One URL update: the status and the section's own filters go together.
                setSearchParams((latest) => {
                  const next = new URLSearchParams(latest);
                  next.delete("status");
                  next.delete(RUN_SOURCE_PARAM);
                  scopeFilterFields.forEach((field) => next.delete(field.key));
                  return next;
                }, { replace: true });
                return;
              }
              setProjectFilter("");
              setStatusFilter("");
              setWorkflowFilter("");
            }}
            values={{
              project_filter: projectFilter,
              status_filter: statusFilter,
              workflow_filter: workflowFilter,
              source_filter: runSource ?? "",
              ...scopeFilterValues,
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
          <DsSearchInput
            aria-label={t("react.jobs.search_jobs", "Search jobs")}
            id="search"
            onValueChange={(value) => {
              setPage(1);
              setSearch(value);
            }}
            placeholder={t("react.jobs.search_jobs_2", "Search jobs…")}
            value={search}
          />
          {scope?.toolbar}
        </div>
        <DsTable
          rowClickFocusable={false}
          ariaLabel={t("jobs.page_title", "Jobs")}
          columns={jobColumns}
          onRowClick={(row) => {
            const jobId = normalizeEntityId(row.job_id || row.id);
            if (jobId) {
              ignoreAsyncResult(navigate(jobPathOf(jobId)));
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
