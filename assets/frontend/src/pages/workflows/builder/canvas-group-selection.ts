import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { WorkflowGraphEdge, WorkflowNodeRef, WorkflowTargetRef } from "@/pages/workflows/shared/api";
import { workflowHookPlacementInvalid } from "@/pages/workflows/shared/workflow-hook-placement";
import { nodeRef, outcomeHandleId, sameNodeRef, targetKey, type WorkflowChangeOperation } from "./api";
import { errorPolicyToken, groupBodyEntryStepId, groupLanes, stepId, stepIsRoot, stepKindOf, stepPorts, uniqueLocalId, type WorkflowDefinitionModel } from "./change-set";
import { parseHookNodeId } from "./hook-frames";

type Translate = I18nContextValue["t"];

/** What Group… wraps the selected steps into: a plain Group (one after another) or Parallel lanes. */
export type SelectionGroupKind = "sequence" | "lanes";

export type SelectionGroupSummary = {
  /** The steps that move in, by name. */
  movesIn: string[];
  /** Each connection into the steps from outside: it now leads to the group. */
  leadsIn: string[];
  /** Where the group goes on after its body: the most used target outside. */
  thenGoesTo: string;
  /** Each connection out of the steps: it now ends the body (and what it led to before, when that is not where the group goes). */
  ends: Array<{ label: string; formerTarget?: string }>;
  /** Connections the move cannot keep: named in attention ink. */
  dropped: string[];
  /** Lanes for Parallel lanes: one per connected chain, by its first step. */
  lanes: Array<{ laneId: string; steps: string[] }>;
};

export type SelectionGroupPlan =
  | { ok: true; containerId: string; nodeKind: "group"; operations: WorkflowChangeOperation[]; summary: SelectionGroupSummary }
  | { ok: false; reason: string };

const FOR_EACH_PREFIX = "for_each:";

/** The key a selectable node goes by: a step's id, a For each's canvas id (`for_each:<id>`). */
function nodeKey(ref: WorkflowTargetRef): string | null {
  if (ref.kind === "step") return ref.id;
  if (ref.kind === "for_each") return `${FOR_EACH_PREFIX}${ref.id}`;
  return null;
}

function refOfKey(key: string): WorkflowNodeRef {
  return key.startsWith(FOR_EACH_PREFIX) ? nodeRef("for_each", key.slice(FOR_EACH_PREFIX.length)) : nodeRef("step", key);
}

/**
 * The selection as the nodes that move: a For each takes its own body along, so
 * a step of that body picked beside it adds nothing.
 */
function movingKeys(definition: WorkflowDefinitionModel, canvasIds: readonly string[]): string[] {
  const loops = new Set(canvasIds.filter((id) => id.startsWith(FOR_EACH_PREFIX)).map((id) => id.slice(FOR_EACH_PREFIX.length)));
  return [...new Set(canvasIds)].filter((id) => {
    const step = id.startsWith(FOR_EACH_PREFIX) ? undefined : definition.steps.find((item) => stepId(item) === id);
    return !(step && loops.has(String(step.for_each_id || "")));
  });
}

/** The canvas ids a Group… can wrap (root steps and root For each); anything else refuses with the reason. */
export function selectionGroupRefusal(definition: WorkflowDefinitionModel, canvasIds: readonly string[], t: Translate = fallbackContext.t): string {
  const nest = t("react.workflows.group_selection_no_nesting", "Groups don't nest: select steps, or a For each that is not in a group.");
  if (!canvasIds.length) return t("react.workflows.group_selection_empty", "Select the steps to group first.");
  for (const id of movingKeys(definition, canvasIds)) {
    if (id.startsWith("group:")) return nest;
    if (parseHookNodeId(id)) return t("react.workflows.group_selection_hook", "Steps inside a hook stay in their hook.");
    if (id.startsWith(FOR_EACH_PREFIX)) {
      const loop = definition.forEach.find((item) => String(item.for_each_id) === id.slice(FOR_EACH_PREFIX.length));
      if (!loop) return t("react.workflows.group_selection_unavailable", "A selected step is no longer in the workflow.");
      if (loop.group_id) return nest;
      continue;
    }
    const step = definition.steps.find((item) => stepId(item) === id);
    if (!step) return t("react.workflows.group_selection_unavailable", "A selected step is no longer in the workflow.");
    if (!stepIsRoot(step)) return nest;
    if (workflowHookPlacementInvalid(stepKindOf(step), step.override, { groupId: "x" })) return t("react.workflows.hook_root_only", "Hooks belong in the workflow root, before or after a Group or For each.");
  }
  return "";
}

