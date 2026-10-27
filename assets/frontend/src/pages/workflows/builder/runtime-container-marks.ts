import type { JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import type { ContainerPass, JobContainerProgressEvidence } from "@/pages/jobs/detail/job-container-progress";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import type { HookExecutionOwner } from "@/pages/workflows/detail/detail-hook-execution-marks";
import { hookMemberId } from "./hook-frames";

export type ContainerRuntimeMark = {
  jobId: string; executionId: string; status: "running" | "completed" | "failed";
  items?: { processed: number; selected: number };
  lanes?: { completed: number; total: number };
};
export type ContainerRuntimeMarks = Record<string, ContainerRuntimeMark>;

export function snapshotContainersForOwner(snapshot: JobExecutionSnapshot | null | undefined, workflowId: string, revision: number,
  projectId: string | undefined, jobId?: string): JobContainerProgressEvidence | undefined {
  const evidence = snapshot?.containerProgress, progress = evidence?.progress;
  if (!snapshot || !progress || !projectId || revision <= 0 || snapshot.definitionWorkflowId !== workflowId || snapshot.definitionRevision !== revision
    || jobId !== undefined && snapshot.jobId !== jobId || progress.job_id !== snapshot.jobId || progress.project_id !== projectId
    || progress.workflow_id !== workflowId || progress.revision !== revision || progress.snapshot_id !== snapshot.definitionSnapshotId
    || progress.digest !== snapshot.definitionDigest) return undefined;
  return evidence;
}

/** Newest pass identity is chosen before lifecycle: unfinished terminal evidence stays neutral. */
export function containerRuntimeMarks(evidence: JobContainerProgressEvidence | undefined, graph: WorkflowCanvasGraph,
  frames: ReadonlyMap<string, HookExecutionOwner> = new Map(), hookId?: string): ContainerRuntimeMarks {
  const marks: ContainerRuntimeMarks = {}, progress = evidence?.progress;
  if (!progress?.covered_from_start || progress.truncated) return marks;
  const frame = hookId ? [...frames.values()].find((candidate) => candidate.hookId === hookId) : undefined;
  if (hookId ? frame?.targetWorkflowId !== progress.workflow_id || frame.targetRevision !== progress.revision
    : graph.workflow.id !== progress.workflow_id || graph.workflow.revision !== progress.revision) return marks;
  const newest = new Map<string, ContainerPass>();
  for (const pass of progress.passes) {
    const key = JSON.stringify([pass.container.kind, pass.container.id]);
    const old = newest.get(key);
    if (!old || pass.started_sequence > old.started_sequence) newest.set(key, pass);
  }
  for (const pass of newest.values()) {
    if (pass.parent_frames.some((parent) => newest.get(JSON.stringify([parent.kind, parent.id]))?.execution_id !== parent.execution_id)) continue;
    const id = hookId ? hookMemberId(hookId, pass.container.id) : pass.container.id;
    const exists = pass.container.kind === "group"
      ? graph.groups.some((group) => group.id === id && !group.hook)
      : graph.for_each?.some((item) => item.id === id);
    if (!exists) continue;
    const status = pass.current && !progress.checkpoint_terminal ? "running" : pass.outcome === "success" ? "completed" : pass.outcome === "error" ? "failed" : null;
    if (!status) continue;
    marks[`${pass.container.kind}:${id}`] = {
      jobId: progress.job_id, executionId: pass.execution_id, status,
      ...(pass.for_each ? { items: { processed: pass.for_each.processed_count, selected: pass.for_each.selected_count } } : {}),
      ...(pass.lanes ? { lanes: { completed: pass.lanes.completed_count, total: pass.lanes.total_count } } : {}),
    };
  }
  return marks;
}
