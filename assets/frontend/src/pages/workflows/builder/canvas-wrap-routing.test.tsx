import { render, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import type { WorkflowCanvasShell } from "./canvas-shell";
import { WorkflowCanvasBuilderSurface } from "./canvas";
import { buildCanvasLayout } from "./layout";
import { manualLayoutStorageKey } from "./canvas-core";
import type { BuilderSurfaceProps } from "./canvas-types";
import type { WorkflowCanvasGraph, WorkflowGraphEdge, WorkflowCanvasStep } from "../shared/api";
import type * as CanvasNotesModule from "./canvas-notes";

// Capture the mounted canvas boundary, including its routed edges after layout
// and the manual-position overlay. The route helper itself is never mocked.
const captured = vi.hoisted(() => ({ shell: null as ComponentProps<typeof WorkflowCanvasShell> | null }));
vi.mock("./canvas-shell", () => ({ WorkflowCanvasShell: (props: ComponentProps<typeof WorkflowCanvasShell>) => {
  captured.shell = props;
  return <div aria-label="Mounted workflow canvas" />;
} }));
vi.mock("./canvas-notes", async (importOriginal) => ({ ...await importOriginal<typeof CanvasNotesModule>(), useCanvasNotes: () => ({
  notes: [], busy: false, disabled: false, error: "", selectedId: null, draft: null, add: vi.fn(), nodeActions: {}, removeFromSelection: (ids: readonly string[]) => [...ids],
  onNodesChange: vi.fn(), save: vi.fn(async () => undefined), reload: vi.fn(),
}) }));

const ids = Array.from({ length: 35 }, (_, index) => `node_${String(index + 1).padStart(3, "0")}`);
const next = (source: string, target: string, outcome: "success" | "error" | "default" = "success", rework = false): WorkflowGraphEdge => ({
  source: { kind: "step", id: source }, outcome: { name: outcome }, target: { kind: "step", id: target }, ...(rework ? { rework: true } : {}),
});
const step = (id: string): WorkflowCanvasStep => ({
  id, step_id: id, kind: "set_variable", display_name: id, config_summary: "",
  ports: [{ outcome: { name: "success" }, label: "Next" }, { outcome: { name: "error" }, label: "Error" }, { outcome: { name: "default" }, label: "Repeat" }],
});
function chain(parallel: boolean): WorkflowCanvasGraph {
  return {
    workflow: { id: "wrap_routing", display_name: "Wrap routing", enabled: false, callable_by_parent: false },
    interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true },
    output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
    entry: { kind: "step", id: ids[0] }, groups: [], for_each: [],
    steps: [...ids.map(step), { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] }],
    edges: [...ids, "done"].slice(1).flatMap((target, index) => {
      const source = ids[index];
      return parallel ? [next(source, target), next(source, target, "error"), next(source, target, "default", true)] : [next(source, target)];
    }),
    automation_rules: [], validation_issues: [],
  };
}
function withGroupAt(index: number): WorkflowCanvasGraph {
  const graph = chain(false);
  const replaced = ids[index];
  const group = { kind: "group" as const, id: "box" };
  graph.steps = [...graph.steps.filter((item) => item.id !== replaced), { ...step("inside"), group_id: "box" }];
  graph.groups = [{ id: "box", display_name: "Box", body: { kind: "sequence", entry: { kind: "step", id: "inside" } } }];
  graph.edges = graph.edges.map((edge) => ({
    ...edge,
    source: edge.source.kind === "step" && edge.source.id === replaced ? group : edge.source,
    target: edge.target.kind === "step" && edge.target.id === replaced ? group : edge.target,
  }));
  graph.edges.push({ source: { kind: "step", id: "inside" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "box", lane_id: null } });
  return graph;
}
const layout = (canvasGraph: WorkflowCanvasGraph) => buildCanvasLayout({
  canvasGraph, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn(),
});
const shell = () => captured.shell!;
const route = (source: string, target: string, outcome: string) => shell().visibleEdges.find((edge) =>
  edge.data?.edge.source.kind === (source.startsWith("group:") ? "group" : "step") &&
  edge.data.edge.source.id === source.replace(/^group:/, "") && edge.target === target && edge.data.edge.outcome.name === outcome,
)?.data?.route;
function mount(canvasGraph: WorkflowCanvasGraph) {
  const changeSet = { operations: [], status: "clean", hasChanges: false, applyIssues: [], blockingIssueMessages: [], keptIssueMessages: [], checkState: "idle", applyRefused: false, applyError: "", apply: vi.fn(async () => false), reload: vi.fn(async () => false), discard: vi.fn() };
  const props = { canvasGraph, advancedMode: true, workflowId: "wrap_routing", changeSet,
    onCloseInspector: vi.fn(), onConnectPort: vi.fn(async () => true), onDisconnectPort: vi.fn(), onInsertStep: vi.fn(),
    onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenNewGroup: vi.fn(), onOpenNewStep: vi.fn(),
    onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn(), onOpenWorkflowAutomation: vi.fn(),
    onOpenWorkflowSettings: vi.fn(), onValidationIssue: vi.fn(), onToggleAdvancedMode: vi.fn(),
  } as unknown as BuilderSurfaceProps;
  return render(<WorkflowCanvasBuilderSurface {...props} />);
}