/** A For each stays in a Group whose steps run one after another, so Parallel lanes cannot take one. */
export function selectionLanesRefusal(canvasIds: readonly string[], t: Translate = fallbackContext.t): string {
  return canvasIds.some((id) => id.startsWith(FOR_EACH_PREFIX))
    ? t("react.workflows.group_lanes_refused_for_each", "A For each stays in a group whose steps run one after another. Move it out first.")
    : "";
}

function mostUsed<T>(values: readonly T[], key: (value: T) => string): T | undefined {
  const counts = new Map<string, { value: T; count: number; last: number }>();
  values.forEach((value, index) => {
    const id = key(value);
    const current = counts.get(id);
    counts.set(id, { value, count: (current?.count ?? 0) + 1, last: index });
  });
  // A tie goes to the target reached from the furthest step (latest in walk order).
  return [...counts.values()].sort((left, right) => right.count - left.count || right.last - left.last)[0]?.value;
}

/** The selected steps in walk order: entries first, then along their connections. */
function walkOrder(ids: readonly string[], internal: readonly WorkflowGraphEdge[]): string[] {
  const inside = new Set(ids);
  const indegree = new Map(ids.map((id) => [id, 0]));
  internal.forEach((edge) => { const target = nodeKey(edge.target); if (!edge.rework && target) indegree.set(target, (indegree.get(target) ?? 0) + 1); });
  const queue = ids.filter((id) => !indegree.get(id));
  const order: string[] = [];
  const seen = new Set<string>();
  while (queue.length) {
    const id = queue.shift()!;
    if (seen.has(id)) continue;
    seen.add(id); order.push(id);
    internal.filter((edge) => !edge.rework && nodeKey(edge.source) === id && inside.has(nodeKey(edge.target) ?? "")).forEach((edge) => {
      const target = nodeKey(edge.target)!;
      indegree.set(target, (indegree.get(target) ?? 1) - 1);
      if (!indegree.get(target)) queue.push(target);
    });
  }
  return [...order, ...ids.filter((id) => !seen.has(id))];
}

/** Connected chains of the selection (lanes), each in walk order. */
function chains(ids: readonly string[], internal: readonly WorkflowGraphEdge[]): string[][] {
  const parent = new Map(ids.map((id) => [id, id]));
  const find = (id: string): string => {
    const up = parent.get(id)!;
    if (up === id) return id;
    const root = find(up);
    parent.set(id, root);
    return root;
  };
  internal.forEach((edge) => { const source = nodeKey(edge.source); const target = nodeKey(edge.target); if (source && target) parent.set(find(source), find(target)); });
  const order = walkOrder(ids, internal);
  const groups = new Map<string, string[]>();
  order.forEach((id) => { const root = find(id); groups.set(root, [...(groups.get(root) ?? []), id]); });
  return [...groups.values()];
}

/** Lane ids in the order chains are numbered: lane_a … lane_z, then lane_27 …. */
export function laneIdsFor(count: number): string[] {
  return Array.from({ length: count }, (_, index) => index < 26 ? `lane_${String.fromCodePoint(97 + index)}` : `lane_${index + 1}`);
}

/**
 * Wrap the selected root steps into a new Group or Parallel lanes group, as one
 * change set intent: the connections between the steps stay; one into them leads
 * to the group; one out of them ends its body, and the group goes on to the most
 * used target outside (a tie goes to the furthest step). An On error route out
 * of the steps cannot cross into the root and is named as dropped.
 */
