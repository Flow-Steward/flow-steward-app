/* eslint-disable react-refresh/only-export-components -- the arrange store, its hook and its control belong together. */
import { useCallback, useState, type Dispatch, type SetStateAction } from "react";
import type { Node } from "@xyflow/react";
import { Check, ChevronDown, ChevronUp } from "lucide-react";
import { DsPopover, type PopoverTriggerProps } from "@/components/shared/popover";
import type { I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { CanvasAutoLayoutIcon } from "./canvas-shell-support";
import type { CanvasPosition, GraphEditNotice } from "./canvas-core";
import "./canvas-menus.css";

/** Automatically: every step is placed by the layout after each change. Manually: steps stay where they are put. */
export type CanvasArrangeMode = "auto" | "manual";

export function arrangeModeStorageKey(workflowId: string) {
  return `platform.workflowCanvas.arrange.${workflowId}`;
}

/** The choice is kept per workflow; without one, a canvas that already holds manual places is Manually. */
export function loadArrangeMode(workflowId: string, hasManualPositions: boolean): CanvasArrangeMode {
  try {
    const stored = globalThis.localStorage?.getItem(arrangeModeStorageKey(workflowId));
    if (stored === "auto" || stored === "manual") return stored;
  } catch { /* Storage can be unavailable; the default still works. */ }
  return hasManualPositions ? "manual" : "auto";
}

export function saveArrangeMode(workflowId: string, mode: CanvasArrangeMode) {
  try { globalThis.localStorage?.setItem(arrangeModeStorageKey(workflowId), mode); } catch { /* Kept for this session only. */ }
}

/** Every movable step and group where it stands now: the places Manually fixes. */
export function arrangeSnapshot(nodes: readonly Node[], current: Record<string, CanvasPosition> = {}): Record<string, CanvasPosition> {
  const positions: Record<string, CanvasPosition> = {};
  for (const node of nodes) {
    if (node.type === "canvasNote" || node.type === "workflowStart" || node.id.startsWith("visual:") || node.hidden || node.draggable === false) continue;
    positions[node.id] = { ...node.position };
  }
  return { ...positions, ...current };
}

type ArrangeOptions = {
  workflowId: string;
  t: I18nContextValue["t"];
  manualPositions: Record<string, CanvasPosition>;
  setManualPositions: Dispatch<SetStateAction<Record<string, CanvasPosition>>>;
  /** The layout's own places (no manual overlay). */
  layoutNodes: readonly Node[];
  setGraphEditNotice: Dispatch<SetStateAction<GraphEditNotice | null>>;
  /** Take the last change back (the placement intent of the drag that switched to Manually). */
  undoLastChange?: () => void;
};

/**
 * Layout › Arrange, kept per workflow. While Automatically, positions placed
 * by hand are not used; a manual placement (a drag, Shift+arrows, a drop, a
 * paste at the pointer) turns it to Manually with every other step kept where
 * it stands, and the toast offers Undo.
 */
export function useCanvasArrangeMode({ workflowId, t, manualPositions, setManualPositions, layoutNodes, setGraphEditNotice, undoLastChange }: ArrangeOptions) {
  const hasManual = Object.keys(manualPositions).length > 0;
  const [state, setState] = useState(() => ({ workflowId, mode: loadArrangeMode(workflowId, hasManual), settled: false }));
  // Another workflow keeps its own choice (render-phase reset, not an effect).
  if (state.workflowId !== workflowId) setState({ workflowId, mode: loadArrangeMode(workflowId, hasManual), settled: false });
  const mode = state.workflowId === workflowId ? state.mode : loadArrangeMode(workflowId, hasManual);
  const setMode = useCallback((next: CanvasArrangeMode) => {
    saveArrangeMode(workflowId, next);
    setState({ workflowId, mode: next, settled: true });
  }, [workflowId]);
  if (state.workflowId === workflowId && !state.settled) {
    // Opening in Automatically: places left from an earlier session are not used.
    setState({ ...state, settled: true });
    if (state.mode === "auto" && hasManual) setManualPositions({});
  } else if (state.workflowId === workflowId && state.mode === "auto" && hasManual) {
    // A manual placement while Automatically: Manually now, every other step kept where it stands.
    saveArrangeMode(workflowId, "manual");
    setState({ ...state, mode: "manual" });
    setManualPositions((current) => arrangeSnapshot(layoutNodes, current));
    setGraphEditNotice({
      label: t("react.workflows.layout_switched_manual", "Layout: manually. Steps stay where you put them."),
      tone: "success",
      action: { label: t("react.workflows.undo", "Undo"), onAction: () => { undoLastChange?.(); setMode("auto"); setManualPositions({}); } },
    });
  }
  const choose = useCallback((next: CanvasArrangeMode) => {
    if (next === mode) return;
    setMode(next);
    if (next === "manual") setManualPositions((current) => arrangeSnapshot(layoutNodes, current));
    else setManualPositions({});
    setGraphEditNotice({
      label: next === "manual"
        ? t("react.workflows.layout_now_manual", "Layout: manually. Steps stay where you put them.")
        : t("react.workflows.layout_now_auto", "Layout: automatically. Steps rearrange after every change."),
      tone: "success",
    });
  }, [layoutNodes, mode, setGraphEditNotice, setManualPositions, setMode, t]);
  return { mode, choose };
}

/** The Layout arrow that opens Arrange; it points up while the menu is open. */
function renderLayoutArrow(props: PopoverTriggerProps, open: boolean, label: string) {
  return (
    <button {...props} aria-label={label} aria-pressed={open} className="fs-icon-button workflow-toolbar-icon workflow-layout-arrow" title={label} type="button">
      {open ? <ChevronUp aria-hidden="true" size={16} /> : <ChevronDown aria-hidden="true" size={16} />}
    </button>
  );
}

/** Auto layout with the Layout arrow beside it: Arrange › Automatically / Manually. */
export function CanvasLayoutControl({ mode, onChoose, onAutoLayout, help, onOpen }: Readonly<{
  mode: CanvasArrangeMode;
  onChoose: (mode: CanvasArrangeMode) => void;
  onAutoLayout: () => void;
  help: string;
  /** Other toolbar surfaces close when Layout opens. */
  onOpen?: () => void;
}>) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [tooltip, setTooltip] = useState(false);
  const options: Array<{ value: CanvasArrangeMode; label: string; hint: string }> = [
    { value: "auto", label: t("react.workflows.layout_auto", "Automatically"), hint: t("react.workflows.layout_auto_hint", "Steps rearrange after every change") },
    { value: "manual", label: t("react.workflows.layout_manual", "Manually"), hint: t("react.workflows.layout_manual_hint", "Steps stay where you put them") },
  ];
  return <span className="workflow-layout-split">
    <span className="fs-icon-button-wrap" onMouseEnter={() => setTooltip(true)} onMouseLeave={() => setTooltip(false)}>
      <button className="fs-button ghost workflow-toolbar-caption-action" aria-label={t("react.workflows.auto_layout", "Auto layout")} aria-describedby={tooltip ? "workflow-auto-layout-tooltip" : undefined} aria-keyshortcuts="Shift+A" title={help} type="button" onFocus={() => setTooltip(true)} onBlur={() => setTooltip(false)} onClick={() => { setOpen(false); onAutoLayout(); }}>
        <CanvasAutoLayoutIcon />
        <span className="workflow-toolbar-caption">{t("react.workflows.auto_layout", "Auto layout")}</span>
      </button>
      {tooltip ? <span id="workflow-auto-layout-tooltip" className="fs-shell-tooltip" role="tooltip">{help}</span> : null}
    </span>
    <DsPopover label={t("react.workflows.layout", "Layout")} open={open} portal onOpenChange={(next) => { if (next) { onOpen?.(); } setOpen(next); }} trigger={(props) => renderLayoutArrow(props, open, t("react.workflows.layout", "Layout"))}>
      <div className="workflow-layout-menu" role="menu" aria-label={t("react.workflows.layout_arrange", "Arrange")}>
        <p className="workflow-menu-heading">{t("react.workflows.layout_arrange", "Arrange")}</p>
        {options.map((option) => <button key={option.value} className="workflow-layout-option" role="menuitemradio" aria-checked={mode === option.value} type="button" onClick={() => { setOpen(false); onChoose(option.value); }}>
          <span><span className="workflow-layout-option-name">{option.label}</span><span className="workflow-layout-option-hint">{option.hint}</span></span>
          {mode === option.value ? <Check aria-hidden="true" size={16} /> : <span />}
        </button>)}
      </div>
    </DsPopover>
  </span>;
}
