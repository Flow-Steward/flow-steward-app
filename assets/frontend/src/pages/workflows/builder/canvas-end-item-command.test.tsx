import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLayoutEffect, useMemo } from "react";
import { MemoryRouter } from "react-router";
import * as api from "./api";
import { WorkflowCanvasBuilderSurface } from "./canvas";
import { canvasGraphWithDefinition, definitionFromWorkflow } from "./change-set";
import { useWorkflowCanvasHandlers } from "../detail/detail-canvas-handlers";
import { useWorkflowChangeSet, type WorkflowChangeSetController } from "../detail/detail-change-set";
import type { WorkflowCanvasGraph, WorkflowGraphEdge } from "../shared/api";

// The real Builder renders the port and stages through the real page callback
// and local history. Only external dry-run I/O and native SVG sizing are stubbed.
const downstream: WorkflowGraphEdge = { source: { kind: "for_each", id: "each" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } };
const graph: WorkflowCanvasGraph = {
  workflow: { id: "end-item", display_name: "End item", enabled: false, callable_by_parent: false },
  interface_contract: {}, output_contract: {}, entry: { kind: "for_each", id: "each" },
  steps: [], groups: [], for_each: [], edges: [downstream], automation_rules: [], validation_issues: [],
};
function definition(laneId: string | null) {
  return definitionFromWorkflow({
    workflow_id: "end-item", revision: 4,
    steps: [
      { step_id: "process", step_kind: "set_variable", display_name: "Process item", for_each_id: "each", lane_id: laneId },
      { step_id: "root_next", step_kind: "set_variable", display_name: "Root next" },
      { step_id: "done", step_kind: "stop_success", display_name: "Done" },
    ],
    for_each: [{ for_each_id: "each", display_name: "Each item", over: "state.items", item_name: "item", body: laneId
      ? { kind: "lanes", lanes: [{ lane_id: laneId, entry: { kind: "step", id: "process" } }] }
      : { kind: "sequence", entry: { kind: "step", id: "process" } } }],
    settings: { execution_graph: { schema_version: 1, entry: graph.entry, edges: [downstream] } },
  });
}
const noop = () => undefined;
const refetch = async () => undefined;
const openNewStepDrawer = vi.fn();
const requestConfirmation = vi.fn(async () => true);
let author: WorkflowChangeSetController | null = null;
function Host({ laneId = null }: { laneId?: string | null }) {
  const current = useMemo(() => definition(laneId), [laneId]);
  const changeSet = useWorkflowChangeSet({ workflowId: "end-item", serverRevision: 4, serverDefinition: current, refetchWorkflowSurfaces: refetch });
  const actions = useWorkflowCanvasHandlers({ changeSet, requestConfirmation, openNewStepDrawer, setSaveNotice: noop });
  useLayoutEffect(() => { author = changeSet; }, [changeSet]);
  return <MemoryRouter><WorkflowCanvasBuilderSurface advancedMode canvasGraph={canvasGraphWithDefinition(graph, changeSet.preview)} changeSet={changeSet} workflowId="end-item"
    onAppendStep={actions.appendStepAfterOutcome} onConnectPort={actions.connectPort} onDisconnectPort={actions.disconnectPort} onInsertStep={actions.insertStepOnEdge}
    onCloseInspector={noop} onOpenGroup={noop} onOpenGroupAutomation={noop} onOpenNewGroup={noop} onOpenNewStep={noop}
    onOpenStep={noop} onOpenStepAutomation={noop} onOpenWorkflowAutomation={noop} onOpenWorkflowSettings={noop}
    onToggleAdvancedMode={noop} onValidationIssue={noop} /></MemoryRouter>;
}
let bboxBefore: PropertyDescriptor | undefined;
beforeEach(() => {
  author = null;
  openNewStepDrawer.mockClear(); requestConfirmation.mockClear();
  vi.spyOn(api, "applyWorkflowChanges").mockImplementation(async (_workflowId, payload) => ({
    workflow_id: "end-item", revision: 4, valid: true, issues: [], blocking_issues: [], applied: false,
    workflow: { workflow_id: "end-item", revision: payload.expected_revision, steps: [], settings: {} },
  }));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  bboxBefore = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
  Object.defineProperty(SVGElement.prototype, "getBBox", { configurable: true, value: () => ({ x: 0, y: 0, width: 120, height: 16 }) });
});
afterEach(() => {
  if (bboxBefore) Object.defineProperty(SVGElement.prototype, "getBBox", bboxBefore);
  else Reflect.deleteProperty(SVGElement.prototype, "getBBox");
  vi.unstubAllGlobals(); vi.restoreAllMocks();
});

