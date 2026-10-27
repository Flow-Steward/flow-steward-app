import { act, render, renderHook, waitFor } from "@testing-library/react";
import { useLayoutEffect, useMemo, useRef, useState, type ComponentProps } from "react";
import { WorkflowCanvasBuilderSurface } from "./canvas";
import { useCanvasSearchNavigation } from "./canvas-surface-interaction-hooks";
import { placeCanvasNode } from "./canvas-placement";
import type { WorkflowCanvasShell } from "./canvas-shell";
import type { BuilderSurfaceProps } from "./canvas-types";
import { definitionFromWorkflow } from "./change-set";
import type { WorkflowCanvasGraph } from "../shared/api";
import { useWorkflowChangeSet } from "../detail/detail-change-set";
import type * as CanvasNotesModule from "./canvas-notes";

// Capture the real canvas's React Flow callbacks without simulating pointer geometry in jsdom.
// The existing canvas connection tests use the same mounted shell boundary.
const captured = vi.hoisted(() => ({
  shell: null as ComponentProps<typeof WorkflowCanvasShell> | null,
  saveNote: vi.fn(async () => undefined),
}));
vi.mock("./canvas-shell", () => ({
  WorkflowCanvasShell: (props: ComponentProps<typeof WorkflowCanvasShell>) => {
    captured.shell = props;
    return <div aria-label="Mounted canvas" />;
  },
}));
vi.mock("./canvas-notes", async (importOriginal) => ({
  ...await importOriginal<typeof CanvasNotesModule>(),
  useCanvasNotes: () => ({
    notes: [], busy: false, disabled: false, error: "", selectedId: null, draft: null, add: vi.fn(), nodeActions: {}, removeFromSelection: (ids: readonly string[]) => [...ids],
    onNodesChange: vi.fn(), save: captured.saveNote, reload: vi.fn(),
  }),
}));

const graph = (workflowId: string): WorkflowCanvasGraph => ({
  workflow: { id: workflowId, display_name: "Position history", enabled: false, callable_by_parent: false },
  interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true },
  output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
  entry: { kind: "step", id: "one" }, groups: [], for_each: [], edges: [], automation_rules: [], validation_issues: [],
  steps: [
    { id: "one", step_id: "one", kind: "set_variable", display_name: "One", config_summary: "state.label", ports: [{ outcome: { name: "success" }, label: "Next" }] },
    { id: "two", step_id: "two", kind: "stop_success", display_name: "Two", config_summary: "", ports: [] },
  ],
});

type ChangeSet = ReturnType<typeof useWorkflowChangeSet>;
let currentChangeSet: ChangeSet | null = null;
const shell = () => captured.shell!;
const position = (id: string) => ({ ...shell().nodesWithCanvasState.find((node) => node.id === id)!.position });
const historyCommand = (name: "undoGraphIntent" | "redoGraphIntent") =>
  (currentChangeSet as unknown as Record<typeof name, (() => void) | undefined>)[name];

function Host({ workflowId = "position_a", revision = 7, parameter = "before" }: {
  workflowId?: string;
  revision?: number;
  parameter?: string;
}) {
  const definition = useMemo(() => definitionFromWorkflow({
    workflow_id: workflowId, revision, groups: [],
    steps: [
      { step_id: "one", step_kind: "set_variable", display_name: "One", override: { set_variable: { assignments: { "state.label": parameter } } } },
      { step_id: "two", step_kind: "stop_success", display_name: "Two" },
    ],
    settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "one" }, edges: [] } },
  }), [workflowId, revision, parameter]);
  const changeSet = useWorkflowChangeSet({
    workflowId, serverDefinition: definition, serverRevision: revision,
    refetchWorkflowSurfaces: async () => ({ workflow_id: workflowId, revision }),
  });
  useLayoutEffect(() => { currentChangeSet = changeSet; }, [changeSet]);
  const canvasGraph = useMemo(() => graph(workflowId), [workflowId]);
  const props = {
    advancedMode: true, workflowId, canvasGraph, changeSet,
    onCloseInspector: vi.fn(), onConnectPort: vi.fn(async () => true), onDisconnectPort: vi.fn(),
    onInsertStep: vi.fn(), onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(),
    onOpenNewGroup: vi.fn(), onOpenNewStep: vi.fn(), onOpenStep: vi.fn(),
    onOpenStepAutomation: vi.fn(), onOpenWorkflowAutomation: vi.fn(),
    onOpenWorkflowSettings: vi.fn(), onValidationIssue: vi.fn(), onToggleAdvancedMode: vi.fn(),
  } as BuilderSurfaceProps;
  return <WorkflowCanvasBuilderSurface {...props} />;
}

beforeEach(() => {
  localStorage.clear();
  captured.shell = null;
  captured.saveNote.mockClear();
  currentChangeSet = null;
});
afterEach(() => { localStorage.clear(); });

