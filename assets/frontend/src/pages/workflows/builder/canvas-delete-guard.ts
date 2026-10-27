import type { WorkflowChangeIssue, WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import { stepForEachId, stepGroupId, stepId, type WorkflowDefinitionModel } from "@/pages/workflows/builder/change-set";

export type DeletionTarget = { kind: "step" | "group" | "for_each"; id: string };
export type DeletionScope = { kind: "step" | "group"; id: string; label: string };
export type DeletionClosure = {
  steps: string[];
  groups: string[];
  forEach: string[];
  scopes: DeletionScope[];
};

/** Only canonical definition members belong to a deletion; Hook display frames do not. */
export function deriveDeletionClosure(definition: WorkflowDefinitionModel, target: DeletionTarget): DeletionClosure | null {
  const steps = new Set<string>();
  const groups = new Set<string>();
  const forEach = new Set<string>();
  if (target.kind === "step") {
    if (!definition.steps.some((step) => stepId(step) === target.id)) return null;
    steps.add(target.id);
  } else if (target.kind === "for_each") {
    if (!definition.forEach.some((item) => item.for_each_id === target.id)) return null;
    forEach.add(target.id);
  } else {
    if (!definition.groups.some((item) => item.group_id === target.id)) return null;
    groups.add(target.id);
    for (const item of definition.forEach) {
      if (item.group_id === target.id) forEach.add(item.for_each_id);
    }
  }
  for (const step of definition.steps) {
    if (groups.has(stepGroupId(step)) || forEach.has(stepForEachId(step))) steps.add(stepId(step));
  }
  const scopes: DeletionScope[] = [
    ...definition.groups.filter((item) => groups.has(item.group_id)).map((item) => ({
      kind: "group" as const, id: item.group_id, label: item.display_name || item.group_id,
    })),
    ...definition.steps.filter((item) => steps.has(stepId(item))).map((item) => ({
      kind: "step" as const, id: stepId(item), label: item.display_name || stepId(item),
    })),
  ];
  return { steps: [...steps], groups: [...groups], forEach: [...forEach], scopes };
}

export function deletionOperation(target: DeletionTarget): WorkflowChangeOperation {
  if (target.kind === "step") return { op: "delete_step", step_id: target.id };
  if (target.kind === "group") return { op: "delete_group", group_id: target.id, with_contents: true };
  return { op: "delete_for_each", for_each_id: target.id, with_contents: true };
}

/** The backend's all-account scope check is authoritative; never surface its path/message. */
export function deletionScopeIssue(issues: WorkflowChangeIssue[]): boolean {
  return issues.some((issue) => issue.code === "workflow.reaction.scope_missing");
}

/** Exact codes are current producer-owned blockers that may coexist with a local draft.
 * Final Apply remains authoritative; unknown and integrity codes fail closed.
 */
const STAGEABLE_DELETE_DRAFT_ISSUES = new Set([
  "workflow.graph.outcome_missing",
  "workflow.graph.unreachable",
  "workflow.graph.entry_missing",
  "workflow.steps_missing",
  "workflow.terminal_success_missing",
  "workflow.terminal_fail_missing",
  "workflow.terminal_fail_intent_missing",
  "workflow.execution_bindings_missing",
  "workflow.step_connector_missing",
  "workflow.step_dataset_query_template_missing",
  "workflow.step_dataset_query_connection_unbound",
  "workflow.step_dataset_query_required_parameter_unmapped",
  "workflow.step_participant_unbound",
  "workflow.step_agent_missing",
  "workflow.connection_required",
  "workflow.connection_unavailable",
  "oauth_needs_attention",
  "oauth_authorization_required",
  "oauth_scope_required",
  "workflow.extension_unavailable",
  "workflow.extension_install_missing",
  "workflow.extension_operation_unavailable",
  "workflow.extension_removed",
  "workflow.extension_disabled",
  "workflow.step_contract.agent.disabled",
  "workflow.step_connector_tool_unavailable",
  "workflow.agent_tool_unavailable",
  "workflow.child.unavailable",
]);

/** A server issue with no understood category cannot authorize destructive staging. */
export function deletionIssuesUnderstood(issues: WorkflowChangeIssue[]): boolean {
  return issues.every((issue) => typeof issue.code === "string" && STAGEABLE_DELETE_DRAFT_ISSUES.has(issue.code));
}
