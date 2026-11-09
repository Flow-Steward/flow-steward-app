import { useCallback, useMemo, useState } from "react";
import { Background, Controls, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { DsAlert, DsDrawer } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";
import { buildCanvasLayout } from "@/pages/workflows/builder/layout";
import { stepNodeTypes } from "@/pages/workflows/builder/canvas-group-controls";
import { jobExecutionTrace } from "./job-execution-trace";
import { jobContainerProgress } from "./job-container-progress";
import { containerRuntimeMarks } from "@/pages/workflows/builder/runtime-container-marks";
import { runtimeEdgeMarks } from "@/pages/workflows/builder/runtime-edge-marks";
import { CanvasEditableEdge } from "@/pages/workflows/builder/canvas-step-nodes";
import type { JobDefinitionPayload, JobExecutionSnapshot, JobExecutionStepsPage } from "./api";
import { archivedDefinitionCanvasGraph, archivedDefinitionContract } from "./job-definition-graph";
import { useJobExecutionSnapshot } from "./job-execution-snapshot";
import { workflowExecutionMarks } from "@/pages/workflows/detail/detail-hook-execution-marks";
import "./job-detail.css";
import { ReadableDataFields } from "./readable-data-fields";

const ignoreAction = () => undefined;
const edgeTypes = { canvasEditable: CanvasEditableEdge };

export function JobDefinitionCanvas({ definition, jobId, projectId, executionPage, executionSnapshot }: Readonly<{
  definition: JobDefinitionPayload; jobId?: string; projectId?: string; executionPage?: JobExecutionStepsPage;
  /** The page already reads the run's step history: use it instead of a second reader of the same pages. */
  executionSnapshot?: JobExecutionSnapshot | null;
}>) {
  const { t } = useI18n();
  const [selection, setSelection] = useState<{ kind: "step" | "group" | "for_each" | "inputs" | "outputs"; id: string } | null>(null);
  const graph = useMemo(() => archivedDefinitionCanvasGraph(definition, t), [definition, t]);
  const takenEdges = useMemo(() => graph ? runtimeEdgeMarks(jobExecutionTrace(jobId || "", definition, executionPage), graph) : new Set<string>(), [definition, executionPage, graph, jobId]);
  const containerMarks = useMemo(() => graph ? containerRuntimeMarks(jobContainerProgress(jobId || "", projectId, definition, executionPage), graph) : {}, [definition, executionPage, graph, jobId, projectId]);
  // Each step the run reached carries its mark (the failed one red, with "Failed"); the rest stand back.
  const pageOwnsHistory = executionSnapshot !== undefined;
  const { snapshot: ownSnapshot } = useJobExecutionSnapshot(jobId || "", Boolean(jobId && graph) && !pageOwnsHistory, projectId);
  const snapshot = pageOwnsHistory ? executionSnapshot : ownSnapshot;
  const stepMarks = useMemo(() => graph && definition.workflow_id && definition.revision
    ? workflowExecutionMarks(snapshot, graph, definition.workflow_id, Number(definition.revision), jobId)
    : {}, [definition.revision, definition.workflow_id, graph, jobId, snapshot]);
  const runMarked = Object.keys(stepMarks).length > 0;
  const contract = useMemo(() => archivedDefinitionContract(definition), [definition]);
  const openInputs = useCallback(() => setSelection({ kind: "inputs", id: "" }), []);
  const openStep = useCallback((id: string) => setSelection({
    kind: graph?.steps.find((step) => step.id === id || step.step_id === id)?.kind === "stop_success" ? "outputs" : "step", id,
  }), [graph]);
  const openGroup = useCallback((id: string) => setSelection({ kind: "group", id }), []);
  const openForEach = useCallback((id: string) => setSelection({ kind: "for_each", id }), []);
  const layout = useMemo(() => graph ? buildCanvasLayout({
    canvasGraph: graph, runtimeEdges: takenEdges, containerRuntimeMarks: containerMarks, runtimeMarks: stepMarks, readOnly: true, archivedContract: contract, onOpenInputs: openInputs, onOpenStep: openStep, onOpenGroup: openGroup,
    onOpenForEach: openForEach, onOpenStepAutomation: ignoreAction, onOpenGroupAutomation: ignoreAction,
  }, t) : null, [contract, graph, takenEdges, containerMarks, stepMarks, openForEach, openGroup, openInputs, openStep, t]);
  const unavailable = { status: t("react.jobs.archived_contract_unavailable", "Archived contract unavailable") };
  let selected: unknown;
  switch (selection?.kind) {
    case "inputs":
      selected = contract ? { input_mode: contract.input_mode, inputs: contract.inputs, trigger_channels: contract.trigger_channels } : unavailable;
      break;
    case "outputs":
      selected = contract ? { output_mode: contract.output_mode, outputs: contract.outputs, returned_files: contract.artifacts } : unavailable;
      break;
    case "step":
      selected = definition.steps?.find((step) => step.step_id === selection.id);
      break;
    case "group":
      selected = definition.groups?.find((group) => group.group_id === selection.id);
      break;
    case "for_each":
      selected = definition.for_each?.find((item) => item.for_each_id === selection.id);
      break;
  }
  if (!layout) return <DsAlert tone="warning" title={t("react.jobs.archived_graph_unavailable", "The archived graph is unavailable or uses an unsupported format.")} />;
  return <>
    <div className="job-definition-canvas h-[420px] overflow-hidden fs-surface-radius border border-subtle fs-surface-inset" data-run-marked={runMarked ? "true" : undefined} data-testid="job-definition-canvas">
      <ReactFlow
        nodes={layout.nodes}
        edges={layout.edges.map((edge) => ({ ...edge, ...(edge.data ? { data: { ...edge.data, hovered: true } } : {}), animated: false, deletable: false, selectable: false }))}
        nodeTypes={stepNodeTypes}
        edgeTypes={edgeTypes}
        onNodeClick={(_, node) => {
          if ("forEach" in node.data) openForEach(node.data.forEach.id);
          else if ("group" in node.data) openGroup(node.data.group.id);
          else if ("step" in node.data) openStep(node.data.step.step_id || node.id);
          else openInputs();
        }}
        nodesDraggable={false}
        nodesConnectable={false}
        edgesReconnectable={false}
        deleteKeyCode={null}
        fitView
        minZoom={0.2}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
    <DsDrawer open={Boolean(selected)} onClose={() => setSelection(null)} title={t("react.jobs.archived_configuration", "Archived configuration")} closeLabel={t("react.jobs.close_archived_configuration", "Close archived configuration")} testId="job-archived-configuration">
      <ReadableDataFields value={selected} />
    </DsDrawer>
  </>;
}
