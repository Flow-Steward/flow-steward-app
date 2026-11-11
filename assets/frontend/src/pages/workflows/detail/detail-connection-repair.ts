/**
 * Moving a workflow off a deleted connection in one save.
 *
 * Steps name a connection by id. When the connection is deleted and a new one is
 * created, every step that named the old id is broken, and an enabled workflow
 * refuses a change that leaves any blocking issue (ADR 0035), so fixing them one
 * step at a time is refused until the last one. The step editor saves the step the
 * author repaired together with every other step on the same deleted connection.
 */
import { objectOrEmpty, textOrEmpty } from "@/pages/workflows/detail/detail-helpers";

type StepLike = Record<string, unknown>;
export type ConnectionRepairOperation = { op: "update_step_config"; step_id: string; patch: Record<string, unknown> };

/** The binding a step runs with: its own column first, as the runtime reads it. */
export function stepConnectionBinding(step: StepLike): Record<string, unknown> {
  const own = objectOrEmpty(step.connection_binding_jsonb);
  if (Object.keys(own).length) return own;
  const connector = objectOrEmpty(objectOrEmpty(step.override).connector);
  return objectOrEmpty(connector.connection_binding);
}

export function stepConnectionId(step: StepLike): string {
  return textOrEmpty(stepConnectionBinding(step).connection_id);
}

/**
 * The other steps' moves from *previousId* to *nextId*: only when the step being
 * saved leaves a connection that no longer exists (it is not among *availableIds*)
 * for another one. A connection that still exists is a deliberate per-step choice
 * and moves nothing else.
 */
export function connectionRepairOperations({
  steps, savedStepId, previousId, nextId, availableIds, isTouched,
}: Readonly<{
  steps: readonly StepLike[];
  savedStepId: string;
  previousId: string;
  nextId: string;
  availableIds: ReadonlySet<string>;
  isTouched?: (stepId: string) => boolean;
}>): ConnectionRepairOperation[] {
  // The new connection must be one the loaded list shows: an unloaded list proves nothing deleted.
  if (!previousId || !nextId || previousId === nextId || !availableIds.has(nextId) || availableIds.has(previousId)) return [];
  return steps.flatMap((step) => {
    const stepId = textOrEmpty(step.step_id);
    if (!stepId || stepId === savedStepId || isTouched?.(stepId)) return [];
    if (stepConnectionId(step) !== previousId) return [];
    return [{
      op: "update_step_config" as const,
      step_id: stepId,
      patch: { connection_binding_jsonb: { ...stepConnectionBinding(step), connection_id: nextId } },
    }];
  });
}
