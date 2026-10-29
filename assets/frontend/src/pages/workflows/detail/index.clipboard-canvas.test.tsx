import { act, fireEvent, queryAllByAttribute, screen, waitFor, within } from "@testing-library/react";
import { createElement } from "react";
import type { ReactFlowInstance, ReactFlowProps } from "@xyflow/react";
import type * as ReactFlowModule from "@xyflow/react";
import type { BuilderClipboardV1 } from "@/pages/workflows/builder/canvas-clipboard";
import type * as CanvasLayout from "@/pages/workflows/builder/layout";
import { loadManualLayout, saveManualLayout } from "@/pages/workflows/builder/canvas-core";
import { loadArrangeMode, saveArrangeMode } from "@/pages/workflows/builder/canvas-layout-menu";
// Hold the real layout's created node at the rendering boundary to exercise
// accepted graph intent while canvas publication is delayed.
const projection = vi.hoisted(() => ({ holdCreatedStep: false }));
vi.mock("@/pages/workflows/builder/layout", async (importOriginal) => {
  const real = await importOriginal<typeof CanvasLayout>();
  return { ...real, buildCanvasLayout: (...args: Parameters<typeof real.buildCanvasLayout>) => {
    const layout = real.buildCanvasLayout(...args);
    return projection.holdCreatedStep ? { ...layout, nodes: layout.nodes.filter((node) => node.id !== "plan_2") } : layout;
  } };
});

// Observe the real ReactFlow instance; the component, graph intent, layout and
// viewport implementation remain real. A held getNodes read models only the
// controlled-node publication boundary, never clipboard or graph acceptance.
const viewportProbe = vi.hoisted(() => ({
  instance: null as ReactFlowInstance | null,
  holdCreated: false,
  holdSizes: false,
  measuredSize: null as { width: number; height: number } | null,
  growManualHeight: null as number | null,
  holdAfterManual: false,
  manualReads: [] as Array<{ x: number; y: number }>,
  moveEvents: [] as Array<"user" | "programmatic">,
  moveStart: null as ReactFlowProps["onMoveStart"] | null,
}));
vi.mock("@xyflow/react", async (importOriginal) => {
  const real = await importOriginal<typeof ReactFlowModule>();
  return { ...real, ReactFlow: (props: ReactFlowProps) => {
    const onMoveStart: NonNullable<ReactFlowProps["onMoveStart"]> = (event, viewport) => {
      viewportProbe.moveEvents.push(event ? "user" : "programmatic");
      props.onMoveStart?.(event, viewport);
    };
    viewportProbe.moveStart = onMoveStart;
    return createElement(real.ReactFlow, { ...props, onMoveStart, onInit: (instance: ReactFlowInstance) => {
      viewportProbe.instance = instance;
      const getNodes = instance.getNodes;
      const published = () => getNodes().flatMap((node) => {
        if (!["plan_2", "done_2"].includes(node.id)) return [node];
        if (viewportProbe.holdCreated) return [];
        const manual = loadManualLayout("weekly_report").plan_2;
        if (manual && (viewportProbe.growManualHeight || viewportProbe.holdAfterManual)) {
          const previous = viewportProbe.manualReads.at(-1);
          if (!previous || previous.x !== manual.x || previous.y !== manual.y) viewportProbe.manualReads.push({ ...manual });
        }
        // Only the public geometry read changes: acceptance, layout, controlled
        // positions, renderer and real viewport implementation remain in place.
        const size = viewportProbe.holdSizes || viewportProbe.holdAfterManual && manual
          ? { width: 0, height: 0 }
          : viewportProbe.growManualHeight && manual
            ? { width: 224, height: viewportProbe.growManualHeight }
            : viewportProbe.measuredSize;
        return [size ? { ...node, ...size, measured: size, style: { ...node.style, ...size } } : node];
      });
      vi.spyOn(instance, "getNodes").mockImplementation(published);
      const getNodesBounds = instance.getNodesBounds;
      vi.spyOn(instance, "getNodesBounds").mockImplementation((nodes, ...options) => {
        if (!viewportProbe.measuredSize && !viewportProbe.growManualHeight) return getNodesBounds(nodes, ...options);
        const current = new Map(published().map((node) => [node.id, node]));
        const resolved = nodes.map((node) => current.get(typeof node === "string" ? node : node.id)).filter((node) => node !== undefined);
        // These measurement tests copy root steps. Nested tests continue through
        // the instance's parent-aware implementation without this seam.
        return real.getNodesBounds(resolved);
      });
      props.onInit?.(instance);
    } });
  } };
});

// JSDOM does not lay out elements. Model only the canvas and its actual
// bottom controls; all card sizes/positions still come from the real layout.
let restoreCanvasRects: (() => void) | undefined;
beforeEach(() => {
  viewportProbe.instance = null; viewportProbe.holdCreated = false; viewportProbe.holdSizes = false;
  viewportProbe.measuredSize = null; viewportProbe.growManualHeight = null; viewportProbe.holdAfterManual = false;
  viewportProbe.manualReads = []; viewportProbe.moveEvents = []; viewportProbe.moveStart = null;
  const rect = Element.prototype.getBoundingClientRect;
  const spy = vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    if (this.classList.contains("workflow-canvas-stage") || this.classList.contains("react-flow")) return new DOMRect(0, 80, 1440, 820);
    if (this.classList.contains("workflow-canvas-toolbar")) return new DOMRect(325, 842, 790, 42);
    if (this.classList.contains("workflow-canvas-minimap")) return new DOMRect(1200, 742, 180, 100);
    if (this instanceof HTMLElement && this.classList.contains("workflow-builder-drawer")) {
      const width = Number(this.dataset.width);
      if (Number.isFinite(width) && width > 0) return new DOMRect(globalThis.innerWidth - width, 80, width, 820);
    }
    return rect.call(this);
  });
  restoreCanvasRects = () => spy.mockRestore();
});
afterEach(() => { restoreCanvasRects?.(); viewportProbe.holdCreated = false; viewportProbe.holdSizes = false; });

import {
  canvasGraphFixture,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowExportPolicy,
  postedChangeSets,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

/** Public attribute queries identify ReactFlow geometry surfaces without DOM traversal. */
function canvasElementByAttribute(container: HTMLElement, attribute: string, value: string | RegExp) {
  const matches = queryAllByAttribute(attribute, container, value);
  if (matches.length !== 1) throw new Error(`Expected one canvas ${attribute}=${String(value)}, found ${matches.length}`);
  return matches[0];
}
function canvasControlRects(stage: HTMLElement) {
  return queryAllByAttribute("class", stage, /(?:^|\s)workflow-canvas-(?:toolbar|minimap|runbar)(?:\s|$)/)
    .map((control) => control.getBoundingClientRect()).filter((rect) => rect.width > 0 && rect.height > 0);
}

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
/** One selected node shows no selection bar (it starts at two); its keys act on it (Canvas › Keys). */
function pressCanvasKey(code: "KeyA" | "KeyC" | "KeyD" | "KeyV") {
  const canvas = screen.getByRole("region", { name: "Workflow canvas" });
  canvas.focus();
  fireEvent.keyDown(canvas, { key: code.slice(3).toLowerCase(), code, ctrlKey: true });
}
const edges = [{ source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } }];
function installWorkflow() {
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps: [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable", override: { set_variable: { assignments: { "state.plan": true } } } },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ], edges }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "step", id: "plan" }, edges,
    steps: [
      { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", config_summary: "state.plan", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
    ],
  }));
}

