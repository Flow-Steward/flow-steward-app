import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { useState } from "react";
import type { Node } from "@xyflow/react";
import { fallbackContext } from "@/lib/i18n/context";
import type { CanvasPosition, GraphEditNotice } from "./canvas-core";
import { arrangeSnapshot, CanvasLayoutControl, loadArrangeMode, saveArrangeMode, useCanvasArrangeMode, useSavedCanvasLayout } from "./canvas-layout-menu";
import type * as notesApi from "./canvas-notes-api";
import { fetchCanvasLayout, saveCanvasLayout } from "./canvas-notes-api";

vi.mock("./canvas-notes-api", async (importOriginal) => ({
  ...(await importOriginal<typeof notesApi>()),
  fetchCanvasLayout: vi.fn(),
  saveCanvasLayout: vi.fn(async (_workflowId: string, layout: unknown) => ({ layout, saved: true })),
}));
const mockedFetchLayout = vi.mocked(fetchCanvasLayout);
const mockedSaveLayout = vi.mocked(saveCanvasLayout);

const layoutNodes = [
  { id: "visual:start", type: "workflowStart", position: { x: 0, y: 0 }, data: {} },
  { id: "fetch", type: "action", position: { x: 400, y: 64 }, data: {} },
  { id: "classify", type: "action", position: { x: 720, y: 64 }, data: {} },
  { id: "annotation:a", type: "canvasNote", position: { x: 9, y: 9 }, data: {} },
] as Node[];

afterEach(() => localStorage.clear());

describe("Layout › Arrange", () => {
  it("is kept per workflow and starts Manually only where manual places exist", () => {
    expect(loadArrangeMode("orders", false)).toBe("auto");
    expect(loadArrangeMode("orders", true)).toBe("manual");
    saveArrangeMode("orders", "auto");
    expect(loadArrangeMode("orders", true)).toBe("auto");
  });

  it("fixes every step and group where it stands, never Start or notes", () => {
    expect(arrangeSnapshot(layoutNodes, { fetch: { x: 416, y: 80 } })).toEqual({ fetch: { x: 416, y: 80 }, classify: { x: 720, y: 64 } });
  });

  it("turns to Manually on a manual placement while Automatically, keeping the others, with Undo", () => {
    const notices: GraphEditNotice[] = [];
    const undo = vi.fn();
    const { result } = renderHook(() => {
      const [positions, setPositions] = useState<Record<string, CanvasPosition>>({});
      const arrange = useCanvasArrangeMode({ workflowId: "orders", t: fallbackContext.t, manualPositions: positions, setManualPositions: setPositions, layoutNodes, setGraphEditNotice: (next) => { if (typeof next !== "function" && next) notices.push(next); }, undoLastChange: undo });
      return { arrange, positions, setPositions };
    });
    expect(result.current.arrange.mode).toBe("auto");
    act(() => result.current.setPositions({ fetch: { x: 432, y: 96 } }));
    expect(result.current.arrange.mode).toBe("manual");
    expect(result.current.positions).toEqual({ fetch: { x: 432, y: 96 }, classify: { x: 720, y: 64 } });
    expect(notices.at(-1)).toMatchObject({ label: "Layout: manually. Steps stay where you put them.", action: { label: "Undo" } });
    act(() => notices.at(-1)?.action?.onAction());
    expect(undo).toHaveBeenCalledOnce();
    expect(result.current.arrange.mode).toBe("auto");
    expect(result.current.positions).toEqual({});
    expect(loadArrangeMode("orders", true)).toBe("auto");
  });

  it("opens Arrange from the arrow, checks the current choice and closes on a choice", () => {
    const onChoose = vi.fn();
    const onAutoLayout = vi.fn();
    render(<CanvasLayoutControl mode="auto" onChoose={onChoose} onAutoLayout={onAutoLayout} help="Auto layout (Shift+A)" />);
    fireEvent.click(screen.getByRole("button", { name: "Auto layout" }));
    expect(onAutoLayout).toHaveBeenCalledOnce();
    const arrow = screen.getByRole("button", { name: "Layout" });
    fireEvent.click(arrow);
    expect(arrow).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("menuitemradio", { name: /Automatically/ })).toBeChecked();
    fireEvent.click(screen.getByRole("menuitemradio", { name: /Manually/ }));
    expect(onChoose).toHaveBeenCalledWith("manual");
    expect(screen.queryByRole("menuitemradio", { name: /Manually/ })).not.toBeInTheDocument();
  });
});

