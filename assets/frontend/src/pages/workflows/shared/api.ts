import { fetchJson, type FetchJsonOptions } from "@/lib/http/client";
import type { TranslateParams } from "@/lib/i18n/context";

export {
  clearWorkflowInputSampleFile,
  clearWorkflowInputSecret,
  setWorkflowInputSampleFile,
  setWorkflowInputSecret,
} from "./workflow-input-api";

/** Safe names-only contract projection from an authorized pinned definition. */
export type WorkflowContractSummary = {
  input_mode: string;
  output_mode: string;
  inputs: Array<{ name: string; required: boolean }>;
  outputs: string[];
  /** Returned files: keys of step results ticked "Return from workflow". */
  artifacts: string[];
  trigger_channels: string[];
};

export type WorkflowListItem = {
  workflow_id: string;
  display_name?: string;
  description?: string;
  enabled?: boolean;
  portability?: WorkflowExportPolicy;
  updated_at?: string;
  [key: string]: unknown;
};

export type WorkflowNodeRef = {
  kind: "step" | "group" | "for_each";
  id: string;
};

export type WorkflowGroupExitRef = {
  kind: "group_exit";
  group_id: string;
  lane_id: string | null;
};

export type WorkflowForEachExitRef = {
  kind: "for_each_exit";
  for_each_id: string;
  lane_id: string | null;
};

export type WorkflowExitRef = WorkflowGroupExitRef | WorkflowForEachExitRef;

export type WorkflowTargetRef = WorkflowNodeRef | WorkflowExitRef;

export type WorkflowOutcomeName =
  | "success"
  | "true"
  | "false"
  | "case"
  | "default"
  | "approved"
  | "rejected"
  | "error";

export type WorkflowOutcome = {
  name: WorkflowOutcomeName;
  case_id?: string;
};

export type WorkflowGraphEdge = {
  source: WorkflowNodeRef;
  outcome: WorkflowOutcome;
  target: WorkflowTargetRef;
  rework?: boolean;
  rework_limit?: number;
};

export type WorkflowExecutionGraph = {
  schema_version: number;
  entry: WorkflowNodeRef | null;
  edges: WorkflowGraphEdge[];
};

export type WorkflowStep = {
  step_id?: string;
  step_kind?: string;
  display_name?: string;
  group_id?: string | null;
  for_each_id?: string | null;
  lane_id?: string | null;
  sort_order?: number;
  participant_key?: string;
  agent_id?: string;
  optional?: boolean;
  /** The run skips a disabled step and goes on along its Next outcome. */
  disabled?: boolean;
  override?: Record<string, unknown>;
  error_policy?: Record<string, unknown> | null;
  boundary_contract?: Record<string, unknown> | null;
  [key: string]: unknown;
};

export type WorkflowGroupLane = {
  lane_id: string;
  entry?: WorkflowNodeRef | null;
  [key: string]: unknown;
};

export type WorkflowGroupBody =
  | { kind: "sequence"; entry?: WorkflowNodeRef | null }
  | { kind: "lanes"; lanes: WorkflowGroupLane[]; concurrency_limit?: number };

/** A Group: its body runs once, as a sequence or as parallel lanes. */
export type WorkflowGroup = {
  group_id: string;
  display_name?: string;
  description?: string;
  body?: WorkflowGroupBody | Record<string, unknown>;
  error_policy?: Record<string, unknown> | null;
  boundary_contract?: Record<string, unknown> | null;
  [key: string]: unknown;
};

/** A For each: its body runs once per item of a ready collection, one item at a time. */
export type WorkflowForEach = {
  for_each_id: string;
  /** The sequence Group it sits in, or null at the root. */
  group_id?: string | null;
  display_name?: string;
  description?: string;
  over: string;
  item_name: string;
  index_name?: string | null;
  body?: WorkflowGroupBody | Record<string, unknown>;
  error_policy?: Record<string, unknown> | null;
  boundary_contract?: Record<string, unknown> | null;
  [key: string]: unknown;
};

export type WorkflowDetail = {
  workflow_id: string;
  revision?: number;
  display_name?: string;
  description?: string;
  enabled?: boolean;
  portability?: WorkflowExportPolicy;
  protected_workflow?: boolean;
  settings?: Record<string, unknown>;
  steps?: WorkflowStep[];
  groups?: WorkflowGroup[];
  for_each?: WorkflowForEach[];
  [key: string]: unknown;
};

