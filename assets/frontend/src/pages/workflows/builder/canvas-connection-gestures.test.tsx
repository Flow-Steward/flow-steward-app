import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { ComponentProps, MouseEvent as ReactMouseEvent } from "react";
import type { WorkflowCanvasShell } from "./canvas-shell";
import type { WorkflowCanvasGraph } from "../shared/api";
import type * as ReactFlowModule from "@xyflow/react";
import type { BuilderSurfaceProps } from "./canvas-types";
import { WorkflowCanvasBuilderSurface } from "./canvas";
import type * as CanvasNotesModule from "./canvas-notes";
const captured = vi.hoisted(() => ({ current: null as ComponentProps<typeof WorkflowCanvasShell> | null, saveNote: vi.fn(async () => undefined) }));
vi.mock("./canvas-shell", () => ({ WorkflowCanvasShell: (props: ComponentProps<typeof WorkflowCanvasShell>) => { captured.current = props; return <><div className="react-flow__pane" data-testid="pane"><div className="react-flow__node" data-testid="card-hit" /><button type="button">Toolbar overlay</button><div role="dialog" aria-label="Floating overlay" /></div><button type="button">Captured drag source</button></>; } }));
vi.mock("@xyflow/react", async (importOriginal) => {
  const actual = await importOriginal<typeof ReactFlowModule>();
  return { ...actual, ReactFlow: (props: { onPaneClick?: (event: ReactMouseEvent<HTMLDivElement>) => void }) => <div aria-label="Actual pane" role="button" tabIndex={0} className="react-flow__pane" onKeyDown={() => undefined} onClick={props.onPaneClick} /> };
});
vi.mock("./canvas-notes", async (importOriginal) => ({ ...await importOriginal<typeof CanvasNotesModule>(), useCanvasNotes: () => ({ notes: [], busy: false, disabled: false, error: "", selectedId: null, draft: null, add: vi.fn(), nodeActions: {}, removeFromSelection: (ids: readonly string[]) => [...ids], onNodesChange: vi.fn(), save: captured.saveNote, reload: vi.fn() }) }));
const graph: WorkflowCanvasGraph = {
  workflow: { id: "ports", display_name: "Ports", enabled: false, callable_by_parent: false },
  interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true },
  output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
  entry: { kind: "for_each", id: "walk" }, groups: [],
  for_each: [{ id: "walk", display_name: "Walk", over: "state.items", item_name: "item", body: { kind: "lanes", lanes: [{ lane_id: "a" }, { lane_id: "b" }] } }],
  steps: [
    { id: "a", step_id: "a", for_each_id: "walk", lane_id: "a", kind: "if", display_name: "Check", node_type: "ifStep", ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }], config_summary: "" },
    { id: "b", step_id: "b", for_each_id: "walk", lane_id: "b", kind: "set_variable", display_name: "Other lane", node_type: "action", ports: [{ outcome: { name: "success" }, label: "Next" }], config_summary: "" },
    { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", node_type: "end", ports: [], config_summary: "" },
  ], edges: [], automation_rules: [], validation_issues: [],
};
function mount(connect = vi.fn(async () => true), canvasGraph: WorkflowCanvasGraph = graph) {
  const append = vi.fn(); const disconnect = vi.fn(); const insert = vi.fn(); const closeInspector = vi.fn();
  const props = { canvasGraph, advancedMode: true, workflowId: "ports", changeSet: { operations: [], status: "clean", hasChanges: false, applyIssues: [], blockingIssueMessages: [], keptIssueMessages: [], checkState: "idle", applyRefused: false, applyError: "", apply: vi.fn(async () => undefined), reload: vi.fn(async () => undefined), discard: vi.fn() }, onAppendStep: append, onConnectPort: connect, onDisconnectPort: disconnect, onInsertStep: insert, onCloseInspector: closeInspector, onToggleAdvancedMode: vi.fn(), onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenNewGroup: vi.fn(), onOpenNewStep: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn(), onOpenWorkflowAutomation: vi.fn(), onOpenWorkflowSettings: vi.fn(), onValidationIssue: vi.fn() } as unknown as BuilderSurfaceProps;
  render(<WorkflowCanvasBuilderSurface {...props} />); return { append, connect, disconnect, insert, closeInspector };
}
const view = () => captured.current!;
describe("real canvas connection controller", () => {
  it("keeps the current refusal reason visible while a drag is still active", () => {
    vi.useFakeTimers();
    try {
      const actions = mount();
      act(() => view().handleConnectStart(new MouseEvent("mousedown", { clientX: 10, clientY: 20 }), { nodeId: "for_each:walk", handleId: "success", handleType: "source" }));
      act(() => { view().handleConnectionTargetChange("b"); });
      act(() => { vi.advanceTimersByTime(3000); });
      expect(view().graphEditNotice?.label).toContain("A connection stays inside");
      expect(actions.connect).not.toHaveBeenCalled();
    } finally { vi.useRealTimers(); }
  });
  it("distinguishes eligible targets from the single pointed target without coloring other refused nodes", () => {
    const actions = mount();
    act(() => view().handleConnectStart(new MouseEvent("mousedown", { clientX: 10, clientY: 20 }), { nodeId: "for_each:walk", handleId: "success", handleType: "source" }));
    const state = (id: string) => view().nodesWithCanvasState.find((node) => node.id === id)?.data.connectionTargetState;
    expect(state("done")).toBe("eligible"); expect(state("b")).toBeUndefined();
    act(() => { view().handleConnectionTargetChange("done"); });
    expect(state("done")).toBe("active-valid"); expect(state("b")).toBeUndefined();
    act(() => { view().handleConnectionTargetChange("b"); });
    expect(state("done")).toBe("eligible"); expect(state("b")).toBe("active-invalid");
    expect(view().graphEditNotice?.label).toContain("A connection stays inside");
    act(() => { view().handleConnectionTargetChange(null); });
    expect(state("done")).toBe("eligible"); expect(state("b")).toBeUndefined();
    expect(actions.connect).not.toHaveBeenCalled();
  });
  it("retains independent For each refs and the source lane on container exits", async () => {
    const actions = mount();
    await act(async () => { view().handleConnect({ source: "for_each:walk", sourceHandle: "success", target: "done", targetHandle: "in" }); });
    expect(actions.connect).toHaveBeenLastCalledWith({ kind: "for_each", id: "walk" }, { name: "success" }, { kind: "step", id: "done" }, false);
    await act(async () => { view().handleConnect({ source: "a", sourceHandle: "true", target: "for_each:walk", targetHandle: "exit" }); });
    expect(actions.connect).toHaveBeenLastCalledWith({ kind: "step", id: "a" }, { name: "true" }, { kind: "for_each_exit", for_each_id: "walk", lane_id: "a" }, false);
  });
  it("refuses cross-lane, external exit and Start mutations without calling the staging owner", async () => {
    const actions = mount(); const before = JSON.stringify(graph);
    for (const connection of [
      { source: "a", sourceHandle: "true", target: "b", targetHandle: "in" },
      { source: "done", sourceHandle: "error", target: "for_each:walk", targetHandle: "exit" },
      { source: "visual:start", sourceHandle: "entry", target: "done", targetHandle: "in" },
    ]) await act(async () => { view().handleConnect(connection); });
    act(() => view().handleEdgesChange([{ type: "remove", id: "visual:entry" }]));
    expect(actions.connect).not.toHaveBeenCalled(); expect(actions.disconnect).not.toHaveBeenCalled(); expect(actions.insert).not.toHaveBeenCalled();
    expect(JSON.stringify(graph)).toBe(before);
  });
  it("refuses a free body Next aimed at another For each exit before the author can stage it", async () => {
    const foreign: WorkflowCanvasGraph = {
      ...graph,
      for_each: [...graph.for_each!, { id: "foreign", display_name: "Other items", over: "state.others", item_name: "other", body: { kind: "sequence" } }],
    };
    const before = JSON.stringify(foreign);
    const actions = mount(vi.fn(async () => true), foreign);
    await act(async () => { view().handleConnect({ source: "b", sourceHandle: "success", target: "for_each:foreign", targetHandle: "exit" }); });
    expect(actions.connect).not.toHaveBeenCalled();
    expect(actions.append).not.toHaveBeenCalled();
    expect(actions.disconnect).not.toHaveBeenCalled();
    expect(view().graphEditNotice).toEqual({ label: "Only a step inside this group can finish at its exit.", tone: "error" });
    expect(JSON.stringify(foreign)).toBe(before);
  });
  it("publishes complete empty-drop creation intent through the current working callback and cancel writes nothing", () => {
    const actions = mount();
    const start = () => act(() => view().handleConnectStart(new MouseEvent("mousedown", { clientX: 10, clientY: 20 }), { nodeId: "a", handleId: "false", handleType: "source" }));
    start();
    act(() => view().handleConnectEnd(new MouseEvent("mouseup", { clientX: 10, clientY: 20 }), { toNode: null } as Parameters<NonNullable<typeof captured.current>["handleConnectEnd"]>[1]));
    expect(actions.append).not.toHaveBeenCalled();
    start(); const end = new MouseEvent("mouseup", { clientX: 300, clientY: 400 }); Object.defineProperty(end, "target", { value: screen.getByTestId("pane") });
    act(() => view().handleConnectEnd(end, { toNode: null, to: { x: 300, y: 400 } } as Parameters<NonNullable<typeof captured.current>["handleConnectEnd"]>[1]));
    expect(actions.append).toHaveBeenCalledWith({ kind: "step", id: "a" }, { name: "false" }, { source: { kind: "step", id: "a" }, outcome: { name: "false" }, scope: { groupId: "", forEachId: "walk", laneId: "a" }, point: { x: 300, y: 400 }, clientPoint: { x: 300, y: 400 }, selectionGeneration: 0 });
    expect(actions.connect).not.toHaveBeenCalled();
  });
  it("inserts on an occupied empty drop with exact old edge and complete creation intent, without appending or staging", () => {
    const edge = { source: { kind: "step", id: "a" }, outcome: { name: "false" }, target: { kind: "for_each_exit", for_each_id: "walk", lane_id: "a" } } as const;
    const occupied = { ...graph, edges: [edge] }; const before = JSON.stringify(occupied);
    const actions = mount(vi.fn(async () => true), occupied);
    act(() => view().handleConnectStart(new MouseEvent("mousedown", { clientX: 10, clientY: 20 }), { nodeId: "a", handleId: "false", handleType: "source" }));
    const end = new MouseEvent("mouseup", { clientX: 300, clientY: 400 }); Object.defineProperty(end, "target", { value: screen.getByTestId("pane") });
    act(() => view().handleConnectEnd(end, { toNode: null, to: { x: 300, y: 400 } } as Parameters<NonNullable<typeof captured.current>["handleConnectEnd"]>[1]));
    expect(actions.insert).toHaveBeenCalledWith(edge, { source: edge.source, outcome: edge.outcome, scope: { groupId: "", forEachId: "walk", laneId: "a" }, point: { x: 300, y: 400 }, clientPoint: { x: 300, y: 400 } });
    expect(actions.append).not.toHaveBeenCalled(); expect(actions.connect).not.toHaveBeenCalled(); expect(actions.disconnect).not.toHaveBeenCalled(); expect(JSON.stringify(occupied)).toBe(before);
  });
  it("uses the release-coordinate pane hit when native document mouseup retains its captured source target", () => {
    const actions = mount();
    const descriptor = Object.getOwnPropertyDescriptor(document, "elementFromPoint");
    const hit = vi.fn(() => screen.getByTestId("pane") as Element | null);
    Object.defineProperty(document, "elementFromPoint", { configurable: true, value: hit });
    try {
      act(() => view().handleConnectStart(new MouseEvent("mousedown", { clientX: 10, clientY: 20 }), { nodeId: "a", handleId: "false", handleType: "source" }));
      const end = new MouseEvent("mouseup", { clientX: 300, clientY: 400 });
      Object.defineProperty(end, "target", { configurable: true, value: screen.getByRole("button", { name: "Captured drag source" }) });
      act(() => view().handleConnectEnd(end, { toNode: null, to: { x: 300, y: 400 } } as Parameters<NonNullable<typeof captured.current>["handleConnectEnd"]>[1]));
      expect(actions.append).toHaveBeenCalledWith({ kind: "step", id: "a" }, { name: "false" }, { source: { kind: "step", id: "a" }, outcome: { name: "false" }, scope: { groupId: "", forEachId: "walk", laneId: "a" }, point: { x: 300, y: 400 }, clientPoint: { x: 300, y: 400 }, selectionGeneration: 0 });
      expect(hit).toHaveBeenCalledWith(300, 400);
      actions.append.mockClear();
      for (const obstruction of [screen.getByTestId("card-hit"), screen.getByRole("button", { name: "Toolbar overlay" }), screen.getByRole("dialog", { name: "Floating overlay" }), null]) {
        hit.mockReturnValue(obstruction);
        act(() => view().handleConnectStart(new MouseEvent("mousedown", { clientX: 10, clientY: 20 }), { nodeId: "a", handleId: "false", handleType: "source" }));
        Object.defineProperty(end, "target", { configurable: true, value: screen.getByTestId("pane") });
        act(() => view().handleConnectEnd(end, { toNode: null } as Parameters<NonNullable<typeof captured.current>["handleConnectEnd"]>[1]));
        expect(actions.append).not.toHaveBeenCalled(); expect(actions.connect).not.toHaveBeenCalled();
      }
    } finally { if (descriptor) Object.defineProperty(document, "elementFromPoint", descriptor); else Reflect.deleteProperty(document, "elementFromPoint"); }
  });
  it("keeps creation open through the drop's generated pane click while later deliberate pane gestures still close it", async () => {
    const actions = mount();
    const { WorkflowCanvasShell: ActualShell } = await vi.importActual<{ WorkflowCanvasShell: typeof WorkflowCanvasShell }>("./canvas-shell");
    render(<MemoryRouter><ActualShell {...view()} /></MemoryRouter>);
    const pane = screen.getByRole("button", { name: "Actual pane" });
    const drop = () => {
      act(() => view().handleConnectStart(new MouseEvent("mousedown", { clientX: 10, clientY: 20 }), { nodeId: "a", handleId: "false", handleType: "source" }));
      const end = new MouseEvent("mouseup", { clientX: 300, clientY: 400 }); Object.defineProperty(end, "target", { value: pane });
      act(() => view().handleConnectEnd(end, { toNode: null, to: { x: 300, y: 400 } } as Parameters<NonNullable<typeof captured.current>["handleConnectEnd"]>[1]));
    };
    drop(); expect(actions.append).toHaveBeenCalledTimes(1);
    fireEvent.click(pane, { clientX: 300, clientY: 400, detail: 1 });
    expect(actions.closeInspector).not.toHaveBeenCalled();
    fireEvent.click(pane, { clientX: 300, clientY: 400, detail: 1 });
    expect(actions.closeInspector).toHaveBeenCalledTimes(1);
    drop(); fireEvent.pointerDown(pane, { clientX: 300, clientY: 400 }); fireEvent.click(pane, { clientX: 300, clientY: 400, detail: 1 });
    expect(actions.closeInspector).toHaveBeenCalledTimes(2);
    drop(); fireEvent.click(pane, { clientX: 300, clientY: 400, detail: 0 });
    expect(actions.closeInspector).toHaveBeenCalledTimes(3);
    expect(actions.connect).not.toHaveBeenCalled(); expect(actions.disconnect).not.toHaveBeenCalled();
  });
  it("does not claim success when the transaction owner cancels and exposes real callback failures", async () => {
    const actions = mount(vi.fn(async () => false));
    await act(async () => view().handleConnect({ source: "for_each:walk", sourceHandle: "success", target: "done", targetHandle: "in" }));
    expect(view().graphEditNotice?.label).not.toBe("Connection added to the unapplied structure.");
    actions.connect.mockRejectedValueOnce(new Error("Conflict: reload the workflow"));
    await act(async () => view().handleConnect({ source: "for_each:walk", sourceHandle: "success", target: "done", targetHandle: "in" }));
    expect(view().graphEditNotice).toEqual({ label: "Conflict: reload the workflow", tone: "error" });
  });
});

