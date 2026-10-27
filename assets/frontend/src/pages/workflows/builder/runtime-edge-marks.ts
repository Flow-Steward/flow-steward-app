import type { JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import { runtimeEdgeKey, sameTraceEdge, type JobExecutionTraceEvidence } from "@/pages/jobs/detail/job-execution-trace";
import type { WorkflowCanvasGraph, WorkflowGraphEdge, WorkflowNodeRef, WorkflowTargetRef } from "@/pages/workflows/shared/api";
import type { HookExecutionOwner } from "@/pages/workflows/detail/detail-hook-execution-marks";
import { hookGroupId, hookMemberId } from "./hook-frames";

/** Step-history completeness is unrelated to the committed trace's coverage. */
export function snapshotTraceForOwner(snapshot: JobExecutionSnapshot | null | undefined, workflowId: string, revision: number, jobId?: string): JobExecutionTraceEvidence | undefined {
  const evidence = snapshot?.runTrace, trace = evidence?.trace;
  if (!snapshot || !trace || revision <= 0 || snapshot.definitionWorkflowId !== workflowId || snapshot.definitionRevision !== revision
    || jobId !== undefined && snapshot.jobId !== jobId || trace.job_id !== snapshot.jobId || trace.workflow_id !== workflowId
    || trace.revision !== revision || !snapshot.definitionSnapshotId || trace.snapshot_id !== snapshot.definitionSnapshotId
    || !snapshot.definitionDigest || trace.digest !== snapshot.definitionDigest) return undefined;
  return evidence;
}

/** Project only exact archived facts; never infer traversal from statuses or endpoints. */
export function runtimeEdgeMarks(evidence: JobExecutionTraceEvidence | undefined, graph: WorkflowCanvasGraph,
  frames: ReadonlyMap<string, HookExecutionOwner> = new Map(), hookId?: string): Set<string> {
  return new Set(runtimeEdgePassCounts(evidence, graph, frames, hookId).keys());
}

/** How often the run took each connection (a loop takes some more than once). */
export function runtimeEdgePassCounts(evidence: JobExecutionTraceEvidence | undefined, graph: WorkflowCanvasGraph,
  frames: ReadonlyMap<string, HookExecutionOwner> = new Map(), hookId?: string): Map<string, number> {
  const marks = new Map<string, number>(), trace = evidence?.trace;
  if (!evidence || trace?.schema_version !== 1 || !trace.covered_from_start || trace.truncated) return marks;
  const childOwner = hookId ? [...frames.values()].find((frame) => frame.hookId === hookId) : undefined;
  if (hookId ? childOwner?.targetWorkflowId !== trace.workflow_id || childOwner.targetRevision !== trace.revision
    : graph.workflow.id !== trace.workflow_id || graph.workflow.revision !== trace.revision) return marks;
  const mapNode = (ref: WorkflowNodeRef): WorkflowNodeRef => {
    if (hookId) return { ...ref, id: hookMemberId(hookId, ref.id) };
    const frame = ref.kind === "step" ? [...frames.values()].find((candidate) => candidate.stepId === ref.id) : undefined;
    return frame && graph.groups.some((group) => group.id === hookGroupId(frame.hookId) && group.hook?.stepId === ref.id)
      ? { kind: "group", id: hookGroupId(frame.hookId) } : ref;
  };
  const mapTarget = (ref: WorkflowTargetRef): WorkflowTargetRef => {
    if (ref.kind === "group_exit") return hookId ? { ...ref, group_id: hookMemberId(hookId, ref.group_id) } : ref;
    if (ref.kind === "for_each_exit") return hookId ? { ...ref, for_each_id: hookMemberId(hookId, ref.for_each_id) } : ref;
    return mapNode(ref);
  };
  for (const edge of evidence.edges) {
    const projected: WorkflowGraphEdge = { ...edge, source: mapNode(edge.source), target: mapTarget(edge.target) };
    const matches = graph.edges.filter((candidate) => sameTraceEdge(projected, candidate));
    if (matches.length === 1) {
      const key = runtimeEdgeKey(matches[0]);
      marks.set(key, (marks.get(key) || 0) + 1);
    }
  }
  return marks;
}
