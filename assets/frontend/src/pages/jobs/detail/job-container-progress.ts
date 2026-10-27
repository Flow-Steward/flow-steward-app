import type { JobDefinitionPayload, JobExecutionStepsPage } from "./api";

export type ContainerNode = { kind: "group" | "for_each"; id: string };
export type ContainerParent = ContainerNode & { execution_id: string; item_index?: number | null };
export type ContainerItems = {
  item_index: number | null; source_count: number; selected_count: number; processed_count: number;
  applied_limit: number; truncated: boolean; broke_at?: number | null;
};
export type ContainerLanes = {
  item_index?: number | null; completed_count: number; total_count: number;
  rows: Array<{ lane_id: string; status: "pending" | "running" | "completed" | "failed" | "escalated" | "retry_operation" }>;
};
export type ContainerFailure = {
  code: string; message: string; boundary: "entry" | "selection" | "exit" | "parallel" | "body";
  step_id?: string | null; step_execution_id?: string | null; attempt?: number | null; lane_id?: string | null; item_index?: number | null;
  handling?: { kind: "route" | "exit_approval"; execution_id: string; transition_id?: string | null; request_id?: string | null } | null;
};
export type ContainerPass = {
  container: ContainerNode; execution_id: string; parent_frames: ContainerParent[];
  started_sequence: number; updated_sequence: number; finished_sequence?: number | null;
  current: boolean; outcome?: "success" | "error" | null;
  for_each?: ContainerItems | null; lanes?: ContainerLanes | null; failure?: ContainerFailure | null;
};
export type JobContainerProgress = {
  schema_version: 1; job_id: string; project_id: string; workflow_id: string; snapshot_id: string; revision: number; digest: string;
  covered_from_start: true; truncated: false; checkpoint_terminal: boolean; last_sequence: number; passes: ContainerPass[];
};
export type JobContainerProgressEvidence = { progress: JobContainerProgress };
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const id = (value: unknown): value is string => typeof value === "string" && value.length > 0;
const integer = (value: unknown, minimum = 0): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
const optionalId = (value: unknown) => value == null || id(value);
const optionalIndex = (value: unknown) => value == null || integer(value);
const keys = (value: Record<string, unknown>, allowed: readonly string[]) => Object.keys(value).every((key) => allowed.includes(key));
function itemsValid(value: Record<string, unknown>) {
  return keys(value, ["item_index", "source_count", "selected_count", "processed_count", "applied_limit", "truncated", "broke_at"])
    && integer(value.source_count) && integer(value.selected_count) && integer(value.processed_count) && integer(value.applied_limit, 1)
    && value.selected_count === Math.min(value.source_count, value.applied_limit) && value.processed_count <= value.selected_count
    && value.truncated === (value.source_count > value.applied_limit) && optionalIndex(value.broke_at)
    && (value.selected_count === 0 ? value.item_index == null : integer(value.item_index) && value.item_index < value.selected_count)
    && (value.broke_at == null || value.broke_at === value.item_index && value.processed_count === Number(value.broke_at) + 1);
}

type ProgressCandidate = Record<string, unknown> & { passes: unknown[]; last_sequence: number; checkpoint_terminal: boolean };
type ArchiveIndex = { groups: Map<string, Record<string, unknown>>; loops: Map<string, Record<string, unknown>>; steps: Map<string, Record<string, unknown>> };
type LifecycleCandidate = Record<string, unknown> & Pick<ContainerPass,
  "container" | "execution_id" | "started_sequence" | "updated_sequence" | "finished_sequence" | "current" | "outcome"> & { parent_frames: unknown[] };
type PassContext = {
  progress: ProgressCandidate; archive: ArchiveIndex; records: Map<string, ContainerPass>; current: Set<string>; previousStart: number;
};

function envelopeValid(jobId: string, projectId: string | undefined, definition: JobDefinitionPayload | undefined,
  page: JobExecutionStepsPage | undefined, raw: Record<string, unknown>): raw is ProgressCandidate {
  if (!definition) return false;
  return id(jobId) && id(projectId) && definition.available === true && definition.schema_version === 1 && definition.execution_graph?.schema_version === 1
    && definition.job_id === jobId && page?.job_id === jobId && raw.schema_version === 1 && raw.job_id === jobId && raw.project_id === projectId
    && id(definition.workflow_id) && raw.workflow_id === definition.workflow_id && id(definition.snapshot_id) && raw.snapshot_id === definition.snapshot_id
    && id(definition.digest) && raw.digest === definition.digest && integer(definition.revision, 1) && raw.revision === definition.revision
    && raw.covered_from_start === true && raw.truncated === false && typeof raw.checkpoint_terminal === "boolean" && integer(raw.last_sequence)
    && keys(raw, ["schema_version", "job_id", "project_id", "workflow_id", "snapshot_id", "revision", "digest", "covered_from_start", "truncated", "checkpoint_terminal", "last_sequence", "passes"])
    && Array.isArray(raw.passes) && raw.passes.length <= 1000;
}

