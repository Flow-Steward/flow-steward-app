import type { GraphDeleteSelection, GraphDeleteSource } from "@/pages/workflows/builder/canvas-types";
import { deriveDeletionClosure, deletionOperation, type DeletionTarget, type DeletionClosure, type DeletionScope } from "@/pages/workflows/builder/canvas-delete-guard";
import { deriveDeletionBatch, type DeletionBatch } from "@/pages/workflows/builder/canvas-delete-batch";
import type { WorkflowDefinitionModel } from "@/pages/workflows/builder/change-set";
import type { DeleteGuardBatchCommandContext, DeleteGuardCommandContext, DeleteGuardSnapshot, useWorkflowDeletionGuard } from "@/pages/workflows/detail/detail-canvas-handlers";
import type { I18nContextValue } from "@/lib/i18n/context";

/** This reader must return null unless the same canvas item is still selected
 * in the same author or active Hook-child owner. Its generation changes on
 * every selection, child, or owner transition, including close/reopen. */
export type GraphDeleteAuthority = DeleteGuardSnapshot & {
  target: DeletionTarget;
  canvasId: string;
  generation: number;
  localStep: boolean;
};

/** One captured selection in exactly one author or active Hook instance. */
export type GraphDeleteBatchAuthority = DeleteGuardSnapshot & {
  targets: readonly DeletionTarget[];
  canvasIds: readonly string[];
  generation: number;
  ownerScopeKey: string;
};

type Dependencies = {
  readAuthority: (selection: GraphDeleteSelection) => GraphDeleteAuthority | null;
  guard: Pick<ReturnType<typeof useWorkflowDeletionGuard>, "requestDelete">;
  openProtectedScope: (authority: GraphDeleteAuthority, scope: DeletionScope) => Promise<boolean>;
  onAccepted: (selection: GraphDeleteSelection, authority: GraphDeleteAuthority, closure: DeletionClosure) => void;
  t: I18nContextValue["t"];
};

function sameAuthority(expected: GraphDeleteAuthority, current: GraphDeleteAuthority | null): current is GraphDeleteAuthority {
  return Boolean(current?.canvasId === expected.canvasId && current.generation === expected.generation &&
    current.target.kind === expected.target.kind && current.target.id === expected.target.id &&
    current.localStep === expected.localStep &&
    current.owner.ownerKey === expected.owner.ownerKey &&
    current.owner.selectionId === expected.target.id &&
    current.owner.workflowId === expected.owner.workflowId &&
    current.owner.projectId === expected.owner.projectId &&
    current.owner.serverRevision === expected.owner.serverRevision &&
    !current.owner.structureLocked &&
    current.changeSet.operations === expected.changeSet.operations &&
    current.changeSet.preview === expected.changeSet.preview &&
    current.changeSet.baseRevision === expected.changeSet.baseRevision &&
    current.changeSet.status === expected.changeSet.status &&
    current.changeSet.status !== "applying" && current.changeSet.status !== "conflict");
}

/** One single-node Delete entrance. Task 4 will add bulk closure semantics. */
export async function requestGraphDelete(
  selection: readonly GraphDeleteSelection[],
  _source: GraphDeleteSource,
  deps: Dependencies,
): Promise<boolean> {
  if (selection.length !== 1) return false;
  const selected = selection[0];
  const captured = deps.readAuthority(selected);
  if (!captured || captured.owner.structureLocked || captured.owner.selectionId !== captured.target.id) return false;
  const closure = deriveDeletionClosure(captured.changeSet.preview as WorkflowDefinitionModel, captured.target);
  if (!closure) return false;
  const current = () => {
    const latest = deps.readAuthority(selected);
    return sameAuthority(captured, latest) ? latest : null;
  };
  // Delete acts at once; the canvas toast names what went and offers Undo (Components › Step actions).
  if (!current()) return false;
  if (captured.localStep && captured.target.kind === "step") {
    captured.changeSet.stageGraphIntent(
      [deletionOperation(captured.target)],
      deps.t("react.workflows.step_delete_staged", "Step deletion added to the unapplied structure."),
    );
    deps.onAccepted(selected, captured, closure);
    return true;
  }
  const context: DeleteGuardCommandContext = {
    captured: { changeSet: captured.changeSet, owner: captured.owner },
    current: (): DeleteGuardSnapshot | null => {
      const latest = current();
      return latest ? { changeSet: latest.changeSet, owner: latest.owner } : null;
    },
    openProtectedScope: (scope) => current() ? deps.openProtectedScope(captured, scope) : Promise.resolve(false),
  };
  return deps.guard.requestDelete(captured.target, () => {
    if (!current()) return;
    const notice = captured.target.kind === "step"
      ? deps.t("react.workflows.step_delete_staged", "Step deletion added to the unapplied structure.")
      : captured.target.kind === "for_each"
        ? deps.t("react.workflows.for_each_delete_staged", "For each deletion added to the unapplied structure.")
        : deps.t("react.workflows.group_delete_staged", "Group deletion added to the unapplied structure.");
    captured.changeSet.stageGraphIntent([deletionOperation(captured.target)], notice);
    deps.onAccepted(selected, captured, closure);
  }, context);
}

