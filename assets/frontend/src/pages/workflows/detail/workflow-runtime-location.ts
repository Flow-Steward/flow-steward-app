import { hookMemberId, type HookFrame } from "@/pages/workflows/builder/hook-frames";

import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";

/** Where a run is now: the step it executes, or the real group whose lanes run. */
export type WorkflowRuntimeLocation = {
  stepId: string;
  groupId?: string;
  forEachId?: string;
  definitionWorkflowId?: string;
  definitionRevision?: number;
  /** The declaring hook disambiguates two bindings of the same child workflow. */
  hookId?: string;
};

export function workflowCanvasNodeIdForRuntimeLocation(
  canvasGraph: WorkflowCanvasGraph | null | undefined,
  location: WorkflowRuntimeLocation | null,
): string {
  if (!canvasGraph || !location) {
    return "";
  }
  const stepId = location.stepId.trim();
  if (stepId) {
    const step = (canvasGraph.steps || []).find(
      (candidate) => candidate.id === stepId || candidate.step_id === stepId,
    );
    if (step) {
      return step.id;
    }
  }
  const groupId = (location.groupId || "").trim();
  if (groupId && (canvasGraph.groups || []).some((group) => group.id === groupId || group.group_id === groupId)) {
    return `group:${groupId}`;
  }
  const forEachId = (location.forEachId || "").trim();
  if (forEachId && (canvasGraph.for_each || []).some((container) => container.id === forEachId)) {
    return `for_each:${forEachId}`;
  }
  return "";
}

/** Never paint a historical run onto a different live definition. */
export function workflowCanvasNodeIdForExecutedLocation(
  canvasGraph: WorkflowCanvasGraph | null | undefined,
  location: WorkflowRuntimeLocation | null,
  workflowId: string,
  revision: number,
  frames?: ReadonlyMap<string, Pick<HookFrame, "hookId" | "targetWorkflowId" | "targetRevision">>,
): string {
  if (!location) return "";
  if (location.definitionWorkflowId === workflowId && location.definitionRevision === revision) {
    return workflowCanvasNodeIdForRuntimeLocation(canvasGraph, location);
  }
  const owners = [...(frames?.values() || [])].filter((frame) =>
    frame.targetWorkflowId === location.definitionWorkflowId
    && frame.targetRevision !== null
    && frame.targetRevision === location.definitionRevision
    && (!location.hookId || frame.hookId === location.hookId));
  if (owners.length !== 1) return "";
  const hookId = owners[0].hookId;
  return workflowCanvasNodeIdForRuntimeLocation(canvasGraph, {
    ...location,
    stepId: location.stepId ? hookMemberId(hookId, location.stepId) : "",
    groupId: location.groupId ? hookMemberId(hookId, location.groupId) : "",
    forEachId: location.forEachId ? hookMemberId(hookId, location.forEachId) : "",
  });
}