it("copies the selected authored step through the real canvas action without staging a graph edit", async () => {
  installWorkflow();
  const writeText = vi.fn<(text: string) => Promise<void>>(async () => undefined);
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn() } });
  try {
    renderPage({ surface: "builder" });
    clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
    pressCanvasKey("KeyC");
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Plan work copied.")).toBeInTheDocument();
    const copied = JSON.parse(String(writeText.mock.calls[0][0])) as { roots: Array<{ kind: string; id: string }> };
    expect(copied.roots).toEqual([{ kind: "step", id: "plan" }]);
    expect(postedChangeSets()).toEqual([]);
    expect(screen.getByRole("group", { name: "Open step Plan work" })).toBeVisible();
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

it("pastes the builder's copy into the workflow root as one unapplied change (Canvas › Copy and paste)", async () => {
  installWorkflow();
  let clip = "";
  const writeText = vi.fn<(text: string) => Promise<void>>(async (text) => { clip = text; });
  const readText = vi.fn<() => Promise<string>>(async () => clip);
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText } });
  try {
    renderPage({ surface: "builder" });
    clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
    pressCanvasKey("KeyC");
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    // The pointer rests over the canvas: the copy's top left lands there, placed by hand.
    fireEvent.pointerMove(screen.getByRole("region", { name: "Workflow canvas" }), { clientX: 320, clientY: 240 });
    pressCanvasKey("KeyV");
    await waitFor(() => expect(screen.getAllByRole("group", { name: "Open step Plan work" })).toHaveLength(2));
    expect(readText).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
    await waitFor(() => expect(loadManualLayout("weekly_report").plan_2).toEqual({ x: expect.any(Number), y: expect.any(Number) }));
    // A place by hand while Arrange is Automatically turns it to Manually, and the toast says so with Undo.
    expect(await screen.findByText("Layout: manually. Steps stay where you put them.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();
    expect(postedChangeSets()).toEqual([]);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

it.each(["auto", "manual"] as const)("duplicates a selected authored step as one unapplied intent (Arrange %s) and canvas Undo removes only the copy", async (mode) => {
  installWorkflow();
  saveArrangeMode("weekly_report", mode);
  renderPage({ surface: "builder" });
  clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
  pressCanvasKey("KeyD");
  const copy = await screen.findByRole("group", { name: "Open step Plan work copy" });
  expect(copy).toBeVisible();
  if (mode === "manual") {
    // Manually: the copy keeps the place it was given, right under its original.
    await waitFor(() => expect(loadManualLayout("weekly_report").plan_2).toEqual({ x: expect.any(Number), y: expect.any(Number) }));
  } else {
    // Automatically: the layout places the copy; no hand-made place is stored for it.
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)"));
    expect(loadManualLayout("weekly_report").plan_2).toBeUndefined();
  }
  expect(postedChangeSets()).toEqual([]);
  const canvas = screen.getByRole("region", { name: "Workflow canvas" });
  canvas.focus();
  fireEvent.keyDown(canvas, { key: "z", code: "KeyZ", ctrlKey: true });
  await waitFor(() => expect(screen.queryByRole("group", { name: "Open step Plan work copy" })).not.toBeInTheDocument());
  expect(screen.getByRole("group", { name: "Open step Plan work" })).toBeVisible();
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expect(postedChangeSets()).toEqual([]);
});

it("reports protected paste refusal without reading the clipboard or staging a graph edit", async () => {
  installWorkflow();
  mockedFetchWorkflowExportPolicy.mockResolvedValueOnce({
    workflow_id: "weekly_report",
    extension_owned: true,
    protected: true,
    structure_change_allowed: false,
  } as never);
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const readText = vi.fn(async () => { throw new DOMException("Read permission denied", "NotAllowedError"); });
  const writeText = vi.fn(async () => undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText } });
  try {
    renderPage({ surface: "builder" });
    await screen.findByRole("group", { name: "Open step Plan work" });
    const canvas = await screen.findByRole("region", { name: "Workflow canvas" });
    const canvasIds = () => queryAllByAttribute("class", canvas, /(?:^|\s)react-flow__node(?:\s|$)/u)
      .map((element) => element.getAttribute("data-id"))
      .filter((id): id is string => id !== null)
      .sort();
    const before = canvasIds();
    expect(before.length).toBeGreaterThan(0);
    expect(screen.queryByTestId("structure-status")).not.toBeInTheDocument();
    const protection = screen.getByRole("note", { name: "Protected extension workflow" });
    const protectionText = protection.textContent;
    canvas.focus();
    fireEvent.keyDown(canvas, { key: "v", code: "KeyV", ctrlKey: true });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This workflow's structure is read-only. Paste into a workflow you can edit.",
    );
    expect(readText).not.toHaveBeenCalled();
    expect(writeText).not.toHaveBeenCalled();
    expect(canvasIds()).toEqual(before);
    expect(screen.queryByTestId("structure-status")).not.toBeInTheDocument();
    expect(protection).toHaveTextContent(protectionText);
    expect(postedChangeSets()).toEqual([]);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

it("reports a clipboard read denial as a paste failure without staging a change", async () => {
  installWorkflow();
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const readText = vi.fn(async () => { throw new DOMException("Read permission denied", "NotAllowedError"); });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    writeText: vi.fn(async () => undefined), readText,
  } });
  try {
    renderPage({ surface: "builder" });
    clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
    pressCanvasKey("KeyV");
    await waitFor(() => expect(readText).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not read from clipboard.");
    expect(postedChangeSets()).toEqual([]);
    expect(screen.getAllByRole("group", { name: "Open step Plan work" })).toHaveLength(1);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

it("names pasted steps and offers Undo for the staged paste", async () => {
  installWorkflow();
  saveArrangeMode("weekly_report", "manual");
  let clip = "";
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  const readText = vi.fn(async () => clip);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText } });
  try {
    renderPage({ surface: "builder" });
    clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
    const canvas = screen.getByRole("region", { name: "Workflow canvas" });
    const canvasIds = () => queryAllByAttribute("class", canvas, /(?:^|\s)react-flow__node(?:\s|$)/u)
      .map((element) => element.getAttribute("data-id"))
      .filter((id): id is string => id !== null)
      .sort();
    pressCanvasKey("KeyC");
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const originalIds = canvasIds();
    fireEvent.pointerMove(canvas, { clientX: 320, clientY: 240 });
    pressCanvasKey("KeyV");
    await waitFor(() => expect(screen.getAllByRole("group", { name: "Open step Plan work" })).toHaveLength(2));
    const pastedIds = canvasIds().filter((id) => !originalIds.includes(id));
    expect(pastedIds.length).toBeGreaterThan(0);
    expect(await screen.findByText("Pasted Plan work to the unapplied structure.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(screen.getAllByRole("group", { name: "Open step Plan work" })).toHaveLength(1));
    expect(canvasIds()).toEqual(originalIds);
    expect(postedChangeSets()).toEqual([]);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

it("reports a clipboard write failure in the canvas without staging a change", async () => {
  installWorkflow();
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    writeText: vi.fn(async () => { throw new Error("synthetic clipboard refusal"); }),
    readText: vi.fn(),
  } });
  try {
    renderPage({ surface: "builder" });
    clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
    pressCanvasKey("KeyC");
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not copy to clipboard.");
    expect(postedChangeSets()).toEqual([]);
    expect(screen.getByRole("group", { name: "Open step Plan work" })).toBeVisible();
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

it("handles Copy and Duplicate keys only from canvas focus, preserving native textbox Copy", async () => {
  installWorkflow();
  const writeText = vi.fn<(text: string) => Promise<void>>(async () => undefined);
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn() } });
  try {
    renderPage({ surface: "builder" });
    const plan = await screen.findByRole("group", { name: "Open step Plan work" });
    clickCard(plan);
    fireEvent.click(screen.getByRole("button", { name: /Search canvas/ }));
    const search = screen.getByRole("textbox", { name: "Search workflow canvas" });
    fireEvent.change(search, { target: { value: "plan" } });
    fireEvent.keyDown(search, { key: "c", code: "KeyC", ctrlKey: true });
    expect(writeText).not.toHaveBeenCalled();
    expect(search).toHaveValue("plan");
    fireEvent.click(screen.getByRole("button", { name: /Search canvas/ }));
    const canvas = screen.getByRole("region", { name: "Workflow canvas" });
    canvas.focus();
    fireEvent.keyDown(canvas, { key: "c", code: "KeyC", ctrlKey: true });
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    canvas.focus();
    fireEvent.keyDown(canvas, { key: "d", code: "KeyD", ctrlKey: true });
    expect(await screen.findByRole("group", { name: "Open step Plan work copy" })).toBeVisible();
    expect(postedChangeSets()).toEqual([]);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

it("shows a busy status during a held clipboard write and retires it after acknowledgement", async () => {
  installWorkflow();
  let finishWrite: (() => void) | undefined;
  const writeText = vi.fn<(text: string) => Promise<void>>(() => new Promise<void>((resolve) => { finishWrite = resolve; }));
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn() } });
  try {
    renderPage({ surface: "builder" });
    clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
    pressCanvasKey("KeyC");
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    // The busy status is a delayed loader: a write under 400ms paints nothing.
    expect(screen.queryByText("Copying selection…")).not.toBeInTheDocument();
    expect(await screen.findByText("Copying selection…", undefined, { timeout: 2_000 })).toBeVisible();
    pressCanvasKey("KeyC");
    expect(writeText).toHaveBeenCalledTimes(1);
    finishWrite?.();
    await waitFor(() => expect(screen.queryByText("Copying selection…")).not.toBeInTheDocument(), { timeout: 2_000 });
    expect(postedChangeSets()).toEqual([]);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

function installContainerWorkflow(lanes = false) {
  const steps = [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable", group_id: "checks", ...(lanes ? { lane_id: "work" } : {}) },
    { step_id: "review", display_name: "Review work", step_kind: "set_variable" },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ];
  const body = lanes ? { kind: "lanes", lanes: [{ lane_id: "work", entry: { kind: "step", id: "plan" } }] } : { kind: "sequence", entry: { kind: "step", id: "plan" } };
  const groups = [{ group_id: "checks", display_name: "Checks", body }];
  const nestedEdges = [
    { source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "checks", lane_id: lanes ? "work" : null } },
    { source: { kind: "group", id: "checks" }, outcome: { name: "success" }, target: { kind: "step", id: "review" } },
    { source: { kind: "step", id: "review" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
  ];
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps, groups, entry: { kind: "group", id: "checks" }, edges: nestedEdges }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "group", id: "checks" },
    groups: [{ id: "checks", group_id: "checks", display_name: "Checks", body }],
    steps: [
      { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", group_id: "checks", ...(lanes ? { lane_id: "work" } : {}), config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "review", step_id: "review", kind: "set_variable", display_name: "Review work", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
    ], edges: nestedEdges,
  }));
}

/** Shift+click adds a node to the selection without opening its editor (Canvas › Selecting). */
function additiveSelectionKey() {
  return { key: "Shift", code: "ShiftLeft", shiftKey: true };
}

it("duplicates a selected Group and its child from the one canonical Group destination", async () => {
  installContainerWorkflow();
  saveManualLayout("weekly_report", { "group:checks": { x: 320, y: 128 }, plan: { x: 64, y: 160 } });
  renderPage({ surface: "builder" });
  const canvas = await screen.findByRole("region", { name: "Workflow canvas" });
  const graph = within(canvas);
  await graph.findByRole("button", { name: /^Checks\b/ });
  const groupNode = graph.getAllByRole("group").find((candidate) =>
    candidate.getAttribute("aria-roledescription") === "node" &&
    candidate.getAttribute("data-id") === "group:checks" &&
    within(candidate).queryByRole("button", { name: /^Checks\b/ }));
  if (!groupNode) throw new Error("Missing React Flow Group wrapper");
  fireEvent.pointerDown(groupNode);
  fireEvent.pointerUp(groupNode);
  clickCard(groupNode);
  await waitFor(() => expect(groupNode).toHaveClass("selected"));
  const additive = additiveSelectionKey();
  fireEvent.keyDown(window, additive);
  const childFrame = graph.getByRole("group", { name: "Open step Plan work" });
  clickCard(childFrame, additive);
  fireEvent.keyUp(window, additive);
  expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument();
  const toolbar = await screen.findByRole("toolbar", { name: "Selected items" });
  expect(toolbar).toHaveTextContent("2 selected");
  expect(groupNode).toHaveClass("selected");
  // React Flow publishes world transforms: Group (320,128) + child local (64,160).
  const childNode = graph.getAllByRole("group").find((candidate) => candidate.getAttribute("data-id") === "plan");
  if (!childNode) throw new Error("Missing React Flow child wrapper");
  await waitFor(() => {
    expect(groupNode.style.transform).toMatch(/^translate\(320px,\s*128px\)$/);
    expect(childNode.style.transform).toMatch(/^translate\(384px,\s*288px\)$/);
  });
  const duplicate = within(toolbar).getByRole("button", { name: "Duplicate" });
  expect(duplicate).toBeEnabled();
  fireEvent.click(duplicate);
  // One journal intent contains create_group, create_step and their internal connect.
  await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)"));
  expect(await graph.findByRole("button", { name: /^Checks copy\b/ })).toBeVisible();
  expect(graph.getAllByRole("group", { name: "Open step Plan work" })).toHaveLength(2);
  expect(screen.queryByRole("group", { name: "Open step Plan work copy" })).not.toBeInTheDocument();
  // The copy is offset in world space; its child retains the exact parent-local offset.
  await waitFor(() => expect(loadManualLayout("weekly_report").plan_2).toEqual({ x: 64, y: 160 }));
  expect(postedChangeSets()).toEqual([]);
  canvas.focus();
  fireEvent.keyDown(canvas, { key: "z", code: "KeyZ", ctrlKey: true });
  await waitFor(() => {
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    expect(screen.queryByRole("button", { name: /^Checks copy\b/ })).not.toBeInTheDocument();
    expect(graph.getByRole("button", { name: /^Checks\b/ })).toBeVisible();
    expect(graph.getAllByRole("group", { name: "Open step Plan work" })).toHaveLength(1);
  });
  expect(postedChangeSets()).toEqual([]);
}, 15_000);

it("allows Copy across source placements while disabling Duplicate without one destination", async () => {
  installContainerWorkflow();
  const writeText = vi.fn<(text: string) => Promise<void>>(async () => undefined);
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn() } });
  try {
    renderPage({ surface: "builder" });
    clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
    const firstEditor = await screen.findByRole("dialog", { name: "Step editor" });
    fireEvent.click(within(firstEditor).getByRole("button", { name: "Close" }));
    const additive = additiveSelectionKey();
    fireEvent.keyDown(window, additive);
    clickCard(screen.getByRole("group", { name: "Open step Review work" }), additive);
    fireEvent.keyUp(window, additive);
    const toolbar = await screen.findByRole("toolbar", { name: "Selected items" });
    expect(toolbar).toHaveTextContent("2 selected");
    expect(within(toolbar).getByRole("button", { name: "Duplicate" })).toBeDisabled();
    fireEvent.click(within(toolbar).getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = JSON.parse(String(writeText.mock.calls[0][0])) as { roots: Array<{ id: string }> };
    expect(copied.roots.map((root) => root.id).sort()).toEqual(["plan", "review"]);
    expect(postedChangeSets()).toEqual([]);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

it("keeps one accepted Duplicate pending through delayed canvas projection and reports a finite visual failure", async () => {
  installWorkflow();
  renderPage({ surface: "builder" });
  clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
  projection.holdCreatedStep = true;
  vi.useFakeTimers();
  try {
    pressCanvasKey("KeyD");
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
    // A second ⌘D waits for the first copy's canvas publication.
    pressCanvasKey("KeyD");
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(screen.getByRole("alert")).toHaveTextContent("The copy was added, but its canvas placement needs attention.");
    expect(postedChangeSets()).toEqual([]);
  } finally {
    projection.holdCreatedStep = false;
    vi.useRealTimers();
  }
});

it("retires a held Duplicate visual write when canvas Undo removes its accepted graph intent", async () => {
  installWorkflow();
  renderPage({ surface: "builder" });
  clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
  projection.holdCreatedStep = true;
  vi.useFakeTimers();
  try {
    pressCanvasKey("KeyD");
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
    const canvas = screen.getByRole("region", { name: "Workflow canvas" });
    canvas.focus();
    fireEvent.keyDown(canvas, { key: "z", code: "KeyZ", ctrlKey: true });
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument();
    expect(loadManualLayout("weekly_report").plan_2).toBeUndefined();
    expect(postedChangeSets()).toEqual([]);
  } finally {
    projection.holdCreatedStep = false;
    vi.useRealTimers();
  }
});


it("preserves a later canvas selection while a committed copy awaits visual publication", async () => {
  const threeEdges = [
    { source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "step", id: "review" } },
    { source: { kind: "step", id: "review" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
  ];
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps: [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable" },
    { step_id: "review", display_name: "Review work", step_kind: "set_variable" },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ], edges: threeEdges }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "step", id: "plan" }, edges: threeEdges,
    steps: [
      { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "review", step_id: "review", kind: "set_variable", display_name: "Review work", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
    ],
  }));
  renderPage({ surface: "builder" });
  clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
  projection.holdCreatedStep = true;
  vi.useFakeTimers();
  try {
    pressCanvasKey("KeyD");
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
    const review = screen.getByRole("group", { name: "Open step Review work" });
    clickCard(review);
    const editor = screen.getByRole("dialog", { name: "Step editor" });
    fireEvent.click(within(editor).getByRole("button", { name: "Close" }));
    const reviewNode = screen.getAllByRole("group").find((candidate) =>
      candidate.getAttribute("aria-roledescription") === "node" &&
      within(candidate).queryByRole("group", { name: "Open step Review work" }));
    if (!reviewNode) throw new Error("Missing Review React Flow wrapper");
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(reviewNode).toHaveClass("selected");
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(reviewNode).toHaveClass("selected");
    expect(screen.getByRole("alert")).toHaveTextContent("The copy was added, but its canvas placement needs attention.");
    expect(postedChangeSets()).toEqual([]);
  } finally {
    projection.holdCreatedStep = false;
    vi.useRealTimers();
  }
});

