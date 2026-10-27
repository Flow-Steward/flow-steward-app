import type { Node } from "@xyflow/react";
import { nearestFreeCanvasPosition, placeCanvasNode, type CanvasPlacementRect } from "./canvas-placement";
const size = { width: 32, height: 32 };
it("snaps requests and allows touching without inventing a gap", () => {
  expect(nearestFreeCanvasPosition({ requested: { x: 19, y: 18 }, size, obstacles: [] })).toEqual({ x: 16, y: 16 });
  expect(nearestFreeCanvasPosition({ requested: { x: 32, y: 0 }, size, obstacles: [{ x: 0, y: 0, ...size }] })).toEqual({ x: 32, y: 0 });
});
it("resolves distance ties by abs dx, down then right, independent of obstacle order", () => {
  const blocked = [{ x: 0, y: 0, ...size }];
  expect(nearestFreeCanvasPosition({ requested: { x: 0, y: 0 }, size, obstacles: blocked })).toEqual({ x: 0, y: 32 });
  const vertical = [...blocked, { x: 0, y: 32, ...size }, { x: 0, y: -32, ...size }];
  expect(nearestFreeCanvasPosition({ requested: { x: 0, y: 0 }, size, obstacles: vertical })).toEqual({ x: 32, y: 0 });
  expect(nearestFreeCanvasPosition({ requested: { x: 0, y: 0 }, size, obstacles: [...vertical].reverse() })).toEqual({ x: 32, y: 0 });
});
it("honors containment bounds and reports a genuinely full bounded scope", () => {
  const bounds = { minX: 0, minY: 0, maxX: 32, maxY: 32 };
  expect(nearestFreeCanvasPosition({ requested: { x: -100, y: 19 }, size, bounds, obstacles: [] })).toEqual({ x: 0, y: 16 });
  expect(nearestFreeCanvasPosition({ requested: { x: 0, y: 0 }, size, bounds: { minX: 0, minY: 0, maxX: 0, maxY: 0 }, obstacles: [{ x: 0, y: 0, ...size }] })).toBeNull();
});
it("matches an exhaustive small-grid distance oracle on offset rectangles", () => {
  const key = (p: { x: number; y: number }) => [p.x * p.x + p.y * p.y, Math.abs(p.x), p.y < 0 ? 1 : 0, p.x < 0 ? 1 : 0, p.y, p.x];
  const compare = (a: { x: number; y: number }, b: { x: number; y: number }) => { const x = key(a); const y = key(b); for (let i = 0; i < x.length; i += 1) if (x[i] !== y[i]) return x[i] - y[i]; return 0; };
  for (let shift = -20; shift <= 20; shift += 5) {
    const obstacles: CanvasPlacementRect[] = [{ x: -8, y: -8, width: 48, height: 48 }, { x: shift, y: 32, width: 50, height: 37 }];
    const free = [];
    for (let x = -128; x <= 128; x += 16) for (let y = -128; y <= 128; y += 16) if (!obstacles.some((b) => x < b.x + b.width && x + size.width > b.x && y < b.y + b.height && y + size.height > b.y)) free.push({ x, y });
    expect(nearestFreeCanvasPosition({ requested: { x: 0, y: 0 }, size, obstacles })).toEqual(free.sort(compare)[0]);
  }
});
it("collides only with the same actual parent and lane; notes stay outside the graph", () => {
  const moving: Node = { id: "moving", parentId: "box", position: { x: 0, y: 0 }, ...size, data: { step: { lane_id: "a" } } };
  const other: Node = { ...moving, id: "other", data: { step: { lane_id: "b" } } };
  expect(placeCanvasNode(moving, { x: 0, y: 0 }, [moving, other])).toEqual({ x: 0, y: 0 });
  expect(placeCanvasNode(moving, { x: 0, y: 0 }, [moving, { ...other, data: moving.data }])).toEqual({ x: 0, y: 32 });
  expect(placeCanvasNode(moving, { x: 0, y: 0 }, [moving, { ...other, type: "canvasNote", data: moving.data }])).toEqual({ x: 0, y: 0 });
});
