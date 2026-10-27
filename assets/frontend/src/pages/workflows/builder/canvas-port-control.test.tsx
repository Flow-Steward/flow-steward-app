import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useConfirmDialog } from "@/components/shared/use-confirm-dialog";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { WorkflowStepStructureSetup } from "../detail/detail-step-structure-setup";
import { ReactFlowProvider, Position } from "@xyflow/react";
import { describe, expect, it, vi } from "vitest";
import { CanvasPortControl, CanvasPortProvider } from "./canvas-port-control";
import { definitionFromWorkflow } from "./change-set";

const source = { kind: "step", id: "check" } as const;
const outcome = { name: "true" } as const;
const definition = definitionFromWorkflow({ workflow_id: "test", steps: [{ step_id: "check", step_kind: "if", display_name: "Check" }, { step_id: "next", step_kind: "stop_success", display_name: "Next step" }], settings: {} });
function mount(occupied = false, dragging = false) {
  const actions = { canvasClick: vi.fn(), insert: vi.fn(), append: vi.fn(), connect: vi.fn(async () => true), disconnect: vi.fn(), notice: vi.fn() };
  render(<div data-testid="scaled-canvas" aria-label="Canvas card" role="button" tabIndex={0} onKeyDown={() => undefined} onClick={actions.canvasClick}><ReactFlowProvider><CanvasPortProvider value={{ definition: occupied ? { ...definition, graph: { ...definition.graph, edges: [{ source, outcome, target: { kind: "step", id: "next" } }] } } : definition, dragging, ...actions }}><CanvasPortControl source={source} sourceLabel="Check" port={{ outcome, label: "True" }} position={Position.Right} style={{ top: 24 }} occupied={occupied} /></CanvasPortProvider></ReactFlowProvider></div>);
  return actions;
}
describe("canvas port composition", () => {
  it("free click and Enter preserve the source/outcome and open creation, while Shift+F10 opens Connect", async () => {
    const actions = mount();
    const trigger = screen.getByRole("button", { name: /Add a step after Check · True/ });
    expect(trigger).toHaveAttribute("aria-haspopup", "dialog");
    expect(trigger).toHaveAttribute("aria-keyshortcuts", "Shift+F10");
    fireEvent.click(trigger);
    expect(actions.append).toHaveBeenCalledWith(source, outcome);
    fireEvent.keyDown(trigger, { key: "Enter" });
    expect(actions.append).toHaveBeenCalledTimes(2);
    fireEvent.keyDown(trigger, { key: "F10", shiftKey: true });
    expect(within(screen.getByRole("menu")).getAllByRole("menuitem")[0]).toHaveTextContent("New step…");
    fireEvent.click(screen.getByRole("menuitem", { name: "Next step" }));
    await waitFor(() => expect(actions.connect).toHaveBeenCalledWith(source, outcome, { kind: "step", id: "next" }));
    expect(actions.disconnect).not.toHaveBeenCalled();
  });
  it("New step on an occupied outcome emits insertion of the exact edge and never appends over its target", () => {
    const actions = mount(true); fireEvent.click(screen.getByRole("button", { name: /True/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "New step…" }));
    expect(actions.insert).toHaveBeenCalledWith({ source, outcome, target: { kind: "step", id: "next" } });
    expect(actions.append).not.toHaveBeenCalled(); expect(actions.connect).not.toHaveBeenCalled(); expect(actions.disconnect).not.toHaveBeenCalled();
  });
  it("occupied click opens Connect, preserves remove, and cancel writes nothing", () => {
    const actions = mount(true);
    const trigger = screen.getByRole("button", { name: /True/ });
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    fireEvent.click(trigger);
    expect(screen.getByRole("menuitem", { name: "Remove connection" })).toBeVisible();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(actions.connect).not.toHaveBeenCalled();
    expect(actions.disconnect).not.toHaveBeenCalled();
  });
  it("opens the occupied menu outside transformed canvas nodes and its pointer action never selects the card", () => {
    const actions = mount(true);
    fireEvent.click(screen.getByRole("button", { name: /True/ }));
    const surface = screen.getByRole("dialog", { name: "Connections" });
    expect(surface).toBeVisible();
    expect(within(screen.getByTestId("scaled-canvas")).queryByRole("dialog")).not.toBeInTheDocument();
    const item = screen.getByRole("menuitem", { name: "New step…" });
    fireEvent.pointerDown(item); fireEvent.mouseDown(item); fireEvent.click(item);
    expect(actions.insert).toHaveBeenCalledWith({ source, outcome, target: { kind: "step", id: "next" } });
    expect(actions.canvasClick).not.toHaveBeenCalled();
  });
  it.each(["Cancel", "Confirm"])("returns focus to the stable port after real replacement confirmation %s", async (decision) => {
    const staged = vi.fn();
    function ConfirmationHarness() {
      const { requestConfirmation, confirmDialog } = useConfirmDialog();
      const connect = async () => { const accepted = await requestConfirmation({ title: "Replace connection" }); if (accepted) staged(); return accepted; };
      return <><ReactFlowProvider><CanvasPortProvider value={{ definition: { ...definition, graph: { ...definition.graph, edges: [{ source, outcome, target: { kind: "step", id: "next" } }] } }, dragging: false, append: vi.fn(), connect, disconnect: vi.fn(), notice: vi.fn() }}><CanvasPortControl source={source} sourceLabel="Check" port={{ outcome, label: "True" }} position={Position.Right} style={{ top: 24 }} /></CanvasPortProvider></ReactFlowProvider><ConfirmDialog {...confirmDialog} /></>;
    }
    const user = userEvent.setup(); render(<ConfirmationHarness />);
    const trigger = screen.getByRole("button", { name: "Connect Check · True" });
    await user.click(trigger);
    await user.click(screen.getByRole("menuitem", { name: "Next step" }));
    const dialog = screen.getByRole("dialog", { name: "Replace connection" });
    await user.click(within(dialog).getByRole("button", { name: decision }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Replace connection" })).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(staged).toHaveBeenCalledTimes(decision === "Confirm" ? 1 : 0);
  });
  it.each([ ["group", "End the group here"], ["for_each", "End the item here"], ["lane", "End the lane here"] ])("uses the same %s exit label as the actual inspector picker", (kind, expected) => {
    const lane = kind === "lane" ? "a" : null;
    const exit = kind === "group" ? { kind: "group_exit", group_id: "box", lane_id: lane } as const : { kind: "for_each_exit", for_each_id: "box", lane_id: lane } as const;
    const current = definitionFromWorkflow({ workflow_id: "scoped", steps: [{ step_id: "check", step_kind: "set_variable", display_name: "Check", group_id: kind === "group" ? "box" : null, for_each_id: kind !== "group" ? "box" : null, lane_id: lane }], groups: kind === "group" ? [{ group_id: "box", display_name: "Box", body: { kind: "sequence" } }] : [], for_each: kind !== "group" ? [{ for_each_id: "box", over: "state.items", item_name: "item", body: lane ? { kind: "lanes", lanes: [{ lane_id: lane }] } : { kind: "sequence" } }] : [], settings: { execution_graph: { schema_version: 1, entry: null, edges: [{ source, outcome: { name: "success" }, target: exit }] } } });
    const { rerender } = render(<WorkflowStepStructureSetup ctx={{ preview: current, selectedStep: { runtimeId: "check", stepKind: "set_variable", raw: {} }, connectPort: vi.fn(), disconnectPort: vi.fn(), moveStep: vi.fn(), setEntry: vi.fn(), setCreateAsEntry: vi.fn(), setStepCreateContext: vi.fn() }} />);
    fireEvent.click(screen.getByRole("combobox", { name: "Next step for Next" }));
    const inspectorLabel = screen.getByRole("option", { name: expected }).textContent;
    rerender(<ReactFlowProvider><CanvasPortProvider value={{ definition: current, dragging: false, append: vi.fn(), connect: vi.fn(async () => true), disconnect: vi.fn(), notice: vi.fn() }}><CanvasPortControl source={source} sourceLabel="Check" port={{ outcome: { name: "success" }, label: "Next" }} position={Position.Right} style={{ top: 24 }} /></CanvasPortProvider></ReactFlowProvider>);
    fireEvent.click(screen.getByRole("button", { name: "Connect Check · Next" }));
    expect(screen.getByRole("menuitem", { name: expected })).toHaveTextContent(inspectorLabel || expected);
  });
  it.each(["Enter", " "])("free body Next opens its existing scoped menu with %s and still appends through New step", (key) => {
    const current = definitionFromWorkflow({ workflow_id: "item-menu", steps: [{ step_id: "check", step_kind: "set_variable", for_each_id: "each" }], for_each: [{ for_each_id: "each", over: "state.items", item_name: "item", body: { kind: "sequence" } }], settings: {} });
    const actions = { append: vi.fn(), connect: vi.fn(async () => true), disconnect: vi.fn(), notice: vi.fn() };
    render(<ReactFlowProvider><CanvasPortProvider value={{ definition: current, dragging: false, ...actions }}><CanvasPortControl source={source} sourceLabel="Check" port={{ outcome: { name: "success" }, label: "Next" }} position={Position.Right} style={{ top: 24 }} /></CanvasPortProvider></ReactFlowProvider>);
    const trigger = screen.getByRole("button", { name: /Check · Next/ });
    fireEvent.keyDown(trigger, { key });
    expect(screen.getByRole("menuitem", { name: "End the item here" })).toBeVisible();
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(actions.append).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    fireEvent.keyDown(trigger, { key: "F10", shiftKey: true });
    fireEvent.click(screen.getByRole("menuitem", { name: "New step…" }));
    expect(actions.append).toHaveBeenCalledExactlyOnceWith(source, { name: "success" });
    expect(actions.connect).not.toHaveBeenCalled();
    expect(actions.disconnect).not.toHaveBeenCalled();
  });
  it("a drag never also adds a step", () => {
    const actions = mount(false, true);
    const trigger = screen.getByRole("button", { name: /True/ });
    fireEvent.mouseDown(trigger, { clientX: 0, clientY: 0 });
    fireEvent.mouseUp(trigger, { clientX: 20, clientY: 20 });
    fireEvent.click(trigger, { clientX: 20, clientY: 20 });
    expect(actions.append).not.toHaveBeenCalled();
  });
});
