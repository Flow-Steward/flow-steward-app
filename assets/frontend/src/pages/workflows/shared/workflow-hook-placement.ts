import type { WorkflowPlacement } from "@/pages/workflows/builder/change-set";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

/** A hook is an Invoke declaration, distinct from an ordinary reusable child call. */
export function workflowStepDeclaresHook(kind: unknown, override: unknown): boolean {
  const hook = record(record(override).invoke_workflow).hook;
  return kind === "invoke_workflow" && Boolean(hook && typeof hook === "object" && !Array.isArray(hook));
}

/** Author hooks run before or after containers, never as members of their body. */
export function workflowHookPlacementInvalid(kind: unknown, override: unknown, placement: Partial<WorkflowPlacement>): boolean {
  return workflowStepDeclaresHook(kind, override) && Boolean(placement.groupId || placement.forEachId);
}
