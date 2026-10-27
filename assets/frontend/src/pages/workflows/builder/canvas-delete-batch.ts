import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import { deriveDeletionClosure, deletionOperation, type DeletionClosure, type DeletionScope, type DeletionTarget } from "@/pages/workflows/builder/canvas-delete-guard";
import { stepForEachId, stepGroupId, stepId, type WorkflowDefinitionModel } from "@/pages/workflows/builder/change-set";

function compareCanvasIds(left: string, right: string): number {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

export type DeleteBatchSelection = {
  /** Sorted, unique displayed React Flow IDs, including the Hook display prefix. */
  canvasIds: readonly string[];
  generation: number;
  /** Author or one Hook instance, not merely the child workflow ID. */
  ownerScopeKey: string;
};

export type DeletionBatch = {
  /** Top-level canonical roots after selected descendants have been removed. */
  roots: DeletionTarget[];
  closure: DeletionClosure;
  operations: WorkflowChangeOperation[];
};

export function validDeleteBatchSelection(selection: DeleteBatchSelection): boolean {
  return Boolean(selection.ownerScopeKey && Number.isSafeInteger(selection.generation) && selection.generation >= 0 &&
    selection.canvasIds.length > 0 && selection.canvasIds.every((id, index) => typeof id === "string" && id.length > 0 &&
      (index === 0 || selection.canvasIds[index - 1] < id)));
}

export function sameDeleteBatchSelection(expected: DeleteBatchSelection, current: DeleteBatchSelection | null): boolean {
  return Boolean(current && validDeleteBatchSelection(expected) && validDeleteBatchSelection(current) &&
    expected.ownerScopeKey === current.ownerScopeKey && expected.generation === current.generation &&
    expected.canvasIds.length === current.canvasIds.length &&
    expected.canvasIds.every((id, index) => id === current.canvasIds[index]));
}

function covered(closure: DeletionClosure, target: DeletionTarget): boolean {
  if (target.kind === "step") return closure.steps.includes(target.id);
  if (target.kind === "group") return closure.groups.includes(target.id);
  return closure.forEach.includes(target.id);
}

const kindOrder = { group: 0, for_each: 1, step: 2 } as const;
const targetKey = (target: DeletionTarget) => `${target.kind}:${target.id}`;

/** All targets and scopes are canonical definition members; visual Hook frames never enter here. */
export function deriveDeletionBatch(definition: WorkflowDefinitionModel, selected: readonly DeletionTarget[]): DeletionBatch | null {
  if (!selected.length || selected.some((target) => !target.id || !["step", "group", "for_each"].includes(target.kind)) ||
    new Set(selected.map(targetKey)).size !== selected.length) return null;
  const parts = selected.map((target) => ({ target, closure: deriveDeletionClosure(definition, target) }));
  if (parts.some(({ closure }) => !closure)) return null;
  // A single-target closure uses Sets; check only participating rows before those
  // Sets could hide duplicate IDs or a Step with two immediate containers.
  const participatingSteps = new Set(parts.flatMap(({ closure }) => closure?.steps || []));
  const participatingGroups = new Set(parts.flatMap(({ closure }) => closure?.groups || []));
  const participatingForEach = new Set(parts.flatMap(({ closure }) => closure?.forEach || []));
  for (const id of participatingSteps) {
    const rows = definition.steps.filter((step) => stepId(step) === id);
    if (rows.length !== 1) return null;
    const groupId = stepGroupId(rows[0]);
    const forEachId = stepForEachId(rows[0]);
    if ((groupId && forEachId) ||
      (groupId && definition.groups.filter((group) => group.group_id === groupId).length !== 1) ||
      (forEachId && definition.forEach.filter((item) => item.for_each_id === forEachId).length !== 1)) return null;
  }
  for (const id of participatingGroups) {
    if (definition.groups.filter((group) => group.group_id === id).length !== 1) return null;
  }
  for (const id of participatingForEach) {
    const rows = definition.forEach.filter((item) => item.for_each_id === id);
    if (rows.length !== 1) return null;
    const groupId = rows[0].group_id;
    if (groupId && definition.groups.filter((group) => group.group_id === groupId).length !== 1) return null;
  }
  const roots = parts.filter(({ target }, index) => !parts.some(({ closure }, otherIndex) =>
    otherIndex !== index && closure && covered(closure, target),
  )).map(({ target }) => target).sort((a, b) => kindOrder[a.kind] - kindOrder[b.kind] || a.id.localeCompare(b.id));
  if (!roots.length) return null;
  const steps = new Set<string>();
  const groups = new Set<string>();
  const forEach = new Set<string>();
  const scopes = new Map<string, DeletionScope>();
  for (const { closure } of parts) {
    if (!closure) return null;
    closure.steps.forEach((id) => steps.add(id));
    closure.groups.forEach((id) => groups.add(id));
    closure.forEach.forEach((id) => forEach.add(id));
    closure.scopes.forEach((scope) => scopes.set(`${scope.kind}:${scope.id}`, scope));
  }
  return {
    roots,
    closure: {
      steps: [...steps].sort(compareCanvasIds), groups: [...groups].sort(compareCanvasIds), forEach: [...forEach].sort(compareCanvasIds),
      scopes: [...scopes.values()].sort((a, b) => a.kind.localeCompare(b.kind) || a.id.localeCompare(b.id)),
    },
    operations: roots.map(deletionOperation),
  };
}
