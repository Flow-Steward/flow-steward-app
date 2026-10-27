import type { WorkflowGraphEdge, WorkflowNodeRef, WorkflowTargetRef } from "@/pages/workflows/shared/api";
import type { JobDefinitionPayload, JobExecutionStepsPage } from "./api";

export type JobTraceFrame = { kind: "group" | "for_each"; id: string; execution_id: string; item_index?: number | null };
export type JobTraceTransition = {
  id: string; sequence: number; source: WorkflowNodeRef; outcome: WorkflowGraphEdge["outcome"]; target: WorkflowTargetRef;
  step_execution_id?: string | null; attempt?: number | null; container_execution_id?: string | null;
  lane_id?: string | null; frames: JobTraceFrame[]; approval_id?: string | null;
};
export type JobWorkflowTrace = {
  schema_version: 1; job_id: string; workflow_id: string; snapshot_id: string; revision: number; digest: string;
  covered_from_start: boolean; terminal: boolean; truncated: boolean; last_sequence: number; transitions: JobTraceTransition[];
};
/** Exact accepted facts and their matching archived authored edges, not inferred history. */
export type JobExecutionTraceEvidence = { trace: JobWorkflowTrace; edges: WorkflowGraphEdge[] };
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const identifier = (value: unknown): value is string => typeof value === "string" && value.length > 0;
const keys = (value: Record<string, unknown>, allowed: string[]) => Object.keys(value).every((key) => allowed.includes(key));
const optionalId = (value: unknown) => value == null || identifier(value);
const integer = (value: unknown, minimum: number): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
const sameNode = (a: WorkflowNodeRef, b: WorkflowNodeRef) => a.kind === b.kind && a.id === b.id;
export function sameTraceTarget(a: WorkflowTargetRef, b: WorkflowTargetRef): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "group_exit" && b.kind === "group_exit") return a.group_id === b.group_id && a.lane_id === b.lane_id;
  if (a.kind === "for_each_exit" && b.kind === "for_each_exit") return a.for_each_id === b.for_each_id && a.lane_id === b.lane_id;
  return "id" in a && "id" in b && a.id === b.id;
}
/** A tuple avoids delimiter collisions in authored IDs/case IDs and includes the target. */
export function runtimeEdgeKey(edge: WorkflowGraphEdge): string {
  const target = edge.target;
  return JSON.stringify([edge.source.kind, edge.source.id, edge.outcome.name, edge.outcome.case_id ?? null,
    target.kind, target.kind === "group_exit" ? target.group_id : target.kind === "for_each_exit" ? target.for_each_id : target.id,
    "lane_id" in target ? target.lane_id : null]);
}

export function sameTraceEdge(a: WorkflowGraphEdge, b: WorkflowGraphEdge): boolean {
  return sameNode(a.source, b.source) && a.outcome.name === b.outcome.name && a.outcome.case_id === b.outcome.case_id && sameTraceTarget(a.target, b.target);
}
function node(value: unknown): value is WorkflowNodeRef {
  const ref = object(value);
  return ["step", "group", "for_each"].includes(String(ref.kind)) && identifier(ref.id) && keys(ref, ["kind", "id"]);
}
function target(value: unknown): value is WorkflowTargetRef {
  const ref = object(value);
  return node(value) || (ref.kind === "group_exit" && identifier(ref.group_id) || ref.kind === "for_each_exit" && identifier(ref.for_each_id))
    && (ref.lane_id === null || identifier(ref.lane_id)) && keys(ref, ["kind", ref.kind === "group_exit" ? "group_id" : "for_each_id", "lane_id"]);
}
function edge(value: unknown): value is WorkflowGraphEdge {
  const item = object(value), outcome = object(item.outcome);
  return node(item.source) && target(item.target) && ["success", "true", "false", "case", "default", "approved", "rejected", "error"].includes(String(outcome.name))
    && Object.keys(outcome).every((key) => key === "name" || key === "case_id")
    && (outcome.name === "case" ? identifier(outcome.case_id) : outcome.case_id === undefined);
}

