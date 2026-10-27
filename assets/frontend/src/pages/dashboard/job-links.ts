import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { jobStatusGroup, type JobStatusGroupKey } from "@/pages/jobs/status-groups";

/** The red card: runs that broke, in the two groups an operator acknowledges. */
export const FAILED_OR_STUCK_GROUPS: readonly JobStatusGroupKey[] = ["failed", "stuck"];

/**
 * The groups of the statuses the server files under needs_attention: the
 * failures, the watchdog's stuck and orphaned marks, a run stopped on a
 * retryable step (Interrupted) and approvals. Interrupted also holds blocked
 * runs, which the queue leaves to its own lane, so the list can show those too.
 */
export const ATTENTION_QUEUE_GROUPS: readonly JobStatusGroupKey[] = ["failed", "stuck", "interrupted", "awaiting_approval"];

/**
 * The job list takes one `status` parameter per group and matches any of them;
 * `hide_acknowledged` leaves out triaged runs, as the attention counts do.
 */
export function jobsListHref(groups: readonly JobStatusGroupKey[], options: { hideAcknowledged?: boolean } = {}): string {
  const params = new URLSearchParams();
  new Set(groups).forEach((group) => params.append("status", group));
  if (options.hideAcknowledged) params.set("hide_acknowledged", "1");
  const query = params.toString();
  return query ? `/jobs?${query}` : "/jobs";
}

/** Rows of the needs_attention group in the Failed or Stuck group. */
export function failedOrStuckRows(
  rows: ReadonlyArray<Record<string, unknown>>,
): Array<Record<string, unknown>> {
  return rows.filter((row) => {
    const group = jobStatusGroup(firstStringFromUnknownValue([row.status, ""]));
    return Boolean(group && FAILED_OR_STUCK_GROUPS.includes(group));
  });
}
