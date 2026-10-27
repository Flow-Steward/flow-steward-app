import { stepExtensionId } from "@/pages/workflows/shared/workflow-step-extension";
import { connectorToolCategory } from "@/pages/workflows/detail/detail-tool-categories";
import { ifConditionSummary } from "@/pages/workflows/builder/if-condition-summary";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type {
  WorkflowCanvasForEach,
  WorkflowCanvasGraph,
  WorkflowCanvasGroup,
  WorkflowCanvasPort,
  WorkflowCanvasStep,
  WorkflowDetail,
  WorkflowExecutionGraph,
  WorkflowForEach,
  WorkflowGraphEdge,
  WorkflowGroup,
  WorkflowGroupLane,
  WorkflowNodeRef,
  WorkflowOutcome,
  WorkflowStep,
} from "@/pages/workflows/shared/api";
import {
  nodeRef,
  sameNodeRef,
  sameOutcome,
  type WorkflowChangeOperation,
} from "@/pages/workflows/builder/api";

/**
 * The builder's local model of one workflow definition, and the change set
 * applied to it before the author presses Apply.
 *
 * The server is the only authority: `POST /changes` validates the final
 * definition and either stores it as one revision or refuses it whole. This
 * module only previews the same operations so the canvas and the drawers show
 * what Apply would send, and it never invents an edge, an entry or a bypass.
 */
export type WorkflowDefinitionModel = {
  steps: WorkflowStep[];
  groups: WorkflowGroup[];
  forEach: WorkflowForEach[];
  graph: WorkflowExecutionGraph;
};

export const STOP_STEP_KINDS = new Set(["stop_success", "stop_fail"]);
/** Loop controls of a sequence For each body: no outgoing edge, no error port. */
export const LOOP_CONTROL_STEP_KINDS = new Set(["break", "continue"]);
/** Kinds allowed only in a sequence For each body. */
export const FOR_EACH_ONLY_STEP_KINDS = new Set(["break", "continue", "collect"]);
/** Kinds whose ports are their structure: a change of these ports is a structural change. */
export const BRANCHING_STEP_KINDS = new Set(["if", "switch", "approval"]);