it.each([null, "email"])("ends the item from a free Next with exact lane %s and restores that one edge with Undo/Redo", async (laneId) => {
  const user = userEvent.setup();
  const initial = definition(laneId);
  render(<Host laneId={laneId} />);
  const source = await screen.findByRole("group", { name: "Open step Process item" });
  const next = within(source).getByRole("button", { name: /Process item · Next/ });
  await user.click(next);
  const command = screen.getByRole("menuitem", { name: laneId ? "End the lane here" : "End the item here" });
  expect(command).toBeVisible();
  expect(openNewStepDrawer).not.toHaveBeenCalled();
  await user.click(command);
  const edge: WorkflowGraphEdge = { source: { kind: "step", id: "process" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "each", lane_id: laneId } };
  await waitFor(() => expect(author!.operations).toEqual([{ op: "connect", edge }]));
  expect(author!.preview.graph.edges).toEqual([downstream, edge]);
  expect(author!.preview.steps).toEqual(initial.steps);
  expect(author!.preview.forEach).toEqual(initial.forEach);
  expect(openNewStepDrawer).not.toHaveBeenCalled();
  expect(requestConfirmation).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  const canvas = screen.getByRole("region", { name: "Workflow canvas" });
  act(() => canvas.focus());
  fireEvent.keyDown(canvas, { key: "z", code: "KeyZ", ctrlKey: true });
  expect(author!.operations).toEqual([]);
  expect(author!.preview.graph.edges).toEqual([downstream]);
  expect(author!.preview.steps).toEqual(initial.steps);
  fireEvent.keyDown(canvas, { key: "z", code: "KeyZ", ctrlKey: true, shiftKey: true });
  expect(author!.operations).toEqual([{ op: "connect", edge }]);
  expect(author!.preview.graph.edges).toEqual([downstream, edge]);
  expect(author!.preview.steps).toEqual(initial.steps);
});

it("keeps End item absent at root and retains root Next creation and the keyboard Connections menu", async () => {
  const user = userEvent.setup(); render(<Host />);
  const source = await screen.findByRole("group", { name: "Open step Root next" });
  const next = within(source).getByRole("button", { name: /Root next · Next/ });
  expect(next).toHaveAttribute("aria-haspopup", "dialog");
  await user.click(next);
  expect(openNewStepDrawer).toHaveBeenCalledExactlyOnceWith({ groupId: "", forEachId: "", laneId: "", insert: { append: true, label: "root_next · success", source: { kind: "step", id: "root_next" }, outcome: { name: "success" } } });
  expect(screen.queryByRole("menuitem", { name: "End the item here" })).not.toBeInTheDocument();
  fireEvent.keyDown(next, { key: "F10", shiftKey: true });
  expect(screen.getByRole("menu", { name: "Connections" })).toBeVisible();
  expect(screen.getByRole("menuitem", { name: "New step…" })).toBeVisible();
  expect(screen.queryByRole("menuitem", { name: /End the (item|lane) here/ })).not.toBeInTheDocument();
  expect(author!.operations).toEqual([]);
  expect(author!.preview.graph.edges).toEqual([downstream]);
});
