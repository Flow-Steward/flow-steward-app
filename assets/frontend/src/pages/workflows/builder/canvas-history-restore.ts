import { sameNodeRef, type WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import {
  applyChangeOperations,
  placementOwnerFields,
  samePlacement,
  stepId,
  stepPlacement,
  type WorkflowDefinitionModel,
} from "@/pages/workflows/builder/change-set";
import type { WorkflowGraphEdge } from "@/pages/workflows/shared/api";

/**
 * The operations that turn one definition back into another (Canvas › Undo).
 *
 * Undo of a change that Apply already saved cannot drop it from the set: the
 * server has it. It stages the reverse instead, as ordinary operations the
 * author applies like any other. The reverse is the difference between the
 * definition after the change and the one before it, so it names only what the
 * change touched: the steps, containers, connections and entries that differ.
 */

const STEP_STRUCTURE_FIELDS = new Set(["step_id", "step_kind", "group_id", "for_each_id", "lane_id"]);
const GROUP_STRUCTURE_FIELDS = new Set(["group_id"]);
const FOR_EACH_STRUCTURE_FIELDS = new Set(["for_each_id", "group_id"]);

type Row = Record<string, unknown>;

function text(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

/** JSON with sorted keys and without undefined values: equal configurations compare equal. */
function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map((item) => stableJson(item ?? null)).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Row)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => left.localeCompare(right));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function byId<T>(rows: readonly T[], id: (row: T) => string): Map<string, T> {
  return new Map(rows.map((row) => [id(row), row]));
}

/** The fields to set so that `current` reads like `target`; a field `target` lacks becomes null. */
function configPatch(current: Row, target: Row, structure: ReadonlySet<string>): Row {
  const patch: Row = {};
  const keys = new Set([...Object.keys(current), ...Object.keys(target)]);
  keys.forEach((key) => {
    if (structure.has(key)) return;
    const wanted = target[key];
    if (stableJson(current[key]) === stableJson(wanted)) return;
    patch[key] = wanted === undefined ? null : clone(wanted);
  });
  return patch;
}

function edgePort(edge: WorkflowGraphEdge): string {
  return stableJson([edge.source, edge.outcome?.name ?? "", edge.outcome?.case_id ?? ""]);
}

function edgeShape(edge: WorkflowGraphEdge): string {
  return stableJson({ ...edge, rework: Boolean(edge.rework) });
}

function edgeOperation(edge: WorkflowGraphEdge, replace: boolean): WorkflowChangeOperation {
  const restored = clone(edge);
  if (!restored.rework) delete restored.rework;
  return replace ? { op: "connect", edge: restored, replace: true } : { op: "connect", edge: restored };
}

const groupKey = (row: { group_id?: unknown }) => text(row.group_id);
const forEachKey = (row: { for_each_id?: unknown }) => text(row.for_each_id);

/**
 * Operations that take `from` to `to`. Applied in order they rebuild containers
 * and steps first (a move or a delete drops a step's connections and entries),
 * then restore configuration, connections and the root entry.
 */
