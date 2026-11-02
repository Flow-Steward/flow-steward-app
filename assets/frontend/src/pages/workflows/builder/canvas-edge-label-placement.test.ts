import { canvasEdgeLabelPoint, edgeLabelRect } from "./canvas-edge-label-placement";
import { routeCanvasEdge, type CanvasEdgeRoute, type CanvasRect } from "./canvas-edge-routing";

const covers = (a: CanvasRect, b: CanvasRect) => a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;

// The designer's case: False runs as a band over the top of a step, and the
// label hanging under the band's midpoint landed under that step's card.
const band: CanvasEdgeRoute = {
  path: "",
  midpoint: { x: 325, y: 80 },
  bounds: { x: 0, y: 80, width: 700, height: 120 },
  kind: "upper-band",
  points: [{ x: 0, y: 200 }, { x: 0, y: 80 }, { x: 700, y: 80 }, { x: 700, y: 150 }],
};
const card: CanvasRect = { x: 200, y: 100, width: 300, height: 200 };

it("keeps a label at the midpoint when it covers no card", () => {
  expect(canvasEdgeLabelPoint(band, "False", [{ x: 900, y: 0, width: 200, height: 200 }])).toEqual(band.midpoint);
});

it("moves a label along its own band to the nearest stretch clear of the card under it", () => {
  const at = canvasEdgeLabelPoint(band, "False", [card]);

  expect(at.y).toBe(80);
  expect(covers(edgeLabelRect(at, "False"), card)).toBe(false);
  // The nearer side of the card wins: the left end is 150px away, the right one 200px.
  expect(at.x).toBeLessThan(card.x);
  expect(at.x).toBeGreaterThan(card.x - 48);
});

it("moves a label along a curve off a card the curve passes over", () => {
  const route = routeCanvasEdge({ source: { x: 0, y: 0 }, target: { x: 400, y: 240 }, obstacles: [], backward: false, failure: false, repeat: false });
  expect(route.kind).toBe("curve");
  const blocker: CanvasRect = { x: route.midpoint.x - 40, y: route.midpoint.y, width: 80, height: 60 };

  const at = canvasEdgeLabelPoint(route, "Approved", [blocker]);

  expect(at).not.toEqual(route.midpoint);
  expect(covers(edgeLabelRect(at, "Approved"), blocker)).toBe(false);
  expect(at.x).toBeGreaterThanOrEqual(0);
  expect(at.x).toBeLessThanOrEqual(400);
});

it("falls back to the midpoint when no point of the line is clear", () => {
  const everywhere: CanvasRect = { x: -100, y: -100, width: 1000, height: 1000 };

  expect(canvasEdgeLabelPoint(band, "False", [everywhere])).toEqual(band.midpoint);
});
