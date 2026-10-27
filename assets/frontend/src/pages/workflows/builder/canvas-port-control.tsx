/* eslint-disable react-refresh/only-export-components -- the port context, its provider and reader belong together. */
import { createContext, useContext, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";
import { Handle, type Position } from "@xyflow/react";
import { Plus } from "lucide-react";
import { DsPopover, type PopoverTriggerProps } from "@/components/shared/popover";
import { useI18n } from "@/lib/i18n/useI18n";
import type { WorkflowCanvasPort, WorkflowGraphEdge, WorkflowNodeRef, WorkflowOutcome, WorkflowTargetRef } from "../shared/api";
import { outcomeHandleId, targetKey } from "./api";
import { portEdge, type WorkflowDefinitionModel } from "./change-set";
import { connectionPlacement, connectionRefusal, scopeExitTargetLabel, scopeTargetOptions } from "./canvas-connection-policy";
import { suppressCanvasNodeClick, canvasPortKey } from "./canvas-core";
import type { CanvasCreationIntent } from "./canvas-types";

type PortActions = {
  definition: WorkflowDefinitionModel;
  dragging: boolean;
  insert?: (edge: WorkflowGraphEdge) => void;
  creationPortKey?: string | null;
  append: (source: WorkflowNodeRef, outcome: WorkflowOutcome, intent?: CanvasCreationIntent) => void;
  connect: (source: WorkflowNodeRef, outcome: WorkflowOutcome, target: WorkflowTargetRef, rework?: boolean) => Promise<boolean> | boolean;
  disconnect: (source: WorkflowNodeRef, outcome: WorkflowOutcome) => void;
  notice: (label: string) => void;
  disabled?: boolean;
};
const PortContext = createContext<PortActions | null>(null);
export function CanvasPortProvider({ value, children }: Readonly<{ value: PortActions; children: ReactNode }>) { return <PortContext.Provider value={value}>{children}</PortContext.Provider>; }
/** The canvas's connection actions (insert, disconnect) for controls beside a port, such as a body's end circle. */
export function useCanvasPortActions(): PortActions | null { return useContext(PortContext); }

type PortButtonOptions = {
  buttonRef: RefObject<HTMLButtonElement | null>;
  pointer: RefObject<{ x: number; y: number } | null>;
  actions: PortActions | null;
  menuOnActivate: boolean;
  expanded: boolean;
  label: string;
  disabled: boolean | undefined;
  openMenu: () => void;
  requestAdd: () => void;
};
/** The port's own button: a click adds or opens the menu, a drag past 4px does neither. */
function renderPortButton(props: PopoverTriggerProps, { buttonRef, pointer, actions, menuOnActivate, expanded, label, disabled, openMenu, requestAdd }: PortButtonOptions) {
  return <button {...props} ref={(button) => { props.ref.current = button; buttonRef.current = button; }} aria-haspopup={menuOnActivate ? "menu" : "dialog"} aria-expanded={expanded} aria-keyshortcuts="Shift+F10" aria-label={label} className="workflow-port-control" type="button" disabled={disabled}
    onMouseDown={(event) => { pointer.current = { x: event.clientX, y: event.clientY }; }}
    onClick={(event) => { event.stopPropagation(); const start = pointer.current; pointer.current = null; if (actions?.dragging || (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 4)) return;
      if (menuOnActivate) openMenu(); else requestAdd(); }}
    onContextMenu={(event) => { event.preventDefault(); event.stopPropagation(); openMenu(); }}
    onKeyDown={(event) => { if ((event.key === "F10" && event.shiftKey) || event.key === "ContextMenu") { event.preventDefault(); event.stopPropagation(); openMenu(); } else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); if (menuOnActivate) openMenu(); else requestAdd(); } }}><Plus aria-hidden="true" size={12} strokeWidth={2} /></button>;
}

