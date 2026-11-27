import { fetchJson } from "@/lib/http/client";

/** A person who may be assigned to an Approval step. */
export type ApproverCandidate = { user_id: string; username: string; display_name: string };

export type StepApprovers = { approvers: ApproverCandidate[]; restricted: boolean };

function stepPath(base: string, workflowId: string, stepId: string): string {
  return `${base}/${encodeURIComponent(workflowId)}/${encodeURIComponent(stepId)}/approvers`;
}

export const approverQueryKeys = {
  candidates: (base: string, workflowId: string) => ["approval-assignees", base, "candidates", workflowId] as const,
  step: (base: string, workflowId: string, stepId: string) => ["approval-assignees", base, "step", workflowId, stepId] as const,
};

/** People who may decide this workflow's approvals in every project it runs in. */
export async function fetchApproverCandidates(base: string, workflowId: string): Promise<ApproverCandidate[]> {
  const params = new URLSearchParams({ workflow_id: workflowId });
  const response = await fetchJson<{ users?: ApproverCandidate[] }>(`${base}/candidates?${params.toString()}`, { method: "GET" });
  return Array.isArray(response?.users) ? response.users : [];
}

function stepApprovers(response: Partial<StepApprovers> | null | undefined): StepApprovers {
  return {
    approvers: Array.isArray(response?.approvers) ? response.approvers : [],
    restricted: Boolean(response?.restricted),
  };
}

export async function fetchStepApprovers(base: string, workflowId: string, stepId: string): Promise<StepApprovers> {
  return stepApprovers(await fetchJson<Partial<StepApprovers>>(stepPath(base, workflowId, stepId), { method: "GET" }));
}

export async function saveStepApprovers(base: string, workflowId: string, stepId: string, userIds: readonly string[]): Promise<StepApprovers> {
  return stepApprovers(await fetchJson<Partial<StepApprovers>>(stepPath(base, workflowId, stepId), {
    method: "PUT",
    body: JSON.stringify({ user_ids: userIds }),
  }));
}
