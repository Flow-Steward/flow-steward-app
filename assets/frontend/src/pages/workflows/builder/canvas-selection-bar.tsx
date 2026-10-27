import { Copy, Trash2, X } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { useI18n } from "@/lib/i18n/useI18n";

/**
 * The selection bar: a pill at the top of the canvas for two or more selected nodes
 * (Canvas › Selecting; Components2 › Selection bar). Every action names its key.
 */
export function CanvasSelectionBar({ count, zoomDisabled, onZoom, onCopy, copyDisabled, onDuplicate, duplicateDisabled, onDelete, deleteDisabled, onClear, children }: Readonly<{
  count: number;
  zoomDisabled?: boolean;
  onZoom: () => void;
  onCopy?: () => void;
  copyDisabled?: boolean;
  onDuplicate?: () => void;
  duplicateDisabled?: boolean;
  onDelete: () => void;
  deleteDisabled?: boolean;
  onClear: () => void;
  /** Group… (CANVAS-B) sits between Duplicate and Delete. */
  children?: ReactNode;
}>) {
  const { t } = useI18n();
  return <div role="toolbar" aria-label={t("react.workflows.selected_items", "Selected items")} className="workflow-selbar nodrag nopan">
    <span className="workflow-selbar-text" role="status" aria-live="polite">{t("react.shared.selected_count", "{count} selected", { count })}</span>
    <Button size="sm" variant="ghost" type="button" disabled={zoomDisabled} title={t("react.workflows.zoom_to_selection_key", "Zoom to the selection (Shift+2)")} onClick={onZoom}>
      {t("react.workflows.zoom_to_selection", "Zoom to selection")}
    </Button>
    {onCopy ? <Button size="sm" variant="ghost" type="button" disabled={copyDisabled} title={t("react.workflows.copy_selection_key", "Copy the selection (⌘C)")} onClick={onCopy}>
      <Copy aria-hidden="true" />{t("react.workflows.copy", "Copy")}
    </Button> : null}
    {onDuplicate ? <Button size="sm" variant="ghost" type="button" disabled={duplicateDisabled} title={t("react.workflows.duplicate_selection_key", "Duplicate the selection (⌘D)")} onClick={onDuplicate}>
      {t("react.workflows.duplicate", "Duplicate")}
    </Button> : null}
    {children}
    <Button size="sm" variant="ghost" type="button" className="workflow-selbar-delete" disabled={deleteDisabled} title={t("react.workflows.delete_selection_key", "Delete the selection (Delete)")} onClick={onDelete}>
      <Trash2 aria-hidden="true" />{t("registry.common.delete", "Delete")}
    </Button>
    <IconButton icon={X} size="small" label={t("react.workflows.clear_selection_key", "Clear the selection (Esc)")} onClick={onClear} />
  </div>;
}