it("retires held copy geometry after Discard without treating a reused local id as the old intent", async () => {
  installWorkflow();
  renderPage({ surface: "builder" });
  clickCard(await screen.findByRole("group", { name: "Open step Plan work" }));
  projection.holdCreatedStep = true;
  vi.useFakeTimers();
  try {
    pressCanvasKey("KeyD");
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(loadManualLayout("weekly_report").plan_2).toBeUndefined();
    expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument();
    expect(postedChangeSets()).toEqual([]);
  } finally {
    projection.holdCreatedStep = false;
    vi.useRealTimers();
  }
});


it("summarizes a multi-root paste by count and offers Undo", async () => {
  installWorkflow();
  saveArrangeMode("weekly_report", "manual");
  let clip = "";
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  const readText = vi.fn(async () => clip);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText } });
  try {
    renderPage({ surface: "builder" });
    await copyPair();
    const canvas = screen.getByRole("region", { name: "Workflow canvas" });
    const canvasIds = () => queryAllByAttribute("class", canvas, /(?:^|\s)react-flow__node(?:\s|$)/u)
      .map((element) => element.getAttribute("data-id"))
      .filter((id): id is string => id !== null)
      .sort();
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const originalIds = canvasIds();
    fireEvent.pointerMove(canvas, { clientX: 320, clientY: 240 });
    pressCanvasKey("KeyV");
    await waitFor(() => {
      expect(screen.getAllByRole("group", { name: "Open step Plan work" })).toHaveLength(2);
      expect(screen.getAllByRole("group", { name: "Open step Done" })).toHaveLength(2);
    });
    const pastedIds = canvasIds().filter((id) => !originalIds.includes(id));
    expect(pastedIds.length).toBeGreaterThan(0);
    expect(await screen.findByText("2 items pasted to the unapplied structure.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => {
      expect(screen.getAllByRole("group", { name: "Open step Plan work" })).toHaveLength(1);
      expect(screen.getAllByRole("group", { name: "Open step Done" })).toHaveLength(1);
    });
    expect(canvasIds()).toEqual(originalIds);
    expect(postedChangeSets()).toEqual([]);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

async function copyPair() {
  await screen.findByRole("group", { name: "Open step Plan work" });
  pressCanvasKey("KeyA");
  const selected = await screen.findByRole("toolbar", { name: "Selected items" });
  expect(selected).toHaveTextContent("2 selected");
  fireEvent.click(within(selected).getByRole("button", { name: "Copy" }));
}
/** Clear the copied pair through the visible selection toolbar. */
async function clearOriginalPairSelection(canvas: HTMLElement) {
  const oldPlan = canvasElementByAttribute(canvas, "data-id", "plan");
  const oldDone = canvasElementByAttribute(canvas, "data-id", "done");
  const selected = screen.getByRole("toolbar", { name: "Selected items" });
  fireEvent.click(within(selected).getByRole("button", { name: "Clear the selection (Esc)" }));
  await waitFor(() => {
    expect(oldPlan).not.toHaveClass("selected");
    expect(oldDone).not.toHaveClass("selected");
  });
  return oldPlan;
}
async function selectOnlyOriginalPlan(canvas: HTMLElement) {
  const oldPlan = await clearOriginalPairSelection(canvas);
  fireEvent.keyDown(oldPlan, { key: "Enter", code: "Enter" });
  return oldPlan;
}
async function realViewport() {
  await waitFor(() => expect(viewportProbe.instance).not.toBeNull());
  return viewportProbe.instance!;
}
function historyKey(redo = false) {
  const canvas = screen.getByRole("region", { name: "Workflow canvas" }); canvas.focus();
  fireEvent.keyDown(canvas, { key: "z", code: "KeyZ", ctrlKey: true, shiftKey: redo });
}
function expectPublishedInside(instance: ReactFlowInstance, ids: string[], right = 1424, bottom = 884) {
  for (const id of ids) expect(instance.getNode(id)).toBeDefined();
  const bounds = instance.getNodesBounds(ids); const viewport = instance.getViewport();
  expect([bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite)).toBe(true);
  expect(bounds.width).toBeGreaterThan(0); expect(bounds.height).toBeGreaterThan(0);
  expect(bounds.x * viewport.zoom + viewport.x).toBeGreaterThanOrEqual(15.99);
  expect((bounds.x + bounds.width) * viewport.zoom + viewport.x).toBeLessThanOrEqual(right + 0.01);
  expect(bounds.y * viewport.zoom + viewport.y + 80).toBeGreaterThanOrEqual(95.99);
  expect((bounds.y + bounds.height) * viewport.zoom + viewport.y + 80).toBeLessThanOrEqual(bottom + 0.01);
  const stage = screen.getByRole("region", { name: "Workflow canvas" });
  const stageRect = stage.getBoundingClientRect();
  const controls = canvasControlRects(stage);
  for (const id of ids) {
    const rect = instance.getNodesBounds([id]);
    expect([rect.x, rect.y, rect.width, rect.height].every(Number.isFinite)).toBe(true);
    expect(rect.width).toBeGreaterThan(0); expect(rect.height).toBeGreaterThan(0);
    const projected = { left: stageRect.left + viewport.x + rect.x * viewport.zoom,
      top: stageRect.top + viewport.y + rect.y * viewport.zoom,
      right: stageRect.left + viewport.x + (rect.x + rect.width) * viewport.zoom,
      bottom: stageRect.top + viewport.y + (rect.y + rect.height) * viewport.zoom };
    for (const control of controls) {
      const overlapX = Math.min(projected.right, control.right) - Math.max(projected.left, control.left);
      const overlapY = Math.min(projected.bottom, control.bottom) - Math.max(projected.top, control.top);
      // Edge contact is usable; any positive area beyond rounding noise is not.
      expect(overlapX > 1e-7 && overlapY > 1e-7, `Published ${id} overlaps a canvas control`).toBe(false);
    }
  }
}

it.each([0.5, 1, 1.5].flatMap((zoom) => ["keyboard", "menu"].map((route) => ({ zoom, route }))))("clamps the whole accepted pointer paste through $route at zoom $zoom, preserving originals and Undo/Redo", async ({ zoom, route }) => {
  installWorkflow();
  const originals = { plan: { x: 320, y: 160 }, done: { x: 640, y: 208 } };
  saveManualLayout("weekly_report", originals); saveArrangeMode("weekly_report", "manual");
  let clip = "";
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn(async () => clip) } });
  try {
    renderPage({ surface: "builder" }); await copyPair();
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = JSON.parse(clip) as BuilderClipboardV1;
    expect(copied.definition.graph.edges).toEqual(edges);
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: 24, y: 24, zoom }, { duration: 0 }); });
    const canvas = screen.getByRole("region", { name: "Workflow canvas" });
    if (route === "keyboard") {
      fireEvent.pointerMove(canvas, { clientX: 720, clientY: 853 }); pressCanvasKey("KeyV");
    } else {
      const pane = canvasElementByAttribute(canvas, "class", /(?:^|\s)react-flow__pane(?:\s|$)/);
      fireEvent.contextMenu(pane, { clientX: 1400, clientY: 800 });
      fireEvent.click(await screen.findByRole("menuitem", { name: /Paste here/ }));
    }
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)"));
    await waitFor(() => expect(instance.getEdges().filter((edge) => edge.source === "plan_2" || edge.target === "done_2"))
      .toEqual([expect.objectContaining({ source: "plan_2", target: "done_2", sourceHandle: "success", targetHandle: "in" })]));
    expect(instance.getNode("plan_2")?.parentId).toBeUndefined();
    expect(instance.getNode("done_2")?.parentId).toBeUndefined();
    await waitFor(() => expectPublishedInside(instance, ["plan_2", "done_2"]));
    const stored = loadManualLayout("weekly_report");
    expect(stored.plan).toEqual(originals.plan); expect(stored.done).toEqual(originals.done);
    expect(stored.done_2.x - stored.plan_2.x).toBeCloseTo(320);
    expect(stored.done_2.y - stored.plan_2.y).toBeCloseTo(48);
    const afterPaste = { plan: stored.plan_2, done: stored.done_2 };
    historyKey(); await waitFor(() => expect(instance.getNodes().some((node) => node.id === "plan_2")).toBe(false));
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    historyKey(true); await waitFor(() => expectPublishedInside(instance, ["plan_2", "done_2"]));
    expect(loadManualLayout("weekly_report").plan_2).toEqual(afterPaste.plan);
    expect(loadManualLayout("weekly_report").done_2).toEqual(afterPaste.done);
    expect(postedChangeSets()).toEqual([]);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);

