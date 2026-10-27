import { CanvasRuntimeMark } from "./canvas-runtime-mark";
import { runtimeMarkState } from "./runtime-mark-state";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  BaseEdge,
  type Edge,
  EdgeLabelRenderer,
  type EdgeProps,
  Handle,
  type Node,
  type NodeProps,
  Position,
  type ConnectionLineComponentProps,
} from "@xyflow/react";
import { localizedSTEP_KIND_OPTIONS } from "@/pages/workflows/detail/detail-helpers";
import { WorkflowStepExtensionMark } from "@/pages/workflows/shared/workflow-step-icon";
import { useState, type FocusEvent, type KeyboardEvent } from "react";
import { MoreHorizontal, Play } from "lucide-react";

import type { WorkflowCanvasPort } from "@/pages/workflows/shared/api";
import type { ContractSummary, StepNodeData } from "@/pages/workflows/builder/layout";
import type { ContractSignature } from "@/pages/workflows/builder/canvas-card-presentation";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { CanvasPortControl } from "./canvas-port-control";
import { CanvasEdgeBeads } from "./canvas-edge-actions";
import { CanvasEdgePassCount } from "./canvas-edge-pass-count";
import { useStepKeepsTestOutput } from "./kept-test-output-context";
import { CanvasBodyEnds } from "./canvas-body-end";
import { useCanvasCardCommands } from "./canvas-context-menus";
import { CanvasMenuSurface, type CanvasMenuItem } from "./canvas-menu-surface";
import { curveEdgePath, routeCanvasEdge } from "./canvas-edge-routing";
import { outcomeHandleId } from "@/pages/workflows/builder/api";
import {
  automationRuleTitle,
  branchHandleStyle,
  runtimeHandleStyle,
  stepVisual,
  summaryBadge,
  validationRingClass,
  type InteractiveCanvasEdgeData,
} from "@/pages/workflows/builder/canvas-core";
import { isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

function joinClasses(...values: unknown[]): string {
  return values.map((value) => stringFromUnknownValue(value)).filter(Boolean).join(" ");
}

export { WorkflowStartNode } from "./canvas-contract-nodes";
import { ContractNames } from "./canvas-contract-nodes";
import { orderedStepPorts, outcomePortBottom, hasOutcomeRows } from "./canvas-card-presentation";
import "./canvas-builder.css";

function routedPorts(data: StepNodeData): WorkflowCanvasPort[] {
  return orderedStepPorts(data.step.kind, data.step.ports || []);
}
function errorPort(data: StepNodeData): WorkflowCanvasPort | undefined {
  return (data.step.ports || []).find((port) => port.outcome.name === "error");
}

function nodeFrameClasses(data: StepNodeData): string {
  return joinClasses(
    data.highlighted ? "ring-2 ring-[var(--fs-border-selected)]" : "",
    data.unreachable ? "ring-2 ring-[var(--fs-status-attention-border)]" : "",
    data.local ? "outline outline-2 outline-dashed outline-[var(--fs-status-attention-border)]" : "",
  );
}

export function CanvasEditableEdge({ id, data, label, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, style }: Readonly<EdgeProps<Edge<InteractiveCanvasEdgeData>>>) {
  const [actionHovered, setActionHovered] = useState(false);
  const route = data?.route || routeCanvasEdge({ source: { x: sourceX, y: sourceY }, target: { x: targetX, y: targetY }, obstacles: [], backward: targetX <= sourceX, failure: Boolean(data?.failurePath), repeat: Boolean(data?.rework), sourceSide: sourcePosition === Position.Bottom ? "bottom" : "right", targetSide: targetPosition === Position.Bottom ? "bottom" : "left" });
  // The selected connection, and the connections of the selected step, stand out (Connections › Kinds).
  const standsOut = data?.selected || data?.stepSelected;
  const stroke = data?.taken ? "var(--fs-workflow-edge-ran)" : data?.failurePath ? "var(--fs-workflow-edge-failure)" : standsOut ? "var(--fs-workflow-edge-selected)" : style?.stroke || "var(--fs-workflow-edge)";
  // No arrowhead: the line ends in the input's 10px circle; the sides give the direction.
  return <>
    <BaseEdge id={id} path={route.path} style={{ ...style, stroke, strokeDasharray: data?.draft ? "var(--fs-workflow-edge-draft-dash)" : data?.failurePath || data?.rework ? "var(--fs-workflow-edge-dash)" : undefined, strokeWidth: standsOut ? "var(--fs-workflow-edge-width-selected)" : style?.strokeWidth || "var(--fs-workflow-edge-width)" }} />
    <CanvasEdgePassCount passes={data?.passes} x={route.midpoint.x} y={route.midpoint.y} />
    {data ? <EdgeLabelRenderer>
      <CanvasEdgeBeads midpoint={route.midpoint} visible={Boolean(!data.selected && (data.hovered || actionHovered))} onHoverChange={setActionHovered}
        onInsert={data.onInsert ? () => data.onInsert?.(data) : undefined} onRemove={data.onDelete ? () => data.onDelete?.(data) : undefined} />
      {label ? <span className="workflow-edge-label nodrag nopan" style={{ transform: `translate(${route.midpoint.x}px, ${route.midpoint.y}px) translate(-50%, 0)` }}>{label}</span> : null}
    </EdgeLabelRenderer> : null}
  </>;
}

export function CanvasConnectionPreview({ fromX, fromY, toX, toY, connectionStatus }: Readonly<ConnectionLineComponentProps>) {
  return <path fill="none" d={curveEdgePath({ x: fromX, y: fromY }, { x: toX + 5, y: toY })} style={{ stroke: connectionStatus === "invalid" ? "var(--fs-status-danger)" : "var(--fs-workflow-edge)", strokeWidth: "var(--fs-workflow-edge-width)", strokeDasharray: "var(--fs-workflow-edge-draft-dash)" }} />;
}

/** Badges only when there is something to say: a lane's band names the lane, selection is the border. */
function renderStepBadges(data: StepNodeData, t: ReturnType<typeof useI18n>["t"]) {
  return <>
    {data.step.disabled ? <span className="workflow-fact" title={t("react.workflows.disabled_step_hint", "The run skips this step and goes on along Next.")}>{t("react.workflows.disabled_badge", "Disabled")}</span> : null}
    {data.isEntry ? <span className="workflow-fact">{t("react.workflows.entry_badge", "Entry")}</span> : null}
    {data.local ? <span className="workflow-fact is-warning">{t("react.workflows.not_applied_badge", "Not applied")}</span> : null}
    {data.unreachable ? <span className="workflow-fact is-warning" title={t("react.workflows.unreachable_step_hint", "No connection leads here from the entry yet.")}>{t("react.workflows.unreachable_badge", "Not reachable")}</span> : null}
  </>;
}

export function StepIdentity({ data, visual }: Readonly<{ data: StepNodeData; visual: ReturnType<typeof stepVisual> }>) {
  const { t } = useI18n();
  // System, Extension and MCP tools are one connector kind, told apart by the marketplace's icons.
  const Icon = data.step.kind === "connector" ? stepVisual("connector", data.step.tool_category).Icon : visual.Icon;
  const kindLabel = data.step.kind === "connector" ? undefined : localizedSTEP_KIND_OPTIONS(t).find((option) => option.value === data.step.kind)?.label || data.step.kind;
  return <div className="workflow-card-heading">
    <span className={joinClasses("workflow-card-icon", visual.iconClass)} aria-hidden={data.step.kind === "connector" || undefined} role={data.step.kind === "connector" ? undefined : "img"} aria-label={kindLabel} title={kindLabel}><Icon aria-hidden="true" strokeWidth={2} /></span>
    <p className="workflow-card-title">{data.step.display_name}</p>
    <WorkflowStepExtensionMark extensionId={data.step.extension_id || ""} />
  </div>;
}

export function StepUtilityStack({ data }: Readonly<{ data: StepNodeData }>) {
  const { t } = useI18n();
  const summary = summaryBadge(data.step.validation_summary);
  const automation = summaryBadge(data.step.automation_summary);
  const errors = Number(summary.error_count || 0);
  const warnings = Number(summary.warning_count || 0);
  const issueTitles = data.validationIssues.map((issue) => issue.title_key ? t(issue.title_key, issue.title, issue.title_params) : issue.title).filter(isTruthy);
  const keptOutput = useStepKeepsTestOutput(data.step.id, data.step.step_id);
  return <div className="workflow-card-facts">
    {renderStepBadges(data, t)}
    {keptOutput ? <span className="workflow-fact" title={t("react.workflows.kept_output_card_title", "Test runs return this step's kept test output instead of running it.")}>{t("react.workflows.kept_output_title", "Test output")}</span> : null}
    {errors ? <span className="workflow-fact is-error" title={issueTitles.join("\n")}>{t(errors === 1 ? "react.workflows.validation_error_one" : "react.workflows.validation_errors_count", errors === 1 ? "{count} error" : "{count} errors", { count: errors })}</span> : null}
    {warnings || (!errors && summary.issues) ? <span className="workflow-fact is-warning" title={issueTitles.join("\n")}>{t(warnings === 1 || summary.issues === 1 ? "react.workflows.validation_warning_one" : "react.workflows.validation_warnings_count", warnings === 1 || summary.issues === 1 ? "{count} warning" : "{count} warnings", { count: warnings || summary.issues })}</span> : null}
    {automation.rules ? <button className="workflow-fact nodrag nopan" data-canvas-secondary-action="true" disabled={data.readOnly} onClick={(event) => { event.stopPropagation(); data.onAutomation(data.step.step_id || data.step.id); }} title={automationRuleTitle(data.automationRules, automation, t)} type="button">{t(automation.rules === 1 ? "react.workflows.rule_one" : "react.workflows.rules_count", automation.rules === 1 ? "{count} rule" : "{count} rules", { count: automation.rules })}</button> : null}
  </div>;
}

function useStepActionVisibility() {
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  return {
    active: hovered || focused,
    frameProps: {
      onMouseEnter: () => setHovered(true),
      onMouseLeave: () => setHovered(false),
      onFocusCapture: () => setFocused(true),
      onBlurCapture: (event: FocusEvent<HTMLFieldSetElement>) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false);
      },
    },
  };
}

