import type { Edge, Node } from "@xyflow/react";
import { describe, expect, it, vi } from "vitest";
import { CANVAS_GRID_STEP, historyKey, isTextEntryTarget, modalDialogOpen, nextZoomStop, runCanvasKey, walkCanvasGraph, type CanvasKeyContext, type CanvasWalkMemory } from "./canvas-keyboard";

function context(overrides: Partial<CanvasKeyContext> = {}): CanvasKeyContext {
  return {
    structureLocked: false,
    fit: vi.fn(), zoomTo100: vi.fn(), zoomStep: vi.fn(), openSearch: vi.fn(), addStep: vi.fn(), autoLayout: vi.fn(), selectAll: vi.fn(),
    escape: vi.fn(() => true), deleteSelection: vi.fn(() => true), walk: vi.fn(() => true), nudge: vi.fn(() => true),
    copy: vi.fn(() => true), cut: vi.fn(() => true), paste: vi.fn(() => true), duplicate: vi.fn(() => true),
    undo: vi.fn(() => true), redo: vi.fn(() => true),
    ...overrides,
  };
}
const key = (code: string, extra: Partial<KeyboardEvent> = {}) => ({ code, key: extra.key ?? code, shiftKey: false, altKey: false, metaKey: false, ctrlKey: false, ...extra });

describe("canvas keys (Canvas › Keys)", () => {
  it("zooms by stops between 25% and 200%", () => {
    expect(nextZoomStop(1, 1)).toBe(1.25);
    expect(nextZoomStop(1, -1)).toBe(0.75);
    expect(nextZoomStop(0.6, 1)).toBe(0.67);
    expect(nextZoomStop(2, 1)).toBe(2);
    expect(nextZoomStop(0.25, -1)).toBe(0.25);
  });

  it("maps the design-tool keys by layout-independent codes", () => {
    const c = context();
    expect(runCanvasKey(key("Digit1", { shiftKey: true }), c)).toBe(true);
    expect(c.fit).toHaveBeenCalledWith("fit");
    expect(runCanvasKey(key("Digit2", { shiftKey: true }), c)).toBe(true);
    expect(c.fit).toHaveBeenCalledWith("selection");
    runCanvasKey(key("Equal", { key: "+", shiftKey: true }), c);
    expect(c.zoomStep).toHaveBeenCalledWith(1);
    runCanvasKey(key("Minus", { key: "-" }), c);
    expect(c.zoomStep).toHaveBeenCalledWith(-1);
    runCanvasKey(key("KeyA", { key: "ф" }), c);
    expect(c.addStep).toHaveBeenCalledTimes(1);
    runCanvasKey(key("KeyA", { shiftKey: true }), c);
    expect(c.autoLayout).toHaveBeenCalledTimes(1);
    for (const [code, name] of [["KeyC", "copy"], ["KeyX", "cut"], ["KeyV", "paste"], ["KeyD", "duplicate"], ["KeyZ", "undo"], ["KeyY", "redo"]] as const) {
      expect(runCanvasKey(key(code, { metaKey: true }), c)).toBe(true);
      expect(c[name]).toHaveBeenCalledTimes(1);
    }
    runCanvasKey(key("KeyZ", { ctrlKey: true, shiftKey: true }), c);
    expect(c.redo).toHaveBeenCalledTimes(2);
    expect(runCanvasKey(key("Delete", { key: "Delete" }), c)).toBe(true);
    expect(runCanvasKey(key("Escape", { key: "Escape" }), c)).toBe(true);
  });

  it("walks with the arrows and moves the selection one grid step with Shift+arrows", () => {
    const c = context();
    runCanvasKey(key("ArrowRight"), c);
    expect(c.walk).toHaveBeenCalledWith("right");
    runCanvasKey(key("ArrowUp", { shiftKey: true }), c);
    expect(c.nudge).toHaveBeenCalledWith(0, -CANVAS_GRID_STEP);
    expect(CANVAS_GRID_STEP).toBe(16);
  });

  it("keeps editing keys off a protected structure and off focused controls", () => {
    const locked = context({ structureLocked: true });
    expect(runCanvasKey(key("KeyA"), locked)).toBe(false);
    expect(runCanvasKey(key("KeyV", { metaKey: true }), locked)).toBe(false);
    expect(runCanvasKey(key("ArrowLeft", { shiftKey: true }), locked)).toBe(false);
    expect(runCanvasKey(key("KeyC", { metaKey: true }), locked)).toBe(true);
    const onControl = context();
    expect(runCanvasKey(key("Delete", { key: "Delete" }), onControl, true)).toBe(false);
    expect(runCanvasKey(key("Digit1", { shiftKey: true }), onControl, true)).toBe(true);
  });

  it("leaves N and ⌘⇧G to the areas that own them until they plug in", () => {
    const c = context();
    expect(runCanvasKey(key("KeyN"), c)).toBe(false);
    expect(runCanvasKey(key("KeyG", { metaKey: true, shiftKey: true }), c)).toBe(false);
    const addNote = vi.fn(); const groupSelection = vi.fn(() => true);
    const owned = context({ addNote, groupSelection });
    expect(runCanvasKey(key("KeyN"), owned)).toBe(true);
    expect(runCanvasKey(key("KeyG", { metaKey: true, shiftKey: true }), owned)).toBe(true);
    expect(addNote).toHaveBeenCalledTimes(1);
    expect(groupSelection).toHaveBeenCalledTimes(1);
  });
});

