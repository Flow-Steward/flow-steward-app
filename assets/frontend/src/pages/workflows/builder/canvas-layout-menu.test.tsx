import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { useState } from "react";
import type { Node } from "@xyflow/react";
import { fallbackContext } from "@/lib/i18n/context";
import type { CanvasPosition, GraphEditNotice } from "./canvas-core";
import { arrangeSnapshot, CanvasLayoutControl, loadArrangeMode, saveArrangeMode, useCanvasArrangeMode } from "./canvas-layout-menu";

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
