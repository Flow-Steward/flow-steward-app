import { fireEvent, render, screen } from "@testing-library/react";
import { fallbackContext } from "@/lib/i18n/context";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import type { WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import { buildCanvasLayout } from "./layout";
import { canvasConnectionDefinition } from "./canvas-connection-policy";
import { definitionFromWorkflow } from "./change-set";
import { containerMenuLevel, groupSelectionLevel, paneMenuLevel, stepMenuLevel, stepMoreItems, type CanvasMenuHost } from "./canvas-context-menus";
import { CanvasMenuSurface, type CanvasMenuItem } from "./canvas-menu-surface";
import { hookMemberId } from "./hook-frames";

const graph: WorkflowCanvasGraph = {
  workflow: { id: "orders", display_name: "Orders", enabled: false, callable_by_parent: false },
  interface_contract: {}, output_contract: {},
  entry: { kind: "step", id: "fetch" },
  groups: [{ id: "notify", display_name: "Notify customer", body: { kind: "lanes", lanes: [{ lane_id: "email" }, { lane_id: "receipt" }] }, automation_summary: { rule_count: 2 } }],
  for_each: [],
  steps: [
    { id: "fetch", step_id: "fetch", kind: "http_request", display_name: "Fetch order", config_summary: "GET", ports: [{ outcome: { name: "success" }, label: "Next" }] },
    { id: "classify", step_id: "classify", kind: "agent", display_name: "Classify request", config_summary: "agent", ports: [{ outcome: { name: "success" }, label: "Next" }], automation_summary: { rule_count: 1 } },
    { id: "priority", step_id: "priority", kind: "if", display_name: "Priority order?", config_summary: "", ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }] },
  ],
  edges: [{ source: { kind: "step", id: "fetch" }, outcome: { name: "success" }, target: { kind: "step", id: "classify" } }],
  validation_issues: [], automation_rules: [],
};

function host(overrides: Partial<CanvasMenuHost> = {}) {
  const stageGraphIntent = vi.fn(() => ({ controller: Symbol("test"), serial: 1 }));
  const undoGraphIntent = vi.fn();
  const owner = { status: "pending", stageGraphIntent, undoGraphIntent, preview: canvasConnectionDefinition(graph) } as unknown as WorkflowChangeSetController;
  const { nodes } = buildCanvasLayout({ canvasGraph: graph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn(), onEditStep: vi.fn() });
  const value: CanvasMenuHost = {
    t: fallbackContext.t, workflowId: "orders", nodes, canvasGraph: graph, structureLocked: false, selectedIds: [],
    select: vi.fn(), owner: () => owner, clipboard: vi.fn(async () => {}), remove: vi.fn(), openNewStepAt: vi.fn(), addNoteAt: vi.fn(),
    selectAll: vi.fn(), fitView: vi.fn(), screenToFlow: (point) => ({ x: point.x * 2, y: point.y * 2 }), notice: vi.fn(), forgetPlacements: vi.fn(),
    ...overrides,
  };
  return { value, stageGraphIntent, undoGraphIntent };
}

const labels = (items: readonly CanvasMenuItem[] = []) => items.map((item) => item.kind === "separator" ? "—" : item.label);