it.each(["reveal", "later-selection", "discard"] as const)("waits for published offset-auto paste geometry: %s", async (action) => {
  installWorkflow(); saveArrangeMode("weekly_report", "auto");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn(async () => clip) } });
  let restoreViewport: (() => void) | undefined;
  try {
    renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: -1600, y: 24, zoom: 1 }, { duration: 0 }); });
    const setViewport = vi.spyOn(instance, "setViewport"); restoreViewport = () => setViewport.mockRestore();
    viewportProbe.holdCreated = true;
    fireEvent.pointerLeave(screen.getByRole("region", { name: "Workflow canvas" })); pressCanvasKey("KeyV");
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)"));
    // The journal is accepted, while the real public geometry reader withholds the batch.
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 50)); });
    expect(setViewport).not.toHaveBeenCalled();
    if (action === "later-selection") {
      const canvas = screen.getByRole("region", { name: "Workflow canvas" });
      const oldPlan = await selectOnlyOriginalPlan(canvas);
      await waitFor(() => expect(oldPlan).toHaveClass("selected"));
      expect(canvasElementByAttribute(canvas, "data-id", "done")).not.toHaveClass("selected");
    } else if (action === "discard") fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    viewportProbe.holdCreated = false;
    if (action === "reveal") {
      await waitFor(() => expect(setViewport).toHaveBeenCalledTimes(1));
      expect(setViewport).toHaveBeenCalledWith(expect.objectContaining({ x: expect.any(Number), y: expect.any(Number), zoom: expect.any(Number) }), { duration: 0 });
      await waitFor(() => expectPublishedInside(instance, ["plan_2", "done_2"]));
      expect(loadManualLayout("weekly_report").plan_2).toBeUndefined();
      expect(loadManualLayout("weekly_report").done_2).toBeUndefined();
      historyKey(); await waitFor(() => expect(instance.getNodes().some((node) => node.id === "plan_2")).toBe(false));
      historyKey(true); await waitFor(() => expect(instance.getNodes().some((node) => node.id === "plan_2")).toBe(true));
      expect(loadManualLayout("weekly_report").plan_2).toBeUndefined();
    } else {
      await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 50)); });
      expect(setViewport).not.toHaveBeenCalled();
      if (action === "discard") expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    }
    expect(postedChangeSets()).toEqual([]);
  } finally {
    viewportProbe.holdCreated = false; restoreViewport?.();
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);


