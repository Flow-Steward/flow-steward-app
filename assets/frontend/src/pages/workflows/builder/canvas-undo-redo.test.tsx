import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { MemoryRouter } from "react-router";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { WorkflowCanvasBuilderSurface } from "./canvas";
import { definitionFromWorkflow } from "./change-set";
import type { WorkflowChangeOperation } from "./api";
import { useWorkflowChangeSet, type WorkflowChangeSetController } from "../detail/detail-change-set";
import type { WorkflowCanvasGraph } from "../shared/api";

/**
 * Canvas › Undo on the builder page: the keys reach the latest action after the
 * ordinary user paths (a drawer's Save button, a click on the canvas), while a
 * text field, an IME composition and a modal dialog keep their own keys.
 */

const graph: WorkflowCanvasGraph = {
  workflow: { id: "undo_flow", display_name: "Undo flow", enabled: false, callable_by_parent: false },
  interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true },
  output_contract: { output_mode: "none", expected_outputs: [] },
  entry: { kind: "step", id: "alpha" }, groups: [], for_each: [], edges: [], automation_rules: [], validation_issues: [],
  steps: [{ id: "alpha", step_id: "alpha", kind: "set_variable", display_name: "Alpha", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] }],
};
const definition = definitionFromWorkflow({
  workflow_id: "undo_flow", revision: 4,
  steps: [{ step_id: "alpha", step_kind: "set_variable", display_name: "Alpha" }],
  settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "alpha" }, edges: [] } },
});
const childDefinition = definitionFromWorkflow({ workflow_id: "hook_child", revision: 2, steps: [] });
const draft: WorkflowChangeOperation = { op: "create_step", step: { step_id: "draft", step_kind: "agent", display_name: "Agent" } };
const setVariable: WorkflowChangeOperation = { op: "create_step", step: { step_id: "set_1", step_kind: "set_variable", display_name: "Set variable" } };
const noop = () => undefined;
let author: WorkflowChangeSetController | null = null;
let child: WorkflowChangeSetController | null = null;

function Host({ dialogOpen = false, hook = false }: { dialogOpen?: boolean; hook?: boolean }) {
  const authorSet = useWorkflowChangeSet({ workflowId: "undo_flow", serverDefinition: definition, serverRevision: 4, refetchWorkflowSurfaces: async () => undefined });
  const childSet = useWorkflowChangeSet({ workflowId: "hook_child", placementViewKey: "before_finish", serverDefinition: childDefinition, serverRevision: 2, refetchWorkflowSurfaces: async () => undefined });
  useLayoutEffect(() => { author = authorSet; child = childSet; }, [authorSet, childSet]);
  // The canvas's one change set inside a hook: the hook's changes plus the workflow's own (detail-hook-scope).
  const canvasChangeSet = hook && childSet.hasChanges
    ? { ...childSet, hasChanges: true, operations: [...authorSet.operations, ...childSet.operations] } as WorkflowChangeSetController
    : authorSet;
  return <MemoryRouter>
    <WorkflowCanvasBuilderSurface advancedMode canvasGraph={graph} changeSet={canvasChangeSet} workflowId="undo_flow"
      // Nothing selected: the placement owner is the author's, as in detail-hook-scope.
      {...(hook ? { placementHistoryOwners: [authorSet, childSet], resolvePlacementOwner: (ids: readonly string[]) => (ids.length ? null : authorSet) } : {})}
      onCloseInspector={noop} onConnectPort={() => false} onDisconnectPort={noop} onInsertStep={noop}
      onOpenGroup={noop} onOpenGroupAutomation={noop} onOpenNewGroup={noop} onOpenNewStep={noop}
      onOpenStep={noop} onOpenStepAutomation={noop} onOpenWorkflowAutomation={noop}
      onOpenWorkflowSettings={noop} onToggleAdvancedMode={noop} onValidationIssue={noop} />
    <aside aria-label="Step editor">
      <button type="button">Save step</button>
      <input aria-label="Step name" defaultValue="Agent" />
      <input aria-label="Optional step" type="checkbox" />
    </aside>
    <ConfirmDialog open={dialogOpen} title="Leave the builder?" body="Dialog keyboard boundary" destructive={false} onCancel={noop} onConfirm={noop} />
  </MemoryRouter>;
}

