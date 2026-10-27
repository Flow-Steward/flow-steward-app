import { fireEvent, queryAllByAttribute, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import type { CanvasCreationSelection } from "./canvas-types";
import { MemoryRouter } from "react-router";
import { WorkflowCanvasBuilderSurface } from "./canvas";
import { manualLayoutStorageKey } from "./canvas-core";
import { definitionFromWorkflow } from "./change-set";
import { useWorkflowChangeSet } from "../detail/detail-change-set";
import type { WorkflowCanvasGraph } from "../shared/api";

// Explicit unit fixture: the real Surface, renderer, placement and local controller render.
// No catalog, persistence or native browser layout success is simulated.
const graph: WorkflowCanvasGraph = {
  workflow: { id: "slot-labels", display_name: "Slot labels", enabled: false, callable_by_parent: false },
  interface_contract: {}, output_contract: {}, entry: { kind: "step", id: "decide" },
  steps: [{ id: "decide", step_id: "decide", kind: "if", display_name: "Decide", config_summary: "", ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }] }],
  groups: [], edges: [], validation_issues: [], automation_rules: [],
};
const switchGraph: WorkflowCanvasGraph = {
  ...graph,
  entry: { kind: "step", id: "priority" },
  steps: [
    { id: "priority", step_id: "priority", kind: "switch", display_name: "Route priority", config_summary: "state.priority (2 cases)", ports: [
      { outcome: { name: "case", case_id: "high" }, label: "High" },
      { outcome: { name: "case", case_id: "low" }, label: "Low" },
      { outcome: { name: "default" }, label: "Default" },
      { outcome: { name: "error" }, label: "On error" },
    ] },
    { id: "occupied", step_id: "occupied", kind: "set_variable", display_name: "Occupied", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
  ],
};
type AppendStep = NonNullable<ComponentProps<typeof WorkflowCanvasBuilderSurface>["onAppendStep"]>;
const noop = () => undefined;
const refetch = async () => undefined;

function SlotCanvas({ activeGraph = graph, onAppendStep = noop, creationSelection }: { activeGraph?: WorkflowCanvasGraph; onAppendStep?: AppendStep; creationSelection?: CanvasCreationSelection }) {
  const definition = definitionFromWorkflow({ workflow_id: "slot-labels", steps: activeGraph.steps.map(({ step_id, display_name, kind }) => ({ step_id, display_name, step_kind: kind })), settings: { execution_graph: { schema_version: 1, entry: activeGraph.entry, edges: activeGraph.edges } } });
  const changeSet = useWorkflowChangeSet({ workflowId: "slot-labels", serverRevision: 4, serverDefinition: definition, refetchWorkflowSurfaces: refetch });
  return <WorkflowCanvasBuilderSurface advancedMode={false} creationSelection={creationSelection} canvasGraph={activeGraph} changeSet={changeSet} workflowId="slot-labels" onAppendStep={onAppendStep} onCloseInspector={noop} onConnectPort={() => false} onDisconnectPort={noop} onInsertStep={noop} onOpenGroup={noop} onOpenGroupAutomation={noop} onOpenNewGroup={noop} onOpenNewStep={noop} onOpenStep={noop} onOpenStepAutomation={noop} onOpenWorkflowAutomation={noop} onOpenWorkflowSettings={noop} onToggleAdvancedMode={noop} onValidationIssue={noop} />;
}

it("renders the actual True and False outcome labels visibly in the selected source's Add next slots", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  const bbox = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
  const hitTest = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
  Object.defineProperty(SVGElement.prototype, "getBBox", { configurable: true, value: () => ({ height: 16, width: 120, x: 0, y: 0 }) });
  try {
    render(<MemoryRouter><SlotCanvas /></MemoryRouter>);
    const source = await screen.findByRole("group", { name: "Open step Decide" });
    Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [source] });
    fireEvent.click(source);
    const [yes, no] = await Promise.all([screen.findByRole("button", { name: "True: add step" }), screen.findByRole("button", { name: "False: add step" })]);
    // Each branch slot names its own outcome in visible text: "True: add step" / "False: add step".
    expect([yes.textContent, no.textContent]).toEqual(["True: add step", "False: add step"]);
    expect(yes).toBeVisible();
    expect(no).toBeVisible();
  } finally {
    if (hitTest) Object.defineProperty(document, "elementsFromPoint", hitTest); else Reflect.deleteProperty(document, "elementsFromPoint");
    if (bbox) Object.defineProperty(SVGElement.prototype, "getBBox", bbox); else Reflect.deleteProperty(SVGElement.prototype, "getBBox");
    vi.unstubAllGlobals();
  }
});


