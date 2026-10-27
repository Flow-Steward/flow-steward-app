import type { ContainerRuntimeMark, ContainerRuntimeMarks } from "./runtime-container-marks";
import { runtimeEdgeKey } from "@/pages/jobs/detail/job-execution-trace";
import { columnsFor } from "./canvas-layout-ranking";
import { wrapRootColumns } from "./canvas-root-wrap";
import type { CanvasEdgeRoute } from "./canvas-edge-routing";
import type { HookExecutionMark, HookExecutionMarks } from "@/pages/workflows/detail/detail-hook-execution-marks";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { Position, type Edge, type Node } from "@xyflow/react";

import type {
  WorkflowCanvasAutomationRule,
  WorkflowCanvasForEach,
  WorkflowCanvasGraph,
  WorkflowContractSummary,
  WorkflowCanvasGroup,
  WorkflowCanvasIssue,
  WorkflowCanvasPort,
  WorkflowCanvasStep,
  WorkflowGraphEdge,
  WorkflowNodeRef,
  WorkflowOutcome,
  WorkflowTargetRef,
} from "@/pages/workflows/shared/api";
import { edgeKey, outcomeHandleId } from "@/pages/workflows/builder/api";
import type { NewContainerDefaults } from "@/pages/workflows/builder/canvas-types";
import { parseHookNodeId } from "@/pages/workflows/builder/hook-frames";
import { errorPolicyToken, groupPorts } from "@/pages/workflows/builder/change-set";
import {
  CANVAS_GRID,
  ACTION_STEP_WIDTH,
  START_WIDTH,
  LANE_CONTROL_SPACE,
  LANE_ADD_CONTROL_HEIGHT,
  CANVAS_MARGIN_X,
  CANVAS_MARGIN_Y,
  COLUMN_GAP,
  GROUP_HEADER_HEIGHT,
  CONTAINER_PORT_CENTER_Y,
  GROUP_MIN_HEIGHT,
  GROUP_MIN_WIDTH,
  GROUP_PADDING_BOTTOM,
  GROUP_PADDING_X,
  IF_NODE_HEIGHT,
  IF_STEP_WIDTH,
  MEASURED_HEIGHT_BUFFER,
  ROW_GAP,
  STEP_MIN_HEIGHT,
  STEP_WIDTH,
  SWITCH_FOOTER_HEIGHT,
  SWITCH_HEADER_HEIGHT,
  SWITCH_ROW_HEIGHT,
  SWITCH_STEP_WIDTH,
  TERMINAL_STEP_MIN_HEIGHT,
  TERMINAL_STEP_WIDTH,
} from "@/pages/workflows/builder/layout-constants";

import { declaredSignatures, schemaSignatures, orderedStepPorts, outcomePortBottom, hasOutcomeRows, type ContractSignature } from "./canvas-card-presentation";
import { INLINE_LOOP_WIDTH, inlineLoopBody, inlineLoopEligible, inlineLoopHeight, type InlineLoopBodyStep } from "./canvas-inline-loop-layout";

/**
 * The builder canvas layout.
 *
 * Root steps, Groups and root For each containers are top-level nodes. A
 * Group runs its body once; a For each runs its body once per item. Each
 * container holds only its own members. Hook frames add visual containment
 * around a connected workflow without changing persisted ownership. Positions are
 * derived from the execution graph for reading and may be moved by hand; a
 * position never changes what runs next.
 */

export type LaneControlBounds = { x: number; y: number; width: number; height: number };
export type LaneFrame = { id: string; x: number; y: number; width: number; height: number; addControl?: LaneControlBounds };

export type CanvasConnectionTargetState = "eligible" | "active-valid" | "active-invalid";

export type GroupNodeData = {
  connectionTargetState?: CanvasConnectionTargetState;
  laneFrames?: LaneFrame[];
  runtimeMark?: HookExecutionMark;
  containerRuntimeMark?: ContainerRuntimeMark;
  readOnly?: boolean;
  errorConnected?: boolean;
  errorPolicy?: string;
  automationRules: WorkflowCanvasAutomationRule[];
  connectTargetActive?: boolean;
  group: WorkflowCanvasGroup;
  highlighted?: boolean;
  isEntry: boolean;
  lanes: string[];
  local: boolean;
  ports: WorkflowCanvasPort[];
  unreachable: boolean;
  validationIssues: WorkflowCanvasIssue[];
  onAutomation: (groupId: string) => void;
  onOpenGroup: (groupId: string, defaultTab?: string) => void;
  onOpenNewStep?: (context: { groupId: string; laneId: string }) => void;
  onOpenNewGroup?: (defaults: NewContainerDefaults) => void;
};

export type ForEachNodeData = {
  connectionTargetState?: CanvasConnectionTargetState;
  laneFrames?: LaneFrame[];
  runtimeMark?: HookExecutionMark;
  containerRuntimeMark?: ContainerRuntimeMark;
  readOnly?: boolean;
  errorConnected?: boolean;
  errorPolicy?: string;
  automationRules: WorkflowCanvasAutomationRule[];
  forEach: WorkflowCanvasForEach;
  highlighted?: boolean;
  isEntry: boolean;
  lanes: string[];
  local: boolean;
  ports: WorkflowCanvasPort[];
  unreachable: boolean;
  validationIssues: WorkflowCanvasIssue[];
  onOpenForEach: (forEachId: string, defaultTab?: string) => void;
  onOpenNewStep?: (context: { groupId: string; laneId: string; forEachId?: string }) => void;
  /** Set when the For each is drawn as an inline loop card: its body, in walk order. */
  bodySteps?: InlineLoopBodyStep[];
  onOpenStep?: (stepId: string) => void;
};

/** A step that ends its group's body: the end circle beside the port it leaves from. */
export type StepBodyEnd = { handle: string; kind: "item" | "lane" | "group" | "hook"; x: number; y: number };

/** What the workflow returns: variable outputs and returned files (`artifacts`), by name. */
export type ContractSummary = { unavailable?: boolean; outputs: string[]; artifacts: string[]; outputSignatures?: ContractSignature[]; artifactSignatures?: ContractSignature[] };

