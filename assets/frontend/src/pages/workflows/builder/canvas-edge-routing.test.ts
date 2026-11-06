import { describe, expect, it } from "vitest";
import { curveEdgePath, routeCanvasEdge, routeCanvasEdges, compactRoutePoints, dropRouteSpurs, type CanvasEdgeRouteInput } from "./canvas-edge-routing";
import reference from "@/test/fixtures/canvas-edge-routing-reference.json";

describe("v133 canvas routing", () => {
  it("routes an explicit logical long jump above even when its manual endpoints look adjacent", () => {
    const route = routeCanvasEdge({ source: { x: 0, y: 24 }, target: { x: 96, y: 24 }, obstacles: [], backward: false, failure: false, repeat: false, longJump: true });
    expect(route.kind).toBe("upper-band"); expect(route.bounds.y).toBeLessThan(24);
  });
  it("preserves lower-detour topology and collinear reversals", () => {
    expect.soft(compactRoutePoints([{ x: 0, y: 0 }, { x: 20, y: 0 }, { x: 10, y: 0 }])).toEqual([{ x: 0, y: 0 }, { x: 20, y: 0 }, { x: 10, y: 0 }]);
    for (const flags of [{ backward: true, failure: false, repeat: false }, { backward: false, failure: true, repeat: false }, { backward: false, failure: false, repeat: true }]) {
      const route = routeCanvasEdge({ source: { x: 240, y: 0 }, target: { x: 0, y: 80 }, obstacles: [{ x: 244, y: 10, width: 8, height: 40 }], endCards: [{ x: 160, y: -24, width: 80, height: 48 }, { x: 0, y: 56, width: 80, height: 48 }], ...flags });
      expect.soft(route.kind).toBe("lower-band");
      expect.soft(route.points!.every((point) => point.y >= 0)).toBe(true);
      expect.soft(route.points!.some((point) => point.x > 256)).toBe(true);
    }
  });

  it("finds a clear local lower detour with many unrelated obstacles and two boxed direct leads", () => {
    const unrelated = Array.from({ length: 90 }, (_, i) => ({ x: 500 + i * 13, y: 200 + i * 11, width: 8, height: 8 }));
    const obstacles = [{ x: 244, y: 10, width: 8, height: 40 }, { x: 244, y: -50, width: 8, height: 40 }, ...unrelated];
    const endCards = [{ x: 160, y: -24, width: 80, height: 48 }, { x: 0, y: 56, width: 80, height: 48 }];
    const input = { source: { x: 240, y: 0 }, target: { x: 0, y: 80 }, obstacles, endCards, backward: false, failure: true, repeat: false };
    const route = routeCanvasEdge(input);
    expect(route.kind).toBe("lower-band"); expect(route.bounds.y).toBeGreaterThanOrEqual(0); expect(route.bounds.height).toBeLessThan(200);
    const points = route.points!;
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1]; const b = points[i]; expect(a).not.toEqual(b); expect(a.x === b.x || a.y === b.y).toBe(true);
      if (i === 1 || i === points.length - 1) continue;
      for (const rect of [...obstacles, ...endCards]) {
        const overlaps = Math.max(a.x, b.x) >= rect.x - 4 && Math.min(a.x, b.x) <= rect.x + rect.width + 4 && Math.max(a.y, b.y) >= rect.y - 4 && Math.min(a.y, b.y) <= rect.y + rect.height + 4;
        expect(overlaps).toBe(false);
      }
    }
    expect(routeCanvasEdge(input)).toEqual(route);
  });

  it("uses exact 8px leads, half-span min24 controls and ends on the rim of the input's 10px circle", () => {
    expect(curveEdgePath({ x: 0, y: 0 }, { x: 80, y: 40 })).toBe("M 0,0 L 8,0 C 37.5,0 37.5,40 67,40 L 75,40");
    expect(curveEdgePath({ x: 0, y: 0 }, { x: 40, y: 40 })).toBe("M 0,0 L 8,0 C 32,0 3,40 27,40 L 35,40");
    expect(curveEdgePath({ x: 0, y: 0 }, { x: 80, y: 0 })).toBe("M 0,0 L 75,0");
    const routed = routeCanvasEdge({ source: { x: 0, y: 0 }, target: { x: 80, y: 40 }, obstacles: [], backward: false, failure: false, repeat: false });
    expect(routed.midpoint).toEqual({ x: 37.5, y: 20 });
  });

  it("takes the short way through the gap to a step dropped under its source, with square corners", () => {
    const source = { x: 0, y: 0, width: 224, height: 80 }; const target = { x: 0, y: 160, width: 224, height: 80 };
    const route = routeCanvasEdge({ source: { x: 224, y: 24 }, target: { x: 0, y: 184 }, obstacles: [], endCards: [source, target], backward: true, failure: false, repeat: false });
    const points = route.points!;
    expect(points.every((point) => point.y >= 24 && point.y <= 184)).toBe(true);
    expect(points.some((point) => point.y > 80 && point.y < 160)).toBe(true);
    expect(route.path).not.toContain(" Q ");
    expect(points.at(-1)).toEqual({ x: -5, y: 184 });
  });

  it("goes round a node placed in the way 12px clear, with the fewest turns and square corners", () => {
    const blocker = { x: 160, y: -400, width: 80, height: 600 };
    const route = routeCanvasEdge({ source: { x: 0, y: 40 }, target: { x: 400, y: 40 }, obstacles: [blocker], endCards: [{ x: -224, y: 16, width: 224, height: 80 }, { x: 400, y: 16, width: 224, height: 80 }], backward: false, failure: false, repeat: false });
    const points = route.points!;
    expect(points).toHaveLength(6);
    expect(route.path).not.toContain(" Q ");
    for (let i = 2; i < points.length - 1; i++) {
      const a = points[i - 1]; const b = points[i];
      const near = Math.max(a.x, b.x) > blocker.x - 12 && Math.min(a.x, b.x) < blocker.x + blocker.width + 12 &&
        Math.max(a.y, b.y) > blocker.y - 12 && Math.min(a.y, b.y) < blocker.y + blocker.height + 12;
      expect(near).toBe(false);
    }
  });

  it("chooses the first clear shape jointly for convergent outcomes", () => {
    const source = { x: 0, y: 0 }; const target = { x: 200, y: 160 };
    const routes = routeCanvasEdges([
      { source, target, obstacles: [{ x: 60, y: 0, width: 35, height: 35 }], backward: false, failure: false, repeat: false, shapeGroupKey: "source:target" },
      { source: { x: 0, y: 32 }, target, obstacles: [{ x: 60, y: 0, width: 35, height: 35 }], backward: false, failure: false, repeat: false, shapeGroupKey: "source:target" },
    ]);
    expect(routes[0].shape).toEqual(routes[1].shape);
    expect(routes[0].shape).toEqual([0.25, 0.5]);
    expect(routes[0].midpoint.y).toBeLessThan(routes[1].midpoint.y);
    expect(routes.every((route) => route.kind === "curve")).toBe(true);
  });

  it("keeps ordinary convergent curves when the same target also receives a failure band", () => {
    const routes = routeCanvasEdges([
      { source: { x: 0, y: 0 }, target: { x: 200, y: 80 }, obstacles: [], backward: false, failure: false, repeat: false, shapeGroupKey: "same" },
      { source: { x: 0, y: 100 }, target: { x: 200, y: 80 }, obstacles: [], backward: false, failure: true, repeat: false, shapeGroupKey: "same", sourceSide: "bottom" },
    ]);
    expect(routes[0].kind).toBe("curve"); expect(routes[1].kind).toBe("lower-band");
  });

  it("uses a rounded upper band when every cubic shape is blocked, without duplicate or diagonal vertices", () => {
    const result = routeCanvasEdge({ source: { x: 0, y: 40 }, target: { x: 240, y: 80 }, obstacles: [{ x: 60, y: -20, width: 120, height: 180 }], backward: false, failure: false, repeat: false });
    expect(result.kind).toBe("upper-band");
    expect(result.bounds.y).toBeLessThan(-24);
    const points = result.points!;
    for (let i = 1; i < points.length; i++) {
      expect(points[i]).not.toEqual(points[i - 1]);
      expect(points[i].x === points[i - 1].x || points[i].y === points[i - 1].y).toBe(true);
    }
    expect(result.path).toContain(" Q ");
  });

  it.each([{ backward: true, failure: false, repeat: false }, { backward: false, failure: true, repeat: false }, { backward: false, failure: false, repeat: true }])("routes backward, failure and repeat under the cards: %j", (flags) => {
    const result = routeCanvasEdge({ source: { x: 240, y: 40 }, target: { x: 0, y: 80 }, obstacles: [{ x: 80, y: 0, width: 80, height: 120 }], ...flags });
    expect(result.kind).toBe("lower-band");
    expect(result.bounds.y + result.bounds.height).toBeGreaterThan(124);
  });
});

