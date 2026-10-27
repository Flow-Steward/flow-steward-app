import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useLayoutEffect } from "react";
import type { ReactFlowInstance } from "@xyflow/react";
import { MemoryRouter } from "react-router";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { WorkflowCanvasBuilderSurface } from "./canvas";
import { canvasGraphWithDefinition, definitionFromWorkflow } from "./change-set";
import { useWorkflowChangeSet } from "../detail/detail-change-set";
import type { WorkflowCanvasGraph } from "../shared/api";

const ids = ["alpha", "beta", "gamma"] as const;
const graph: WorkflowCanvasGraph = {
  workflow: { id: "selection_commands", display_name: "Selection commands", enabled: false, callable_by_parent: false },
  interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true },
  output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
  entry: { kind: "step", id: "alpha" }, groups: [], for_each: [],
  steps: ids.map((id) => ({ id, step_id: id, kind: "set_variable", display_name: id[0].toUpperCase() + id.slice(1), config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] })),
  edges: ids.slice(1).map((id, index) => ({ source: { kind: "step", id: ids[index] }, outcome: { name: "success" }, target: { kind: "step", id } })),
  automation_rules: [], validation_issues: [],
};
const definition = definitionFromWorkflow({
  workflow_id: "selection_commands", revision: 4,
  steps: graph.steps.map((item) => ({ step_id: item.id, step_kind: item.kind, display_name: item.display_name })),
  settings: { execution_graph: { schema_version: 1, entry: graph.entry, edges: graph.edges } },
});
const otherWorkflowId = "selection_commands_other";
const otherGraph: WorkflowCanvasGraph = { ...graph, workflow: { ...graph.workflow, id: otherWorkflowId } };
const otherDefinition = definitionFromWorkflow({
  workflow_id: otherWorkflowId, revision: 4,
  steps: graph.steps.map((item) => ({ step_id: item.id, step_kind: item.kind, display_name: item.display_name })),
  settings: { execution_graph: { schema_version: 1, entry: graph.entry, edges: graph.edges } },
});
const noop = () => undefined;
let controller: ReturnType<typeof useWorkflowChangeSet> | null = null;
let flow: ReactFlowInstance | null = null;
function Host({ dialogOpen = false, workflowId = "selection_commands", commandViewIdentity = "", previewCreation = false }: { dialogOpen?: boolean; workflowId?: string; commandViewIdentity?: string; previewCreation?: boolean }) {
  const changeSet = useWorkflowChangeSet({ workflowId, serverDefinition: workflowId === otherWorkflowId ? otherDefinition : definition, serverRevision: 4, refetchWorkflowSurfaces: async () => undefined });
  useLayoutEffect(() => { controller = changeSet; }, [changeSet]);
  const serverGraph = workflowId === otherWorkflowId ? otherGraph : graph;
  const canvasGraph = previewCreation ? canvasGraphWithDefinition(serverGraph, changeSet.preview) : serverGraph;
  return <MemoryRouter>
    <WorkflowCanvasBuilderSurface advancedMode canvasGraph={canvasGraph} changeSet={changeSet} workflowId={workflowId} commandViewIdentity={commandViewIdentity}
      creationSelection={previewCreation ? { nodeId: "for_each:temporary", nonce: 1 } : undefined}
      onCanvasReady={(instance) => { flow = instance; }}
      onCloseInspector={noop} onConnectPort={() => false} onDisconnectPort={noop} onInsertStep={noop}
      onOpenGroup={noop} onOpenGroupAutomation={noop} onOpenNewGroup={noop} onOpenNewStep={noop}
      onOpenStep={noop} onOpenStepAutomation={noop} onOpenWorkflowAutomation={noop}
      onOpenWorkflowSettings={noop} onToggleAdvancedMode={noop} onValidationIssue={noop} />
    <ConfirmDialog open={dialogOpen} title="Keep this selection?" body="Dialog keyboard boundary" destructive={false} onCancel={noop} onConfirm={noop} />
  </MemoryRouter>;
}
let getBBoxBefore: PropertyDescriptor | undefined;
beforeEach(() => {
  localStorage.clear(); controller = null; flow = null;
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  getBBoxBefore = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
  Object.defineProperty(SVGElement.prototype, "getBBox", { configurable: true, value: () => ({ x: 0, y: 0, width: 120, height: 16 }) });
});
afterEach(() => {
  localStorage.clear(); controller = null; flow = null;
  if (getBBoxBefore) Object.defineProperty(SVGElement.prototype, "getBBox", getBBoxBefore);
  else Reflect.deleteProperty(SVGElement.prototype, "getBBox");
  vi.unstubAllGlobals();
});
const card = (name: string) => screen.getByRole("group", { name: `Open step ${name}` });
const bar = () => screen.getByRole("toolbar", { name: "Selected items" });
/** JSDOM has no hit-testing layout. Model only this dispatched card click's
 * known target stack, preserving the production secondary-action hit-test path. */
