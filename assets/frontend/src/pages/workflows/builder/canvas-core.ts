import { outcomeHandleId } from "./api";
import { routeCanvasEdge } from "./canvas-edge-routing";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { Node } from "@xyflow/react";
import { Bot, CircleStop, Code2, FileCode2, FileImage, FileText, GitBranch, Globe2, Group, Hourglass, Layers3, ListPlus, LogOut, Milestone, Network, PlugZap, Repeat2, Shuffle, SkipForward, Sparkles, StickyNote, Table2, UserCheck, Workflow, Wrench } from "lucide-react";

import type {
  WorkflowCanvasAutomationRule,
  WorkflowNodeRef,
  WorkflowOutcome,
  WorkflowCanvasGraph,
  WorkflowCanvasIssue,
  WorkflowCanvasScopeSummary,
} from "@/pages/workflows/shared/api";
import type { CanvasEdgeData, StepNodeData } from "@/pages/workflows/builder/layout";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export type CanvasPosition = { x: number; y: number };
export type InteractiveCanvasEdgeData = CanvasEdgeData & {
  hovered?: boolean;
  onDelete?: (data?: CanvasEdgeData) => void;
  onInsert?: (data: CanvasEdgeData) => void;
  selected?: boolean;
  /** The edge leaves or enters the selected step: it stands out (Connections › Kinds). */
  stepSelected?: boolean;
};

export type SearchResult = {
  id: string;
  kind: "group" | "step";
  label: string;
  secondary: string;
};

/** The canvas toast. One action (Undo) keeps it 6s; an error stays until dismissed. */
export type GraphEditNotice = {
  label: string;
  tone: "error" | "saving" | "success";
  action?: { label: string; onAction: () => void };
};

let suppressCanvasNodeClickUntil = 0;

export function suppressCanvasNodeClick(durationMs = 250) {
  suppressCanvasNodeClickUntil = Date.now() + durationMs;
}

export function consumeCanvasNodeClickSuppression() {
  if (Date.now() > suppressCanvasNodeClickUntil) {
    return false;
  }
  suppressCanvasNodeClickUntil = 0;
  return true;
}

export function manualLayoutStorageKey(workflowId: string) {
  return `platform.workflowCanvas.manualLayout.${workflowId}`;
}

export function loadManualLayout(workflowId: string): Record<string, CanvasPosition> {
  if (globalThis.window === undefined) {
    return {};
  }
  try {
    const raw = globalThis.localStorage.getItem(manualLayoutStorageKey(workflowId));
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    return Object.fromEntries(
      Object.entries(parsed).filter(([, value]) => {
        if (!value || typeof value !== "object" || Array.isArray(value)) {
          return false;
        }
        const position = value as Partial<CanvasPosition>;
        return Number.isFinite(position.x) && Number.isFinite(position.y);
      }),
    ) as Record<string, CanvasPosition>;
  } catch {
    return {};
  }
}

export function saveManualLayout(workflowId: string, positions: Record<string, CanvasPosition>) {
  if (globalThis.window === undefined) {
    return;
  }
  const key = manualLayoutStorageKey(workflowId);
  if (!Object.keys(positions).length) {
    globalThis.localStorage.removeItem(key);
    return;
  }
  globalThis.localStorage.setItem(key, JSON.stringify(positions));
}

export function isStepNodeType(nodeType: string | undefined) {
  return [
    "step",
    "action",
    "start",
    "end",
    "ifStep",
    "switchStep",
    "forEach",
    "batch",
  ].includes(firstStringFromUnknownValue([nodeType, ""]));
}

export function isStepNode(node: Node | undefined | null): node is Node<StepNodeData> {
  return Boolean(node && isStepNodeType(node.type));
}

export function nodeSize(node: Node) {
  return {
    height: Number(node.height || node.style?.height || 0),
    width: Number(node.width || node.style?.width || 0),
  };
}

export function absoluteNodeCenter(
  node: Node,
  nodesById: Map<string, Node>,
) {
  const size = nodeSize(node);
  let x = node.position.x + size.width / 2;
  let y = node.position.y + size.height / 2;
  const seen = new Set([node.id]);
  let parent = node.parentId ? nodesById.get(node.parentId) : undefined;
  while (parent && !seen.has(parent.id)) {
    seen.add(parent.id);
    x += parent.position.x;
    y += parent.position.y;
    parent = parent.parentId ? nodesById.get(parent.parentId) : undefined;
  }
  return { x, y };
}

