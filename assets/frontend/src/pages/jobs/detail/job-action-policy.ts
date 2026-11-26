import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

/**
 * Cancel, restart and acknowledge need `jobs.write` (ADR 0076), the permission of
 * POST /api/jobs/{id}/cancel and its siblings. Shared with the full Jobs header; the
 * API remains the mutation authority.
 */
export function canMutateJob(permissions: ReadonlySet<string>): boolean {
  return permissions.has("jobs.write");
}

export function isCancellableJobStatus(status: unknown): boolean {
  return ["in_progress", "resume_pending", "pending", "awaiting_approval", "awaiting_timer", "awaiting_child", "awaiting_event", "running"]
    .includes(firstStringFromUnknownValue([status, ""]).trim().toLowerCase());
}
