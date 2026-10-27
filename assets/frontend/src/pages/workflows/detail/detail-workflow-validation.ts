import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { WorkflowForEach, WorkflowGroup, WorkflowStep } from "@/pages/workflows/shared/api";
import type {
  WorkflowGroupRow,
  WorkflowStepRow,
  WorkflowValidationIssueGroup,
} from "@/pages/workflows/detail/detail-core";
import {
  entityOrEmpty,
  textOrEmpty,
} from "@/pages/workflows/detail/detail-test-run";
import { objectOrEmpty } from "@/pages/workflows/detail/detail-interface";
import { isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => textOrEmpty(item)).filter(isTruthy);
}

export function isPlaceholderStepLabel(value: string): boolean {
  const normalized = textOrEmpty(value);
  if (!normalized) {
    return false;
  }
  return /^step[\s_-]*\d+$/i.test(normalized);
}

export function normalizedStepKeyValue(value: unknown): string {
  const normalized = textOrEmpty(value);
  return normalized.toLowerCase() === "none" ? "" : normalized;
}

export function stepDuplicateKey(step: Record<string, unknown>): string {
  return [
    normalizedStepKeyValue(step.step_kind) || "agent",
    normalizedStepKeyValue(step.agent_id),
    normalizedStepKeyValue(step.connector_id),
    normalizedStepKeyValue(step.workflow_id),
    normalizedStepKeyValue(step.participant_key),
    normalizedStepKeyValue(step.block_key),
  ].map((value) => stringFromUnknownValue(value)).join("::");
}