export type WorkflowDagNode = {
  id: string;
  type?: "step" | "group";
  step_id?: string;
  group_id?: string | null;
  lane_id?: string | null;
  display_name?: string;
  step_kind?: string;
  group_kind?: string;
  [key: string]: unknown;
};

export type WorkflowDagEdge = {
  id: string;
  source: string;
  target: string;
  type?: string;
  label?: string | null;
  [key: string]: unknown;
};

export type WorkflowDag = {
  workflow_id: string;
  display_name?: string;
  nodes: WorkflowDagNode[];
  edges: WorkflowDagEdge[];
};

export type WorkflowValidation = {
  workflow_id: string;
  validation?: {
    valid?: boolean;
    errors?: Array<Record<string, unknown>>;
    warnings?: Array<Record<string, unknown>>;
  };
};

export type WorkflowCanvasScopeSummary = {
  issue_count?: number;
  error_count?: number;
  warning_count?: number;
  rule_count?: number;
  enabled_count?: number;
  event_types?: string[];
  action_types?: string[];
};

export type WorkflowCanvasIssue = {
  scope: "workflow" | "group" | "for_each" | "step";
  scope_id: string;
  authored_id?: string;
  group_id?: string;
  step_id?: string;
  severity: "error" | "warning";
  level?: "error" | "warning";
  code?: string;
  title: string;
  title_key?: string;
  title_params?: TranslateParams;
  message?: string;
  message_key?: string;
  message_params?: TranslateParams;
  field_path?: string;
  suggested_action?: string;
  suggested_action_key?: string;
  issues?: Array<{
    path?: string;
    message?: string;
    source_step_id?: string;
    outcome_name?: string;
    outcome_case_id?: string;
    message_key?: string;
    message_params?: TranslateParams;
    field_path?: string;
    suggested_action?: string;
    suggested_action_key?: string;
  }>;
};

/** One outcome a step must route, as the canvas draws it (a named port). */
export type WorkflowCanvasPort = {
  outcome: WorkflowOutcome;
  label: string;
};

export type WorkflowCanvasStep = {
  error_policy?: Record<string, unknown>;
  id: string;
  step_id?: string;
  node_type?: string;
  kind: string;
  group_id?: string | null;
  for_each_id?: string | null;
  lane_id?: string | null;
  display_name: string;
  config_summary: string;
  ports?: WorkflowCanvasPort[];
  config_summary_key?: string;
  config_summary_params?: TranslateParams;
  optional?: boolean;
  /** The run skips a disabled step and goes on along its Next outcome. */
  disabled?: boolean;
  /** Editor-only source of a connector step: system, extension or MCP tools. */
  tool_category?: "connector" | "mcp_tools" | "extension_tools";
  /** The extension a connector step comes from; its logo marks the node. */
  extension_id?: string | null;
  automation_summary?: WorkflowCanvasScopeSummary;
  validation_summary?: WorkflowCanvasScopeSummary;
};

export type WorkflowCanvasGroup = {
  id: string;
  /** Canvas-only containment; persisted group_id remains this group's own id. */
  parent?: WorkflowNodeRef;
  lane_id?: string | null;
  group_id?: string;
  display_name: string;
  body?: Record<string, unknown>;
  error_policy?: Record<string, unknown>;
  automation_summary?: WorkflowCanvasScopeSummary;
  validation_summary?: WorkflowCanvasScopeSummary;
  /** Set only on the frame the builder draws for a hook step (builder/hook-frames.ts). */
  hook?: WorkflowCanvasHookFrame;
};

/** What a hook frame's header says: the hook, and what the project connects there. */
export type WorkflowCanvasHookFrame = {
  /** UI-only author preview identity; never serialized into workflow requests. */
  declarationPending?: boolean;
  hookId: string;
  stepId: string;
  displayName: string;
  connected: boolean | null;
  targetWorkflowId: string;
  targetName: string;
  usedIn: number;
  /** The steps the hook holds (its hidden end excluded): 0 is an empty hook. */
  stepCount?: number;
  /** No project is chosen: the hook has no place for its steps yet (R2-10). */
  needsProject?: boolean;
  /** Names from the connected definition; projected Stop cards retain its contract. */
  outputContract?: Pick<WorkflowContractSummary, "outputs" | "artifacts">;
};

