import { fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";

import { CanvasNoteToolbarButton } from "./canvas-note-placement";

function renderButton(placementProps: Record<string, unknown> = {}, withCenterObstacle = false) {
  const canvasRootRef = createRef<HTMLDivElement>();
  const onPlace = vi.fn();
  const onBeforePlace = vi.fn();
  render(<div ref={canvasRootRef} data-testid="canvas">
    {withCenterObstacle ? <div className="react-flow__node" data-testid="center-obstacle" /> : null}
    <CanvasNoteToolbarButton compact={false} disabled={false} canvasRootRef={canvasRootRef} zoom={() => 1} onBeforePlace={onBeforePlace} onPlace={onPlace} {...placementProps} />
  </div>);
  vi.spyOn(screen.getByTestId("canvas"), "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, x: 0, y: 0, toJSON: () => ({}) });
  if (withCenterObstacle) {
    vi.spyOn(screen.getByTestId("center-obstacle"), "getBoundingClientRect").mockReturnValue({ left: 300, top: 220, right: 500, bottom: 380, width: 200, height: 160, x: 300, y: 220, toJSON: () => ({}) });
  }
  return { button: screen.getByRole("button", { name: "Add note" }), onPlace, onBeforePlace };
}

afterEach(() => vi.restoreAllMocks());

describe("Note on the canvas toolbar", () => {
  it("places a note in the middle of the view on a plain click and names its key", () => {
    const { button, onPlace } = renderButton();
    expect(button).toHaveAttribute("aria-keyshortcuts", "N");
    expect(button).toHaveAttribute("title", "Add a note (N), or drag it to its place");
    fireEvent.click(button);
    expect(onPlace).toHaveBeenCalledWith();
  });

  it("uses the current zoom when placing a note after zoom changes without rerendering", () => {
    let currentZoom = 1;
    const identity = (point: { x: number; y: number }) => point;
    const { button, onPlace } = renderButton({
      zoom: () => currentZoom,
      inspectorWidth: 0,
      screenToFlow: identity,
      flowToScreen: identity,
    }, true);

    currentZoom = 2;
    fireEvent.click(button);

    expect(onPlace).toHaveBeenCalledTimes(1);
    expect(onPlace).toHaveBeenCalledWith();
  });

  it("moves a default note to the nearest visible grid slot when the viewport center is occupied", () => {
    const identity = (point: { x: number; y: number }) => point;
    const { button, onPlace } = renderButton({
      inspectorWidth: 0,
      screenToFlow: identity,
      flowToScreen: identity,
    }, true);

    fireEvent.click(button);

    expect(onPlace).toHaveBeenCalledTimes(1);
    const point = onPlace.mock.calls[0][0] as { clientX: number; clientY: number } | undefined;
    expect(point).toBeDefined();
    const note = { left: point!.clientX - 136, right: point!.clientX + 136, top: point!.clientY - 88, bottom: point!.clientY + 88 };
    expect(note.left).toBeGreaterThanOrEqual(16);
    expect(note.right).toBeLessThanOrEqual(784);
    expect(note.top).toBeGreaterThanOrEqual(16);
    expect(note.bottom).toBeLessThanOrEqual(584);
    expect(note.left < 500 && note.right > 300 && note.top < 380 && note.bottom > 220).toBe(false);
  });

  it("drops a dragged note where the pointer lets go, showing a contour on the way", () => {
    const { button, onPlace } = renderButton();
    fireEvent.pointerDown(button, { button: 0, clientX: 400, clientY: 580 });
    fireEvent.pointerMove(button, { clientX: 300, clientY: 200 });
    // eslint-disable-next-line testing-library/no-node-access -- The contour is a decorative, aria-hidden overlay.
    expect(document.querySelector(".workflow-note-drop-contour")).toHaveStyle({ width: "256px", height: "160px" });
    fireEvent.pointerUp(button, { clientX: 300, clientY: 200 });
    fireEvent.click(button);
    expect(onPlace).toHaveBeenCalledTimes(1);
    expect(onPlace).toHaveBeenCalledWith({ clientX: 300, clientY: 200 });
    // eslint-disable-next-line testing-library/no-node-access -- The contour is a decorative, aria-hidden overlay.
    expect(document.querySelector(".workflow-note-drop-contour")).toBeNull();
  });

  it("adds nothing when dropped off the canvas or when Escape cancels the drag", () => {
    const { button, onPlace } = renderButton();
    fireEvent.pointerDown(button, { button: 0, clientX: 400, clientY: 580 });
    fireEvent.pointerMove(button, { clientX: 900, clientY: 700 });
    fireEvent.pointerUp(button, { clientX: 900, clientY: 700 });
    fireEvent.click(button);
    fireEvent.pointerDown(button, { button: 0, clientX: 400, clientY: 580 });
    fireEvent.pointerMove(button, { clientX: 300, clientY: 200 });
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.pointerUp(button, { clientX: 300, clientY: 200 });
    fireEvent.click(button);
    expect(onPlace).not.toHaveBeenCalled();
  });
});