describe("canvas routing on large real canvases", () => {
  const rect = ([x, y, width, height]: number[]) => ({ x, y, width, height });
  type ReferenceCase = { name: string; input: Omit<CanvasEdgeRouteInput, "obstacles" | "endCards"> & { obstacles: number[][]; endCards: number[][] }; expected: unknown };
  const cases = (reference as { cases: ReferenceCase[] }).cases;

  it.each(cases.map((entry) => [entry.name, entry] as const))("draws %s exactly as the reference router did", (_name, entry) => {
    const input = { ...entry.input, obstacles: entry.input.obstacles.map(rect), endCards: entry.input.endCards.map(rect) };
    expect(routeCanvasEdges([input])[0]).toEqual(entry.expected);
  });

  it("keeps the reference set covering detours round many cards", () => {
    expect(cases.length).toBeGreaterThanOrEqual(10);
    expect(cases.every((entry) => entry.input.obstacles.length > 40)).toBe(true);
    expect(new Set(cases.map((entry) => (entry.expected as { kind: string }).kind))).toEqual(new Set(["upper-band", "lower-band"]));
  });
});

describe("a band passes only the cards in its way", () => {
  const between = { x: 240, y: 440, width: 120, height: 120 };
  const farAbove = { x: 200, y: -2000, width: 400, height: 300 };
  const farBelow = { x: 200, y: 2400, width: 400, height: 300 };

  it("lifts a long jump just over the card between its ends, not over a card far up the canvas", () => {
    const route = routeCanvasEdge({ source: { x: 0, y: 500 }, target: { x: 600, y: 500 }, obstacles: [between, farAbove], backward: false, failure: false, repeat: false, longJump: true });
    expect(route.kind).toBe("upper-band");
    expect(Math.min(...route.points!.map((point) => point.y))).toBe(between.y - 24);
  });

  it("drops a backward connection just under the card in its way, not under a card far down the canvas", () => {
    const route = routeCanvasEdge({ source: { x: 600, y: 500 }, target: { x: 0, y: 500 }, obstacles: [between, farBelow], backward: true, failure: false, repeat: false });
    expect(route.kind).toBe("lower-band");
    expect(Math.max(...route.points!.map((point) => point.y))).toBe(between.y + between.height + 24);
  });

  it("still climbs past every card stacked in its way", () => {
    const stacked = [between, { x: 240, y: 300, width: 120, height: 150 }, { x: 260, y: 180, width: 80, height: 110 }];
    const route = routeCanvasEdge({ source: { x: 0, y: 500 }, target: { x: 600, y: 500 }, obstacles: [...stacked, farAbove], backward: false, failure: false, repeat: false, longJump: true });
    expect(Math.min(...route.points!.map((point) => point.y))).toBe(180 - 24);
  });

  it("keeps a band beside the corridor's other cards at the band clearance", () => {
    const route = routeCanvasEdge({ source: { x: 0, y: 500 }, target: { x: 600, y: 500 }, obstacles: [between, farAbove], backward: false, failure: false, repeat: false, longJump: true });
    const top = Math.min(...route.points!.map((point) => point.y));
    for (const card of [between, farAbove]) expect(top <= card.y - 24 || top >= card.y + card.height + 24).toBe(true);
  });
});