export function dedupePlaceholderWorkflowSteps(
  steps: Array<Record<string, unknown>>,
): Array<Record<string, unknown>> {
  const seen = new Set<string>();
  return steps.filter((step) => {
    const key = stepDuplicateKey(step);
    const configuredLabel =
      textOrEmpty(step.display_name) ||
      textOrEmpty(step.step_label) ||
      textOrEmpty(step.name) ||
      textOrEmpty(step.title) ||
      textOrEmpty(step.step_id);
    if (
      seen.has(key) &&
      (!configuredLabel || isPlaceholderStepLabel(configuredLabel))
    ) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

export function normalizeWorkflowStep(
  step: Record<string, unknown>,
  index: number,
  local = false,
): WorkflowStepRow {
  const agentEntity = entityOrEmpty(step.agent);
  const skillEntity = entityOrEmpty(step.skill);
  const runtimeId = textOrEmpty(step.step_id) || `step_${index + 1}`;
  const ordinal = index + 1;
  const stepKind = textOrEmpty(step.step_kind) || "agent";
  const agentId =
    textOrEmpty(step.agent_id) ||
    textOrEmpty(agentEntity.agent_id) ||
    textOrEmpty(agentEntity.id) ||
    textOrEmpty(agentEntity.key) ||
    textOrEmpty(agentEntity.name);
  const skillId =
    textOrEmpty(step.skill_id) ||
    textOrEmpty(skillEntity.skill_id) ||
    textOrEmpty(skillEntity.id) ||
    textOrEmpty(skillEntity.key) ||
    textOrEmpty(skillEntity.name);
  const agentLabel =
    textOrEmpty(step.agent_display_name) ||
    textOrEmpty(step.agent_name) ||
    textOrEmpty(agentEntity.display_name) ||
    textOrEmpty(agentEntity.label) ||
    textOrEmpty(agentEntity.name) ||
    agentId;
  const skillLabel =
    textOrEmpty(step.skill_display_name) ||
    textOrEmpty(skillEntity.display_name) ||
    textOrEmpty(skillEntity.label) ||
    textOrEmpty(skillEntity.name) ||
    skillId;
  const mappedBlockKey = textOrEmpty(step.block_key);
  const blockKey = mappedBlockKey;
  const configuredLabel =
    textOrEmpty(step.display_name) ||
    textOrEmpty(step.step_label) ||
    textOrEmpty(step.name) ||
    textOrEmpty(step.title);
  const labelFromConfig = isPlaceholderStepLabel(configuredLabel)
    ? ""
    : configuredLabel;
  const primaryLabel =
    labelFromConfig ||
    agentLabel ||
    skillLabel ||
    agentId ||
    skillId ||
    textOrEmpty(step.participant_key) ||
    textOrEmpty(step.step_id) ||
    runtimeId ||
    `step_${ordinal}`;
  const secondaryLabel =
    textOrEmpty(step.description) ||
    (agentLabel && skillLabel ? `${agentLabel} -> ${skillLabel}` : "") ||
    skillLabel ||
    skillId ||
    textOrEmpty(step.step_kind);
  return {
    agentId,
    agentLabel,
    blockKey,
    groupId: textOrEmpty(step.group_id),
    forEachId: textOrEmpty(step.for_each_id),
    laneId: textOrEmpty(step.lane_id),
    local,
    optional: Boolean(step.optional),
    ordinal,
    primaryLabel,
    raw: step,
    runtimeId,
    secondaryLabel,
    skillLabel,
    skillId,
    stepKind,
    stepKey: runtimeId,
  };
}

function sortOrderOf(step: Record<string, unknown>, index: number): number {
  const value = Number(step.sort_order);
  return Number.isFinite(value) ? value : index;
}

export type WorkflowDefinitionRows = {
  groupRows: WorkflowGroupRow[];
  rootSteps: WorkflowStepRow[];
  stepRows: WorkflowStepRow[];
  stepRowsById: Map<string, WorkflowStepRow>;
};

/**
 * Rows for the drawers and the step list. Order follows sort_order for display
 * only; it never decides what runs next, the execution graph does.
 */
export function normalizeWorkflowDefinition(
  steps: WorkflowStep[],
  groups: WorkflowGroup[],
  forEach: WorkflowForEach[] = [],
  localStepIds: Set<string> = new Set(),
  localGroupIds: Set<string> = new Set(),
): WorkflowDefinitionRows {
  const stepRows = steps
    .map((step, index) => ({ index, step: step as Record<string, unknown> }))
    .sort((left, right) => sortOrderOf(left.step, left.index) - sortOrderOf(right.step, right.index))
    .map(({ step }, index) =>
      normalizeWorkflowStep(step, index, localStepIds.has(textOrEmpty(step.step_id))),
    );
  const stepRowsById = new Map(stepRows.map((row) => [row.runtimeId, row]));
  const containerRow = (
    container: WorkflowGroup | WorkflowForEach,
    containerKind: "group" | "for_each",
  ): WorkflowGroupRow => {
    const isForEach = containerKind === "for_each";
    const item = container as WorkflowForEach;
    const id = textOrEmpty(isForEach ? item.for_each_id : (container as WorkflowGroup).group_id);
    const body = objectOrEmpty(container.body);
    const bodyKind = textOrEmpty(body.kind);
    return {
      bodyKind: bodyKind === "sequence" || bodyKind === "lanes" ? bodyKind : "",
      containerKind,
      description: textOrEmpty(container.description),
      displayName: textOrEmpty(container.display_name) || id,
      groupId: id,
      lanes: Array.isArray(body.lanes)
        ? body.lanes.map((lane) => textOrEmpty(objectOrEmpty(lane).lane_id)).filter(isTruthy)
        : [],
      local: localGroupIds.has(id),
      over: isForEach ? textOrEmpty(item.over) : "",
      itemName: isForEach ? textOrEmpty(item.item_name) : "",
      indexName: isForEach ? textOrEmpty(item.index_name) : "",
      parentGroupId: isForEach ? textOrEmpty(item.group_id) : "",
      raw: container,
      steps: stepRows.filter((row) => (isForEach ? row.forEachId === id : row.groupId === id && !row.forEachId)),
      forEachIds: isForEach
        ? []
        : forEach.filter((entry) => textOrEmpty(entry.group_id) === id).map((entry) => textOrEmpty(entry.for_each_id)),
    };
  };
  const groupRows = [
    ...groups.map((group) => containerRow(group, "group")),
    ...forEach.map((item) => containerRow(item, "for_each")),
  ];
  return {
    groupRows,
    rootSteps: stepRows.filter((row) => !row.groupId && !row.forEachId),
    stepRows,
    stepRowsById,
  };
}

export function workflowIsScaffoldOnly(steps: WorkflowStep[]): boolean {
  if (!steps.length) {
    return true;
  }
  return steps.every((step) => {
    const kind = textOrEmpty(step.step_kind).toLowerCase();
    return kind === "set_variable" || kind === "stop_success";
  });
}

export function workflowValidationIssueText(issue: Record<string, unknown>): string {
  return textOrEmpty(issue.message || issue.error || issue.warning);
}

export function workflowValidationIssueCode(issue: Record<string, unknown>): string {
  return (
    textOrEmpty(issue.code || issue.type || issue.issue_type) ||
    "validation.issue"
  );
}

export function parseWorkflowIssuePath(path: string): {
  groupId: string;
  stepId: string;
} {
  const stepMatch = /workflow\.steps\.([^.[\]]+)/.exec(path);
  const groupMatch = /workflow\.groups\.([^.[\]]+)/.exec(path);
  return {
    groupId: groupMatch?.[1] || "",
    stepId: stepMatch?.[1] || "",
  };
}

export function workflowValidationIssueSource(issue: Record<string, unknown>): string {
  const direct = textOrEmpty(issue.source);
  if (direct) {
    return direct;
  }
  const message = workflowValidationIssueText(issue);
  const quotedSource =
    /source '([^']+)'/.exec(message)?.[1] ||
    /maps to '([^']+)'/.exec(message)?.[1] ||
    "";
  if (quotedSource) {
    return quotedSource;
  }
  return /\bmessage\.step\.[A-Za-z0-9_-]+\.(?:artifacts\.)?[A-Za-z0-9_.-]+/.exec(message)?.[0] || "";
}

