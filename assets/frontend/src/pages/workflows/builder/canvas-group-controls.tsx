import { canvasMotionDuration, fitCanvasToNodes } from "./canvas-viewport";
import { nextZoomStop } from "./canvas-keyboard";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable react-refresh/only-export-components */
import { memo, useState } from "react";

import { type Node, type NodeProps, useReactFlow, useStore, useViewport } from "@xyflow/react";
import { Maximize2, Minus, Plus } from "lucide-react";

import type { GroupNodeData } from "@/pages/workflows/builder/layout";
import { CanvasAddMenu } from "@/pages/workflows/builder/canvas-add-menu";
import { ContainerCard } from "@/pages/workflows/builder/canvas-container-card";
import { ForEachNode } from "@/pages/workflows/builder/canvas-for-each-node";
import { InlineLoopNode } from "@/pages/workflows/builder/canvas-inline-loop";
import {
  CanvasEditableEdge,
  GenericStepNode,
  IfStepNode,
  SwitchStepNode,
  TerminalStepNode,
  WorkflowStartNode,
} from "@/pages/workflows/builder/canvas-step-nodes";
import { IconButton } from "@/components/ui/icon-button";
import { Button } from "@/components/ui/button";
import { ignoreAsyncResult } from "@/lib/utils/async";

const BADGE_CLASS =
  "rounded-full border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] px-2 py-0.5 text-[var(--fs-text-tertiary)]";

const HOOK_BADGE_CLASS =
  "rounded-full border border-[var(--fs-border-selected)] bg-[var(--fs-bg-surface)] px-2 py-0.5 font-medium text-[var(--fs-text-brand)]";

/**
 * What a hook frame's header says after its name: Hook, and Empty while it
 * holds no steps (R2-11: a hook is a place for steps, never a named workflow).
 */
function HookFrameBadges({ hook }: Readonly<{ hook: NonNullable<GroupNodeData["group"]["hook"]> }>) {
  const { t } = useI18n();
  return (
    <>
      <span className={HOOK_BADGE_CLASS}>{t("react.workflows.hook", "Hook")}</span>
      {hook.connected !== null && !hook.declarationPending && !hook.stepCount ? (
        <span className={BADGE_CLASS} title={t("react.workflows.hook_empty_hint", "Nothing runs here yet: the run goes on with the inputs unchanged.")}>
          {t("react.workflows.hook_empty", "Empty")}</span>
      ) : null}
      {hook.usedIn > 1 ? (
        <span className="rounded-full border border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] px-2 py-0.5 text-[var(--fs-status-attention-ink)]">
          {t("react.workflows.hook_used_in_places", "Used in {count} places", { count: hook.usedIn })}</span>
      ) : null}
    </>
  );
}

/** A Group: its body runs once, as one sequence or as parallel lanes. A hook frame is drawn by it too. */
export function GroupNode({ data, id, selected }: Readonly<NodeProps<Node<GroupNodeData>>>) {
  const { t } = useI18n();
  const { group } = data;
  const [showAddMenu, setShowAddMenu] = useState(false);
  if (group.hook) {
    return (
      <ContainerCard
        automationSummary={group.automation_summary}
        badges={<HookFrameBadges hook={group.hook} />}
        addDisabledReason={group.hook.declarationPending
          ? t("react.workflows.hook_apply_before_steps", "Apply the declaration before adding its steps.")
          : group.hook.needsProject ? t("react.workflows.hook_choose_project_first", "Choose a project in the header first: each project keeps its own steps in a hook.") : undefined}
        containerId={group.hook.hookId}
        containerKind="group"
        data={data}
        displayName={group.display_name}
        editLabel={t("react.workflows.edit_hook", "Edit hook")}
        onAddStep={data.onOpenNewStep ? () => setShowAddMenu((current) => !current) : undefined}
        addMenu={{ open: showAddMenu, onOpenChange: setShowAddMenu, children: (
          <div className="nodrag nopan">
            <CanvasAddMenu
              scope="hook"
              onStep={() => { setShowAddMenu(false); data.onOpenNewStep?.({ groupId: group.id, laneId: "" }); }}
              onGroup={() => { setShowAddMenu(false); data.onOpenNewGroup?.({ containerKind: "group", hookId: group.hook!.hookId }); }}
            />
          </div>
        ) }}
        onAutomation={() => data.onAutomation(group.id)}
        onOpen={() => data.onOpenGroup(group.id)}
        tone="hook"
        validationSummary={group.validation_summary}
      />
    );
  }
  return (
    <ContainerCard
      automationSummary={group.automation_summary}
      badges={
        <span className={BADGE_CLASS}>
          {data.lanes.length
            ? t("react.workflows.group_kind_parallel", "Parallel lanes")
            : t("react.workflows.group_body_one_after_another", "One after another")}
        </span>
      }
      containerId={group.id}
      containerKind="group"
      data={data}
      displayName={group.display_name}
      onAddStep={data.onOpenNewStep ? (laneId) => data.onOpenNewStep?.({ groupId: group.id, laneId }) : undefined}
      onAutomation={() => data.onAutomation(group.id)}
      onOpen={() => data.onOpenGroup(group.id)}
      nodeId={id}
      selected={selected}
      validationSummary={group.validation_summary}
    />
  );
}

export const stepNodeTypes = {
  workflowStart: memo(WorkflowStartNode),
  action: memo(GenericStepNode),
  batch: memo(GenericStepNode),
  end: memo(TerminalStepNode),
  forEachContainer: memo(ForEachNode),
  inlineLoop: memo(InlineLoopNode),
  groupContainer: memo(GroupNode),
  ifStep: memo(IfStepNode),
  start: memo(GenericStepNode),
  step: memo(GenericStepNode),
  switchStep: memo(SwitchStepNode),
};

export const canvasEdgeTypes = {
  canvasEditable: memo(CanvasEditableEdge),
};

export function CanvasViewportControls() {
  const { t } = useI18n();
  const instance = useReactFlow();
  const { zoom } = useViewport();
  const width = useStore((state) => state.width);
  const height = useStore((state) => state.height);
  // Every title names its key (Canvas › Keys); + and − zoom by stops.
  return <div className="workflow-zoom-controls">
    <IconButton className="workflow-toolbar-icon" icon={Minus} label={t("react.workflows.zoom_out_key", "Zoom out (−)")} aria-keyshortcuts="-" onClick={() => ignoreAsyncResult(instance.zoomTo(nextZoomStop(zoom, -1), { duration: canvasMotionDuration() }))} />
    <Button variant="ghost" size="md" className="workflow-zoom-value" aria-label={t("react.workflows.zoom_to_100_key", "Zoom to 100% (Shift+0)")} title={t("react.workflows.zoom_to_100_key", "Zoom to 100% (Shift+0)")} aria-keyshortcuts="Shift+0" onClick={() => ignoreAsyncResult(instance.zoomTo(1, { duration: canvasMotionDuration() }))}>{Math.round(zoom * 100)}%</Button>
    <IconButton className="workflow-toolbar-icon" icon={Plus} label={t("react.workflows.zoom_in_key", "Zoom in (+)")} aria-keyshortcuts="+" onClick={() => ignoreAsyncResult(instance.zoomTo(nextZoomStop(zoom, 1), { duration: canvasMotionDuration() }))} />
    <IconButton className="workflow-toolbar-icon" icon={Maximize2} label={t("react.workflows.fit_to_screen_key", "Fit to screen (Shift+1)")} aria-keyshortcuts="Shift+1" onClick={() => ignoreAsyncResult(fitCanvasToNodes(instance, canvasMotionDuration(), { width, height }))} />
  </div>;
}