function stopStepActionKeyPropagation(event: KeyboardEvent<HTMLButtonElement>) {
  if (event.key === "Enter" || event.key === " ") event.stopPropagation();
}

function SelectedStepActions({ data, selected, active }: Readonly<{ data: StepNodeData; selected?: boolean; active: boolean }>) {
  const { t } = useI18n();
  const scopeKey = data.actionScopeKey || "";
  const [menu, setMenu] = useState({ scopeKey, open: false });
  // Frames can remain mounted and hovered after keyboard Hook navigation.
  // Reconcile only this transient menu; returning to an old scope stays closed.
  if (menu.scopeKey !== scopeKey) setMenu({ scopeKey, open: false });
  const open = menu.scopeKey === scopeKey && menu.open;
  const [opening, setOpening] = useState(false);
  const [moreAnchor, setMoreAnchor] = useState<HTMLElement | null>(null);
  const commands = useCanvasCardCommands();
  const canvasNodeId = data.step.id;
  // Duplicate, Disable and Delete; a card rendered without a canvas offers Delete alone.
  const moreItems: CanvasMenuItem[] = commands?.moreItems(canvasNodeId) ?? (data.onDeleteStep
    ? [{ key: "delete", label: t("react.workflows.delete", "Delete"), danger: true, onSelect: () => data.onDeleteStep?.(data.step.step_id || data.step.id) }]
    : []);
  // An open More stays while the pointer moves from the card into its (portaled) menu.
  if (!(selected || active || opening || open) || (data.readOnly && !data.onOpenStepTestRun)) return null;
  return <div className="workflow-selected-actions nodrag nopan" data-canvas-secondary-action="true" onPointerDown={(event) => event.stopPropagation()}>
    {data.onOpenStepTestRun ? <IconButton icon={Play} label={t("react.workflows.test_step", "Test step")} aria-busy={opening} onKeyDown={stopStepActionKeyPropagation} onClick={(event) => {
      event.stopPropagation();
      const control = event.currentTarget;
      setOpening(true);
      ignoreAsyncResult((async () => {
        try {
          if (await data.onOpenStepTestRun?.(data.step.step_id || data.step.id) === false) control.focus();
        } finally { setOpening(false); }
      })());
    }} /> : null}
    {!data.readOnly ? <>
    <Button variant="secondary" size="sm" aria-label={t("react.workflows.more_step_actions", "More actions for {name}", { name: data.step.display_name })} aria-haspopup="menu" aria-expanded={open} onKeyDown={stopStepActionKeyPropagation} onClick={(event) => { event.stopPropagation(); setMoreAnchor(event.currentTarget); setMenu((current) => ({ scopeKey, open: !(current.scopeKey === scopeKey && current.open) })); }}><MoreHorizontal className="h-4 w-4" />{t("react.workflows.more", "More")}</Button>
    {open && moreAnchor ? <CanvasMenuSurface label={t("react.workflows.more_step_actions", "More actions for {name}", { name: data.step.display_name })} anchor={moreAnchor} level={{ items: moreItems }} onClose={() => setMenu({ scopeKey, open: false })} /> : null}
    </> : null}
  </div>;
}

