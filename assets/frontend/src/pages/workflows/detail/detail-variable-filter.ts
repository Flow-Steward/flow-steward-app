import type { WorkflowVariableSuggestionOption } from "@/pages/workflows/detail/detail-helpers";
import type { WorkflowForEach, WorkflowNodeRef } from "@/pages/workflows/shared/api";
import {
  groupBodyEntryStepId,
  stepForEachId,
  stepGroupId,
  stepId,
  stepLaneId,
  type WorkflowDefinitionModel,
} from "@/pages/workflows/builder/change-set";

// Item 11: the variable suggestions narrow to what is typed, as an autocomplete does.

export type RankedVariable = { group: string; option: WorkflowVariableSuggestionOption };

function withoutStatePrefix(value: string): string {
  return value.replace(/^state\./, "");
}

// Every option whose path, business label, or description contains the typed text anywhere,
// case-insensitively; the ones whose path starts with it -- with or without the
// "state." prefix -- first. Empty text: all of them.
//
// A State path field prefixes what is typed ("rrent_st" is stored as
// "state.rrent_st"), so a typed "state.<fragment>" also matches a state path whose
// name contains the fragment mid-word, and a description that contains it.
export function rankVariables(entries: readonly RankedVariable[], query: string): RankedVariable[] {
  const typed = query.trim().toLowerCase();
  const seen = new Set<string>();
  const unique = entries.filter((entry) => {
    if (seen.has(entry.option.value)) return false;
    seen.add(entry.option.value);
    return true;
  });
  if (!typed) return unique;
  const fragment = typed.startsWith("state.") ? withoutStatePrefix(typed) : "";
  const contains = (entry: RankedVariable) => {
    const value = entry.option.value.toLowerCase();
    const label = String(entry.option.label ?? "").toLowerCase();
    const description = String(entry.option.description ?? "").toLowerCase();
    if (value.includes(typed) || label.includes(typed) || description.includes(typed)) return true;
    if (!fragment) return false;
    return (value.startsWith("state.") && withoutStatePrefix(value).includes(fragment)) ||
      label.includes(fragment) || description.includes(fragment);
  };
  const matches = unique.filter(contains);
  const starts = (entry: RankedVariable) => {
    const value = entry.option.value.toLowerCase();
    const label = String(entry.option.label ?? "").toLowerCase();
    return value.startsWith(typed) || withoutStatePrefix(value).startsWith(typed) ||
      label.startsWith(typed) || Boolean(fragment && label.startsWith(fragment));
  };
  return [...matches.filter(starts), ...matches.filter((entry) => !starts(entry))];
}

// A template field ({{state.x}} inside text): what is typed after the "{{" the text
// ends in, or null when it does not end in an open one.
export function openTemplateQuery(text: string): string | null {
  const open = text.lastIndexOf("{{");
  if (open < 0) return null;
  const typed = text.slice(open + 2);
  return typed.includes("}}") ? null : typed.trim();
}

// The chosen path in place of that open "{{", closed; the text before it is kept.
export function insertTemplateVariable(text: string, value: string): string {
  const open = text.lastIndexOf("{{");
  const before = open < 0 ? text : text.slice(0, open);
  return `${before}{{ ${value} }}`;
}

/**
 * B7: where the variables offered to the open editor come from. The server answers
 * for a saved step; a step that exists only in the unapplied change set borrows the
 * position it will run at.
 *
 * - a saved step: what runs before it;
 * - a new or unapplied step placed after a saved step: what runs after that step,
 *   that step's own outputs included (walking back through other unapplied steps);
 * - a new step in a Group or For each: that container's context, at its first saved
 *   step, plus the For each's item names;
 * - otherwise every saved step's outputs, marked as a position not known yet.
 *
 * The global variables come with every answer: they never depend on the position.
 */
export type WorkflowVariablesContext = {
  /** The saved step the server answers for; "" when there is none to anchor to. */
  stepId: string;
  position: "before" | "after" | "unknown";
  /** Without an anchor step: the saved For each whose item names are offered anyway. */
  forEachId: string;
  /** Item names of a For each that exists only in the unapplied change set. */
  localLoop: Array<Record<string, unknown>>;
};

type VariablesContextStep = { local?: boolean; runtimeId: string } | null;

export const UNKNOWN_POSITION: WorkflowVariablesContext = { stepId: "", position: "unknown", forEachId: "", localLoop: [] };