const node = (id: string, x: number, y: number, extra: Partial<Node> = {}): Node => ({ id, position: { x, y }, width: 224, height: 80, data: {}, ...extra });
const edge = (source: string, target: string, sourceHandle = "success", targetHandle = "in"): Edge => ({ id: `${source}:${sourceHandle}`, source, target, sourceHandle, targetHandle });

describe("walking the graph (Canvas › Walking the graph)", () => {
  const nodes = [
    node("visual:start", 0, 0, { data: { entryNodeId: "check" } }),
    node("check", 320, 0, { data: { step: { ports: [{ outcome: { name: "true" } }, { outcome: { name: "false" } }] } } }),
    node("yes", 640, 0), node("no", 640, 160),
    node("group:loop", 960, 0, { width: 400, height: 300 }),
    node("inner", 32, 128, { parentId: "group:loop", data: { isEntry: true } }),
  ];
  const edges = [edge("check", "no", "false"), edge("check", "yes", "true"), edge("yes", "group:loop", "success", "entry"), edge("inner", "group:loop", "success", "exit")];
  const walk = (current: string | null, direction: "right" | "left" | "up" | "down", memory: CanvasWalkMemory) =>
    walkCanvasGraph({ current, direction, nodes, edges, memory });

  it("picks the entry when nothing is selected, then follows True first", () => {
    const memory: CanvasWalkMemory = { back: {}, out: null };
    expect(walk(null, "right", memory)).toBe("check");
    expect(walk("check", "right", memory)).toBe("yes");
    expect(walk("yes", "left", memory)).toBe("check");
  });

  it("moves between the nodes one step leads to with ↑ ↓", () => {
    const memory: CanvasWalkMemory = { back: { yes: "check" }, out: null };
    expect(walk("yes", "down", memory)).toBe("no");
    expect(walk("no", "down", memory)).toBe("yes");
  });

  it("goes into a group, returns to it after its last step, then follows the group's own connection", () => {
    const memory: CanvasWalkMemory = { back: {}, out: null };
    expect(walk("group:loop", "right", memory)).toBe("inner");
    expect(walk("inner", "right", memory)).toBe("group:loop");
    expect(memory.out).toBe("group:loop");
    expect(walk("group:loop", "right", memory)).toBeNull();
  });
});

describe("arrows on a focused control", () => {
  it("leave the control its own arrows", () => {
    const c = context();
    expect(runCanvasKey(key("ArrowRight"), c, true)).toBe(false);
    expect(c.walk).not.toHaveBeenCalled();
  });
});

