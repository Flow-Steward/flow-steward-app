import { StickyNote } from "lucide-react";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type RefObject } from "react";
import { createPortal } from "react-dom";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { NOTE_DEFAULT_SIZE, type CanvasNoteDropPoint } from "@/pages/workflows/builder/canvas-notes";
import { CanvasToolbarHint } from "@/pages/workflows/builder/canvas-shell-support";

const DRAG_THRESHOLD_PX = 4;

function pointOnCanvas(canvasRoot: HTMLElement | null, x: number, y: number): boolean {
  const bounds = canvasRoot?.getBoundingClientRect();
  if (!bounds || x < bounds.left || x > bounds.right || y < bounds.top || y > bounds.bottom) return false;
  const under = document.elementFromPoint?.(x, y);
  // Dropping back on the toolbar or another floating panel places nothing.
  return !under?.closest(".react-flow__panel, .workflow-toolbar-popover");
}

/**
 * Note on the canvas toolbar: a click places a sheet in the middle of the view; dragged,
 * a contour follows the pointer and the note lands where it is dropped. Dropped off the
 * canvas, or Escape while dragging, adds nothing.
 */
export function CanvasNoteToolbarButton({ compact, disabled, canvasRootRef, zoom, onBeforePlace, onPlace }: Readonly<{
  compact: boolean;
  disabled: boolean;
  canvasRootRef: RefObject<HTMLDivElement | null>;
  zoom: () => number;
  onBeforePlace: () => void;
  onPlace: (point?: CanvasNoteDropPoint) => void;
}>) {
  const { t } = useI18n();
  const start = useRef<{ x: number; y: number; pointerId: number } | null>(null);
  const swallowClick = useRef(false);
  const [drag, setDrag] = useState<{ x: number; y: number; onCanvas: boolean } | null>(null);
  const dragging = Boolean(drag);

  useEffect(() => {
    if (!dragging) return;
    const cancel = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      start.current = null;
      setDrag(null);
    };
    globalThis.addEventListener("keydown", cancel, true);
    return () => globalThis.removeEventListener("keydown", cancel, true);
  }, [dragging]);

  const onPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (disabled || event.button !== 0) return;
    start.current = { x: event.clientX, y: event.clientY, pointerId: event.pointerId };
    swallowClick.current = false;
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };
  const onPointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const origin = start.current;
    if (origin?.pointerId !== event.pointerId) return;
    if (!drag && Math.hypot(event.clientX - origin.x, event.clientY - origin.y) < DRAG_THRESHOLD_PX) return;
    swallowClick.current = true;
    setDrag({ x: event.clientX, y: event.clientY, onCanvas: pointOnCanvas(canvasRootRef.current, event.clientX, event.clientY) });
  };
  const onPointerUp = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const origin = start.current;
    start.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    if (!origin || !drag) return;
    setDrag(null);
    if (pointOnCanvas(canvasRootRef.current, event.clientX, event.clientY)) {
      onBeforePlace();
      onPlace({ clientX: event.clientX, clientY: event.clientY });
    }
  };
  const scale = zoom();
  const width = NOTE_DEFAULT_SIZE.width * scale;
  const height = NOTE_DEFAULT_SIZE.height * scale;

  const hint = t("react.workflows.add_note_title", "Add a note (N), or drag it to its place");

  return (
    <>
      <CanvasToolbarHint compact={compact} label={hint}>{(describedBy) => <Button variant="ghost" size="md" className="workflow-toolbar-caption-action" aria-label={t("react.workflows.add_note", "Add note")} aria-describedby={describedBy}
        aria-keyshortcuts="N" title={compact ? undefined : hint} disabled={disabled}
        onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
        onPointerCancel={() => { start.current = null; setDrag(null); }}
        onClick={() => {
          if (swallowClick.current) { swallowClick.current = false; return; }
          onBeforePlace();
          onPlace();
        }}>
        <StickyNote aria-hidden="true" /><span className="workflow-toolbar-caption">{t("react.workflows.note", "Note")}</span>
      </Button>}</CanvasToolbarHint>
      {drag?.onCanvas ? createPortal(<div className="workflow-note-drop-contour" aria-hidden="true" style={{ left: drag.x - width / 2, top: drag.y - height / 2, width, height }} />, document.body) : null}
    </>
  );
}