it.each([
  { label: "High", outcome: { name: "case" as const, case_id: "high" }, occupied: undefined },
  { label: "Low", outcome: { name: "case" as const, case_id: "low" }, occupied: undefined },
  { label: "Default", outcome: { name: "default" as const }, occupied: undefined },
  { label: "Low", outcome: { name: "case" as const, case_id: "low" }, occupied: { name: "case" as const, case_id: "high" } },
  { label: "High", outcome: { name: "case" as const, case_id: "high" }, occupied: { name: "default" as const } },
])("forwards $label's exact Switch identity and slot point with occupied=$occupied", async ({ label, outcome, occupied }) => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  const bbox = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
  const hitTest = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
  Object.defineProperty(SVGElement.prototype, "getBBox", { configurable: true, value: () => ({ height: 16, width: 120, x: 0, y: 0 }) });
  const activeGraph: WorkflowCanvasGraph = { ...switchGraph, edges: occupied ? [{ source: { kind: "step", id: "priority" }, outcome: occupied, target: { kind: "step", id: "occupied" } }] : [] };
  const onAppendStep = vi.fn<AppendStep>();
  try {
    render(<MemoryRouter><SlotCanvas activeGraph={activeGraph} onAppendStep={onAppendStep} /></MemoryRouter>);
    const source = await screen.findByRole("group", { name: "Open step Route priority" });
    Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [source] });
    fireEvent.click(source);
    const slot = await screen.findByRole("button", { name: `${label}: add step` });
    for (const name of ["High", "Low", "Default", "On error"]) {
      const occupiedLabel = occupied?.name === "default" ? "Default" : occupied ? "High" : undefined;
      if (name === occupiedLabel) expect(screen.queryByRole("button", { name: `${name}: add step` })).not.toBeInTheDocument();
      else expect(screen.getByRole("button", { name: `${name}: add step` })).toBeVisible();
    }
    const point = { x: Number.parseFloat(slot.style.left), y: Number.parseFloat(slot.style.top) };
    expect(Number.isFinite(point.x) && Number.isFinite(point.y)).toBe(true);
    fireEvent.click(slot);
    expect(onAppendStep).toHaveBeenCalledExactlyOnceWith({ kind: "step", id: "priority" }, outcome, { source: { kind: "step", id: "priority" }, outcome, scope: { groupId: "", forEachId: "", laneId: "" }, point, selectionGeneration: 1 });
  } finally {
    if (hitTest) Object.defineProperty(document, "elementsFromPoint", hitTest); else Reflect.deleteProperty(document, "elementsFromPoint");
    if (bbox) Object.defineProperty(SVGElement.prototype, "getBBox", bbox); else Reflect.deleteProperty(SVGElement.prototype, "getBBox");
    vi.unstubAllGlobals();
  }
});