it("records a completed single XY drag once, not every move frame or a later note save, and retains newer saved parameters", () => {
  const view = render(<Host />);
  const before = position("one");
  const otherBefore = position("two");
  const node = shell().nodesWithCanvasState.find((item) => item.id === "one")!;
  act(() => shell().handleNodesChange([{ type: "position", id: "one", position: { x: before.x + 16, y: before.y + 160 }, dragging: true }]));
  act(() => shell().handleNodesChange([{ type: "position", id: "one", position: { x: before.x + 32, y: before.y + 320 }, dragging: true }]));
  act(() => shell().handleNodeDragStop({ ...node, position: { x: before.x + 48, y: before.y + 480 } }));
  const final = position("one");
  expect(final).not.toEqual(before);
  expect(currentChangeSet!.operations).toEqual([]); // XY never becomes a server graph operation.

  const note = { id: "note", workflow_id: "position_a", text: "annotation", x: 10, y: 20, color: "yellow", width: 240, height: 120 };
  act(() => shell().handleNodeDragStop({ id: "annotation:note", type: "canvasNote", position: { x: 33, y: 47 }, data: { note } }));
  expect(captured.saveNote).toHaveBeenCalledExactlyOnceWith({ ...note, x: 33, y: 47 });
  view.rerender(<Host revision={8} parameter="saved after drag" />);
  expect(currentChangeSet!.preview.steps.find((step) => step.step_id === "one")).toMatchObject({
    override: { set_variable: { assignments: { "state.label": "saved after drag" } } },
  });
  act(() => historyCommand("undoGraphIntent")?.());
  expect(position("one")).toEqual(before); // One Undo covers the whole gesture, not its last frame or note save.
  expect(position("two")).toEqual(otherBefore);
  expect(currentChangeSet!.preview.steps.find((step) => step.step_id === "one")).toMatchObject({
    override: { set_variable: { assignments: { "state.label": "saved after drag" } } },
  });
  act(() => historyCommand("redoGraphIntent")?.());
  expect(position("one")).toEqual(final);
  expect(currentChangeSet!.operations).toEqual([]);
});

it("treats a selected two-card XY drag as one placement intent", () => {
  render(<Host />);
  const beforeOne = position("one");
  const beforeTwo = position("two");
  const dragged = shell().nodesWithCanvasState.find((item) => item.id === "one")!;
  act(() => shell().handleNodesChange([
    { type: "select", id: "one", selected: true },
    { type: "select", id: "two", selected: true },
  ]));
  act(() => shell().handleNodesChange([
    { type: "position", id: "one", position: { x: beforeOne.x + 32, y: beforeOne.y + 320 }, dragging: true },
    { type: "position", id: "two", position: { x: beforeTwo.x + 32, y: beforeTwo.y + 320 }, dragging: true },
  ]));
  act(() => shell().handleNodeDragStop({ ...dragged, position: { x: beforeOne.x + 32, y: beforeOne.y + 320 } }));
  const finalOne = position("one");
  const finalTwo = position("two");
  expect(finalOne).not.toEqual(beforeOne);
  expect(finalTwo).not.toEqual(beforeTwo);
  act(() => historyCommand("undoGraphIntent")?.());
  expect(position("one")).toEqual(beforeOne);
  expect(position("two")).toEqual(beforeTwo);
  act(() => historyCommand("redoGraphIntent")?.());
  expect(position("one")).toEqual(finalOne);
  expect(position("two")).toEqual(finalTwo);
  expect(currentChangeSet!.operations).toEqual([]);
});

it("does not replay a prior workflow's XY intent after the owning workflow changes", async () => {
  const view = render(<Host workflowId="position_a" />);
  const first = position("one");
  const dragged = shell().nodesWithCanvasState.find((item) => item.id === "one")!;
  act(() => shell().handleNodeDragStop({ ...dragged, position: { x: first.x + 32, y: first.y + 320 } }));
  expect(position("one")).not.toEqual(first);
  view.rerender(<Host workflowId="position_b" />);
  await waitFor(() => expect(position("one")).toEqual(first));
  const second = position("one");
  act(() => historyCommand("undoGraphIntent")?.());
  expect(position("one")).toEqual(second);
  act(() => historyCommand("redoGraphIntent")?.());
  expect(position("one")).toEqual(second);
  expect(currentChangeSet!.operations).toEqual([]);
});