function renderErrorPortHandle(data: StepNodeData) {
  const port = errorPort(data);
  if (!port || (!data.errorConnected && !data.highlighted)) return null;
  return <><span className="workflow-error-label">{port.label}</span><CanvasPortControl source={{ kind: "step", id: data.step.id }} sourceLabel={data.step.display_name} port={port} readOnly={data.readOnly} position={Position.Bottom} style={{ ...branchHandleStyle, bottom: 0, left: "88%" }} /></>;
}

function stepFrameProps(data: StepNodeData, className: string, t: ReturnType<typeof useI18n>["t"]) {
  return {
    "aria-label": t("react.workflows.open_step_value0", "Open step {value0}", { value0: data.step.display_name }),
    className: joinClasses(className, validationRingClass(summaryBadge(data.step.validation_summary)), nodeFrameClasses(data)),
    "data-workflow-step-frame": "true",
    "data-connection-target": data.connectionTargetState,
    "data-run": runtimeMarkState(data.runtimeMark),
    "data-disabled": data.step.disabled ? "true" : undefined,
  };
}

function StepSummary({ data }: Readonly<{ data: StepNodeData }>) {
  const { t } = useI18n();
  const text = data.step.config_summary_key ? t(data.step.config_summary_key, data.step.config_summary, data.step.config_summary_params) : data.step.config_summary;
  return <p className={joinClasses("workflow-card-summary", data.step.kind === "http_request" ? "truncate" : "")} title={text}>{text}</p>;
}

