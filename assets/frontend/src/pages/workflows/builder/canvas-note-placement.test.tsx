import { fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";

import { CanvasNoteToolbarButton } from "./canvas-note-placement";

function renderButton() {
  const canvasRootRef = createRef<HTMLDivElement>();
  const onPlace = vi.fn();
  const onBeforePlace = vi.fn();
  render(<div ref={canvasRootRef} data-testid="canvas"><CanvasNoteToolbarButton compact={false} disabled={false} canvasRootRef={canvasRootRef} zoom={() => 1} onBeforePlace={onBeforePlace} onPlace={onPlace} /></div>);
  vi.spyOn(screen.getByTestId("canvas"), "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, x: 0, y: 0, toJSON: () => ({}) });
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