export type WorkflowCanvasForEach = {
  id: string;
  /** Visual containment when a connected workflow is projected into a hook. */
  parent?: WorkflowNodeRef;
  lane_id?: string | null;
  for_each_id?: string;
  group_id?: string | null;
  display_name: string;
  over: string;
  item_name: string;
  index_name?: string | null;
  body?: Record<string, unknown>;
  error_policy?: Record<string, unknown>;
  automation_summary?: WorkflowCanvasScopeSummary;
  validation_summary?: WorkflowCanvasScopeSummary;
};

export type WorkflowCanvasAutomationRule = {
  scope: "workflow" | "group" | "for_each" | "step";
  scope_id: string;
  reaction_key?: string;
  display_name?: string;
  event_type?: string;
  action_type?: string;
  enabled?: boolean;
};

export type WorkflowCanvasGraph = {
  workflow: {
    id: string;
    display_name: string;
    enabled: boolean;
    revision?: number | null;
    callable_by_parent: boolean;
  };
  interface_contract: {
    trigger_channels?: string[];
    input_mode?: string;
    expected_inputs?: WorkflowContractField[];
    input_schema?: Record<string, unknown>;
    strict_inputs?: boolean;
  };
  output_contract: {
    output_mode?: string;
    expected_outputs?: WorkflowContractField[];
    /** File outputs, derived from the steps' "Return from workflow" settings. */
    returned_files?: Array<Record<string, unknown>>;
  };
  entry?: WorkflowNodeRef | null;
  groups: WorkflowCanvasGroup[];
  for_each?: WorkflowCanvasForEach[];
  steps: WorkflowCanvasStep[];
  edges: WorkflowGraphEdge[];
  validation_issues: WorkflowCanvasIssue[];
  automation_rules: WorkflowCanvasAutomationRule[];
};

export type WorkflowContractField = {
  name: string;
  label?: string;
  type?: string;
  required?: boolean;
  description?: string;
  options?: string[];
  /**
   * The value fixed for this workflow revision. Absent when nothing is
   * configured, and always absent for `type: "secret"` — a configured secret is
   * reported as state, never as a value.
   */
  default?: unknown;
  /**
   * Whether a default is configured. Separate from `default` because `false`,
   * `0` and `""` are values an operator can mean, and because a secret has a
   * state with no value to read it from.
   */
  default_configured?: boolean;
  [key: string]: unknown;
};

export type WorkflowContractResponse = {
  workflow_id: string;
  display_name?: string;
  enabled?: boolean;
  interface?: {
    enabled?: boolean;
    trigger_channels?: string[];
    input_mode?: string;
    strict_inputs?: boolean;
    expected_inputs?: WorkflowContractField[];
    input_schema?: Record<string, unknown>;
    output_mode?: string;
    expected_outputs?: WorkflowContractField[];
    returned_files?: Array<Record<string, unknown>>;
  };
  definition_issues?: Array<Record<string, unknown>>;
};

/**
 * One extension an unavailable workflow needs, as the backend reports it: the
 * structured reason behind `availability_reason_text`, for the UI to word.
 */
export type MissingExtensionDetail = {
  extension_id: string;
  display_name?: string;
  /** not_installed | installed_disabled | installed_incompatible | unknown */
  state?: string;
  min_version?: string;
  max_version_exclusive?: string;
};

export type WorkflowExportPolicy = {
  workflow_id: string;
  extension_owned?: boolean;
  protected?: boolean;
  full_export_allowed?: boolean;
  reference_export_allowed?: boolean;
  clone_allowed?: boolean;
  structure_read_allowed?: boolean;
  structure_change_allowed?: boolean;
  step_test_run_allowed?: boolean;
  source_install_id?: string;
  source_release_id?: string;
  /** The extension a protected workflow comes from, by the names a person reads. */
  source_extension_id?: string;
  source_extension_name?: string;
  source_extension_version?: string;
  local_workflow_id?: string;
  projection_mode?: string;
  availability_status?: string;
  availability_reason_code?: string;
  availability_reason_text?: string;
  new_runs_blocked?: boolean;
  visible_in_catalog?: boolean;
  missing_extension_details?: MissingExtensionDetail[];
  /** Whether this viewer may install or turn on extensions (admin). */
  viewer_can_manage_extensions?: boolean;
  reason?: string;
  [key: string]: unknown;
};