function text(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function clone<T>(value: T): T {
  return value === undefined ? value : structuredClone(value);
}

export function emptyExecutionGraph(): WorkflowExecutionGraph {
  return { schema_version: 1, entry: null, edges: [] };
}

export function definitionFromWorkflow(workflow: WorkflowDetail | null | undefined): WorkflowDefinitionModel {
  const settings = record(workflow?.settings);
  const rawGraph = record(settings.execution_graph);
  const edges = Array.isArray(rawGraph.edges)
    ? (rawGraph.edges as WorkflowGraphEdge[]).filter((edge) => Boolean(edge && typeof edge === "object"))
    : [];
  const entry = rawGraph.entry && typeof rawGraph.entry === "object"
    ? (rawGraph.entry as WorkflowNodeRef)
    : null;
  return {
    steps: Array.isArray(workflow?.steps) ? clone(workflow.steps) : [],
    groups: Array.isArray(workflow?.groups) ? clone(workflow.groups) : [],
    forEach: Array.isArray(workflow?.for_each) ? clone(workflow.for_each) : [],
    graph: {
      schema_version: Number(rawGraph.schema_version || 1),
      entry: entry ? clone(entry) : null,
      edges: clone(edges),
    },
  };
}

export function stepId(step: WorkflowStep | null | undefined): string {
  return text(step?.step_id);
}

export function stepGroupId(step: WorkflowStep | null | undefined): string {
  return text(step?.group_id);
}

export function stepLaneId(step: WorkflowStep | null | undefined): string {
  return text(step?.lane_id);
}

export function stepForEachId(step: WorkflowStep | null | undefined): string {
  return text(step?.for_each_id);
}

/** A step at the root has no owner: neither a Group nor a For each. */
export function stepIsRoot(step: WorkflowStep | null | undefined): boolean {
  return !stepGroupId(step) && !stepForEachId(step);
}

/**
 * Where a step lives: the root, a Group body or lane, or a For each body or
 * lane. A step has one immediate owner, so at most one of the two ids is set.
 */
export type WorkflowPlacement = { groupId: string; forEachId: string; laneId: string };

export const ROOT_PLACEMENT: WorkflowPlacement = { groupId: "", forEachId: "", laneId: "" };

export function stepPlacement(step: WorkflowStep | null | undefined): WorkflowPlacement {
  return { groupId: stepGroupId(step), forEachId: stepForEachId(step), laneId: stepLaneId(step) };
}

export function samePlacement(left: WorkflowPlacement, right: WorkflowPlacement): boolean {
  return left.groupId === right.groupId && left.forEachId === right.forEachId && left.laneId === right.laneId;
}

/** The owner fields an operation (`create_step`, `move_step`, `set_entry`) sends for a placement. */
export function placementOwnerFields(placement: Partial<WorkflowPlacement>) {
  return {
    group_id: text(placement.forEachId) ? null : text(placement.groupId) || null,
    for_each_id: text(placement.forEachId) || null,
    lane_id: text(placement.laneId) || null,
  };
}

/**
 * A step move, as the operations Apply sends. The step leaves every entry it
 * was (the server drops them with its edges); a place that has no entry yet
 * starts with the moved step, staged explicitly as its own set_entry.
 */
export function moveStepOperations(
  definition: WorkflowDefinitionModel,
  movedStepId: string,
  placement: Partial<WorkflowPlacement>,
): WorkflowChangeOperation[] {
  const owner = placementOwnerFields(placement);
  const move: WorkflowChangeOperation = { op: "move_step", step_id: movedStepId, ...owner };
  const container = placementContainer(definition, placement);
  const ref = nodeRef("step", movedStepId);
  let placeHasEntry: boolean;
  if (container) {
    placeHasEntry = Boolean(groupBodyEntryStepId(container, text(placement.laneId)));
  } else {
    placeHasEntry = Boolean(definition.graph.entry) && !sameNodeRef(definition.graph.entry, ref);
  }
  if (placeHasEntry) return [move];
  return [move, {
    op: "set_entry",
    target: ref,
    ...(owner.group_id ? { group_id: owner.group_id } : {}),
    ...(owner.for_each_id ? { for_each_id: owner.for_each_id } : {}),
    ...(owner.lane_id ? { lane_id: owner.lane_id } : {}),
  }];
}

/** The Group or For each that owns a placement, or null at the root. */
export function placementContainer(
  definition: WorkflowDefinitionModel,
  placement: Partial<WorkflowPlacement>,
): WorkflowGroup | WorkflowForEach | null {
  const forEachId = text(placement.forEachId);
  if (forEachId) return definition.forEach.find((item) => text(item.for_each_id) === forEachId) || null;
  const groupId = text(placement.groupId);
  return groupId ? definition.groups.find((group) => text(group.group_id) === groupId) || null : null;
}

export function stepKindOf(step: WorkflowStep | null | undefined): string {
  return text(step?.step_kind) || "agent";
}

type ContainerLike = { body?: unknown } | null | undefined;

export function groupLanes(group: ContainerLike): WorkflowGroupLane[] {
  const body = record(group?.body);
  return Array.isArray(body.lanes)
    ? (body.lanes as WorkflowGroupLane[]).filter((lane) => Boolean(text(lane?.lane_id)))
    : [];
}

export function groupBodyKind(group: ContainerLike): "sequence" | "lanes" | "" {
  const kind = text(record(group?.body).kind);
  return kind === "sequence" || kind === "lanes" ? kind : "";
}

/** The step a group body (or one of its lanes) starts at, if one is set. */
export function groupBodyEntryStepId(group: ContainerLike, laneId = ""): string {
  if (laneId) {
    const lane = groupLanes(group).find((item) => text(item.lane_id) === laneId);
    return text(record(lane?.entry).id);
  }
  return text(record(record(group?.body).entry).id);
}

export function switchCaseIds(config: unknown): string[] {
  const cases = record(config).cases;
  return Array.isArray(cases)
    ? cases.map((item) => text(record(item).case_id)).filter(Boolean)
    : [];
}

export function errorPolicyToken(value: unknown): string {
  return text(record(value).on_error);
}

/** The non-error outcomes a step of this kind routes; a Stop routes none. */
export function stepOutcomes(kind: string, config: unknown): WorkflowOutcome[] {
  if (STOP_STEP_KINDS.has(kind) || LOOP_CONTROL_STEP_KINDS.has(kind)) {
    return [];
  }
  if (kind === "if") {
    return [{ name: "true" }, { name: "false" }];
  }
  if (kind === "switch") {
    return [
      ...switchCaseIds(config).map((caseId): WorkflowOutcome => ({ name: "case", case_id: caseId })),
      { name: "default" },
    ];
  }
  if (kind === "approval") {
    return [{ name: "approved" }, { name: "rejected" }];
  }
  return [{ name: "success" }];
}

export function outcomeLabel(
  outcome: WorkflowOutcome,
  t: I18nContextValue["t"] = fallbackContext.t,
  caseLabels: Record<string, string> = {},
): string {
  switch (outcome.name) {
    case "success":
      return t("react.workflows.outcome_success", "Next");
    case "true":
      return t("react.workflows.outcome_true", "True");
    case "false":
      return t("react.workflows.outcome_false", "False");
    case "default":
      return t("react.workflows.outcome_default", "Default");
    case "approved":
      return t("react.workflows.outcome_approved", "Approved");
    case "rejected":
      return t("react.workflows.outcome_rejected", "Rejected");
    case "error":
      return t("react.workflows.outcome_error", "On error");
    default:
      return caseLabels[outcome.case_id || ""] || outcome.case_id || "";
  }
}

function switchCaseLabels(config: unknown): Record<string, string> {
  const cases = record(config).cases;
  const labels: Record<string, string> = {};
  (Array.isArray(cases) ? cases : []).forEach((item) => {
    const row = record(item);
    const caseId = text(row.case_id);
    if (caseId) {
      labels[caseId] = text(row.label) || text(row.when) || caseId;
    }
  });
  return labels;
}

/**
 * Every executable nonterminal step offers an error route. Connecting it also
 * stages the existing error policy in the same change set.
 */
export function stepPorts(
  step: WorkflowStep,
  t: I18nContextValue["t"] = fallbackContext.t,
): WorkflowCanvasPort[] {
  const kind = stepKindOf(step);
  const config = record(record(step.override)[kind]);
  const labels = kind === "switch" ? switchCaseLabels(config) : {};
  const ports = stepOutcomes(kind, config).map((outcome) => ({
    outcome,
    label: outcomeLabel(outcome, t, labels),
  }));
  if (!STOP_STEP_KINDS.has(kind) && !LOOP_CONTROL_STEP_KINDS.has(kind)) {
    ports.push({ outcome: { name: "error" }, label: outcomeLabel({ name: "error" }, t) });
  }
  return ports;
}

/** Groups and For each containers expose their explicit success and error boundaries. */
export function groupPorts(
  _group: Pick<WorkflowGroup, "error_policy">,
  t: I18nContextValue["t"] = fallbackContext.t,
): WorkflowCanvasPort[] {
  const ports: WorkflowCanvasPort[] = [
    { outcome: { name: "success" }, label: outcomeLabel({ name: "success" }, t) },
  ];
  ports.push({ outcome: { name: "error" }, label: outcomeLabel({ name: "error" }, t) });
  return ports;
}

export function edgeTouchesNode(edge: WorkflowGraphEdge, ref: WorkflowNodeRef): boolean {
  return sameNodeRef(edge.source, ref) || sameNodeRef(edge.target, ref);
}

export function edgesTouchingStep(definition: WorkflowDefinitionModel, targetStepId: string) {
  const ref = nodeRef("step", targetStepId);
  return definition.graph.edges.filter((edge) => edgeTouchesNode(edge, ref));
}

export function portEdge(
  definition: WorkflowDefinitionModel,
  source: WorkflowNodeRef,
  outcome: WorkflowOutcome,
): WorkflowGraphEdge | null {
  return (
    definition.graph.edges.find(
      (edge) => sameNodeRef(edge.source, source) && sameOutcome(edge.outcome, outcome),
    ) || null
  );
}

/** Where a step is the entry: the root, a sequence body, or a lane. */
export function entryRolesOfStep(definition: WorkflowDefinitionModel, targetStepId: string): string[] {
  const roles: string[] = [];
  if (sameNodeRef(definition.graph.entry, nodeRef("step", targetStepId))) {
    roles.push("root");
  }
  definition.groups.forEach((group) => {
    if (groupBodyEntryStepId(group) === targetStepId) {
      roles.push(`body:${group.group_id}`);
    }
    groupLanes(group).forEach((lane) => {
      if (text(record(lane.entry).id) === targetStepId) {
        roles.push(`lane:${group.group_id}:${lane.lane_id}`);
      }
    });
  });
  definition.forEach.forEach((item) => {
    if (groupBodyEntryStepId(item) === targetStepId) {
      roles.push(`for_each:${item.for_each_id}`);
    }
    groupLanes(item).forEach((lane) => {
      if (text(record(lane.entry).id) === targetStepId) {
        roles.push(`for_each_lane:${item.for_each_id}:${lane.lane_id}`);
      }
    });
  });
  return roles;
}

function dropEntryRefs(definition: WorkflowDefinitionModel, targetStepId: string, kind: WorkflowNodeRef["kind"] = "step") {
  const ref = nodeRef(kind, targetStepId);
  if (sameNodeRef(definition.graph.entry, ref)) {
    definition.graph.entry = null;
  }
  [...definition.groups, ...definition.forEach].forEach((group) => {
    const body = record(group.body);
    if (sameNodeRef(body.entry, ref)) {
      delete body.entry;
    }
    groupLanes(group).forEach((lane) => {
      if (sameNodeRef(lane.entry, ref)) {
        delete lane.entry;
      }
    });
  });
}

/** What a step keeps through a kind change; the rest comes from the new payload. */
const STEP_KIND_INDEPENDENT_FIELDS = [
  "step_id",
  "group_id",
  "for_each_id",
  "lane_id",
  "display_name",
  "error_policy",
  "boundary_contract",
  "optional",
  "disabled",
] as const;

/** The step a change_step_kind produces, exactly as the server builds it. */
export function stepAfterKindChange(current: WorkflowStep, payload: Record<string, unknown>): WorkflowStep {
  const kept: Record<string, unknown> = {};
  const source = current as Record<string, unknown>;
  STEP_KIND_INDEPENDENT_FIELDS.forEach((key) => {
    if (key in source) {
      kept[key] = source[key];
    }
  });
  return { ...kept, ...clone(payload), step_id: stepId(current) } as WorkflowStep;
}

/**
 * The edges a kind change drops: those on outcomes the new kind does not have.
 * An error edge stays unless the step becomes a Stop.
 */
export function edgesDroppedByKindChange(
  definition: WorkflowDefinitionModel,
  targetStepId: string,
  payload: Record<string, unknown>,
): WorkflowGraphEdge[] {
  const kind = text(payload.step_kind);
  const config = record(record(payload.override)[kind]);
  const kept = stepOutcomes(kind, config);
  if (!STOP_STEP_KINDS.has(kind) && !LOOP_CONTROL_STEP_KINDS.has(kind)) {
    kept.push({ name: "error" });
  }
  const ref = nodeRef("step", targetStepId);
  return definition.graph.edges.filter(
    (edge) => sameNodeRef(edge.source, ref) && !kept.some((outcome) => sameOutcome(outcome, edge.outcome)),
  );
}

function applyOne(definition: WorkflowDefinitionModel, operation: WorkflowChangeOperation): string {
  const findStep = (id: string) => definition.steps.find((step) => stepId(step) === id);
  const findGroup = (id: string) => definition.groups.find((group) => text(group.group_id) === id);
  const findForEach = (id: string) => definition.forEach.find((item) => text(item.for_each_id) === id);
  switch (operation.op) {
    case "create_step": {
      const step = clone(operation.step);
      if (!stepId(step) || findStep(stepId(step))) {
        return `step ${stepId(step)} exists`;
      }
      step.group_id = text(step.group_id) || null;
      step.for_each_id = text(step.for_each_id) || null;
      step.lane_id = text(step.lane_id) || null;
      definition.steps.push(step);
      if (stepIsRoot(step) && !definition.graph.entry) {
        definition.graph.entry = nodeRef("step", stepId(step));
      }
      return "";
    }
    case "create_for_each": {
      const item = clone(operation.for_each);
      if (!text(item.for_each_id) || findForEach(text(item.for_each_id))) {
        return `for each ${text(item.for_each_id)} exists`;
      }
      item.group_id = text(item.group_id) || null;
      definition.forEach.push(item);
      if (!item.group_id && !definition.graph.entry) {
        definition.graph.entry = nodeRef("for_each", text(item.for_each_id));
      }
      return "";
    }
    case "update_for_each_config": {
      const item = findForEach(operation.for_each_id);
      if (!item) return `for each ${operation.for_each_id} missing`;
      Object.assign(item, clone(operation.patch));
      return "";
    }
    case "move_for_each": {
      const item = findForEach(operation.for_each_id);
      if (!item) return `for each ${operation.for_each_id} missing`;
      item.group_id = operation.group_id || null;
      const ref = nodeRef("for_each", operation.for_each_id);
      definition.graph.edges = definition.graph.edges.filter((edge) => !edgeTouchesNode(edge, ref));
      dropEntryRefs(definition, operation.for_each_id, "for_each");
      return "";
    }
    case "delete_for_each": {
      if (!findForEach(operation.for_each_id)) return `for each ${operation.for_each_id} missing`;
      removeForEach(definition, operation.for_each_id);
      return "";
    }
    case "create_group": {
      const group = clone(operation.group);
      if (!text(group.group_id) || findGroup(text(group.group_id))) {
        return `group ${text(group.group_id)} exists`;
      }
      definition.groups.push(group);
      definition.graph.entry ??= nodeRef("group", text(group.group_id));
      return "";
    }
    case "update_step_config": {
      const step = findStep(operation.step_id);
      if (!step) return `step ${operation.step_id} missing`;
      Object.assign(step, clone(operation.patch));
      return "";
    }
    case "change_step_kind": {
      const index = definition.steps.findIndex((step) => stepId(step) === operation.step_id);
      if (index < 0) return `step ${operation.step_id} missing`;
      const dropped = new Set(edgesDroppedByKindChange(definition, operation.step_id, operation.step));
      definition.steps[index] = stepAfterKindChange(definition.steps[index], operation.step);
      definition.graph.edges = definition.graph.edges.filter((edge) => !dropped.has(edge));
      return "";
    }
    case "update_group_config": {
      const group = findGroup(operation.group_id);
      if (!group) return `group ${operation.group_id} missing`;
      Object.assign(group, clone(operation.patch));
      return "";
    }
    case "move_step": {
      const step = findStep(operation.step_id);
      if (!step) return `step ${operation.step_id} missing`;
      step.group_id = operation.group_id || null;
      step.for_each_id = operation.for_each_id || null;
      step.lane_id = operation.lane_id || null;
      const ref = nodeRef("step", operation.step_id);
      definition.graph.edges = definition.graph.edges.filter((edge) => !edgeTouchesNode(edge, ref));
      dropEntryRefs(definition, operation.step_id);
      return "";
    }
    case "delete_step": {
      if (!findStep(operation.step_id)) return `step ${operation.step_id} missing`;
      definition.steps = definition.steps.filter((step) => stepId(step) !== operation.step_id);
      const ref = nodeRef("step", operation.step_id);
      definition.graph.edges = definition.graph.edges.filter((edge) => !edgeTouchesNode(edge, ref));
      dropEntryRefs(definition, operation.step_id);
      return "";
    }
    case "delete_group": {
      if (!findGroup(operation.group_id)) return `group ${operation.group_id} missing`;
      definition.forEach
        .filter((item) => text(item.group_id) === operation.group_id)
        .map((item) => text(item.for_each_id))
        .forEach((forEachId) => removeForEach(definition, forEachId));
      const removed = new Set(
        definition.steps.filter((step) => stepGroupId(step) === operation.group_id).map(stepId),
      );
      definition.steps = definition.steps.filter((step) => stepGroupId(step) !== operation.group_id);
      definition.groups = definition.groups.filter((group) => text(group.group_id) !== operation.group_id);
      const groupRef = nodeRef("group", operation.group_id);
      definition.graph.edges = definition.graph.edges.filter(
        (edge) =>
          !edgeTouchesNode(edge, groupRef) &&
          !(edge.target.kind === "group_exit" && edge.target.group_id === operation.group_id) &&
          !removed.has(edge.source.kind === "step" ? edge.source.id : "") &&
          !(edge.target.kind === "step" && removed.has(edge.target.id)),
      );
      if (sameNodeRef(definition.graph.entry, groupRef)) {
        definition.graph.entry = null;
      }
      return "";
    }
    case "set_entry": {
      if (!operation.group_id && !operation.for_each_id) {
        definition.graph.entry = clone(operation.target);
        return "";
      }
      const owner = operation.group_id ? findGroup(operation.group_id) : findForEach(operation.for_each_id || "");
      if (!owner) return `container ${operation.group_id || operation.for_each_id} missing`;
      const body = { ...record(owner.body) };
      const entry = operation.target.kind === "for_each"
        ? nodeRef("for_each", operation.target.id)
        : nodeRef("step", operation.target.id);
      if (operation.lane_id) {
        const lanes = groupLanes({ body }).map((lane) =>
          text(lane.lane_id) === operation.lane_id ? { ...lane, entry } : lane,
        );
        body.lanes = lanes;
      } else {
        body.entry = entry;
      }
      owner.body = body;
      return "";
    }
    case "connect": {
      const existing = portEdge(definition, operation.edge.source, operation.edge.outcome);
      if (existing && !operation.replace) return "outcome taken";
      definition.graph.edges = definition.graph.edges.filter((edge) => edge !== existing);
      definition.graph.edges.push(clone(operation.edge));
      return "";
    }
    case "disconnect": {
      const existing = portEdge(definition, operation.source, operation.outcome);
      if (!existing) return "edge missing";
      definition.graph.edges = definition.graph.edges.filter((edge) => edge !== existing);
      return "";
    }
    case "insert_step_between": {
      const existing = portEdge(definition, operation.source, operation.outcome);
      if (!existing) return "edge missing";
      const sourceStep = operation.source.kind === "step" ? findStep(operation.source.id) : null;
      const sourceForEach = operation.source.kind === "for_each" ? findForEach(operation.source.id) : null;
      const step = clone(operation.step);
      step.group_id = sourceStep ? stepGroupId(sourceStep) || null : text(sourceForEach?.group_id) || null;
      step.for_each_id = sourceStep ? stepForEachId(sourceStep) || null : null;
      step.lane_id = sourceStep ? stepLaneId(sourceStep) || null : null;
      definition.steps.push(step);
      const index = definition.graph.edges.indexOf(existing);
      definition.graph.edges.splice(
        index,
        1,
        { ...existing, target: nodeRef("step", stepId(step)) },
        {
          source: nodeRef("step", stepId(step)),
          outcome: { name: "success" },
          target: clone(existing.target),
          rework: false,
        },
      );
      return "";
    }
    default:
      return "";
  }
}

function removeForEach(definition: WorkflowDefinitionModel, forEachId: string) {
  const removed = new Set(
    definition.steps.filter((step) => stepForEachId(step) === forEachId).map(stepId),
  );
  definition.steps = definition.steps.filter((step) => stepForEachId(step) !== forEachId);
  definition.forEach = definition.forEach.filter((item) => text(item.for_each_id) !== forEachId);
  const ref = nodeRef("for_each", forEachId);
  definition.graph.edges = definition.graph.edges.filter(
    (edge) =>
      !edgeTouchesNode(edge, ref) &&
      !(edge.target.kind === "for_each_exit" && edge.target.for_each_id === forEachId) &&
      !removed.has(edge.source.kind === "step" ? edge.source.id : "") &&
      !(edge.target.kind === "step" && removed.has(edge.target.id)),
  );
  dropEntryRefs(definition, forEachId, "for_each");
}

export type ChangeSetPreview = {
  definition: WorkflowDefinitionModel;
  /** Operation index -> why the preview could not apply it (the server decides on Apply). */
  rejected: Record<number, string>;
};

export function applyChangeOperations(
  base: WorkflowDefinitionModel,
  operations: WorkflowChangeOperation[],
): ChangeSetPreview {
  const definition = clone(base);
  const rejected: Record<number, string> = {};
  operations.forEach((operation, index) => {
    const reason = applyOne(definition, operation);
    if (reason) {
      rejected[index] = reason;
    }
  });
  return { definition, rejected };
}

/** Only a step with exactly one outcome (success) can take an edge's place; the engine refuses others. */
export function stepInsertableBetween(step: WorkflowStep): boolean {
  const kind = stepKindOf(step);
  const outcomes = stepOutcomes(kind, record(record(step.override)[kind]));
  return !STOP_STEP_KINDS.has(kind) && outcomes.length === 1 && outcomes[0].name === "success";
}

/** Only a step whose one outcome is Next can be disabled: the run goes on along it. */
export function stepCanBeDisabled(step: WorkflowStep): boolean {
  return stepInsertableBetween(step) && !LOOP_CONTROL_STEP_KINDS.has(stepKindOf(step));
}

/** Whether *source* still exists with this outcome among its ports. */
function sourceHasOutcome(definition: WorkflowDefinitionModel, source: WorkflowNodeRef, outcome: WorkflowOutcome): boolean {
  if (source.kind === "step") {
    const step = definition.steps.find((item) => stepId(item) === source.id);
    return Boolean(step) && stepPorts(step as WorkflowStep).some((port) => sameOutcome(port.outcome, outcome));
  }
  const exists = source.kind === "group"
    ? definition.groups.some((group) => text(group.group_id) === source.id)
    : source.kind === "for_each" && definition.forEach.some((item) => text(item.for_each_id) === source.id);
  return exists && ["success", "error"].includes(outcome.name);
}

/** The owner fields of a step created after *source*: the scope an inserted step would join. */
function ownerAfterSource(definition: WorkflowDefinitionModel, source: WorkflowNodeRef) {
  if (source.kind === "step") {
    return placementOwnerFields(stepPlacement(definition.steps.find((item) => stepId(item) === source.id)));
  }
  const forEach = source.kind === "for_each" ? definition.forEach.find((item) => text(item.for_each_id) === source.id) : null;
  return placementOwnerFields({ ...ROOT_PLACEMENT, groupId: text(forEach?.group_id) });
}

/**
 * One operation restated against the definition it meets at its place in the set.
 *
 * Operations are staged against the preview of the set so far, but a later
 * edit can change what an earlier one leaves behind: deleting a step the set
 * created drops its create and its connections, and editing a staged step can
 * change its kind. The engine refuses a set atomically when an operation names
 * an edge that is not there (`workflow.change.edge_unknown`), so the set is
 * kept in the shape the engine applies:
 * - a disconnect of an edge that no longer exists is already done, and is dropped;
 * - an insert on an edge that no longer exists, or of a step that cannot take
 *   an edge's place, becomes what "Add after" stages for a free outcome: create
 *   the step in the source's scope and connect it after the source (replacing
 *   the edge when one is still there).
 */
function restatedOperation(
  definition: WorkflowDefinitionModel,
  operation: WorkflowChangeOperation,
): WorkflowChangeOperation[] {
  if (operation.op === "disconnect") {
    return portEdge(definition, operation.source, operation.outcome) ? [operation] : [];
  }
  if (operation.op !== "insert_step_between") {
    return [operation];
  }
  const existing = portEdge(definition, operation.source, operation.outcome);
  if (existing && stepInsertableBetween(operation.step)) {
    return [operation];
  }
  const created: WorkflowChangeOperation = {
    op: "create_step",
    step: { ...operation.step, ...ownerAfterSource(definition, operation.source) },
  };
  if (!sourceHasOutcome(definition, operation.source, operation.outcome)) {
    return [created];
  }
  return [
    created,
    {
      op: "connect",
      edge: { source: clone(operation.source), outcome: clone(operation.outcome), target: nodeRef("step", stepId(operation.step)) },
      ...(existing ? { replace: true } : {}),
    },
  ];
}

/**
 * A parameter edit of a step this same set creates (or whose kind it changes)
 * travels inside that creation, as one change. The history keeps the edit as
 * its own Undo step; only the set that Apply sends is folded.
 */
export function foldStagedStepEdits(operations: WorkflowChangeOperation[]): WorkflowChangeOperation[] {
  const result: WorkflowChangeOperation[] = [];
  operations.forEach((operation) => {
    if (operation.op !== "update_step_config") {
      result.push(operation);
      return;
    }
    for (let index = result.length - 1; index >= 0; index -= 1) {
      const earlier = result[index];
      if ((earlier.op === "create_step" || earlier.op === "insert_step_between") && stepId(earlier.step) === operation.step_id) {
        result[index] = { ...earlier, step: { ...earlier.step, ...clone(operation.patch) } };
        return;
      }
      if (earlier.op === "change_step_kind" && earlier.step_id === operation.step_id) {
        result[index] = { ...earlier, step: { ...earlier.step, ...clone(operation.patch) } };
        return;
      }
      if (earlier.op === "delete_step" && earlier.step_id === operation.step_id) break;
    }
    result.push(operation);
  });
  return result;
}

/** The set in the shape the engine applies, with each operation valid where it stands. */
export function reconcileChangeOperations(
  base: WorkflowDefinitionModel,
  operations: WorkflowChangeOperation[],
): WorkflowChangeOperation[] {
  const definition = clone(base);
  const result: WorkflowChangeOperation[] = [];
  operations.forEach((operation) => {
    restatedOperation(definition, operation).forEach((restated) => {
      applyOne(definition, restated);
      result.push(restated);
    });
  });
  return result;
}

/** Steps a change set created; they exist only locally until Apply. */
export function locallyCreatedStepIds(operations: WorkflowChangeOperation[]): Set<string> {
  const ids = new Set<string>();
  operations.forEach((operation) => {
    if (operation.op === "create_step" || operation.op === "insert_step_between") {
      ids.add(stepId(operation.step));
    }
  });
  return ids;
}

export function locallyCreatedForEachIds(operations: WorkflowChangeOperation[]): Set<string> {
  return new Set(
    operations
      .filter((operation): operation is Extract<WorkflowChangeOperation, { op: "create_for_each" }> =>
        operation.op === "create_for_each",
      )
      .map((operation) => text(operation.for_each.for_each_id)),
  );
}

export function locallyCreatedGroupIds(operations: WorkflowChangeOperation[]): Set<string> {
  return new Set(
    operations
      .filter((operation): operation is Extract<WorkflowChangeOperation, { op: "create_group" }> =>
        operation.op === "create_group",
      )
      .map((operation) => text(operation.group.group_id)),
  );
}

function refNamesStep(ref: unknown, targetStepId: string): boolean {
  return sameNodeRef(ref, nodeRef("step", targetStepId));
}

/** Whether a structural operation concerns this step (membership, kind/ports, edges, entry). */
export function operationTouchesStep(operation: WorkflowChangeOperation, targetStepId: string): boolean {
  switch (operation.op) {
    case "create_step":
    case "insert_step_between":
      return (
        stepId(operation.step) === targetStepId ||
        (operation.op === "insert_step_between" && refNamesStep(operation.source, targetStepId))
      );
    case "update_step_config":
    case "change_step_kind":
    case "move_step":
    case "delete_step":
      return operation.step_id === targetStepId;
    case "set_entry":
      return refNamesStep(operation.target, targetStepId);
    case "connect":
      return refNamesStep(operation.edge.source, targetStepId) || refNamesStep(operation.edge.target, targetStepId);
    case "disconnect":
      return refNamesStep(operation.source, targetStepId);
    default:
      return false;
  }
}

export function operationTouchesForEach(operation: WorkflowChangeOperation, forEachId: string): boolean {
  const ref = nodeRef("for_each", forEachId);
  switch (operation.op) {
    case "create_for_each":
      return text(operation.for_each.for_each_id) === forEachId;
    case "update_for_each_config":
    case "delete_for_each":
    case "move_for_each":
      return operation.for_each_id === forEachId;
    case "set_entry":
      return operation.for_each_id === forEachId || sameNodeRef(operation.target, ref);
    case "connect":
      return (
        sameNodeRef(operation.edge.source, ref) ||
        sameNodeRef(operation.edge.target, ref) ||
        (operation.edge.target.kind === "for_each_exit" && operation.edge.target.for_each_id === forEachId)
      );
    case "disconnect":
      return sameNodeRef(operation.source, ref);
    case "move_step":
      return operation.for_each_id === forEachId;
    default:
      return false;
  }
}

export function operationTouchesGroup(operation: WorkflowChangeOperation, groupId: string): boolean {
  const groupRef = nodeRef("group", groupId);
  switch (operation.op) {
    case "create_group":
      return text(operation.group.group_id) === groupId;
    case "update_group_config":
    case "delete_group":
      return operation.group_id === groupId;
    case "set_entry":
      return operation.group_id === groupId || sameNodeRef(operation.target, groupRef);
    case "connect":
      return (
        sameNodeRef(operation.edge.source, groupRef) ||
        sameNodeRef(operation.edge.target, groupRef) ||
        (operation.edge.target.kind === "group_exit" && operation.edge.target.group_id === groupId)
      );
    case "disconnect":
      return sameNodeRef(operation.source, groupRef);
    case "move_step":
      return operation.group_id === groupId;
    default:
      return false;
  }
}

/** A step id not used in the definition yet, derived from a label. */
export function uniqueLocalId(label: string, taken: Iterable<string>, fallback = "step"): string {
  const used = new Set(taken);
  const base =
    label
      .trim()
      .toLowerCase()
      .replaceAll(/[^a-z0-9]+/g, "_")
      .replaceAll(/^_|_$/g, "") || fallback;
  if (!used.has(base)) {
    return base;
  }
  for (let index = 2; index < 10_000; index += 1) {
    const candidate = `${base}_${index}`;
    if (!used.has(candidate)) {
      return candidate;
    }
  }
  return `${base}_${Date.now()}`;
}

export type LocalDefinitionIssue = {
  code: string;
  message: string;
  stepId?: string;
  groupId?: string;
  forEachId?: string;
};

/**
 * Plain structural gaps of a previewed definition: a missing entry, an outcome
 * that leads nowhere. The authoritative check is the server's, on Apply.
 */
export function localDefinitionIssues(
  definition: WorkflowDefinitionModel,
  t: I18nContextValue["t"] = fallbackContext.t,
): LocalDefinitionIssue[] {
  const issues: LocalDefinitionIssue[] = [];
  const rootNodes = [
    ...definition.groups.map((group) => nodeRef("group", text(group.group_id))),
    ...definition.forEach.filter((item) => !text(item.group_id)).map((item) => nodeRef("for_each", text(item.for_each_id))),
    ...definition.steps.filter(stepIsRoot).map((step) => nodeRef("step", stepId(step))),
  ];
  if (rootNodes.length && !definition.graph.entry) {
    issues.push({
      code: "entry_missing",
      message: t("react.workflows.issue_entry_missing", "The workflow has no entry. Choose the step or group it starts with."),
    });
  }
  definition.steps.forEach((step) => {
    const id = stepId(step);
    stepPorts(step, t).forEach((port) => {
      if (port.outcome.name === "error") return;
      if (!portEdge(definition, nodeRef("step", id), port.outcome)) {
        issues.push({
          code: "outcome_unrouted",
          message: t("react.workflows.issue_outcome_unrouted", "Outcome \"{outcome}\" of step {step} leads nowhere.", {
            outcome: port.label,
            step: text(step.display_name) || id,
          }),
          stepId: id,
        });
      }
    });
  });
  definition.steps.filter((step) => step.disabled === true && !stepCanBeDisabled(step)).forEach((step) => {
    issues.push({
      code: "step_disabled_needs_next",
      message: t("react.workflows.issue_step_disabled_needs_next", "{step}: only a step with one Next outcome can be disabled.", { step: text(step.display_name) || stepId(step) }),
      stepId: stepId(step),
    });
  });
  definition.groups.forEach((group) => {
    const id = text(group.group_id);
    const label = text(group.display_name) || id;
    if (!portEdge(definition, nodeRef("group", id), { name: "success" })) {
      issues.push({
        code: "group_outcome_unrouted",
        message: t("react.workflows.issue_group_outcome_unrouted", "Group {group} has no next step after it succeeds.", { group: label }),
        groupId: id,
      });
    }
    if (groupBodyKind(group) === "sequence" && !groupBodyEntryStepId(group)) {
      issues.push({
        code: "group_entry_missing",
        message: t("react.workflows.issue_group_entry_missing", "Group {group} has no entry step.", { group: label }),
        groupId: id,
      });
    }
    groupLanes(group).forEach((lane) => {
      if (!text(record(lane.entry).id)) {
        issues.push({
          code: "lane_entry_missing",
          message: t("react.workflows.issue_lane_entry_missing", "Lane {lane} of group {group} has no entry step.", {
            group: label,
            lane: text(lane.lane_id),
          }),
          groupId: id,
        });
      }
    });
  });
  definition.forEach.forEach((item) => {
    const id = text(item.for_each_id);
    const label = text(item.display_name) || id;
    if (!portEdge(definition, nodeRef("for_each", id), { name: "success" })) {
      issues.push({
        code: "for_each_outcome_unrouted",
        message: t("react.workflows.issue_for_each_outcome_unrouted", "For each {for_each} has no next step after it finishes.", { for_each: label }),
        forEachId: id,
      });
    }
    if (!text(item.over) || !text(item.item_name)) {
      issues.push({
        code: "for_each_source_missing",
        message: t("react.workflows.issue_for_each_source_missing", "For each {for_each} needs a source list and an item name.", { for_each: label }),
        forEachId: id,
      });
    }
    if (groupBodyKind(item) === "sequence" && !groupBodyEntryStepId(item)) {
      issues.push({
        code: "for_each_entry_missing",
        message: t("react.workflows.issue_for_each_entry_missing", "For each {for_each} has no entry step.", { for_each: label }),
        forEachId: id,
      });
    }
  });
  return issues;
}

function kindNodeType(kind: string): string {
  const types: Record<string, string> = {
    if: "ifStep",
    switch: "switchStep",
    batch: "batch",
    approval: "approval",
    wait: "wait",
    stop_success: "end",
    stop_fail: "end",
  };
  return types[kind] || "action";
}

/**
 * The canvas graph of the previewed definition: the server's projection for
 * every node it already knows, plus locally created nodes, with membership,
 * entry and edges taken from the preview.
 */
export function canvasGraphWithDefinition(
  canvasGraph: WorkflowCanvasGraph,
  definition: WorkflowDefinitionModel,
  t: I18nContextValue["t"] = fallbackContext.t,
): WorkflowCanvasGraph {
  const knownSteps = new Map((canvasGraph.steps || []).map((step) => [step.step_id || step.id, step]));
  const knownGroups = new Map((canvasGraph.groups || []).map((group) => [group.group_id || group.id, group]));
  const steps: WorkflowCanvasStep[] = definition.steps.map((step) => {
    const id = stepId(step);
    const known = knownSteps.get(id);
    const kind = stepKindOf(step);
    return {
      ...(known || {
        id,
        step_id: id,
        kind,
        node_type: kindNodeType(kind),
        config_summary: kind,
      }),
      kind,
      node_type: kindNodeType(kind),
      config_summary: known?.kind === kind ? known.config_summary : kind === "if"
        ? ifConditionSummary(record(record(step.override).if), t)
        : kind,
      display_name: text(step.display_name) || known?.display_name || id,
      group_id: stepGroupId(step) || null,
      for_each_id: stepForEachId(step) || null,
      lane_id: stepLaneId(step) || null,
      ports: stepPorts(step, t),
      error_policy: record(step.error_policy),
      extension_id: stepExtensionId(step as Record<string, unknown>) || null,
      disabled: step.disabled === true,
      ...(kind === "connector" ? { tool_category: connectorToolCategory(step as Record<string, unknown>, record(record(step.override).connector)) } : {}),
    };
  });
  const groups: WorkflowCanvasGroup[] = definition.groups.map((group) => {
    const id = text(group.group_id);
    const known = knownGroups.get(id);
    return {
      ...(known || { id, group_id: id }),
      display_name: text(group.display_name) || known?.display_name || id,
      body: record(group.body),
      error_policy: record(group.error_policy),
    };
  });
  const knownForEach = new Map((canvasGraph.for_each || []).map((item) => [item.for_each_id || item.id, item]));
  const forEach: WorkflowCanvasForEach[] = definition.forEach.map((item) => {
    const id = text(item.for_each_id);
    const known = knownForEach.get(id);
    return {
      ...(known || { id, for_each_id: id }),
      group_id: text(item.group_id) || null,
      display_name: text(item.display_name) || known?.display_name || id,
      over: text(item.over),
      item_name: text(item.item_name),
      index_name: text(item.index_name) || null,
      body: record(item.body),
      error_policy: record(item.error_policy),
    };
  });
  return {
    ...canvasGraph,
    entry: definition.graph.entry,
    steps,
    groups,
    for_each: forEach,
    edges: definition.graph.edges,
  };
}
