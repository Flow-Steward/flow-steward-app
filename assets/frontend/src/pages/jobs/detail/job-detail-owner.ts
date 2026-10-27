import type { JobDetail } from "./api";
import { safeEntityId } from "./job-detail-core";
import { buildCanonicalJobDetailView, projectIdFromPayload } from "./job-detail-view-model";

export type JobDetailOwnerScope = Readonly<{ workflowId: string; projectId: string }>;

function record(value: unknown): Record<string, unknown> { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }

/** Exact selected-run identity verification shared by compact and embedded observers.
 * Optional aliases may be absent; every populated recognized alias must agree.
 */
export function verifiesJobDetailOwner(detail: JobDetail | undefined, jobId: string, owner: JobDetailOwnerScope): detail is JobDetail {
  if (!detail || !jobId) return false;
  const data = record(detail);
  const job = record(data.job);
  const canonical = buildCanonicalJobDetailView({ detail, detailEntity: data, runtimeEntity: record(data.runtime) });
  if (canonical.jobId !== jobId || canonical.selectedWorkflowId !== owner.workflowId || projectIdFromPayload(detail) !== owner.projectId) return false;
  // Validate only aliases recognized by the real canonical helpers. In particular,
  // top-level project may be a scalar identity OR an object with id/project_id;
  // job.project is recognized only as a scalar. No recursive metadata scan.
  const project = record(data.project);
  const runtime = record(data.runtime);
  const aliasesMatch = (aliases: readonly unknown[], expected: string) => aliases.every((value) => {
    const identity = safeEntityId(value);
    return identity === null || identity === expected; // optional absent/empty alias
  });
  return aliasesMatch([data.id, job.id], jobId) && aliasesMatch([
    data.current_project_id, data.project_id, data.projectId, data.project,
    job.project_id, job.projectId, job.project, project.id, project.project_id,
  ], owner.projectId) && aliasesMatch([
    data.selected_workflow, job.selected_workflow, runtime.selected_workflow,
  ], owner.workflowId);
}
