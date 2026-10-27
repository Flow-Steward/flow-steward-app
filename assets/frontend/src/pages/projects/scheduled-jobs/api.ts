import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type ScheduledJob = Record<string, unknown> & {
  id?: string;
  workflow_id?: string;
  status?: string;
  enabled?: boolean;
  next_run_at_user_local?: string;
  next_run_at_utc?: string;
};

export type ScheduleWorkflowChoice = { workflow_id: string; display_name?: string; enabled?: boolean };

export async function fetchProjectScheduleWorkflows(projectId: string): Promise<ScheduleWorkflowChoice[]> {
  const payload = await fetchJson<{ workflows: ScheduleWorkflowChoice[] }>(
    `/api/projects/${encodeURIComponent(projectId)}/workflow-catalog?enabled_only=false`,
  );
  return Array.isArray(payload.workflows) ? payload.workflows : [];
}

export type ScheduledRun = Record<string, unknown> & {
  id?: string;
  scheduled_job_id?: string;
  status?: string;
  created_at_user_local?: string;
};

export type ScheduledJobsPayload = {
  project_id: string;
  timezone_context: Record<string, unknown>;
  jobs: ScheduledJob[];
  runs: ScheduledRun[];
};

type ScheduledJobsRaw = {
  project_id?: string;
  timezone_context?: Record<string, unknown>;
  jobs?: ScheduledJob[];
  runs?: ScheduledRun[];
  [key: string]: unknown;
};

export type ScheduledJobCreateInput = {
  workflow_id: string;
  cron_expr: string;
  schedule_timezone?: string;
  inputs?: Record<string, unknown>;
  enabled?: boolean;
};

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

export async function fetchProjectScheduledJobs(projectId: string, limitRuns = 20): Promise<ScheduledJobsPayload> {
  const payload = await fetchJson<ScheduledJobsRaw>(
    `/api/projects/${encodeURIComponent(projectId)}/scheduled-jobs?limit_runs=${Math.max(1, Math.min(limitRuns, 200))}`,
  );
  return {
    project_id: firstStringFromUnknownValue([payload.project_id, projectId, ""]).trim(),
    timezone_context:
      payload.timezone_context && typeof payload.timezone_context === "object" ? payload.timezone_context : {},
    jobs: asArray<ScheduledJob>(payload.jobs),
    runs: asArray<ScheduledRun>(payload.runs),
  };
}

export async function createProjectScheduledJob(projectId: string, input: ScheduledJobCreateInput) {
  const body = {
    cron_expr: input.cron_expr,
    enabled: input.enabled,
    schedule_timezone: firstStringFromUnknownValue([input.schedule_timezone, ""]).trim() || undefined,
    inputs: input.inputs,
    workflow_id: input.workflow_id,
  };
  return fetchJson<Record<string, unknown>>(`/api/projects/${encodeURIComponent(projectId)}/scheduled-jobs`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function toggleProjectScheduledJob(projectId: string, scheduledJobId: string, enabled: boolean) {
  return fetchJson<Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/scheduled-jobs/${scheduledJobId}/toggle`,
    {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    },
  );
}

export async function runProjectScheduledJobNow(projectId: string, scheduledJobId: string) {
  return fetchJson<Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/scheduled-jobs/${scheduledJobId}/run-now`,
    {
      method: "POST",
      body: JSON.stringify({}),
    },
  );
}

export async function deleteProjectScheduledJob(projectId: string, scheduledJobId: string) {
  return fetchJson<Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/scheduled-jobs/${scheduledJobId}`,
    {
      method: "DELETE",
    },
  );
}

export async function updateProjectScheduledJob(projectId: string, scheduledJobId: string, input: ScheduledJobCreateInput) {
  return fetchJson<Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/scheduled-jobs/${encodeURIComponent(scheduledJobId)}`,
    { method: "PUT", body: JSON.stringify(input) },
  );
}

export async function previewProjectCron(projectId: string, cronExpr: string, count = 5, scheduleTimezone?: string) {
  const query = new URLSearchParams();
  query.set("cron_expr", cronExpr);
  query.set("count", stringFromUnknownValue(Math.max(1, Math.min(count, 20))));
  if (scheduleTimezone) query.set("schedule_timezone", scheduleTimezone);
  return fetchJson<Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/cron-preview?${query.toString()}`,
  );
}