/** The producing step and output name of a `message.step.<id>[.artifacts].<name>` source. */
export function stepSourceParts(source: string): {
  sourceName: string;
  sourceStepId: string;
} {
  const parts = source.split(".");
  if (parts.length >= 4 && parts[0] === "message" && parts[1] === "step") {
    const nameParts = parts[3] === "artifacts" ? parts.slice(4) : parts.slice(3);
    return { sourceStepId: parts[2], sourceName: nameParts.join(".") };
  }
  return { sourceStepId: "", sourceName: "" };
}

export function workflowValidationIssueInputName(
  issue: Record<string, unknown>,
): string {
  const direct = textOrEmpty(issue.input_name || issue.inputName);
  if (direct) {
    return direct;
  }
  const message = workflowValidationIssueText(issue);
  return (
    /input '([^']+)'/.exec(message)?.[1] ||
    /context_inject '([^']+)'/.exec(message)?.[1] ||
    /key '([^']+)'/.exec(message)?.[1] ||
    ""
  );
}

export function workflowValidationIssueCategory(code: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (code.startsWith("invoke.")) {
    return t("react.workflows.manual_child_workflow_mapping", "Child workflow mapping");
  }
  if (code.includes("boundary") || code.includes("input_source") || code.includes("required_input")) {
    return t("react.workflows.manual_step_handoff", "Step handoff");
  }
  if (code.includes("step_contract")) {
    return t("react.workflows.manual_step_contract", "Step contract");
  }
  return t("react.workflows.manual_workflow_readiness", "Workflow readiness");
}

export function humanizeWorkflowValidationCode(code: string): string {
  return code
    .replace(/^workflow\./, "")
    .replace(/^invoke\./, "")
    .replaceAll(".", " ")
    .replaceAll("_", " ")
    .replaceAll(/\b\w/g, (char) => char.toUpperCase());
}

export function workflowValidationIssueGroupKey(
  issue: Record<string, unknown>,
  source: string,
  inputName: string,
): string {
  const code = workflowValidationIssueCode(issue);
  if (source.startsWith("message.step.")) {
    return `source:${source}`;
  }
  if (
    code === "invoke.required_input_unmapped" ||
    code === "invoke.input_mapping_unknown_key"
  ) {
    return `invoke:${textOrEmpty(issue.child_workflow_id)}:${inputName}`;
  }
  if (inputName) {
    return `${code}:${inputName}`;
  }
  return `${code}:${workflowValidationIssueText(issue)}`;
}