it.each([
  { occupied: ["high", "low"], append: "Default", outcome: { name: "default" as const } },
  { occupied: ["low"], append: "Default", outcome: { name: "default" as const } },
  { occupied: ["default"], append: "Low", outcome: { name: "case" as const, case_id: "low" } },
])("orders actual solid and ghost Switch curves with occupied=$occupied", async ({ occupied, append, outcome }) => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  const bbox = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
  const hitTest = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
  Object.defineProperty(SVGElement.prototype, "getBBox", { configurable: true, value: () => ({ height: 16, width: 120, x: 0, y: 0 }) });
  const key = manualLayoutStorageKey("slot-labels");
  const previous = localStorage.getItem(key);
  const anchors = [
    { key: "high", id: "set_variable", label: "High", y: 112 },
    { key: "low", id: "set_variable_2", label: "Low", y: 432 },
    { key: "default", id: "set_variable_3", label: "Default", y: 576 },
  ];
  const activeGraph: WorkflowCanvasGraph = {
    ...graph, entry: { kind: "step", id: "set_priority" },
    steps: [
      { id: "set_priority", step_id: "set_priority", kind: "set_variable", display_name: "Set priority", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { ...switchGraph.steps[0], id: "route_priority", step_id: "route_priority" },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
      ...anchors.filter((anchor) => occupied.includes(anchor.key)).map((anchor) => ({ id: anchor.id, step_id: anchor.id, kind: "set_variable", display_name: `Mark ${anchor.label}`, config_summary: "", ports: [{ outcome: { name: "success" as const }, label: "Next" }] })),
    ],
    edges: [
      { source: { kind: "step", id: "set_priority" }, outcome: { name: "success" }, target: { kind: "step", id: "route_priority" } },
      ...anchors.filter((anchor) => occupied.includes(anchor.key)).map((anchor) => ({ source: { kind: "step" as const, id: "route_priority" }, outcome: anchor.key === "default" ? { name: "default" as const } : { name: "case" as const, case_id: anchor.key }, target: { kind: "step" as const, id: anchor.id } })),
    ],
  };
  const manual = { set_priority: { x: 448, y: 64 }, route_priority: { x: 768, y: 64 }, done: { x: 1136, y: 256 }, ...Object.fromEntries(anchors.filter((anchor) => occupied.includes(anchor.key)).map((anchor) => [anchor.id, { x: 1136, y: anchor.y }])) };
  localStorage.setItem(key, JSON.stringify(manual));
  const onAppendStep = vi.fn<AppendStep>();
  try {
    render(<MemoryRouter><SlotCanvas activeGraph={activeGraph} onAppendStep={onAppendStep} /></MemoryRouter>);
    const source = await screen.findByRole("group", { name: "Open step Route priority" });
    Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [source] });
    fireEvent.click(source);
    const canvas = screen.getByRole("region", { name: "Workflow canvas" });
    await waitFor(() => {
      const controls = anchors.map((anchor) => {
        const handle = anchor.key === "default" ? "default" : `case:${anchor.key}`;
        const wrapper = occupied.includes(anchor.key)
          ? queryAllByAttribute("data-id", canvas, `edge:step:route_priority:${handle}`)[0]
          : screen.getByText((_content, element) => element?.textContent === `${anchor.label}: add step`, { selector: "div" });
        expect(wrapper).toBeDefined();
        const paths = occupied.includes(anchor.key)
          ? queryAllByAttribute("class", wrapper!, /(?:^|\s)react-flow__edge-path(?:\s|$)/)
          : queryAllByAttribute("d", wrapper!, /^M /);
        expect(paths).toHaveLength(1);
        const d = paths[0].getAttribute("d");
        expect(d?.match(/[A-Za-z]/g)).toEqual(["M", "L", "C", "L"]);
        const numbers = d!.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
        expect(numbers).toHaveLength(12);
        return { x: numbers.filter((_, i) => i % 2 === 0), y: numbers.filter((_, i) => i % 2 === 1) };
      });
      controls.slice(1).forEach((points, i) => {
        expect(points.x).toEqual(controls[i].x);
        points.y.forEach((y, j) => expect(y).toBeGreaterThan(controls[i].y[j]));
      });
      if (!occupied.includes("default")) expect(Number.parseFloat(screen.getByRole("button", { name: "Default: add step" }).style.top)).toBeGreaterThanOrEqual(576);
    });
    const slot = screen.getByRole("button", { name: `${append}: add step` });
    const point = { x: Number.parseFloat(slot.style.left), y: Number.parseFloat(slot.style.top) };
    fireEvent.click(slot);
    expect(onAppendStep).toHaveBeenCalledExactlyOnceWith({ kind: "step", id: "route_priority" }, outcome, { source: { kind: "step", id: "route_priority" }, outcome, scope: { groupId: "", forEachId: "", laneId: "" }, point, selectionGeneration: 1 });
    expect(JSON.parse(localStorage.getItem(key) || "{}")).toEqual(manual);
  } finally {
    if (previous === null) localStorage.removeItem(key); else localStorage.setItem(key, previous);
    if (hitTest) Object.defineProperty(document, "elementsFromPoint", hitTest); else Reflect.deleteProperty(document, "elementsFromPoint");
    if (bbox) Object.defineProperty(SVGElement.prototype, "getBBox", bbox); else Reflect.deleteProperty(SVGElement.prototype, "getBBox");
    vi.unstubAllGlobals();
  }
});


it("does not auto-select a delayed creation after the author selected another card", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  const bbox = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
  Object.defineProperty(SVGElement.prototype, "getBBox", { configurable: true, value: () => ({ height: 16, width: 120, x: 0, y: 0 }) });
  try {
    const pending = { nodeId: "new_default", nonce: 1, selectionGeneration: 0 };
    const activeGraph: WorkflowCanvasGraph = { ...switchGraph, edges: [{ source: { kind: "step", id: "priority" }, outcome: { name: "case", case_id: "high" }, target: { kind: "step", id: "occupied" } }] };
    const view = render(<MemoryRouter><SlotCanvas activeGraph={activeGraph} creationSelection={pending} /></MemoryRouter>);
    const high = await screen.findByRole("group", { name: "Open step Occupied" });
    fireEvent.click(high);
    const nodeById = (id: string) => {
      const matches = queryAllByAttribute("data-id", view.container, id);
      expect(matches).toHaveLength(1);
      return matches[0];
    };
    await waitFor(() => expect(nodeById("occupied")).toHaveClass("selected"));
    const published: WorkflowCanvasGraph = { ...activeGraph, steps: [...activeGraph.steps,
      { id: "new_default", step_id: "new_default", kind: "set_variable", display_name: "New Default", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] }],
      edges: [...activeGraph.edges, { source: { kind: "step", id: "priority" }, outcome: { name: "default" }, target: { kind: "step", id: "new_default" } }] };
    view.rerender(<MemoryRouter><SlotCanvas activeGraph={published} creationSelection={pending} /></MemoryRouter>);
    await screen.findByRole("group", { name: "Open step New Default" });
    expect(nodeById("new_default")).not.toHaveClass("selected");
    expect(nodeById("occupied")).toHaveClass("selected");
  } finally {
    if (bbox) Object.defineProperty(SVGElement.prototype, "getBBox", bbox); else Reflect.deleteProperty(SVGElement.prototype, "getBBox");
    vi.unstubAllGlobals();
  }
});
