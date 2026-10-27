import type { JobContainerProgressEvidence } from "./job-container-progress";
import type { JobExecutionTraceEvidence } from "./job-execution-trace";
import type { WorkflowContractSummary } from "@/pages/workflows/shared/api";
import { fetchJson } from "@/lib/http/client";
import { isHttpError } from "@/lib/http/errors";

export type JobDiagnosticsTab = {
  count?: number;
  rows?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export type JobDiagnosticsPayload = {
  facts?: Array<Record<string, unknown>>;
  root_cause?: Record<string, unknown>;
  tabs?: Record<string, JobDiagnosticsTab>;
  raw_refs?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export type JobLifecyclePayload = {
  job_id?: string;
  status?: Record<string, unknown>;
  step?: Record<string, unknown>;
  steps?: Array<Record<string, unknown>>;
  runtime?: JobDiagnosticsTab;
  errors?: JobDiagnosticsTab;
  costs?: Record<string, unknown>;
  approval?: Record<string, unknown>;
  root_cause?: Record<string, unknown>;
  [key: string]: unknown;
};

export type JobDetailSectionsPayload = {
  execution_steps?: Array<Record<string, unknown>>;
  execution_steps_page?: JobExecutionStepsPageMeta;
  selected_step?: Record<string, unknown>;
  review_workspace?: Record<string, unknown>;
  previous_decisions?: Array<Record<string, unknown>>;
  cost_breakdown?: Record<string, unknown>;
  diagnostics?: JobDiagnosticsPayload;
  [key: string]: unknown;
};

export type JobExecutionStepsPageMeta = {
  count?: number;
  total_durable_steps?: number;
  has_more?: boolean;
  next_cursor?: string;
};

export type JobExecutionStepsPage = {
  job_id: string;
  steps: Array<Record<string, unknown>>;
  page: JobExecutionStepsPageMeta;
  /** The workflow run's error: code, message and the failing step. */
  run_error?: { code?: string; message?: string; step_id?: string | null; step_execution_id?: string | null } | null;
  run_terminal?: boolean;
  /** Versioned checkpoint trace, present only on the first page; validate before use. */
  run_trace?: unknown;
  /** Optional committed aggregate; validated against verified project and archive before use. */
  container_progress?: unknown;
  child_invocations?: JobChildInvocation[];
  child_invocations_truncated?: boolean;
};

export type JobChildInvocation = {
  step_id: string;
  step_execution_id: string;
  invocation_key: string;
  child_job_id: string;
  child_workflow_id: string;
};

/** Archived execution evidence for a canvas observer; never a live-definition guess. */
export type JobExecutionSnapshot = {
  jobId: string;
  definitionWorkflowId: string;
  definitionRevision: number;
  definitionDigest?: string;
  definitionSnapshotId?: string;
  runTrace?: JobExecutionTraceEvidence;
  containerProgress?: JobContainerProgressEvidence;
  definitionSteps: Array<{ stepId: string; hookId?: string; label?: string }>;
  runError?: { code?: string; message?: string; stepId?: string; stepExecutionId?: string };
  steps: Array<{
    stepId: string;
    stepExecutionId?: string;
    occurrence?: number;
    groupId?: string;
    forEachId?: string;
    laneId?: string;
    iteration?: number;
    attempt?: number;
    status: string;
    startedAt?: string;
    finishedAt?: string;
    durationMs?: number;
    /** A workflow test run returned the step's kept test output instead of running it. */
    kept?: boolean;
  }>;
  childInvocations: Array<{
    stepId: string;
    stepExecutionId: string;
    invocationKey: string;
    childJobId: string;
    childWorkflowId: string;
  }>;
  terminal: boolean;
  /** False while pages remain, or when a bounded projection is truncated. */
  complete: boolean;
};

export type JobDetailProjectionPayload = {
  header?: Record<string, unknown>;
  hero_summary?: Record<string, unknown>;
  next_action?: Record<string, unknown>;
  job_actions?: Record<string, unknown>;
  user_role?: string;
  [key: string]: unknown;
};

export type JobDetailJobPayload = {
  id?: string | number;
  status?: string;
  project_id?: string | null;
  thread_id?: string | null;
  task_summary?: string;
  display_summary?: string;
  selected_workflow?: string | null;
  selected_workflow_display_name?: string | null;
  selected_workflow_source?: string | null;
  workflow_selection_reason?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  current_step_id?: string | null;
  current_group_id?: string | null;
  current_agent?: string | null;
  failure_summary?: JobFailureSummary;
  invocation?: Record<string, unknown>;
  [key: string]: unknown;
};

export type JobDetail = {
  workflow_result?: { outputs: Record<string, unknown>; artifacts: Record<string, unknown> } | null;
  id: string;
  status: string;
  task_summary?: string;
  display_summary?: string;
  selected_workflow?: string | null;
  selected_workflow_display_name?: string | null;
  selected_workflow_source?: string | null;
  workflow_selection_reason?: string | null;
  failure_summary?: JobFailureSummary;
  invocation?: Record<string, unknown>;
  started_at?: string;
  finished_at?: string;
  elapsed_seconds?: number;
  hero_summary?: Record<string, unknown>;
  next_action?: Record<string, unknown>;
  review_workspace?: Record<string, unknown>;
  execution_steps?: Array<Record<string, unknown>>;
  previous_decisions?: Array<Record<string, unknown>>;
  cost_breakdown?: Record<string, unknown>;
  diagnostics?: JobDiagnosticsPayload;
  lifecycle?: JobLifecyclePayload;
  sections?: JobDetailSectionsPayload;
  projection?: JobDetailProjectionPayload;
  job?: JobDetailJobPayload;
  runtime_journal?: Array<Record<string, unknown>>;
  job_errors?: Array<Record<string, unknown>>;
  approval_gate?: Record<string, unknown>;
  approval_history?: Array<Record<string, unknown>>;
  incident?: Record<string, unknown>;
  [key: string]: unknown;
};

export type JobFailureSummary = {
  present?: boolean;
  kind?: string;
  status?: string;
  title?: string;
  summary?: string;
  detail?: string;
  severity?: string;
  retryable?: boolean | null;
  source?: string | null;
  category?: string | null;
  root_cause?: string;
  next_action?: string;
  remediation?: { summary?: string; steps?: string[] };
  evidence_refs?: string[];
  related_workflows?: Array<Record<string, unknown>>;
  raw_diagnostics_available?: boolean;
  diagnostic_ids?: Record<string, string>;
  evidence?: Array<Record<string, unknown>>;
};

export type JobIncidentDrilldown = {
  job_id: string;
  project_id?: string | null;
  status?: string;
  root_cause?: Record<string, unknown>;
  failure_context?: Record<string, unknown>;
  related_events?: Array<Record<string, unknown>>;
  related_dispatches?: Array<Record<string, unknown>>;
  related_attempts?: Array<Record<string, unknown>>;
};

export type JobDecisionOverridesPayload = {
  job_id: string;
  overrides: Array<Record<string, unknown>>;
};

/** The definition a run executed, from the snapshot archive (never the live workflow). */
export type JobDefinitionPayload = {
  job_id: string;
  available: boolean;
  unavailable_reason?: string | null;
  snapshot_id?: string | null;
  workflow_id?: string | null;
  revision?: number | null;
  digest?: string | null;
  schema_version?: number | null;
  steps?: Array<Record<string, unknown>>;
  groups?: Array<Record<string, unknown>>;
  for_each?: Array<Record<string, unknown>>;
  execution_graph?: Record<string, unknown> | null;
  contract_summary?: WorkflowContractSummary | null;
  /** A protected author graph: the real structure, sensitive values redacted. */
  protected?: boolean;
};

export type JobCostBreakdownPayload = {
  breakdown: Array<Record<string, unknown>>;
  total_cost_usd?: number;
  known_cost_usd?: number;
  unknown_cli_tokens?: number;
  cost_knownness?: string;
  total_cost_display?: string;
};

export type JobApprovalPayload = {
  job_id: string;
  thread_id?: string;
  task_summary?: string;
  project_id?: string | null;
  payload?: Record<string, unknown>;
  review_workspace?: Record<string, unknown>;
  [key: string]: unknown;
};

export type JobApprovalHistoryPayload = {
  job_id: string;
  approvals: Array<Record<string, unknown>>;
};

export type ApprovalDecisionPayload = {
  approval_id: string;
  approved?: boolean;
  comment?: string;
  action?: string;
  override_content?: string;
};

export async function fetchJobDetail(jobId: string) {
  const params = new URLSearchParams({ compact: "true", step_limit: "0" });
  return fetchJson<JobDetail>(`/api/v2/jobs/${encodeURIComponent(jobId)}?${params.toString()}`, {
    cache: "no-store",
  });
}

export async function fetchJobExecutionStepsPage(jobId: string, cursor: string) {
  const params = new URLSearchParams({ limit: "50", cursor });
  return fetchJson<JobExecutionStepsPage>(
    `/api/v2/jobs/${encodeURIComponent(jobId)}/execution-steps?${params.toString()}`,
    { cache: "no-store" },
  );
}

/** The full value of one step output that Job Details shows cut short. */
export async function fetchJobStepOutputValue(jobId: string, messageId: string, field: string) {
  const params = new URLSearchParams({ field });
  return fetchJson<{ field: string; value: unknown }>(
    `/api/jobs/${encodeURIComponent(jobId)}/messages/${encodeURIComponent(messageId)}/output-value?${params.toString()}`,
  );
}

export async function fetchJobIncident(jobId: string) {
  return fetchJson<JobIncidentDrilldown>(`/api/jobs/${encodeURIComponent(jobId)}/incident`);
}

export async function fetchJobDecisionOverrides(jobId: string) {
  return fetchJson<JobDecisionOverridesPayload>(`/api/jobs/${encodeURIComponent(jobId)}/decision-overrides`);
}

export async function fetchJobDefinition(jobId: string) {
  return fetchJson<JobDefinitionPayload>(`/api/jobs/${encodeURIComponent(jobId)}/definition`);
}

export async function fetchJobCostBreakdown(jobId: string) {
  return fetchJson<JobCostBreakdownPayload>(`/api/jobs/${encodeURIComponent(jobId)}/cost-breakdown`);
}

export async function fetchJobApproval(jobId: string) {
  try {
    return await fetchJson<JobApprovalPayload>(`/api/jobs/${encodeURIComponent(jobId)}/approval`);
  } catch (error) {
    if (isHttpError(error) && error.status === 409 && error.payload && typeof error.payload === "object") {
      const code = (error.payload as Record<string, unknown>).code;
      if (code === "approval_not_awaiting") {
        return undefined;
      }
    }
    throw error;
  }
}

export async function fetchJobApprovalHistory(jobId: string) {
  return fetchJson<JobApprovalHistoryPayload>(`/api/jobs/${encodeURIComponent(jobId)}/approvals`);
}

export async function submitApprovalDecision(threadId: string, payload: ApprovalDecisionPayload) {
  return fetchJson<Record<string, unknown>>(`/api/approvals/${encodeURIComponent(threadId)}`, {
    body: JSON.stringify(payload),
    method: "POST",
  });
}

export async function cancelJob(jobId: string) {
  return fetchJson<Record<string, unknown>>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
  });
}

/** Marks a failed or stuck run as triaged; its status does not change. */
export async function acknowledgeJob(jobId: string) {
  return fetchJson<Record<string, unknown>>(`/api/jobs/${encodeURIComponent(jobId)}/acknowledge`, {
    method: "POST",
  });
}

export async function unacknowledgeJob(jobId: string) {
  return fetchJson<Record<string, unknown>>(`/api/jobs/${encodeURIComponent(jobId)}/unacknowledge`, {
    method: "POST",
  });
}

export async function restartJob(jobId: string) {
  return fetchJson<Record<string, unknown>>(`/api/jobs/${encodeURIComponent(jobId)}/restart`, {
    method: "POST",
  });
}