let getBBoxBefore: PropertyDescriptor | undefined;
beforeEach(() => {
  author = null; child = null;
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  getBBoxBefore = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
  Object.defineProperty(SVGElement.prototype, "getBBox", { configurable: true, value: () => ({ x: 0, y: 0, width: 120, height: 16 }) });
});
afterEach(() => {
  if (getBBoxBefore) Object.defineProperty(SVGElement.prototype, "getBBox", getBBoxBefore);
  else Reflect.deleteProperty(SVGElement.prototype, "getBBox");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const undoKey = { key: "z", code: "KeyZ", ctrlKey: true };
const redoKey = { key: "z", code: "KeyZ", ctrlKey: true, shiftKey: true };
const tools = () => screen.getByRole("toolbar", { name: "Canvas tools" });

async function mounted(props: { hook?: boolean } = {}) {
  const view = render(<Host {...props} />);
  await screen.findByRole("group", { name: "Open step Alpha" });
  return view;
}

it("undoes a form-staged step from the page body, a drawer button and a checkbox, with every key", async () => {
  await mounted();
  // The step editor's Save stages the step (changeSet.stage): an ordinary user path.
  act(() => author!.stage(draft));
  const save = screen.getByRole("button", { name: "Save step" });
  act(() => { save.focus(); save.blur(); });
  expect(document.body).toHaveFocus();
  fireEvent.keyDown(document.body, undoKey);
  expect(author!.operations).toEqual([]);
  fireEvent.keyDown(document.body, redoKey);
  expect(author!.operations).toEqual([draft]);
  // The drawer's button keeps the focus after Save.
  act(() => save.focus());
  fireEvent.keyDown(save, { key: "z", code: "KeyZ", metaKey: true });
  expect(author!.operations).toEqual([]);
  const optional = screen.getByRole("checkbox", { name: "Optional step" });
  act(() => optional.focus());
  fireEvent.keyDown(optional, { key: "y", code: "KeyY", ctrlKey: true });
  expect(author!.operations).toEqual([draft]);
  // A Russian layout prints я on KeyZ; ⌘Shift+Z redoes.
  fireEvent.keyDown(optional, { key: "я", code: "KeyZ", ctrlKey: true });
  expect(author!.operations).toEqual([]);
  fireEvent.keyDown(optional, { key: "Я", code: "KeyZ", metaKey: true, shiftKey: true });
  expect(author!.operations).toEqual([draft]);
  fireEvent.keyDown(optional, { key: "y", code: "KeyY", metaKey: true });
  expect(author!.operations).toEqual([draft]); // Nothing left to redo: no change.
});

it("leaves the keys to a text field, an IME composition and an open modal dialog", async () => {
  const view = await mounted();
  act(() => author!.stage(draft));
  const name = screen.getByRole("textbox", { name: "Step name" });
  act(() => name.focus());
  expect(fireEvent.keyDown(name, undoKey)).toBe(true); // Not prevented: the field's own Undo runs.
  expect(author!.operations).toEqual([draft]);
  const save = screen.getByRole("button", { name: "Save step" });
  act(() => save.focus());
  fireEvent.keyDown(save, { ...undoKey, isComposing: true });
  expect(author!.operations).toEqual([draft]);
  view.rerender(<Host dialogOpen />);
  const dialog = screen.getByRole("dialog", { name: "Leave the builder?" });
  fireEvent.keyDown(within(dialog).getByRole("button", { name: /Cancel/ }), undoKey);
  expect(author!.operations).toEqual([draft]);
  view.rerender(<Host />);
  act(() => screen.getByRole("button", { name: "Save step" }).focus());
  fireEvent.keyDown(screen.getByRole("button", { name: "Save step" }), undoKey);
  expect(author!.operations).toEqual([]);
});

it("undoes from a focused canvas toolbar button and takes the focus back to the canvas on a click", async () => {
  await mounted();
  act(() => author!.stage(draft));
  const search = within(tools()).getByRole("button", { name: /Search canvas/ });
  act(() => search.focus());
  fireEvent.keyDown(search, undoKey);
  expect(author!.operations).toEqual([]);
  const name = screen.getByRole("textbox", { name: "Step name" });
  act(() => name.focus());
  const canvas = screen.getByRole("region", { name: "Workflow canvas" });
  fireEvent.pointerDown(canvas);
  expect(canvas).toHaveFocus();
  fireEvent.keyDown(canvas, redoKey);
  expect(author!.operations).toEqual([draft]);
});

it("undoes the latest action across the workflow and its hook, with nothing selected", async () => {
  await mounted({ hook: true });
  act(() => author!.stage(draft));
  act(() => child!.stage(setVariable));
  const status = screen.getByTestId("structure-status");
  expect(status).toHaveTextContent("Structure not applied: 2 change(s)");
  fireEvent.keyDown(document.body, undoKey);
  expect(child!.operations).toEqual([]);
  expect(author!.operations).toEqual([draft]);
  expect(status).toHaveTextContent("Structure not applied: 1 change(s)");
  fireEvent.keyDown(document.body, undoKey);
  expect(author!.operations).toEqual([]);
  expect(status).not.toHaveTextContent("not applied");
  // Redo walks forward in the same order: the workflow's action, then the hook's.
  fireEvent.keyDown(document.body, redoKey);
  expect(author!.operations).toEqual([draft]);
  expect(child!.operations).toEqual([]);
  fireEvent.keyDown(document.body, redoKey);
  expect(child!.operations).toEqual([setVariable]);
  expect(status).toHaveTextContent("Structure not applied: 2 change(s)");
});

it("adds no Undo or Redo button to the canvas toolbar", async () => {
  await mounted();
  expect(within(tools()).queryByRole("button", { name: /undo|redo/i })).not.toBeInTheDocument();
});
