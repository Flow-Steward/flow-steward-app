import { DsPopover, type PopoverTriggerProps } from "@/components/shared/popover";
import { CanvasPortControl } from "./canvas-port-control";
import { CONTAINER_PORT_CENTER_Y } from "./layout-constants";
import { CanvasContainerRuntimeMark } from "./canvas-container-runtime-mark";
import { CanvasRuntimeMark } from "./canvas-runtime-mark";
import { useI18n } from "@/lib/i18n/useI18n";
import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { Handle, Position } from "@xyflow/react";
import { MoreHorizontal, Plus, Repeat2, Group } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useCanvasCardCommands } from "./canvas-context-menus";
import { CanvasMenuSurface } from "./canvas-menu-surface";

import type { GroupNodeData } from "@/pages/workflows/builder/layout";
import {
  automationRuleTitle,
  consumeCanvasNodeClickSuppression,
  isSecondaryNodeAction,
  summaryBadge,
  suppressCanvasNodeClick,
} from "@/pages/workflows/builder/canvas-core";
import type { WorkflowCanvasScopeSummary } from "@/pages/workflows/shared/api";
import { isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

type ContainerCardProps = {
  /** "group" runs its body once; "for_each" runs it once per item. */
  containerKind: "group" | "for_each";
  containerId: string;
  displayName: string;
  /** Kind-specific badges (body shape, source list). */
  badges: ReactNode;
  data: Pick<
    GroupNodeData,
    | "automationRules"
    | "connectTargetActive"
    | "connectionTargetState"
    | "errorConnected"
    | "errorPolicy"
    | "highlighted"
    | "isEntry"
    | "lanes"
    | "laneFrames"
    | "local"
    | "ports"
    | "readOnly"
    | "runtimeMark"
    | "containerRuntimeMark"
    | "unreachable"
    | "validationIssues"
  >;
  validationSummary?: WorkflowCanvasScopeSummary;
  automationSummary?: WorkflowCanvasScopeSummary;
  onOpen: () => void;
  addDisabledReason?: string;
  /** Reactions exist for Groups; a For each has none, so its card has no automation button. */
  onAutomation?: () => void;
  onAddStep?: (laneId: string) => void;
  /** A hook frame: the same container in the hook tone, its Add step open even on a read-only canvas. */
  tone?: "hook";
  /** The settings command's label; a hook frame names itself. */
  editLabel?: string;
  addMenu?: { children: ReactNode; open: boolean; onOpenChange: (open: boolean) => void };
  /** The canvas node id and selection: a selected container shows More above it, like a step. */
  nodeId?: string;
  selected?: boolean;
};

function stopContainerActionKeyPropagation(event: KeyboardEvent<HTMLButtonElement>) {
  if (event.key === "Enter" || event.key === " ") event.stopPropagation();
}

/** More above a selected Group or For each: its right-click commands (Edit, Add step, Convert, Delete…). */
function SelectedContainerActions({ nodeId, displayName, selected, active, onActiveChange }: Readonly<{ nodeId: string; displayName: string; selected: boolean; active: boolean; onActiveChange: (active: boolean) => void }>) {
  const { t } = useI18n();
  const commands = useCanvasCardCommands();
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const items = commands?.containerItems(nodeId) ?? [];
  // Shown when the container is selected or its header is pointed at, as a step's More is.
  if (!(selected || active || open) || !items.length) return null;
  const label = t("react.workflows.more_step_actions", "More actions for {name}", { name: displayName });
  return <div className="workflow-selected-actions nodrag nopan" data-canvas-secondary-action="true" onPointerDown={(event) => event.stopPropagation()} onMouseEnter={() => onActiveChange(true)} onMouseLeave={() => onActiveChange(false)}>
    <Button variant="secondary" size="sm" aria-label={label} aria-haspopup="menu" aria-expanded={open} onKeyDown={stopContainerActionKeyPropagation} onClick={(event) => { event.stopPropagation(); setAnchor(event.currentTarget); setOpen((current) => !current); }}><MoreHorizontal className="h-4 w-4" />{t("react.workflows.more", "More")}</Button>
    {open && anchor ? <CanvasMenuSurface label={label} anchor={anchor} level={{ items }} onClose={() => setOpen(false)} /> : null}
  </div>;
}

/**
 * A container on the canvas: a Group (its body runs once) or a For each (its
 * body runs once per item). Its steps are drawn inside it; its entry handle is
 * where the outer scope reaches it, and its exit handle is where the steps
 * inside it finish.
 */
export function ContainerCard({
  containerKind,
  containerId,
  displayName,
  badges,
  data,
  validationSummary,
  automationSummary,
  onOpen,
  onAutomation,
  onAddStep,
  tone,
  addMenu,
  addDisabledReason,
  nodeId,
  selected = false,
}: Readonly<ContainerCardProps>) {
  const { t } = useI18n();
  const pointerStartRef = useRef<{ x: number; y: number } | null>(null);
  const [headerActive, setHeaderActive] = useState(false);
  const validation = summaryBadge(validationSummary);
  const automation = summaryBadge(automationSummary);
  const issueTitles = data.validationIssues.map((issue) => issue.title).filter(isTruthy);
  const hasErrorPort = data.ports.some((port) => port.outcome.name === "error");
  const addTargets = data.lanes.length ? data.lanes : [""];
  const isForEach = containerKind === "for_each";
  const isHook = tone === "hook";
  // A hook's content is the project's own workflow, so it stays addable on a protected canvas.
  const showAdd = Boolean(onAddStep) && (!data.readOnly || isHook);
  // The group goes on by its own outcomes once its body ends; the exit has no circle of its own.
  let afterLabel = "";
  if (isForEach) afterLabel = t("react.workflows.after_all_items", "After all items");
  else if (data.lanes.length) afterLabel = t("react.workflows.after_all_lanes", "After all lanes");

  const addButton = (laneId: string, popoverProps?: PopoverTriggerProps) => (
          <button
            {...popoverProps}
            aria-label={
              laneId
                ? t("react.workflows.add_step_to_lane", "Add step to lane {lane}", { lane: laneId })
                : t("react.workflows.add_step_to_value0", "Add step to {value0}", { value0: displayName })
            }
            className="workflow-container-add nodrag nopan"
            disabled={Boolean(addDisabledReason)}
            title={laneId ? t("react.workflows.add_step_to_lane", "Add step to lane {lane}", { lane: laneId }) : t("react.workflows.add_step_to_value0", "Add step to {value0}", { value0: displayName })}
            data-canvas-secondary-action="true"
            key={laneId || "body"}
            onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
              suppressCanvasNodeClick(1200);
              if (popoverProps) popoverProps.onClick();
              else onAddStep?.(laneId);
            }}
            onPointerDownCapture={() => suppressCanvasNodeClick(1200)}
            type="button"
          >
            <Plus className="h-3.5 w-3.5" />
<span className="min-w-0 truncate">{laneId
              ? t("react.workflows.add_step_to_lane", "Add step to lane {lane}", { lane: laneId })
              : t("react.workflows.add_step_variant_839bd5e0", "Add step")}</span>
          </button>
  );
  const footerTargets = addTargets.filter((laneId) => !data.laneFrames?.some((frame) => frame.id === laneId && frame.addControl));

  return (
    <div
      className={[
        "workflow-container relative flex h-full w-full flex-col border",
        isHook ? "bg-[var(--fs-action-primary-wash)]" : isForEach ? "bg-[var(--fs-workflow-lane-loop-bg)]" : "bg-[var(--fs-workflow-lane-bg)]",
        data.unreachable
          ? "border-[var(--fs-status-attention-border)] border-dashed "
          : isHook ? "border-[var(--fs-border-selected)]" : "border-[var(--fs-border-default)]",
        data.local ? "outline outline-2 outline-dashed outline-[var(--fs-status-attention-border)]" : "",
        data.connectTargetActive && !data.connectionTargetState ? "ring-2 ring-[var(--fs-border-selected)]" : "",
        data.highlighted ? "ring-2 ring-[var(--fs-border-selected)]" : "",
      ].map((value) => stringFromUnknownValue(value)).join(" ")}
      data-connection-target={data.connectionTargetState}
      {...(isForEach ? { "data-for-each-id": containerId } : { "data-group-id": containerId })}
      {...(isHook ? { "data-hook-frame": containerId } : {})}
    >
      <Handle
        id="entry"
        position={Position.Left}
        style={{ background: "var(--fs-bg-surface)", border: "1.5px solid var(--fs-workflow-edge)", height: 10, left: 0, top: CONTAINER_PORT_CENTER_Y, width: 10 }}
        title={isForEach ? t("react.workflows.for_each_entry_handle", "For each entry") : t("react.workflows.group_entry_handle", "Group entry")}
        type="target"
      />
      <Handle
        id="exit"
        position={Position.Bottom}
        style={{ background: "var(--fs-bg-surface)", border: "1.5px solid var(--fs-workflow-edge)", bottom: 0, height: 10, width: 10 }}
        title={
          isForEach
            ? t("react.workflows.for_each_exit_handle", "For each exit: each item's steps finish here")
            : t("react.workflows.group_exit_handle", "Group exit: steps inside finish here")
        }
        type="target"
      />
      {nodeId && !isHook ? <SelectedContainerActions nodeId={nodeId} displayName={displayName} selected={selected} active={headerActive} onActiveChange={setHeaderActive} /> : null}
      <div
        onMouseEnter={() => setHeaderActive(true)}
        onMouseLeave={() => setHeaderActive(false)}
        className="workflow-container-header group-drag-handle flex w-full cursor-pointer items-start justify-between gap-2 border-b border-[var(--fs-border-default)] text-left"
        title={t("react.workflows.group_header_hint", "Click to open; drag to move the group on the canvas")}
        onClick={(event) => {
          // Shift+click adds the group to the selection or takes it out (Canvas › Selecting):
          // the canvas handles it, so the header opens nothing and lets the click through.
          if (event.shiftKey) { pointerStartRef.current = null; return; }
          event.stopPropagation();
          const start = pointerStartRef.current;
          pointerStartRef.current = null;
          if (isSecondaryNodeAction(event.target)) return;
          const suppressed = consumeCanvasNodeClickSuppression();
          const moved = event.detail !== 0 && start && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 4;
          if (!suppressed && !moved) onOpen();
        }}
        onKeyDown={(event) => {
          if ((event.key === "Enter" || event.key === " ") && !isSecondaryNodeAction(event.target)) {
            event.preventDefault();
            event.stopPropagation();
            pointerStartRef.current = null;
            consumeCanvasNodeClickSuppression();
            onOpen();
          }
        }}
        onPointerDown={(event) => {
          pointerStartRef.current = { x: event.clientX, y: event.clientY };
        }}
        role="button"
        tabIndex={0}
      >
        <div className="workflow-container-heading min-w-0">
          <p className="workflow-container-title flex items-center gap-2 text-sm font-medium text-[var(--fs-text-primary)]">{isForEach ? <Repeat2 className="h-4 w-4 shrink-0" role="img" aria-label={t("react.workflows.for_each_badge", "For each item")} /> : <Group className="h-4 w-4 shrink-0" />}<span className="min-w-0 truncate" title={displayName}>{displayName}</span></p>
          <div className="workflow-card-facts">
            {badges}

            {data.isEntry ? (
              <span className="workflow-fact border-[var(--fs-status-success-border)] bg-[var(--fs-status-success-bg)] text-[var(--fs-status-success)]">
                {t("react.workflows.entry_badge", "Entry")}</span>
            ) : null}
            {data.local ? (
              <span className="workflow-fact is-warning">
                {t("react.workflows.not_applied_badge", "Not applied")}</span>
            ) : null}
            {data.unreachable ? (
              <span className="workflow-fact is-warning">
                {t("react.workflows.unreachable_badge", "Not reachable")}</span>
            ) : null}
          </div>
        </div>
        {!data.readOnly ? <div className="workflow-container-tools">
          {validation.issues ? (
            <span
              className="rounded border border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] px-1.5 py-0.5 text-[10px] text-[var(--fs-status-attention-ink)]"
              title={issueTitles.join("\n") || t("react.workflows.value0_validation_issue_s", "{value0} validation issue(s)", { value0: validation.issues })}
            >
              {validation.issues}
            </span>
          ) : null}
          {onAutomation && automation.rules ? (
            <button
              className="workflow-fact nodrag"
              data-canvas-secondary-action="true"
              onClick={(event) => {
                event.stopPropagation();
                onAutomation();
              }}
              title={automationRuleTitle(data.automationRules, automation, t)}
              type="button"
            >
              {t(automation.rules === 1 ? "react.workflows.rule_one" : "react.workflows.rules_count", automation.rules === 1 ? "{count} rule" : "{count} rules", { count: automation.rules })}
            </button>
          ) : null}
        </div> : null}
      </div>
      <CanvasPortControl source={{ kind: containerKind, id: containerId }} sourceLabel={displayName} port={data.ports.find((port) => port.outcome.name === "success") || { outcome: { name: "success" }, label: t("react.workflows.outcome_success", "Next") }} readOnly={data.readOnly} position={Position.Right} style={{ right: 0, top: CONTAINER_PORT_CENTER_Y }} />
      {afterLabel ? <span className="workflow-container-after" style={{ top: CONTAINER_PORT_CENTER_Y + 20 }}>{afterLabel}</span> : null}
      {hasErrorPort ? <div className={`workflow-container-error-port${data.errorConnected || data.highlighted ? " is-shown" : ""}`}>
        <span className="pointer-events-none absolute bottom-2 right-3 text-[10px] text-[var(--fs-status-danger)]">{t("react.workflows.outcome_error", "On error")}</span>
        <CanvasPortControl source={{ kind: containerKind, id: containerId }} sourceLabel={displayName} port={data.ports.find((port) => port.outcome.name === "error")!} readOnly={data.readOnly} position={Position.Bottom} style={{ bottom: 0, left: "calc(100% - 24px)" }} />
      </div> : null}
      <span className="workflow-runtime-mark"><CanvasRuntimeMark mark={data.runtimeMark} /><CanvasContainerRuntimeMark mark={data.containerRuntimeMark} containerId={containerId} /></span>
      {data.laneFrames?.map((lane, index) => <div className="workflow-parallel-frame" key={lane.id} style={{ left: lane.x, top: lane.y, width: lane.width, height: lane.height }}>
        <svg width="100%" height="100%" aria-hidden="true"><rect x="0.75" y="0.75" width={lane.width - 1.5} height={lane.height - 1.5} rx="12" /></svg>
        <span className="workflow-lane-label">{t("react.workflows.parallel_lane_label", "Parallel lane {lane}", { lane: index + 1 })}</span>
      </div>)}
      {showAdd && onAddStep ? <>
        {data.laneFrames?.filter((frame) => frame.addControl).map((frame) => {
          const control = frame.addControl!;
          return <div key={frame.id} className="workflow-container-lane-add" style={{ left: control.x, top: control.y, width: control.width, height: control.height }}>{addButton(frame.id)}</div>;
        })}
        {footerTargets.length || addMenu ? <div className="workflow-container-footer">{addMenu ? <DsPopover portal label={t("react.workflows.add_step_to_value0", "Add step to {value0}", { value0: displayName })} open={addMenu.open} onOpenChange={addMenu.onOpenChange} trigger={(props) => addButton("", props)}>{addMenu.children}</DsPopover> : footerTargets.map((laneId) => addButton(laneId))}{addDisabledReason ? <p className="text-xs fs-ink-secondary">{addDisabledReason}</p> : null}</div> : null}
      </> : null}
    </div>
  );
}