export function measuredHeightsEqual(
  left: Record<string, number>,
  right: Record<string, number>,
) {
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  if (leftKeys.length !== rightKeys.length) {
    return false;
  }
  return leftKeys.every((key) => Math.abs((left[key] || 0) - (right[key] || 0)) <= 2);
}

export function normalizedSearchText(value: string | undefined | null) {
  return firstStringFromUnknownValue([value, ""]).trim().toLowerCase();
}

export function summaryBadge(summary: WorkflowCanvasScopeSummary | undefined) {
  const rules = Number(summary?.rule_count || 0);
  const issues = Number(summary?.issue_count || 0);
  const error_count = Number(summary?.error_count || 0);
  const warning_count = Number(summary?.warning_count || 0);
  return { error_count, issues, rules, warning_count };
}

export function isSecondaryNodeAction(target: EventTarget | null, point?: { clientX: number; clientY: number }) {
  const secondarySelector = "button,a,input,textarea,select,[data-canvas-secondary-action='true']";
  if (target instanceof Element && target.closest(secondarySelector)) {
    return true;
  }
  if (point && globalThis.document !== undefined) {
    return document
      .elementsFromPoint(point.clientX, point.clientY)
      .some((element) => element instanceof HTMLElement && Boolean(element.closest(secondarySelector)));
  }
  return false;
}

export function stableBranchEdgePath(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
) {
  const route = routeCanvasEdge({ source: { x: sourceX, y: sourceY }, target: { x: targetX, y: targetY }, obstacles: [], backward: targetX < sourceX, failure: false, repeat: false });
  return [route.path, route.midpoint.x, route.midpoint.y] as const;
}

const STEP_ICONS = {
  agent: Bot,
  connector: PlugZap,
  invoke_workflow: Workflow,
  http_request: Globe2,
  dataset_query: Table2,
  set_variable: Code2,
  transform: Sparkles,
  tabular_feed: Table2,
  file_from_base64: FileImage,
  template: FileText,
  if: GitBranch,
  switch: Shuffle,
  collect: ListPlus,
  continue: SkipForward,
  break: LogOut,
  batch: Layers3,
  approval: UserCheck,
  wait: Hourglass,
  stop_success: Milestone,
  stop_fail: CircleStop,
  for_each: Repeat2,
  group: Group,
  note: StickyNote,
};

/** System, extension and MCP tools are one connector kind, told apart by the marketplace's icons. */
const TOOL_CATEGORY_ICONS = {
  connector: Wrench,
  extension_tools: FileCode2,
  mcp_tools: Network,
};

const ACTION_KINDS = new Set(["agent", "connector", "invoke_workflow", "http_request"]);
const DATA_KINDS = new Set(["dataset_query", "set_variable", "collect", "transform", "tabular_feed", "file_from_base64", "template"]);

function familyIconClass(kind: string) {
  if (ACTION_KINDS.has(kind)) {
    return "bg-[var(--fs-workflow-family-action-tile)] text-[var(--fs-workflow-family-action-icon)]";
  }
  if (DATA_KINDS.has(kind)) {
    return "bg-[var(--fs-workflow-family-data-tile)] text-[var(--fs-workflow-family-data-icon)]";
  }
  if (kind === "stop_success") {
    return "bg-[var(--fs-status-success-bg)] text-[var(--fs-status-success)]";
  }
  if (kind === "stop_fail") {
    return "bg-[var(--fs-status-danger-bg)] text-[var(--fs-status-danger)]";
  }
  return "bg-[var(--fs-bg-surface-muted)] text-[var(--fs-text-secondary)]";
}

export function stepVisual(kind: string, toolCategory?: string) {
  const toolIcon = kind === "connector" && toolCategory && Object.hasOwn(TOOL_CATEGORY_ICONS, toolCategory)
    ? TOOL_CATEGORY_ICONS[toolCategory as keyof typeof TOOL_CATEGORY_ICONS]
    : undefined;
  return {
    Icon: toolIcon ?? (Object.hasOwn(STEP_ICONS, kind)
      ? STEP_ICONS[kind as keyof typeof STEP_ICONS]
      : Sparkles),
    cardClass: "border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)]",
    titleClass: "text-[var(--fs-text-primary)]",
    kindClass: "text-[var(--fs-text-secondary)]",
    iconClass: familyIconClass(kind),
    shapeClass: "rounded-lg",
  };
}