describe("Saved canvas layout", () => {
  beforeEach(() => { mockedFetchLayout.mockReset(); mockedSaveLayout.mockClear(); });

  /** The canvas's own state, with the saved layout hook on top (as the Surface wires it). */
  function renderSavedLayout(initial: Record<string, CanvasPosition>, editable = true) {
    return renderHook(() => {
      const [positions, setPositions] = useState(initial);
      const arrange = useCanvasArrangeMode({ workflowId: "orders", t: fallbackContext.t, manualPositions: positions, setManualPositions: setPositions, layoutNodes, setGraphEditNotice: () => {} });
      useSavedCanvasLayout({ workflowId: "orders", editable, mode: arrange.mode, positions, hydrate: arrange.hydrate, setManualPositions: setPositions });
      return { positions, setPositions, mode: arrange.mode };
    });
  }

  it("shows the places saved with the workflow after a new sign-in, without writing them back", async () => {
    const saved = { mode: "manual" as const, positions: { fetch: { x: 480, y: 176 } } };
    mockedFetchLayout.mockResolvedValue({ layout: saved, saved: true });
    // Signing out cleared this browser's copy: it starts with nothing.
    const { result } = renderSavedLayout({});
    await vi.waitFor(() => expect(result.current.positions).toEqual(saved.positions));
    expect(result.current.mode).toBe("manual");
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(mockedSaveLayout).not.toHaveBeenCalled();
  });

  it("saves a drag with the workflow once it settles", async () => {
    mockedFetchLayout.mockResolvedValue({ layout: { mode: "manual", positions: { fetch: { x: 400, y: 64 } } }, saved: true });
    const { result } = renderSavedLayout({});
    await vi.waitFor(() => expect(result.current.positions).toEqual({ fetch: { x: 400, y: 64 } }));
    act(() => result.current.setPositions({ fetch: { x: 640, y: 320 } }));
    await vi.waitFor(() => expect(mockedSaveLayout).toHaveBeenCalledWith("orders", { mode: "manual", positions: { fetch: { x: 640, y: 320 } } }), { timeout: 2000 });
    expect(mockedSaveLayout).toHaveBeenCalledTimes(1);
  });

  it("sends up places this browser kept from before they were saved, and keeps a saved Automatically", async () => {
    mockedFetchLayout.mockResolvedValue({ layout: { mode: "auto", positions: {} }, saved: false });
    saveArrangeMode("orders", "manual");
    renderSavedLayout({ fetch: { x: 416, y: 80 } });
    await vi.waitFor(() => expect(mockedSaveLayout).toHaveBeenCalledWith("orders", { mode: "manual", positions: { fetch: { x: 416, y: 80 } } }), { timeout: 2000 });

    mockedSaveLayout.mockClear();
    mockedFetchLayout.mockResolvedValue({ layout: { mode: "auto", positions: {} }, saved: true });
    const { result } = renderSavedLayout({ fetch: { x: 416, y: 80 } });
    await vi.waitFor(() => expect(result.current.positions).toEqual({}));
    expect(result.current.mode).toBe("auto");
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(mockedSaveLayout).not.toHaveBeenCalled();
  });

  it("never writes before the saved layout was read, nor when it could not be read or the canvas is read-only", async () => {
    mockedFetchLayout.mockRejectedValue(new Error("offline"));
    const { result: offline } = renderSavedLayout({});
    act(() => offline.current.setPositions({ fetch: { x: 1, y: 2 } }));
    mockedFetchLayout.mockResolvedValue({ layout: { mode: "manual", positions: {} }, saved: true });
    const { result: readOnly } = renderSavedLayout({}, false);
    await vi.waitFor(() => expect(readOnly.current.mode).toBe("manual"));
    act(() => readOnly.current.setPositions({ fetch: { x: 3, y: 4 } }));
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(mockedSaveLayout).not.toHaveBeenCalled();
  });
});
