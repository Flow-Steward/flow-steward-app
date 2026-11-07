import { connectorAllowsNull, connectorLiteralError } from "./detail-connector-literal";
import {
  connectorInputMappingRows,
  contractItemName,
  isPlatformOwnedConnectorInput,
} from "./detail-step-editing";
import { objectOrEmpty, type ConnectorInputMappingRow } from "@/pages/workflows/detail/detail-interface";

export type ConnectorInputCarry = {
  /** Mapped inputs the next action declares too, with their stored values unchanged. */
  kept: Record<string, unknown>;
  /** Inputs that had a value but cannot be kept, in their previous order. */
  dropped: string[];
};

function hasValue(row: ConnectorInputMappingRow): boolean {
  if (row.originalTypedEmpty) return false;
  if (row.mode === "null") return true;
  if (row.mode === "literal" && row.originalTextLiteral) return true;
  const text = row.value.trim();
  return Boolean(text) && (row.mode !== "state" || text !== "state.");
}

function compatible(row: { mode: string; value: string }, parameter: Record<string, unknown>): boolean {
  if (row.mode === "null") return connectorAllowsNull(parameter);
  if (row.mode === "literal") return !connectorLiteralError(parameter, row.value);
  // A state path or a raw string is resolved at run time, as it was for the previous action.
  return true;
}

/**
 * The input mapping a step keeps when its action changes.
 *
 * A mapped input survives only when the next action declares an input of the same name
 * and its value is valid for that input (a literal must pass the input's type and limits).
 * Its stored value is kept exactly, so nothing about how it runs changes. Inputs the next
 * action does not declare, or whose literal no longer fits, are not stored; the ones that
 * had a value are returned as `dropped` so the editor can say what was removed. Empty rows
 * go without notice. With no next action (a new action source) nothing is kept.
 */
export function carryConnectorInputMapping(
  previous: unknown,
  nextParameters: Array<Record<string, unknown>>,
): ConnectorInputCarry {
  // Entries, then Object.fromEntries: every name (including "__proto__" or
  // "constructor") becomes an own data property, and no prototype is ever set.
  const keptEntries: Array<[string, unknown]> = [];
  const dropped: string[] = [];
  Object.entries(objectOrEmpty(previous)).forEach(([key, value]) => {
    if (!key || isPlatformOwnedConnectorInput(key)) return;
    const parameter = nextParameters.find((candidate) => contractItemName(candidate) === key);
    const [row] = connectorInputMappingRows(Object.fromEntries([[key, value]]), parameter ? [parameter] : []);
    if (!row || row.key !== key) return;
    if (!hasValue(row)) return;
    if (parameter && compatible(row, parameter)) keptEntries.push([key, value]);
    else dropped.push(key);
  });
  return { kept: Object.fromEntries(keptEntries), dropped };
}