function clickCard(target: HTMLElement, init: MouseEventInit = {}) {
  const descriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
  const stack: Element[] = [];
  for (let element: Element | null = target; element; element = element.parentElement) stack.push(element);
  Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: (x: number, y: number) => {
    if (x !== (init.clientX ?? 0) || y !== (init.clientY ?? 0)) throw new Error("Unmodelled hit-test point outside the dispatched card click");
    return stack;
  } });
  try { fireEvent.click(target, init); }
  finally {
    if (descriptor) Object.defineProperty(document, "elementsFromPoint", descriptor);
    else Reflect.deleteProperty(document, "elementsFromPoint");
  }
}
/** React Flow's focusable wrapper has no unique accessible name. */
function focusCanvasNode(name: string) {
  const node = screen.getAllByRole("group").find((candidate) =>
    candidate.getAttribute("aria-roledescription") === "node" &&
    within(candidate).queryByRole("group", { name: `Open step ${name}` }));
  if (!node) throw new Error(`Missing ReactFlow wrapper for ${name}`);
  act(() => node.focus());
  expect(node).toHaveFocus();
  return node;
}

it("adds a node by Shift+click and presents Zoom, Delete and Clear on a floating bar from two selected", async () => {
  render(<Host />);
  const alpha = await screen.findByRole("group", { name: "Open step Alpha" });
  clickCard(alpha);
  await waitFor(() => expect(flow?.getNodes().find((node) => node.id === "alpha")?.selected).toBe(true));
  // One selected node is the open step, not a selection: no bar yet (Components2 › Selection bar).
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  const additive = { key: "Shift", code: "ShiftLeft", shiftKey: true };
  fireEvent.keyDown(window, additive);
  clickCard(card("Beta"), additive);
  await waitFor(() => {
    expect(flow?.getNodes().find((node) => node.id === "alpha")?.selected).toBe(true);
    expect(flow?.getNodes().find((node) => node.id === "beta")?.selected).toBe(true);
  });
  fireEvent.keyUp(window, additive);
  expect(screen.getByRole("button", { name: "More actions for Alpha" })).toBeVisible();
  expect(screen.getByRole("button", { name: "More actions for Beta" })).toBeVisible();
  const selected = bar(); // First expected semantic RED: Task 4 selection bar is absent.
  expect(within(selected).getByText("2 selected")).toBeVisible();
  expect(within(selected).getByRole("button", { name: "Zoom to selection" })).toHaveAttribute("title", "Zoom to the selection (Shift+2)");
  expect(within(selected).getByRole("button", { name: "Delete" })).toHaveAttribute("title", "Delete the selection (Delete)");
  expect(within(selected).queryByRole("button", { name: /Copy|Duplicate|Disable/ })).not.toBeInTheDocument();
  fireEvent.click(within(selected).getByRole("button", { name: "Clear the selection (Esc)" }));
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "More actions for Alpha" })).not.toBeInTheDocument();
  expect(screen.getByRole("toolbar", { name: "Canvas tools" })).toBeVisible();
});

it("scopes both Control/Meta+A and Escape to the canvas, leaving search input, IME and modal focus alone", async () => {
  const view = render(<Host />);
  await screen.findByRole("group", { name: "Open step Alpha" });
  let canvasNode = focusCanvasNode("Alpha");
  fireEvent.keyDown(canvasNode, { key: "a", code: "KeyA", ctrlKey: true, isComposing: true });
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  for (const modifier of [{ ctrlKey: true }, { metaKey: true }]) {
    canvasNode = focusCanvasNode("Alpha");
    fireEvent.keyDown(canvasNode, { key: "a", code: "KeyA", ...modifier });
    expect(within(bar()).getByText("3 selected")).toBeVisible();
    fireEvent.keyDown(canvasNode, { key: "Escape", code: "Escape" });
    expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  }
  fireEvent.click(screen.getByRole("button", { name: /Search canvas/ }));
  const search = screen.getByRole("textbox", { name: "Search workflow canvas" });
  fireEvent.change(search, { target: { value: "beta" } });
  fireEvent.keyDown(search, { key: "a", code: "KeyA", ctrlKey: true });
  expect(search).toHaveValue("beta");
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Search canvas/ }));
  clickCard(card("Alpha"));
  canvasNode = focusCanvasNode("Alpha");
  fireEvent.keyDown(canvasNode, { key: "a", code: "KeyA", ctrlKey: true });
  expect(bar()).toHaveTextContent("3 selected");
  view.rerender(<Host dialogOpen />);
  const dialog = screen.getByRole("dialog", { name: "Keep this selection?" });
  fireEvent.keyDown(within(dialog).getByRole("button", { name: /Cancel/ }), { key: "Escape", code: "Escape" });
  view.rerender(<Host />);
  expect(bar()).toHaveTextContent("3 selected");
  canvasNode = focusCanvasNode("Alpha");
  fireEvent.keyDown(canvasNode, { key: "Escape", code: "Escape" });
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  view.rerender(<Host dialogOpen />);
  const blockedDialog = screen.getByRole("dialog", { name: "Keep this selection?" });
  fireEvent.keyDown(within(blockedDialog).getByRole("button", { name: /Cancel/ }), { key: "a", code: "KeyA", ctrlKey: true });
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
}, 15_000);