function archiveIndex(definition: JobDefinitionPayload | undefined): ArchiveIndex | undefined {
  if (!definition || !Array.isArray(definition.groups) || !Array.isArray(definition.for_each) || !Array.isArray(definition.steps)) return undefined;
  const groups = new Map<string, Record<string, unknown>>(), loops = new Map<string, Record<string, unknown>>(), steps = new Map<string, Record<string, unknown>>();
  for (const [rows, field, map] of [[definition.groups, "group_id", groups], [definition.for_each, "for_each_id", loops], [definition.steps, "step_id", steps]] as const) {
    for (const value of rows) {
      const row = object(value), key = row[field];
      if (!id(key) || map.has(key)) return undefined;
      map.set(key, row);
    }
  }
  const archive = { groups, loops, steps };
  return [...steps.values()].every((step) => Boolean(stepScope(step, archive))) ? archive : undefined;
}

function lifecycleValid(row: Record<string, unknown>, context: PassContext): row is LifecycleCandidate {
  const node = object(row.container);
  return keys(row, ["container", "execution_id", "parent_frames", "started_sequence", "updated_sequence", "finished_sequence", "current", "outcome", "for_each", "lanes", "failure"])
    && keys(node, ["kind", "id"]) && ["group", "for_each"].includes(String(node.kind)) && id(node.id) && id(row.execution_id)
    && !context.records.has(row.execution_id) && integer(row.started_sequence, 1) && row.started_sequence > context.previousStart
    && integer(row.updated_sequence, row.started_sequence) && row.updated_sequence <= context.progress.last_sequence
    && [null, undefined, "success", "error"].includes(row.outcome as string | null | undefined)
    && (row.outcome == null) === (row.finished_sequence == null)
    && (row.finished_sequence == null || integer(row.finished_sequence, row.started_sequence) && row.finished_sequence <= row.updated_sequence)
    && typeof row.current === "boolean" && (!row.current || row.outcome == null && !context.progress.checkpoint_terminal)
    && Array.isArray(row.parent_frames);
}

function parentsValid(row: LifecycleCandidate, archived: Record<string, unknown>, context: PassContext): boolean {
  const parentId = row.container.kind === "for_each" && id(archived.group_id) ? archived.group_id : null;
  if (row.parent_frames.length !== (parentId ? 1 : 0)) return false;
  for (const value of row.parent_frames) {
    const parent = object(value);
    if (!keys(parent, ["kind", "id", "execution_id", "item_index"]) || parent.kind !== "group" || parent.id !== parentId || !context.archive.groups.has(String(parent.id))
      || !id(parent.execution_id) || parent.execution_id === row.execution_id || parent.item_index != null) return false;
    const retained = context.records.get(parent.execution_id);
    if (retained?.container.kind !== parent.kind || retained.container.id !== parent.id || retained.started_sequence >= row.started_sequence
      || retained.finished_sequence != null && (row.started_sequence > retained.finished_sequence
        || row.finished_sequence != null && row.finished_sequence > retained.finished_sequence) || row.current && !retained.current) return false;
  }
  return true;
}

function selectionValid(row: LifecycleCandidate): boolean {
  if (row.for_each != null && (row.container.kind !== "for_each" || !itemsValid(object(row.for_each)))) return false;
  return row.container.kind !== "for_each" || row.outcome !== "success" || row.for_each != null;
}

function laneRowsValid(rows: unknown[], declared: unknown[]): boolean {
  const seen = new Set<string>();
  for (const [index, value] of rows.entries()) {
    const lane = object(value);
    if (!keys(lane, ["lane_id", "status"]) || !id(lane.lane_id) || seen.has(lane.lane_id) || lane.lane_id !== object(declared[index]).lane_id
      || !["pending", "running", "completed", "failed", "escalated", "retry_operation"].includes(String(lane.status))) return false;
    seen.add(lane.lane_id);
  }
  return true;
}

function lanesValid(row: LifecycleCandidate, archived: Record<string, unknown>): boolean {
  if (row.lanes == null) return true;
  const lanes = object(row.lanes), body = object(archived.body), declared = body.lanes;
  return keys(lanes, ["item_index", "rows", "completed_count", "total_count"]) && body.kind === "lanes" && Array.isArray(declared)
    && Array.isArray(lanes.rows) && integer(lanes.completed_count) && lanes.total_count === lanes.rows.length && lanes.rows.length === declared.length
    && (lanes.item_index ?? null) === (object(row.for_each).item_index ?? null) && laneRowsValid(lanes.rows, declared)
    && lanes.completed_count === lanes.rows.filter((lane) => object(lane).status === "completed").length;
}