it.each([false, true])("preserves nested child-local offsets and lane identity through a pointer-pasted Group (lanes %s)", async (lanes) => {
  installContainerWorkflow(lanes);
  saveManualLayout("weekly_report", { "group:checks": { x: 320, y: 128 }, plan: { x: 64, y: 160 } });
  saveArrangeMode("weekly_report", "manual");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn(async () => clip) } });
  try {
    renderPage({ surface: "builder" }); await screen.findByRole("button", { name: /^Checks\b/ });
    const canvas = screen.getByRole("region", { name: "Workflow canvas" });
    const group = canvasElementByAttribute(canvas, "data-id", "group:checks");
    const additive = additiveSelectionKey(); fireEvent.keyDown(window, additive); clickCard(group, additive); fireEvent.keyUp(window, additive);
    await waitFor(() => expect(group).toHaveClass("selected")); pressCanvasKey("KeyC");
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = JSON.parse(clip) as BuilderClipboardV1;
    expect(copied.roots).toEqual([{ kind: "group", id: "checks" }]);
    expect(copied.definition.steps).toHaveLength(1);
    expect(copied.definition.steps[0].lane_id ?? null).toBe(lanes ? "work" : null);
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: 24, y: 24, zoom: 0.5 }, { duration: 0 }); });
    fireEvent.pointerMove(canvas, { clientX: 720, clientY: 853 }); pressCanvasKey("KeyV");
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)"));
    await waitFor(() => expectPublishedInside(instance, ["group:checks_2", "plan_2"]));
    expect(instance.getNode("plan_2")?.parentId).toBe("group:checks_2");
    const child = instance.getNode("plan_2")?.data as { step?: { group_id?: string; lane_id?: string } };
    expect(child.step?.group_id).toBe("checks_2"); expect(child.step?.lane_id ?? null).toBe(lanes ? "work" : null);
    expect(loadManualLayout("weekly_report").plan_2).toEqual({ x: 64, y: 160 });
    expect(loadManualLayout("weekly_report")["group:checks"]).toEqual({ x: 320, y: 128 });
    historyKey(); await waitFor(() => expect(instance.getNode("plan_2")).toBeUndefined());
    historyKey(true); await waitFor(() => expectPublishedInside(instance, ["group:checks_2", "plan_2"]));
    expect(loadManualLayout("weekly_report").plan_2).toEqual({ x: 64, y: 160 });
    expect(postedChangeSets()).toEqual([]);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);


it.each(["becomes-ready", "timeout"] as const)("keeps accepted offset-auto paste pending while dimensions are unavailable: %s", async (outcome) => {
  installWorkflow(); saveArrangeMode("weekly_report", "auto");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  const readText = vi.fn(async () => clip);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText } });
  let restoreViewport: (() => void) | undefined;
  try {
    renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: -1600, y: 24, zoom: 1 }, { duration: 0 }); });
    const setViewport = vi.spyOn(instance, "setViewport"); restoreViewport = () => setViewport.mockRestore();
    viewportProbe.holdSizes = true;
    vi.useFakeTimers();
    fireEvent.pointerLeave(screen.getByRole("region", { name: "Workflow canvas" }));
    await act(async () => { pressCanvasKey("KeyV"); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); });
    expect(readText).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)");
    // The IDs exist in the real store; only their public dimension read is withheld.
    expect(instance.getNode("plan_2")).toBeDefined(); expect(instance.getNode("done_2")).toBeDefined();
    expect(instance.getNodes().find((node) => node.id === "plan_2")?.measured?.width).toBe(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(setViewport).not.toHaveBeenCalled();
    expect(loadManualLayout("weekly_report").plan_2).toBeUndefined();
    // A second real keyboard paste is held behind this accepted intent.
    await act(async () => { pressCanvasKey("KeyV"); await Promise.resolve(); });
    expect(readText).toHaveBeenCalledTimes(1);
    if (outcome === "becomes-ready") {
      viewportProbe.holdSizes = false;
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(setViewport).toHaveBeenCalledTimes(1);
      expect(setViewport).toHaveBeenCalledWith(expect.any(Object), { duration: 0 });
      expectPublishedInside(instance, ["plan_2", "done_2"]);
      expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument();
      expect(loadArrangeMode("weekly_report", false)).toBe("auto");
    } else {
      await act(async () => { await vi.advanceTimersByTimeAsync(4_899); });
      expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(screen.getByRole("alert")).toHaveTextContent("The copy was added, but its canvas placement needs attention.");
      expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)");
      expect(setViewport).not.toHaveBeenCalled();
      expect(loadArrangeMode("weekly_report", false)).toBe("auto");
      expect(loadManualLayout("weekly_report").plan_2).toBeUndefined();
      // Busy retires finitely; the original selection can be copied again.
      await act(async () => { pressCanvasKey("KeyC"); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); });
      expect(writeText).toHaveBeenCalledTimes(2);
    }
    expect(readText).toHaveBeenCalledTimes(1); expect(postedChangeSets()).toEqual([]);
  } finally {
    viewportProbe.holdSizes = false; vi.useRealTimers(); restoreViewport?.();
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);

it("preserves an oversized pointer batch exactly and retires it through the bounded attention path", async () => {
  installWorkflow();
  const originals = { plan: { x: 320, y: 160 }, done: { x: 2400, y: 208 } };
  saveManualLayout("weekly_report", originals); saveArrangeMode("weekly_report", "manual");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; }); const readText = vi.fn(async () => clip);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText } });
  let restoreViewport: (() => void) | undefined;
  try {
    renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = JSON.parse(clip) as BuilderClipboardV1;
    expect(copied.relativeWorldPositions.done.x - copied.relativeWorldPositions.plan.x).toBe(2080);
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: 24, y: 24, zoom: 1 }, { duration: 0 }); });
    const setViewport = vi.spyOn(instance, "setViewport"); restoreViewport = () => setViewport.mockRestore();
    vi.useFakeTimers();
    fireEvent.pointerMove(screen.getByRole("region", { name: "Workflow canvas" }), { clientX: 720, clientY: 853 });
    // Flush acceptance and its timeout effect before advancing publication time.
    await act(async () => { pressCanvasKey("KeyV"); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(readText).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)");
    const stored = loadManualLayout("weekly_report");
    expect(stored.plan).toEqual(originals.plan); expect(stored.done).toEqual(originals.done);
    expect(stored.done_2.x - stored.plan_2.x).toBe(2080);
    expect(stored.done_2.y - stored.plan_2.y).toBe(48);
    expect([stored.plan_2.x, stored.plan_2.y, stored.done_2.x, stored.done_2.y].every(Number.isFinite)).toBe(true);
    const bounds = instance.getNodesBounds(["plan_2", "done_2"]);
    // At fixed zoom the full batch is wider than the usable pane: containment
    // cannot be satisfied by translation. No false full-visibility assertion.
    expect(bounds.width * instance.getZoom()).toBeGreaterThan(1408);
    expect(instance.getZoom()).toBe(1); expect(setViewport).not.toHaveBeenCalled();
    expect(loadArrangeMode("weekly_report", true)).toBe("manual");
    await act(async () => { pressCanvasKey("KeyV"); await Promise.resolve(); });
    expect(readText).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(4_899); });
    expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByRole("alert")).toHaveTextContent("The copy was added, but its canvas placement needs attention.");
    expect(loadManualLayout("weekly_report").plan_2).toEqual(stored.plan_2);
    expect(loadManualLayout("weekly_report").done_2).toEqual(stored.done_2);
    await act(async () => { pressCanvasKey("KeyC"); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); });
    expect(writeText).toHaveBeenCalledTimes(2);
    await act(async () => { historyKey(); await vi.advanceTimersByTimeAsync(0); });
    expect(instance.getNode("plan_2")).toBeUndefined(); expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    await act(async () => { historyKey(true); await vi.advanceTimersByTimeAsync(0); });
    expect(instance.getNode("plan_2")).toBeDefined(); expect(instance.getNode("done_2")).toBeDefined();
    expect(loadManualLayout("weekly_report").done_2.x - loadManualLayout("weekly_report").plan_2.x).toBe(2080);
    expect(postedChangeSets()).toEqual([]);
  } finally {
    vi.useRealTimers(); restoreViewport?.();
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);

