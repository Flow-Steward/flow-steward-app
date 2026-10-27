import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { HOOK_END_STEP_ID, parseHookNodeId } from "./hook-frames";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { nodeRef, outcomeHandleId, targetKey } from "./api";
import { groupBodyKind, groupPorts, placementContainer, ROOT_PLACEMENT, samePlacement, stepPlacement, stepPorts, type WorkflowDefinitionModel, type WorkflowPlacement } from "./change-set";
import type { WorkflowCanvasGraph, WorkflowCanvasPort, WorkflowForEach, WorkflowNodeRef, WorkflowOutcome, WorkflowTargetRef } from "../shared/api";
const textOrEmpty = (value: unknown) => value === null || value === undefined ? "" : stringFromUnknownValue(value).trim();

export type TargetOption = { key: string; label: string; target: WorkflowTargetRef };

/** The existing inspector labels, shared with every scoped connection entrypoint. */
export function scopeExitTargetLabel(placement: WorkflowPlacement, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (placement.laneId) return t("react.workflows.end_lane_here", "End the lane here");
  if (placement.forEachId) return t("react.workflows.end_item_here", "End the item here");
  if (isHookRootPlacement(placement)) return t("react.workflows.end_hook_here", "End the hook here");
  return t("react.workflows.end_group_here", "End the group here");
}

/**
 * The targets a node can route to: the nodes of its own scope (never itself),
 * and inside a container that container's (or lane's) exit. An edge never
 * crosses scopes. The node is a step, or a Group / For each node.
 */
export function scopeTargetOptions(
  definition: WorkflowDefinitionModel,
  placement: WorkflowPlacement,
  self: WorkflowNodeRef,
  exitLabel: string,
  t: I18nContextValue["t"] = fallbackContext.t,
): TargetOption[] {
  const options: TargetOption[] = [];
  const add = (target: WorkflowTargetRef, label: string) => {
    if (target.kind === self.kind && "id" in target && target.id === self.id) return;
    options.push({ key: targetKey(target), label, target });
  };
  definition.steps
    .filter((candidate) => samePlacement(stepPlacement(candidate), placement))
    .forEach((candidate) => {
      const id = textOrEmpty(candidate.step_id);
      // A hook's hidden end, in its own workflow's terms, is the end of the hook.
      const hookEnd = id === HOOK_END_STEP_ID && textOrEmpty(candidate.step_kind) === "stop_success";
      add(nodeRef("step", id), hookEnd ? t("react.workflows.end_hook_here", "End the hook here") : textOrEmpty(candidate.display_name) || id);
    });
  // A For each node sits at the root or in a sequence Group body; never in a lane or another For each.
  if (!placement.forEachId && !placement.laneId) {
    definition.forEach
      .filter((item) => samePlacement(connectionPlacement(definition, nodeRef("for_each", textOrEmpty(item.for_each_id))) || ROOT_PLACEMENT, placement))
      .forEach((item) => {
        const id = textOrEmpty(item.for_each_id);
        add(nodeRef("for_each", id), textOrEmpty(item.display_name) || id);
      });
  }
  if (placement.forEachId) {
    add({ kind: "for_each_exit", for_each_id: placement.forEachId, lane_id: placement.laneId || null }, exitLabel);
  } else if (placement.groupId && !isHookRootPlacement(placement)) {
    add({ kind: "group_exit", group_id: placement.groupId, lane_id: placement.laneId || null }, exitLabel);
  } else {
    if (isHookRootPlacement(placement)) add({ kind: "group_exit", group_id: placement.groupId, lane_id: null }, exitLabel);
    definition.groups.filter((group) => samePlacement(connectionPlacement(definition, nodeRef("group", textOrEmpty(group.group_id))) || ROOT_PLACEMENT, placement)).forEach((group) => {
      const id = textOrEmpty(group.group_id);
      add(nodeRef("group", id), textOrEmpty(group.display_name) || id);
    });
  }
  return options;
}


function isHookRootPlacement(placement: WorkflowPlacement) { return Boolean(placement.groupId && !placement.forEachId && !placement.laneId && parseHookNodeId(placement.groupId)?.memberStepId === ""); }

/** One placement lookup for author definitions and their canvas projection. */
export function connectionPlacement(definition: WorkflowDefinitionModel, ref: WorkflowNodeRef): WorkflowPlacement | null {
  if (ref.kind === "step") { const step = definition.steps.find((item) => item.step_id === ref.id); return step ? stepPlacement(step) : null; }
  if (ref.kind === "for_each") {
    const item = definition.forEach.find((candidate) => candidate.for_each_id === ref.id);
    return item ? { ...ROOT_PLACEMENT, groupId: textOrEmpty(item.group_id), ...((item as WorkflowForEach & { parent?: WorkflowNodeRef }).parent ? parentPlacement((item as WorkflowForEach & { parent?: WorkflowNodeRef }).parent!) : {}) } : null;
  }
  const group = definition.groups.find((item) => item.group_id === ref.id);
  return group ? { ...ROOT_PLACEMENT, ...((group as { parent?: WorkflowNodeRef }).parent ? parentPlacement((group as { parent?: WorkflowNodeRef }).parent!) : {}) } : null;
}
function parentPlacement(parent: WorkflowNodeRef): WorkflowPlacement { return { ...ROOT_PLACEMENT, ...(parent.kind === "for_each" ? { forEachId: parent.id } : { groupId: parent.id }) }; }

