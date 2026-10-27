import type { JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import type { HookFrame } from "@/pages/workflows/builder/hook-frames";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import { workflowCanvasNodeIdForExecutedLocation } from "./workflow-runtime-location";

export type HookExecutionOwner = Pick<HookFrame, "hookId" | "stepId" | "targetWorkflowId" | "targetRevision">;
export type HookExecutionMark = Omit<JobExecutionSnapshot["steps"][number], "stepId"> & {
  jobId: string; definitionDigest?: string;
  /** Passes of this step in the run (a loop runs it more than once), and their summed time. */
  passes?: number; totalDurationMs?: number;
};
export type HookExecutionMarks = Record<string, HookExecutionMark>;

/** How often each step ran in this run, and how long all its passes took together. */
function passTotals(snapshot: JobExecutionSnapshot) {
  const totals = new Map<string, { passes: number; durationMs: number; timed: boolean }>();
  const seen = new Set<string>();
  for (const step of snapshot.steps) {
    const identity = step.stepExecutionId ? `${step.stepId}:${step.stepExecutionId}` : "";
    if (identity && seen.has(identity)) continue;
    if (identity) seen.add(identity);
    const total = totals.get(step.stepId) || { passes: 0, durationMs: 0, timed: false };
    total.passes += 1;
    if (typeof step.durationMs === "number") { total.durationMs += step.durationMs; total.timed = true; }
    totals.set(step.stepId, total);
  }
  return totals;
}

/** History is chronological; occurrence also distinguishes retry/loop executions. */
function latestExecutions(snapshot: JobExecutionSnapshot) {
  const latest = new Map<string, JobExecutionSnapshot["steps"][number]>();
  for (const step of snapshot.steps) {
    const previous = latest.get(step.stepId);
    if (!previous || (step.occurrence || 1) >= (previous.occurrence || 1)) latest.set(step.stepId, step);
  }
  return latest;
}

/** Only recorded, normalized evidence; no live editor values or inferred timing. */
function executionMark(snapshot: JobExecutionSnapshot, step: JobExecutionSnapshot["steps"][number]): HookExecutionMark | null {
  const { stepId, ...execution } = step;
  if (!stepId) return null;
  return { ...execution, jobId: snapshot.jobId, ...(snapshot.definitionDigest ? { definitionDigest: snapshot.definitionDigest } : {}) };
}

/** The ordinary author steps of this exact archived job and displayed revision. */
export function workflowExecutionMarks(snapshot: JobExecutionSnapshot | null, graph: WorkflowCanvasGraph, workflowId: string, revision: number, jobId?: string): HookExecutionMarks {
  if (!snapshot?.complete || snapshot.definitionWorkflowId !== workflowId || snapshot.definitionRevision !== revision
    || (jobId !== undefined && snapshot.jobId !== jobId) || (graph.workflow?.id && graph.workflow.id !== workflowId)) return {};
  const declared = new Set(snapshot.definitionSteps.map((step) => step.stepId));
  const marks: HookExecutionMarks = {};
  const totals = passTotals(snapshot);
  for (const step of latestExecutions(snapshot).values()) {
    if (!declared.has(step.stepId) || !step.stepExecutionId) continue;
    const nodeId = workflowCanvasNodeIdForExecutedLocation(graph, {
      stepId: step.stepId, groupId: step.groupId, forEachId: step.forEachId,
      definitionWorkflowId: snapshot.definitionWorkflowId, definitionRevision: snapshot.definitionRevision,
    }, workflowId, revision);
    const mark = executionMark(snapshot, step);
    const total = totals.get(step.stepId);
    if (nodeId && mark) marks[nodeId] = total && total.passes > 1
      ? { ...mark, passes: total.passes, ...(total.timed ? { totalDurationMs: total.durationMs } : {}) } : mark;
  }
  return marks;
}

/** Resolve the latest invocation from the archived author, never the current binding alone. */
export function hookInvocationForExecution(snapshot: JobExecutionSnapshot | null, frame: HookExecutionOwner, workflowId: string, revision: number): JobExecutionSnapshot["childInvocations"][number] | null {
  if (!snapshot?.complete || snapshot.definitionWorkflowId !== workflowId || snapshot.definitionRevision !== revision) return null;
  if (!snapshot.definitionSteps.some((step) => step.stepId === frame.stepId && step.hookId === frame.hookId)) return null;
  const execution = latestExecutions(snapshot).get(frame.stepId);
  if (!execution?.stepExecutionId) return null;
  const children = snapshot.childInvocations.filter((child) => child.stepId === frame.stepId
    && child.stepExecutionId === execution.stepExecutionId && child.childWorkflowId === frame.targetWorkflowId);
  return children.length === 1 ? children[0] : null;
}

/** Keep completed/failed member evidence only on the exact child definition on screen. */
export function hookExecutionMarks(snapshot: JobExecutionSnapshot | null, frame: HookExecutionOwner, graph: WorkflowCanvasGraph): HookExecutionMarks {
  if (!snapshot?.complete || !frame.targetRevision || snapshot.definitionWorkflowId !== frame.targetWorkflowId
    || snapshot.definitionRevision !== frame.targetRevision) return {};
  const marks: HookExecutionMarks = {};
  const frames = new Map([[frame.stepId, frame]]);
  for (const step of latestExecutions(snapshot).values()) {
    const nodeId = workflowCanvasNodeIdForExecutedLocation(graph, {
      stepId: step.stepId, hookId: frame.hookId,
      definitionWorkflowId: snapshot.definitionWorkflowId, definitionRevision: snapshot.definitionRevision,
    }, "", 0, frames);
    if (!nodeId) continue;
    const mark = executionMark(snapshot, step);
    if (mark) marks[nodeId] = mark;
  }
  return marks;
}
