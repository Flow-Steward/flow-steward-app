import type { Node } from "@xyflow/react";
import type { WorkflowCanvasPort, WorkflowGraphEdge, WorkflowNodeRef } from "@/pages/workflows/shared/api";
import type { I18nContextValue } from "@/lib/i18n/context";
import { outcomeHandleId, targetLabel } from "./api";
import { nodeIdForRef } from "./layout";

type NamedNodeData = {
  step?: { display_name?: string; step_id?: string; id?: string; ports?: WorkflowCanvasPort[] };
  group?: { display_name?: string; id?: string };
  forEach?: { display_name?: string; id?: string };
  ports?: WorkflowCanvasPort[];
};

/** What a person calls a node on the canvas: its display name, else its id. */
export function canvasNodeName(node: Node | undefined, fallback = ""): string {
  const data = (node?.data || {}) as NamedNodeData;
  const named = data.step?.display_name || data.group?.display_name || data.forEach?.display_name;
  return String(named || "").trim() || fallback;
}

/** The outcome as its card names it ("Next", "True", a case), else its handle id. */
export function canvasOutcomeLabel(node: Node | undefined, edge: Pick<WorkflowGraphEdge, "outcome">): string {
  const data = (node?.data || {}) as NamedNodeData;
  const handle = outcomeHandleId(edge.outcome);
  const port = (data.step?.ports || data.ports || []).find((candidate) => outcomeHandleId(candidate.outcome) === handle);
  return String(port?.label || "").trim() || handle;
}

/** "Classify request · Next → Priority order?" with the ids underneath: "classify · success → is_priority".
 * Pass the drawn edge's node ids when they differ from the refs (Hook frames). */
export function canvasEdgeNames(edge: WorkflowGraphEdge, nodesById: ReadonlyMap<string, Node>, t: I18nContextValue["t"], drawn?: { source: string; target: string }): { title: string; ids: string } {
  const source = nodesById.get(drawn?.source || nodeIdForRef(edge.source));
  const exit = edge.target.kind === "group_exit" || edge.target.kind === "for_each_exit";
  const target = exit ? undefined : nodesById.get(drawn?.target || nodeIdForRef(edge.target as WorkflowNodeRef));
  const targetName = target ? canvasNodeName(target, targetLabel(edge.target, t)) : targetLabel(edge.target, t);
  return {
    title: `${canvasNodeName(source, edge.source.id)} · ${canvasOutcomeLabel(source, edge)} → ${targetName}`,
    ids: `${edge.source.id} · ${outcomeHandleId(edge.outcome)} → ${targetLabel(edge.target, t)}`,
  };
}
