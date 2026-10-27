import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { canvasGraphWithDefinition, definitionFromWorkflow, groupLanes } from "@/pages/workflows/builder/change-set";
import type { WorkflowCanvasGraph, WorkflowContractSummary, WorkflowTargetRef } from "@/pages/workflows/shared/api";
import type { JobDefinitionPayload } from "./api";
import { targetLabel } from "@/pages/workflows/builder/api";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

/** An archived graph has no live-workflow fallback, including for unsupported formats. */
export function archivedDefinitionCanvasGraph(definition: JobDefinitionPayload, t: I18nContextValue["t"] = fallbackContext.t): WorkflowCanvasGraph | null {
  if (!definition.available || definition.schema_version !== 1 || definition.execution_graph?.schema_version !== 1) return null;
  if (!Array.isArray(definition.steps) || !Array.isArray(definition.groups) || !Array.isArray(definition.for_each)) return null;
  if (definition.groups.some((group) => typeof group.group_id !== "string")) return null;
  if (definition.for_each.some((item) => typeof item.for_each_id !== "string" || typeof item.over !== "string" || typeof item.item_name !== "string")) return null;
  const workflowId = definition.workflow_id || "";
  const model = definitionFromWorkflow({
    workflow_id: workflowId,
    steps: definition.steps || [],
    groups: definition.groups.map((group) => ({ ...group, group_id: String(group.group_id) })),
    for_each: definition.for_each.map((item) => ({
      ...item,
      for_each_id: String(item.for_each_id),
      over: String(item.over),
      item_name: String(item.item_name),
    })),
    settings: { execution_graph: definition.execution_graph },
  });
  return canvasGraphWithDefinition({
    workflow: { id: workflowId, display_name: workflowId, revision: definition.revision, enabled: false, callable_by_parent: false },
    interface_contract: {}, output_contract: {}, groups: [], steps: [], edges: [], validation_issues: [], automation_rules: [],
  }, model, t);
}


/** Whitelist the pinned summary again before rendering cards or the inspector. */
export function archivedDefinitionContract(definition: JobDefinitionPayload): WorkflowContractSummary | null {
  const value = definition.contract_summary;
  if (!definition.available || !value || !["fields", "json_schema", "passthrough"].includes(value.input_mode)
    || !["none", "variables", "artifacts", "variables_and_artifacts"].includes(value.output_mode)) return null;
  const lists = [value.outputs, value.artifacts, value.trigger_channels];
  if (!lists.every((items) => Array.isArray(items) && items.every((item) => typeof item === "string"))) return null;
  if (!Array.isArray(value.inputs) || !value.inputs.every((item) => item && typeof item.name === "string" && typeof item.required === "boolean")) return null;
  return {
    input_mode: value.input_mode, output_mode: value.output_mode,
    inputs: value.inputs.map(({ name, required }) => ({ name, required })),
    outputs: [...value.outputs], artifacts: [...value.artifacts],
    trigger_channels: [...value.trigger_channels],
  };
}


/** Destinations of this immutable approval execution, never an editable workflow graph. */
export function archivedApprovalDestinations(
  definition: JobDefinitionPayload | undefined,
  owner: Readonly<{ jobId: string; workflowId: string; stepId: string; stepExecutionId: string }>,
  t: I18nContextValue["t"] = fallbackContext.t,
): { approved: string | null; rejected: string | null } {
  const unavailable = { approved: null, rejected: null };
  if (!definition || !owner.jobId || !owner.workflowId || !owner.stepId || !owner.stepExecutionId ||
    definition.job_id !== owner.jobId || definition.workflow_id !== owner.workflowId ||
    !definition.snapshot_id || !definition.digest || !Number.isInteger(definition.revision) || Number(definition.revision) <= 0) return unavailable;
  try {
    const graph = archivedDefinitionCanvasGraph(definition, t);
    const approval = graph?.steps.filter((step) => step.step_id === owner.stepId);
    if (!graph || approval?.length !== 1 || approval[0].kind !== "approval") return unavailable;
    const label = (target: WorkflowTargetRef): string | null => {
      if (target.kind === "step") {
        const rows = graph.steps.filter((step) => step.step_id === target.id);
        return rows.length === 1 ? rows[0].display_name : null;
      }
      if (target.kind === "group") {
        const rows = graph.groups.filter((group) => group.id === target.id);
        return rows.length === 1 ? rows[0].display_name : null;
      }
      if (target.kind === "for_each") {
        const rows = graph.for_each?.filter((item) => item.id === target.id) || [];
        return rows.length === 1 ? rows[0].display_name : null;
      }
      if (target.kind !== "group_exit" && target.kind !== "for_each_exit") return null;
      const containers = target.kind === "group_exit"
        ? graph.groups.filter((group) => group.id === target.group_id)
        : graph.for_each?.filter((item) => item.id === target.for_each_id) || [];
      if (containers.length !== 1) return null;
      const container = containers[0];
      const lanes = target.lane_id ? groupLanes(container).filter((item) => item.lane_id === target.lane_id) : [];
      if (target.lane_id && lanes.length !== 1) return null;
      const lane = lanes[0];
      return target.kind === "group_exit"
        ? targetLabel({ ...target, group_id: container.display_name, lane_id: lane ? stringFromUnknownValue(lane.display_name) || lane.lane_id : null }, t)
        : targetLabel({ ...target, for_each_id: container.display_name }, t);
    };
    const destination = (outcome: "approved" | "rejected") => {
      const edges = graph.edges.filter((edge) => edge.source.kind === "step" && edge.source.id === owner.stepId && edge.outcome.name === outcome);
      return edges.length === 1 ? label(edges[0].target) : null;
    };
    return { approved: destination("approved"), rejected: destination("rejected") };
  } catch {
    return unavailable;
  }
}