export type StepNodeData = {
  /** Stable project/workflow/Hook command owner, for transient card menus. */
  actionScopeKey?: string;
  connectionTargetState?: CanvasConnectionTargetState;
  runtimeMark?: HookExecutionMark;
  readOnly?: boolean;
  /** Handle ids of the routed outcomes that lead nowhere yet; each offers "add a step after". */
  freeOutcomes?: string[];
  onAppendStep?: (source: WorkflowNodeRef, outcome: WorkflowOutcome) => void;
  contractSummary?: ContractSummary;
  outcomeTargets?: Record<string, string>;
  errorConnected?: boolean;
  errorPolicy?: string;
  automationRules: WorkflowCanvasAutomationRule[];
  highlighted?: boolean;
  isEntry: boolean;
  local: boolean;
  step: WorkflowCanvasStep;
  unreachable: boolean;
  validationIssues: WorkflowCanvasIssue[];
  onAutomation: (stepId: string) => void;
  onOpenStep: (stepId: string) => void;
  onEditStep: (stepId: string) => void;
  /** "Move to…": the editor at Placement; absent where the page offers none. */
  onPlaceStep?: (stepId: string) => void;
  onOpenStepTestRun?: (stepId: string) => boolean | void | Promise<boolean | void>;
  onOpenInterfaceSection?: (tab: "outputs") => void;
  onDeleteStep?: (canvasStepId: string) => void;
  /** Outcomes that end the body of the step's group: drawn as end circles, not as lines to the exit. */
  bodyEnds?: StepBodyEnd[];
};

export type StartNodeData = {
  entryNodeId?: string;
  hasEntry?: boolean;
  inputSignatures?: ContractSignature[];
  readOnly?: boolean;
  contractUnavailable?: boolean;
  inputMode?: string;
  workflowId: string;
  projectHint?: string;
  triggerChannels: string[];
  inputs: string[];
  workflowEnabled: boolean;
  onOpenInputs?: () => void;
  onAddStep?: () => void;
  empty: boolean;
};

export type CanvasEdgeData = {
  taken?: boolean;
  /** How often the run took this connection, when more than once (a loop). */
  passes?: number;
  route?: CanvasEdgeRoute;
  /** Logical layout metadata, never inferred from hand-placed screen coordinates. */
  routing?: { sameScope: boolean; sourceColumn?: number; targetColumn?: number; longJump: boolean };
  draft: boolean;
  edge: WorkflowGraphEdge;
  edgeKey: string;
  failurePath: boolean;
  label: string;
  rework: boolean;
};

type CanvasNodeHandle = NonNullable<Node["handles"]>[number];

export function nodeIdForRef(ref: WorkflowNodeRef): string {
  if (ref.kind === "group") return `group:${ref.id}`;
  if (ref.kind === "for_each") return `for_each:${ref.id}`;
  return ref.id;
}

function targetNodeId(target: WorkflowTargetRef): string {
  if (target.kind === "group_exit") {
    return `group:${target.group_id}`;
  }
  if (target.kind === "for_each_exit") {
    return `for_each:${target.for_each_id}`;
  }
  return nodeIdForRef(target);
}

function targetHandleId(target: WorkflowTargetRef): string {
  if (target.kind === "group_exit" || target.kind === "for_each_exit") {
    return "exit";
  }
  return target.kind === "group" || target.kind === "for_each" ? "entry" : "in";
}

export function stepNodeWidth(step: Pick<WorkflowCanvasStep, "kind">) {
  if (step.kind === "stop_success" || step.kind === "stop_fail") {
    return TERMINAL_STEP_WIDTH;
  }
  if (step.kind === "if") {
    return IF_STEP_WIDTH;
  }
  if (step.kind === "switch") {
    return SWITCH_STEP_WIDTH;
  }
  if (step.kind === "batch") {
    return STEP_WIDTH;
  }
  return ACTION_STEP_WIDTH;
}

function stepPorts(step: WorkflowCanvasStep): WorkflowCanvasPort[] {
  const ports = Array.isArray(step.ports) ? step.ports : [];
  if (["stop_success", "stop_fail", "break", "continue"].includes(step.kind)) return [];
  return ports.some((port) => port.outcome.name === "error") ? ports : [...ports, { outcome: { name: "error" }, label: "On error" }];
}

export function stepNodeHeight(step: WorkflowCanvasStep, measuredHeight?: number) {
  const measured = Number.isFinite(measuredHeight)
    ? Math.ceil(Number(measuredHeight) + MEASURED_HEIGHT_BUFFER)
    : 0;
  const clampMeasured = (estimated: number) => Math.ceil(Math.max(estimated, measured) / CANVAS_GRID) * CANVAS_GRID;
  const ports = stepPorts(step).filter((port) => port.outcome.name !== "error");
  if (step.kind === "if") {
    return clampMeasured(IF_NODE_HEIGHT);
  }
  if (step.kind === "switch") {
    const rows = Math.max(ports.length, 1);
    return clampMeasured(SWITCH_HEADER_HEIGHT + rows * SWITCH_ROW_HEIGHT + SWITCH_FOOTER_HEIGHT);
  }
  if (step.kind === "stop_success" || step.kind === "stop_fail") {
    const summaryRows = step.config_summary && step.config_summary.length > 52 ? 1 : 0;
    return clampMeasured(TERMINAL_STEP_MIN_HEIGHT + summaryRows * 14);
  }
  const portRows = ports.length > 1 ? Math.ceil(ports.length / 2) : 0;
  const summaryRows = step.config_summary && step.config_summary.length > 68 ? 2 : 1;
  const estimated = Math.min(420, STEP_MIN_HEIGHT + portRows * 24 + summaryRows * 16);
  return clampMeasured(estimated);
}

export function canvasNodeType(step: WorkflowCanvasStep) {
  if (step.node_type && step.node_type !== "approval" && step.node_type !== "wait") {
    return step.node_type;
  }
  if (step.kind === "if") return "ifStep";
  if (step.kind === "switch") return "switchStep";
  if (step.kind === "batch") return "batch";
  if (step.kind === "stop_success" || step.kind === "stop_fail") return "end";
  return "action";
}

/** Where a step's port handles sit; the node components render the same ids. */
export function stepPortHandles(step: WorkflowCanvasStep, width: number, height: number): CanvasNodeHandle[] {
  const ports = stepPorts(step);
  const routed = orderedStepPorts(step.kind, ports);
  const handles: CanvasNodeHandle[] = routed.map((port, index) => ({
    id: outcomeHandleId(port.outcome), position: Position.Right, type: "source",
    width: 10, height: 10, x: width - 5,
    y: hasOutcomeRows(step.kind, routed.length) ? height - outcomePortBottom(routed.length, index) - 5 : 19,
  }));
  if (ports.some((port) => port.outcome.name === "error")) {
    handles.push({
      id: "error",
      position: Position.Bottom,
      type: "source",
      width: 10,
      height: 10,
      x: width * 0.88 - 5,
      y: height - 5,
    });
  }
  return handles;
}

