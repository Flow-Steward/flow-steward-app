import { listFromUnknown, objectOrEmpty, textOrEmpty } from "@/pages/workflows/detail/detail-helpers";

type Row = Record<string, unknown>;

/** The action an Invoke step stands for, or null for a plain Invoke (ADR 0065). */
export function actionInvokeRef(config: unknown): { extensionId: string; installId: string; actionId: string } | null {
  const action = objectOrEmpty(objectOrEmpty(config).action);
  const extensionId = textOrEmpty(action.extension_id);
  const actionId = textOrEmpty(action.action_id);
  if (!extensionId || !actionId) return null;
  return { extensionId, installId: textOrEmpty(action.install_id), actionId };
}

/** The Invoke config a composite action is saved as, or null for an ordinary action. */
export function actionInvokeConfigForOperation(
  operation: unknown,
  extensionId: string,
  installId: string,
  outputMapping: Row = {},
): Row | null {
  const row = objectOrEmpty(operation);
  const implementation = objectOrEmpty(row.implementation);
  const workflowId = textOrEmpty(implementation.workflow_id);
  const actionId = textOrEmpty(row.action_id || row.operation_id);
  if (textOrEmpty(implementation.kind) !== "workflow" || !workflowId || !actionId) return null;
  return {
    workflow_id: workflowId,
    action: { extension_id: extensionId, install_id: installId, action_id: actionId },
    installs: objectOrEmpty(implementation.installs),
    connections: singleChoiceConnections(implementation),
    input_mapping: {},
    output_mapping: outputMapping,
  };
}

/** A slot with exactly one fitting connection is chosen for the person. */
function singleChoiceConnections(implementation: Row): Row {
  const chosen: Row = {};
  for (const slot of listFromUnknown(implementation.slots)) {
    const connections = listFromUnknown(slot.connections);
    const name = textOrEmpty(slot.slot);
    if (name && connections.length === 1) {
      chosen[name] = { connection_id: textOrEmpty(connections[0].connection_id) };
    }
  }
  return chosen;
}
