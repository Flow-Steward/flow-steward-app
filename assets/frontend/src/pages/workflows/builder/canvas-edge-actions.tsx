import { Plus, Trash2, X } from "lucide-react";
import type { MouseEvent } from "react";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { useI18n } from "@/lib/i18n/useI18n";
import type { CanvasPoint } from "./canvas-edge-routing";

function act(handler: () => void) {
  return (event: MouseEvent<HTMLButtonElement>) => { event.preventDefault(); event.stopPropagation(); handler(); };
}

/** Two round actions at a hovered line's middle, 16px apart: Insert a step here, and Remove connection (Components2 › Connection actions). */
export function CanvasEdgeBeads({ midpoint, visible, onInsert, onRemove, onHoverChange }: Readonly<{
  midpoint: CanvasPoint;
  visible: boolean;
  onInsert?: () => void;
  onRemove?: () => void;
  onHoverChange: (hovered: boolean) => void;
}>) {
  const { t } = useI18n();
  if (!onInsert && !onRemove) return null;
  return <div className={`workflow-edge-beads nodrag nopan${visible ? " is-visible" : ""}`} onMouseEnter={() => onHoverChange(true)} onMouseLeave={() => onHoverChange(false)}
    style={{ transform: `translate(${midpoint.x}px, ${midpoint.y}px) translate(-50%, -50%)` }}>
    {onInsert ? <IconButton className="workflow-edge-bead" icon={Plus} size="small" label={t("react.workflows.insert_step_on_connection", "Insert a step here")} onClick={act(onInsert)} /> : null}
    {onRemove ? <IconButton className="workflow-edge-bead is-remove" icon={Trash2} size="small" label={t("react.workflows.remove_connection", "Remove connection")} onClick={act(onRemove)} /> : null}
  </div>;
}

/** The selected connection in words, its ids beneath, its actions on a second row; the × or Esc closes it. */
export function CanvasConnectionBar({ title, ids, draft, onInsert, onRemove, onClose }: Readonly<{
  title: string;
  ids: string;
  draft: boolean;
  onInsert: () => void;
  onRemove: () => void;
  onClose: () => void;
}>) {
  const { t } = useI18n();
  return <div className="workflow-edgebar-dock">
    <section className="workflow-edgebar" aria-label={t("react.workflows.connection_selected", "Connection selected")}>
      <span className="workflow-edgebar-text">
        <span className="workflow-micro-label">{t("react.workflows.connection_selected", "Connection selected")}</span>
        <span className="workflow-edgebar-title" title={title}>{title}</span>
        <code className="workflow-edgebar-sub">{ids}</code>
      </span>
      {draft ? <span className="workflow-fact is-warning">{t("react.workflows.not_applied_badge", "Not applied")}</span> : null}
      <div className="workflow-edgebar-actions">
        <Button size="sm" variant="primary" type="button" onClick={onInsert}><Plus aria-hidden="true" />{t("react.workflows.insert_step_on_connection", "Insert a step here")}</Button>
        <Button size="sm" variant="danger" type="button" title={t("react.workflows.remove_connection_hint", "Remove this connection (Delete). Nothing is reconnected around it.")} onClick={onRemove}>
          {t("react.workflows.remove_connection", "Remove connection")}
        </Button>
      </div>
      <IconButton className="workflow-edgebar-x" icon={X} size="small" label={t("react.workflows.close_esc", "Close (Esc)")} onClick={onClose} />
    </section>
  </div>;
}
