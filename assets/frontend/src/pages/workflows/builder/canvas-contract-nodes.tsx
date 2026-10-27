import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { useI18n } from "@/lib/i18n/useI18n";
import { Button } from "@/components/ui/button";
import { CanvasScheduleSummary } from "./canvas-schedule-summary";
import { localizedWORKFLOW_INTERFACE_INPUT_MODES } from "@/pages/workflows/detail/detail-helpers";
import type { StartNodeData } from "./layout";
import type { ContractSignature } from "./canvas-card-presentation";
import { runtimeHandleStyle } from "./canvas-core";
import "./canvas-builder.css";

export function ContractNames({ title, names, signatures, emptyLabel, onOpen }: Readonly<{ title: string; names: string[]; signatures?: ContractSignature[]; emptyLabel?: string; onOpen?: () => void }>) {
  const { t } = useI18n();
  // Name-only contracts retain literal names and unknown signature metadata.
  const fields: ContractSignature[] = signatures ?? names.map((name) => ({ name }));
  return <div className="workflow-contract-section">
    {onOpen ? <Button className="nodrag nopan workflow-contract-section-action workflow-micro-label" data-canvas-secondary-action="true" onClick={(event) => { event.stopPropagation(); onOpen(); }} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") event.stopPropagation(); }} size="sm" type="button" variant="ghost">{title}</Button> : <p className="workflow-micro-label">{title}</p>}
    <div className="fs-param-group">{fields.length ? fields.map((field, index) => <span className="fs-param-chip" key={`${field.name}:${index}`}>
      <span className="fs-param-name">{field.name}{field.required ? <span className="fs-param-required" aria-label={t("react.workflows.required", "Required")}>*</span> : null}</span>
      {field.type ? <span className="fs-param-type">{field.type}</span> : null}
    </span>) : <span className="workflow-empty">{emptyLabel || t("react.workflows.none_declared", "None declared")}</span>}</div>
    {fields.some((field) => field.required) ? <p className="fs-param-legend">{t("react.workflows.required_legend", "* Required")}</p> : null}
  </div>;
}

export function WorkflowStartNode({ data }: Readonly<NodeProps<Node<StartNodeData>>>) {
  const { t } = useI18n();
  const unavailable = data.contractUnavailable ? t("react.jobs.archived_contract_unavailable", "Archived contract unavailable") : undefined;
  // Only legacy Start inputs encode requiredness with a suffix; declared facts stay authoritative.
  const inputSignatures = data.inputSignatures ?? data.inputs.map((name) => ({ name: name.replace(/\*$/, ""), required: name.endsWith("*") }));
  const modeLabel = data.inputMode && data.inputMode !== "fields" ? localizedWORKFLOW_INTERFACE_INPUT_MODES(t).find((mode) => mode.value === data.inputMode)?.label : undefined;
  const channelLabels: Record<string, string> = { ui: "UI", api: "API", webhook: t("react.workflows.incoming_webhook", "Incoming Webhook"), schedule: t("react.workflows.schedule", "Schedule"), parent_workflow: t("react.workflows.parent_workflow", "Parent workflow") };
  return <div data-workflow-step-frame="true" className="workflow-card workflow-start">
    <div data-workflow-step-measure-id="visual:start">
      <Button className="nodrag nopan workflow-start-title" variant="ghost" aria-label={t("react.workflows.start_inputs", "Start — workflow inputs")} onClick={(event) => { event.stopPropagation(); data.onOpenInputs?.(); }} type="button"><span aria-hidden="true" className="workflow-start-dot" />{t("react.workflows.start", "Start")}</Button>
      <div className="workflow-card-facts">
        {data.hasEntry === false ? <span className="workflow-fact is-error">{t("react.workflows.no_entry", "No entry")}</span> : null}
        {!data.readOnly && !data.workflowEnabled ? <span className="workflow-fact">{t("react.workflows.workflow_disabled", "Workflow disabled")}</span> : null}
      </div>
      <div className="workflow-contract-section"><p className="workflow-micro-label">{t("react.workflows.configured_triggers", "Configured triggers")}</p><div className="workflow-trigger-list">{data.triggerChannels.length ? data.triggerChannels.map((channel) => <span className="workflow-fact" key={channel}>{channelLabels[channel] || channel}</span>) : <span className="workflow-empty">{unavailable || t("react.workflows.no_triggers_configured", "No triggers configured")}</span>}</div></div>
      {!data.readOnly && data.triggerChannels.includes("schedule") ? <div className="workflow-schedule"><CanvasScheduleSummary workflowId={data.workflowId} projectHint={data.projectHint} /></div> : null}
      <ContractNames title={t("react.workflows.inputs", "Inputs")} names={data.inputs} signatures={inputSignatures} emptyLabel={unavailable || modeLabel || t("react.workflows.no_inputs_declared", "No inputs declared")} />
      {!data.readOnly && data.empty ? <Button className="mt-3" variant="secondary" size="sm" onClick={(event) => { event.stopPropagation(); data.onAddStep?.(); }} type="button">{t("react.workflows.add_first_step", "Add first step")}</Button> : null}
    </div>
    <Handle id="entry" type="source" position={Position.Right} isConnectable={false} style={{ ...runtimeHandleStyle("source", "start"), top: 24 }} />
  </div>;
}