export function planSelectionGroup(
  definition: WorkflowDefinitionModel,
  canvasIds: readonly string[],
  kind: SelectionGroupKind,
  t: Translate = fallbackContext.t,
): SelectionGroupPlan {
  const refusal = selectionGroupRefusal(definition, canvasIds, t) || (kind === "lanes" ? selectionLanesRefusal(canvasIds, t) : "");
  if (refusal) return { ok: false, reason: refusal };
  const ids = movingKeys(definition, canvasIds);
  const selected = new Set(ids);
  const stepById = new Map(definition.steps.map((step) => [stepId(step), step]));
  const name = (ref: WorkflowTargetRef): string => {
    if (ref.kind === "step") return String(stepById.get(ref.id)?.display_name || ref.id);
    if (ref.kind === "group") return String(definition.groups.find((group) => group.group_id === ref.id)?.display_name || ref.id);
    if (ref.kind === "for_each") return String(definition.forEach.find((item) => item.for_each_id === ref.id)?.display_name || ref.id);
    return ref.kind;
  };
  const outcomeLabel = (edge: WorkflowGraphEdge) => {
    const step = edge.source.kind === "step" ? stepById.get(edge.source.id) : undefined;
    return (step ? stepPorts(step, t).find((port) => outcomeHandleId(port.outcome) === outcomeHandleId(edge.outcome))?.label : "") || outcomeHandleId(edge.outcome);
  };
  const isSelectedStep = (ref: WorkflowTargetRef) => selected.has(nodeKey(ref) ?? "");
  const edges = definition.graph.edges;
  const internal = edges.filter((edge) => isSelectedStep(edge.source) && isSelectedStep(edge.target));
  const incoming = edges.filter((edge) => !isSelectedStep(edge.source) && isSelectedStep(edge.target));
  const outgoing = edges.filter((edge) => isSelectedStep(edge.source) && !isSelectedStep(edge.target));
  const leaving = outgoing.filter((edge) => edge.outcome.name !== "error");
  const errorRoutes = outgoing.filter((edge) => edge.outcome.name === "error");
  const order = walkOrder(ids, internal);
  const lanes = kind === "lanes" ? chains(ids, internal) : [order];
  const laneIds = laneIdsFor(lanes.length);
  const laneOf = new Map<string, string>();
  if (kind === "lanes") lanes.forEach((members, index) => members.forEach((id) => laneOf.set(id, laneIds[index])));
  const entryOf = (members: readonly string[]) => {
    const into = mostUsed(incoming.filter((edge) => members.includes(nodeKey(edge.target) ?? "")), (edge) => targetKey(edge.target));
    if (into && nodeKey(into.target)) return nodeKey(into.target)!;
    const rootEntry = definition.graph.entry;
    const rootKey = rootEntry ? nodeKey(rootEntry) : null;
    if (rootKey && members.includes(rootKey)) return rootKey;
    return members.find((id) => !internal.some((edge) => !edge.rework && nodeKey(edge.target) === id)) ?? members[0];
  };
  const displayName = kind === "lanes" ? t("react.workflows.group_kind_parallel", "Parallel lanes") : t("react.workflows.group", "Group");
  const taken = [...definition.steps.map(stepId), ...definition.groups.map((group) => String(group.group_id)), ...definition.forEach.map((item) => String(item.for_each_id))];
  const containerId = uniqueLocalId(displayName, taken, "group");
  const containerRef = nodeRef("group", containerId);
  const operations: WorkflowChangeOperation[] = [{
    op: "create_group",
    group: { group_id: containerId, display_name: displayName, body: kind === "lanes" ? { kind: "lanes", lanes: laneIds.map((laneId) => ({ lane_id: laneId })) } : { kind: "sequence" } },
  }];
  // An On error route cannot cross into the root: its step falls back to failing.
  const droppedRoutes = new Set<string>();
  errorRoutes.forEach((edge) => {
    const step = edge.source.kind === "step" ? stepById.get(edge.source.id) : undefined;
    const loop = edge.source.kind === "for_each" ? definition.forEach.find((item) => String(item.for_each_id) === edge.source.id) : undefined;
    if (step && errorPolicyToken(step.error_policy) === "route_to_step") {
      operations.push({ op: "update_step_config", step_id: edge.source.id, patch: { error_policy: { ...step.error_policy, on_error: "fail" } } });
    } else if (loop && errorPolicyToken(loop.error_policy) === "route_to_step") {
      operations.push({ op: "update_for_each_config", for_each_id: edge.source.id, patch: { error_policy: { ...loop.error_policy, on_error: "fail" } } });
    }
    droppedRoutes.add(nodeKey(edge.source) ?? "");
  });
  // A For each takes its body along; a move drops the edges that touched the node, the connects below restore them.
  ids.forEach((id) => {
    const ref = refOfKey(id);
    operations.push(ref.kind === "for_each"
      ? { op: "move_for_each", for_each_id: ref.id, group_id: containerId }
      : { op: "move_step", step_id: ref.id, group_id: containerId, for_each_id: null, lane_id: laneOf.get(id) || null });
  });
  internal.forEach((edge) => operations.push({ op: "connect", edge: { ...edge } }));
  lanes.forEach((members, index) => {
    const entry = entryOf(members);
    if (!entry) return;
    operations.push({ op: "set_entry", target: refOfKey(entry), group_id: containerId, ...(kind === "lanes" ? { lane_id: laneIds[index] } : {}) });
  });
  const leadsIn: string[] = [];
  const connectedIn = new Set<string>();
  incoming.forEach((edge) => {
    const key = `${edge.source.kind}:${edge.source.id}:${outcomeHandleId(edge.outcome)}`;
    if (connectedIn.has(key)) return;
    connectedIn.add(key);
    operations.push({ op: "connect", edge: { source: edge.source, outcome: edge.outcome, target: containerRef, rework: Boolean(edge.rework) } });
    leadsIn.push(`${name(edge.source)} · ${outcomeLabel(edge)}`);
  });
  const next = mostUsed(leaving.map((edge) => ({ edge, at: order.indexOf(nodeKey(edge.source) ?? "") })).sort((left, right) => left.at - right.at).map(({ edge }) => edge), (edge) => targetKey(edge.target));
  const ends = leaving.map((edge) => {
    const exit: WorkflowTargetRef = { kind: "group_exit", group_id: containerId, lane_id: laneOf.get(nodeKey(edge.source) ?? "") || null };
    operations.push({ op: "connect", edge: { source: edge.source, outcome: edge.outcome, target: exit, rework: false } });
    const formerTarget = next && targetKey(next.target) === targetKey(edge.target) ? undefined : name(edge.target);
    return { label: `${name(edge.source)} · ${outcomeLabel(edge)}`, ...(formerTarget ? { formerTarget } : {}) };
  });
  if (next && (next.target.kind === "step" || next.target.kind === "group" || next.target.kind === "for_each")) {
    operations.push({ op: "connect", edge: { source: containerRef, outcome: { name: "success" }, target: next.target, rework: false } });
  }
  const rootEntry = definition.graph.entry;
  if (!rootEntry || selected.has(nodeKey(rootEntry) ?? "")) operations.push({ op: "set_entry", target: containerRef });
  return {
    ok: true,
    containerId,
    nodeKind: "group",
    operations,
    summary: {
      movesIn: order.map((id) => name(refOfKey(id))),
      leadsIn,
      thenGoesTo: next ? name(next.target) : "",
      ends,
      dropped: errorRoutes.filter((edge) => droppedRoutes.has(nodeKey(edge.source) ?? "")).map((edge) => `${name(edge.source)} · ${outcomeLabel(edge)} → ${name(edge.target)}`),
      lanes: kind === "lanes" ? lanes.map((members, index) => ({ laneId: laneIds[index], steps: members.map((id) => name(refOfKey(id))) })) : [],
    },
  };
}