beforeEach(() => { localStorage.clear(); captured.shell = null; });
afterEach(() => { localStorage.clear(); captured.shell = null; });

it("routes a normal cross-row root edge above while Repeat and failure stay below", () => {
  mount(chain(true));
  const byId = new Map(shell().nodesWithCanvasState.map((node) => [node.id, node]));
  const crossing = shell().visibleEdges.find((edge) => {
    if (edge.data?.edge.outcome.name !== "success" || edge.data.edge.rework) return false;
    const source = byId.get(edge.source); const target = byId.get(edge.target);
    return source && target && !source.parentId && !target.parentId && target.position.y > source.position.y && target.position.x <= source.position.x;
  });
  expect(crossing).toBeDefined(); // Real Wrap row transition, never an invented route-input flag.
  const source = crossing!.source; const target = crossing!.target;
  expect(route(source, target, "success")?.kind).toBe("upper-band");
  expect(route(source, target, "error")?.kind).toBe("lower-band");
  expect(route(source, target, "default")?.kind).toBe("lower-band");
});

it("drops the automatic row hint when a manually placed child reconciles its root Group", async () => {
  // Choose a real wrapped boundary with a Group target through the public layout,
  // then exercise the same graph through the mounted Canvas routing adapter.
  const graph = ids.slice(1, -1).map((_, offset) => withGroupAt(offset + 1)).find((candidate) => {
    const result = layout(candidate);
    const group = result.nodes.find((node) => node.id === "group:box");
    const incoming = result.edges.find((edge) => edge.target === "group:box" && edge.data?.edge.outcome.name === "success");
    const source = result.nodes.find((node) => node.id === incoming?.source);
    return group && source && group.position.y > source.position.y && group.position.x <= source.position.x;
  });
  expect(graph).toBeDefined();
  const automatic = layout(graph!);
  const child = automatic.nodes.find((node) => node.id === "inside")!;
  const incoming = automatic.edges.find((edge) => edge.target === "group:box" && edge.data?.edge.outcome.name === "success")!;
  const view = mount(graph!);
  expect(route(incoming.source, "group:box", "success")?.kind).toBe("upper-band");
  const automaticGroup = shell().nodesWithCanvasState.find((node) => node.id === "group:box")!;
  view.unmount();
  const manual = { inside: { x: -1000, y: child.position.y } }; // No root Group key.
  localStorage.setItem(manualLayoutStorageKey("wrap_routing"), JSON.stringify(manual));
  mount(graph!);
  await waitFor(() => expect(shell().nodesWithCanvasState.find((node) => node.id === "group:box")?.position.x).not.toBe(automaticGroup.position.x));
  expect(route(incoming.source, "group:box", "success")?.kind).toBe("lower-band");
  expect(JSON.parse(localStorage.getItem(manualLayoutStorageKey("wrap_routing")) || "{}")).toEqual(manual);
});


it("uses displayed geometry when the cross-row target root itself is placed by hand", async () => {
  const view = mount(chain(false));
  const byId = new Map(shell().nodesWithCanvasState.map((node) => [node.id, node]));
  const crossing = shell().visibleEdges.find((edge) => {
    if (edge.data?.edge.outcome.name !== "success") return false;
    const source = byId.get(edge.source); const target = byId.get(edge.target);
    return source && target && !source.parentId && !target.parentId &&
      target.position.y > source.position.y && target.position.x <= source.position.x;
  });
  expect(crossing).toBeDefined();
  const sourceId = crossing!.source; const targetId = crossing!.target;
  expect(route(sourceId, targetId, "success")?.kind).toBe("upper-band");
  const source = byId.get(sourceId)!; const target = byId.get(targetId)!;
  view.unmount();
  const manual = { [targetId]: { x: source.position.x - 160, y: target.position.y + 160 } };
  localStorage.setItem(manualLayoutStorageKey("wrap_routing"), JSON.stringify(manual));
  mount(chain(false));
  await waitFor(() => expect(shell().nodesWithCanvasState.find((node) => node.id === targetId)?.position).toEqual(manual[targetId]));
  expect(route(sourceId, targetId, "success")?.kind).toBe("lower-band");
  expect(JSON.parse(localStorage.getItem(manualLayoutStorageKey("wrap_routing")) || "{}")).toEqual(manual);
});

it("keeps an adjacent same-row normal edge on the existing curve route", () => {
  mount(chain(false));
  const byId = new Map(shell().nodesWithCanvasState.map((node) => [node.id, node]));
  const sameRow = shell().visibleEdges.find((edge) => {
    if (edge.data?.edge.outcome.name !== "success") return false;
    const source = byId.get(edge.source); const target = byId.get(edge.target);
    return source && target && !source.parentId && !target.parentId &&
      source.position.y === target.position.y && target.position.x > source.position.x;
  });
  expect(sameRow).toBeDefined();
  expect(route(sameRow!.source, sameRow!.target, "success")?.kind).toBe("curve");
});
