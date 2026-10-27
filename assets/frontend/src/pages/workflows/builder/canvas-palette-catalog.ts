import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { localizedSTEP_KIND_OPTIONS, objectOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import { defaultOverrideForStepKind } from "@/pages/workflows/detail/detail-step-editing";
import type { ToolCategory } from "@/pages/workflows/detail/detail-tool-categories";
import { stepInsertableBetween } from "./change-set";
import type { LocalContainerChoice, LocalStepChoice, PaletteChoice } from "./canvas-types";

type Translate = I18nContextValue["t"];

/** The order Add step lists its families in; Canvas › Note closes the list. */
export const PALETTE_FAMILIES = ["Actions", "Data", "Flow", "Finish", "Groups", "Canvas"] as const;
export type PaletteFamily = (typeof PALETTE_FAMILIES)[number];

/** A family's heading, shared by Add step and the legend. */
export function paletteFamilyLabel(family: PaletteFamily, t: Translate = fallbackContext.t): string {
  switch (family) {
    case "Actions": return t("react.workflows.palette_actions", "Actions");
    case "Data": return t("react.workflows.palette_data", "Data");
    case "Flow": return t("react.workflows.palette_flow", "Flow");
    case "Finish": return t("react.workflows.palette_finish", "Finish");
    case "Canvas": return t("react.workflows.palette_canvas", "Canvas");
    default: return t("react.workflows.palette_groups", "Groups");
  }
}

export type PaletteEntry = {
  choice: PaletteChoice;
  family: PaletteFamily;
  /** One line under the name: what the kind does. */
  description: string;
  /** Set when the kind cannot go where Add step was asked for; the row stays, disabled. */
  disabledReason?: string;
  /** The kind whose icon tile the row shows. */
  iconKind: string;
  toolCategory?: ToolCategory;
};

export type PaletteScope = {
  /** Collect, Break and Continue belong directly in a sequence For each. */
  sequenceForEach: boolean;
  allowGroup: boolean;
  allowForEach: boolean;
  allowHookDeclaration?: boolean;
  /** Add step takes an occupied connection's place. */
  inserting: boolean;
  /** Add step was asked for after an outcome or on a connection: a note has no place there. */
  connected?: boolean;
};

const ACTION_ORDER = ["agent", "tool:connector", "tool:extension_tools", "tool:mcp_tools", "http_request", "invoke_workflow"];
const DATA_ORDER = ["set_variable", "transform", "tabular_feed", "dataset_query", "file_from_base64", "template", "collect"];
const FLOW_ORDER = ["if", "switch", "approval", "wait", "container:for_each", "container:inline_loop", "batch", "break", "continue"];
const FINISH_ORDER = ["stop_success", "stop_fail"];
const FOR_EACH_ONLY = new Set(["collect", "break", "continue"]);

function stepDescription(kind: string, t: Translate): string {
  switch (kind) {
    case "agent": return t("react.workflows.palette_desc_agent", "Ask an agent to read, decide or write.");
    case "http_request": return t("react.workflows.palette_desc_http_request", "Call an HTTP endpoint and keep the response.");
    case "invoke_workflow": return t("react.workflows.palette_desc_invoke_workflow", "Run another workflow and use its outputs.");
    case "set_variable": return t("react.workflows.palette_desc_set_variable", "Write values into the workflow state.");
    case "transform": return t("react.workflows.palette_desc_transform", "Map fields into a new shape.");
    case "tabular_feed": return t("react.workflows.palette_desc_tabular_feed", "Read or write a CSV or sheet feed.");
    case "dataset_query": return t("react.workflows.palette_desc_dataset_query", "Query a DataSet.");
    case "file_from_base64": return t("react.workflows.palette_desc_file_from_base64", "Turn base64 data into a file.");
    case "template": return t("react.workflows.palette_desc_template", "Render text from a template.");
    case "collect": return t("react.workflows.palette_desc_collect", "Keep a value from each item.");
    case "if": return t("react.workflows.palette_desc_if", "Branch on a condition: True or False.");
    case "switch": return t("react.workflows.palette_desc_switch", "Route by value to one of several cases.");
    case "approval": return t("react.workflows.palette_desc_approval", "Wait for a person to approve or reject.");
    case "wait": return t("react.workflows.palette_desc_wait", "Pause, then continue.");
    case "batch": return t("react.workflows.palette_desc_batch", "Split items into batches.");
    case "break": return t("react.workflows.palette_desc_break", "Stop the loop here.");
    case "continue": return t("react.workflows.palette_desc_continue", "Skip to the next item.");
    case "stop_success": return t("react.workflows.palette_desc_stop_success", "End the run and return the outputs.");
    case "stop_fail": return t("react.workflows.palette_desc_stop_fail", "End the run as failed, with a message.");
    default: return "";
  }
}

function toolLabel(category: ToolCategory, t: Translate): string {
  if (category === "extension_tools") return t("react.workflows.palette_extension_tools", "Extension tools");
  if (category === "mcp_tools") return t("react.workflows.palette_mcp_tools", "MCP tools");
  return t("react.workflows.palette_system_tools", "System tools");
}

function toolDescription(category: ToolCategory, t: Translate): string {
  if (category === "extension_tools") return t("react.workflows.palette_desc_extension_tools", "Run an action of an installed extension.");
  if (category === "mcp_tools") return t("react.workflows.palette_desc_mcp_tools", "Run a tool of a connected MCP server.");
  return t("react.workflows.palette_desc_system_tools", "Run a built-in tool on text, numbers or JSON.");
}

function familyOf(kind: string): PaletteFamily {
  if (ACTION_ORDER.includes(kind)) return "Actions";
  if (DATA_ORDER.includes(kind)) return "Data";
  if (FINISH_ORDER.includes(kind)) return "Finish";
  return "Flow";
}

function orderIndex(key: string): number {
  for (const list of [ACTION_ORDER, DATA_ORDER, FLOW_ORDER, FINISH_ORDER]) {
    const index = list.indexOf(key);
    if (index >= 0) return index;
  }
  return 99;
}

/**
 * Every kind Add step offers, in the published order, each with its one-line
 * description. Kinds that cannot go on a connection stay listed, disabled, and
 * say why, instead of disappearing.
 */
export function paletteEntries(scope: PaletteScope, t: Translate = fallbackContext.t): PaletteEntry[] {
  const onConnection = t("react.workflows.palette_refused_on_connection", "Only a step with one Next outcome can go on a connection.");
  const groupOnConnection = t("react.workflows.palette_refused_group_on_connection", "A group has its own outcomes, so it cannot go on a connection.");
  const entries: Array<[string, PaletteEntry]> = [];
  for (const option of localizedSTEP_KIND_OPTIONS(t)) {
    if (FOR_EACH_ONLY.has(option.value) && !scope.sequenceForEach) continue;
    const base: LocalStepChoice = {
      catalogKey: `builtin:${option.value}`, stepKind: option.value, displayName: option.label,
      family: familyOf(option.value === "connector" ? "agent" : option.value) as LocalStepChoice["family"],
      override: objectOrEmpty(JSON.parse(defaultOverrideForStepKind(option.value) || "{}")),
    };
    const refused = scope.inserting && !stepInsertableBetween({ step_id: "palette_candidate", step_kind: base.stepKind, override: base.override }) ? onConnection : undefined;
    if (option.value === "connector") {
      for (const category of ["connector", "extension_tools", "mcp_tools"] as const) {
        entries.push([`tool:${category}`, {
          family: "Actions", iconKind: "connector", toolCategory: category,
          description: toolDescription(category, t),
          choice: { ...base, catalogKey: `tool-category:${category}`, displayName: toolLabel(category, t), toolCategory: category, family: "Actions" },
          ...(refused ? { disabledReason: refused } : {}),
        }]);
      }
      continue;
    }
    entries.push([option.value, { family: familyOf(option.value), iconKind: option.value, description: stepDescription(option.value, t), choice: base, ...(refused ? { disabledReason: refused } : {}) }]);
  }
  if (scope.allowForEach) {
    // For each is a loop, not a kind of group: it sits in Flow, before Inline loop.
    entries.push(["container:for_each", { family: "Flow", iconKind: "for_each", description: t("react.workflows.palette_desc_for_each", "Run the steps inside once for each item."), choice: { nodeKind: "for_each", catalogKey: "container:for_each", displayName: t("react.workflows.for_each", "For each"), family: "Flow", containerDefaults: { containerKind: "for_each" } }, ...(scope.inserting ? { disabledReason: groupOnConnection } : {}) }]);
    const inline: LocalContainerChoice = { nodeKind: "for_each", catalogKey: "container:inline_loop", displayName: t("react.workflows.palette_inline_loop", "Inline loop"), family: "Flow", presentation: "inline", containerDefaults: { containerKind: "for_each" } };
    entries.push(["container:inline_loop", { family: "Flow", iconKind: "for_each", description: t("react.workflows.palette_desc_inline_loop", "Repeat a few simple steps for each item, inside its card."), choice: inline, ...(scope.inserting ? { disabledReason: groupOnConnection } : {}) }]);
  }
  const groups: PaletteEntry[] = [];
  if (scope.allowGroup) {
    // A plain Group first: steps that belong together, run one after another.
    groups.push(
      { family: "Groups", iconKind: "group", description: t("react.workflows.palette_desc_group", "Steps that belong together. Can become a child workflow."), choice: { nodeKind: "group", catalogKey: "container:group", displayName: t("react.workflows.group", "Group"), family: "Groups", containerDefaults: { containerKind: "group", bodyKind: "sequence" } }, ...(scope.inserting ? { disabledReason: groupOnConnection } : {}) },
      { family: "Groups", iconKind: "group", description: t("react.workflows.palette_desc_parallel_lanes", "A group whose lanes run side by side."), choice: { nodeKind: "group", catalogKey: "container:parallel_lanes", displayName: t("react.workflows.group_kind_parallel", "Parallel lanes"), family: "Groups", containerDefaults: { containerKind: "group", bodyKind: "lanes" } }, ...(scope.inserting ? { disabledReason: groupOnConnection } : {}) },
    );
  }
  if (scope.allowHookDeclaration && !scope.inserting) {
    const label = t("react.workflows.hook", "Hook");
    groups.push({ family: "Groups", iconKind: "invoke_workflow", description: t("react.workflows.palette_desc_hook", "A place where a project adds its own steps."), choice: { catalogKey: "hook:declaration", stepKind: "invoke_workflow", displayName: label, family: "Groups", override: { invoke_workflow: { hook: { contract_version: 1, display_name: label, input_schema: { type: "object", properties: {} }, input_mapping: {}, stage_contract: { summary: "" } } } } } });
  }
  const ordered = PALETTE_FAMILIES.flatMap((family) => entries
    .filter(([, entry]) => entry.family === family)
    .sort(([left], [right]) => orderIndex(left) - orderIndex(right))
    .map(([, entry]) => entry));
  const canvas: PaletteEntry[] = scope.connected ? [] : [{ family: "Canvas", iconKind: "note", description: t("react.workflows.palette_desc_note", "Text and images on the canvas. Saved separately from structure."), choice: { nodeKind: "note", catalogKey: "canvas:note", displayName: t("react.workflows.note", "Note"), family: "Canvas" } }];
  return [...ordered.filter((entry) => entry.family !== "Groups" && entry.family !== "Canvas"), ...groups, ...canvas];
}

/**
 * Search names, kinds and families first: "wait" finds Wait, not Approval's
 * "Wait for a person…". Descriptions are searched only when no name matches,
 * so "base64" still finds File from base64 and "note" finds Canvas › Note.
 */
export function filterPaletteEntries(entries: readonly PaletteEntry[], query: string, locale = "en", familyLabel: (family: PaletteFamily) => string = (family) => family): PaletteEntry[] {
  const needle = query.trim().toLocaleLowerCase(locale);
  if (!needle) return [...entries];
  const named = entries.filter((entry) => {
    const kind = entry.choice.nodeKind === "note" ? "note" : entry.choice.stepKind || entry.choice.nodeKind || "";
    return `${entry.choice.displayName} ${kind} ${familyLabel(entry.family)}`.toLocaleLowerCase(locale).includes(needle);
  });
  return named.length ? named : entries.filter((entry) => entry.description.toLocaleLowerCase(locale).includes(needle));
}
