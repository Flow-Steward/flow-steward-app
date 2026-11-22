import { isHttpError } from "@/lib/http/errors";

/** A refused run as the dialog shows it: under a field when the server names one. */
export type RunErrorView = { field: string; message: string; reasons: string[] };

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/** A key's value in the payload, its `detail` or its envelope `error`/`details`. */
function payloadValue(payload: unknown, key: string): unknown {
  const top = record(payload);
  const candidates = [top, record(top.detail), record(top.error), record(record(top.error).details), record(top.payload)];
  for (const candidate of candidates) {
    if (candidate[key] !== undefined) return candidate[key];
  }
  return undefined;
}

function reasonText(reason: unknown): string {
  if (typeof reason === "string") return reason;
  const row = record(reason);
  return typeof row.message === "string" ? row.message : typeof row.code === "string" ? row.code : "";
}

/** The input a request-validation error points at: `["body", "inputs", "amount", …]`. */
function locatedField(payload: unknown, names: ReadonlySet<string>): string {
  const detail = payloadValue(payload, "detail") ?? record(payload).detail;
  for (const row of Array.isArray(detail) ? detail : []) {
    const loc = record(row).loc;
    if (!Array.isArray(loc)) continue;
    const at = loc.indexOf("inputs");
    const name = at >= 0 ? String(loc[at + 1] ?? "") : "";
    if (names.has(name)) return name;
  }
  return "";
}

/**
 * Where a refused run's message belongs (ADR 0075): next to the field when the
 * server names an input (`input_name`, a request-validation location, or
 * `'name'` in the message), otherwise in the dialog; an operator refusal
 * carries its reasons.
 */
export function describeRunError(error: unknown, fieldNames: readonly string[], fallback: string): RunErrorView {
  const message = error instanceof Error && error.message ? error.message : fallback;
  if (!isHttpError(error)) return { field: "", message, reasons: [] };
  const names = new Set(fieldNames);
  const inputName = payloadValue(error.payload, "input_name");
  const reasonsRaw = payloadValue(error.payload, "reasons");
  const reasons = (Array.isArray(reasonsRaw) ? reasonsRaw : []).map(reasonText).filter(Boolean);
  let field = typeof inputName === "string" && names.has(inputName) ? inputName : "";
  if (!field) field = locatedField(error.payload, names);
  if (!field) field = fieldNames.find((name) => message.includes(`'${name}'`)) ?? "";
  return { field, message, reasons };
}