/** A multi-root command uses one combined candidate and one guard, then stages
 * the accepted operations as a single history intent. */
export async function requestGraphDeleteBatch(
  selection: readonly GraphDeleteSelection[],
  deps: {
    readAuthority: (items: readonly GraphDeleteSelection[]) => GraphDeleteBatchAuthority | null;
    guard: Pick<ReturnType<typeof useWorkflowDeletionGuard>, "requestDeleteBatch">;
    openProtectedScope: (authority: GraphDeleteBatchAuthority, scope: DeletionScope) => Promise<boolean>;
    onAccepted: (authority: GraphDeleteBatchAuthority, batch: DeletionBatch) => void;
    t: I18nContextValue["t"];
  },
): Promise<boolean> {
  if (selection.length < 2) return false;
  const captured = deps.readAuthority(selection);
  if (!captured || captured.owner.structureLocked || captured.canvasIds.length !== selection.length ||
    captured.targets.length !== selection.length) return false;
  const batch = deriveDeletionBatch(captured.changeSet.preview as WorkflowDefinitionModel, captured.targets);
  if (!batch) return false;
  const current = () => {
    const latest = deps.readAuthority(selection);
    return latest?.owner.ownerKey === captured.owner.ownerKey &&
      latest.owner.selectionId === captured.owner.selectionId &&
      latest.owner.workflowId === captured.owner.workflowId &&
      latest.owner.projectId === captured.owner.projectId &&
      latest.owner.serverRevision === captured.owner.serverRevision && !latest.owner.structureLocked &&
      latest.generation === captured.generation && latest.ownerScopeKey === captured.ownerScopeKey &&
      latest.canvasIds.length === captured.canvasIds.length && latest.canvasIds.every((id, index) => id === captured.canvasIds[index]) &&
      latest.targets.length === captured.targets.length && latest.targets.every((target, index) =>
        target.kind === captured.targets[index].kind && target.id === captured.targets[index].id) &&
      latest.changeSet.operations === captured.changeSet.operations &&
      latest.changeSet.preview === captured.changeSet.preview &&
      latest.changeSet.baseRevision === captured.changeSet.baseRevision &&
      latest.changeSet.status === captured.changeSet.status &&
      latest.changeSet.status !== "applying" && latest.changeSet.status !== "conflict" ? latest : null;
  };
  // One change, at once, with Undo in the canvas toast; no confirmation dialog.
  if (!current()) return false;
  const context: DeleteGuardBatchCommandContext = {
    captured: { changeSet: captured.changeSet, owner: captured.owner },
    selection: { canvasIds: captured.canvasIds, generation: captured.generation, ownerScopeKey: captured.ownerScopeKey },
    current: () => {
      const latest = current();
      return latest ? { changeSet: latest.changeSet, owner: latest.owner } : null;
    },
    currentSelection: () => {
      const latest = current();
      return latest ? { canvasIds: latest.canvasIds, generation: latest.generation, ownerScopeKey: latest.ownerScopeKey } : null;
    },
    openProtectedScope: (scope) => current() ? deps.openProtectedScope(captured, scope) : Promise.resolve(false),
  };
  return deps.guard.requestDeleteBatch(captured.targets, (accepted) => {
    if (!current()) return;
    captured.changeSet.stageGraphIntent(accepted.operations,
      deps.t("react.workflows.selected_delete_staged", "Selected items deletion added to the unapplied structure."));
    deps.onAccepted(captured, accepted);
  }, context);
}