/** One interactive composition over the accepted static 10px ReactFlow handle. */
export function CanvasPortControl({ source, sourceLabel, port, position, style, readOnly, occupied: occupiedOverride }: Readonly<{
  source: WorkflowNodeRef;
  sourceLabel: string;
  port: WorkflowCanvasPort;
  position: Position;
  style: CSSProperties;
  readOnly?: boolean;
  /** Useful when rendering a static card without a graph controller. */
  occupied?: boolean;
}>) {
  const actions = useContext(PortContext); const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const portButtonRef = useRef<HTMLButtonElement>(null);
  const pointer = useRef<{ x: number; y: number } | null>(null);
  const edge = actions ? portEdge(actions.definition, source, port.outcome) : null;
  const occupied = occupiedOverride ?? Boolean(edge);
  const disabled = readOnly || actions?.disabled || !actions;
  const handle = outcomeHandleId(port.outcome);
  const placement = actions ? connectionPlacement(actions.definition, source) : null;
  const targets = actions && placement ? scopeTargetOptions(actions.definition, placement, source, scopeExitTargetLabel(placement, t), t) : [];
  const permittedTargets = targets.filter((target) => !actions || !connectionRefusal(actions.definition, source, port.outcome, target.target, false, t));
  // A free body Next can finish the current item through the existing scoped menu.
  const menuOnActivate = occupied || (source.kind === "step" && port.outcome.name === "success" && permittedTargets.some((target) => target.target.kind === "for_each_exit"));
  const label = menuOnActivate
    ? t("react.workflows.connect_port_menu", "Connect {step} · {outcome}", { step: sourceLabel, outcome: port.label })
    : t("react.workflows.add_step_after_outcome_unconnected", "Add a step after {step} · {outcome}, not connected", { step: sourceLabel, outcome: port.label });
  const requestAdd = () => { if (!disabled && !actions?.dragging) { setOpen(false); suppressCanvasNodeClick(); if (occupied) { if (edge) actions?.insert?.(edge); } else actions?.append(source, port.outcome); } };
  // The builder no longer marks a rework (repeat) path; a saved one still loads, runs and draws as one.
  const connect = async (target: WorkflowTargetRef) => {
    if (!actions || disabled) return;
    const refusal = connectionRefusal(actions.definition, source, port.outcome, target, false, t);
    if (refusal) { actions.notice(refusal.label); return; }
    // The real confirmation hook captures its return target synchronously.
    // A menu item may unmount on the modal outside press; the port remains.
    portButtonRef.current?.focus();
    try { const staged = await actions.connect(source, port.outcome, target); if (staged) setOpen(false); }
    catch (error) { actions.notice(error instanceof Error ? error.message : t("react.workflows.connection_change_failed", "The connection could not be changed.")); }
  };
  const openMenu = () => {
    if (disabled) return;
    suppressCanvasNodeClick(); setOpen(true);
  };
  const active = open || actions?.creationPortKey === canvasPortKey(source, port.outcome);
  return <Handle id={handle} position={position} type="source" isConnectable={!disabled} style={{ ...style, height: 10, width: 10, background: "transparent", border: "none" }} title={port.label} className={`workflow-port-handle${port.outcome.name === "error" ? " is-error" : ""}`}>
    <div className="workflow-port-anchor nodrag nopan" data-canvas-secondary-action="true" data-port-dragging={actions?.dragging || undefined} data-port-active={active || undefined} data-port-occupied={occupied || undefined}>
      <DsPopover portal open={open} onOpenChange={setOpen} label={t("react.workflows.connections", "Connections")} trigger={(props) => renderPortButton(props, { buttonRef: portButtonRef, pointer, actions, menuOnActivate, expanded: open || Boolean(active), label, disabled, openMenu, requestAdd })}>
        <div role="menu" tabIndex={-1} aria-label={t("react.workflows.connections", "Connections")} className="workflow-port-menu" onPointerDown={(event) => event.stopPropagation()} onClick={(event) => event.stopPropagation()} onMouseDown={(event) => event.stopPropagation()} onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
            event.preventDefault(); event.stopPropagation(); const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"], [role="menuitemcheckbox"]')]; const index = items.indexOf(document.activeElement as HTMLButtonElement); const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length; items[next]?.focus();
          }
        }}>
          <button type="button" role="menuitem" aria-keyshortcuts="A" onClick={requestAdd}>{t("react.workflows.new_step_menu", "New step…")}<kbd className="workflow-menu-kbd" aria-hidden="true">A</kbd></button>
          {permittedTargets.map((target) => <button type="button" role="menuitem" key={target.key} aria-current={edge && targetKey(edge.target) === target.key ? "true" : undefined} onClick={() => { void connect(target.target); }}>{target.label}</button>)}
          {occupied ? <button type="button" role="menuitem" onClick={() => { try { actions?.disconnect(source, port.outcome); setOpen(false); } catch (error) { actions?.notice(error instanceof Error ? error.message : t("react.workflows.connection_change_failed", "The connection could not be changed.")); } }}>{t("react.workflows.remove_connection", "Remove connection")}</button> : null}
        </div>
      </DsPopover>
    </div>
  </Handle>;
}