export function GenericStepNode({ data, selected }: Readonly<NodeProps<Node<StepNodeData>>>) {
  const { t } = useI18n();
  const { step } = data;
  const visual = stepVisual(step.kind);
  const actions = useStepActionVisibility();
  const ports = routedPorts(data);
  const rows = hasOutcomeRows(step.kind, ports.length);
  return <fieldset {...stepFrameProps(data, "workflow-card group", t)} {...actions.frameProps}>
    <Handle position={Position.Left} type="target" id="in" style={{ ...runtimeHandleStyle("target", step.kind), top: 24 }} />
    {ports.map((port, index) => <CanvasPortControl key={outcomeHandleId(port.outcome)} source={{ kind: "step", id: step.id }} sourceLabel={step.display_name} port={port} readOnly={data.readOnly} position={Position.Right} style={{ ...(rows ? { top: "auto", bottom: outcomePortBottom(ports.length, index), transform: "translate(50%, 50%)" } : { top: 24 }) }} />)}
    {renderErrorPortHandle({ ...data, highlighted: data.highlighted || selected })}
    <CanvasBodyEnds source={{ kind: "step", id: step.step_id || step.id }} ends={data.bodyEnds} readOnly={data.readOnly} />
    <SelectedStepActions data={data} selected={selected} active={actions.active} /><span className="workflow-runtime-mark"><CanvasRuntimeMark mark={data.runtimeMark} /></span>
    <div className="workflow-card-content" style={rows ? { paddingBottom: ports.length * 32 + 8 } : undefined} data-workflow-step-measure-id={step.id}>
      <StepIdentity data={data} visual={visual} />
      <StepUtilityStack data={data} />
      <StepSummary data={data} />
      {rows ? <ul className="workflow-card-outcomes" aria-label={t("react.workflows.outcomes", "Outcomes")}>
        {ports.map((port) => <li className="workflow-card-outcome" data-outcome-row={outcomeHandleId(port.outcome)} key={outcomeHandleId(port.outcome)}><span>{port.label}</span>{data.outcomeTargets?.[outcomeHandleId(port.outcome)] ? <span className="workflow-card-target" title={data.outcomeTargets[outcomeHandleId(port.outcome)]}>{data.outcomeTargets[outcomeHandleId(port.outcome)]}</span> : null}</li>)}
      </ul> : null}
    </div>
  </fieldset>;
}