export function definitionRestoreOperations(
  from: WorkflowDefinitionModel,
  to: WorkflowDefinitionModel,
): WorkflowChangeOperation[] {
  const structural: WorkflowChangeOperation[] = [];
  const fromSteps = byId(from.steps, stepId);
  const toSteps = byId(to.steps, stepId);
  const fromGroups = byId(from.groups, groupKey);
  const toGroups = byId(to.groups, groupKey);
  const fromForEach = byId(from.forEach, forEachKey);
  const toForEach = byId(to.forEach, forEachKey);

  // 1. Containers that come back, then the steps that come back, in their places.
  to.groups.forEach((group) => {
    if (!fromGroups.has(groupKey(group))) structural.push({ op: "create_group", group: clone(group) });
  });
  to.forEach.forEach((item) => {
    if (!fromForEach.has(forEachKey(item))) structural.push({ op: "create_for_each", for_each: clone(item) });
  });
  to.forEach.forEach((item) => {
    const current = fromForEach.get(forEachKey(item));
    if (current && text(current.group_id) !== text(item.group_id)) {
      structural.push({ op: "move_for_each", for_each_id: forEachKey(item), group_id: text(item.group_id) || null });
    }
  });
  to.steps.forEach((step) => {
    const current = fromSteps.get(stepId(step));
    if (!current) {
      structural.push({ op: "create_step", step: { ...clone(step), step_id: stepId(step), step_kind: text(step.step_kind) } });
      return;
    }
    if (!samePlacement(stepPlacement(current), stepPlacement(step))) {
      structural.push({ op: "move_step", step_id: stepId(step), ...placementOwnerFields(stepPlacement(step)) });
    }
    if (text(current.step_kind) !== text(step.step_kind)) {
      const payload = Object.fromEntries(Object.entries(clone(step) as Row)
        .filter(([key]) => !["step_id", "group_id", "for_each_id", "lane_id"].includes(key)));
      structural.push({ op: "change_step_kind", step_id: stepId(step), step: payload as Row & { step_kind: string } });
    }
  });
  // 2. What the change created goes away: steps first, then emptied containers.
  from.steps.forEach((step) => {
    if (!toSteps.has(stepId(step))) structural.push({ op: "delete_step", step_id: stepId(step) });
  });
  from.forEach.forEach((item) => {
    if (!toForEach.has(forEachKey(item))) structural.push({ op: "delete_for_each", for_each_id: forEachKey(item), with_contents: true });
  });
  from.groups.forEach((group) => {
    if (!toGroups.has(groupKey(group))) structural.push({ op: "delete_group", group_id: groupKey(group), with_contents: true });
  });

  // 3. Configuration, judged on what the structural operations leave.
  const afterStructure = applyChangeOperations(from, structural).definition;
  const configuration: WorkflowChangeOperation[] = [];
  afterStructure.steps.forEach((step) => {
    const target = toSteps.get(stepId(step));
    if (!target) return;
    const patch = configPatch(step as Row, target as Row, STEP_STRUCTURE_FIELDS);
    if (Object.keys(patch).length) configuration.push({ op: "update_step_config", step_id: stepId(step), patch });
  });
  afterStructure.groups.forEach((group) => {
    const target = toGroups.get(groupKey(group));
    if (!target) return;
    const patch = configPatch(group as Row, target as Row, GROUP_STRUCTURE_FIELDS);
    if (Object.keys(patch).length) configuration.push({ op: "update_group_config", group_id: groupKey(group), patch });
  });
  afterStructure.forEach.forEach((item) => {
    const target = toForEach.get(forEachKey(item));
    if (!target) return;
    const patch = configPatch(item as Row, target as Row, FOR_EACH_STRUCTURE_FIELDS);
    if (Object.keys(patch).length) configuration.push({ op: "update_for_each_config", for_each_id: forEachKey(item), patch });
  });

  // 4. Connections and the root entry, judged on the restored nodes.
  const afterConfiguration = applyChangeOperations(afterStructure, configuration).definition;
  const graph: WorkflowChangeOperation[] = [];
  const targetEdges = new Map(to.graph.edges.map((edge) => [edgePort(edge), edge]));
  const currentEdges = new Map(afterConfiguration.graph.edges.map((edge) => [edgePort(edge), edge]));
  currentEdges.forEach((edge, port) => {
    if (!targetEdges.has(port)) graph.push({ op: "disconnect", source: clone(edge.source), outcome: clone(edge.outcome) });
  });
  targetEdges.forEach((edge, port) => {
    const current = currentEdges.get(port);
    if (!current) graph.push(edgeOperation(edge, false));
    else if (edgeShape(current) !== edgeShape(edge)) graph.push(edgeOperation(edge, true));
  });
  const entry = to.graph.entry;
  if (entry && !sameNodeRef(afterConfiguration.graph.entry, entry)) {
    graph.push({ op: "set_entry", target: clone(entry) });
  }
  return [...structural, ...configuration, ...graph];
}

/** Whether `operations` apply to `definition` without a refused operation. */
export function operationsApply(definition: WorkflowDefinitionModel, operations: WorkflowChangeOperation[]): boolean {
  return Object.keys(applyChangeOperations(definition, operations).rejected).length === 0;
}

export { stableJson as stableDefinitionJson };
