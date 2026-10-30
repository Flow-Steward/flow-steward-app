import { canvasNodeLane } from "./canvas-placement";
import { CANVAS_GRID, GROUP_HEADER_HEIGHT, GROUP_PADDING_X, GROUP_PADDING_BOTTOM, GROUP_MIN_WIDTH, GROUP_MIN_HEIGHT, LANE_CONTROL_SPACE, LANE_ADD_CONTROL_HEIGHT } from "./layout-constants";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { Node } from "@xyflow/react";

import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import type { CanvasConnectionTargetState, GroupNodeData, StartNodeData, LaneFrame } from "@/pages/workflows/builder/layout";
import {
  isStepNode,
  nodeSize,
  normalizedSearchText,
  type CanvasPosition,
  type SearchResult,
} from "@/pages/workflows/builder/canvas-core";
import { edgeKey, nodeRef, type WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import { isTruthy } from "@/lib/utils/unknown-values";
import type { WorkflowStructureStatus } from "@/pages/workflows/detail/detail-change-set";

/** Edges a change set adds, drawn dashed until they are applied. */
export function draftEdgeKeysFromOperations(operations: WorkflowChangeOperation[]): Set<string> {
  const keys = new Set<string>();
  operations.forEach((operation) => {
    if (operation.op === "connect") {
      keys.add(edgeKey(operation.edge));
    } else if (operation.op === "insert_step_between") {
      keys.add(edgeKey({ source: operation.source, outcome: operation.outcome }));
      keys.add(edgeKey({ source: nodeRef("step", operation.step.step_id), outcome: { name: "success" } }));
    }
  });
  return keys;
}

/** Canvas node ids of steps and groups that exist only in the unapplied change set. */
export function localNodeIdsFromOperations(operations: WorkflowChangeOperation[]): Set<string> {
  const ids = new Set<string>();
  operations.forEach((operation) => {
    if (operation.op === "create_step" || operation.op === "insert_step_between") {
      ids.add(operation.step.step_id);
    } else if (operation.op === "create_group") {
      ids.add(`group:${operation.group.group_id}`);
    }
  });
  return ids;
}

export function structureStatusView(
  status: WorkflowStructureStatus,
  changeCount: number,
  t: I18nContextValue["t"] = fallbackContext.t,
) {
  if (status === "applying") {
    return {
      className: "border-[var(--fs-border-selected)] bg-[var(--fs-bg-surface)] text-[var(--fs-text-primary)]",
      label: t("react.workflows.applying_changes", "Applying…"),
    };
  }
  if (status === "conflict") {
    return {
      className: "border-[var(--fs-status-danger-border)] bg-[var(--fs-status-danger-bg)] text-[var(--fs-status-danger)]",
      label: t("react.workflows.status_structure_conflict", "Workflow changed elsewhere"),
    };
  }
  if (changeCount) {
    return {
      className: "border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] text-[var(--fs-status-attention-ink)]",
      label: t("react.workflows.status_structure_not_applied_count", "Structure not applied: {count} change(s)", {
        count: changeCount,
      }),
    };
  }
  return {
    className: "border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] text-[var(--fs-text-secondary)]",
    label: t("react.workflows.status_structure_applied", "Structure applied"),
  };
}

export function buildCanvasSearchResults(
  canvasGraph: WorkflowCanvasGraph,
  searchQuery: string,
): SearchResult[] {
  const query = normalizedSearchText(searchQuery);
  if (!query) {
    return [];
  }
  const matches = (result: SearchResult) =>
    normalizedSearchText(result.label).includes(query) ||
    normalizedSearchText(result.secondary).includes(query);
  const groupResults = (canvasGraph.groups || [])
    .map((group): SearchResult => ({
      id: `group:${group.id}`,
      kind: "group",
      label: group.display_name,
      secondary: group.id,
    }))
    .filter(matches);
  const stepResults = canvasGraph.steps
    .map((step): SearchResult => ({
      id: step.id,
      kind: "step",
      label: step.display_name,
      secondary: [step.step_id || step.id, step.group_id || ""].filter(isTruthy).join(" · "),
    }))
    .filter(matches);
  return [...groupResults, ...stepResults].slice(0, 12);
}

/** Resize from actual children, deepest first, preserving their absolute view positions. */
export function reconcileCanvasGeometry(nodes: readonly Node[]): Node[] {
  const result = nodes.map((node) => ({ ...node, position: { ...node.position } }));
  const byId = new Map(result.map((node) => [node.id, node]));
  const depth = (node: Node) => { const seen = new Set([node.id]); let value = 0; let parent = node.parentId ? byId.get(node.parentId) : undefined; while (parent && !seen.has(parent.id)) { seen.add(parent.id); value += 1; parent = parent.parentId ? byId.get(parent.parentId) : undefined; } return value; };
  const containers = result.filter((node) => node.type === "groupContainer" || node.type === "forEachContainer").sort((a, b) => depth(b) - depth(a));
  const ceil = (n: number) => Math.ceil(n / CANVAS_GRID) * CANVAS_GRID;
  for (const container of containers) {
    const children = result.filter((node) => node.parentId === container.id && node.type !== "canvasNote");
    if (!children.length) continue;
    const shiftX = ceil(Math.max(0, GROUP_PADDING_X - Math.min(...children.map((node) => node.position.x))));
    const shiftY = ceil(Math.max(0, GROUP_HEADER_HEIGHT + GROUP_PADDING_X - Math.min(...children.map((node) => node.position.y))));
    container.position = { x: container.position.x - shiftX, y: container.position.y - shiftY };
    for (const child of children) child.position = { x: child.position.x + shiftX, y: child.position.y + shiftY };
    const width = ceil(Math.max(GROUP_MIN_WIDTH, ...children.map((node) => node.position.x + nodeSize(node).width + GROUP_PADDING_X)));
    let bottom = Math.max(...children.map((node) => node.position.y + nodeSize(node).height));
    const oldFrames = (container.data as { laneFrames?: LaneFrame[] }).laneFrames;
    if (oldFrames?.length) {
      const laneFrames = oldFrames.map((frame) => {
        const lane = children.filter((node) => canvasNodeLane(node) === frame.id);
        const left = lane.length ? Math.min(...lane.map((node) => node.position.x)) : frame.x + 6 + shiftX;
        const top = lane.length ? Math.min(...lane.map((node) => node.position.y)) : frame.y + 6 + shiftY;
        const right = lane.length ? Math.max(...lane.map((node) => node.position.x + nodeSize(node).width)) : left + frame.width - 12;
        const end = lane.length ? Math.max(...lane.map((node) => node.position.y + nodeSize(node).height)) : top + frame.height - 12 - LANE_CONTROL_SPACE;
        bottom = Math.max(bottom, end + LANE_CONTROL_SPACE);
        return { ...frame, x: left - 6, y: top - 6, width: right - left + 12, height: end - top + 12 + LANE_CONTROL_SPACE, addControl: { x: left, y: end + 12, width: right - left, height: LANE_ADD_CONTROL_HEIGHT } };
      });
      container.data = { ...container.data, laneFrames };
    }
    const height = ceil(Math.max(GROUP_MIN_HEIGHT, bottom + GROUP_PADDING_BOTTOM));
    container.width = width; container.height = height;
    container.style = { ...container.style, width, height };
    container.handles = container.handles?.map((handle) => ({ ...handle, ...(handle.position === "bottom" ? { y: height - (handle.height ?? 10) / 2, x: handle.id === "exit" ? width / 2 - 5 : width - 29 } : handle.position === "right" ? { x: width - 5 } : {}) }));
  }
  return result;
}

/** Persist adjusted local coordinates when a resized ancestor changes its origin. */
export function manualPositionsFromNodes(nodes: readonly Node[], baseline: readonly Node[], previous: Record<string, CanvasPosition>): Record<string, CanvasPosition> {
  const before = new Map(baseline.map((node) => [node.id, node]));
  const positions = { ...previous };
  for (const node of nodes) {
    if (node.type === "canvasNote" || node.type === "workflowStart") continue;
    const original = before.get(node.id);
    if (positions[node.id] || original?.position.x !== node.position.x || original.position.y !== node.position.y) positions[node.id] = { ...node.position };
  }
  return positions;
}

export function applyCanvasNodeState({
  connectionHelpActive,
  connectionTargetStates,
  connectionPointedTarget,
  highlightedNodeId,
  manualPositions,
  nodes,
}: {
  connectionHelpActive: boolean;
  connectionTargetStates?: ReadonlyMap<string, "valid" | "invalid">;
  connectionPointedTarget?: { id: string; valid: boolean } | null;
  highlightedNodeId: string | null;
  manualPositions: Record<string, CanvasPosition>;
  nodes: Node[];
}) {
  const targetState = (id: string): CanvasConnectionTargetState | undefined => {
    if (!connectionHelpActive) return undefined;
    if (connectionPointedTarget?.id === id) return connectionPointedTarget.valid ? "active-valid" : "active-invalid";
    return connectionTargetStates?.get(id) === "valid" ? "eligible" : undefined;
  };
  const overlaid = nodes.map((node) => manualPositions[node.id] && node.type !== "workflowStart" ? { ...node, position: manualPositions[node.id] } : node);
  const positioned = Object.keys(manualPositions).length ? reconcileCanvasGeometry(overlaid) : overlaid;
  return positioned.map((node) => {
    if (node.type === "workflowStart") {
      const startNode = node as Node<StartNodeData>;
      const entry = nodes.find((candidate) => candidate.id === startNode.data.entryNodeId && !candidate.parentId);
      const entryPosition = positioned.find((candidate) => candidate.id === entry?.id)?.position;
      return entry && entryPosition ? {
        ...startNode,
        position: {
          x: startNode.position.x + entryPosition.x - entry.position.x,
          y: startNode.position.y + entryPosition.y - entry.position.y,
        },
      } : startNode;
    }
    if (node.type === "groupContainer" || node.type === "forEachContainer" || node.type === "inlineLoop") {
      const groupNode = node as Node<GroupNodeData>;
      return {
        ...groupNode,
        data: {
          ...groupNode.data,
          connectTargetActive: targetState(node.id) === "active-valid",
          connectionTargetState: targetState(node.id),
          highlighted: highlightedNodeId === groupNode.id,
        },
      };
    }
    if (isStepNode(node)) {
      return {
        ...node,
        data: {
          ...node.data,
          connectionTargetState: targetState(node.id),
          highlighted: highlightedNodeId === node.id,
        },
      };
    }
    return node;
  });
}