it("retires pending paste geometry through a project view epoch A→B→A without late fitting or placement", async () => {
  installWorkflow(); saveArrangeMode("weekly_report", "auto");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; }); const readText = vi.fn(async () => clip);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText } });
  const restorers: Array<() => void> = [];
  try {
    const entry = "/registry/workflows/weekly_report/builder?project_id=project_alpha";
    const view = renderPage({ surface: "builder", initialEntry: entry });
    await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: -1600, y: 24, zoom: 1 }, { duration: 0 }); });
    const staleViewport = vi.spyOn(instance, "setViewport"); restorers.push(() => staleViewport.mockRestore());
    viewportProbe.holdCreated = true;
    vi.useFakeTimers();
    fireEvent.pointerLeave(screen.getByRole("region", { name: "Workflow canvas" }));
    await act(async () => { pressCanvasKey("KeyV"); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)");
    expect(readText).toHaveBeenCalledTimes(1); expect(staleViewport).not.toHaveBeenCalled();
    await act(async () => { await view.router.navigate(entry.replace("project_alpha", "project_beta")); await vi.advanceTimersByTimeAsync(100); });
    expect(view.router.state.location.search).toContain("project_id=project_beta");
    await act(async () => { await view.router.navigate(entry); await vi.advanceTimersByTimeAsync(100); });
    expect(view.router.state.location.search).toContain("project_id=project_alpha");
    expect(viewportProbe.instance).not.toBeNull();
    const current = viewportProbe.instance!;
    // Same workflow may retain its component or remount. Observe both instances;
    // initialization fits during navigation are outside the pending-paste proof.
    const currentViewport = current === instance ? staleViewport : vi.spyOn(current, "setViewport");
    if (current !== instance) restorers.push(() => currentViewport.mockRestore());
    staleViewport.mockClear(); currentViewport.mockClear();
    const afterNavigation = { ...current.getViewport() };
    viewportProbe.holdCreated = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(staleViewport).not.toHaveBeenCalled(); expect(currentViewport).not.toHaveBeenCalled();
    expect(current.getViewport()).toEqual(afterNavigation);
    expect(loadManualLayout("weekly_report").plan_2).toBeUndefined();
    // Pass the original five-second deadline after cancellation too: the old
    // owner's timeout must not reappear in the returned-to-A view.
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(staleViewport).not.toHaveBeenCalled(); expect(currentViewport).not.toHaveBeenCalled();
    expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument();
    // New epoch is usable; old busy state cannot disable a fresh Copy.
    await act(async () => { pressCanvasKey("KeyA"); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); });
    const selected = screen.getByRole("toolbar", { name: "Selected items" });
    expect(within(selected).getByRole("button", { name: "Copy" })).toBeEnabled();
    await act(async () => { pressCanvasKey("KeyC"); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); });
    expect(writeText).toHaveBeenCalledTimes(2);
    expect(readText).toHaveBeenCalledTimes(1); expect(postedChangeSets()).toEqual([]);
  } finally {
    viewportProbe.holdCreated = false; vi.useRealTimers(); for (const restore of restorers.reverse()) restore();
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);

it("never publishes a held source-owner paste into a newly mounted destination workflow", async () => {
  installWorkflow(); saveArrangeMode("weekly_report", "auto"); saveArrangeMode("destination", "auto");
  const detail = mockedFetchWorkflowDetail.getMockImplementation()!;
  const canvasGraph = mockedFetchWorkflowCanvasGraph.getMockImplementation()!;
  mockedFetchWorkflowDetail.mockImplementation(async (workflowId) => ({ ...await detail(workflowId), workflow_id: workflowId, display_name: workflowId === "weekly_report" ? "Source workflow" : "Destination workflow" }));
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (workflowId) => {
    const graph = await canvasGraph(workflowId);
    return { ...graph, workflow: { ...graph.workflow, id: workflowId, display_name: workflowId === "weekly_report" ? "Source workflow" : "Destination workflow" } };
  });
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; }); const readText = vi.fn(async () => clip);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText } });
  const restorers: Array<() => void> = [];
  try {
    const { router } = renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const source = await realViewport(); const staleViewport = vi.spyOn(source, "setViewport"); restorers.push(() => staleViewport.mockRestore());
    viewportProbe.holdCreated = true;
    fireEvent.pointerLeave(screen.getByRole("region", { name: "Workflow canvas" })); pressCanvasKey("KeyV");
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)"));
    expect(staleViewport).not.toHaveBeenCalled();
    await act(async () => { await router.navigate("/registry/workflows/destination/builder"); });
    const leave = await screen.findByRole("dialog", { name: "Leave the builder?" });
    fireEvent.click(within(leave).getByRole("button", { name: "Leave and discard" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/registry/workflows/destination/builder"));
    await screen.findAllByText("Destination workflow");
    await waitFor(() => expect(viewportProbe.instance).not.toBe(source));
    const destination = await realViewport();
    await act(async () => { await new Promise<void>((resolve) => window.requestAnimationFrame(() => { window.requestAnimationFrame(() => resolve()); })); });
    const destinationViewport = vi.spyOn(destination, "setViewport"); restorers.push(() => destinationViewport.mockRestore());
    const afterNavigation = { ...destination.getViewport() };
    viewportProbe.holdCreated = false;
    await act(async () => { await new Promise<void>((resolve) => window.requestAnimationFrame(() => { window.requestAnimationFrame(() => resolve()); })); });
    expect(staleViewport).not.toHaveBeenCalled(); expect(destinationViewport).not.toHaveBeenCalled();
    expect(destination.getViewport()).toEqual(afterNavigation);
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    expect(destination.getNode("plan_2")).toBeUndefined(); expect(destination.getNode("done_2")).toBeUndefined();
    expect(loadManualLayout("destination").plan_2).toBeUndefined(); expect(loadManualLayout("weekly_report").plan_2).toBeUndefined();
    expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument();
    // A fresh destination paste may reuse plan_2, but uses only the new owner and new intent.
    fireEvent.pointerLeave(screen.getByRole("region", { name: "Workflow canvas" })); pressCanvasKey("KeyV");
    await waitFor(() => expect(readText).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(destinationViewport).toHaveBeenCalledTimes(1));
    await waitFor(() => expectPublishedInside(destination, ["plan_2", "done_2"]));
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)");
    expect(loadManualLayout("destination").plan_2).toBeUndefined(); expect(postedChangeSets()).toEqual([]);
  } finally {
    viewportProbe.holdCreated = false; for (const restore of restorers.reverse()) restore();
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);


it.each([0.5, 1, 1.5].flatMap((zoom) => ["header", "inspector"].map((leaveTo) => ({ zoom, leaveTo }))))("bounds only a clipped manual offset batch after $leaveTo leave at zoom $zoom", async ({ zoom, leaveTo }) => {
  installWorkflow();
  Object.defineProperty(globalThis, "innerWidth", { configurable: true, value: 1440, writable: true });
  const originals = { plan: { x: 320, y: 160 }, done: { x: 640, y: 208 } };
  saveManualLayout("weekly_report", originals); saveArrangeMode("weekly_report", "manual");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; }); const readText = vi.fn(async () => clip);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText } });
  let restoreViewport: (() => void) | undefined;
  try {
    renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = JSON.parse(clip) as BuilderClipboardV1;
    expect(copied.definition.graph.edges).toEqual(edges);
    const instance = await realViewport();
    let drawerWidth = 0;
    if (leaveTo === "inspector") {
      clickCard(screen.getByRole("group", { name: "Open step Plan work" }));
      const drawer = await screen.findByRole("dialog", { name: "Step editor" });
      drawerWidth = Number(drawer.getAttribute("data-width"));
      expect(drawerWidth).toBeGreaterThan(0);
    }
    await act(async () => { await instance.setViewport({ x: 24, y: 24, zoom }, { duration: 0 }); });
    const beforeViewport = { ...instance.getViewport() };
    const setViewport = vi.spyOn(instance, "setViewport"); restoreViewport = () => setViewport.mockRestore();
    const stage = screen.getByRole("region", { name: "Workflow canvas" });
    const stageRect = stage.getBoundingClientRect();
    // Contract control: no-pointer Offset retains the existing full-stage center
    // and the command's first one-grid offset, including when the drawer is open.
    const center = instance.screenToFlowPosition({ x: stageRect.left + stageRect.width / 2, y: stageRect.top + stageRect.height / 2 });
    const anchor = { x: Math.round(center.x / 16) * 16 + 16, y: Math.round(center.y / 16) * 16 + 16 };
    const requested = Object.fromEntries(Object.entries(copied.relativeWorldPositions).map(([id, point]) => [id, { x: anchor.x + point.x, y: anchor.y + point.y }]));
    const right = 1440 - drawerWidth - 16; const bottom = 884;
    const controlRects = () => canvasControlRects(stage);
    const clearOfControls = (card: { left: number; top: number; right: number; bottom: number }) => !controlRects().some((control) =>
      card.left < control.right && card.right > control.left && card.top < control.bottom && card.bottom > control.top);
    const rawScreen = ["plan", "done"].map((id) => {
      const size = instance.getNodesBounds([id]); const point = requested[id];
      return { left: point.x * zoom + 24, top: point.y * zoom + 24 + 80,
        right: (point.x + size.width) * zoom + 24, bottom: (point.y + size.height) * zoom + 24 + 80 };
    });
    const alreadyVisible = rawScreen.every((rect) => rect.left >= 16 && rect.top >= 96 && rect.right <= right && rect.bottom <= bottom && clearOfControls(rect));
    const expectCardsClear = () => {
      const view = instance.getViewport();
      for (const id of ["plan_2", "done_2"]) {
        const rect = instance.getNodesBounds([id]);
        expect(clearOfControls({ left: rect.x * view.zoom + view.x, top: rect.y * view.zoom + view.y + 80,
          right: (rect.x + rect.width) * view.zoom + view.x, bottom: (rect.y + rect.height) * view.zoom + view.y + 80 })).toBe(true);
      }
    };
    fireEvent.pointerLeave(stage); pressCanvasKey("KeyV");
    await waitFor(() => expect(readText).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)"));
    await waitFor(() => { expectPublishedInside(instance, ["plan_2", "done_2"], right, bottom); expectCardsClear(); });
    const stored = loadManualLayout("weekly_report");
    const delta = { x: stored.plan_2.x - requested.plan.x, y: stored.plan_2.y - requested.plan.y };
    expect(stored.done_2.x - requested.done.x).toBeCloseTo(delta.x);
    expect(stored.done_2.y - requested.done.y).toBeCloseTo(delta.y);
    expect(delta.x / 16).toBe(Math.trunc(delta.x / 16));
    expect(delta.y / 16).toBe(Math.trunc(delta.y / 16));
    expect(stored.done_2.x - stored.plan_2.x).toBeCloseTo(320);
    expect(stored.done_2.y - stored.plan_2.y).toBeCloseTo(48);
    if (alreadyVisible) {
      // A visible offset anchor is exact even if it overlaps a source card.
      // No nearest-free/collision search is authorized by this visibility fix.
      expect(delta).toEqual({ x: 0, y: 0 });
      expect(stored.plan_2).toEqual(requested.plan); expect(stored.done_2).toEqual(requested.done);
    } else {
      expect(Math.abs(delta.x) + Math.abs(delta.y)).toBeGreaterThan(0);
    }
    expect(stored.plan).toEqual(originals.plan); expect(stored.done).toEqual(originals.done);
    expect(loadArrangeMode("weekly_report", true)).toBe("manual");
    expect(instance.getViewport()).toEqual(beforeViewport); expect(setViewport).not.toHaveBeenCalled();
    const afterPaste = { plan: stored.plan_2, done: stored.done_2 };
    historyKey(); await waitFor(() => expect(instance.getNode("plan_2")).toBeUndefined());
    historyKey(true); await waitFor(() => { expectPublishedInside(instance, ["plan_2", "done_2"], right, bottom); expectCardsClear(); });
    expect(loadManualLayout("weekly_report").plan_2).toEqual(afterPaste.plan);
    expect(loadManualLayout("weekly_report").done_2).toEqual(afterPaste.done);
    expect(postedChangeSets()).toEqual([]);
  } finally {
    restoreViewport?.();
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);