export type WorkflowStepContractPreviewRow = {
  name: string;
  type?: string;
  description?: string;
  required?: boolean;
  source?: string;
  resolved?: boolean;
  resolution?: string;
  suggested_sources?: string[];
  contract_source?: string;
  contract_source_id?: string;
  source_available?: boolean;
  source_type?: string;
  read_only?: boolean;
  [key: string]: unknown;
};

export type WorkflowStepContractPreview = {
  workflow_id: string;
  step_id?: string;
  step_kind?: string;
  agent_id?: string;
  skill_id?: string;
  block_key?: string;
  published_block?: Record<string, unknown> | null;
  skill?: {
    id?: string;
    label?: string;
  };
  locked_required?: WorkflowStepContractPreviewRow[];
  unresolved_required?: WorkflowStepContractPreviewRow[];
  optional?: WorkflowStepContractPreviewRow[];
  custom?: WorkflowStepContractPreviewRow[];
  outputs?: Array<Record<string, unknown>>;
  available_type_map?: Record<string, string>;
};

export async function fetchWorkflows({
  enabledOnly,
  search,
}: {
  enabledOnly: boolean;
  search: string;
}, options?: FetchJsonOptions) {
  const params = new URLSearchParams();
  if (enabledOnly) {
    params.set("enabled_only", "true");
  }
  if (search.trim()) {
    params.set("search", search.trim());
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return fetchJson<WorkflowListItem[]>(`/api/workflows${suffix}`, { cache: "no-store" }, options);
}

export async function fetchWorkflowDetail(workflowId: string, options?: FetchJsonOptions) {
  return fetchJson<WorkflowDetail>(`/api/workflows/${encodeURIComponent(workflowId)}`, { cache: "no-store" }, options);
}

export async function fetchWorkflowDag(workflowId: string) {
  return fetchJson<WorkflowDag>(`/api/workflows/${encodeURIComponent(workflowId)}/dag`, { cache: "no-store" });
}

export async function fetchWorkflowCanvasGraph(workflowId: string) {
  return fetchJson<WorkflowCanvasGraph>(
    `/api/workflows/${encodeURIComponent(workflowId)}/canvas-graph`,
    { cache: "no-store" },
  );
}

export async function fetchWorkflowValidation(workflowId: string) {
  return fetchJson<WorkflowValidation>(`/api/workflows/${encodeURIComponent(workflowId)}/contract-validation`);
}

function stepRoute(workflowId: string, stepId: string, leaf: string) {
  return `/api/workflows/${encodeURIComponent(workflowId)}/steps/${encodeURIComponent(stepId)}/${leaf}`;
}

export async function fetchWorkflowStepInputDefaults(workflowId: string, stepId: string) {
  return fetchJson<unknown>(stepRoute(workflowId, stepId, "input-defaults"), { cache: "no-store" });
}

export async function fetchWorkflowStepIOValidation(workflowId: string, stepId: string) {
  return fetchJson<unknown[]>(stepRoute(workflowId, stepId, "io-validation"), { cache: "no-store" });
}

export async function fetchWorkflowStepAvailableOutputs(workflowId: string, stepId: string) {
  return fetchJson<unknown[]>(stepRoute(workflowId, stepId, "available-outputs"), { cache: "no-store" });
}

/**
 * The variables one editor offers. With a saved step: what runs before it, or with
 * `position: "after"` what a step placed right after it reads. Without a step the
 * position is unknown: every saved step's outputs (and a For each's item names).
 */
export async function fetchWorkflowStepAvailableVariables(
  workflowId: string,
  stepId: string,
  options: { position?: "before" | "after"; forEachId?: string } = {},
) {
  if (!stepId) {
    const query = options.forEachId ? `?for_each_id=${encodeURIComponent(options.forEachId)}` : "";
    return fetchJson<Record<string, unknown>>(
      `/api/workflows/${encodeURIComponent(workflowId)}/available-variables${query}`,
      { cache: "no-store" },
    );
  }
  const query = options.position === "after" ? "?position=after" : "";
  return fetchJson<Record<string, unknown>>(`${stepRoute(workflowId, stepId, "available-variables")}${query}`, {
    cache: "no-store",
  });
}

export async function fetchWorkflowContract(workflowId: string) {
  return fetchJson<WorkflowContractResponse>(
    `/api/workflows/${encodeURIComponent(workflowId)}/contract`,
    { cache: "no-store" },
  );
}

export type WorkflowStats = {
  workflow_id: string;
  total_runs: number;
  completed_count: number;
  failed_count: number;
  active_count: number;
  cancelled_count: number;
  success_rate: number | null;
  total_cost_usd: number;
  avg_cost_per_run_usd: number;
  jobs_with_cost: number;
  last_run_at: string | null;
  avg_duration_seconds: number | null;
};

export async function fetchWorkflowStats(workflowId: string, options?: FetchJsonOptions) {
  return fetchJson<WorkflowStats>(
    `/api/workflows/${encodeURIComponent(workflowId)}/stats`,
    {},
    options,
  );
}

export async function fetchWorkflowExportPolicy(workflowId: string, options?: FetchJsonOptions) {
  return fetchJson<WorkflowExportPolicy>(
    `/api/workflows/${encodeURIComponent(workflowId)}/export-policy`,
    { cache: "no-store" },
    options,
  );
}

export async function fetchWorkflowStepContractPreview(
  workflowId: string,
  params: {
    stepId?: string;
    agentId?: string;
    blockKey?: string;
    participantKey?: string;
    blockBindingMode?: "auto" | "explicit";
  },
) {
  const query = new URLSearchParams();
  if (params.stepId?.trim()) {
    query.set("step_id", params.stepId.trim());
  }
  if (params.agentId?.trim()) {
    query.set("agent_id", params.agentId.trim());
  }
  if (params.participantKey?.trim()) query.set("participant_key", params.participantKey.trim());
  if (params.blockBindingMode) query.set("block_binding_mode", params.blockBindingMode);
  if (params.blockKey?.trim()) {
    query.set("block_key", params.blockKey.trim());
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return fetchJson<WorkflowStepContractPreview>(
    `/api/workflows/${encodeURIComponent(workflowId)}/step-contract-preview${suffix}`,
  );
}

export type TransformActionContract = {
  action_id: string;
  category: string;
  display_name: string;
  source?: string;
  tool_id?: string;
  tool_action_id?: string;
  transform_exposure?: string;
  value_scope: string;
  input_type: string;
  output_type: string;
  params_schema: {
    type?: string;
    required?: string[];
    properties?: Record<string, { type?: string; enum?: unknown[] }>;
    additionalProperties?: boolean;
  };
  param_sources?: Record<
    string,
    {
      literal?: boolean;
      selector?: boolean;
      current?: boolean;
      types?: string[];
    }
  >;
  pure?: boolean;
  deterministic?: boolean;
  io_allowed?: boolean;
  transform_safe?: boolean;
  backends?: { scalar?: boolean; polars?: boolean };
};

export type TransformActionCatalogResponse = {
  catalog_version?: number;
  actions: TransformActionContract[];
};

export type TransformPreviewResponse = {
  ok: boolean;
  errors: Array<Record<string, unknown>>;
  output?: unknown;
  result_target?: string;
  state_patch: Record<string, unknown>;
  detail?: string;
  preview?: boolean;
};

export type TabularFeedInspectResponse = {
  ok: boolean;
  result?: {
    format?: string;
    encoding?: string;
    delimiter?: string;
    sheets?: unknown[];
    columns?: unknown[];
    warnings?: unknown[];
    row_count_estimate?: number;
    preview_rows?: unknown[];
  };
  errors?: Array<Record<string, unknown>>;
};

export async function inspectWorkflowTabularFeed(
  projectId: string,
  workflowId: string,
  artifactHandle: string,
  options: Record<string, unknown>,
) {
  return fetchJson<TabularFeedInspectResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}/tabular-feed/inspect`,
    {
      body: JSON.stringify({ artifact_handle: artifactHandle, options }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
  );
}

export async function fetchWorkflowTransformActions(workflowId: string) {
  return fetchJson<TransformActionCatalogResponse>(
    `/api/workflows/${encodeURIComponent(workflowId)}/transform-actions`,
    { cache: "no-store" },
  );
}

export async function previewWorkflowTransform(
  workflowId: string,
  config: Record<string, unknown>,
  sampleData: unknown,
) {
  return fetchJson<TransformPreviewResponse>(
    `/api/workflows/${encodeURIComponent(workflowId)}/transform-preview`,
    {
      body: JSON.stringify({ config, sample_data: sampleData }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
  );
}