it("retires an old Hook placement intent when two frames reuse the same child workflow", () => {
  const childDefinition = definitionFromWorkflow({
    workflow_id: "shared_child", revision: 3, groups: [],
    steps: [{ step_id: "later", step_kind: "set_variable", display_name: "Later" }],
  });
  const view = renderHook(({ hookId }) => useWorkflowChangeSet({
    workflowId: "shared_child", placementViewKey: hookId, serverDefinition: childDefinition, serverRevision: 3,
    refetchWorkflowSurfaces: async () => ({ workflow_id: "shared_child", revision: 3 }),
  }), { initialProps: { hookId: "before_finish" } });
  act(() => view.result.current.recordPlacementIntent({
    canvasWorkflowId: "author", subjectIds: ["hook:before_finish:later"], canonicalStepSubjectIds: ["later"],
    before: { "hook:before_finish:later": null }, after: { "hook:before_finish:later": { x: 64, y: 96 } },
  }));
  expect(view.result.current.hasChanges).toBe(false);
  expect(view.result.current.canUndo).toBe(true);
  view.rerender({ hookId: "after_finish" }); // Same child workflow id, different visual Hook owner.
  expect(view.result.current.canUndo).toBe(false);
  expect(view.result.current.placementReplay).toBeNull();
  act(() => view.result.current.undoGraphIntent());
  expect(view.result.current.placementReplay).toBeNull();
  view.rerender({ hookId: "before_finish" });
  expect(view.result.current.canUndo).toBe(false);
  expect(view.result.current.placementReplay).toBeNull();
  // Also retire a one-shot token emitted in A before its canvas could consume it.
  act(() => view.result.current.recordPlacementIntent({
    canvasWorkflowId: "author", subjectIds: ["hook:before_finish:later"], canonicalStepSubjectIds: ["later"],
    before: { "hook:before_finish:later": null }, after: { "hook:before_finish:later": { x: 96, y: 128 } },
  }));
  act(() => view.result.current.undoGraphIntent());
  expect(view.result.current.placementReplay).toMatchObject({ viewKey: "before_finish", ownerWorkflowId: "shared_child" });
  view.rerender({ hookId: "after_finish" });
  expect(view.result.current.placementReplay).toBeNull();
  view.rerender({ hookId: "before_finish" });
  expect(view.result.current.placementReplay).toBeNull();
  expect(view.result.current.operations).toEqual([]);
});

it("drops a created child Step's displayed Hook placement with its canonical child id", () => {
  const childDefinition = definitionFromWorkflow({ workflow_id: "shared_child", revision: 3, groups: [], steps: [] });
  const view = renderHook(() => useWorkflowChangeSet({
    workflowId: "shared_child", placementViewKey: "before_finish", serverDefinition: childDefinition, serverRevision: 3,
    refetchWorkflowSurfaces: async () => ({ workflow_id: "shared_child", revision: 3 }),
  }));
  act(() => view.result.current.stage({ op: "create_step", step: { step_id: "later", step_kind: "set_variable", display_name: "Later" } }));
  act(() => view.result.current.recordPlacementIntent({
    canvasWorkflowId: "author", subjectIds: ["hook:before_finish:later"], canonicalStepSubjectIds: ["later"],
    before: { "hook:before_finish:later": null }, after: { "hook:before_finish:later": { x: 64, y: 96 } },
  }));
  expect(view.result.current.canUndo).toBe(true);
  act(() => view.result.current.dropStagedStep("later"));
  expect(view.result.current.operations).toEqual([]);
  expect(view.result.current.canUndo).toBe(false);
  expect(view.result.current.placementReplay).toBeNull();
});


it("lets an in-flight same-workflow Apply finish across a visual Hook view change", async () => {
  const childDefinition = definitionFromWorkflow({ workflow_id: "shared_child", revision: 3, groups: [], steps: [] });
  const created = { op: "create_step" as const, step: { step_id: "saved", step_kind: "set_variable", display_name: "Saved" } };
  const ordinary = { op: "create_step" as const, step: { step_id: "ordinary", step_kind: "set_variable", display_name: "Ordinary" } };
  let releaseWrite: ((response: Response) => void) | undefined;
  const heldWrite = new Promise<Response>((resolve) => { releaseWrite = resolve; });
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (!String(input).endsWith("/api/workflows/shared_child/changes")) throw new Error("Unexpected request");
    const body = JSON.parse(String(init?.body || "{}")) as { dry_run?: boolean };
    return body.dry_run
      ? new Response(JSON.stringify({ workflow_id: "shared_child", revision: 3, valid: true, applied: false, issues: [], blocking_issues: [], workflow: {} }), { status: 200 })
      : heldWrite;
  });
  try {
    const view = renderHook(({ hookId }) => useWorkflowChangeSet({
      workflowId: "shared_child", placementViewKey: hookId, serverDefinition: childDefinition, serverRevision: 3,
      refetchWorkflowSurfaces: async () => ({ workflow_id: "shared_child", revision: 4 }),
    }), { initialProps: { hookId: "before_finish" } });
    let token: ReturnType<typeof view.result.current.stageGraphIntent> = null;
    await act(() => { token = view.result.current.stageGraphIntent([created]); });
    act(() => view.result.current.stage(ordinary));
    let applying: Promise<boolean> | undefined;
    act(() => { applying = view.result.current.apply(); });
    expect(view.result.current.status).toBe("applying");
    let attached = true;
    act(() => { attached = view.result.current.attachPlacementToCreation(token!, { kind: "step", id: "saved" }, {
      canvasWorkflowId: "author", subjectIds: ["hook:before_finish:saved"], canonicalStepSubjectIds: ["saved"],
      before: { "hook:before_finish:saved": null }, after: { "hook:before_finish:saved": { x: 64, y: 576 } },
    }, view.result.current.placementSubjectGeneration("hook:before_finish:saved")); });
    expect(attached).toBe(false);
    view.rerender({ hookId: "after_finish" }); // Same workflow, changed visual projection only.
    expect(view.result.current.operations).toEqual([created, ordinary]);
    await act(async () => {
      releaseWrite?.(new Response(JSON.stringify({ workflow_id: "shared_child", revision: 4, valid: true, issues: [], workflow: {} }), { status: 200 }));
      expect(await applying).toBe(true);
    });
    expect(view.result.current.status).toBe("clean");
    expect(view.result.current.operations).toEqual([]);
    // The history survives Apply (Canvas › Undo): Undo would stage the reverse.
    expect(view.result.current.canUndo).toBe(true);
  } finally {
    fetchSpy.mockRestore();
  }
});