export function IfStepNode(props: Readonly<NodeProps<Node<StepNodeData>>>) { return <GenericStepNode {...props} />; }
export function SwitchStepNode(props: Readonly<NodeProps<Node<StepNodeData>>>) { return <GenericStepNode {...props} />; }

/**
 * Signatures of the variable outputs and the returned files, in that order;
 * undefined when the summary is names only (archived or hook contracts).
 */
function outputSignatures(summary: ContractSummary): ContractSignature[] | undefined {
  if (!summary.outputSignatures && !summary.artifactSignatures) return undefined;
  return [
    ...(summary.outputSignatures ?? summary.outputs.map((name) => ({ name }))),
    ...(summary.artifactSignatures ?? summary.artifacts.map((name) => ({ name }))),
  ];
}

export function TerminalStepNode({ data, selected }: Readonly<NodeProps<Node<StepNodeData>>>) {
  const { t } = useI18n();
  const unavailable = data.contractSummary?.unavailable ? t("react.jobs.archived_contract_unavailable", "Archived contract unavailable") : undefined;
  const visual = stepVisual(data.step.kind);
  const actions = useStepActionVisibility();
  return <fieldset {...stepFrameProps(data, "workflow-card group", t)} {...actions.frameProps}>
    <Handle position={Position.Left} type="target" id="in" style={{ ...runtimeHandleStyle("target", data.step.kind), top: 24 }} />
    <SelectedStepActions data={data} selected={selected} active={actions.active} /><span className="workflow-runtime-mark"><CanvasRuntimeMark mark={data.runtimeMark} /></span>
    <div className="workflow-card-content" data-workflow-step-measure-id={data.step.id}>
      <StepIdentity data={data} visual={visual} /><StepUtilityStack data={data} /><StepSummary data={data} />
      {data.contractSummary ? <>
        {/* Returned files are outputs: ticked on their step, listed with the variable outputs. */}
        <ContractNames title={t("react.workflows.outputs", "Outputs")} names={[...data.contractSummary.outputs, ...data.contractSummary.artifacts]} signatures={outputSignatures(data.contractSummary)} emptyLabel={unavailable} onOpen={data.onOpenInterfaceSection ? () => data.onOpenInterfaceSection?.("outputs") : undefined} />
      </> : null}
    </div>
  </fieldset>;
}
