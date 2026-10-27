import { useQueries } from "@tanstack/react-query";
import { useMemo } from "react";

import { queryKeys } from "@/lib/query/keys";

import type {
  WorkflowCanvasForEach,
  WorkflowCanvasGraph,
  WorkflowCanvasGroup,
  WorkflowCanvasHookFrame,
  WorkflowCanvasStep,
  WorkflowGraphEdge,
  WorkflowNodeRef,
  WorkflowTargetRef,
} from "@/pages/workflows/shared/api";
import { fetchWorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import { nodeRef, sameNodeRef, type WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import {
  applyChangeOperations,
  portEdge,
  stepId,
  stepInsertableBetween,
  stepIsRoot,
  stepKindOf,
  type WorkflowDefinitionModel,
  type WorkflowPlacement,
} from "@/pages/workflows/builder/change-set";
import { fetchWorkflowWhereUsed } from "@/pages/workflows/shared/hook-api";
import { HOOK_END_STEP_ID, useWorkflowHookStates, type WorkflowHookState } from "@/pages/workflows/shared/workflow-hooks";

/**
 * A hook step drawn as a Group (B14).
 *
 * The builder replaces each hook step with a synthetic Group whose
 * members are the connected client workflow's steps, so the hook is drawn by
 * the ordinary Group container: header, frame, body layout, "Add step",
 * connections. Everything about the substitution lives here — the ids, the
 * graph rewrite and the way back to the author's references — so the frame can
 * preserve the connected workflow's ownership while editing in place.
 *
 * Ids: the frame is the Group `hook:<hook_id>` (canvas node `group:hook:<hook_id>`),
 * a member is the step `hook:<hook_id>:<step_id>`.
 */
export const HOOK_ID_PREFIX = "hook:";

/**
 * The hidden end of a hook's storage workflow (R2-11) is never drawn: a step
 * leading to it is drawn as leading to the end of the hook frame, and the end
 * of the hook stands for it.
 */
export { HOOK_END_STEP_ID };

export type HookFrame = WorkflowCanvasHookFrame & {
  description: string;
  bindingRevision: number;
  /** The definition that supplied the projected members, distinct from the binding revision. */
  targetRevision: number | null;
  members: WorkflowCanvasStep[];
  memberGroups: WorkflowCanvasGroup[];
  memberForEach: WorkflowCanvasForEach[];
  memberEdges: WorkflowGraphEdge[];
  memberEntry: WorkflowNodeRef | null;
};

export function hookGroupId(hookId: string): string {
  return `${HOOK_ID_PREFIX}${hookId}`;
}

export function hookMemberId(hookId: string, stepId: string): string {
  return `${HOOK_ID_PREFIX}${hookId}:${stepId}`;
}

/** A frame or member id (canvas node ids carry a `group:` prefix) back to its hook. */
export function parseHookNodeId(id: string): { hookId: string; memberStepId: string } | null {
  const bare = id.replace(/^(group|for_each):/, "");
  if (!bare.startsWith(HOOK_ID_PREFIX)) return null;
  const rest = bare.slice(HOOK_ID_PREFIX.length);
  const split = rest.indexOf(":");
  return split < 0
    ? { hookId: rest, memberStepId: "" }
    : { hookId: rest.slice(0, split), memberStepId: rest.slice(split + 1) };
}

/** Re-identify only graph references, leaving business configuration and lane ids alone. */
function memberRef<T extends WorkflowTargetRef>(hookId: string, ref: T): T {
  if (ref.kind === "group_exit") return { ...ref, group_id: hookMemberId(hookId, ref.group_id) };
  if (ref.kind === "for_each_exit") return { ...ref, for_each_id: hookMemberId(hookId, ref.for_each_id) };
  return { ...ref, id: hookMemberId(hookId, ref.id) };
}

function mapBody(body: Record<string, unknown> | undefined, mapRef: (ref: WorkflowNodeRef) => WorkflowNodeRef): Record<string, unknown> | undefined {
  if (!body) return body;
  const entry = (value: unknown) => {
    if (!value || typeof value !== "object") return value;
    const ref = value as WorkflowNodeRef;
    return ["step", "group", "for_each"].includes(ref.kind) && typeof ref.id === "string" ? mapRef(ref) : value;
  };
  return {
    ...body,
    ...("entry" in body ? { entry: entry(body.entry) } : {}),
    ...(Array.isArray(body.lanes) ? { lanes: body.lanes.map((value) => {
      if (!value || typeof value !== "object") return value;
      const lane = value as Record<string, unknown>;
      return { ...lane, ...("entry" in lane ? { entry: entry(lane.entry) } : {}) };
    }) } : {}),
  };
}

/** Map an ordinary child placement to its visual hook scope, preserving lanes. */
export function canvasPlacement(hookId: string, placement: WorkflowPlacement): WorkflowPlacement {
  return {
    ...placement,
    groupId: placement.groupId ? hookMemberId(hookId, placement.groupId) : placement.forEachId ? "" : hookGroupId(hookId),
    forEachId: placement.forEachId ? hookMemberId(hookId, placement.forEachId) : "",
  };
}

/** Restore persisted child ownership, including the hook frame's visual root. */
export function childPlacement(placement: WorkflowPlacement): WorkflowPlacement {
  const raw = (id: string) => parseHookNodeId(id)?.memberStepId ?? id;
  return { ...placement, groupId: raw(placement.groupId), forEachId: raw(placement.forEachId) };
}

/** The end of a hook frame: a member that finishes the hook leads here. */
export function hookFrameExit(hookId: string): WorkflowTargetRef {
  return { kind: "group_exit", group_id: hookGroupId(hookId), lane_id: null };
}

function isHookEndRef(ref: WorkflowTargetRef): boolean {
  return ref.kind === "step" && ref.id === HOOK_END_STEP_ID;
}

/** The complete connected graph, projected into one visual hook container. */
export function hookFrame(state: WorkflowHookState & { needsProject?: boolean }, childGraph?: WorkflowCanvasGraph | null, usedIn = 0): HookFrame {
  const ref = <T extends WorkflowTargetRef>(value: T) => memberRef(state.hookId, value);
  const frameParent: WorkflowNodeRef = { kind: "group", id: hookGroupId(state.hookId) };
  // The storage workflow's end is the hook's own end: never a card in the frame.
  const hiddenEnd = (childGraph?.steps || []).some((step) => (step.step_id || step.id) === HOOK_END_STEP_ID && step.kind === "stop_success");
  const target = (value: WorkflowTargetRef) => hiddenEnd && isHookEndRef(value) ? hookFrameExit(state.hookId) : ref(value);
  const members = (childGraph?.steps || []).filter((step) => !hiddenEnd || (step.step_id || step.id) !== HOOK_END_STEP_ID).map((step) => ({
    ...step,
    id: hookMemberId(state.hookId, step.id),
    step_id: hookMemberId(state.hookId, step.step_id || step.id),
    group_id: step.group_id ? hookMemberId(state.hookId, step.group_id) : step.for_each_id ? step.group_id : frameParent.id,
    ...(step.for_each_id ? { for_each_id: hookMemberId(state.hookId, step.for_each_id) } : {}),
  }));
  const memberGroups = (childGraph?.groups || []).map((group) => ({
    ...group,
    id: hookMemberId(state.hookId, group.id),
    ...(group.group_id ? { group_id: hookMemberId(state.hookId, group.group_id) } : {}),
    parent: group.parent ? ref(group.parent) : frameParent,
    body: mapBody(group.body, ref),
  }));
  const memberForEach = (childGraph?.for_each || []).map((item) => ({
    ...item,
    id: hookMemberId(state.hookId, item.id),
    ...(item.for_each_id ? { for_each_id: hookMemberId(state.hookId, item.for_each_id) } : {}),
    ...(item.group_id ? { group_id: hookMemberId(state.hookId, item.group_id) } : {}),
    parent: item.parent ? ref(item.parent) : item.group_id ? ref({ kind: "group", id: item.group_id }) : frameParent,
    body: mapBody(item.body, ref),
  }));
  const revision = childGraph?.workflow.revision;
  return {
    hookId: state.hookId, stepId: state.stepId, displayName: state.displayName,
    description: state.description, connected: state.connected,
    targetWorkflowId: state.targetWorkflowId, targetName: state.targetName,
    bindingRevision: state.bindingRevision, usedIn,
    stepCount: members.length,
    ...(state.needsProject ? { needsProject: true } : {}),
    ...(childGraph ? { outputContract: {
      outputs: childGraph.output_contract.output_mode === "none" ? [] : (childGraph.output_contract.expected_outputs || []).map((field) => field.name),
      artifacts: (childGraph.output_contract.returned_files || []).map((field) => typeof field.name === "string" ? field.name : "").filter(Boolean),
    } } : {}),
    targetRevision: typeof revision === "number" && Number.isInteger(revision) && revision > 0 ? revision : null,
    members, memberGroups, memberForEach,
    memberEdges: (childGraph?.edges || []).map((edge) => ({ ...edge, source: ref(edge.source), target: target(edge.target) })),
    memberEntry: childGraph?.entry && !(hiddenEnd && isHookEndRef(childGraph.entry)) ? ref(childGraph.entry) : null,
  };
}

function frameRef(ref: WorkflowNodeRef, framesByStep: ReadonlyMap<string, HookFrame>): WorkflowNodeRef {
  const frame = ref.kind === "step" ? framesByStep.get(ref.id) : undefined;
  return frame ? { kind: "group", id: hookGroupId(frame.hookId) } : ref;
}

function frameTarget(ref: WorkflowTargetRef, framesByStep: ReadonlyMap<string, HookFrame>): WorkflowTargetRef {
  return "kind" in ref && (ref.kind === "step" || ref.kind === "group" || ref.kind === "for_each")
    ? frameRef(ref as WorkflowNodeRef, framesByStep)
    : ref;
}

/**
 * Replace root hook steps with visual Group frames. Invalid nested declarations
 * remain ordinary nodes so their validation errors stay visible. This
 * containment changes rendering only; members retain their child ownership.
 */
export function withHookFrames(graph: WorkflowCanvasGraph, frames: ReadonlyMap<string, HookFrame>): WorkflowCanvasGraph {
  const framesByStep = new Map(
    [...frames.values()]
      .filter((frame) => graph.steps.some((step) => (step.step_id || step.id) === frame.stepId && !step.group_id && !step.for_each_id))
      .map((frame) => [frame.stepId, frame]),
  );
  if (!framesByStep.size) return graph;
  const groups: WorkflowCanvasGroup[] = [...framesByStep.values()].map((frame) => {
    const step = graph.steps.find((candidate) => (candidate.step_id || candidate.id) === frame.stepId);
    const summary: WorkflowCanvasHookFrame = {
      declarationPending: frame.declarationPending,
      hookId: frame.hookId,
      stepId: frame.stepId,
      displayName: frame.displayName,
      connected: frame.connected,
      targetWorkflowId: frame.targetWorkflowId,
      targetName: frame.targetName,
      usedIn: frame.usedIn,
      stepCount: frame.stepCount,
      ...(frame.needsProject ? { needsProject: true } : {}),
      outputContract: frame.outputContract,
    };
    const memberEntry = frame.memberEntry;
    return {
      id: hookGroupId(frame.hookId),
      display_name: frame.displayName,
      body: memberEntry ? { entry: memberEntry } : {},
      error_policy: step?.error_policy,
      automation_summary: step?.automation_summary,
      validation_summary: step?.validation_summary,
      hook: summary,
    };
  });
  return {
    ...graph,
    entry: graph.entry ? frameRef(graph.entry, framesByStep) : graph.entry,
    groups: [
      ...graph.groups.map((group) => ({ ...group, body: mapBody(group.body, (ref) => frameRef(ref, framesByStep)) })),
      ...groups,
      ...[...framesByStep.values()].flatMap((frame) => frame.memberGroups),
    ],
    for_each: [
      ...(graph.for_each || []).map((item) => ({ ...item, body: mapBody(item.body, (ref) => frameRef(ref, framesByStep)) })),
      ...[...framesByStep.values()].flatMap((frame) => frame.memberForEach),
    ],
    steps: [
      ...graph.steps.filter((step) => !framesByStep.has(step.step_id || step.id)),
      ...[...framesByStep.values()].flatMap((frame) => frame.members),
    ],
    edges: [
      ...graph.edges.map((edge) => ({
        ...edge,
        source: frameRef(edge.source, framesByStep),
        target: frameTarget(edge.target, framesByStep),
      })),
      ...[...framesByStep.values()].flatMap((frame) => frame.memberEdges),
    ],
  };
}

/** A canvas reference back in the author's terms: a hook frame is its hook step. */
export function authorRef<T extends WorkflowTargetRef>(ref: T, frames: ReadonlyMap<string, HookFrame>): T {
  if (!("kind" in ref) || ref.kind !== "group") return ref;
  const parsed = parseHookNodeId((ref as WorkflowNodeRef).id);
  if (!parsed || parsed.memberStepId) return ref;
  const frame = [...frames.values()].find((candidate) => candidate.hookId === parsed.hookId);
  return frame ? ({ kind: "step", id: frame.stepId } as T) : ref;
}

/** Whether a node or scope exit belongs to the projected client workflow. */
export function isHookMemberRef(ref: WorkflowTargetRef): boolean {
  const id = ref.kind === "group_exit" ? ref.group_id : ref.kind === "for_each_exit" ? ref.for_each_id : ref.id;
  return Boolean(parseHookNodeId(id)?.memberStepId);
}

/**
 * The frames of a workflow's hooks in a project: each hook's binding, the
 * connected workflow's canvas graph, and how many places use that workflow.
 */
export function useHookFrames({
  enabled = true,
  projectId,
  steps,
  workflowId,
}: Readonly<{ enabled?: boolean; projectId: string; steps: unknown; workflowId: string }>) {
  const { hooks } = useWorkflowHookStates({ enabled, projectId, steps, workflowId });
  // Without a project a hook has no place for its steps yet: Add step says why.
  const needsProject = !projectId;
  const targets = useMemo(
    () => [...new Set(hooks.map((hook) => hook.targetWorkflowId).filter(Boolean))],
    [hooks],
  );
  const graphs = useQueries({
    queries: targets.map((targetId) => ({
      queryKey: queryKeys.workflowCanvasGraph(targetId),
      queryFn: () => fetchWorkflowCanvasGraph(targetId),
      enabled,
      retry: false,
      staleTime: 15_000,
    })),
  });
  const usage = useQueries({
    queries: targets.map((targetId) => ({
      queryKey: ["workflows", "where-used", projectId, targetId],
      queryFn: () => fetchWorkflowWhereUsed(projectId, targetId),
      enabled: enabled && Boolean(projectId),
      retry: false,
      staleTime: 15_000,
    })),
  });
  // Stable strings, so the frames keep their identity until something changes.
  const graphKey = graphs.map((query) => query.dataUpdatedAt).join(",");
  const usageKey = usage.map((query) => query.data?.consumers.length ?? -1).join(",");
  return useMemo(() => {
    const graphByTarget = new Map(targets.map((targetId, index) => [targetId, graphs[index]?.data || null]));
    const usedByTarget = new Map(targets.map((targetId, index) => [targetId, usage[index]?.data?.consumers.length || 0]));
    return new Map(
      hooks.map((hook) => [
        hook.stepId,
        hookFrame({ ...hook, needsProject }, graphByTarget.get(hook.targetWorkflowId), usedByTarget.get(hook.targetWorkflowId) || 0),
      ]),
    );
    // graphs and usage are read through their keys.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hooks, targets, graphKey, usageKey, needsProject]);
}

/** A child draft in canvas ids; the original operation remains unchanged for persistence. */
export function canvasOperation(hookId: string, operation: WorkflowChangeOperation): WorkflowChangeOperation {
  const id = (value: string) => hookMemberId(hookId, value);
  const ref = <T extends WorkflowTargetRef>(value: T) => (isHookEndRef(value) ? hookFrameExit(hookId) : memberRef(hookId, value)) as T;
  const placement = <T extends object>(value: T): T => {
    const owner = value as { group_id?: unknown; for_each_id?: unknown };
    return {
      ...value,
      ...(typeof owner.group_id === "string" && owner.group_id ? { group_id: id(owner.group_id) } : {}),
      ...(typeof owner.for_each_id === "string" && owner.for_each_id ? { for_each_id: id(owner.for_each_id) } : {}),
    };
  };
  const bodyConfig = <T extends Record<string, unknown>>(value: T): T => ({
    ...value,
    ...(value.body && typeof value.body === "object" && !Array.isArray(value.body)
      ? { body: mapBody(value.body as Record<string, unknown>, ref) } : {}),
  });
  const step = (value: Extract<WorkflowChangeOperation, { op: "create_step" }>["step"]) => ({ ...placement(value), step_id: id(value.step_id) });
  switch (operation.op) {
    case "create_step": return { ...operation, step: step(operation.step) };
    case "create_group": return { ...operation, group: { ...bodyConfig(operation.group), group_id: id(operation.group.group_id) } };
    case "create_for_each": return { ...operation, for_each: { ...bodyConfig(placement(operation.for_each)), for_each_id: id(operation.for_each.for_each_id) } };
    case "update_step_config": return { ...operation, step_id: id(operation.step_id) };
    case "change_step_kind": return { ...operation, step_id: id(operation.step_id), step: placement(operation.step) };
    case "update_group_config": return { ...operation, group_id: id(operation.group_id), patch: bodyConfig(operation.patch) };
    case "update_for_each_config": return { ...operation, for_each_id: id(operation.for_each_id), patch: bodyConfig(placement(operation.patch)) };
    case "set_entry": return { ...placement(operation), target: ref(operation.target) };
    case "connect": return { ...operation, edge: { ...operation.edge, source: ref(operation.edge.source), target: ref(operation.edge.target) } };
    case "disconnect": return { ...operation, source: ref(operation.source) };
    case "insert_step_between": return { ...operation, source: ref(operation.source), step: step(operation.step) };
    case "move_step": return { ...placement(operation), step_id: id(operation.step_id) };
    case "move_for_each": return { ...placement(operation), for_each_id: id(operation.for_each_id) };
    case "delete_step": return { ...operation, step_id: id(operation.step_id) };
    case "delete_group": return { ...operation, group_id: id(operation.group_id) };
    case "delete_for_each": return { ...operation, for_each_id: id(operation.for_each_id) };
  }
}

/** A projected node or scope exit in the child workflow's own terms; the end of the hook is its hidden end step. */
export function childRef<T extends WorkflowTargetRef>(ref: T): T {
  if (ref.kind === "group_exit") {
    const parsed = parseHookNodeId(ref.group_id);
    if (parsed && !parsed.memberStepId) return { kind: "step", id: HOOK_END_STEP_ID } as T;
    return parsed?.memberStepId ? { ...ref, group_id: parsed.memberStepId } : ref;
  }
  if (ref.kind === "for_each_exit") {
    const parsed = parseHookNodeId(ref.for_each_id);
    return parsed?.memberStepId ? { ...ref, for_each_id: parsed.memberStepId } : ref;
  }
  const parsed = parseHookNodeId(ref.id);
  return parsed?.memberStepId ? { ...ref, id: parsed.memberStepId } : ref;
}

function hasHookEnd(definition: WorkflowDefinitionModel): boolean {
  return definition.steps.some((step) => stepId(step) === HOOK_END_STEP_ID && stepKindOf(step) === "stop_success");
}

function rootNodes(definition: WorkflowDefinitionModel): WorkflowNodeRef[] {
  return [
    ...definition.groups.map((group) => nodeRef("group", String(group.group_id || ""))),
    ...definition.forEach.filter((item) => !item.group_id).map((item) => nodeRef("for_each", String(item.for_each_id || ""))),
    ...definition.steps.filter((step) => stepIsRoot(step) && stepId(step) !== HOOK_END_STEP_ID).map((step) => nodeRef("step", stepId(step))),
  ];
}

/**
 * What keeps a hook's storage workflow whole after *next* (R2-11), in its own
 * terms. The hook is a place for steps whose hidden end the canvas draws as the
 * end of the frame, so:
 *
 * - the first node added to an empty hook starts it and goes on to its end,
 *   like the first step of an empty Group body;
 * - when its last node is removed (or the hook is cleared), the end starts it
 *   again, so the empty hook stays valid and passes its inputs through.
 *
 * A workflow without the hidden end (an older or shared one) gets nothing.
 */
export function hookEndCompletion(
  base: WorkflowDefinitionModel,
  pending: readonly WorkflowChangeOperation[],
  next: readonly WorkflowChangeOperation[],
): WorkflowChangeOperation[] {
  const before = applyChangeOperations(base, [...pending]).definition;
  const after = applyChangeOperations(before, [...next]).definition;
  const healed = healDeletedHookSteps(before, after, next);
  if (!hasHookEnd(before) || !hasHookEnd(after)) return healed;
  const ends = hookEndOperations(before, after, next);
  const startsAgain = ends.some((operation) => operation.op === "set_entry");
  return [...ends, ...healed.filter((operation) => !(startsAgain && operation.op === "set_entry"))];
}

/**
 * Removing a step from a hook keeps the chain whole: whatever led to it now
 * leads where it went (or to the hook's end), and if it started the hook its
 * successor starts it. Older hooks without the hidden end are healed the same way.
 */
function healDeletedHookSteps(
  before: WorkflowDefinitionModel,
  after: WorkflowDefinitionModel,
  next: readonly WorkflowChangeOperation[],
): WorkflowChangeOperation[] {
  const exists = (ref: WorkflowNodeRef) => ref.kind === "step"
    ? after.steps.some((item) => stepId(item) === ref.id)
    : ref.kind === "group" ? after.groups.some((item) => String(item.group_id || "") === ref.id)
      : ref.kind === "for_each" ? after.forEach.some((item) => String(item.for_each_id || "") === ref.id) : true;
  const end = nodeRef("step", HOOK_END_STEP_ID);
  const operations: WorkflowChangeOperation[] = [];
  for (const operation of next) {
    if (operation.op !== "delete_step") continue;
    const removed = nodeRef("step", operation.step_id);
    const target = portEdge(before, removed, { name: "success" })?.target;
    // Only a node can take over; a group or lane exit stays as the builder leaves it.
    const onward = target && (target.kind === "step" || target.kind === "group" || target.kind === "for_each") ? target as WorkflowNodeRef : null;
    const successor: WorkflowNodeRef | null = onward && exists(onward) ? onward : hasHookEnd(after) ? end : null;
    if (!successor) continue;
    for (const edge of before.graph.edges) {
      if (!sameNodeRef(edge.target, removed) || !exists(edge.source) || portEdge(after, edge.source, edge.outcome)) continue;
      operations.push({ op: "connect", edge: { source: edge.source, outcome: edge.outcome, target: successor } });
    }
    if (sameNodeRef(before.graph.entry, removed) && !sameNodeRef(after.graph.entry, successor) && !(after.graph.entry && exists(after.graph.entry))) {
      operations.push({ op: "set_entry", target: successor });
    }
  }
  return operations;
}

function hookEndOperations(
  before: WorkflowDefinitionModel,
  after: WorkflowDefinitionModel,
  next: readonly WorkflowChangeOperation[],
): WorkflowChangeOperation[] {
  const end = nodeRef("step", HOOK_END_STEP_ID);
  const roots = rootNodes(after);
  if (!roots.length) return sameNodeRef(after.graph.entry, end) ? [] : [{ op: "set_entry", target: end }];
  if (!sameNodeRef(before.graph.entry, end) || !sameNodeRef(after.graph.entry, end)) return [];
  const created = next.flatMap((operation): WorkflowNodeRef[] => {
    if (operation.op === "create_step") return [nodeRef("step", operation.step.step_id)];
    if (operation.op === "create_group") return [nodeRef("group", operation.group.group_id)];
    if (operation.op === "create_for_each") return [nodeRef("for_each", operation.for_each.for_each_id)];
    return [];
  }).filter((ref) => roots.some((root) => sameNodeRef(root, ref)));
  if (created.length !== 1) return [];
  const first = created[0];
  const operations: WorkflowChangeOperation[] = [{ op: "set_entry", target: first }];
  const step = first.kind === "step" ? after.steps.find((item) => stepId(item) === first.id) : undefined;
  const endsHere = first.kind === "step" ? Boolean(step && stepInsertableBetween(step)) : true;
  if (endsHere && !portEdge(after, first, { name: "success" })) {
    operations.push({ op: "connect", edge: { source: first, outcome: { name: "success" }, target: end } });
  }
  return operations;
}

/** The operations that empty a hook's storage workflow: every node but its hidden end, then the end starts it. */
export function clearHookOperations(definition: WorkflowDefinitionModel): WorkflowChangeOperation[] {
  const operations: WorkflowChangeOperation[] = [
    // A hook made before the hidden end existed (Build/Return/done) gets one, so it can be emptied.
    ...(hasHookEnd(definition) ? [] : [{ op: "create_step", step: { step_id: HOOK_END_STEP_ID, step_kind: "stop_success" } } as WorkflowChangeOperation]),
    ...definition.groups.map((group): WorkflowChangeOperation => ({ op: "delete_group", group_id: String(group.group_id || ""), with_contents: true })),
    ...definition.forEach.filter((item) => !item.group_id).map((item): WorkflowChangeOperation => ({ op: "delete_for_each", for_each_id: String(item.for_each_id || ""), with_contents: true })),
    ...definition.steps.filter((step) => stepIsRoot(step) && stepId(step) !== HOOK_END_STEP_ID).map((step): WorkflowChangeOperation => ({ op: "delete_step", step_id: stepId(step) })),
  ];
  if (!sameNodeRef(definition.graph.entry, nodeRef("step", HOOK_END_STEP_ID))) operations.push({ op: "set_entry", target: nodeRef("step", HOOK_END_STEP_ID) });
  return operations;
}