it("rejects callbacks captured in Hook A after an A→B→A view cycle while fresh A can Undo", () => {
  const childDefinition = definitionFromWorkflow({ workflow_id: "shared_child", revision: 3, groups: [], steps: [] });
  const view = renderHook(({ hookId }) => useWorkflowChangeSet({
    workflowId: "shared_child", placementViewKey: hookId, serverDefinition: childDefinition, serverRevision: 3,
    refetchWorkflowSurfaces: async () => ({ workflow_id: "shared_child", revision: 3 }),
  }), { initialProps: { hookId: "before_finish" } });
  const oldRecord = view.result.current.recordPlacementIntent;
  const placement = { canvasWorkflowId: "author", subjectIds: ["hook:before_finish:later"], canonicalStepSubjectIds: ["later"],
    before: { "hook:before_finish:later": null }, after: { "hook:before_finish:later": { x: 64, y: 96 } } };
  act(() => view.result.current.recordPlacementIntent(placement));
  expect(view.result.current.canUndo).toBe(true);
  const oldUndo = view.result.current.undoGraphIntent; // Captured with a real, undoable A intent.
  view.rerender({ hookId: "after_finish" });
  view.rerender({ hookId: "before_finish" });
  act(() => { oldRecord(placement); oldUndo(); });
  expect(view.result.current.canUndo).toBe(false);
  expect(view.result.current.placementReplay).toBeNull();
  act(() => view.result.current.recordPlacementIntent(placement));
  expect(view.result.current.canUndo).toBe(true);
  act(() => oldUndo());
  expect(view.result.current.canUndo).toBe(true);
  act(() => view.result.current.undoGraphIntent());
  expect(view.result.current.canUndo).toBe(false);
  expect(view.result.current.placementReplay).toMatchObject({ viewKey: "before_finish", ownerWorkflowId: "shared_child" });
});


it("rejects callbacks captured in workflow A after A→B→A with an unchanged view key", () => {
  const definitions = {
    owner_a: definitionFromWorkflow({ workflow_id: "owner_a", revision: 3, groups: [], steps: [] }),
    owner_b: definitionFromWorkflow({ workflow_id: "owner_b", revision: 3, groups: [], steps: [] }),
  };
  const view = renderHook(({ workflowId }) => useWorkflowChangeSet({
    workflowId, placementViewKey: "", serverDefinition: definitions[workflowId], serverRevision: 3,
    refetchWorkflowSurfaces: async () => ({ workflow_id: workflowId, revision: 3 }),
  }), { initialProps: { workflowId: "owner_a" as "owner_a" | "owner_b" } });
  const placement = { canvasWorkflowId: "owner_a", subjectIds: ["one"], canonicalStepSubjectIds: ["one"],
    before: { one: null }, after: { one: { x: 48, y: 96 } } };
  const oldRecord = view.result.current.recordPlacementIntent;
  act(() => view.result.current.recordPlacementIntent(placement));
  expect(view.result.current.canUndo).toBe(true);
  const oldUndo = view.result.current.undoGraphIntent; // Has an actual A intent to reverse.
  view.rerender({ workflowId: "owner_b" });
  expect(view.result.current.canUndo).toBe(false);
  view.rerender({ workflowId: "owner_a" });
  expect(view.result.current.canUndo).toBe(false);
  act(() => { oldRecord(placement); oldUndo(); });
  expect(view.result.current.canUndo).toBe(false);
  expect(view.result.current.placementReplay).toBeNull();
  act(() => view.result.current.recordPlacementIntent(placement));
  expect(view.result.current.canUndo).toBe(true);
  act(() => oldUndo());
  expect(view.result.current.canUndo).toBe(true);
  act(() => view.result.current.undoGraphIntent());
  expect(view.result.current.canUndo).toBe(false);
  expect(view.result.current.placementReplay).toMatchObject({ ownerWorkflowId: "owner_a", viewKey: "" });
});