it("routes graph Undo/Redo keys only from canvas focus while preserving native search-input editing", async () => {
  render(<Host />);
  await screen.findByRole("group", { name: "Open step Alpha" });
  let canvasNode = focusCanvasNode("Alpha");
  await act(() => controller!.stageGraphIntent([{ op: "create_step", step: { step_id: "draft", step_kind: "set_variable", display_name: "Draft" } }]));
  expect(controller!.operations).toHaveLength(1);
  canvasNode = focusCanvasNode("Alpha");
  fireEvent.keyDown(canvasNode, { key: "z", code: "KeyZ", ctrlKey: true });
  expect(controller!.operations).toEqual([]);
  canvasNode = focusCanvasNode("Alpha");
  fireEvent.keyDown(canvasNode, { key: "z", code: "KeyZ", ctrlKey: true, shiftKey: true });
  expect(controller!.operations).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: /Search canvas/ }));
  const search = screen.getByRole("textbox", { name: "Search workflow canvas" });
  fireEvent.change(search, { target: { value: "text draft" } });
  fireEvent.keyDown(search, { key: "z", code: "KeyZ", ctrlKey: true });
  fireEvent.keyDown(search, { key: "z", code: "KeyZ", ctrlKey: true, shiftKey: true });
  expect(search).toHaveValue("text draft");
  expect(search).toHaveFocus();
  expect(controller!.operations).toHaveLength(1);
});

it("retires selected display IDs when an unkeyed canvas switches workflows with the same node IDs", async () => {
  const view = render(<Host />);
  clickCard(await screen.findByRole("group", { name: "Open step Alpha" }));
  await waitFor(() => expect(flow?.getNodes().find((node) => node.id === "alpha")?.selected).toBe(true));
  view.rerender(<Host workflowId={otherWorkflowId} />);
  await screen.findByRole("group", { name: "Open step Alpha" });
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  await waitFor(() => expect(flow?.getNodes().find((node) => node.id === "alpha")?.selected).toBe(false));
  clickCard(screen.getByRole("group", { name: "Open step Alpha" }));
  await waitFor(() => expect(flow?.getNodes().find((node) => node.id === "alpha")?.selected).toBe(true));
});

it("retires selected display IDs when a Hook view changes but the child workflow reuses them", async () => {
  const view = render(<Host commandViewIdentity="hook-a:child" />);
  clickCard(await screen.findByRole("group", { name: "Open step Alpha" }));
  await waitFor(() => expect(flow?.getNodes().find((node) => node.id === "alpha")?.selected).toBe(true));
  view.rerender(<Host commandViewIdentity="hook-b:child" />);
  await screen.findByRole("group", { name: "Open step Alpha" });
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  await waitFor(() => expect(flow?.getNodes().find((node) => node.id === "alpha")?.selected).toBe(false));
});


it("retires a hovered Step More menu on command-owner changes without reviving it on return", async () => {
  const view = render(<Host commandViewIdentity="hook-a:child" />);
  const alpha = await screen.findByRole("group", { name: "Open step Alpha" });
  fireEvent.mouseEnter(alpha, { relatedTarget: document.body });
  fireEvent.click(screen.getByRole("button", { name: "More actions for Alpha" }));
  expect(screen.getByRole("menuitem", { name: "Disable" })).toBeVisible();
  view.rerender(<Host commandViewIdentity="hook-a:child" />);
  expect(screen.getByRole("menuitem", { name: "Disable" })).toBeVisible();

  view.rerender(<Host commandViewIdentity="hook-b:child" />);
  expect(screen.getByRole("group", { name: "Open step Alpha" })).toBe(alpha);
  expect(screen.queryByRole("menuitem", { name: "Disable" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "More actions for Alpha" })).toHaveAttribute("aria-expanded", "false");
  view.rerender(<Host commandViewIdentity="hook-a:child" />);
  expect(screen.getByRole("group", { name: "Open step Alpha" })).toBe(alpha);
  expect(screen.queryByRole("menuitem", { name: "Disable" })).not.toBeInTheDocument();
  expect(controller!.operations).toEqual([]);
});