describe("Canvas right-click menus", () => {
  it("lists a step's commands in the published order with their keys", () => {
    const level = stepMenuLevel(host().value, "fetch");
    expect(labels(level.items)).toEqual(["Open", "Copy", "Duplicate", "Disable", "Make it the workflow entry", "Move to…", "Group…", "—", "Delete"]);
    expect(level.items?.find((item) => item.kind !== "separator" && item.label === "Group…")).toMatchObject({ shortcut: "⌘⇧G" });
    expect(level.items?.find((item) => item.kind !== "separator" && item.label === "Make it the workflow entry")).toMatchObject({ disabledReason: "The run starts here already." });
  });

  it("keeps a step with automation rules: Delete says why and opens Automation", () => {
    const level = stepMenuLevel(host().value, "classify");
    const remove = level.items?.find((item) => item.kind !== "separator" && item.key === "delete");
    expect(remove).toMatchObject({ disabledReason: "Has 1 automation rule. Remove it in Automation first.", reasonAction: { label: "Open Automation" } });
  });

  it("disables a step through one undoable change set intent and offers Undo in the toast", () => {
    const { value, stageGraphIntent, undoGraphIntent } = host();
    const disable = stepMoreItems(value, "fetch").find((item) => item.kind !== "separator" && item.key === "disable");
    expect(labels(stepMoreItems(value, "fetch"))).toEqual(["Duplicate", "Disable", "Delete"]);
    if (!disable || disable.kind === "separator") throw new Error("no Disable");
    disable.onSelect?.();
    expect(stageGraphIntent).toHaveBeenCalledWith([{ op: "update_step_config", step_id: "fetch", patch: { disabled: true } }], "Fetch order disabled. The run skips it.");
    const notice = vi.mocked(value.notice).mock.calls[0][0];
    notice.action?.onAction();
    expect(undoGraphIntent).toHaveBeenCalledOnce();
  });

  it("refuses to disable a step without a single Next outcome", () => {
    const disable = stepMoreItems(host().value, "priority").find((item) => item.kind !== "separator" && item.key === "disable");
    expect(disable).toMatchObject({ disabledReason: "Only a step with one Next outcome can be disabled." });
  });

  it("offers a group's lanes, its entry and Delete with its reason", () => {
    const level = containerMenuLevel(host().value, "group:notify");
    expect(labels(level.items)).toEqual(["Edit group", "Add step to lane email", "Add step to lane receipt", "Make it the workflow entry", "—", "Delete group"]);
    expect(level.items?.at(-1)).toMatchObject({ disabledReason: "Has 2 automation rules. Remove them in Automation first." });
  });

  it("offers Convert to child workflow… on a group, refused with the reason while it cannot run", () => {
    const convertGroup = vi.fn();
    const level = containerMenuLevel(host({ convertGroup }).value, "group:notify");
    expect(labels(level.items)).toEqual(["Edit group", "Add step to lane email", "Add step to lane receipt", "Make it the workflow entry", "Convert to child workflow…", "—", "Delete group"]);
    const convert = level.items?.find((item) => item.kind !== "separator" && item.key === "convert");
    if (!convert || convert.kind === "separator") throw new Error("no Convert");
    convert.onSelect?.();
    expect(convertGroup).toHaveBeenCalledExactlyOnceWith("notify", "Notify customer");
    const blocked = containerMenuLevel(host({ convertGroup, convertGroupBlockedReason: "Apply or discard structure changes before converting a group." }).value, "group:notify");
    expect(blocked.items?.find((item) => item.kind !== "separator" && item.key === "convert")).toMatchObject({ disabledReason: "Apply or discard structure changes before converting a group." });
  });

  it("Group… offers a plain Group first, then Parallel lanes, and adds it as one undoable change", () => {
    const { value, stageGraphIntent, undoGraphIntent } = host();
    const level = groupSelectionLevel(value, ["fetch", "classify"]);
    expect(labels(level.items)).toEqual(["Group", "Parallel lanes"]);
    const group = level.items?.[0];
    if (!group || group.kind === "separator" || !group.submenu) throw new Error("no Group");
    const summary = group.submenu();
    expect(summary.heading).toBe("New group: One after another");
    render(<>{summary.content?.(vi.fn())}</>);
    expect(screen.getByText("Fetch order, Classify request")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add group" }));
    const [operations, label] = stageGraphIntent.mock.calls[0] as unknown as [Array<{ op: string; group?: { body: unknown }; edge?: unknown }>, string];
    expect(label).toBe("2 steps grouped.");
    expect(operations[0]).toMatchObject({ op: "create_group", group: { display_name: "Group", body: { kind: "sequence" } } });
    // The connection between the selected steps is kept.
    expect(operations).toContainEqual({ op: "connect", edge: { source: { kind: "step", id: "fetch" }, outcome: { name: "success" }, target: { kind: "step", id: "classify" } } });
    expect(value.select).toHaveBeenCalledWith([]);
    vi.mocked(value.notice).mock.calls.at(-1)?.[0].action?.onAction();
    expect(undoGraphIntent).toHaveBeenCalledOnce();
  });

  it("Group… takes a For each as a plain Group, and says why Parallel lanes cannot hold it", () => {
    const loop = definitionFromWorkflow({
      workflow_id: "orders",
      steps: [{ step_id: "fetch", step_kind: "agent", display_name: "Fetch order", for_each_id: "records" }],
      for_each: [{ for_each_id: "records", display_name: "Each record", over: "state.items", item_name: "record", body: { kind: "sequence", entry: { kind: "step", id: "fetch" } } }],
      settings: { execution_graph: { schema_version: 1, entry: { kind: "for_each", id: "records" }, edges: [{ source: { kind: "step", id: "fetch" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "records", lane_id: null } }] } },
    });
    const stageGraphIntent = vi.fn(() => ({ controller: Symbol("test"), serial: 1 }));
    const owner = { status: "pending", stageGraphIntent, undoGraphIntent: vi.fn(), preview: loop } as unknown as WorkflowChangeSetController;
    const { value } = host({ owner: () => owner });
    const level = groupSelectionLevel(value, ["for_each:records"]);
    expect(labels(level.items)).toEqual(["Group", "Parallel lanes"]);
    expect(level.items?.[0]).toMatchObject({ key: "sequence" });
    expect(level.items?.[1]).toMatchObject({ key: "lanes", disabledReason: "A For each stays in a group whose steps run one after another. Move it out first." });
    const first = level.items?.[0];
    if (!first || first.kind === "separator" || !first.submenu) throw new Error("no Group");
    render(<>{first.submenu().content?.(vi.fn())}</>);
    fireEvent.click(screen.getByRole("button", { name: "Add group" }));
    const [operations, label] = stageGraphIntent.mock.calls[0] as unknown as [Array<{ op: string }>, string];
    expect(label).toBe("1 steps grouped.");
    expect(operations.map((operation) => operation.op)).toEqual(["create_group", "move_for_each", "set_entry", "set_entry"]);
  });

  it("keeps Paste visible but disabled with a read-only reason on a protected canvas", () => {
    const paste = vi.fn();
    const { value } = host({ structureLocked: true, paste });
    const level = paneMenuLevel(value, { x: 10, y: 20 });
    expect(labels(level.items)).toEqual(["Paste here", "Select all", "Fit view"]);
    const item = level.items?.find((candidate) => candidate.kind !== "separator" && candidate.key === "paste");
    expect(item).toMatchObject({ disabledReason: "This workflow's structure is read-only. Paste into a workflow you can edit." });

    render(<CanvasMenuSurface label="Canvas actions" anchor={{ x: 10, y: 20 }} onClose={vi.fn()} level={level} />);
    const pasteItem = screen.getByRole("menuitem", { name: /Paste here/ });
    expect(pasteItem).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("This workflow's structure is read-only. Paste into a workflow you can edit.")).toBeVisible();
    fireEvent.click(pasteItem);
    expect(paste).not.toHaveBeenCalled();
  });

  it("adds a step or a note where empty canvas was right-clicked and says why Paste is unavailable", () => {
    const { value } = host();
    const level = paneMenuLevel(value, { x: 10, y: 20 });
    expect(labels(level.items)).toEqual(["Add step here", "Add note here", "Paste here", "—", "Select all", "Fit view"]);
    expect(level.items?.find((item) => item.kind !== "separator" && item.key === "paste")).toMatchObject({ disabledReason: "Copy a step or a group first." });
    const step = level.items?.find((item) => item.kind !== "separator" && item.key === "step");
    if (!step || step.kind === "separator") throw new Error("no Add step here");
    step.onSelect?.();
    expect(value.openNewStepAt).toHaveBeenCalledWith({}, { point: { x: 20, y: 40 }, clientPoint: { x: 10, y: 20 } });
  });
});

