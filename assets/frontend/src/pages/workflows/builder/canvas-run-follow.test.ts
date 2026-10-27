import { act, renderHook } from "@testing-library/react";
import type { Node, ReactFlowInstance } from "@xyflow/react";

import { useCanvasRunFollow } from "./canvas-run-follow";

function card(id: string, status: string, x: number): Node {
  return { id, position: { x, y: 0 }, width: 200, height: 100, data: { runtimeMark: { status } } } as Node;
}

function instance() {
  return { setCenter: vi.fn(async () => true), getZoom: () => 1 } as unknown as ReactFlowInstance & { setCenter: ReturnType<typeof vi.fn> };
}

it("follows the step a run waits at, and offers Follow run once the author moves the view", () => {
  const flow = instance();
  const nodes = [card("transform", "completed", 0), card("approval", "awaiting_approval", 400)];
  const { result } = renderHook(() => useCanvasRunFollow({ instance: flow, nodes, runKey: "run-1", active: true }));

  expect(flow.setCenter).toHaveBeenCalledTimes(1);
  expect(flow.setCenter.mock.calls[0].slice(0, 2)).toEqual([500, 50]);
  expect(result.current.paused).toBe(false);

  act(() => result.current.pause());
  expect(result.current.paused).toBe(true);
  act(() => result.current.resume());
  expect(result.current.paused).toBe(false);
  expect(flow.setCenter).toHaveBeenCalledTimes(2);
});

it("prefers the running step over one that waits", () => {
  const flow = instance();
  const nodes = [card("wait", "awaiting_timer", 0), card("lane", "in_progress", 400)];
  renderHook(() => useCanvasRunFollow({ instance: flow, nodes, runKey: "run-1", active: true }));
  expect(flow.setCenter.mock.calls[0].slice(0, 2)).toEqual([500, 50]);
});
