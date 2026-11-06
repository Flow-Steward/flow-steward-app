import type { Node } from "@xyflow/react";
import { reuseUnchangedNodes } from "./canvas-node-identity";

const card = (id: string, x: number, data: Record<string, unknown> = {}): Node => ({ id, type: "action", position: { x, y: 0 }, data });

describe("cards keep their objects while nothing about them changed", () => {
  it("gives back the earlier list when every card is unchanged and in place", () => {
    const earlier = [card("a", 0, { frames: [{ x: 1, add: { y: 2 } }] }), card("b", 100)];
    const again = [card("a", 0, { frames: [{ x: 1, add: { y: 2 } }] }), card("b", 100)];
    expect(reuseUnchangedNodes(earlier, again)).toBe(earlier);
  });

  it("replaces only the card that moved", () => {
    const earlier = [card("a", 0), card("b", 100)];
    const result = reuseUnchangedNodes(earlier, [card("a", 16), card("b", 100)]);
    expect(result).not.toBe(earlier);
    expect(result[0]).not.toBe(earlier[0]);
    expect(result[0].position).toEqual({ x: 16, y: 0 });
    expect(result[1]).toBe(earlier[1]);
  });

  it("takes a new list when cards come, go or change order", () => {
    const earlier = [card("a", 0), card("b", 100)];
    const reordered = reuseUnchangedNodes(earlier, [card("b", 100), card("a", 0)]);
    expect(reordered).not.toBe(earlier);
    expect(reordered).toEqual([earlier[1], earlier[0]]);
    expect(reuseUnchangedNodes(earlier, [card("a", 0)])).toEqual([earlier[0]]);
  });

  it("compares handlers, maps and class instances by identity only", () => {
    const open = () => undefined;
    const earlier = [card("a", 0, { onOpen: open, marks: new Map([["k", 1]]) })];
    expect(reuseUnchangedNodes(earlier, [card("a", 0, { onOpen: () => undefined, marks: earlier[0].data.marks })])[0]).not.toBe(earlier[0]);
    expect(reuseUnchangedNodes(earlier, [card("a", 0, { onOpen: open, marks: new Map([["k", 1]]) })])[0]).not.toBe(earlier[0]);
    expect(reuseUnchangedNodes(earlier, [card("a", 0, { onOpen: open, marks: earlier[0].data.marks })])).toBe(earlier);
  });

  it("copies the first list so later reuse never shares the caller's array", () => {
    const first = [card("a", 0)];
    const result = reuseUnchangedNodes(null, first);
    expect(result).not.toBe(first);
    expect(result[0]).toBe(first[0]);
  });
});