it("places a tall clipped pointer batch beside the actual controls with one common grid delta", async () => {
  installWorkflow();
  const originals = { plan: { x: 320, y: 160 }, done: { x: 320, y: 720 } };
  saveManualLayout("weekly_report", originals); saveArrangeMode("weekly_report", "manual");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn(async () => clip) } });
  try {
    renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = JSON.parse(clip) as BuilderClipboardV1;
    expect(copied.relativeWorldPositions.done.y - copied.relativeWorldPositions.plan.y).toBe(560);
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: 24, y: 24, zoom: 1 }, { duration: 0 }); });
    const stage = screen.getByRole("region", { name: "Workflow canvas" });
    // This pointer is inside the real stage, just below its top and near its right edge.
    fireEvent.pointerMove(stage, { clientX: 1400, clientY: 104 }); pressCanvasKey("KeyV");
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)"));
    await waitFor(() => expectPublishedInside(instance, ["plan_2", "done_2"], 1424, 884));
    const stored = loadManualLayout("weekly_report");
    // Requested world (1376,0)/(1376,560); the nearest feasible common delta
    // is (-432,0). A rectangle ending above the minimap cannot fit this height.
    expect(stored.plan_2).toEqual({ x: 944, y: 0 }); expect(stored.done_2).toEqual({ x: 944, y: 560 });
    for (const id of ["plan_2", "done_2"]) {
      const rect = instance.getNodesBounds([id]);
      const screenRect = { left: rect.x + 24, right: rect.x + rect.width + 24, top: rect.y + 104, bottom: rect.y + rect.height + 104 };
      expect(canvasControlRects(stage).some((control) => screenRect.left < control.right && screenRect.right > control.left && screenRect.top < control.bottom && screenRect.bottom > control.top)).toBe(false);
    }
    expect(stored.plan).toEqual(originals.plan); expect(stored.done).toEqual(originals.done);
    expect(loadArrangeMode("weekly_report", true)).toBe("manual");
    expect(instance.getEdges()).toEqual(expect.arrayContaining([expect.objectContaining({ source: "plan_2", target: "done_2" })]));
    historyKey(); await waitFor(() => expect(instance.getNode("plan_2")).toBeUndefined());
    historyKey(true); await waitFor(() => expect(instance.getNode("plan_2")).toBeDefined());
    expect(loadManualLayout("weekly_report").plan_2).toEqual(stored.plan_2);
    expect(loadManualLayout("weekly_report").done_2).toEqual(stored.done_2);
    expect(postedChangeSets()).toEqual([]);
  } finally {
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);

it("recomputes a pending pointer translation when positive published heights grow from 144 to 320", async () => {
  installWorkflow();
  const originals = { plan: { x: 320, y: 160 }, done: { x: 640, y: 208 } };
  saveManualLayout("weekly_report", originals); saveArrangeMode("weekly_report", "manual");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn(async () => clip) } });
  try {
    renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: 24, y: 24, zoom: 1 }, { duration: 0 }); });
    viewportProbe.measuredSize = { width: 224, height: 144 }; viewportProbe.growManualHeight = 320;
    vi.useFakeTimers();
    fireEvent.pointerMove(screen.getByRole("region", { name: "Workflow canvas" }), { clientX: 720, clientY: 853 });
    await act(async () => { pressCanvasKey("KeyV"); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)");
    expect(viewportProbe.manualReads.length).toBeGreaterThanOrEqual(2);
    const first = viewportProbe.manualReads[0]; const stored = loadManualLayout("weekly_report");
    expect(stored.plan_2.y).toBeLessThan(first.y);
    expect((stored.plan_2.x - first.x) / 16).toBe(Math.trunc((stored.plan_2.x - first.x) / 16));
    expect((stored.plan_2.y - first.y) / 16).toBe(Math.trunc((stored.plan_2.y - first.y) / 16));
    expect(stored.done_2.x - stored.plan_2.x).toBe(320); expect(stored.done_2.y - stored.plan_2.y).toBe(48);
    expectPublishedInside(instance, ["plan_2", "done_2"]);
    expect(stored.plan).toEqual(originals.plan); expect(stored.done).toEqual(originals.done);
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument();
    expect(loadArrangeMode("weekly_report", true)).toBe("manual"); expect(postedChangeSets()).toEqual([]);
  } finally {
    vi.useRealTimers();
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);

it("refits changed positive auto bounds without treating its programmatic null move event as user intent", async () => {
  installWorkflow(); saveArrangeMode("weekly_report", "auto");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn(async () => clip) } });
  let restoreViewport: (() => void) | undefined;
  try {
    renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: -1600, y: 24, zoom: 1 }, { duration: 0 }); });
    viewportProbe.measuredSize = { width: 224, height: 144 }; viewportProbe.moveEvents = [];
    const setViewport = instance.setViewport;
    const fits = vi.spyOn(instance, "setViewport").mockImplementation((viewport, options) => {
      const result = setViewport(viewport, options);
      // The first positive estimate is replaced before its verification frame.
      // Exercise the public null-event contract explicitly even with zero-duration transforms.
      viewportProbe.moveStart?.(null, viewport);
      viewportProbe.measuredSize = { width: 224, height: 600 };
      return result;
    });
    restoreViewport = () => fits.mockRestore(); vi.useFakeTimers();
    fireEvent.pointerLeave(screen.getByRole("region", { name: "Workflow canvas" }));
    await act(async () => { pressCanvasKey("KeyV"); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)");
    expect(fits).toHaveBeenCalledTimes(2);
    expect(fits.mock.calls[1][0].zoom).toBeLessThan(fits.mock.calls[0][0].zoom);
    expect(viewportProbe.moveEvents).toContain("programmatic"); expect(viewportProbe.moveEvents).not.toContain("user");
    expectPublishedInside(instance, ["plan_2", "done_2"]);
    expect(loadManualLayout("weekly_report").plan_2).toBeUndefined(); expect(loadArrangeMode("weekly_report", false)).toBe("auto");
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument();
    expect(postedChangeSets()).toEqual([]);
  } finally {
    vi.useRealTimers(); restoreViewport?.();
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);

