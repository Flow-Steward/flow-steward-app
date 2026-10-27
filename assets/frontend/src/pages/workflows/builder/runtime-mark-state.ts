import type { HookExecutionMark } from "@/pages/workflows/detail/detail-hook-execution-marks";

export type RuntimeMarkState = "done" | "failed" | "running" | "waiting" | "skipped" | "kept";

/** The run state a recorded step execution shows on its card. */
export function runtimeMarkState(mark?: Pick<HookExecutionMark, "status" | "kept">): RuntimeMarkState | undefined {
  if (!mark) return undefined;
  const status = String(mark.status || "").toLowerCase();
  if (["failed", "error", "fail", "interrupted"].includes(status)) return "failed";
  if (["skipped", "cancelled", "canceled"].includes(status)) return "skipped";
  if (status.startsWith("awaiting") || status === "blocked") return "waiting";
  if (["in_progress", "running", "pending", "queued", "dispatched"].includes(status)) return "running";
  return mark.kept ? "kept" : "done";
}
