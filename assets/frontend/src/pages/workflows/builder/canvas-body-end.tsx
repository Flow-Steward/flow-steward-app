import { useEffect, useRef, useState } from "react";
import { Check, Plus, RotateCw, Trash2 } from "lucide-react";
import { IconButton } from "@/components/ui/icon-button";
import { useI18n } from "@/lib/i18n/useI18n";
import type { WorkflowNodeRef } from "@/pages/workflows/shared/api";
import { outcomeFromHandleId } from "./api";
import { portEdge } from "./change-set";
import { useCanvasPortActions } from "./canvas-port-control";
import type { StepBodyEnd } from "./layout";
import "./canvas-menus.css";

/** The name, the "what next" sentence and the disconnect label of a body's end, as its card says them. */
function bodyEndText(kind: StepBodyEnd["kind"], t: ReturnType<typeof useI18n>["t"]): { name: string; next: string; disconnect: string } {
  if (kind === "item") return { name: t("react.workflows.body_end_item", "Next item"), next: t("react.workflows.body_end_item_hint", "After this step the loop moves on to the next item."), disconnect: t("react.workflows.body_end_disconnect_item", "Disconnect from the next item") };
  if (kind === "hook") return { name: t("react.workflows.body_end_hook", "End of the hook"), next: t("react.workflows.body_end_hook_hint", "After this step the hook is finished; the workflow goes on with its results."), disconnect: t("react.workflows.body_end_disconnect_hook", "Disconnect from the end of the hook") };
  if (kind === "lane") return { name: t("react.workflows.body_end_lane", "End of the lane"), next: t("react.workflows.body_end_lane_hint", "After this step the lane is finished; the group goes on once every lane is."), disconnect: t("react.workflows.body_end_disconnect_lane", "Disconnect from the end of the lane") };
  return { name: t("react.workflows.body_end_group", "End of the group"), next: t("react.workflows.body_end_group_hint", "After this step the group is finished and the workflow continues after it."), disconnect: t("react.workflows.body_end_disconnect_group", "Disconnect from the end of the group") };
}

/** How long the card stays after the pointer leaves, so it can be reached across the gap. */
const BODY_END_CLOSE_DELAY_MS = 200;

/**
 * A step that ends its group's body: a short stub to a BPMN end circle beside
 * the port it leaves from, in place of a line to the group's exit — the loop
 * arrow (Next item) in a For each, a check (Lane done) in parallel lanes.
 * Hovering or focusing it names it, says what comes next and offers Insert a
 * step here and Remove connection.
 */
export function CanvasBodyEnds({ source, ends, readOnly }: Readonly<{ source: WorkflowNodeRef; ends?: readonly StepBodyEnd[]; readOnly?: boolean }>) {
  if (!ends?.length) return null;
  return <>{ends.map((end) => <BodyEnd key={end.handle} source={source} end={end} readOnly={readOnly} />)}</>;
}

function BodyEnd({ source, end, readOnly }: Readonly<{ source: WorkflowNodeRef; end: StepBodyEnd; readOnly?: boolean }>) {
  const { t } = useI18n();
  const actions = useCanvasPortActions();
  const [open, setOpen] = useState(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelClose = () => { if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null; } };
  const show = () => { cancelClose(); setOpen(true); };
  const hideSoon = () => { cancelClose(); closeTimer.current = setTimeout(() => { closeTimer.current = null; setOpen(false); }, BODY_END_CLOSE_DELAY_MS); };
  useEffect(() => cancelClose, []);
  const text = bodyEndText(end.kind, t);
  const outcome = outcomeFromHandleId(end.handle);
  const edge = actions && outcome ? portEdge(actions.definition, source, outcome) : null;
  const editable = Boolean(edge && actions && !actions.disabled && !readOnly);
  const Icon = end.kind === "item" ? RotateCw : Check;
  return <div className="workflow-body-end nodrag nopan" data-kind={end.kind} data-canvas-secondary-action="true" style={{ left: end.x, top: end.y }}
    onMouseEnter={show} onMouseLeave={hideSoon}
    onFocusCapture={show} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) { cancelClose(); setOpen(false); } }}>
    <span className="workflow-body-end-stub" aria-hidden="true" />
    <span className="relative">
      <button type="button" className="workflow-body-end-circle" aria-label={`${text.name} · ${text.next}`} aria-expanded={open}>
        <Icon aria-hidden="true" />
      </button>
      {open ? <span className="workflow-body-end-tip" role="tooltip">
        <span><strong>{text.name}</strong> · {text.next}</span>
        {editable ? <span className="workflow-body-end-actions">
          <IconButton icon={Plus} size="small" label={t("react.workflows.insert_step_on_connection", "Insert a step here")} onClick={(event) => { event.stopPropagation(); if (edge) actions?.insert?.(edge); }} />
          <IconButton icon={Trash2} size="small" label={text.disconnect} onClick={(event) => { event.stopPropagation(); if (outcome) actions?.disconnect(source, outcome); }} />
        </span> : null}
      </span> : null}
    </span>
  </div>;
}
