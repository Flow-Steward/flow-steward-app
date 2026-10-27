import { NodeResizer, type Node, type NodeProps } from "@xyflow/react";
import { ImagePlus, MoreHorizontal, Pencil, StickyNote, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type RefObject } from "react";

import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { renderMarkdownBlocks } from "@/pages/extensions/extension-markdown";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { FileThumbnail } from "@/components/shared/file-thumbnail";
import { noteImageUrl, type CanvasNote } from "@/pages/workflows/builder/canvas-notes-api";
import { NOTE_MIN_SIZE, snapToCanvasGrid, type CanvasNoteActions } from "@/pages/workflows/builder/canvas-notes";
import { CANVAS_GRID } from "@/pages/workflows/builder/layout-constants";

export type CanvasNoteData = {
  note: CanvasNote;
  workflowId: string;
  busy: boolean;
  editing: boolean;
  actions: CanvasNoteActions;
};

const NOTE_IMAGE_TYPES = "image/png,image/jpeg,image/gif,image/webp";

/** The paper's natural height (label, text, image), so a resize never clips what is written. */
function useContentHeight(contentRef: RefObject<HTMLDivElement | null>) {
  const [height, setHeight] = useState(0);
  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const measure = () => setHeight(content.offsetHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(content);
    return () => observer.disconnect();
  }, [contentRef]);
  return height;
}

type MenuItem = { key: string; label: string; danger?: boolean; onSelect: () => void };

function CanvasNoteMenu({ label, items, style, onClose }: Readonly<{ label: string; items: MenuItem[]; style?: CSSProperties; onClose: (restoreFocus: boolean) => void }>) {
  const menuRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);
  useEffect(() => {
    const frame = globalThis.requestAnimationFrame(() => menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus());
    const close = (event: PointerEvent) => { if (!menuRef.current?.contains(event.target as globalThis.Node)) onCloseRef.current(false); };
    document.addEventListener("pointerdown", close, true);
    return () => { globalThis.cancelAnimationFrame(frame); document.removeEventListener("pointerdown", close, true); };
  }, []);
  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const entries = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    const index = entries.indexOf(document.activeElement as HTMLElement);
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(true); }
    else if (event.key === "ArrowDown") { event.preventDefault(); entries[(index + 1) % entries.length]?.focus(); }
    else if (event.key === "ArrowUp") { event.preventDefault(); entries[(index - 1 + entries.length) % entries.length]?.focus(); }
    else if (event.key === "Enter" || event.key === " ") event.stopPropagation();
  };
  return (
    <div ref={menuRef} className="workflow-step-menu workflow-note-menu nodrag nopan" role="menu" aria-label={label} tabIndex={-1} style={style} onKeyDown={onKeyDown} onPointerDown={(event) => event.stopPropagation()}>
      {items.map((item) => <Button key={item.key} variant={item.danger ? null : "ghost"} className={item.danger ? "ghost-danger" : undefined} role="menuitem" onClick={(event) => { event.stopPropagation(); onClose(false); item.onSelect(); }}>{item.label}</Button>)}
    </div>
  );
}