it("publishes a confirmed reused-ID point after another card was selected, without stealing its focus", () => {
  const token = { controller: Symbol("creation"), serial: 7 };
  const center = vi.fn();
  const attach = vi.fn(() => true);
  const source = { id: "route_priority", type: "workflowStep", position: { x: 768, y: 64 }, width: 280, height: 160, data: {} };
  const high = { id: "set_variable", type: "workflowStep", position: { x: 1136, y: 112 }, width: 280, height: 120, data: {} };
  const target = { id: "set_variable_3", type: "workflowStep", position: { x: 1136, y: -32 }, width: 280, height: 120, data: {} };
  const owner = { workflowId: "author", placementViewKey: "", placementViewEpoch: 0,
    hasGraphIntent: (candidate: unknown) => candidate === token, placementSubjectGeneration: () => 0, attachPlacementToCreation: attach };
  const selection = { current: { ids: ["route_priority"], generation: 4 } };
  const evidence = { nodeId: "set_variable_3", nonce: 1, target: { kind: "step" as const, id: "set_variable_3" }, token,
    ownerWorkflowId: "author", viewKey: "", viewEpoch: 0, point: { x: 1136, y: 576 }, selectionGeneration: 4,
    intent: { source: { kind: "step" as const, id: "route_priority" }, outcome: { name: "default" as const }, scope: { groupId: "", forEachId: "", laneId: "" } } };
  const edge = { id: "default-edge", source: "route_priority", target: "set_variable_3", sourceHandle: "default" };
  const view = renderHook(({ published, focused, nonce, creation }: { published: boolean; focused: string; nonce: number; creation: typeof evidence | null }) => {
    const [manual, setManual] = useState({ route_priority: { x: 768, y: 64 }, set_variable: { x: 1136, y: 112 }, set_variable_3: { x: 1136, y: -32 } });
    const handled = useRef(0);
    const nodes = published ? [source, high, target] : [source, high];
    useCanvasSearchNavigation({
      autoArrange: false, currentNodesById: new Map(nodes.map((node) => [node.id, node])) as never,
      nodes: nodes as never, edges: published ? [edge] as never : [],
      reactFlowInstance: { getZoom: () => 1, setCenter: center, screenToFlowPosition: (point: { x: number; y: number }) => point } as never,
      setHighlightedNodeId: vi.fn(), setShowSearchResults: vi.fn(), openGroupFromCanvas: vi.fn(), openStepFromCanvas: vi.fn(),
      focusNodeId: focused, focusNodeNonce: nonce, focusNodeCreation: creation, focusNodePlace: false,
      handledFocusNonceRef: handled, canvasMeasureRootRef: { current: null }, manualPositions: manual,
      workflowId: "author", placementGestureGenerationFor: () => 0, placementOwnerFor: () => owner as never, currentSelection: () => selection.current,
      setManualPositions: setManual as never, setCreationIntent: vi.fn(), searchIndex: 0, searchResults: [], setSearchIndex: vi.fn(),
    });
    return { manual, setManual };
  }, { initialProps: { published: false, focused: "set_variable_3", nonce: 1, creation: evidence as typeof evidence | null } });
  selection.current = { ids: ["set_variable"], generation: 5 }; // The author selects High before the new card is published.
  view.rerender({ published: true, focused: "set_variable_3", nonce: 1, creation: evidence });
  expect(view.result.current.manual.set_variable_3).toEqual({ x: 1136, y: 576 });
  expect(view.result.current.manual.set_variable).toEqual({ x: 1136, y: 112 });
  expect(attach).toHaveBeenCalledExactlyOnceWith(token, evidence.target, expect.objectContaining({
    before: { set_variable_3: { x: 1136, y: -32 } }, after: { set_variable_3: { x: 1136, y: 576 } },
  }), 0);
  expect(center).not.toHaveBeenCalled(); // Geometry publishes, but old autofocus cannot pull the view away from High.
  view.rerender({ published: true, focused: "set_variable", nonce: 2, creation: null });
  expect(view.result.current.manual.set_variable).toEqual({ x: 1136, y: 112 });
  expect(attach).toHaveBeenCalledTimes(1);
});