export function workflowValidationIssueTitle(
  group: Pick<
    WorkflowValidationIssueGroup,
    "code" | "inputName" | "source" | "sourceStepId"
  >,
  stepRowsById: Map<string, WorkflowStepRow>, t: I18nContextValue["t"] = fallbackContext.t
): string {
  if (group.sourceStepId && !stepRowsById.has(group.sourceStepId)) {
    return t("react.workflows.missing_source_step_value0", "Missing source step: {value0}", { value0: group.sourceStepId });
  }
  if (group.source) {
    return t("react.workflows.unavailable_source_value0", "Unavailable source: {value0}", { value0: group.source });
  }
  if (group.code === "invoke.required_input_unmapped") {
    return t("react.workflows.child_input_is_not_mapped_value0", "Child input is not mapped: {value0}", { value0: group.inputName || "required input" });
  }
  if (group.code === "invoke.input_mapping_unknown_key") {
    return t("react.workflows.unknown_child_input_mapping_value0", "Unknown child input mapping: {value0}", { value0: group.inputName || "mapping key" });
  }
  if (group.inputName) {
    return `${humanizeWorkflowValidationCode(group.code)}: ${group.inputName}`;
  }
  return humanizeWorkflowValidationCode(group.code);
}

export function affectedWorkflowLabel(
  group: WorkflowGroupRow | null,
  step: WorkflowStepRow | null,
): string {
  if (step) {
    const stepLabel = step.primaryLabel || step.runtimeId;
    return group ? `${group.displayName || group.groupId} / ${stepLabel}` : stepLabel;
  }
  return group ? group.displayName || group.groupId : "";
}

function mergeValidationIssueGroup(
  issueGroup: WorkflowValidationIssueGroup,
  issue: Record<string, unknown>,
  message: string,
  label: string,
  group: WorkflowGroupRow | null,
  step: WorkflowStepRow | null,
): void {
  issueGroup.issues.push(issue);
  if (message && !issueGroup.messages.includes(message)) issueGroup.messages.push(message);
  if (label && !issueGroup.affectedLabels.includes(label)) issueGroup.affectedLabels.push(label);
  if (!issueGroup.targetStep && step) issueGroup.targetStep = step;
  if (!issueGroup.targetGroup && group) issueGroup.targetGroup = group;
}

function validationIssueTargets(
  issue: Record<string, unknown>,
  rows: WorkflowDefinitionRows,
): { group: WorkflowGroupRow | null; step: WorkflowStepRow | null } {
  const parsed = parseWorkflowIssuePath(textOrEmpty(issue.path));
  const stepId = textOrEmpty(issue.step_id) || parsed.stepId;
  const step = stepId ? rows.stepRowsById.get(stepId) || null : null;
  const groupId = textOrEmpty(issue.group_id) || parsed.groupId || step?.groupId || "";
  const group = groupId ? rows.groupRows.find((row) => row.groupId === groupId) || null : null;
  return { group, step };
}

export function buildWorkflowValidationIssueGroups(
  issues: Array<Record<string, unknown>>,
  rows: WorkflowDefinitionRows,
  severity: "error" | "warning", t: I18nContextValue["t"] = fallbackContext.t
): WorkflowValidationIssueGroup[] {
  const groups = new Map<string, WorkflowValidationIssueGroup>();
  issues.forEach((issue, index) => {
    const code = workflowValidationIssueCode(issue);
    const { group, step } = validationIssueTargets(issue, rows);
    const source = workflowValidationIssueSource(issue);
    const sourceParts = stepSourceParts(source);
    const sourceStepId =
      textOrEmpty(issue.source_step_id) || sourceParts.sourceStepId;
    const sourceName = textOrEmpty(issue.source_name) || sourceParts.sourceName;
    const inputName = workflowValidationIssueInputName(issue);
    const key = workflowValidationIssueGroupKey(issue, source, inputName);
    const label = affectedWorkflowLabel(group, step);
    const message =
      workflowValidationIssueText(issue) ||
      `${severity === "error" ? "Validation error" : "Validation warning"} #${
        index + 1
      }`;
    const existing = groups.get(key);
    if (existing) {
      mergeValidationIssueGroup(existing, issue, message, label, group, step);
      return;
    }
    const nextGroup: WorkflowValidationIssueGroup = {
      affectedLabels: label ? [label] : [],
      category: workflowValidationIssueCategory(code, t),
      code,
      inputName,
      issues: [issue],
      key,
      messages: message ? [message] : [],
      severity,
      source,
      sourceName,
      sourceStepId,
      targetGroup: group,
      targetStep: step,
      title: "",
    };
    nextGroup.title = workflowValidationIssueTitle(nextGroup, rows.stepRowsById, t);
    groups.set(key, nextGroup);
  });
  return Array.from(groups.values()).sort((left, right) => {
    if (left.category !== right.category) {
      return left.category.localeCompare(right.category);
    }
    return left.title.localeCompare(right.title);
  });
}