/** True when the plan keeps an edge that leads from one selected step to another. */
export function planKeepsEdge(plan: SelectionGroupPlan, edge: WorkflowGraphEdge): boolean {
  return plan.ok && plan.operations.some((operation) => operation.op === "connect" && sameNodeRef(operation.edge.source, edge.source) && outcomeHandleId(operation.edge.outcome) === outcomeHandleId(edge.outcome) && targetKey(operation.edge.target) === targetKey(edge.target));
}

export type GroupBodyChangePlan =
  | { ok: true; operations: WorkflowChangeOperation[]; lanes: string[] }
  | { ok: false; reason: string };

/** The lanes a Group's steps fall into: one per connected chain (at least one). */
export function groupChainLaneIds(definition: WorkflowDefinitionModel, groupId: string): string[] {
  const ids = definition.steps.filter((step) => step.group_id === groupId).map(stepId);
  const isMember = (ref: WorkflowTargetRef) => ref.kind === "step" && ids.includes(ref.id);
  const internal = definition.graph.edges.filter((edge) => isMember(edge.source) && isMember(edge.target));
  return laneIdsFor(Math.max(1, ids.length ? chains(ids, internal).length : 2));
}

/**
 * Switch a Group between One after another and Parallel lanes, keeping its steps
 * and the connections between them, as one change set intent. To lanes: each
 * connected chain becomes a lane, in the order the draft names them. To one
 * after another: the lanes run in turn, each lane's ends leading to the next
 * lane's entry. A For each stays in a Group whose steps run one after another.
 */
