export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export type ApiEnvelopeKind = "list" | "detail" | "mutation";

export type ApiEnvelopeError = {
  code?: string;
  message?: string;
  status?: number;
  details?: unknown;
};

export type ApiEnvelope<TData> = {
  ok: boolean;
  kind: ApiEnvelopeKind;
  data: TData | null;
  error: ApiEnvelopeError | null;
  meta?: Record<string, unknown>;
};

export type ApiErrorPayload = {
  error?: string;
  message?: string;
  code?: string;
  status?: number;
  details?: unknown;
  [key: string]: unknown;
};

export function isApiEnvelope(value: unknown): value is ApiEnvelope<unknown> {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  const kind = candidate.kind;
  return (
    typeof candidate.ok === "boolean" &&
    (kind === "list" || kind === "detail" || kind === "mutation") &&
    "data" in candidate &&
    "error" in candidate
  );
}