/** Fail closed without throwing; partial step rows do not determine trace coverage. */
export function jobExecutionTrace(jobId: string, definition: JobDefinitionPayload | undefined, page: JobExecutionStepsPage | undefined): JobExecutionTraceEvidence | undefined {
  const raw = object(page?.run_trace);
  if (!definition?.available || definition.schema_version !== 1 || definition.execution_graph?.schema_version !== 1
    || page?.job_id !== jobId || definition.job_id !== jobId || raw.schema_version !== 1
    || raw.job_id !== jobId || !identifier(jobId) || !identifier(definition.workflow_id) || raw.workflow_id !== definition.workflow_id
    || !identifier(definition.snapshot_id) || raw.snapshot_id !== definition.snapshot_id
    || !identifier(definition.digest) || raw.digest !== definition.digest || !integer(definition.revision, 1) || raw.revision !== definition.revision
    || !keys(raw, ["schema_version", "job_id", "workflow_id", "snapshot_id", "revision", "digest", "covered_from_start", "terminal", "truncated", "last_sequence", "transitions"])
    || raw.covered_from_start !== true || raw.truncated !== false || typeof raw.terminal !== "boolean"
    || !integer(raw.last_sequence, 0) || !Array.isArray(raw.transitions) || raw.transitions.length > 1000
    || !Array.isArray(definition.steps) || !Array.isArray(definition.groups) || !Array.isArray(definition.for_each)
    || !Array.isArray(definition.execution_graph.edges) || !definition.execution_graph.edges.every(edge)) return undefined;
  if (!definition.steps.every((row) => identifier(row.step_id)) || !definition.groups.every((row) => identifier(row.group_id))
    || !definition.for_each.every((row) => identifier(row.for_each_id))) return undefined;
  const steps = new Map(definition.steps.map((row) => [row.step_id, row]));
  const groups = new Map(definition.groups.map((row) => [row.group_id, row]));
  const items = new Map(definition.for_each.map((row) => [row.for_each_id, row]));
  // Duplicate authored identities cannot establish one unambiguous scope.
  if (steps.size !== definition.steps.length || groups.size !== definition.groups.length || items.size !== definition.for_each.length) return undefined;
  const rowFor = (ref: WorkflowNodeRef) => (ref.kind === "step" ? steps : ref.kind === "group" ? groups : items).get(ref.id);
  const parents = (ref: WorkflowNodeRef): WorkflowNodeRef[] | null => {
    const row = rowFor(ref);
    if (!row) return null;
    if (ref.kind === "group") return [];
    if (ref.kind === "step" && identifier(row.for_each_id)) {
      const item = items.get(row.for_each_id);
      if (!item || identifier(row.group_id) && row.group_id !== item.group_id) return null;
      if (identifier(item.group_id) && !groups.has(item.group_id)) return null;
      return [...(identifier(item.group_id) ? [{ kind: "group" as const, id: item.group_id }] : []), { kind: "for_each", id: row.for_each_id }];
    }
    return identifier(row.group_id) ? groups.has(row.group_id) ? [{ kind: "group", id: row.group_id }] : null : [];
  };
  const scope = (ref: WorkflowNodeRef): { parents: WorkflowNodeRef[]; lane: string | null } | null => {
    const chain = parents(ref), row = rowFor(ref);
    if (!chain || !row) return null;
    const lane = identifier(row.lane_id) ? row.lane_id : null;
    const owner = chain.at(-1);
    if (lane) {
      const body = object(owner && rowFor(owner)?.body);
      if (body.kind !== "lanes" || !Array.isArray(body.lanes) || !body.lanes.some((value) => object(value).lane_id === lane)) return null;
    }
    return { parents: chain, lane };
  };
  const edges: WorkflowGraphEdge[] = [], ids = new Set<string>();
  for (const [index, value] of raw.transitions.entries()) {
    const fact = object(value);
    if (!edge(value) || !keys(fact, ["id", "sequence", "source", "outcome", "target", "step_execution_id", "attempt", "container_execution_id", "lane_id", "frames", "approval_id"]) || !identifier(fact.id) || fact.id.length !== 64 || ids.has(fact.id) || fact.sequence !== index + 1
      || !optionalId(fact.step_execution_id) || !optionalId(fact.container_execution_id) || !optionalId(fact.lane_id) || !optionalId(fact.approval_id)
      || !Array.isArray(fact.frames) || fact.frames.length > 2) return undefined;
    const sourceScope = scope(value.source);
    if (!sourceScope) return undefined;
    const expectedFrames = value.source.kind === "step" ? sourceScope.parents : [...sourceScope.parents, value.source];
    if (fact.frames.length !== expectedFrames.length || fact.frames.some((rawFrame, position) => {
      const frame = object(rawFrame), expected = expectedFrames[position];
      return !keys(frame, ["kind", "id", "execution_id", "item_index"]) || frame.kind !== expected.kind || frame.id !== expected.id || !identifier(frame.execution_id)
        || (frame.kind === "for_each" ? frame.item_index != null && !integer(frame.item_index, 0) : frame.item_index != null);
    })) return undefined;
    if (value.source.kind === "step") {
      if (!identifier(fact.step_execution_id) || !integer(fact.attempt, 1) || fact.container_execution_id != null || (fact.lane_id ?? null) !== sourceScope.lane) return undefined;
    } else if (!identifier(fact.container_execution_id) || fact.step_execution_id != null || fact.attempt != null || fact.lane_id != null
      || object(fact.frames.at(-1)).execution_id !== fact.container_execution_id) return undefined;
    if (node(value.target)) {
      const targetScope = scope(value.target);
      if (targetScope?.lane !== sourceScope.lane || targetScope.parents.length !== sourceScope.parents.length
        || targetScope.parents.some((ref, position) => !sameNode(ref, sourceScope.parents[position]))) return undefined;
    } else {
      const owner = sourceScope.parents.at(-1), targetRef = value.target;
      if (!owner || (targetRef.kind === "group_exit" ? owner.kind !== "group" || owner.id !== targetRef.group_id
        : owner.kind !== "for_each" || owner.id !== targetRef.for_each_id) || targetRef.lane_id !== sourceScope.lane) return undefined;
    }
    const matches = definition.execution_graph.edges.filter((candidate) => sameTraceEdge(candidate, value));
    if (matches.length !== 1) return undefined;
    edges.push(matches[0]); ids.add(fact.id);
  }
  if (raw.last_sequence !== raw.transitions.length) return undefined;
  return { trace: raw as unknown as JobWorkflowTrace, edges };
}