it("does not overwrite a newer drag while a confirmed creation waits for graph publication", () => {
  const token = { controller: Symbol("creation"), serial: 8 };
  const attach = vi.fn(() => true);
  const target = { id: "new", type: "workflowStep", position: { x: 0, y: 0 }, width: 280, height: 120, data: {} };
  const evidence = { nodeId: "new", nonce: 1, target: { kind: "step" as const, id: "new" }, token,
    ownerWorkflowId: "author", viewKey: "", viewEpoch: 0, point: { x: 600, y: 576 } };
  const view = renderHook(({ published }) => {
    const [manual, setManual] = useState({ new: { x: 100, y: -32 } });
    const handled = useRef(0);
    useCanvasSearchNavigation({
      autoArrange: false, currentNodesById: new Map(published ? [["new", target]] : []) as never,
      nodes: published ? [target] as never : [], edges: [],
      reactFlowInstance: { getZoom: () => 1, setCenter: vi.fn(), screenToFlowPosition: (point: { x: number; y: number }) => point } as never,
      setHighlightedNodeId: vi.fn(), setShowSearchResults: vi.fn(), openGroupFromCanvas: vi.fn(), openStepFromCanvas: vi.fn(),
      focusNodeId: "new", focusNodeNonce: 1, focusNodeCreation: evidence, focusNodePlace: false,
      handledFocusNonceRef: handled, canvasMeasureRootRef: { current: null }, manualPositions: manual,
      workflowId: "author", placementGestureGenerationFor: () => 0, placementOwnerFor: () => ({ workflowId: "author", placementViewKey: "", placementViewEpoch: 0,
        hasGraphIntent: () => true, placementSubjectGeneration: () => 0, attachPlacementToCreation: attach }) as never,
      currentSelection: () => ({ ids: [], generation: 0 }), setManualPositions: setManual as never,
      setCreationIntent: vi.fn(), searchIndex: 0, searchResults: [], setSearchIndex: vi.fn(),
    });
    return { manual, setManual };
  }, { initialProps: { published: false } });
  act(() => view.result.current.setManual((current) => ({ ...current, new: { x: 700, y: 700 } })));
  view.rerender({ published: true });
  expect(view.result.current.manual.new).toEqual({ x: 700, y: 700 });
  expect(attach).not.toHaveBeenCalled();
});


it("retired Hook A creation callback cannot attach XY after A→B→A", () => {
  const definition = definitionFromWorkflow({ workflow_id: "shared_child", revision: 3, groups: [], steps: [] });
  const view = renderHook(({ hookId }) => useWorkflowChangeSet({
    workflowId: "shared_child", placementViewKey: hookId, serverDefinition: definition, serverRevision: 3,
    refetchWorkflowSurfaces: async () => ({ workflow_id: "shared_child", revision: 3 }),
  }), { initialProps: { hookId: "before_finish" } });
  let token: ReturnType<typeof view.result.current.stageGraphIntent> = null;
  act(() => { token = view.result.current.stageGraphIntent([{ op: "create_step", step: { step_id: "later", step_kind: "set_variable" } }]); });
  expect(token).not.toBeNull();
  const oldAttach = view.result.current.attachPlacementToCreation;
  const oldEpoch = view.result.current.placementViewEpoch;
  const placement = { canvasWorkflowId: "author", subjectIds: ["hook:before_finish:later"], canonicalStepSubjectIds: ["later"],
    before: { "hook:before_finish:later": null }, after: { "hook:before_finish:later": { x: 64, y: 576 } } };
  view.rerender({ hookId: "after_finish" });
  view.rerender({ hookId: "before_finish" });
  expect(view.result.current.placementViewEpoch).toBeGreaterThan(oldEpoch);
  let accepted = true;
  act(() => { accepted = oldAttach(token!, { kind: "step", id: "later" }, placement, 0); });
  expect(accepted).toBe(false);
  expect(view.result.current.placementReplay).toBeNull();
});


it("retires a delayed reused-ID claim after a later real-owner move and Undo back to the same XY", () => {
  const definition = definitionFromWorkflow({ workflow_id: "author", revision: 3, groups: [], steps: [] });
  const view = renderHook(() => useWorkflowChangeSet({
    workflowId: "author", serverDefinition: definition, serverRevision: 3,
    refetchWorkflowSurfaces: async () => ({ workflow_id: "author", revision: 3 }),
  }));
  let token: ReturnType<typeof view.result.current.stageGraphIntent> = null;
  act(() => { token = view.result.current.stageGraphIntent([{ op: "create_step", step: { step_id: "reused", step_kind: "set_variable" } }]); });
  expect(token).not.toBeNull();
  const baseline = view.result.current.placementSubjectGeneration("reused");
  act(() => view.result.current.recordPlacementIntent({
    canvasWorkflowId: "author", subjectIds: ["reused"], canonicalStepSubjectIds: ["reused"],
    before: { reused: { x: 1136, y: -32 } }, after: { reused: { x: 1400, y: 600 } },
  }));
  act(() => view.result.current.undoGraphIntent());
  expect(view.result.current.placementSubjectGeneration("reused")).toBeGreaterThan(baseline);
  const claim = { canvasWorkflowId: "author", subjectIds: ["reused"], canonicalStepSubjectIds: ["reused"],
    before: { reused: { x: 1136, y: -32 } }, after: { reused: { x: 1136, y: 576 } } };
  let accepted = true;
  act(() => { accepted = view.result.current.attachPlacementToCreation(token!, { kind: "step", id: "reused" }, claim, baseline); });
  expect(accepted).toBe(false);
  expect(view.result.current.journal.items[0].placement).toBeUndefined();
});