export function planGroupBodyChange(
  definition: WorkflowDefinitionModel,
  groupId: string,
  next: { kind: "sequence" | "lanes"; laneIds?: readonly string[]; concurrencyLimit?: number },
  t: Translate = fallbackContext.t,
): GroupBodyChangePlan {
  const group = definition.groups.find((item) => String(item.group_id) === groupId);
  if (!group) return { ok: false, reason: t("react.workflows.change_unavailable_now", "Wait for the structure to update, then try again.") };
  const ids = definition.steps.filter((step) => step.group_id === groupId).map(stepId);
  const isMember = (ref: WorkflowTargetRef) => ref.kind === "step" && ids.includes(ref.id);
  const isExit = (ref: WorkflowTargetRef) => ref.kind === "group_exit" && ref.group_id === groupId;
  const edges = definition.graph.edges;
  const internal = edges.filter((edge) => isMember(edge.source) && isMember(edge.target));
  const exits = edges.filter((edge) => isMember(edge.source) && isExit(edge.target));
  const operations: WorkflowChangeOperation[] = [];
  if (next.kind === "lanes") {
    if (definition.forEach.some((item) => item.group_id === groupId)) {
      return { ok: false, reason: t("react.workflows.group_lanes_refused_for_each", "A For each stays in a group whose steps run one after another. Move it out first.") };
    }
    const memberChains = ids.length ? chains(ids, internal) : [];
    const laneIds = next.laneIds?.length ? [...next.laneIds] : laneIdsFor(Math.max(1, memberChains.length));
    // A chain beyond the named lanes joins the last lane; the validator says if it is unreachable.
    const laneOf = new Map<string, string>();
    memberChains.forEach((members, index) => members.forEach((id) => laneOf.set(id, laneIds[Math.min(index, laneIds.length - 1)])));
    const body: Record<string, unknown> = { kind: "lanes", lanes: laneIds.map((laneId) => ({ lane_id: laneId })) };
    if (next.concurrencyLimit !== undefined) body.concurrency_limit = next.concurrencyLimit;
    operations.push({ op: "update_group_config", group_id: groupId, patch: { body } });
    ids.forEach((id) => operations.push({ op: "move_step", step_id: id, group_id: groupId, for_each_id: null, lane_id: laneOf.get(id) || null }));
    internal.forEach((edge) => operations.push({ op: "connect", edge: { ...edge } }));
    exits.forEach((edge) => operations.push({ op: "connect", edge: { ...edge, target: { kind: "group_exit", group_id: groupId, lane_id: edge.source.kind === "step" ? laneOf.get(edge.source.id) || null : null } } }));
    const formerEntry = groupBodyEntryStepId(group);
    memberChains.forEach((members, index) => {
      if (index >= laneIds.length) return;
      const entry = members.includes(formerEntry) ? formerEntry : walkOrder(members, internal)[0];
      if (entry) operations.push({ op: "set_entry", target: nodeRef("step", entry), group_id: groupId, lane_id: laneIds[index] });
    });
    return { ok: true, operations, lanes: laneIds };
  }
  // One after another: the lanes in their order, each one's ends leading to the next one's entry.
  const lanes = groupLanes(group)
    .map((lane) => ({ laneId: String(lane.lane_id), entry: groupBodyEntryStepId(group, String(lane.lane_id)) }))
    .filter((lane) => lane.entry && ids.includes(lane.entry));
  operations.push({ op: "update_group_config", group_id: groupId, patch: { body: { kind: "sequence" } } });
  ids.forEach((id) => operations.push({ op: "move_step", step_id: id, group_id: groupId, for_each_id: null, lane_id: null }));
  internal.forEach((edge) => operations.push({ op: "connect", edge: { ...edge } }));
  exits.forEach((edge) => {
    const at = lanes.findIndex((lane) => lane.laneId === (edge.target.kind === "group_exit" ? edge.target.lane_id : ""));
    const following = at >= 0 ? lanes[at + 1] : undefined;
    const target: WorkflowTargetRef = following ? nodeRef("step", following.entry) : { kind: "group_exit", group_id: groupId, lane_id: null };
    operations.push({ op: "connect", edge: { ...edge, target } });
  });
  if (lanes[0]) operations.push({ op: "set_entry", target: nodeRef("step", lanes[0].entry), group_id: groupId });
  return { ok: true, operations, lanes: [] };
}
