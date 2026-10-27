import type { DsBadgeTone } from "@/components/shared/design-system";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";

/**
 * The eight status groups operators filter and read by, in display order. A
 * stored status outside every group is unknown or legacy and has no group.
 * The backend keeps the same table in core/application/observability_vocabulary.py
 * (JOB_STATUS_GROUPS); tests/unit/test_job_status_groups.py pins the two.
 */
export const JOB_STATUS_GROUPS = [
  { key: "in_progress", label: "In progress", statuses: ["in_progress", "pending", "resume_pending"] },
  { key: "waiting", label: "Waiting", statuses: ["awaiting_timer", "awaiting_child", "external_wait"] },
  { key: "awaiting_approval", label: "Waiting for approval", statuses: ["awaiting_approval"] },
  { key: "stuck", label: "Stuck", statuses: ["stuck", "orphaned"] },
  { key: "interrupted", label: "Interrupted", statuses: ["retryable_interrupted_step", "blocked"] },
  { key: "failed", label: "Failed", statuses: ["failed", "max_iterations", "triage_needed"] },
  { key: "completed", label: "Completed", statuses: ["completed"] },
  { key: "cancelled", label: "Cancelled", statuses: ["cancelled"] },
] as const;

export type JobStatusGroupKey = (typeof JOB_STATUS_GROUPS)[number]["key"];

/** Red for what broke, orange for work that runs or waits, as the list painted it. */
const GROUP_TONES: Record<JobStatusGroupKey, DsBadgeTone> = {
  in_progress: "orange",
  waiting: "orange",
  awaiting_approval: "orange",
  stuck: "danger",
  interrupted: "orange",
  failed: "danger",
  completed: "green",
  cancelled: "neutral",
};

const GROUP_BY_STATUS = new Map<string, JobStatusGroupKey>(
  JOB_STATUS_GROUPS.flatMap((group) => group.statuses.map((status) => [status, group.key] as const)),
);

const GROUP_KEYS = new Set<string>(JOB_STATUS_GROUPS.map((group) => group.key));

function normalized(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

/** The group a stored status belongs to; `null` for an unknown or legacy code. */
export function jobStatusGroup(status: unknown): JobStatusGroupKey | null {
  return GROUP_BY_STATUS.get(normalized(status)) ?? null;
}

export function isJobStatusGroupKey(value: string): value is JobStatusGroupKey {
  return GROUP_KEYS.has(value);
}

export function jobStatusGroupLabel(group: JobStatusGroupKey, t: I18nContextValue["t"] = fallbackContext.t): string {
  const labels: Record<JobStatusGroupKey, string> = {
    in_progress: t("react.jobs.status_group_in_progress", "In progress"),
    waiting: t("react.jobs.status_group_waiting", "Waiting"),
    awaiting_approval: t("react.jobs.status_group_awaiting_approval", "Waiting for approval"),
    stuck: t("react.jobs.status_group_stuck", "Stuck"),
    interrupted: t("react.jobs.status_group_interrupted", "Interrupted"),
    failed: t("react.jobs.status_group_failed", "Failed"),
    completed: t("react.jobs.status_group_completed", "Completed"),
    cancelled: t("react.jobs.status_group_cancelled", "Cancelled"),
  };
  return labels[group];
}

export function jobStatusGroupTone(group: JobStatusGroupKey | null): DsBadgeTone {
  return group ? GROUP_TONES[group] : "neutral";
}

/** Every stored status of the given groups, in table order, for the jobs API. */
export function jobStatusesOfGroups(groups: readonly string[]): string[] {
  const chosen = new Set(groups);
  return JOB_STATUS_GROUPS.filter((group) => chosen.has(group.key)).flatMap((group) => [...group.statuses]);
}

/**
 * Group keys for URL values. A value is a group key or, from a link made before
 * the groups, a stored status, which means its group; an unknown value is
 * dropped. The result is deduplicated and in table order.
 */
export function jobStatusGroupsFromValues(values: readonly string[]): JobStatusGroupKey[] {
  const chosen = new Set<JobStatusGroupKey>();
  for (const raw of values.flatMap((value) => value.split(","))) {
    const value = normalized(raw);
    if (isJobStatusGroupKey(value)) chosen.add(value);
    else {
      const group = jobStatusGroup(value);
      if (group) chosen.add(group);
    }
  }
  return JOB_STATUS_GROUPS.map((group) => group.key).filter((key) => chosen.has(key));
}