function stepNodeHandles(step: WorkflowCanvasStep, height: number): CanvasNodeHandle[] {
  const width = stepNodeWidth(step);
  return [
    {
      id: "in",
      position: Position.Left,
      type: "target",
      width: 10,
      height: 10,
      x: -5,
      y: 19,
    },
    ...stepPortHandles(step, width, height),
  ];
}

/** An inline loop card has a step's ports: input and Next 24 below the top, On error at the bottom. */
function inlineLoopHandles(width: number, height: number, ports: WorkflowCanvasPort[]): CanvasNodeHandle[] {
  const handles: CanvasNodeHandle[] = [
    { id: "entry", position: Position.Left, type: "target", width: 10, height: 10, x: -5, y: 19 },
    { id: "exit", position: Position.Bottom, type: "target", width: 10, height: 10, x: width / 2 - 5, y: height - 5 },
    { id: "success", position: Position.Right, type: "source", width: 10, height: 10, x: width - 5, y: 19 },
  ];
  if (ports.some((port) => port.outcome.name === "error")) {
    handles.push({ id: "error", position: Position.Bottom, type: "source", width: 10, height: 10, x: width * 0.88 - 5, y: height - 5 });
  }
  return handles;
}

function groupNodeHandles(width: number, height: number, ports: WorkflowCanvasPort[]): CanvasNodeHandle[] {
  const handles: CanvasNodeHandle[] = [
    { id: "entry", position: Position.Left, type: "target", width: 10, height: 10, x: -5, y: CONTAINER_PORT_CENTER_Y - 5 },
    { id: "exit", position: Position.Bottom, type: "target", width: 10, height: 10, x: width / 2 - 5, y: height - 5 },
    { id: "success", position: Position.Right, type: "source", width: 10, height: 10, x: width - 5, y: CONTAINER_PORT_CENTER_Y - 5 },
  ];
  if (ports.some((port) => port.outcome.name === "error")) {
    handles.push({ id: "error", position: Position.Bottom, type: "source", width: 10, height: 10, x: width - 29, y: height - 5 });
  }
  return handles;
}

type Rank = Map<string, number>;

/** Full scope reachability is independent of the rank-only acyclic graph. */
function reachableFrom(entryId: string, nodeIds: readonly string[], adjacency: ReadonlyMap<string, readonly string[]>): Set<string> {
  const inside = new Set(nodeIds);
  const reached = new Set<string>();
  const pending = inside.has(entryId) ? [entryId] : [];
  for (const id of pending) {
    if (reached.has(id)) continue;
    reached.add(id);
    for (const target of adjacency.get(id) ?? []) if (inside.has(target) && !reached.has(target)) pending.push(target);
  }
  return reached;
}

type SizedNode = { id: string; width: number; height: number };

/** One stable, left-to-right outcome-ordering pass within an actual scope. */
function gridPositions(nodes: SizedNode[], rank: Rank, edges: readonly WorkflowGraphEdge[], portY: (node: SizedNode, handle: string) => number | undefined) {
  const columns = new Map<number, SizedNode[]>();
  for (const node of nodes) {
    const column = rank.get(node.id) ?? 0;
    columns.set(column, [...(columns.get(column) ?? []), node]);
  }
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const positions = new Map<string, { x: number; y: number }>();
  let x = 0;
  let maxBottom = 0;
  for (const column of [...columns.keys()].sort((a, b) => a - b)) {
    const members = columns.get(column)!;
    const incoming = new Map<string, number[]>();
    for (const edge of edges) {
      if (edge.rework || edge.target.kind === "group_exit" || edge.target.kind === "for_each_exit") continue;
      const source = byId.get(nodeIdForRef(edge.source));
      const target = nodeIdForRef(edge.target);
      if (!source || !byId.has(target) || rank.get(source.id) !== column - 1 || rank.get(target) !== column) continue;
      const sourcePosition = positions.get(source.id);
      const offset = portY(source, outcomeHandleId(edge.outcome));
      if (!sourcePosition || offset === undefined) continue;
      incoming.set(target, [...(incoming.get(target) ?? []), sourcePosition.y + offset]);
    }
    const mean = (id: string) => { const values = incoming.get(id)!; return values.reduce((a, b) => a + b, 0) / values.length; };
    const keyed = members.filter((node) => incoming.has(node.id)).sort((a, b) => mean(a.id) - mean(b.id));
    let keyedIndex = 0;
    const ordered = members.map((node) => incoming.has(node.id) ? keyed[keyedIndex++] : node);
    let y = 0;
    for (const member of ordered) {
      positions.set(member.id, { x, y });
      maxBottom = Math.max(maxBottom, y + member.height);
      y += member.height + ROW_GAP;
    }
    x += Math.max(...members.map((node) => node.width)) + COLUMN_GAP;
  }
  return { height: maxBottom, positions, width: Math.max(0, x - COLUMN_GAP) };
}

function groupByScope<T extends { scope: string; scope_id: string }>(rows: T[]) {
  const grouped = new Map<string, T[]>();
  rows.forEach((row) => {
    const key = `${row.scope}:${row.scope_id}`;
    grouped.set(key, [...(grouped.get(key) || []), row]);
  });
  return grouped;
}

type CanvasContainer = WorkflowCanvasGroup | WorkflowCanvasForEach;

function bodyEntryRef(group: CanvasContainer, laneId = ""): Record<string, unknown> | undefined {
  const body = group.body || {};
  if (laneId) {
    const lanes = Array.isArray(body.lanes) ? body.lanes : [];
    const lane = lanes.find((item) => (item as Record<string, unknown>)?.lane_id === laneId) as
      | Record<string, unknown>
      | undefined;
    return lane?.entry as Record<string, unknown> | undefined;
  }
  return body.entry as Record<string, unknown> | undefined;
}

/** The canvas node id a body or lane starts at, including virtual nested Groups. */
function bodyEntryNodeId(group: CanvasContainer, laneId = ""): string {
  const entry = bodyEntryRef(group, laneId);
  if (typeof entry?.id !== "string") return "";
  return nodeIdForRef(entry as WorkflowNodeRef);
}

