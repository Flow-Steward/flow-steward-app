import { useEffect, useId, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Search } from "lucide-react";
import { DsPopover, type PopoverTriggerProps } from "@/components/shared/popover";
import { WorkflowStepIcon } from "@/pages/workflows/shared/workflow-step-icon";
import { useI18n } from "@/lib/i18n/useI18n";
import { filterPaletteEntries, PALETTE_FAMILIES, paletteEntries, paletteFamilyLabel, type PaletteEntry, type PaletteFamily } from "./canvas-palette-catalog";
import type { PaletteChoice } from "./canvas-types";
import "./canvas-menus.css";

/** The invisible anchor the palette opens from, at the pointer. */
function renderPaletteAnchor(props: PopoverTriggerProps, clientPoint: { x: number; y: number }) {
  return <button {...props} aria-hidden="true" tabIndex={-1} style={{ position: "fixed", left: clientPoint.x, top: clientPoint.y, width: 1, height: 1, padding: 0, border: 0, opacity: 0, pointerEvents: "none" }} type="button" />;
}

/** Step-type picker: no global command-menu state or query-first Escape. */
export function CanvasAddStepPalette({ clientPoint, title, subtitle, sequenceForEach, allowGroup, allowForEach, allowHookDeclaration = false, laneId, inserting, connected = inserting, busy = false, error = "", dragging = false, onChoose, onDragStart, onDismiss }: Readonly<{
  clientPoint: { x: number; y: number };
  projectId?: string;
  /** What Add step was asked for, in words: "Add after Classify request · Next". */
  title?: string;
  subtitle?: string;
  sequenceForEach: boolean;
  allowGroup: boolean;
  allowForEach: boolean;
  allowHookDeclaration?: boolean;
  /** The parent parallel lane, when adding a step into a lane. */
  laneId?: string;
  inserting: boolean;
  /** Asked for after an outcome or on a connection: Canvas › Note has no place there. */
  connected?: boolean;
  busy?: boolean;
  error?: string;
  /** A kind is being dragged out onto the canvas: the palette fades and lets the pointer through. */
  dragging?: boolean;
  onChoose: (choice: PaletteChoice) => void;
  /** A press on a row that may become a drag onto the canvas. */
  onDragStart?: (entry: PaletteEntry, event: ReactPointerEvent<HTMLButtonElement>) => void;
  onDismiss: () => void;
}>) {
  const { t, locale } = useI18n();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const id = useId();
  const familyLabel = (family: PaletteFamily) => paletteFamilyLabel(family, t);
  const entries = useMemo(
    () => paletteEntries({ sequenceForEach, allowGroup, allowForEach, allowHookDeclaration, laneId, inserting, connected }, t),
    [allowForEach, allowGroup, allowHookDeclaration, connected, inserting, laneId, sequenceForEach, t],
  );
  const results = filterPaletteEntries(entries, query, locale, familyLabel);
  const selectable = (index: number) => index >= 0 && index < results.length && !results[index].disabledReason;
  const step = (from: number, direction: 1 | -1) => {
    for (let index = from + direction; index >= 0 && index < results.length; index += direction) if (selectable(index)) return index;
    return from;
  };
  useEffect(() => { input.current?.focus(); }, []);
  useEffect(() => { if (active >= 0) list.current?.querySelector(`[data-palette-index="${active}"]`)?.scrollIntoView?.({ block: "nearest" }); }, [active]);
  const choose = (entry: PaletteEntry | undefined) => { if (entry && !entry.disabledReason && !busy) onChoose(entry.choice); };
  const label = title || t("react.workflows.add_step_title", "Add step");
  return <DsPopover label={label} open onOpenChange={(open) => { if (!open) onDismiss(); }} portal trigger={(props) => renderPaletteAnchor(props, clientPoint)}>
    <div className="workflow-palette" data-dragging={dragging || undefined}>
      <div className="workflow-palette-title">
        <p className="workflow-palette-heading">{label}</p>
        {subtitle ? <p className="workflow-palette-subtitle">{subtitle}</p> : null}
      </div>
      <div className="flex items-center gap-2 px-1">
        <Search aria-hidden="true" className="size-4 shrink-0 text-[var(--fs-text-tertiary)]" />
        <input className="fs-input min-w-0 flex-1" aria-label={t("react.workflows.search_steps", "Search steps")} placeholder={t("react.workflows.search_steps", "Search steps")} aria-activedescendant={active >= 0 ? `${id}-option-${active}` : undefined} aria-autocomplete="list" aria-controls={`${id}-list`} aria-expanded="true" autoComplete="off" data-popover-initial-focus readOnly={busy} ref={input} role="combobox" value={query} onChange={(event) => { setQuery(event.target.value); setActive(-1); }} onKeyDown={(event) => {
          if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onDismiss(); }
          else if (event.key === "ArrowDown") { event.preventDefault(); setActive((current) => step(current, 1)); }
          else if (event.key === "ArrowUp") { event.preventDefault(); setActive((current) => current < 0 ? step(results.length, -1) : step(current, -1)); }
          else if (event.key === "Home") { event.preventDefault(); setActive(step(-1, 1)); }
          else if (event.key === "End") { event.preventDefault(); setActive(step(results.length, -1)); }
          else if (event.key === "Enter" && !busy) { event.preventDefault(); choose(selectable(active) ? results[active] : results.find((entry) => !entry.disabledReason)); }
        }} />
      </div>
      <div id={`${id}-list`} ref={list} role="listbox" aria-label={t("react.workflows.add_step_title", "Add step")} className="workflow-palette-list">
        {PALETTE_FAMILIES.map((family) => { const members = results.filter((entry) => entry.family === family); return members.length ? <div role="group" aria-label={familyLabel(family)} key={family}>
          <p className="workflow-palette-family">{familyLabel(family)}</p>
          {members.map((entry) => { const index = results.indexOf(entry); const refused = Boolean(entry.disabledReason); return <button aria-selected={active === index} aria-disabled={refused || undefined} aria-label={entry.choice.displayName} aria-describedby={`${id}-description-${index}`} className="workflow-palette-option" data-active={active === index || undefined} data-palette-index={index} disabled={busy} id={`${id}-option-${index}`} key={entry.choice.catalogKey} onClick={() => choose(entry)} onMouseEnter={() => { if (!refused) setActive(index); }} onPointerDown={(event) => { if (!refused && !busy) onDragStart?.(entry, event); }} role="option" tabIndex={-1} type="button">
            <WorkflowStepIcon kind={entry.iconKind} toolCategory={entry.toolCategory} />
            <span className="workflow-palette-option-text">
              <span className="workflow-palette-option-name">{entry.choice.displayName}</span>
              <span className="workflow-palette-option-description" id={`${id}-description-${index}`}>{entry.disabledReason || entry.description}</span>
            </span>
          </button>; })}
        </div> : null; })}
        {!results.length ? <p className="p-2 text-sm text-[var(--fs-text-tertiary)]" role="status">{t("react.workflows.palette_no_results", "No matching steps")}</p> : null}
      </div>
      {error ? <p className="px-2 text-xs text-[var(--fs-status-danger)]" role="alert">{error}</p> : null}
      <p className="workflow-palette-footer" aria-hidden="true">
        <span>{t("react.workflows.palette_footer_move", "↑ ↓ move")}</span>
        <span>{t("react.workflows.palette_footer_add", "Enter add")}</span>
        <span>{t("react.workflows.palette_footer_close", "Esc close")}</span>
        {onDragStart ? <span>{t("react.workflows.palette_footer_drag", "Drag to place")}</span> : null}
      </p>
    </div>
  </DsPopover>;
}
