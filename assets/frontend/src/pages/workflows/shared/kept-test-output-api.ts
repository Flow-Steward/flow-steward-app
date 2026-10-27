import { fetchJson } from "@/lib/http/client";

/** Values a step returns in test runs instead of doing its work; never part of the definition. */
export type KeptTestOutput = {
  step_id: string;
  /** Output target (for example `state.shipment`) → JSON value. */
  outputs: Record<string, unknown>;
  source_kind: "run" | "manual";
  source_job_id: string;
  updated_by?: string;
  updated_at?: string;
};

export type KeptTestOutputsResult = { workflow_id: string; kept_outputs: KeptTestOutput[] };

export const keptTestOutputsQueryKey = (workflowId: string) => ["workflows", "kept-test-outputs", workflowId] as const;

function keptPath(workflowId: string, stepId?: string) {
  const base = `/api/workflows/${encodeURIComponent(workflowId)}/kept-test-outputs`;
  return stepId ? `${base}/${encodeURIComponent(stepId)}` : base;
}

export function fetchKeptTestOutputs(workflowId: string) {
  return fetchJson<KeptTestOutputsResult>(keptPath(workflowId));
}

export function saveKeptTestOutput(workflowId: string, stepId: string, body: { outputs: Record<string, unknown>; source_kind: "run" | "manual"; source_job_id?: string }) {
  return fetchJson<KeptTestOutput>(keptPath(workflowId, stepId), {
    method: "PUT",
    body: JSON.stringify({ outputs: body.outputs, source_kind: body.source_kind, source_job_id: body.source_job_id || "" }),
  });
}

export function deleteKeptTestOutput(workflowId: string, stepId: string) {
  return fetchJson<{ workflow_id: string; step_id: string; removed: boolean }>(keptPath(workflowId, stepId), { method: "DELETE" });
}

/** Kinds whose effect is a decision or control flow always run in tests (backend mirror). */
export const ALWAYS_RUN_STEP_KINDS: ReadonlySet<string> = new Set([
  "if", "switch", "approval", "wait", "stop_success", "stop_fail", "set_variable", "batch", "break", "continue", "collect",
]);

export function stepKindKeepsOutput(kind: string | undefined | null): boolean {
  return !ALWAYS_RUN_STEP_KINDS.has(String(kind || "agent").trim().toLowerCase());
}