/** Writing in place: the paper stays, Image, Delete and Done sit under the text. */
function CanvasNoteWriter({ note, workflowId, busy, actions, paperRef }: Readonly<{ note: CanvasNote; workflowId: string; busy: boolean; actions: CanvasNoteActions; paperRef: RefObject<HTMLDivElement | null> }>) {
  const { t } = useI18n();
  const [text, setText] = useState(note.text);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const latestText = useRef(text);
  useEffect(() => { latestText.current = text; }, [text]);

  useEffect(() => {
    const field = textRef.current;
    if (!field) return;
    field.focus();
    field.setSelectionRange(field.value.length, field.value.length);
  }, []);
  useLayoutEffect(() => {
    const field = textRef.current;
    if (!field) return;
    field.style.height = "auto";
    field.style.height = `${field.scrollHeight}px`;
  }, [text]);
  const leaving = useRef(false);
  // One way out at a time: a click outside and the blur it causes leave writing once.
  const leave = useCallback(() => {
    if (leaving.current) return;
    leaving.current = true;
    ignoreAsyncResult(actions.finish(latestText.current).then((left) => { if (!left) leaving.current = false; }));
  }, [actions]);
  // A click anywhere outside the paper keeps the text and leaves writing.
  useEffect(() => {
    const leaveOnOutsidePointer = (event: PointerEvent) => {
      if (!paperRef.current?.contains(event.target as globalThis.Node)) leave();
    };
    document.addEventListener("pointerdown", leaveOnOutsidePointer, true);
    return () => document.removeEventListener("pointerdown", leaveOnOutsidePointer, true);
  }, [leave, paperRef]);

  const finish = leave;
  return (
    <div className="workflow-note-edit nodrag nopan nowheel" onBlur={(event) => {
      const next = event.relatedTarget;
      if (next instanceof globalThis.Node && !paperRef.current?.contains(next)) finish();
    }}>
      <label className="sr-only" htmlFor={`note-text-${note.id}`}>{t("react.workflows.note_text", "Note text")}</label>
      <textarea ref={textRef} id={`note-text-${note.id}`} className="workflow-note-text" rows={2} maxLength={20000} value={text}
        placeholder={t("react.workflows.note_placeholder", "Write a note… Markdown works")}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape" || (event.key === "Enter" && (event.metaKey || event.ctrlKey))) {
            event.preventDefault();
            event.stopPropagation();
            finish();
          }
        }} />
      {note.image ? <div className="workflow-note-image-row">
        <FileThumbnail key={note.image.artifact_id} name={note.image.filename} previewUrl={noteImageUrl(workflowId, note)} description={t("react.workflows.note_image_description", "Attached to this workflow note.")} imageClassName="workflow-note-thumb" />
        <IconButton icon={X} iconSize={14} size="small" label={t("react.workflows.note_remove_image", "Remove image")} disabled={busy} onClick={() => ignoreAsyncResult(actions.clearImage(note.id))} />
      </div> : null}
      <div className="workflow-note-bar">
        <Button asChild variant="ghost" size="sm" aria-disabled={busy}>
          <label className="workflow-note-file" title={t("react.workflows.note_add_image_title", "Add image: PNG, JPEG, GIF or WebP")}>
            <ImagePlus aria-hidden="true" /><span className="workflow-note-file-text">{t("react.workflows.note_image_button", "Image")}</span>
            <input className="sr-only" type="file" accept={NOTE_IMAGE_TYPES} disabled={busy} aria-label={t("react.workflows.note_add_image", "Add image")} onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) ignoreAsyncResult(actions.attachImage(file, text));
              event.target.value = "";
            }} />
          </label>
        </Button>
        <span className="workflow-note-spacer" aria-hidden="true" />
        <Button variant={null} size="sm" className="ghost-danger" disabled={busy} onClick={() => actions.removeWithUndo(note.id)}>{t("react.workflows.note_delete", "Delete")}</Button>
        <Button variant="primary" size="sm" disabled={busy} title={t("react.workflows.note_done_title", "Done (Esc)")} aria-keyshortcuts="Escape Meta+Enter Control+Enter" onClick={finish}>{t("react.workflows.note_done", "Done")}</Button>
      </div>
    </div>
  );
}