export type ConnectionRefusal = { code: "source" | "outcome" | "self" | "scope" | "target" | "foreign-exit" | "nested" | "rework-outcome" | "rework-exit" | "rework-lane"; label: string };
export function connectionRefusal(definition: WorkflowDefinitionModel, source: WorkflowNodeRef, outcome: WorkflowOutcome, target: WorkflowTargetRef, rework = false, t: I18nContextValue["t"] = fallbackContext.t): ConnectionRefusal | null {
  const refuse = (code: ConnectionRefusal["code"], label: string): ConnectionRefusal => ({ code, label });
  const placement = connectionPlacement(definition, source);
  if (!placement) return refuse("source", t("react.workflows.connect_from_a_named_outcome", "Drag from one of the step's named outcomes to the next step."));
  const sourceNode = source.kind === "step" ? definition.steps.find((step) => step.step_id === source.id) : source.kind === "group" ? definition.groups.find((item) => item.group_id === source.id) : definition.forEach.find((item) => item.for_each_id === source.id);
  const declared = (sourceNode as { ports?: WorkflowCanvasPort[] })?.ports;
  const ports = declared || (source.kind === "step" ? stepPorts(definition.steps.find((step) => step.step_id === source.id)!, t) : groupPorts(sourceNode || {}, t));
  if (!ports.some((port) => outcomeHandleId(port.outcome) === outcomeHandleId(outcome))) return refuse("outcome", t("react.workflows.connect_from_a_named_outcome", "Drag from one of the step's named outcomes to the next step."));
  const exit = target.kind === "group_exit" || target.kind === "for_each_exit";
  if (!exit && source.kind === target.kind && source.id === target.id) return refuse("self", t("react.workflows.connect_to_itself_refused", "A step cannot lead to itself."));
  if (exit) {
    // A hook frame's exit is the end of the hook (its hidden end step), reached like a Group's exit.
    const sameOwner = target.kind === "for_each_exit" ? placement.forEachId === target.for_each_id && !placement.groupId : placement.groupId === target.group_id && !placement.forEachId;
    if (!sameOwner || (target.lane_id || "") !== placement.laneId) return refuse("foreign-exit", t("react.workflows.group_exit_only_from_inside", "Only a step inside this group can finish at its exit."));
  } else {
    const nextPlacement = connectionPlacement(definition, target);
    if (!nextPlacement) return refuse("target", t("react.workflows.connection_target_missing", "This connection's target is no longer available."));
    if (!samePlacement(placement, nextPlacement)) return refuse("scope", t("react.workflows.connect_across_scopes_refused", "A connection stays inside one group or lane, or in the root. Move the step first."));
    const owner = placementContainer(definition, placement);
    if ((target.kind === "group" && (placement.groupId || placement.forEachId) && !isHookRootPlacement(placement)) || (target.kind === "for_each" && (placement.forEachId || groupBodyKind(owner) === "lanes"))) return refuse("nested", t("react.workflows.connection_nested_refused", "A For each body or parallel lane holds no Group or For each. A Group is entered from the root."));
  }
  if (rework) {
    if (!["true", "false", "case", "default", "rejected", "error"].includes(outcome.name)) return refuse("rework-outcome", t("react.workflows.rework_outcome_refused", "A rework path leaves from True, False, a case, Default, Rejected or On error, outside parallel lanes."));
    if (exit) return refuse("rework-exit", t("react.workflows.rework_exit_refused", "A rework path returns to a step, not an exit."));
    if (placement.laneId || groupBodyKind(placementContainer(definition, placement)) === "lanes") return refuse("rework-lane", t("react.workflows.rework_lane_refused", "A parallel lane has no rework."));
  }
  return null;
}

/** Preserve projected hook IDs and parent ownership; this is a view adapter. */
export function canvasConnectionDefinition(graph: WorkflowCanvasGraph): WorkflowDefinitionModel {
  return {
    steps: graph.steps.map((step) => ({ ...step, step_id: step.id, step_kind: step.kind, ports: ["stop_success", "stop_fail", "break", "continue"].includes(step.kind) ? [] : (step.ports || []).some((port) => port.outcome.name === "error") ? step.ports : [...(step.ports || []), { outcome: { name: "error" as const }, label: "On error" }] })),
    groups: graph.groups.map((group) => ({ ...group, group_id: group.id })),
    forEach: (graph.for_each || []).map((item) => ({ ...item, for_each_id: item.id })),
    graph: { schema_version: 1, entry: graph.entry || null, edges: graph.edges },
  };
}