function handlingValid(value: unknown, row: LifecycleCandidate): boolean {
  if (value == null) return true;
  const handling = object(value), accepted = [row.execution_id, ...row.parent_frames.map((frame) => object(frame).execution_id)];
  // Backend validates exact accepted handling against committed checkpoints.
  // Its proof can outlive the bounded D061 tail; do not require that tail here.
  return keys(handling, ["kind", "execution_id", "transition_id", "request_id"]) && id(handling.execution_id) && accepted.includes(handling.execution_id)
    && (handling.kind === "route" ? id(handling.transition_id) && handling.transition_id.length === 64 && handling.request_id == null
      : handling.kind === "exit_approval" && handling.execution_id === row.execution_id && id(handling.request_id) && handling.transition_id == null);
}

/** A step has one immediate owner; enclosing Group membership comes through its For each. */
function stepScope(step: Record<string, unknown>, archive: ArchiveIndex): { forEachId: string | null; groupId: string | null } | undefined {
  if (!optionalId(step.group_id) || !optionalId(step.for_each_id) || id(step.group_id) && id(step.for_each_id)) return undefined;
  const forEachId = id(step.for_each_id) ? step.for_each_id : null;
  const loop = forEachId ? archive.loops.get(forEachId) : undefined;
  if (forEachId && !loop || loop && !optionalId(loop.group_id)) return undefined;
  let groupId = id(step.group_id) ? step.group_id : null;
  if (loop) groupId = id(loop.group_id) ? loop.group_id : null;
  if (groupId && !archive.groups.has(groupId)) return undefined;
  return { forEachId, groupId };
}

function failureSourceValid(failure: Record<string, unknown>, row: LifecycleCandidate, archive: ArchiveIndex): boolean {
  if (!id(failure.step_id)) return true;
  const step = archive.steps.get(failure.step_id), scope = step ? stepScope(step, archive) : undefined;
  return Boolean(step && scope && (row.container.kind === "group" ? scope.groupId === row.container.id : scope.forEachId === row.container.id)
    && (failure.lane_id == null || step.lane_id === failure.lane_id));
}

function failureValid(row: LifecycleCandidate, archive: ArchiveIndex): boolean {
  if (row.failure == null) return row.outcome !== "error";
  const failure = object(row.failure);
  return keys(failure, ["code", "message", "boundary", "step_id", "step_execution_id", "attempt", "lane_id", "item_index", "handling"])
    && typeof failure.code === "string" && failure.code.length <= 500 && typeof failure.message === "string" && failure.message.length <= 500
    && ["entry", "selection", "exit", "parallel", "body"].includes(String(failure.boundary)) && optionalId(failure.step_id) && optionalId(failure.step_execution_id)
    && optionalId(failure.lane_id) && optionalIndex(failure.item_index) && (failure.attempt == null || integer(failure.attempt, 1))
    && failureSourceValid(failure, row, archive) && handlingValid(failure.handling, row);
}

function readPass(value: unknown, context: PassContext): ContainerPass | undefined {
  const row = object(value);
  if (!lifecycleValid(row, context)) return undefined;
  const archived = (row.container.kind === "group" ? context.archive.groups : context.archive.loops).get(row.container.id);
  if (!archived || !parentsValid(row, archived, context) || !selectionValid(row) || !lanesValid(row, archived) || !failureValid(row, context.archive)
    || row.current && context.current.has(JSON.stringify([row.container.kind, row.container.id]))) return undefined;
  return row as unknown as ContainerPass;
}

/** Committed public aggregate only; its coverage never depends on operation pages or D061's retained tail. */
export function jobContainerProgress(jobId: string, projectId: string | undefined, definition: JobDefinitionPayload | undefined,
  page: JobExecutionStepsPage | undefined): JobContainerProgressEvidence | undefined {
  const raw = object(page?.container_progress);
  if (!envelopeValid(jobId, projectId, definition, page, raw)) return undefined;
  const archive = archiveIndex(definition);
  if (!archive) return undefined;
  const context: PassContext = { progress: raw, archive, records: new Map(), current: new Set(), previousStart: 0 };
  for (const value of raw.passes) {
    const row = readPass(value, context);
    if (!row) return undefined;
    if (row.current) context.current.add(JSON.stringify([row.container.kind, row.container.id]));
    context.previousStart = row.started_sequence;
    context.records.set(row.execution_id, row);
  }
  if (!raw.passes.length && raw.last_sequence !== 0) return undefined;
  return { progress: raw as unknown as JobContainerProgress };
}