it("snaps real dragstop to a free same-scope position and preserves graph membership", () => {
  const plain = { ...graph, entry: { kind: "step" as const, id: "one" }, for_each: [], steps: [
    { ...graph.steps[1], id: "one", step_id: "one", for_each_id: undefined, lane_id: undefined },
    { ...graph.steps[1], id: "two", step_id: "two", for_each_id: undefined, lane_id: undefined },
  ] };
  localStorage.clear(); const before = JSON.stringify(plain); mount(vi.fn(async () => true), plain);
  const one = view().nodesWithCanvasState.find((n) => n.id === "one")!;
  const two = view().nodesWithCanvasState.find((n) => n.id === "two")!;
  act(() => view().handleNodeDragStop({ ...two, position: { x: one.position.x + 3, y: one.position.y + 2 } }));
  const moved = view().nodesWithCanvasState.find((n) => n.id === "two")!;
  expect(moved.position).toEqual({ x: one.position.x, y: one.position.y + one.height! });
  expect(moved.position.x % 16).toBe(0); expect(moved.position.y % 16).toBe(0); expect(JSON.stringify(plain)).toBe(before);
});

it("fits live deep manual movement, persists adjusted coordinates and shrinks after real dragstop", () => {
  localStorage.clear();
  const nested: WorkflowCanvasGraph = { ...graph, entry: { kind: "group", id: "outer" }, groups: [{ id: "outer", display_name: "Once", body: { kind: "sequence", entry: { kind: "for_each", id: "walk" } } }], for_each: [{ ...graph.for_each![0], group_id: "outer", body: { kind: "sequence", entry: { kind: "step", id: "a" } } }], steps: graph.steps.slice(0, 2).map((s) => ({ ...s, lane_id: undefined })) };
  const definitionBefore = JSON.stringify(nested); mount(vi.fn(async () => true), nested);
  const card = () => view().nodesWithCanvasState.find((n) => n.id === "a")!;
  const container = () => view().nodesWithCanvasState.find((n) => n.id === "for_each:walk")!;
  act(() => view().handleNodesChange([{ type: "position", id: "a", position: { x: 800, y: 704 }, dragging: true }]));
  const bigWidth = container().width!; const bigHeight = container().height!;
  expect(bigWidth).toBeGreaterThanOrEqual(800 + card().width! + 32); expect(bigHeight).toBeGreaterThanOrEqual(704 + card().height! + 64);
  act(() => view().handleNodeDragStop({ ...card(), position: { x: 32, y: 128 } }));
  expect(card().position).toEqual({ x: 32, y: 128 }); expect(container().width).toBeLessThan(bigWidth); expect(container().height).toBeLessThan(bigHeight);
  expect(container().parentId).toBe("group:outer"); expect(card().parentId).toBe("for_each:walk"); expect(JSON.stringify(nested)).toBe(definitionBefore);
});
it("keeps the real note dragstop save callback and note metadata intact", () => {
  mount(); captured.saveNote.mockClear();
  const note = { id: "note", workflow_id: "ports", text: "annotation", x: 10, y: 20, color: "yellow", width: 240, height: 120 };
  act(() => view().handleNodeDragStop({ id: "annotation:note", type: "canvasNote", position: { x: 33, y: 47 }, data: { note } }));
  expect(captured.saveNote).toHaveBeenCalledExactlyOnceWith({ ...note, x: 33, y: 47 });
});
