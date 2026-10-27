import { useQueries, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import type { FetchJsonOptions } from "@/lib/http/client";
import { queryKeys } from "@/lib/query/keys";
import { fetchWorkflowDetail } from "@/pages/workflows/shared/api";
import { fetchWorkflowHooks, workflowHooksQueryKey } from "@/pages/workflows/shared/hook-api";

/** One hook point of a workflow and, when it is known, its binding in a project. */
export type WorkflowHookState = {
  hookId: string;
  stepId: string;
  displayName: string;
  description: string;
  /** Whether the project connects a workflow here; null while that is not known. */
  connected: boolean | null;
  targetWorkflowId: string;
  /** The connected workflow's display name, or its id until the name is loaded. */
  targetName: string;
  /** The binding's revision in the project: the next bind or create states it. */
  bindingRevision: number;
  /** The steps the hook holds in the project (its hidden end excluded); null while unknown. */
  stepCount?: number | null;
};

/** The hidden end of a hook's storage workflow: its only step while the hook is empty (R2-11). */
export const HOOK_END_STEP_ID = "hook_end";

function hookStepCount(detail: unknown): number | null {
  const steps = record(detail).steps;
  if (!Array.isArray(steps)) return null;
  return steps.filter((step) => text(record(step).step_id) !== HOOK_END_STEP_ID).length;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** The hooks a definition declares: Invoke steps carrying `override.invoke_workflow.hook`. */
export function declaredWorkflowHooks(steps: unknown): WorkflowHookState[] {
  const rows = Array.isArray(steps) ? steps : [];
  return rows.flatMap((step) => {
    const raw = record(step);
    const hook = record(record(record(raw.override).invoke_workflow).hook);
    const hookId = text(hook.hook_id);
    const stepId = text(raw.step_id);
    if (!hookId || !stepId) return [];
    return [{
      hookId,
      stepId,
      displayName: text(hook.display_name) || text(raw.display_name) || hookId,
      description: text(hook.description),
      connected: null,
      targetWorkflowId: "",
      targetName: "",
      bindingRevision: 0,
    }];
  });
}

/**
 * The hooks a workflow declares, with their bindings in `projectId`. The steps
 * say where the hooks are, so they show before (and without) a project; the
 * project's hook list then says which are connected, and to which workflow.
 */
export function useWorkflowHookStates({
  enabled = true,
  options,
  projectId,
  steps,
  workflowId,
}: Readonly<{ enabled?: boolean; options?: FetchJsonOptions; projectId: string; steps: unknown; workflowId: string }>) {
  const declared = useMemo(() => declaredWorkflowHooks(steps), [steps]);
  const hooksQuery = useQuery({
    queryKey: workflowHooksQueryKey(projectId, workflowId),
    queryFn: () => options ? fetchWorkflowHooks(projectId, workflowId, options) : fetchWorkflowHooks(projectId, workflowId),
    enabled: enabled && declared.length > 0 && Boolean(projectId && workflowId),
    retry: false,
  });
  const bindings = useMemo(
    () => new Map((hooksQuery.data?.hooks || []).map((hook) => [hook.hook_id, hook])),
    [hooksQuery.data],
  );
  const targetIds = useMemo(
    () => [...new Set([...bindings.values()].map((hook) => text(hook.binding?.target_workflow_id)).filter(Boolean))],
    [bindings],
  );
  const targetQueries = useQueries({
    queries: targetIds.map((targetId) => ({
      queryKey: queryKeys.workflowDetail(targetId),
      queryFn: () => options ? fetchWorkflowDetail(targetId, options) : fetchWorkflowDetail(targetId),
      enabled,
      retry: false,
      staleTime: 30_000,
    })),
  });
  // One string, so the hooks below keep their identity until a name changes:
  // the canvas lays itself out again whenever they change.
  const targetNameKey = targetQueries.map((query) => text(query.data?.display_name)).join("\u0000");
  const stepCountKey = targetQueries.map((query) => String(hookStepCount(query.data) ?? "")).join(",");
  const loaded = hooksQuery.isSuccess;
  const hooks = useMemo(() => {
    const names = targetNameKey.split("\u0000");
    const targetNames = new Map(targetIds.map((targetId, index) => [targetId, names[index] || targetId]));
    const counts = stepCountKey.split(",");
    const stepCounts = new Map(targetIds.map((targetId, index) => [targetId, counts[index] === "" || counts[index] === undefined ? null : Number(counts[index])]));
    return declared.map((hook) => {
      if (!loaded) return hook;
      const candidate = bindings.get(hook.hookId);
      const bound = candidate?.step_id === hook.stepId ? candidate : undefined;
      const targetWorkflowId = text(bound?.binding?.target_workflow_id);
      return {
        ...hook,
        displayName: hook.displayName,
        description: hook.description,
        connected: Boolean(targetWorkflowId),
        targetWorkflowId,
        targetName: targetWorkflowId ? targetNames.get(targetWorkflowId) || targetWorkflowId : "",
        bindingRevision: Number(bound?.binding_revision || 0),
        stepCount: targetWorkflowId ? stepCounts.get(targetWorkflowId) ?? null : 0,
      };
    });
  }, [bindings, declared, loaded, stepCountKey, targetIds, targetNameKey]);
  return { hooks, error: hooksQuery.error, isLoading: hooksQuery.isLoading };
}
