import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { WorkflowCanvasGraph, WorkflowCanvasIssue } from "@/pages/workflows/shared/api";

/**
 * Validation that follows the step editor while the value is typed.
 *
 * The saved revision's issues come from the server. For the step being edited,
 * the rules below are the server's own step-configuration rules (same codes,
 * same conditions), evaluated against the editor's draft: an issue the draft
 * has fixed leaves the card, the header count and the issues list at once, and
 * one the draft introduces appears there. A rule the browser cannot decide
 * (a participant-bound agent, a connection list still loading) keeps the
 * server's result, as does every rule that exists only on the server.
 */

type LiveState = "present" | "absent" | "unknown";

type LiveRule = {
  code: string;
  state: LiveState;
  fieldPath: string;
  titleKey: string;
  title: string;
};

export type LiveStepDraft = {
  stepId: string;
  /** The kind the saved step has; a kind change waits for Apply and is not judged here. */
  savedKind: string;
  kind: string;
  overrideDraft: string;
  agentId: string;
  /** The agent comes from a workflow participant binding the browser does not resolve. */
  agentFromParticipant: boolean;
  /** True: a required extension connection is not chosen; null: not known yet. */
  connectionMissing: boolean | null;
};

const IF_CONSTANT_OPERATORS = new Set(["always_true", "always_false"]);

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function text(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

function state(missing: boolean): LiveState {
  return missing ? "present" : "absent";
}

function ifConditionStates(condition: unknown): Array<[string, LiveState]> {
  if (condition === null || condition === undefined || (typeof condition === "string" && !condition.trim())) {
    return [["workflow.step_if_condition_missing", "present"], ["workflow.step_if_condition_selector_missing", "absent"]];
  }
  const object = record(condition);
  if ("selector" in object || "operator" in object) {
    const operator = text(object.operator).toLowerCase();
    return [
      ["workflow.step_if_condition_missing", "absent"],
      ["workflow.step_if_condition_selector_missing", state(!IF_CONSTANT_OPERATORS.has(operator) && !text(object.selector))],
    ];
  }
  // Expressions and operator objects keep the server's word.
  return [["workflow.step_if_condition_missing", typeof condition === "string" || Object.keys(object).length ? "absent" : "unknown"]];
}

function liveRules(draft: LiveStepDraft, override: Record<string, unknown>, t: I18nContextValue["t"]): LiveRule[] {
  const config = record(override[draft.kind]);
  const rule = (code: string, ruleState: LiveState, fieldPath: string, titleKey: string, title: string): LiveRule =>
    ({ code, state: ruleState, fieldPath, titleKey, title: t(titleKey, title) });
  switch (draft.kind) {
    case "connector": {
      const connector = record(override.connector);
      const bound = Boolean(text(record(connector.connection_binding).connection_id));
      let connection: LiveState = "unknown";
      if (bound) connection = "absent";
      else if (draft.connectionMissing === true) connection = "present";
      return [
        rule("workflow.step_connector_missing", state(!text(connector.connector_id)), "connector_id",
          "react.workflows.live_issue_connector_missing", "Choose the tool or the action source this step calls."),
        rule("workflow.connection_required", connection, "connection_binding_jsonb.connection_id",
          "react.workflows.live_issue_connection_required", "Choose a connection for this extension action before running this workflow."),
      ];
    }
    case "agent":
      return [rule("workflow.step_agent_missing", draft.agentFromParticipant ? "unknown" : state(!draft.agentId), "agent_id",
        "react.workflows.live_issue_agent_missing", "Choose the agent this step asks.")];
    case "http_request":
      return [
        rule("workflow.step_http_request_url_missing", state(!text(config.url)), "override.http_request.url",
          "react.workflows.live_issue_http_url_missing", "Enter the URL this request calls."),
        rule("workflow.step_http_request_response_target_missing", state(!text(config.response_target)), "override.http_request.response_target",
          "react.workflows.live_issue_http_response_target_missing", "Name the variable that keeps the response."),
      ];
    case "dataset_query":
      return [rule("workflow.step_dataset_query_template_missing", state(!text(config.query_template_id)), "override.dataset_query.query_template_id",
        "react.workflows.live_issue_dataset_query_missing", "Choose the saved query this step runs.")];
    case "invoke_workflow":
      return [rule("workflow.invoke.target_missing", state(!text(config.workflow_id) && !Object.keys(record(config.hook)).length), "override.invoke_workflow.workflow_id",
        "react.workflows.live_issue_invoke_target_missing", "Choose the workflow this step invokes.")];
    case "if":
      return ifConditionStates(config.condition).map(([code, ruleState]) => rule(code, ruleState, "override.if.condition",
        code === "workflow.step_if_condition_missing" ? "react.workflows.live_issue_if_condition_missing" : "react.workflows.live_issue_if_selector_missing",
        code === "workflow.step_if_condition_missing" ? "Choose what this condition tests." : "Choose the value this condition tests."));
    case "switch":
      return [rule("workflow.step_switch_selector_missing", state(!text(config.selector)), "override.switch.selector",
        "react.workflows.live_issue_switch_selector_missing", "Choose the value the switch compares.")];
    case "set_variable":
      return [rule("workflow.step_set_variable_target_missing", state(!text(config.target) && !(config.assignments && typeof config.assignments === "object" && !Array.isArray(config.assignments))), "override.set_variable",
        "react.workflows.live_issue_set_variable_target_missing", "Name the variable this step sets.")];
    case "batch":
      return [
        rule("workflow.step_batch_source_path_missing", state(!text(config.source_path)), "override.batch.source_path",
          "react.workflows.live_issue_batch_source_missing", "Choose the list this step splits into batches."),
        rule("workflow.step_batch_result_target_missing", state(!text(config.result_target)), "override.batch.result_target",
          "react.workflows.live_issue_batch_target_missing", "Name the variable that keeps the batches."),
      ];
    default:
      return [];
  }
}

function issueTouchesStep(issue: WorkflowCanvasIssue, stepId: string): boolean {
  return issue.scope === "step" && (issue.scope_id === stepId || issue.step_id === stepId);
}

/** The draft's verdict per code, or null when the draft cannot be judged (no step, malformed JSON, a kind change). */
export function liveStepRuleStates(draft: LiveStepDraft | null, t: I18nContextValue["t"] = fallbackContext.t): LiveRule[] | null {
  if (!draft?.stepId || (draft.savedKind || "agent") !== (draft.kind || "agent")) return null;
  let override: Record<string, unknown>;
  try {
    override = record(draft.overrideDraft ? JSON.parse(draft.overrideDraft) : {});
  } catch {
    return null;
  }
  return liveRules(draft, override, t);
}

/** Saved issues with the edited step's configuration issues replaced by what its draft says now. */
export function liveValidationIssues(
  issues: WorkflowCanvasIssue[],
  draft: LiveStepDraft | null,
  t: I18nContextValue["t"] = fallbackContext.t,
): WorkflowCanvasIssue[] {
  const rules = liveStepRuleStates(draft, t);
  if (!rules?.length || !draft) return issues;
  const decided = new Map(rules.filter((rule) => rule.state !== "unknown").map((rule) => [rule.code, rule]));
  const kept = issues.filter((issue) => !(issueTouchesStep(issue, draft.stepId) && decided.get(issue.code || "")?.state === "absent"));
  const added = rules
    .filter((rule) => rule.state === "present")
    .filter((rule) => !kept.some((issue) => issueTouchesStep(issue, draft.stepId) && issue.code === rule.code))
    .map((rule): WorkflowCanvasIssue => ({
      scope: "step",
      scope_id: draft.stepId,
      authored_id: draft.stepId,
      step_id: draft.stepId,
      severity: "error",
      level: "error",
      code: rule.code,
      title: rule.title,
      title_key: rule.titleKey,
      message: rule.title,
      message_key: rule.titleKey,
      field_path: `steps.${draft.stepId}.${rule.fieldPath}`,
      issues: [],
    }));
  if (kept.length === issues.length && !added.length) return issues;
  return [...kept, ...added];
}

/** The canvas graph with live issues for the step being edited. Unchanged input keeps its identity. */
export function withLiveValidation(
  graph: WorkflowCanvasGraph | null | undefined,
  draft: LiveStepDraft | null,
  t: I18nContextValue["t"] = fallbackContext.t,
): WorkflowCanvasGraph | null | undefined {
  if (!graph) return graph;
  const issues = liveValidationIssues(graph.validation_issues || [], draft, t);
  return issues === graph.validation_issues ? graph : { ...graph, validation_issues: issues };
}

/** A clicked issue the draft has fixed is no longer shown in the editor. */
export function liveIssueResolved(
  issue: WorkflowCanvasIssue | null | undefined,
  draft: LiveStepDraft | null,
  t: I18nContextValue["t"] = fallbackContext.t,
): boolean {
  if (!issue || !draft || !issueTouchesStep(issue, draft.stepId)) return false;
  const rules = liveStepRuleStates(draft, t);
  return Boolean(rules?.some((rule) => rule.code === issue.code && rule.state === "absent"));
}