export function validationRingClass(summary: ReturnType<typeof summaryBadge>) {
  if (summary.issues && Number(summary.error_count || 0)) {
    return "ring-2 ring-[var(--fs-status-danger-border)]";
  }
  if (summary.issues) {
    return "ring-2 ring-[var(--fs-status-attention-border)]";
  }
  return "";
}

export function automationRuleTitle(
  rules: WorkflowCanvasAutomationRule[],
  summary: ReturnType<typeof summaryBadge>, t: I18nContextValue["t"] = fallbackContext.t
) {
  if (rules.length) {
    return rules
      .map((rule) =>
        [
          rule.display_name || rule.reaction_key || t("react.workflows.automation_rule", "Automation rule"),
          rule.event_type,
          rule.action_type,
        ]
          .filter(isTruthy)
          .join(" · "),
      )
      .join("\n");
  }
  if (summary.rules) {
    return t("react.workflows.value0_automation_rule_s_configured", "{value0} automation rule(s) configured", { value0: summary.rules });
  }
  return t("react.workflows.open_automation", "Open automation");
}

export function validationIssueTargetLabel(
  issue: WorkflowCanvasIssue,
  graph: WorkflowCanvasGraph, t: I18nContextValue["t"] = fallbackContext.t
) {
  if (issue.scope === "step") {
    const step = graph.steps.find(
      (candidate) =>
        candidate.id === issue.scope_id ||
        candidate.id === issue.step_id ||
        candidate.step_id === issue.step_id,
    );
    if (step?.display_name) {
      const name = t("react.workflows.step_value0", "step: {value0}", { value0: step.display_name });
      return step.step_id ? `${name} (${step.step_id})` : name;
    }
    if (step?.step_id) return `step_id: ${step.step_id}`;
  }
  if (issue.scope === "group") {
    const group = (graph.groups || []).find(
      (candidate) => candidate.id === issue.scope_id || candidate.id === issue.group_id,
    );
    if (group) {
      return `${group.display_name} (${group.id})`;
    }
  }
  return t("react.workflows.workflow_value0", "workflow: {value0}", { value0: graph.workflow.id });
}

/** The producer names a missing case; only the current graph may supply its business label. */
export function validationIssueActionLabel(
  issue: WorkflowCanvasIssue,
  graph: WorkflowCanvasGraph,
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  const fallback = issue.suggested_action_key
    ? t(issue.suggested_action_key, issue.suggested_action || "") : issue.suggested_action || "";
  if (issue.code !== "workflow.graph.outcome_missing" || issue.scope !== "step") return fallback;
  const step = graph.steps.find((candidate) => candidate.id === issue.scope_id || candidate.step_id === issue.step_id);
  if (!step) return fallback;
  const labels = (issue.issues || []).flatMap((row) => {
    if (row.outcome_name !== "case" || !row.outcome_case_id) return [];
    const port = (step.ports || []).find((candidate) => candidate.outcome.name === "case" &&
      candidate.outcome.case_id === row.outcome_case_id);
    return port?.label ? [port.label] : [];
  });
  if (!labels.length) return fallback;
  return t("react.workflows.diagnostic_action_missing_switch_cases", "Connect Switch case {cases} in Connections.", {
    cases: [...new Set(labels)].join(", "),
  });
}

export const hiddenHandleStyle = {
  background: "transparent",
  border: "none",
  height: 10,
  opacity: 0,
  width: 10,
} as const;

export const hiddenRuntimeHandleStyle = {
  ...hiddenHandleStyle,
  bottom: -5,
} as const;

export function runtimeHandleStyle(role: "source" | "target", kind: string) {
  return {
    background: role === "target" || kind === "start" ? "var(--fs-bg-surface)" : "var(--fs-workflow-edge)",
    border: "1.5px solid var(--fs-workflow-edge)",
    height: 10,
    width: 10,
  } as const;
}

export const branchHandleStyle = {
  background: "var(--fs-workflow-edge)",
  border: "1px solid var(--fs-workflow-edge)",
  bottom: 0,
  height: 14,
  width: 14,
} as const;

export function canvasPortKey(source: WorkflowNodeRef, outcome: WorkflowOutcome) { return `${source.kind}:${source.id}:${outcomeHandleId(outcome)}`; }