function groupLaneIds(group: CanvasContainer): string[] {
  const lanes = Array.isArray(group.body?.lanes) ? group.body.lanes : [];
  return lanes
    .map((lane) => (lane as Record<string, unknown>)?.lane_id)
    .filter((laneId): laneId is string => typeof laneId === "string" && Boolean(laneId));
}

function edgeColor(edge: WorkflowGraphEdge) {
  return edge.outcome.name === "error" ? "var(--fs-workflow-edge-failure)" : "var(--fs-workflow-edge)";
}

export function buildCanvasLayout(
  {
    canvasGraph,
    actionScopeKey,
    readOnly = false,
    structureLocked = false,
    archivedContract,
    draftEdgeKeys = new Set<string>(),
    localNodeIds = new Set<string>(),
    measuredStepHeights,
    runtimeMarks = {},
    runtimeEdges = new Set<string>(),
    containerRuntimeMarks = {},
    onOpenGroup,
    onOpenGroupAutomation,
    onOpenForEach = onOpenGroup,
    onOpenNewStep,
    onOpenNewGroup,
    onAppendStep,
    onOpenStep,
    onEditStep = onOpenStep,
    onPlaceStep,
    onOpenStepTestRun,
    stepTestRunAllowed,
    onOpenStopInterfaceSection,
    onDeleteStep,
    onOpenStepAutomation,
    onOpenInputs,
    projectHint,
    inlineLoopIds,
  }: {
    canvasGraph: WorkflowCanvasGraph;
    actionScopeKey?: string;
    /** An archived job definition: no authoring and the archived contract. */
    readOnly?: boolean;
    /** A protected author graph: the live definition, shown without authoring controls. */
    structureLocked?: boolean;
    archivedContract?: WorkflowContractSummary | null;
    draftEdgeKeys?: Set<string>;
    localNodeIds?: Set<string>;
    measuredStepHeights?: Record<string, number>;
    runtimeMarks?: HookExecutionMarks;
    /** Connections the run took; a map also says how often. */
    runtimeEdges?: ReadonlySet<string> | ReadonlyMap<string, number>;
    containerRuntimeMarks?: ContainerRuntimeMarks;
    onOpenGroup: (groupId: string, defaultTab?: string) => void;
    onOpenGroupAutomation: (groupId: string) => void;
    onOpenForEach?: (forEachId: string, defaultTab?: string) => void;
    onOpenNewGroup?: (defaults: NewContainerDefaults) => void;
    onOpenNewStep?: (context: { groupId: string; laneId: string; forEachId?: string }) => void;
    /** Add a step after one free outcome of a step: created already connected to it. */
    onAppendStep?: (source: WorkflowNodeRef, outcome: WorkflowOutcome) => void;
    onOpenStep: (stepId: string) => void;
    onEditStep?: (stepId: string) => void;
    onPlaceStep?: (stepId: string) => void;
    onOpenStepTestRun?: (stepId: string) => boolean | void | Promise<boolean | void>;
    stepTestRunAllowed?: (stepId: string) => boolean;
    onOpenStopInterfaceSection?: (tab: "outputs") => void;
    onDeleteStep?: (canvasStepId: string) => void;
    onOpenStepAutomation: (stepId: string) => void;
    onOpenInputs?: () => void;
    projectHint?: string;
    /** For each ids drawn as inline loop cards (canvas presentation only). */
    inlineLoopIds?: ReadonlySet<string>;
  },
  t: I18nContextValue["t"] = fallbackContext.t,
) {
  const nodeReadOnly = readOnly || structureLocked;
  // The summary deliberately whitelists names, never field defaults or arbitrary metadata.
  let inputs: string[];
  if (readOnly) {
    inputs = (archivedContract?.inputs || []).map((field) => `${field.name}${field.required ? "*" : ""}`);
  } else if (canvasGraph.interface_contract.input_mode === "json_schema") {
    inputs = Object.keys(canvasGraph.interface_contract.input_schema?.properties || {}).map((name) =>
      `${name}${Array.isArray(canvasGraph.interface_contract.input_schema?.required) && canvasGraph.interface_contract.input_schema.required.includes(name) ? "*" : ""}`);
  } else {
    inputs = (canvasGraph.interface_contract.expected_inputs || []).map((field) => `${field.name}${field.required ? "*" : ""}`);
  }
  const inputSignatures: ContractSignature[] = readOnly
    ? (archivedContract?.inputs || []).map(({ name, required }) => ({ name, required }))
    : canvasGraph.interface_contract.input_mode === "json_schema"
      ? schemaSignatures(canvasGraph.interface_contract.input_schema || {})
      : declaredSignatures(canvasGraph.interface_contract.expected_inputs || []);
  const contractSummary: ContractSummary = readOnly ? {
    unavailable: !archivedContract,
    outputs: archivedContract?.outputs || [], artifacts: archivedContract?.artifacts || [],
  } : {
    outputSignatures: canvasGraph.output_contract.output_mode === "none" ? [] : declaredSignatures(canvasGraph.output_contract.expected_outputs || []),
    artifactSignatures: declaredSignatures(canvasGraph.output_contract.returned_files || []),
    outputs: canvasGraph.output_contract.output_mode === "none" ? [] : (canvasGraph.output_contract.expected_outputs || []).map((field) => field.name),
    artifacts: (canvasGraph.output_contract.returned_files || []).map((field) => typeof field.name === "string" ? field.name : "").filter(Boolean),
  };
  const startHeight = Math.ceil((Math.max(180, measuredStepHeights?.["visual:start"] || 0) + MEASURED_HEIGHT_BUFFER) / CANVAS_GRID) * CANVAS_GRID;
  const rootOffset = 0;
  const validationIssuesByScope = groupByScope(canvasGraph.validation_issues || []);
  const automationRulesByScope = groupByScope(canvasGraph.automation_rules || []);
  const steps = canvasGraph.steps || [];
  const groups = canvasGraph.groups || [];
  const forEachItems = canvasGraph.for_each || [];
  const edges = canvasGraph.edges || [];
  const portsByStep = new Map(steps.map((step) => [step.id, stepPorts(step)]));

  const sizeOfStep = (step: WorkflowCanvasStep): SizedNode => ({
    id: step.id,
    width: stepNodeWidth(step),
    height: stepNodeHeight(step, measuredStepHeights?.[step.id]),
  });

  const adjacency = new Map<string, string[]>();
  edges.forEach((edge) => {
    if (edge.rework || edge.target.kind === "group_exit" || edge.target.kind === "for_each_exit") return;
    const sourceId = nodeIdForRef(edge.source);
    const targetId = targetNodeId(edge.target);
    adjacency.set(sourceId, [...(adjacency.get(sourceId) || []), targetId]);
  });

  const stepsById = new Map(steps.map((step) => [step.id, step]));
  // The exact same handle geometry feeds rendering, edge endpoints and ordering.
  const portY = (node: SizedNode, handleId: string) => {
    const step = stepsById.get(node.id);
    const handles = step ? stepPortHandles(step, node.width, node.height) : groupNodeHandles(node.width, node.height, [{ outcome: { name: "error" }, label: "" }]);
    const handle = handles.find((item) => item.type === "source" && item.id === handleId);
    return handle ? handle.y + (handle.height ?? 10) / 2 : undefined;
  };

  type ContainerLayout = {
    height: number;
    positions: Map<string, { x: number; y: number }>;
    reached: Set<string>;
    laneFrames: LaneFrame[];
    width: number;
  };
  // Lay out one container's members (steps, and For each nodes inside a
  // sequence Group) so the container knows its own size.
  const routingColumns = new Map<string, number>();
  const layoutContainer = (container: CanvasContainer, members: SizedNode[], laneOf: (id: string) => string): ContainerLayout => {
    const lanes = groupLaneIds(container);
    const positions = new Map<string, { x: number; y: number }>();
    const reached = new Set<string>();
    const laneFrames: LaneFrame[] = [];
    let width = 0;
    let y = GROUP_HEADER_HEIGHT + GROUP_PADDING_X;
    const rows: Array<{ laneId: string; members: SizedNode[] }> = lanes.length
      ? lanes.map((laneId) => ({ laneId, members: members.filter((node) => laneOf(node.id) === laneId) }))
      : [{ laneId: "", members }];
    const orphanMembers = lanes.length ? members.filter((node) => !lanes.includes(laneOf(node.id))) : [];
    if (orphanMembers.length) {
      rows.push({ laneId: "", members: orphanMembers });
    }
    rows.forEach((row) => {
      const ids = row.members.map((node) => node.id);
      const rank = columnsFor(bodyEntryNodeId(container, row.laneId), ids, adjacency);
      reachableFrom(bodyEntryNodeId(container, row.laneId), ids, adjacency).forEach((id) => reached.add(id));
      rank.forEach((column, id) => routingColumns.set(id, column));
      const grid = gridPositions(row.members, rank, edges, portY);
      grid.positions.forEach((position, id) => {
        positions.set(id, { x: GROUP_PADDING_X + position.x, y: y + position.y });
      });
      if (row.laneId) laneFrames.push({ id: row.laneId, x: GROUP_PADDING_X - 6, y: y - 6, width: Math.max(grid.width, ACTION_STEP_WIDTH) + 12, height: Math.max(grid.height, STEP_MIN_HEIGHT / 2) + 12 + LANE_CONTROL_SPACE, addControl: { x: GROUP_PADDING_X, y: y + Math.max(grid.height, STEP_MIN_HEIGHT / 2) + 12, width: Math.max(grid.width, ACTION_STEP_WIDTH), height: LANE_ADD_CONTROL_HEIGHT } });
      width = Math.max(width, grid.width);
      y += Math.ceil((Math.max(grid.height, row.members.length ? 0 : STEP_MIN_HEIGHT / 2) + (row.laneId ? LANE_CONTROL_SPACE : 0) + ROW_GAP) / CANVAS_GRID) * CANVAS_GRID;
    });
    return {
      height: Math.ceil(Math.max(GROUP_MIN_HEIGHT, y - ROW_GAP + GROUP_PADDING_BOTTOM) / CANVAS_GRID) * CANVAS_GRID,
      positions,
      reached,
      laneFrames,
      width: Math.max(GROUP_MIN_WIDTH, width + GROUP_PADDING_X * 2),
    };
  };
  const containers = new Map<string, CanvasContainer>([
    ...groups.map((group): [string, CanvasContainer] => [`group:${group.id}`, group]),
    ...forEachItems.map((item): [string, CanvasContainer] => [`for_each:${item.id}`, item]),
  ]);
  const parentByNode = new Map<string, string>();
  groups.forEach((group) => {
    if (group.parent) parentByNode.set(`group:${group.id}`, nodeIdForRef(group.parent));
  });
  forEachItems.forEach((item) => {
    const parent = item.parent || (item.group_id ? { kind: "group" as const, id: item.group_id } : null);
    if (parent) parentByNode.set(`for_each:${item.id}`, nodeIdForRef(parent));
  });
  steps.forEach((step) => {
    if (step.for_each_id) parentByNode.set(step.id, `for_each:${step.for_each_id}`);
    else if (step.group_id) parentByNode.set(step.id, `group:${step.group_id}`);
  });
  // Invalid parents must not hide a node or create an infinite layout walk.
  parentByNode.forEach((parent, id) => {
    const path = new Set([id]);
    let next: string | undefined = parent;
    while (next) {
      if (!containers.has(next) || path.has(next)) {
        parentByNode.delete(id);
        break;
      }
      path.add(next);
      next = parentByNode.get(next);
    }
  });
  const laneById = new Map([
    ...steps.map((step): [string, string] => [step.id, step.lane_id || ""]),
    ...[...containers].map(([id, container]): [string, string] => [id, container.lane_id || ""]),
  ]);
  const laneOfNode = (id: string) => laneById.get(id) || "";
  const containerLayouts = new Map<string, ContainerLayout>();
  // Inline loops: a For each drawn as one step-sized card; its body is listed inside, not drawn.
  const inlineBodies = new Map<string, InlineLoopBodyStep[]>();
  forEachItems.forEach((item) => {
    if (inlineLoopIds?.has(item.id) && inlineLoopEligible(item, steps, forEachItems)) inlineBodies.set(`for_each:${item.id}`, inlineLoopBody(item, steps, edges));
  });
  const stepSizes = new Map(steps.map((step) => [step.id, sizeOfStep(step)]));
  const childrenByParent = new Map<string, string[]>();
  parentByNode.forEach((parent, id) => childrenByParent.set(parent, [...(childrenByParent.get(parent) || []), id]));
  // Measure deepest containers first, then give their complete size to their
  // parent. The hook and its owning workflow keep independent definitions.
  const sizeOfNode = (id: string): SizedNode => {
    const stepSize = stepSizes.get(id);
    if (stepSize) return stepSize;
    let layout = containerLayouts.get(id);
    const inlineBody = inlineBodies.get(id);
    if (!layout && inlineBody) {
      layout = { width: INLINE_LOOP_WIDTH, height: inlineLoopHeight(inlineBody.length, measuredStepHeights?.[id]), positions: new Map(), reached: new Set(childrenByParent.get(id) || []), laneFrames: [] };
      containerLayouts.set(id, layout);
    }
    if (!layout) {
      layout = layoutContainer(containers.get(id)!, (childrenByParent.get(id) || []).map(sizeOfNode), laneOfNode);
      containerLayouts.set(id, layout);
    }
    return { id, width: layout.width, height: layout.height };
  };
  const rootNodes = [...containers.keys(), ...stepSizes.keys()].filter((id) => !parentByNode.has(id)).map(sizeOfNode);
  const entryId = canvasGraph.entry ? nodeIdForRef(canvasGraph.entry) : "";
  const rootRank = columnsFor(entryId, rootNodes.map((node) => node.id), adjacency);
  const rootReached = reachableFrom(entryId, rootNodes.map((node) => node.id), adjacency);
  rootRank.forEach((column, id) => { if (rootNodes.some((node) => node.id === id)) routingColumns.set(id, column); });
  const rootGrid = gridPositions(rootNodes, rootRank, edges, portY);
  const rootWrap = wrapRootColumns({
    nodes: rootNodes,
    positions: rootGrid.positions,
    rank: rootRank,
    links: edges.map((edge) => ({ source: nodeIdForRef(edge.source), target: targetNodeId(edge.target), rework: Boolean(edge.rework) })),
    startHeight,
    startY: rootGrid.positions.get(entryId)?.y ?? 0,
  });
  const rootPositions = rootWrap?.positions ?? rootGrid.positions;
  const groupLayouts = new Map(groups.map((group) => [group.id, containerLayouts.get(`group:${group.id}`)!]));
  const forEachLayouts = new Map(forEachItems.map((item) => [item.id, containerLayouts.get(`for_each:${item.id}`)!]));
  const reached = new Map<string, boolean>();
  const nodeReachable = (id: string): boolean => {
    if (reached.has(id)) return reached.get(id)!;
    const parent = parentByNode.get(id);
    const value = parent ? nodeReachable(parent) && Boolean(containerLayouts.get(parent)?.reached.has(id)) : rootReached.has(id);
    reached.set(id, value);
    return value;
  };
  const positionOf = (id: string) => {
    const parent = parentByNode.get(id);
    return parent
      ? containerLayouts.get(parent)?.positions.get(id) || { x: GROUP_PADDING_X, y: GROUP_HEADER_HEIGHT }
      : rootPositions.get(id) || { x: 0, y: 0 };
  };
  const isEntry = (id: string) => {
    const parent = parentByNode.get(id);
    const container = parent ? containers.get(parent) : undefined;
    return container ? bodyEntryNodeId(container, laneOfNode(id)) === id : entryId === id;
  };
  const containerReadonly = (id: string) => parseHookNodeId(id)?.memberStepId ? readOnly : nodeReadOnly;
  const parentProps = (id: string) => parentByNode.has(id) ? { extent: "parent" as const, expandParent: true, parentId: parentByNode.get(id) } : {};
  const absolutePosition = (id: string) => {
    const position = positionOf(id);
    return parentByNode.has(id) ? position : { x: CANVAS_MARGIN_X + START_WIDTH + COLUMN_GAP + position.x, y: CANVAS_MARGIN_Y + position.y + rootOffset };
  };

  const nodes: Array<Node<GroupNodeData | ForEachNodeData | StepNodeData | StartNodeData>> = [];
  const entryPosition = rootPositions.get(entryId) || { x: 0, y: 0 };
  nodes.push({
    id: "visual:start", type: "workflowStart", draggable: false,
    data: { entryNodeId: rootNodes.some((node) => node.id === entryId) ? entryId : undefined, hasEntry: Boolean(canvasGraph.entry), inputSignatures, readOnly: nodeReadOnly, contractUnavailable: readOnly && !archivedContract, inputMode: readOnly ? archivedContract?.input_mode : undefined, onOpenInputs, onAddStep: () => onOpenNewStep?.({ groupId: "", laneId: "" }), empty: !rootNodes.length, inputs, triggerChannels: readOnly ? archivedContract?.trigger_channels || [] : canvasGraph.interface_contract.trigger_channels || [], workflowEnabled: canvasGraph.workflow.enabled, workflowId: canvasGraph.workflow.id, projectHint },
    position: { x: CANVAS_MARGIN_X + entryPosition.x, y: CANVAS_MARGIN_Y + entryPosition.y },
    width: START_WIDTH, height: startHeight, style: { width: START_WIDTH, height: startHeight },
    handles: [{ id: "entry", type: "source", position: Position.Right, x: START_WIDTH - 5, y: 19, width: 10, height: 10 }],
  });
  groups.forEach((group) => {
    const nodeId = `group:${group.id}`;
    const layout = groupLayouts.get(group.id)!;
    const groupReadOnly = containerReadonly(group.id);
    const ports = groupPorts(group, t);
    nodes.push({
      id: nodeId,
      type: "groupContainer",
      data: {
        readOnly: groupReadOnly,
        ...(runtimeMarks[nodeId] ? { runtimeMark: runtimeMarks[nodeId] } : {}),
        ...(containerRuntimeMarks[nodeId] ? { containerRuntimeMark: containerRuntimeMarks[nodeId] } : {}),
        automationRules: automationRulesByScope.get(`group:${group.id}`) || [],
        group,
        errorConnected: edges.some((edge) => edge.source.kind === "group" && edge.source.id === group.id && edge.outcome.name === "error"),
        errorPolicy: errorPolicyToken(group.error_policy) || "fail",
        isEntry: isEntry(nodeId),
        lanes: groupLaneIds(group),
        laneFrames: layout.laneFrames,
        local: localNodeIds.has(nodeId) || Boolean(group.hook && (group.hook.declarationPending || localNodeIds.has(group.hook.stepId))),
        ports,
        unreachable: Boolean(canvasGraph.entry) && !nodeReachable(nodeId),
        validationIssues: validationIssuesByScope.get(`group:${group.id}`) || [],
        onAutomation: onOpenGroupAutomation,
        onOpenGroup,
        onOpenNewStep,
        onOpenNewGroup,
      },
      dragHandle: ".group-drag-handle",
      draggable: !groupReadOnly,
      ...(structureLocked && !groupReadOnly ? { connectable: true } : {}),
      ...parentProps(nodeId),
      handles: groupNodeHandles(layout.width, layout.height, ports),
      height: layout.height,
      position: absolutePosition(nodeId),
      style: { width: layout.width, height: layout.height },
      width: layout.width,
    });
  });
  forEachItems.forEach((item) => {
    const nodeId = `for_each:${item.id}`;
    const layout = forEachLayouts.get(item.id)!;
    const forEachReadOnly = containerReadonly(item.id);
    const parent = parentByNode.get(nodeId);
    const ports = groupPorts(item, t);
    const inlineBody = inlineBodies.get(nodeId);
    nodes.push({
      id: nodeId,
      type: inlineBody ? "inlineLoop" : "forEachContainer",
      data: {
        readOnly: forEachReadOnly,
        ...(runtimeMarks[nodeId] ? { runtimeMark: runtimeMarks[nodeId] } : {}),
        ...(containerRuntimeMarks[nodeId] ? { containerRuntimeMark: containerRuntimeMarks[nodeId] } : {}),
        automationRules: [],
        forEach: item,
        errorConnected: edges.some((edge) => edge.source.kind === "for_each" && edge.source.id === item.id && edge.outcome.name === "error"),
        errorPolicy:
          errorPolicyToken(item.error_policy) ||
          errorPolicyToken(parent ? containers.get(parent)?.error_policy : undefined) ||
          "fail",
        isEntry: isEntry(nodeId),
        lanes: groupLaneIds(item),
        laneFrames: layout.laneFrames,
        local: localNodeIds.has(nodeId),
        ports,
        unreachable: Boolean(canvasGraph.entry) && !nodeReachable(nodeId),
        validationIssues: validationIssuesByScope.get(`for_each:${item.id}`) || [],
        onOpenForEach,
        onOpenNewStep,
        ...(inlineBody ? { bodySteps: inlineBody, onOpenStep } : {}),
      },
      ...(inlineBody ? {} : { dragHandle: ".group-drag-handle" }),
      draggable: !forEachReadOnly,
      ...(structureLocked && !forEachReadOnly ? { connectable: true } : {}),
      ...parentProps(nodeId),
      handles: inlineBody ? inlineLoopHandles(layout.width, layout.height, ports) : groupNodeHandles(layout.width, layout.height, ports),
      height: layout.height,
      position: absolutePosition(nodeId),
      style: { width: layout.width, height: layout.height },
      width: layout.width,
    });
  });
  steps.forEach((step) => {
    const size = sizeOfStep(step);
    // A step inside a hook frame is the project's own workflow: editable even on
    // a protected author canvas (B14).
    const hookRef = parseHookNodeId(step.id);
    const hookMember = Boolean(hookRef?.memberStepId);
    const hookContract = hookMember ? groups.find((group) => group.hook?.hookId === hookRef?.hookId)?.hook?.outputContract : undefined;
    const stepContractSummary: ContractSummary = hookMember ? {
      ...(!hookContract ? { unavailable: true } : {}),
      outputs: hookContract?.outputs || [], artifacts: hookContract?.artifacts || [],
    } : contractSummary;
    const stepReadOnly = hookMember ? readOnly : nodeReadOnly;
    const parent = parentByNode.get(step.id);
    const owner = parent ? containers.get(parent) : undefined;
    const stepRef = step.step_id || step.id;
    const stepHandles = stepNodeHandles(step, size.height);
    const bodyEnds: StepBodyEnd[] = edges
      .filter((edge) => edge.source.kind === "step" && edge.source.id === stepRef && edge.outcome.name !== "error" && (edge.target.kind === "group_exit" || edge.target.kind === "for_each_exit"))
      .flatMap((edge) => {
        const handle = stepHandles.find((item) => item.type === "source" && item.id === outcomeHandleId(edge.outcome));
        if (!handle) return [];
        const target = edge.target as Extract<WorkflowTargetRef, { kind: "group_exit" | "for_each_exit" }>;
        const lanes = target.kind === "group_exit" && groups.find((group) => group.id === target.group_id)?.body?.kind === "lanes";
        const hookEnd = target.kind === "group_exit" && parseHookNodeId(target.group_id)?.memberStepId === "";
        const kind: StepBodyEnd["kind"] = hookEnd ? "hook" : target.lane_id || lanes ? "lane" : target.kind === "for_each_exit" ? "item" : "group";
        return [{ handle: handle.id || "", kind, x: handle.x + (handle.width ?? 10), y: handle.y + (handle.height ?? 10) / 2 }];
      });
    const freeOutcomes = stepReadOnly || !onAppendStep
      ? []
      : stepPorts(step)
        .filter((port) => port.outcome.name !== "error")
        .map((port) => outcomeHandleId(port.outcome))
        .filter((handle) => !edges.some((edge) => edge.source.kind === "step" && edge.source.id === stepRef && outcomeHandleId(edge.outcome) === handle));
    nodes.push({
      id: step.id,
      type: canvasNodeType(step),
      data: {
        actionScopeKey,
        readOnly: stepReadOnly,
        ...(runtimeMarks[step.id] ? { runtimeMark: runtimeMarks[step.id] } : {}),
        automationRules: automationRulesByScope.get(`step:${step.step_id || step.id}`) || [],
        isEntry: isEntry(step.id),
        local: localNodeIds.has(step.id),
        freeOutcomes,
        onAppendStep,
        outcomeTargets: Object.fromEntries(edges.filter((edge) => edge.source.kind === "step" && edge.source.id === stepRef).map((edge) => {
          const target = edge.target;
          const label = target.kind === "step" ? steps.find((item) => item.id === target.id || item.step_id === target.id)?.display_name || target.id
            : target.kind === "group" ? groups.find((item) => item.id === target.id)?.display_name || target.id
            : target.kind === "for_each" ? forEachItems.find((item) => item.id === target.id)?.display_name || target.id
            : t("react.workflows.exit", "Exit");
          return [outcomeHandleId(edge.outcome), label];
        })),
        step: { ...step, ports: stepPorts(step) },
        ...(step.kind === "stop_success" ? { contractSummary: stepContractSummary } : {}),
        ...(step.kind === "stop_success" && !readOnly && !structureLocked && !hookMember && onOpenStopInterfaceSection
          ? { onOpenInterfaceSection: onOpenStopInterfaceSection }
          : {}),
        errorConnected: edges.some((edge) => edge.source.kind === "step" && edge.source.id === (step.step_id || step.id) && edge.outcome.name === "error"),
        errorPolicy:
          errorPolicyToken(step.error_policy) ||
          errorPolicyToken(owner?.error_policy) ||
          errorPolicyToken(
            groups.find(
              (group) =>
                group.id === (forEachItems.find((item) => item.id === step.for_each_id)?.group_id || ""),
            )?.error_policy,
          ) ||
          "fail",
        unreachable: Boolean(canvasGraph.entry) && !nodeReachable(step.id),
        validationIssues: validationIssuesByScope.get(`step:${step.step_id || step.id}`) || [],
        onAutomation: onOpenStepAutomation,
        onOpenStep,
        onEditStep,
        onPlaceStep,
        onOpenStepTestRun: !readOnly && stepTestRunAllowed?.(step.step_id || step.id) ? onOpenStepTestRun : undefined,
        onDeleteStep,
        ...(bodyEnds.length ? { bodyEnds } : {}),
      },
      ...(parent && inlineBodies.has(parent) ? { hidden: true } : {}),
      draggable: !stepReadOnly,
      ...(hookMember && structureLocked ? { connectable: !stepReadOnly } : {}),
      ...parentProps(step.id),
      handles: stepHandles,
      height: size.height,
      position: absolutePosition(step.id),
      style: { width: size.width, height: size.height },
      width: size.width,
    });
  });

  const totalWidth = rootWrap
    ? Math.max(980, CANVAS_MARGIN_X * 2 + rootWrap.width + 180)
    : Math.max(980, CANVAS_MARGIN_X * 2 + START_WIDTH + COLUMN_GAP + rootGrid.width + 180);

  const canvasEdges: Array<Edge<CanvasEdgeData>> = edges.map((edge) => {
    const key = edgeKey(edge);
    const sourcePorts =
      edge.source.kind === "step"
        ? portsByStep.get(edge.source.id) || []
        : groupPorts(
            (edge.source.kind === "for_each"
              ? forEachItems.find((item) => item.id === edge.source.id)
              : groups.find((group) => group.id === edge.source.id)) || {},
            t,
          );
    const port = sourcePorts.find((candidate) => outcomeHandleId(candidate.outcome) === outcomeHandleId(edge.outcome));
    const label =
      edge.outcome.name === "success" && !edge.rework
        ? ""
        : [port?.label || outcomeHandleId(edge.outcome), edge.rework ? t("react.workflows.edge_repeat", "Repeat") : ""]
            .filter(Boolean)
            .join(" · ");
    const runtimeKey = runtimeEdgeKey(edge);
    const taken = runtimeEdges.has(runtimeKey);
    const passes = runtimeEdges instanceof Map ? Number(runtimeEdges.get(runtimeKey) || 0) : 0;
    const color = taken ? "var(--fs-workflow-edge-ran)" : edgeColor(edge);
    const failurePath = edge.outcome.name === "error";
    const draft = draftEdgeKeys.has(key);
    const sourceId = nodeIdForRef(edge.source);
    const targetId = targetNodeId(edge.target);
    const ordinaryTarget = edge.target.kind !== "group_exit" && edge.target.kind !== "for_each_exit";
    const sameScope = ordinaryTarget && (stepSizes.has(sourceId) || containers.has(sourceId)) && (stepSizes.has(targetId) || containers.has(targetId)) && parentByNode.get(sourceId) === parentByNode.get(targetId) && laneOfNode(sourceId) === laneOfNode(targetId);
    const sourceColumn = sameScope ? routingColumns.get(sourceId) : undefined;
    const targetColumn = sameScope ? routingColumns.get(targetId) : undefined;
    const routing = { sameScope, sourceColumn, targetColumn, longJump: sourceColumn !== undefined && targetColumn !== undefined && targetColumn > sourceColumn + 1 };
    // A body's end is the end circle beside its step; an inline loop's body is listed in its card.
    const endsBody = !ordinaryTarget && edge.source.kind === "step" && edge.outcome.name !== "error";
    const insideInline = [sourceId, targetId].some((id) => { const parent = parentByNode.get(id); return Boolean(parent && inlineBodies.has(parent)); }) || (!ordinaryTarget && inlineBodies.has(targetId));
    return {
      id: `edge:${key}`,
      animated: false,
      ...(endsBody || insideInline ? { hidden: true } : {}),
      data: {
        routing,
        taken,
        ...(passes > 1 ? { passes } : {}),
        draft,
        edge,
        edgeKey: key,
        failurePath,
        label,
        rework: Boolean(edge.rework),
      },
      label: label || undefined,
      labelBgBorderRadius: 8,
      labelBgPadding: [7, 4],
      labelBgStyle: {
        fill: "var(--fs-bg-surface)",
        stroke: "var(--fs-border-default)",
      },
      labelStyle: { fill: "var(--fs-text-primary)", fontSize: 11, fontWeight: 700 },
      source: nodeIdForRef(edge.source),
      sourceHandle: outcomeHandleId(edge.outcome),
      style: {
        stroke: color,
        strokeDasharray: draft ? "var(--fs-workflow-edge-draft-dash)" : (failurePath || edge.rework ? "var(--fs-workflow-edge-dash)" : undefined),
        strokeWidth: "var(--fs-workflow-edge-width)",
      },
      target: targetNodeId(edge.target),
      targetHandle: targetHandleId(edge.target),
      type: "canvasEditable",
    };
  });

  if (canvasGraph.entry && nodes.some((node) => node.id === entryId)) {
    canvasEdges.unshift({ id: "visual:entry", source: "visual:start", sourceHandle: "entry", target: entryId, targetHandle: canvasGraph.entry.kind === "step" ? "in" : "entry", type: "canvasEditable", selectable: false, deletable: false, style: { stroke: "var(--fs-workflow-edge)", strokeWidth: "var(--fs-workflow-edge-width)" } });
  }
  // React Flow requires parents before descendants; preserve sibling order.
  const ordered: typeof nodes = [];
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const emitted = new Set<string>();
  const append = (node: (typeof nodes)[number]) => {
    if (emitted.has(node.id)) return;
    const parent = node.parentId ? byId.get(node.parentId) : undefined;
    if (parent) append(parent);
    emitted.add(node.id);
    ordered.push(node);
  };
  nodes.forEach(append);
  return { edges: canvasEdges, nodes: ordered, totalWidth, ...(rootWrap ? { rootRows: rootWrap.rootRows } : {}) };
}