function incomingSource(preview: WorkflowDefinitionModel, stepId: string): WorkflowNodeRef | null {
  const edge = preview.graph.edges.find(
    (item) => !item.rework && item.target.kind === "step" && item.target.id === stepId,
  );
  return edge ? edge.source : null;
}

/** Editor rows for the item and index names of a For each, as the server words them. */
export function forEachLoopVariables(item: WorkflowForEach | null | undefined): Array<Record<string, unknown>> {
  if (!item) return [];
  const label = item.display_name || item.for_each_id;
  const rows: Array<Record<string, unknown>> = [];
  if (item.item_name) {
    rows.push({
      name: item.item_name,
      type: "json",
      description: `The current item of For each ${label}`,
      description_key: "react.workflows.variable_description.for_each_item",
      description_params: { for_each: label },
      source: "for_each",
      for_each_id: item.for_each_id,
    });
  }
  if (item.index_name && item.index_name !== item.item_name) {
    rows.push({
      name: item.index_name,
      type: "number",
      description: `The position of the current item in For each ${label}, from 0`,
      description_key: "react.workflows.variable_description.for_each_index",
      description_params: { for_each: label },
      source: "for_each",
      for_each_id: item.for_each_id,
    });
  }
  return rows;
}

export function workflowVariablesContext({
  createContext,
  drawerMode,
  groupAnchorStepId = "",
  localForEachIds,
  localStepIds,
  preview,
  selectedStep,
}: {
  createContext?: { groupId?: string; forEachId?: string; laneId?: string; insert?: { source: WorkflowNodeRef } | null } | null;
  drawerMode: string;
  /** The group drawer: the container's first saved step, as before. */
  groupAnchorStepId?: string;
  localForEachIds: ReadonlySet<string>;
  localStepIds: ReadonlySet<string>;
  preview: WorkflowDefinitionModel;
  selectedStep?: VariablesContextStep;
}): WorkflowVariablesContext {
  if (drawerMode === "group") {
    return groupAnchorStepId ? { ...UNKNOWN_POSITION, stepId: groupAnchorStepId, position: "before" } : UNKNOWN_POSITION;
  }
  if (drawerMode !== "step") return UNKNOWN_POSITION;
  const editedId = selectedStep?.runtimeId || "";
  if (editedId && !selectedStep?.local) return { ...UNKNOWN_POSITION, stepId: editedId, position: "before" };

  // After the saved step it is (or will be) connected to.
  let source: WorkflowNodeRef | null = createContext?.insert?.source || (editedId ? incomingSource(preview, editedId) : null);
  const visited = new Set<string>([editedId]);
  while (source?.kind === "step" && localStepIds.has(source.id) && !visited.has(source.id)) {
    visited.add(source.id);
    source = incomingSource(preview, source.id);
  }
  if (source?.kind === "step" && source.id && !localStepIds.has(source.id)) {
    return { ...UNKNOWN_POSITION, stepId: source.id, position: "after" };
  }

  // Inside a Group or For each: the container's context at its first saved step.
  const edited = editedId ? preview.steps.find((step) => stepId(step) === editedId) : undefined;
  const forEachId = createContext ? createContext.forEachId || "" : stepForEachId(edited);
  const groupId = forEachId ? "" : createContext ? createContext.groupId || "" : stepGroupId(edited);
  const laneId = createContext ? createContext.laneId || "" : stepLaneId(edited);
  const forEach = forEachId ? preview.forEach.find((item) => item.for_each_id === forEachId) : undefined;
  const container = forEach || (groupId ? preview.groups.find((group) => group.group_id === groupId) : undefined);
  if (!container) return UNKNOWN_POSITION;
  const saved = (id: string) => Boolean(id) && !localStepIds.has(id) && id !== editedId;
  const members = preview.steps
    .filter((step) => (forEachId ? stepForEachId(step) === forEachId : stepGroupId(step) === groupId && !stepForEachId(step)))
    .map(stepId);
  const entry = groupBodyEntryStepId(container, laneId);
  const anchor = saved(entry) && members.includes(entry) ? entry : members.find(saved) || "";
  const localLoop = forEachId && localForEachIds.has(forEachId) ? forEachLoopVariables(forEach) : [];
  if (anchor) return { stepId: anchor, position: "before", forEachId: "", localLoop };
  return { ...UNKNOWN_POSITION, forEachId: forEachId && !localForEachIds.has(forEachId) ? forEachId : "", localLoop };
}
