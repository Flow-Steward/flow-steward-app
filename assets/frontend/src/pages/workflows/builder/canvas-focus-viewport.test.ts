import { canvasFocusViewport } from "./canvas-viewport";

describe("an opened issue's card is shown at a readable zoom", () => {
  const stage = { left: 0, top: 0 };
  // The free pane: the canvas left of an open drawer, above the controls.
  const pane = { left: 16, top: 16, right: 816, bottom: 616 };
  const card = { x: 2000, y: 1500, width: 240, height: 120 };
  const screenBox = (view: { x: number; y: number; zoom: number }) => ({
    left: card.x * view.zoom + view.x, top: card.y * view.zoom + view.y,
    right: (card.x + card.width) * view.zoom + view.x, bottom: (card.y + card.height) * view.zoom + view.y,
  });

  it("keeps the view when the card is already in the pane at a readable zoom", () => {
    const view = { x: -1700, y: -1300, zoom: 1 };
    expect(canvasFocusViewport(card, pane, stage, view, 0.9)).toBe(view);
  });

  it("zooms in to the readable zoom and centres a card seen from far out", () => {
    const view = canvasFocusViewport(card, pane, stage, { x: 0, y: 0, zoom: 0.25 }, 0.9);
    expect(view.zoom).toBe(0.9);
    const box = screenBox(view);
    expect((box.left + box.right) / 2).toBeCloseTo((pane.left + pane.right) / 2);
    expect((box.top + box.bottom) / 2).toBeCloseTo((pane.top + pane.bottom) / 2);
  });

  it("brings a card hidden under the drawer into the pane without zooming out", () => {
    const view = canvasFocusViewport(card, pane, stage, { x: -1100, y: -1300, zoom: 1.2 }, 0.9);
    expect(view.zoom).toBe(1.2);
    const box = screenBox(view);
    expect(box.left).toBeGreaterThanOrEqual(pane.left);
    expect(box.right).toBeLessThanOrEqual(pane.right);
  });

  it("never zooms a large frame past what fits the pane", () => {
    const frame = { x: 0, y: 0, width: 2000, height: 900 };
    const view = canvasFocusViewport(frame, pane, stage, { x: 0, y: 0, zoom: 0.2 }, 0.8);
    expect(view.zoom).toBeCloseTo(800 / 2000);
  });
});