it("retires a discarded temporary container selection without reviving it when its display ID returns", async () => {
  render(<Host previewCreation />);
  await screen.findByRole("group", { name: "Open step Alpha" });
  const create = { op: "create_for_each" as const, for_each: {
    for_each_id: "temporary", group_id: null, display_name: "Temporary items",
    body: { kind: "sequence" as const }, over: "state.items", item_name: "item", index_name: null,
  } };
  await act(() => controller!.stageGraphIntent([create]));
  await waitFor(() => expect(flow?.getNodes().find((node) => node.id === "for_each:temporary")?.selected).toBe(true));
  expect(controller!.operations).toEqual([create]);

  act(() => controller!.discard());
  await waitFor(() => expect(flow?.getNodes().some((node) => node.id === "for_each:temporary")).toBe(false));
  expect(controller!.operations).toEqual([]);
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  expect(screen.getByRole("toolbar", { name: "Canvas tools" })).toBeVisible();
  expect(card("Alpha")).toBeVisible();

  // The handled creation nonce is unchanged: a reintroduced ID is a new node,
  // not authority to revive the discarded node's old selection.
  await act(() => controller!.stageGraphIntent([create]));
  await waitFor(() => expect(flow?.getNodes().find((node) => node.id === "for_each:temporary")?.selected).toBe(false));
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  expect(controller!.operations).toEqual([create]);
});

it("walks the graph with the arrows: entry first, along the connection, and back the way it came", async () => {
  render(<Host />);
  await screen.findByRole("group", { name: "Open step Alpha" });
  const selectedId = () => flow?.getNodes().filter((node) => node.selected).map((node) => node.id);
  const region = screen.getByRole("region", { name: "Workflow canvas" });
  act(() => region.focus());
  fireEvent.keyDown(region, { key: "ArrowRight", code: "ArrowRight" });
  await waitFor(() => expect(selectedId()).toEqual(["alpha"]));
  fireEvent.keyDown(focusCanvasNode("Alpha"), { key: "ArrowRight", code: "ArrowRight" });
  await waitFor(() => expect(selectedId()).toEqual(["beta"]));
  fireEvent.keyDown(focusCanvasNode("Beta"), { key: "ArrowRight", code: "ArrowRight" });
  await waitFor(() => expect(selectedId()).toEqual(["gamma"]));
  fireEvent.keyDown(focusCanvasNode("Gamma"), { key: "ArrowLeft", code: "ArrowLeft" });
  await waitFor(() => expect(selectedId()).toEqual(["beta"]));
});

it("moves the selection one grid step with Shift+arrows instead of nudging pixels", async () => {
  render(<Host />);
  await screen.findByRole("group", { name: "Open step Alpha" });
  clickCard(card("Alpha"));
  await waitFor(() => expect(flow?.getNodes().find((node) => node.id === "alpha")?.selected).toBe(true));
  const before = flow!.getNodes().find((node) => node.id === "alpha")!.position;
  fireEvent.keyDown(focusCanvasNode("Alpha"), { key: "ArrowDown", code: "ArrowDown", shiftKey: true });
  await waitFor(() => expect(flow!.getNodes().find((node) => node.id === "alpha")!.position).toEqual({ x: before.x, y: before.y + 16 }));
});

/** Exact SVG line paint has no accessible role; stay inside the located canvas. */
function edgePath(id: string) {
  const canvas = screen.getByRole("region", { name: "Workflow canvas" });
  return canvas.querySelector<SVGPathElement>(`.react-flow__edge[data-id="${id}"] .react-flow__edge-path`);
}

it("makes the selected step's connections stand out", async () => {
  render(<Host />);
  await screen.findByRole("group", { name: "Open step Alpha" });
  await waitFor(() => expect(edgePath("edge:step:alpha:success")).not.toBeNull());
  expect(edgePath("edge:step:alpha:success")).not.toHaveStyle({ stroke: "var(--fs-workflow-edge-selected)" });
  clickCard(card("Alpha"));
  await waitFor(() => expect(edgePath("edge:step:alpha:success")).toHaveStyle({ stroke: "var(--fs-workflow-edge-selected)", strokeWidth: "var(--fs-workflow-edge-width-selected)" }));
  expect(edgePath("edge:step:beta:success")).not.toHaveStyle({ stroke: "var(--fs-workflow-edge-selected)" });
});
