import type { WorkflowCanvasPort } from "@/pages/workflows/shared/api";

/** Only declared signature facts: values, defaults and secret content never enter a card. */
export type ContractSignature = { name: string; type?: string; required?: boolean };
export function declaredSignatures(fields: ReadonlyArray<Record<string, unknown>>): ContractSignature[] {
  return fields.flatMap((field) => typeof field.name === "string" && field.name
    ? [{ name: field.name, ...(typeof field.type === "string" ? { type: field.type } : {}), ...(typeof field.required === "boolean" ? { required: field.required } : {}) }]
    : []);
}
export function schemaSignatures(schema: Record<string, unknown>): ContractSignature[] {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) return [];
  const required = Array.isArray(schema.required) ? schema.required : [];
  return Object.entries(properties).map(([name, value]) => {
    const field = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
    const type = typeof field.type === "string" ? field.type : Array.isArray(field.type) && field.type.every((item) => typeof item === "string") ? field.type.join(" | ") : undefined;
    return { name, ...(type ? { type } : {}), required: required.includes(name) };
  });
}
/** Named outcomes are read top to bottom; the exact same order owns static handle geometry. */
export function orderedStepPorts(kind: string, ports: WorkflowCanvasPort[]): WorkflowCanvasPort[] {
  const routed = ports.filter((port) => port.outcome.name !== "error");
  const order = kind === "if" ? ["true", "false"] : kind === "approval" ? ["approved", "rejected"] : [];
  return order.length ? [...routed].sort((a, b) => order.indexOf(a.outcome.name) - order.indexOf(b.outcome.name)) : routed;
}
export const OUTCOME_ROW_HEIGHT = 32;
export function outcomePortBottom(count: number, index: number): number {
  return 12 + (count - index - 1) * OUTCOME_ROW_HEIGHT + OUTCOME_ROW_HEIGHT / 2;
}

export function hasOutcomeRows(kind: string, count: number): boolean {
  return count > 1 || (count > 0 && ["if", "switch", "approval"].includes(kind));
}