it("accepts only the projected Hook subject for one creation and refuses a stale workflow owner", () => {
  const definitions = {
    author: definitionFromWorkflow({ workflow_id: "author", revision: 3, groups: [], steps: [] }),
    foreign: definitionFromWorkflow({ workflow_id: "foreign", revision: 3, groups: [], steps: [] }),
  };
  const view = renderHook(() => useWorkflowChangeSet({
    workflowId: "author", placementViewKey: "before", serverDefinition: definitions.author, serverRevision: 3,
    refetchWorkflowSurfaces: async () => ({ workflow_id: "author", revision: 3 }),
  }));
  let hookToken: ReturnType<typeof view.result.current.stageGraphIntent> = null;
  act(() => { hookToken = view.result.current.stageGraphIntent([{ op: "create_step", step: { step_id: "later", step_kind: "set_variable" } }]); });
  const delta = { canvasWorkflowId: "author", subjectIds: ["hook:before:later"], canonicalStepSubjectIds: ["later"],
    before: { "hook:before:later": null }, after: { "hook:before:later": { x: 64, y: 576 } } };
  const baseline = view.result.current.placementSubjectGeneration("hook:before:later");
  let refused = true;
  act(() => { refused = view.result.current.attachPlacementToCreation(hookToken!, { kind: "step", id: "later" },
    { ...delta, subjectIds: ["later"], before: { later: null }, after: { later: { x: 64, y: 576 } } }, baseline); });
  expect(refused).toBe(false);
  let accepted = false;
  act(() => { accepted = view.result.current.attachPlacementToCreation(hookToken!, { kind: "step", id: "later" }, delta, baseline); });
  expect(accepted).toBe(true);
  expect(view.result.current.journal.items[0].placement).toMatchObject({ subjectIds: ["hook:before:later"], viewKey: "before" });

  const utils = renderHook(({ workflowId }) => useWorkflowChangeSet({
    workflowId, serverDefinition: definitions[workflowId], serverRevision: 3,
    refetchWorkflowSurfaces: async () => ({ workflow_id: workflowId, revision: 3 }),
  }), { initialProps: { workflowId: "author" as "author" | "foreign" } });
  let token: ReturnType<typeof utils.result.current.stageGraphIntent> = null;
  act(() => { token = utils.result.current.stageGraphIntent([{ op: "create_step", step: { step_id: "later", step_kind: "set_variable" } }]); });
  const oldAttach = utils.result.current.attachPlacementToCreation;
  utils.rerender({ workflowId: "foreign" });
  utils.rerender({ workflowId: "author" });
  act(() => { refused = oldAttach(token!, { kind: "step", id: "later" },
    { canvasWorkflowId: "author", subjectIds: ["later"], canonicalStepSubjectIds: ["later"], before: { later: null }, after: { later: { x: 64, y: 576 } } }, 0); });
  expect(refused).toBe(false);
});


it("refuses creation XY while conflicted and after its token is undone", () => {
  const definition = definitionFromWorkflow({ workflow_id: "author", revision: 3, groups: [], steps: [] });
  const view = renderHook(() => useWorkflowChangeSet({
    workflowId: "author", serverDefinition: definition, serverRevision: 3,
    refetchWorkflowSurfaces: async () => ({ workflow_id: "author", revision: 3 }),
  }));
  let token: ReturnType<typeof view.result.current.stageGraphIntent> = null;
  act(() => { token = view.result.current.stageGraphIntent([{ op: "create_step", step: { step_id: "new", step_kind: "set_variable" } }]); });
  const placement = { canvasWorkflowId: "author", subjectIds: ["new"], canonicalStepSubjectIds: ["new"],
    before: { new: null }, after: { new: { x: 608, y: 576 } } };
  const generation = view.result.current.placementSubjectGeneration("new");
  let accepted = true;
  act(() => view.result.current.reportConflict());
  act(() => { accepted = view.result.current.attachPlacementToCreation(token!, { kind: "step", id: "new" }, placement, generation); });
  expect(accepted).toBe(false);
  act(() => view.result.current.discard());
  act(() => { accepted = view.result.current.attachPlacementToCreation(token!, { kind: "step", id: "new" }, placement, generation); });
  expect(accepted).toBe(false);
});


