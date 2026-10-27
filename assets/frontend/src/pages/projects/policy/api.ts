import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type ProjectPolicyPayload = Record<string, unknown>;

// This mirrors the public ProjectPolicyRequest contract. Keep policy controls
// bound to this list so the UI cannot submit values that the API rejects.
export const PROJECT_POLICY_AUTONOMY_MODES = ["manual", "supervised", "auto"] as const;

type PolicyUpdatePayload = {
  autonomy_mode: (typeof PROJECT_POLICY_AUTONOMY_MODES)[number];
  autonomy_confidence_threshold: number;
  retry_strategy: string;
  retry_max_attempts: number;
  retry_backoff_initial_seconds: number;
  retry_backoff_max_seconds: number;
  concurrency_cap: number;
  tool_policy_mode: string;
};

function toNumber(value: unknown, fallback: number) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function toAutonomyMode(value: unknown) {
  const autonomyMode = stringFromUnknownValue(value).trim();
  return PROJECT_POLICY_AUTONOMY_MODES.includes(
    autonomyMode as (typeof PROJECT_POLICY_AUTONOMY_MODES)[number],
  )
    ? (autonomyMode as (typeof PROJECT_POLICY_AUTONOMY_MODES)[number])
    : "supervised";
}

export function normalizeProjectPolicySavePayload(payload: ProjectPolicyPayload): PolicyUpdatePayload {
  const policy =
    payload.policy && typeof payload.policy === "object"
      ? (payload.policy as Record<string, unknown>)
      : payload;

  return {
    autonomy_mode: toAutonomyMode(policy.autonomy_mode),
    autonomy_confidence_threshold: toNumber(policy.autonomy_confidence_threshold, 0.8),
    retry_strategy: firstStringFromUnknownValue([policy.retry_strategy, "bounded"]),
    retry_max_attempts: toNumber(policy.retry_max_attempts, 2),
    retry_backoff_initial_seconds: toNumber(policy.retry_backoff_initial_seconds, 2),
    retry_backoff_max_seconds: toNumber(policy.retry_backoff_max_seconds, 120),
    concurrency_cap: toNumber(policy.concurrency_cap, 1),
    tool_policy_mode: firstStringFromUnknownValue([policy.tool_policy_mode, "registry_only"]),
  };
}

export async function fetchProjectPolicy(projectId: string) {
  return fetchJson<ProjectPolicyPayload>(`/api/projects/${encodeURIComponent(projectId)}/policy`);
}

export async function saveProjectPolicy(projectId: string, payload: ProjectPolicyPayload) {
  return fetchJson<ProjectPolicyPayload>(`/api/projects/${encodeURIComponent(projectId)}/policy`, {
    method: "PUT",
    body: JSON.stringify(normalizeProjectPolicySavePayload(payload)),
  });
}