export function CanvasNoteNode({ data, selected }: Readonly<NodeProps<Node<CanvasNoteData>>>) {
  const { t } = useI18n();
  const { note, busy, workflowId, editing, actions } = data;
  const paperRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [menu, setMenu] = useState<{ kind: "toolbar" } | { kind: "context"; x: number; y: number } | null>(null);
  const contentHeight = useContentHeight(contentRef);
  const minHeight = Math.max(NOTE_MIN_SIZE.height, Math.ceil(contentHeight / CANVAS_GRID) * CANVAS_GRID);
  const title = note.text.trim().split("\n")[0]?.replace(/^#+\s*/, "").trim();

  // The note's node is the focus stop: Enter writes, the context-menu key opens the menu.
  useEffect(() => {
    const wrapper = paperRef.current?.closest<HTMLElement>(".react-flow__node");
    if (!wrapper || busy || editing) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target !== wrapper) return;
      const plain = !event.metaKey && !event.ctrlKey && !event.altKey;
      if (plain && !event.shiftKey && event.key === "Enter") {
        event.preventDefault(); event.stopPropagation(); actions.edit(note);
      } else if (event.key === "ContextMenu" || (plain && event.shiftKey && event.key === "F10")) {
        event.preventDefault(); event.stopPropagation(); setMenu({ kind: "toolbar" });
      }
    };
    wrapper.addEventListener("keydown", onKeyDown);
    return () => wrapper.removeEventListener("keydown", onKeyDown);
  }, [actions, busy, editing, note]);

  const closeMenu = (restoreFocus: boolean) => {
    setMenu(null);
    if (restoreFocus) paperRef.current?.closest<HTMLElement>(".react-flow__node")?.focus();
  };
  const menuItems: MenuItem[] = [
    { key: "edit", label: t("react.workflows.note_edit", "Edit"), onSelect: () => actions.edit(note) },
    { key: "duplicate", label: t("react.workflows.duplicate", "Duplicate"), onSelect: () => ignoreAsyncResult(actions.duplicate(note)) },
    { key: "delete", label: t("react.workflows.note_delete", "Delete"), danger: true, onSelect: () => actions.removeWithUndo(note.id) },
  ];
  const openContextMenu = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (editing || busy) return;
    event.preventDefault();
    event.stopPropagation();
    const paper = paperRef.current;
    const rect = paper?.getBoundingClientRect();
    const scale = rect && paper?.offsetWidth ? rect.width / paper.offsetWidth : 1;
    setMenu({ kind: "context", x: rect ? (event.clientX - rect.left) / scale : 0, y: rect ? (event.clientY - rect.top) / scale : 0 });
  };
  const showToolbar = selected && !editing && !busy;

  return (
    <div ref={paperRef} className="workflow-note" data-note-id={note.id} data-selected={selected ? "true" : "false"} data-editing={editing ? "true" : "false"}
      role="group" aria-label={title ? t("react.workflows.note_named", "Note: {title}", { title }) : t("react.workflows.canvas_note", "Note")}
      onDoubleClick={(event) => { if (!editing && !busy) { event.stopPropagation(); actions.edit(note); } }}
      onContextMenu={openContextMenu}>
      <NodeResizer minWidth={NOTE_MIN_SIZE.width} minHeight={minHeight} maxWidth={4000} maxHeight={4000} isVisible={showToolbar}
        onResizeEnd={(_, size) => ignoreAsyncResult(actions.save({
          ...note, x: snapToCanvasGrid(size.x), y: snapToCanvasGrid(size.y),
          width: Math.max(NOTE_MIN_SIZE.width, snapToCanvasGrid(size.width)), height: Math.max(minHeight, snapToCanvasGrid(size.height)),
        }))} />
      <div ref={contentRef} className="workflow-note-content">
        <div className="workflow-note-label"><StickyNote aria-hidden="true" /><span>{t("react.workflows.canvas_note", "Note")}</span></div>
        {editing ? <CanvasNoteWriter note={note} workflowId={workflowId} busy={busy} actions={actions} paperRef={paperRef} /> : <div className="workflow-note-body">
          {renderMarkdownBlocks(note.text)}
          {note.image ? <FileThumbnail key={note.image.artifact_id} name={note.image.filename} previewUrl={noteImageUrl(workflowId, note)} description={t("react.workflows.note_image_description", "Attached to this workflow note.")} imageClassName="workflow-note-image" /> : null}
        </div>}
      </div>
      {showToolbar ? <div className="workflow-selected-actions nodrag nopan" role="toolbar" aria-label={t("react.workflows.note_actions", "Note actions")} data-canvas-secondary-action="true" onPointerDown={(event) => event.stopPropagation()}>
        <IconButton icon={Pencil} iconSize={16} size="small" label={t("react.workflows.edit_note_shortcut", "Edit note (Enter)")} aria-keyshortcuts="Enter" onClick={(event) => { event.stopPropagation(); actions.edit(note); }} />
        <div className="relative">
          <IconButton icon={MoreHorizontal} iconSize={16} size="small" label={t("react.workflows.more_note_actions", "More note actions")} aria-haspopup="menu" aria-expanded={menu?.kind === "toolbar"} onClick={(event) => { event.stopPropagation(); setMenu((current) => current?.kind === "toolbar" ? null : { kind: "toolbar" }); }} />
          {menu?.kind === "toolbar" ? <CanvasNoteMenu label={t("react.workflows.more_note_actions", "More note actions")} items={menuItems.slice(1)} onClose={closeMenu} /> : null}
        </div>
      </div> : null}
      {menu?.kind === "toolbar" && !showToolbar ? <CanvasNoteMenu label={t("react.workflows.note_actions", "Note actions")} items={menuItems} style={{ left: 0, right: "auto", top: 0 }} onClose={closeMenu} /> : null}
      {menu?.kind === "context" ? <CanvasNoteMenu label={t("react.workflows.note_actions", "Note actions")} items={menuItems} style={{ left: menu.x, right: "auto", top: menu.y }} onClose={closeMenu} /> : null}
    </div>
  );
}