describe("Undo and Redo keys (Canvas › Undo)", () => {
  // The physical key decides, so a Russian layout (я = KeyZ, н = KeyY) works the same.
  it.each([
    ["Ctrl+Z", { code: "KeyZ", key: "z", ctrlKey: true }, "undo"],
    ["⌘Z", { code: "KeyZ", key: "z", metaKey: true }, "undo"],
    ["Ctrl+Y", { code: "KeyY", key: "y", ctrlKey: true }, "redo"],
    ["⌘Y", { code: "KeyY", key: "y", metaKey: true }, "redo"],
    ["Ctrl+Shift+Z", { code: "KeyZ", key: "Z", ctrlKey: true, shiftKey: true }, "redo"],
    ["⌘Shift+Z", { code: "KeyZ", key: "Z", metaKey: true, shiftKey: true }, "redo"],
    ["Ctrl+Z on a Russian layout", { code: "KeyZ", key: "я", ctrlKey: true }, "undo"],
    ["⌘Z on a Russian layout", { code: "KeyZ", key: "я", metaKey: true }, "undo"],
    ["Ctrl+Y on a Russian layout", { code: "KeyY", key: "н", ctrlKey: true }, "redo"],
    ["⌘Shift+Z on a Russian layout", { code: "KeyZ", key: "Я", metaKey: true, shiftKey: true }, "redo"],
  ] as const)("%s", (_name, extra, action) => {
    const event = key(extra.code, extra);
    expect(historyKey(event)).toBe(action);
    const c = context();
    expect(runCanvasKey(event, c)).toBe(true);
    expect(c[action]).toHaveBeenCalledTimes(1);
  });

  it("is not Z alone, Alt+Z or a key that only prints z", () => {
    expect(historyKey(key("KeyZ", { key: "z" }))).toBeNull();
    expect(historyKey(key("KeyZ", { key: "z", ctrlKey: true, altKey: true }))).toBeNull();
    expect(historyKey(key("KeyX", { key: "z", ctrlKey: true }))).toBeNull();
  });

  it("runs on a protected structure and from a focused canvas control", () => {
    const locked = context({ structureLocked: true });
    expect(runCanvasKey(key("KeyZ", { metaKey: true }), locked)).toBe(true);
    const onControl = context();
    expect(runCanvasKey(key("KeyZ", { ctrlKey: true }), onControl, true)).toBe(true);
    expect(runCanvasKey(key("KeyY", { ctrlKey: true }), onControl, true)).toBe(true);
    expect(onControl.undo).toHaveBeenCalledTimes(1);
    expect(onControl.redo).toHaveBeenCalledTimes(1);
  });

  it("leaves text fields, code editors and modal dialogs their own keys", () => {
    const host = document.createElement("div");
    host.innerHTML = [
      '<input data-id="text" type="text"><input data-id="box" type="checkbox"><textarea data-id="area"></textarea>',
      '<div class="cm-editor"><div data-id="code" tabindex="0"></div></div>',
      '<div contenteditable="true" data-id="rich"></div><button data-id="button" type="button">Save</button>',
    ].join("");
    document.body.append(host);
    const at = (id: string) => host.querySelector(`[data-id="${id}"]`);
    try {
      expect(isTextEntryTarget(at("text"))).toBe(true);
      expect(isTextEntryTarget(at("area"))).toBe(true);
      expect(isTextEntryTarget(at("code"))).toBe(true);
      expect(isTextEntryTarget(at("rich"))).toBe(true);
      expect(isTextEntryTarget(at("box"))).toBe(false);
      expect(isTextEntryTarget(at("button"))).toBe(false);
      expect(modalDialogOpen(host)).toBe(false);
      host.insertAdjacentHTML("beforeend", '<dialog open data-id="drawer"></dialog>');
      expect(modalDialogOpen(host)).toBe(false); // The builder drawer is not modal.
      host.insertAdjacentHTML("beforeend", '<dialog open aria-modal="true"></dialog>');
      expect(modalDialogOpen(host)).toBe(true);
    } finally {
      host.remove();
    }
  });
});
