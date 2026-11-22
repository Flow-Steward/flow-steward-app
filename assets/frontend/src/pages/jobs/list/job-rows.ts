import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { formatUserDateTime } from "@/lib/time/user-timezone";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { readStoredWorkspaceId } from "@/lib/utils/workspace-selection";
import { jobStatusParams, type JobListItem } from "@/pages/jobs/list/api";
import { AWAITING_REVIEW_ROW_STATUSES, AWAITING_REVIEW_STATUS } from "@/pages/jobs/list/job-filters";
import { findWorkflowSource, type WorkflowSource } from "@/pages/jobs/list/workflow-options";
import { jobStatusGroup, jobStatusGroupLabel } from "@/pages/jobs/status-groups";

export type JobsWindow = Window & {
  _allJobs?: JobListItem[];
  _workflowDisplayNames?: Record<string, string>;
  renderJobs?: () => void;
  _rebuildStatusFilter?: () => void;
  _syncJobFiltersAndGrid?: () => void;
  refreshJobs?: () => Promise<void>;
};

export const FALLBACK_BOOTSTRAP_ROWS: JobListItem[] = [
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

export function normalizeEntityId(value: unknown): string {
  return firstStringFromUnknownValue([value, ""]).trim();
}

export function selectedWorkspaceIdFromStorage(): string {
  return normalizeEntityId(readStoredWorkspaceId());
}

export function normalizeJobRows(payload: unknown): JobListItem[] {
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

export function normalizeWorkflowId(job: Record<string, unknown>): string {
  return firstStringFromUnknownValue([job.selected_workflow, job.workflow_id, ""]).trim();
}

export function workflowLabel(workflowId: string, displayNames: Record<string, string>): string {
  if (!workflowId) {
    return "";
  }
  const display = firstStringFromUnknownValue([displayNames[workflowId], ""]).trim();
  if (!display || display === workflowId) {
    return workflowId;
  }
  return display;
}

export function workflowDisplayName(
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

function matchesStatusFilter(job: JobListItem, filterValue: string): boolean {
  const groups = jobStatusParams(filterValue);
  if (!groups.length) {
    return true;
  }
  // The server already read "awaiting review" down the Invoke chain.
  if (groups.includes(AWAITING_REVIEW_STATUS) && AWAITING_REVIEW_ROW_STATUSES.has(normalizeStatus(job.status))) {
    return true;
  }
  const group = jobStatusGroup(normalizeStatus(job.status));
  return Boolean(group && groups.includes(group));
}

/** The list's own filters, applied to the loaded page of jobs. */
export type JobRowFilters = Readonly<{
  search: string;
  projectId: string;
  status: string;
  workflowId: string;
  hideAcknowledged: boolean;
  displayNames: Record<string, string>;
  sources: ReadonlyMap<string, WorkflowSource>;
}>;

export function filterJobRows(jobs: readonly JobListItem[], filters: JobRowFilters): JobListItem[] {
  const searchQuery = filters.search.trim().toLowerCase();
  const projectValue = filters.projectId.trim();
  const statusValue = filters.status.trim();
  const workflowValue = filters.workflowId.trim();
  return jobs.filter((job) => {
    const workflowId = normalizeWorkflowId(job);
    if (searchQuery) {
      const haystack = [
        job.run_title || "",
        job.display_summary || job.task_summary || "",
        job.task_summary || "",
        job.project_name || "",
        workflowId,
        workflowDisplayName(job, filters.displayNames, filters.sources),
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
    if (filters.hideAcknowledged && job.acknowledged_at) {
      return false;
    }
    if (workflowValue && workflowId !== workflowValue) {
      return false;
    }
    return true;
  });
}

/** What a grid row needs beyond the job itself. */
export type JobDisplayContext = Readonly<{
  t: I18nContextValue["t"];
  userTimezone: string;
  displayNames: Record<string, string>;
  sources: ReadonlyMap<string, WorkflowSource>;
}>;

/** One job as the grid shows it: the job plus its display, tooltip and label fields. */
export function jobDisplayRow(
  job: JobListItem,
  { t, userTimezone, displayNames, sources }: JobDisplayContext,
): Record<string, unknown> {
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
    _workflow_display: workflowDisplayName(job, displayNames, sources),
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
}
