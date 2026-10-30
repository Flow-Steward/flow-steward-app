/* eslint-disable react-refresh/only-export-components -- menu builders, their hook and the card context belong together. */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import type { Node } from "@xyflow/react";
import { Button } from "@/components/ui/button";
import { ignoreAsyncResult } from "@/lib/utils/async";
import type { I18nContextValue } from "@/lib/i18n/context";
import type { WorkflowCanvasGraph, WorkflowNodeRef } from "@/pages/workflows/shared/api";
import type { WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import { stepCanBeDisabled, type WorkflowDefinitionModel } from "./change-set";
import { canvasConnectionDefinition, connectionPlacement } from "./canvas-connection-policy";
import { isStepNode, summaryBadge, type GraphEditNotice } from "./canvas-core";
import { planSelectionGroup, selectionGroupRefusal, selectionLanesRefusal, type SelectionGroupKind } from "./canvas-group-selection";
import { inlineLoopEligible, inlineLoopIds, setInlineLoop } from "./canvas-inline-loop-layout";
import { CanvasMenuSurface, type CanvasMenuItem, type CanvasMenuLevel } from "./canvas-menu-surface";
import { parseHookNodeId } from "./hook-frames";
import type { ForEachNodeData, GroupNodeData, StepNodeData } from "./layout";
import type { CanvasPlacementPoint } from "./canvas-types";

type Translate = I18nContextValue["t"];
type Point = { x: number; y: number };

/** What the canvas lends its menus: selection, commands and the change set owner. */
export type CanvasMenuHost = {
  t: Translate;
  workflowId: string;
  nodes: readonly Node[];
  canvasGraph: WorkflowCanvasGraph;
  structureLocked: boolean;
  selectedIds: readonly string[];
  select: (ids: string[]) => void;
  /** The real author (or Hook child) change set that owns these nodes. */
  owner: (ids: readonly string[]) => WorkflowChangeSetController | null;
  clipboard?: (command: "copy" | "duplicate") => Promise<void>;
  /** Paste at a client point; absent until the canvas offers paste. */
  paste?: (clientPoint: Point) => void;
  canPaste?: boolean;
  remove: (ids: readonly string[]) => void;
  openNewStepAt: (context: { groupId?: string; forEachId?: string; laneId?: string }, at?: CanvasPlacementPoint) => void;
  addNoteAt: (point?: Point) => void;
  notesDisabled?: boolean;
  selectAll: () => void;
  fitView: () => void;
  screenToFlow: (point: Point) => Point;
  notice: (notice: GraphEditNotice) => void;
  /** Drop manual places of nodes that moved into another container (their coordinates were the old parent's). */
  forgetPlacements: (ids: readonly string[]) => void;
  /** Open Convert group to child workflow for a saved Group; absent where the author cannot convert. */
  convertGroup?: (groupId: string, name: string) => void;
  /** Why a Group cannot be converted right now (unapplied structure, unsaved settings). */
  convertGroupBlockedReason?: string;
};

const isContainerNode = (node: Node | undefined) => node?.type === "groupContainer" || node?.type === "forEachContainer" || node?.type === "inlineLoop";

function stepRefId(node: Node<StepNodeData>) { return node.data.step.step_id || node.data.step.id; }

function ruleCount(summary: unknown) { return summaryBadge(summary as Parameters<typeof summaryBadge>[0]).rules; }

function deleteRefusal(rules: number, t: Translate): string {
  if (!rules) return "";
  return rules === 1
    ? t("react.workflows.delete_refused_rule_one", "Has 1 automation rule. Remove it in Automation first.")
    : t("react.workflows.delete_refused_rules", "Has {count} automation rules. Remove them in Automation first.", { count: rules });
}

/** Stage one structural intent on the owner, with a toast that offers Undo. */
function stageWithUndo(host: CanvasMenuHost, ids: readonly string[], operations: Parameters<WorkflowChangeSetController["stageGraphIntent"]>[0], label: string): boolean {
  const owner = host.owner(ids);
  if (!owner || owner.status === "applying" || owner.status === "conflict") {
    host.notice({ label: host.t("react.workflows.change_unavailable_now", "Wait for the structure to update, then try again."), tone: "error" });
    return false;
  }
  if (!owner.stageGraphIntent(operations, label)) return false;
  host.notice({ label, tone: "success", action: { label: host.t("react.workflows.undo", "Undo"), onAction: () => owner.undoGraphIntent() } });
  return true;
}

/** Group…: a plain Group (one after another) or Parallel lanes, or the reason it cannot (groups don't nest; a For each may sit in a plain Group). */
export function groupSelectionLevel(host: CanvasMenuHost, ids: readonly string[]): CanvasMenuLevel {
  const { t } = host;
  const owner = host.owner(ids);
  const definition = owner?.preview as WorkflowDefinitionModel | undefined;
  const refusal = !definition ? t("react.workflows.change_unavailable_now", "Wait for the structure to update, then try again.") : selectionGroupRefusal(definition, ids, t);
  const heading = t("react.workflows.group_selection", "Group…");
  if (refusal) return { heading, items: [{ key: "refused", label: t("react.workflows.group_selection_unavailable_title", "Cannot group"), disabledReason: refusal }] };
  const choose = (kind: SelectionGroupKind): CanvasMenuLevel => groupSummaryLevel(host, ids, kind);
  const lanesRefusal = selectionLanesRefusal(ids, t);
  return {
    heading,
    items: [
      { key: "sequence", label: t("react.workflows.group", "Group"), submenu: () => choose("sequence") },
      { key: "lanes", label: t("react.workflows.group_kind_parallel", "Parallel lanes"), ...(lanesRefusal ? { disabledReason: lanesRefusal } : { submenu: () => choose("lanes") }) },
    ],
  };
}

/** The New group summary: what moves in and each connection, before it is added. */
function groupSummaryLevel(host: CanvasMenuHost, ids: readonly string[], kind: SelectionGroupKind): CanvasMenuLevel {
  const { t } = host;
  const owner = host.owner(ids);
  const definition = owner?.preview as WorkflowDefinitionModel | undefined;
  const plan = definition ? planSelectionGroup(definition, ids, kind, t) : { ok: false as const, reason: t("react.workflows.change_unavailable_now", "Wait for the structure to update, then try again.") };
  const kindLabel = kind === "lanes" ? t("react.workflows.group_kind_parallel", "Parallel lanes") : t("react.workflows.group_body_one_after_another", "One after another");
  return {
    heading: t("react.workflows.new_group_heading", "New group: {kind}", { kind: kindLabel }),
    content: (close) => {
      if (!plan.ok) return <p className="workflow-menu-summary" role="alert">{plan.reason}</p>;
      const { summary } = plan;
      return <>
        <dl className="workflow-menu-summary">
          <dt>{t("react.workflows.group_summary_moves_in", "Moves in")}</dt><dd>{summary.movesIn.join(", ")}</dd>
          {summary.lanes.length > 1 ? summary.lanes.map((lane) => <dd key={lane.laneId}>{t("react.workflows.group_summary_lane", "Parallel lane {lane}: {steps}", { lane: lane.laneId, steps: lane.steps.join(" → ") })}</dd>) : null}
          {summary.leadsIn.map((label) => <dd key={`in:${label}`}><span className="text-[var(--fs-text-tertiary)]">{t("react.workflows.group_summary_leads_in", "Leads into the group")}</span> {label}</dd>)}
          {summary.thenGoesTo ? <dd><span className="text-[var(--fs-text-tertiary)]">{t("react.workflows.group_summary_then", "Then the group goes to")}</span> {summary.thenGoesTo}</dd> : null}
          {summary.ends.map((end) => <dd key={`end:${end.label}`} className={end.formerTarget ? "is-attention" : undefined}><span className="text-[var(--fs-text-tertiary)]">{t("react.workflows.group_summary_ends", "Ends the group")}</span> {end.label}{end.formerTarget ? ` ${t("react.workflows.group_summary_no_longer", "(no longer leads to {target})", { target: end.formerTarget })}` : ""}</dd>)}
          {summary.dropped.map((label) => <dd key={`drop:${label}`} className="is-attention">{t("react.workflows.group_summary_dropped", "Removed: {connection}", { connection: label })}</dd>)}
        </dl>
        <div className="workflow-menu-actions">
          <Button size="sm" variant="secondary" type="button" onClick={close}>{t("registry.common.cancel", "Cancel")}</Button>
          <Button size="sm" variant="primary" type="button" onClick={() => {
            close();
            const label = kind === "lanes"
              ? t("react.workflows.group_selection_done", "{count} steps grouped into {kind}.", { count: summary.movesIn.length, kind: kindLabel })
              : t("react.workflows.group_selection_done_sequence", "{count} steps grouped.", { count: summary.movesIn.length });
            if (stageWithUndo(host, ids, plan.operations, label)) { host.forgetPlacements(ids); host.select([]); }
          }}>{t("react.workflows.group_selection_add", "Add group")}</Button>
        </div>
      </>;
    },
  };
}

function entryItem(host: CanvasMenuHost, node: Node, ref: WorkflowNodeRef, isEntry: boolean): CanvasMenuItem {
  const { t } = host;
  const definition = canvasConnectionDefinition(host.canvasGraph);
  const placement = connectionPlacement(definition, ref);
  const root = !placement || (!placement.groupId && !placement.forEachId);
  const label = root ? t("react.workflows.make_workflow_entry", "Make it the workflow entry") : t("react.workflows.make_entry_here", "Make it the entry here");
  return {
    key: "entry", label,
    ...(isEntry ? { disabledReason: root ? t("react.workflows.already_workflow_entry", "The run starts here already.") : t("react.workflows.already_body_entry", "This place starts here already.") } : {}),
    onSelect: () => {
      stageWithUndo(host, [node.id], [{
        op: "set_entry", target: ref,
        ...(placement?.groupId ? { group_id: placement.groupId } : {}),
        ...(placement?.forEachId ? { for_each_id: placement.forEachId } : {}),
        ...(placement?.laneId ? { lane_id: placement.laneId } : {}),
      }], t("react.workflows.entry_changed", "{name} is the entry now.", { name: (node.data as { step?: { display_name?: string } }).step?.display_name || ref.id }));
    },
  };
}

/** A single step goes through its card's own delete command (the step's More); a selection as one change. */
function removeStep(host: CanvasMenuHost, node: Node<StepNodeData>, targets: readonly string[]) {
  if (targets.length === 1 && targets[0] === node.id && node.data.onDeleteStep) node.data.onDeleteStep(node.data.step.step_id || node.data.step.id);
  else host.remove(targets);
}

/** Duplicate, Disable / Enable and Delete: the step toolbar's More. */
export function stepMoreItems(host: CanvasMenuHost, nodeId: string): CanvasMenuItem[] {
  const { t } = host;
  const node = host.nodes.find((candidate) => candidate.id === nodeId);
  if (!node || !isStepNode(node)) return [];
  const data = node.data;
  const hook = Boolean(parseHookNodeId(nodeId));
  // A protected author stays read-only, but the steps of its hooks' workflows remain editable.
  const editable = !data.readOnly && (!host.structureLocked || Boolean(parseHookNodeId(nodeId)?.memberStepId));
  const items: CanvasMenuItem[] = [];
  const targets = host.selectedIds.includes(nodeId) ? [...host.selectedIds] : [nodeId];
  if (host.clipboard && editable) items.push({ key: "duplicate", label: t("react.workflows.duplicate", "Duplicate"), shortcut: "⌘D", onSelect: () => runOnSelection(host, targets, () => host.clipboard?.("duplicate")) });
  if (editable && !hook) items.push(disableItem(host, node));
  if (editable) {
    const rules = ruleCount(data.step.automation_summary) || data.automationRules.length;
    items.push({ key: "delete", label: t("react.workflows.delete", "Delete"), danger: true,
      ...(rules ? { disabledReason: deleteRefusal(rules, t), reasonAction: { label: t("react.workflows.open_automation", "Open Automation"), onSelect: () => data.onAutomation(stepRefId(node)) } } : {}),
      onSelect: () => removeStep(host, node, targets) });
  }
  return items;
}

function disableItem(host: CanvasMenuHost, node: Node<StepNodeData>): CanvasMenuItem {
  const { t } = host;
  const step = node.data.step;
  const disabled = step.disabled === true;
  const eligible = stepCanBeDisabled({ step_id: stepRefId(node), step_kind: step.kind, override: {} }) || disabled;
  return {
    key: "disable",
    label: disabled ? t("react.workflows.enable_step", "Enable") : t("react.workflows.disable_step", "Disable"),
    ...(eligible ? {} : { disabledReason: t("react.workflows.disable_refused", "Only a step with one Next outcome can be disabled.") }),
    onSelect: () => stageWithUndo(host, [node.id], [{ op: "update_step_config", step_id: stepRefId(node), patch: { disabled: !disabled } }], disabled
      ? t("react.workflows.step_enabled", "{name} enabled.", { name: step.display_name })
      : t("react.workflows.step_disabled", "{name} disabled. The run skips it.", { name: step.display_name })),
  };
}

/** Select the nodes first, then run a command that reads the selection (Copy, Duplicate). */
function runOnSelection(host: CanvasMenuHost, ids: readonly string[], command: () => unknown) {
  const same = ids.length === host.selectedIds.length && ids.every((id) => host.selectedIds.includes(id));
  if (same) { void command(); return; }
  host.select([...ids]);
  // The clipboard snapshot follows the selection in a layout effect; run after it commits.
  globalThis.requestAnimationFrame(() => { void command(); });
}

/** Right-click a step. */
export function stepMenuLevel(host: CanvasMenuHost, nodeId: string): CanvasMenuLevel {
  const { t } = host;
  const node = host.nodes.find((candidate) => candidate.id === nodeId);
  if (!node || !isStepNode(node)) return { items: [] };
  const data = node.data;
  const id = stepRefId(node);
  const hook = Boolean(parseHookNodeId(nodeId));
  // A protected author stays read-only, but the steps of its hooks' workflows remain editable.
  const editable = !data.readOnly && (!host.structureLocked || Boolean(parseHookNodeId(nodeId)?.memberStepId));
  const targets = host.selectedIds.includes(nodeId) ? [...host.selectedIds] : [nodeId];
  const items: CanvasMenuItem[] = [
    // Open is the step itself, even for Stop · success, whose card opens Workflow interface › Outputs.
    { key: "open", label: t("react.workflows.open", "Open"), shortcut: "Enter", onSelect: () => data.onEditStep(id) },
  ];
  if (data.onOpenStepTestRun) items.push({ key: "test", label: t("react.workflows.test_step", "Test step"), onSelect: () => { ignoreAsyncResult(data.onOpenStepTestRun?.(id)); } });
  if (host.clipboard) items.push({ key: "copy", label: t("react.workflows.copy", "Copy"), shortcut: "⌘C", onSelect: () => runOnSelection(host, targets, () => host.clipboard?.("copy")) });
  if (host.clipboard && editable) items.push({ key: "duplicate", label: t("react.workflows.duplicate", "Duplicate"), shortcut: "⌘D", onSelect: () => runOnSelection(host, targets, () => host.clipboard?.("duplicate")) });
  if (editable && !hook) {
    items.push(
      disableItem(host, node),
      entryItem(host, node, { kind: "step", id }, data.isEntry),
      { key: "move", label: t("react.workflows.move_to", "Move to…"), onSelect: () => (data.onPlaceStep ?? data.onEditStep)(id) },
      { key: "group", label: t("react.workflows.group_selection", "Group…"), shortcut: "⌘⇧G", submenu: () => groupSelectionLevel(host, targets) },
    );
  }
  if (editable) {
    const rules = ruleCount(data.step.automation_summary) || data.automationRules.length;
    items.push({ kind: "separator", key: "sep" }, { key: "delete", label: t("react.workflows.delete", "Delete"), danger: true,
      ...(rules ? { disabledReason: deleteRefusal(rules, t), reasonAction: { label: t("react.workflows.open_automation", "Open Automation"), onSelect: () => data.onAutomation(id) } } : {}),
      onSelect: () => removeStep(host, node, targets) });
  }
  return { items };
}

/** Right-click a group, a For each or an inline loop. */
export function containerMenuLevel(host: CanvasMenuHost, nodeId: string): CanvasMenuLevel {
  const { t } = host;
  const node = host.nodes.find((candidate) => candidate.id === nodeId);
  if (!node || !isContainerNode(node)) return { items: [] };
  const forEach = node.type !== "groupContainer";
  const groupData = node.data as GroupNodeData;
  const forEachData = node.data as ForEachNodeData;
  const containerId = forEach ? forEachData.forEach.id : groupData.group.id;
  const name = forEach ? forEachData.forEach.display_name : groupData.group.display_name;
  // A protected author stays read-only, but a hook's own child groups and For each stay the project's to edit.
  const hookChild = Boolean(parseHookNodeId(nodeId)?.memberStepId);
  const readOnly = Boolean((node.data as { readOnly?: boolean }).readOnly) || (host.structureLocked && !hookChild);
  const hook = Boolean(!forEach && groupData.group.hook);
  const items: CanvasMenuItem[] = [{
    key: "edit", label: forEach ? t("react.workflows.edit_loop", "Edit loop") : t("react.workflows.edit_group", "Edit group"),
    onSelect: () => (forEach ? forEachData.onOpenForEach(containerId) : groupData.onOpenGroup(containerId)),
  }];
  if (!readOnly && !hook && node.type !== "inlineLoop") {
    const lanes = (node.data as { lanes: string[] }).lanes;
    if (lanes.length) lanes.forEach((laneId) => items.push({ key: `lane:${laneId}`, label: t("react.workflows.add_step_to_lane", "Add step to lane {lane}", { lane: laneId }), onSelect: () => host.openNewStepAt(forEach ? { forEachId: containerId, laneId } : { groupId: containerId, laneId }) }));
    else items.push({ key: "add", label: t("react.workflows.add_step_title", "Add step"), onSelect: () => host.openNewStepAt(forEach ? { forEachId: containerId } : { groupId: containerId }) });
  }
  if (!readOnly && !hook) {
    // Entry and the inline presentation address the author definition by its own ids; a hook child keeps them in its hook.
    if (!hookChild) items.push(entryItem(host, node, { kind: forEach ? "for_each" : "group", id: containerId }, Boolean((node.data as { isEntry?: boolean }).isEntry)));
    if (forEach && !hookChild) {
      const inline = inlineLoopIds(host.workflowId).has(containerId);
      const eligible = inlineLoopEligible(forEachData.forEach, host.canvasGraph.steps, host.canvasGraph.for_each || []);
      items.push({
        key: "inline", label: inline ? t("react.workflows.show_as_group", "Show as a group") : t("react.workflows.show_as_inline_loop", "Show as an inline loop"),
        ...(!inline && !eligible ? { disabledReason: t("react.workflows.inline_loop_refused", "An inline loop lists a few simple steps in sequence.") } : {}),
        onSelect: () => setInlineLoop(host.workflowId, containerId, !inline),
      });
    }
    if (!forEach && !hookChild && host.convertGroup) {
      const convert = host.convertGroup;
      items.push({
        key: "convert", label: t("react.workflows.convert_group_to_child_menu", "Convert to child workflow…"),
        ...(host.convertGroupBlockedReason ? { disabledReason: host.convertGroupBlockedReason } : {}),
        onSelect: () => convert(containerId, name || containerId),
      });
    }
    const rules = ruleCount(forEach ? forEachData.forEach.automation_summary : groupData.group.automation_summary) || (node.data as { automationRules?: unknown[] }).automationRules?.length || 0;
    items.push({ kind: "separator", key: "sep" }, {
      key: "delete", label: forEach ? t("react.workflows.delete_loop", "Delete loop") : t("react.workflows.delete_group", "Delete group"), danger: true,
      ...(rules ? { disabledReason: deleteRefusal(rules, t), ...(!forEach ? { reasonAction: { label: t("react.workflows.open_automation", "Open Automation"), onSelect: () => groupData.onAutomation(containerId) } } : {}) } : {}),
      onSelect: () => host.remove([nodeId]),
    });
  }
  return { heading: name, items };
}

/** Right-click empty canvas: a step or a note here, Paste here, Select all, Fit view. */
export function paneMenuLevel(host: CanvasMenuHost, clientPoint: Point): CanvasMenuLevel {
  const { t } = host;
  const point = host.screenToFlow(clientPoint);
  const items: CanvasMenuItem[] = [];
  if (!host.structureLocked) {
    items.push(
      { key: "step", label: t("react.workflows.add_step_here", "Add step here"), shortcut: "A", onSelect: () => host.openNewStepAt({}, { point, clientPoint }) },
      { key: "note", label: t("react.workflows.add_note_here", "Add note here"), shortcut: "N", ...(host.notesDisabled ? { disabledReason: t("react.workflows.notes_busy", "Notes are saving. Try again in a moment.") } : {}), onSelect: () => host.addNoteAt(point) },
    );
  }
  items.push({
    key: "paste",
    label: t("react.workflows.paste_here", "Paste here"),
    shortcut: "⌘V",
    ...(host.structureLocked
      ? { disabledReason: t("react.workflows.paste_read_only_reason", "This workflow's structure is read-only. Paste into a workflow you can edit.") }
      : host.paste && host.canPaste
        ? {}
        : { disabledReason: t("react.workflows.paste_nothing", "Copy a step or a group first.") }),
    onSelect: () => host.paste?.(clientPoint),
  });
  if (!host.structureLocked) items.push({ kind: "separator", key: "sep" });
  items.push(
    { key: "all", label: t("react.workflows.select_all", "Select all"), shortcut: "⌘A", onSelect: host.selectAll },
    { key: "fit", label: t("react.workflows.fit_view", "Fit view"), shortcut: "Shift+1", onSelect: host.fitView },
  );
  return { items };
}

type OpenMenu = { key: number; label: string; anchor: Point | HTMLElement; level: CanvasMenuLevel };

/** The canvas's one open command menu, and what step cards read through context (their More). */
export function useCanvasMenus(host: CanvasMenuHost) {
  const [menu, setMenu] = useState<OpenMenu | null>(null);
  const close = useCallback(() => setMenu(null), []);
  const open = useCallback((label: string, anchor: Point | HTMLElement, level: CanvasMenuLevel) => setMenu((current) => ({ key: (current?.key ?? 0) + 1, label, anchor, level })), []);
  const openNodeMenu = (nodeId: string, anchor: Point | HTMLElement) => {
    const node = host.nodes.find((candidate) => candidate.id === nodeId);
    if (!node) return;
    if (!host.selectedIds.includes(nodeId) && (isStepNode(node) || isContainerNode(node))) host.select([nodeId]);
    if (isStepNode(node)) open(host.t("react.workflows.step_actions", "Step actions"), anchor, stepMenuLevel(host, nodeId));
    else if (isContainerNode(node)) open(host.t("react.workflows.group_actions", "Group actions"), anchor, containerMenuLevel(host, nodeId));
  };
  const openPaneMenu = (clientPoint: Point) => open(host.t("react.workflows.canvas_actions", "Canvas actions"), clientPoint, paneMenuLevel(host, clientPoint));
  const openGroupMenu = (ids: readonly string[], anchor: Point | HTMLElement) => open(host.t("react.workflows.group_selection", "Group…"), anchor, groupSelectionLevel(host, ids));
  const element: ReactNode = menu ? <CanvasMenuSurface key={menu.key} label={menu.label} anchor={menu.anchor} level={menu.level} onClose={close} /> : null;
  return { openNodeMenu, openPaneMenu, openGroupMenu, close, element, open };
}

/** What a card's More reads: a step's commands, or a Group's / For each's (the same as its right-click menu). */
export type CanvasCardCommands = { moreItems: (nodeId: string) => CanvasMenuItem[]; containerItems: (nodeId: string) => CanvasMenuItem[] };
const CanvasCardCommandsContext = createContext<CanvasCardCommands | null>(null);

export function CanvasCardCommandsProvider({ host, children }: Readonly<{ host: CanvasMenuHost; children: ReactNode }>) {
  const value = useMemo<CanvasCardCommands>(() => ({
    moreItems: (nodeId) => stepMoreItems(host, nodeId),
    containerItems: (nodeId) => containerMenuLevel(host, nodeId).items ?? [],
  }), [host]);
  return <CanvasCardCommandsContext.Provider value={value}>{children}</CanvasCardCommandsContext.Provider>;
}

export function useCanvasCardCommands(): CanvasCardCommands | null {
  return useContext(CanvasCardCommandsContext);
}

/** What the canvas hands its shell for menus, layout and placement (kept as one prop). */
export type CanvasShellExtras = {
  arrangeMode: "auto" | "manual";
  onArrangeChange: (mode: "auto" | "manual") => void;
  addNoteAt: (point?: Point) => void;
  forgetPlacements: (ids: readonly string[]) => void;
  openNewStepAt: CanvasMenuHost["openNewStepAt"];
  convertGroup?: NonNullable<CanvasMenuHost["convertGroup"]>;
  convertGroupBlockedReason?: string;
};