describe("a route never runs out and back on one line", () => {
  it("drops a spur that dives past the entry and comes back up to it", () => {
    const points = [{ x: 3760, y: -264 }, { x: 3768, y: -264 }, { x: 3768, y: 772 }, { x: 51, y: 772 }, { x: 51, y: 1336 }, { x: 51, y: 808 }, { x: 59, y: 808 }];
    expect(dropRouteSpurs(points)).toEqual([{ x: 3760, y: -264 }, { x: 3768, y: -264 }, { x: 3768, y: 772 }, { x: 51, y: 772 }, { x: 51, y: 808 }, { x: 59, y: 808 }]);
  });

  it("keeps the leads out of and into the ports as they are", () => {
    const points = [{ x: 0, y: 0 }, { x: 8, y: 0 }, { x: 4, y: 0 }, { x: 4, y: 80 }, { x: -9, y: 80 }, { x: -4, y: 80 }];
    expect(dropRouteSpurs(points)).toEqual(points);
  });

  it("keeps a route without spurs unchanged", () => {
    const points = [{ x: 0, y: 0 }, { x: 8, y: 0 }, { x: 8, y: 100 }, { x: 300, y: 100 }, { x: 300, y: 40 }, { x: 308, y: 40 }];
    expect(dropRouteSpurs(points)).toEqual(points);
  });

  it("leaves no spur in any reference route, wrapped rows included", () => {
    for (const entry of (reference as { cases: { expected: { points?: { x: number; y: number }[] } }[] }).cases) {
      const points = entry.expected.points || [];
      expect(dropRouteSpurs(points)).toEqual(points);
    }
  });
});