describe("Container menus on a protected author", () => {
  const childLanes = hookMemberId("before", "child_lanes");
  const childLoop = hookMemberId("before", "child_loop");
  const lockedGraph: WorkflowCanvasGraph = {
    ...graph,
    groups: [...graph.groups, { id: childLanes, display_name: "Client lanes", body: { kind: "lanes", lanes: [{ lane_id: "sms" }] } }],
    for_each: [{ id: childLoop, display_name: "Client items", over: "state.items", item_name: "item", body: { kind: "sequence" } }],
  };
  function lockedHost() {
    const { nodes } = buildCanvasLayout({ canvasGraph: lockedGraph, structureLocked: true, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
    return host({ nodes, canvasGraph: lockedGraph, structureLocked: true }).value;
  }

  it("keeps the author's own group read-only: Edit only, no Add step, entry or Delete", () => {
    expect(labels(containerMenuLevel(lockedHost(), "group:notify").items)).toEqual(["Edit group"]);
  });

  it("keeps a hook's child group and For each editable, Delete included, without author-only entry or presentation", () => {
    const value = lockedHost();
    const lanes = containerMenuLevel(value, `group:${childLanes}`);
    expect(labels(lanes.items)).toEqual(["Edit group", "Add step to lane sms", "—", "Delete group"]);
    const remove = lanes.items?.at(-1);
    if (!remove || remove.kind === "separator") throw new Error("no Delete");
    expect(remove.disabledReason).toBeUndefined();
    remove.onSelect?.();
    expect(value.remove).toHaveBeenCalledWith([`group:${childLanes}`]);
    expect(labels(containerMenuLevel(value, `for_each:${childLoop}`).items)).toEqual(["Edit loop", "Add step", "—", "Delete loop"]);
  });
});

describe("Canvas menu surface", () => {
  it("runs a command, shows a refused one with its reason and opens Group… in place", () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<CanvasMenuSurface label="Step actions" anchor={{ x: 4, y: 4 }} onClose={onClose} level={{ items: [
      { key: "open", label: "Open", shortcut: "Enter", onSelect },
      { key: "refused", label: "Delete", disabledReason: "Has 1 automation rule. Remove it in Automation first." },
      { key: "group", label: "Group…", submenu: () => ({ heading: "Group…", items: [{ key: "sequence", label: "Group" }] }) },
    ] }} />);
    expect(screen.getByRole("menuitem", { name: /Delete/ })).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete/ }));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("menuitem", { name: "Group…" }));
    expect(screen.getByRole("menuitem", { name: "Group" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.click(screen.getByRole("menuitem", { name: /Open/ }));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalled();
  });
});
