import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

/** Shared with the full Jobs header; the API remains the mutation authority. */
export function canMutateJob(roles: readonly unknown[]): boolean {
  const normalized = new Set(roles.map((role) => firstStringFromUnknownValue([role, ""]).trim().toLowerCase()).filter(isTruthy));
  return normalized.has("admin") || normalized.has("operator");
}

export function isCancellableJobStatus(status: unknown): boolean {
  return ["in_progress", "resume_pending", "pending", "awaiting_approval", "awaiting_timer", "awaiting_child", "running"]
    .includes(firstStringFromUnknownValue([status, ""]).trim().toLowerCase());
}