it.each(["pane-pan", "zoom-control"] as const)("retires pending auto reveal after a real %s, preserving the newer camera", async (action) => {
  if (action === "zoom-control") vi.useRealTimers();
  installWorkflow(); saveArrangeMode("weekly_report", "auto");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn(async () => clip) } });
  let restoreViewport: (() => void) | undefined;
  let restoreRendererSize: (() => void) | undefined;
  try {
    renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const instance = await realViewport();
    const stage = screen.getByRole("region", { name: "Workflow canvas" });
    const renderer = canvasElementByAttribute(stage, "class", /(?:^|\s)react-flow__renderer(?:\s|$)/);
    const stageRect = stage.getBoundingClientRect();
    // D3's animated zoom reads client dimensions, which JSDOM otherwise leaves at zero.
    const width = vi.spyOn(renderer, "clientWidth", "get").mockReturnValue(stageRect.width);
    const height = vi.spyOn(renderer, "clientHeight", "get").mockReturnValue(stageRect.height);
    restoreRendererSize = () => { width.mockRestore(); height.mockRestore(); };
    await act(async () => { await instance.setViewport({ x: 24, y: 24, zoom: 1 }, { duration: 0 }); });
    expect(instance.getViewport()).toEqual({ x: 24, y: 24, zoom: 1 });
    viewportProbe.holdSizes = true; viewportProbe.moveEvents = [];
    const fits = vi.spyOn(instance, "setViewport"); restoreViewport = () => fits.mockRestore();
    if (action === "pane-pan") vi.useFakeTimers();
    fireEvent.pointerLeave(stage);
    await act(async () => {
      pressCanvasKey("KeyV"); await Promise.resolve();
      if (action === "pane-pan") await vi.advanceTimersByTimeAsync(50);
      else await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)"); expect(fits).not.toHaveBeenCalled();
    expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument();
    if (action === "pane-pan") {
      const pane = canvasElementByAttribute(stage, "class", /(?:^|\s)react-flow__pane(?:\s|$)/);
      const nativeRealm = (globalThis as typeof globalThis & { jsdom?: { window: Window } }).jsdom?.window;
      if (!nativeRealm) throw new Error("Pane pan requires Vitest's existing JSDOM Window.");
      const beforePan = { ...instance.getViewport() };
      fireEvent.mouseDown(pane, { view: nativeRealm, button: 1, buttons: 4, clientX: 600, clientY: 400 });
      fireEvent.mouseMove(nativeRealm, { view: nativeRealm, button: 1, buttons: 4, clientX: 640, clientY: 420 });
      fireEvent.mouseUp(nativeRealm, { view: nativeRealm, button: 1, buttons: 0, clientX: 640, clientY: 420 });
      expect(viewportProbe.moveEvents).toContain("user");
      expect(Math.abs(instance.getViewport().x - beforePan.x) + Math.abs(instance.getViewport().y - beforePan.y)).toBeGreaterThan(0);
    } else {
      const beforeZoom = instance.getZoom();
      fireEvent.click(screen.getByRole("button", { name: "Zoom in (+)" }));
      // The real control uses a separate useReactFlow API object; observe the
      // completed public viewport on real timers instead of spying on that object.
      await waitFor(() => expect(instance.getZoom()).toBe(1.25), { interval: 10 });
      expect(instance.getZoom()).toBe(1.25);
      expect(Object.values(instance.getViewport()).every(Number.isFinite)).toBe(true);
      expect(instance.getZoom()).toBeGreaterThan(beforeZoom);
      // User controls use the same null-event imperative API as automatic fit;
      // their action boundary, rather than that event alone, owns cancellation.
      expect(viewportProbe.moveEvents).toContain("programmatic");
    }
    const afterManualCamera = { ...instance.getViewport() }; viewportProbe.holdSizes = false;
    if (action === "pane-pan") {
      await act(async () => { await vi.advanceTimersByTimeAsync(5_100); });
    } else {
      // Keep the real clock running through the pending auto-reveal deadline.
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5_100)); });
    }
    expect(fits).not.toHaveBeenCalled(); expect(instance.getViewport()).toEqual(afterManualCamera);
    expect(loadManualLayout("weekly_report").plan_2).toBeUndefined();
    expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument(); expect(postedChangeSets()).toEqual([]);
  } finally {
    vi.useRealTimers(); restoreViewport?.(); restoreRendererSize?.();
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);

it("preserves a newer keyboard placement after Undo returns its pasted subject to identical coordinates", async () => {
  installWorkflow();
  const originals = { plan: { x: 320, y: 160 }, done: { x: 640, y: 208 } };
  saveManualLayout("weekly_report", originals); saveArrangeMode("weekly_report", "manual");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn(async () => clip) } });
  try {
    renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: 24, y: 24, zoom: 1 }, { duration: 0 }); });
    const canvas = screen.getByRole("region", { name: "Workflow canvas" });
    await clearOriginalPairSelection(canvas);
    viewportProbe.holdAfterManual = true;
    vi.useFakeTimers();
    fireEvent.pointerMove(canvas, { clientX: 720, clientY: 853 });
    await act(async () => { pressCanvasKey("KeyV"); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)");
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    const before = { ...loadManualLayout("weekly_report").plan_2 }; expect(before.x).toEqual(expect.any(Number));
    const copy = canvasElementByAttribute(canvas, "data-id", "plan_2");
    // Focus alone leaves the command selection unchanged. The real canvas
    // key handler nudges this focused subject, so selection cancellation cannot
    // mask a missing per-subject gesture-generation guard.
    copy.focus();
    fireEvent.keyDown(copy, { key: "ArrowRight", code: "ArrowRight", shiftKey: true });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(loadManualLayout("weekly_report").plan_2).not.toEqual(before);
    await act(async () => { historyKey(); await vi.advanceTimersByTimeAsync(0); });
    expect(loadManualLayout("weekly_report").plan_2).toEqual(before);
    // Releasing larger geometry now must not infer ownership from equal XY.
    viewportProbe.holdAfterManual = false; viewportProbe.measuredSize = { width: 224, height: 320 };
    await act(async () => { await vi.advanceTimersByTimeAsync(5_100); });
    expect(loadManualLayout("weekly_report").plan_2).toEqual(before);
    expect(instance.getNode("plan_2")).toBeDefined(); expect(loadManualLayout("weekly_report").plan).toEqual(originals.plan);
    expect(screen.queryByText("The copy was added, but its canvas placement needs attention.")).not.toBeInTheDocument(); expect(postedChangeSets()).toEqual([]);
  } finally {
    vi.useRealTimers();
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);


it.each(["pointer", "offset"] as const)("publishes accepted %s manual geometry after an unrelated later selection, preserving that selection", async (mode) => {
  installWorkflow();
  const originals = { plan: { x: 320, y: 160 }, done: { x: 640, y: 208 } };
  saveManualLayout("weekly_report", originals); saveArrangeMode("weekly_report", "manual");
  let clip = ""; const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const writeText = vi.fn(async (text: string) => { clip = text; });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText, readText: vi.fn(async () => clip) } });
  try {
    renderPage({ surface: "builder" }); await copyPair(); await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const instance = await realViewport();
    await act(async () => { await instance.setViewport({ x: 24, y: 24, zoom: 1 }, { duration: 0 }); });
    const canvas = screen.getByRole("region", { name: "Workflow canvas" }); viewportProbe.holdCreated = true;
    if (mode === "pointer") fireEvent.pointerMove(canvas, { clientX: 720, clientY: 853 });
    else fireEvent.pointerLeave(canvas);
    pressCanvasKey("KeyV");
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 3 change(s)"));
    await waitFor(() => expect(instance.getEdges()).toEqual(expect.arrayContaining([expect.objectContaining({ source: "plan_2", target: "done_2", sourceHandle: "success", targetHandle: "in" })])));
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 50)); });
    expect(instance.getNode("plan_2")).toBeDefined(); expect(instance.getNodes().some((node) => node.id === "plan_2")).toBe(false);
    expect(loadManualLayout("weekly_report").plan_2).toBeUndefined(); expect(loadManualLayout("weekly_report").done_2).toBeUndefined();
    // Selection changes only an old card. It grants no placement ownership
    // over the held new subjects, before or during their functional state write.
    const oldPlan = await selectOnlyOriginalPlan(canvas);
    await waitFor(() => expect(oldPlan).toHaveClass("selected"));
    expect(canvasElementByAttribute(canvas, "data-id", "done")).not.toHaveClass("selected");
    viewportProbe.holdCreated = false;
    await waitFor(() => expectPublishedInside(instance, ["plan_2", "done_2"]));
    const stored = loadManualLayout("weekly_report");
    expect(stored.plan_2).toEqual({ x: expect.any(Number), y: expect.any(Number) });
    expect(stored.done_2.x - stored.plan_2.x).toBe(320); expect(stored.done_2.y - stored.plan_2.y).toBe(48);
    expect(stored.plan).toEqual(originals.plan); expect(stored.done).toEqual(originals.done);
    // Older automatic selection must not take the user's selection back.
    expect(oldPlan).toHaveClass("selected");
    expect(canvasElementByAttribute(canvas, "data-id", "plan_2")).not.toHaveClass("selected");
    expect(canvasElementByAttribute(canvas, "data-id", "done_2")).not.toHaveClass("selected");
    expect(loadArrangeMode("weekly_report", true)).toBe("manual"); expect(postedChangeSets()).toEqual([]);
  } finally {
    viewportProbe.holdCreated = false;
    if (original) Object.defineProperty(navigator, "clipboard", original); else Reflect.deleteProperty(navigator, "clipboard");
  }
}, 15_000);
