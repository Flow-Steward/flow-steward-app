import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { useI18n } from "@/lib/i18n/useI18n";
import { WorkflowStepIcon } from "@/pages/workflows/shared/workflow-step-icon";
import { CanvasPortControl } from "./canvas-port-control";
import { CanvasRuntimeMark } from "./canvas-runtime-mark";
import { CanvasContainerRuntimeMark } from "./canvas-container-runtime-mark";
import { runtimeHandleStyle } from "./canvas-core";
import type { ForEachNodeData } from "./layout";
import "./canvas-builder.css";
import "./canvas-menus.css";

/**
 * Inline loop (Components › Flow): a For each drawn as one step card that lists
 * its loop body. It keeps the step's ports — input and Next at the title row,
 * On error at the bottom — and opens the For each editor; a body row opens its step.
 */
export function InlineLoopNode({ data, selected }: Readonly<NodeProps<Node<ForEachNodeData>>>) {
  const { t } = useI18n();
  const { forEach } = data;
  const body = data.bodySteps || [];
  const success = data.ports.find((port) => port.outcome.name === "success") || { outcome: { name: "success" as const }, label: t("react.workflows.outcome_success", "Next") };
  const error = data.ports.find((port) => port.outcome.name === "error");
  const concurrency = typeof forEach.body?.concurrency_limit === "number" && forEach.body.concurrency_limit > 0 ? forEach.body.concurrency_limit : 0;
  const open = () => data.onOpenForEach(forEach.id);
  return <fieldset className="workflow-card group" data-workflow-step-frame="true" data-connection-target={data.connectionTargetState} aria-label={t("react.workflows.open_loop_value0", "Open loop {value0}", { value0: forEach.display_name })} data-selected={selected || undefined}>
    <Handle position={Position.Left} type="target" id="entry" style={{ ...runtimeHandleStyle("target", "for_each"), top: 24 }} />
    <Handle position={Position.Bottom} type="target" id="exit" style={{ opacity: 0, pointerEvents: "none" }} />
    <CanvasPortControl source={{ kind: "for_each", id: forEach.id }} sourceLabel={forEach.display_name} port={success} readOnly={data.readOnly} position={Position.Right} style={{ top: 24 }} />
    {error && (data.errorConnected || selected) ? <CanvasPortControl source={{ kind: "for_each", id: forEach.id }} sourceLabel={forEach.display_name} port={error} readOnly={data.readOnly} position={Position.Bottom} style={{ bottom: 0, left: "88%" }} /> : null}
    <span className="workflow-runtime-mark"><CanvasRuntimeMark mark={data.runtimeMark} /><CanvasContainerRuntimeMark mark={data.containerRuntimeMark} containerId={forEach.id} /></span>
    <div className="workflow-card-content" data-workflow-step-measure-id={`for_each:${forEach.id}`}>
      <div className="workflow-card-heading">
        <WorkflowStepIcon kind="for_each" className="workflow-card-icon" />
        <p className="workflow-card-title">{forEach.display_name}</p>
      </div>
      <div className="workflow-card-facts">
        {data.isEntry ? <span className="workflow-fact">{t("react.workflows.entry_badge", "Entry")}</span> : null}
        {data.local ? <span className="workflow-fact is-warning">{t("react.workflows.not_applied_badge", "Not applied")}</span> : null}
        {data.unreachable ? <span className="workflow-fact is-warning">{t("react.workflows.unreachable_badge", "Not reachable")}</span> : null}
        {!forEach.over ? <span className="workflow-fact is-warning">{t("react.workflows.for_each_source_missing", "Source not set")}</span> : null}
      </div>
      <p className="workflow-card-summary">
        {forEach.over ? t("react.workflows.inline_loop_source", "{over} as {item}", { over: forEach.over, item: forEach.item_name || "item" }) : null}
        {concurrency ? `\n${t("react.workflows.inline_loop_concurrency", "concurrency {count}", { count: concurrency })}` : null}
      </p>
      <ul className="workflow-inline-loop-body" aria-label={t("react.workflows.loop_body", "Loop body")}>
        <li className="workflow-inline-loop-head"><span>{t("react.workflows.loop_body", "Loop body")}</span><span>{t(body.length === 1 ? "react.workflows.loop_body_step_one" : "react.workflows.loop_body_steps", body.length === 1 ? "{count} step" : "{count} steps", { count: body.length })}</span></li>
        {body.map((step) => <li key={step.id}>
          <button type="button" className="workflow-inline-loop-step nodrag nopan" data-canvas-secondary-action="true" onClick={(event) => { event.stopPropagation(); data.onOpenStep?.(step.id); }}>
            <span>{step.name}</span>
            {step.summary ? <span className="workflow-inline-loop-step-summary" title={step.summary}>{step.summary}</span> : null}
          </button>
        </li>)}
        {!body.length && data.onOpenNewStep && !data.readOnly ? <li><button type="button" className="workflow-inline-loop-step nodrag nopan" data-canvas-secondary-action="true" onClick={(event) => { event.stopPropagation(); data.onOpenNewStep?.({ groupId: "", laneId: "", forEachId: forEach.id }); }}>{t("react.workflows.add_step_title", "Add step")}</button></li> : null}
      </ul>
    </div>
    <button type="button" className="sr-only" onClick={open}>{t("react.workflows.edit_loop", "Edit loop")}</button>
  </fieldset>;
}