it("places a no-point creation right of its manual source instead of retaining a retired XY", () => {
  const token = { controller: Symbol("no-point"), serial: 1 };
  const source = { id: "source", type: "workflowStep", position: { x: 100, y: 100 }, width: 280, height: 120, data: {} };
  const target = { id: "target", type: "workflowStep", position: { x: 100, y: -32 }, width: 280, height: 120, data: {} };
  const attach = vi.fn(() => true);
  const view = renderHook(() => {
    const [manual, setManual] = useState({ source: { x: 100, y: 100 }, target: { x: 100, y: -32 } });
    const handled = useRef(0);
    useCanvasSearchNavigation({
      autoArrange: false, currentNodesById: new Map([["source", source], ["target", target]]) as never,
      nodes: [source, target] as never, edges: [{ id: "edge", source: "source", sourceHandle: "success", target: "target" }] as never,
      reactFlowInstance: { getZoom: () => 1, setCenter: vi.fn(), screenToFlowPosition: (point: { x: number; y: number }) => point } as never,
      setHighlightedNodeId: vi.fn(), setShowSearchResults: vi.fn(), openGroupFromCanvas: vi.fn(), openStepFromCanvas: vi.fn(),
      focusNodeId: "target", focusNodeNonce: 1, focusNodeCreation: { nodeId: "target", nonce: 1,
        target: { kind: "step", id: "target" }, token, ownerWorkflowId: "author", viewKey: "", viewEpoch: 0 },
      focusNodePlace: false, handledFocusNonceRef: handled, canvasMeasureRootRef: { current: null }, manualPositions: manual,
      workflowId: "author", placementGestureGenerationFor: () => 0, placementOwnerFor: () => ({ workflowId: "author", placementViewKey: "", placementViewEpoch: 0,
        hasGraphIntent: () => true, placementSubjectGeneration: () => 0, attachPlacementToCreation: attach }) as never,
      currentSelection: () => ({ ids: [], generation: 0 }), setManualPositions: setManual as never,
      setCreationIntent: vi.fn(), searchIndex: 0, searchResults: [], setSearchIndex: vi.fn(),
    });
    return manual;
  });
  expect(view.result.current.target.x).toBeGreaterThan(source.position.x);
  expect(view.result.current.target).not.toEqual({ x: 100, y: -32 });
  expect(attach).toHaveBeenCalledTimes(1);
});

it("converts a root pointer to its parent-relative point and ignores an obstacle in another lane", () => {
  const token = { controller: Symbol("pointer"), serial: 1 };
  const parent = { id: "group:outer", type: "workflowGroup", position: { x: 400, y: 200 }, width: 900, height: 700, data: {} };
  const target = { id: "target", type: "workflowStep", parentId: "group:outer", position: { x: 208, y: -32 },
    width: 280, height: 120, data: { step: { lane_id: "a" } } };
  const otherLane = { id: "other", type: "workflowStep", parentId: "group:outer", position: { x: 208, y: 384 },
    width: 280, height: 120, data: { step: { lane_id: "b" } } };
  const nodes = [parent, target, otherLane];
  const expected = placeCanvasNode(target as never, { x: 208, y: 376 }, nodes as never);
  const attach = vi.fn(() => true);
  const view = renderHook(() => {
    const [manual, setManual] = useState({ target: { x: 208, y: -32 } });
    const handled = useRef(0);
    useCanvasSearchNavigation({
      autoArrange: false, currentNodesById: new Map(nodes.map((node) => [node.id, node])) as never,
      nodes: nodes as never, edges: [],
      reactFlowInstance: { getZoom: () => 1, setCenter: vi.fn(), screenToFlowPosition: (point: { x: number; y: number }) => point } as never,
      setHighlightedNodeId: vi.fn(), setShowSearchResults: vi.fn(), openGroupFromCanvas: vi.fn(), openStepFromCanvas: vi.fn(),
      focusNodeId: "target", focusNodeNonce: 1, focusNodeCreation: { nodeId: "target", nonce: 1,
        target: { kind: "step", id: "target" }, token, ownerWorkflowId: "author", viewKey: "", viewEpoch: 0, point: { x: 608, y: 576 } },
      focusNodePlace: false, handledFocusNonceRef: handled, canvasMeasureRootRef: { current: null }, manualPositions: manual,
      workflowId: "author", placementGestureGenerationFor: () => 0, placementOwnerFor: () => ({ workflowId: "author", placementViewKey: "", placementViewEpoch: 0,
        hasGraphIntent: () => true, placementSubjectGeneration: () => 0, attachPlacementToCreation: attach }) as never,
      currentSelection: () => ({ ids: [], generation: 0 }), setManualPositions: setManual as never,
      setCreationIntent: vi.fn(), searchIndex: 0, searchResults: [], setSearchIndex: vi.fn(),
    });
    return manual;
  });
  expect(view.result.current.target).toEqual(expected);
  expect(expected.x).toBe(208);
  expect(attach).toHaveBeenCalledTimes(1);
});
