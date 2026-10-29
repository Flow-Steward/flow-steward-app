import { chooseCanvasMinimapRect } from "./canvas-minimap-placement";

const rect = (left: number, top: number, right: number, bottom: number) => ({ left, top, right, bottom });

it("moves the minimap away from a lane Add control inside the inspector-constrained canvas", () => {
  const placement = chooseCanvasMinimapRect(
    rect(0, 0, 1000, 600),
    rect(0, 450, 800, 584),
    { width: 184, height: 184 },
    [rect(610, 400, 790, 450), rect(350, 520, 650, 560)],
  );

  expect(placement).toEqual({ left: 600, top: 16, right: 784, bottom: 200 });
});
