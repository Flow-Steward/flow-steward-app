import { fireEvent, render, screen } from "@testing-library/react";
import type { Node } from "@xyflow/react";
import { describe, expect, it, vi } from "vitest";
import { fallbackContext } from "@/lib/i18n/context";
import { CanvasConnectionBar, CanvasEdgeBeads } from "./canvas-edge-actions";
import { canvasEdgeNames } from "./canvas-edge-labels";

describe("connection actions (Components2 › Connection actions)", () => {
  it("offers Insert a step here and Remove connection at the hovered line's middle; Remove acts at once", () => {
    const onInsert = vi.fn(); const onRemove = vi.fn(); const onHoverChange = vi.fn();
    render(<CanvasEdgeBeads midpoint={{ x: 120, y: 40 }} visible onInsert={onInsert} onRemove={onRemove} onHoverChange={onHoverChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove connection" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Insert a step here" }));
    expect(onInsert).toHaveBeenCalledTimes(1);
  });

  it("names the selected connection in words, its ids beneath, and closes by its ×", () => {
    const step = (id: string, name: string, ports: unknown[] = []) => ({ id, position: { x: 0, y: 0 }, data: { step: { id, step_id: id, display_name: name, ports } } }) as Node;
    const nodes = new Map<string, Node>([
      ["classify", step("classify", "Classify request", [{ outcome: { name: "success" }, label: "Next" }])],
      ["is_priority", step("is_priority", "Priority order?")],
    ]);
    const names = canvasEdgeNames({ source: { kind: "step", id: "classify" }, outcome: { name: "success" }, target: { kind: "step", id: "is_priority" } }, nodes, fallbackContext.t);
    expect(names).toEqual({ title: "Classify request · Next → Priority order?", ids: "classify · success → is_priority" });
    const onClose = vi.fn(); const onRemove = vi.fn();
    render(<CanvasConnectionBar {...names} draft onInsert={vi.fn()} onRemove={onRemove} onClose={onClose} />);
    const bar = screen.getByRole("region", { name: "Connection selected" });
    expect(bar).toHaveTextContent("Classify request · Next → Priority order?");
    expect(bar).toHaveTextContent("classify · success → is_priority");
    expect(bar).toHaveTextContent("Not applied");
    // The builder no longer marks a rework (repeat) path (owner decision, 2026-10-21).
    expect(screen.getByRole("button", { name: "Insert a step here" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /rework/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove connection" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Close (Esc)" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
