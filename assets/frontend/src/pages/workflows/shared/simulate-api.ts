import { fetchJson, type FetchJsonOptions } from "@/lib/http/client";

export type WorkflowSimulationResult = {
  workflow_id: string;
  validation?: Record<string, unknown>;
  classification?: Record<string, unknown>;
  steps?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export type WorkflowTestRunResult = {
  status: string;
  job_id?: string | null;
  approval_id?: string | null;
  dry_run?: boolean;
  would_execute_ephemerally?: boolean;
  thread_id?: string | null;
  execution_id?: string | null;
  final_status?: string | null;
  final_report?: string | null;
  error?: string | null;
  needs_user_approval: boolean;
  approval_context?: string | null;
  current_step_id?: string | null;
  current_group_id?: string | null;
  active_steps?: Array<{
    step_id: string;
    step_execution_id?: string;
    attempt?: number;
    group_id?: string | null;
    lane_id?: string | null;
    iteration?: number | null;
    status?: string;
  }>;
  step_id?: string | null;
  step_kind?: string | null;
  trace?: Array<Record<string, unknown>>;
  outputs?: Record<string, unknown>;
  artifacts?: Record<string, unknown>;
  workflow_outputs?: Record<string, unknown>;
  workflow_id: string;
  project_id?: string | null;
  project_name?: string | null;
  task_id?: string | null;
  task_summary?: string | null;
  task_envelope?: Record<string, unknown>;
  mode?: string | null;
  test_mode?: boolean;
  would_queue?: boolean;
  validation?: Record<string, unknown>;
  workflow_terminal?: unknown;
  state?: Record<string, unknown>;
};

export async function simulateWorkflow(workflowId: string, taskEnvelope: Record<string, unknown>) {
  return fetchJson<WorkflowSimulationResult>(`/api/workflows/${encodeURIComponent(workflowId)}/simulate`, {
    method: "POST",
    body: JSON.stringify({ task_envelope: taskEnvelope }),
  });
}

export type StepTestRunResult = {
  status: string;
  job_id?: string | null;
  thread_id?: string | null;
  execution_id?: string | null;
  final_status?: string | null;
  final_report?: string | null;
  summary?: string;
  error?: string | null;
  workflow_id: string;
  step_id: string;
  step_kind: string;
  inputs?: Record<string, unknown>;
  result?: Record<string, unknown>;
  state_patch?: Record<string, unknown>;
  workflow_outputs?: Record<string, unknown>;
  agent?: Record<string, unknown>;
  [key: string]: unknown;
};

export async function testRunWorkflowStep({
  workflowId,
  stepId,
  projectId,
  taskSummary,
  inputs,
  inputBindings,
  expectedRevision,
}: {
  workflowId: string;
  stepId: string;
  projectId: string;
  taskSummary: string;
  inputs?: Record<string, unknown>;
  inputBindings?: Array<Record<string, unknown>>;
  /** The revision the builder shows; the server refuses a run of any other one. */
  expectedRevision?: number | null;
}) {
  return fetchJson<StepTestRunResult>(
    `/api/workflows/${encodeURIComponent(workflowId)}/steps/${encodeURIComponent(stepId)}/test-run`,
    {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        task_summary: taskSummary,
        ...(expectedRevision ? { expected_revision: expectedRevision } : {}),
        task_envelope: {
          title: taskSummary,
          description: taskSummary,
          project_id: projectId,
          requested_workflow_id: workflowId,
          inputs: {
            project_id: projectId,
            ...inputs,
          },
          metadata: {
            source: "step_test_run",
            requested_workflow_id: workflowId,
            step_id: stepId,
            input_bindings: inputBindings || [],
          },
        },
      }),
    },
  );
}

export async function testRunWorkflow({
  workflowId,
  projectId,
  taskSummary,
  dryRun = false,
  inputs,
  expectedRevision,
}: {
  workflowId: string;
  projectId: string;
  taskSummary: string;
  dryRun?: boolean;
  inputs?: Record<string, unknown>;
  /** The revision the builder shows; the server refuses a run of any other one. */
  expectedRevision?: number | null;
}) {
  return fetchJson<WorkflowTestRunResult>(`/api/workflows/${encodeURIComponent(workflowId)}/test-run`, {
    method: "POST",
    body: JSON.stringify({
      project_id: projectId,
      task_summary: taskSummary,
      dry_run: dryRun,
      ...(expectedRevision ? { expected_revision: expectedRevision } : {}),
      task_envelope: {
        title: taskSummary,
        description: taskSummary,
        project_id: projectId,
        requested_workflow_id: workflowId,
        inputs: {
          project_id: projectId,
          ...inputs,
        },
        metadata: {
          source: "workflow_test_run",
          requested_workflow_id: workflowId,
        },
      },
    }),
  });
}

export async function fetchWorkflowTestRunState(threadId: string, options?: FetchJsonOptions) {
  return fetchJson<WorkflowTestRunResult>(
    `/api/workflows/test-runs/${encodeURIComponent(threadId)}`,
    {
      method: "GET",
    },
    options,
  );
}

export async function submitWorkflowTestRunApproval(
  threadId: string,
  payload: {
    approval_id: string;
    approved: boolean;
    comment?: string;
    action?: string;
  },
) {
  return fetchJson<Record<string, unknown>>(`/api/approvals/${encodeURIComponent(threadId)}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
