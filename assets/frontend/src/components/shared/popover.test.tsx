import { useState } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DsPopover } from "@/components/shared/popover";

function Harness({ initialFocus = false, portal = false }: Readonly<{ initialFocus?: boolean; portal?: boolean }>) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button">Outside</button>
      <DsPopover
        portal={portal}
        label="Period"
        onOpenChange={setOpen}
        open={open}
        trigger={(props) => (
          <button type="button" {...props}>
            All events
          </button>
        )}
        wide
      >
        <button type="button">First</button>
        <button data-popover-initial-focus={initialFocus ? "" : undefined} type="button">
          Chosen
        </button>
        <button onClick={() => setOpen(false)} type="button">
          Apply
        </button>
      </DsPopover>
    </>
  );
}

describe("DsPopover", () => {
  it("exposes its state on the trigger and moves focus into the surface", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "All events" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveAttribute("aria-haspopup", "dialog");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(trigger);
    const surface = screen.getByRole("dialog", { name: "Period" });
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-controls", surface.id);
    expect(surface).toHaveClass("fs-popover", "wide");
    expect(screen.getByRole("button", { name: "First" })).toHaveFocus();
  });

  it("focuses the marked element first when there is one", async () => {
    const user = userEvent.setup();
    render(<Harness initialFocus />);
    await user.click(screen.getByRole("button", { name: "All events" }));
    expect(screen.getByRole("button", { name: "Chosen" })).toHaveFocus();
  });

  it("focuses the marked element of a portaled surface that opens at mount, once it is placed", async () => {
    // Add step on the canvas opens this way: already open when it mounts, portaled,
    // hidden until positioned. A focus() while hidden does nothing, so typing went nowhere.
    render(
      <DsPopover label="Add step" open onOpenChange={() => undefined} portal trigger={(props) => <button type="button" {...props}>Anchor</button>}>
        <button type="button">First</button>
        <input aria-label="Search steps" data-popover-initial-focus />
      </DsPopover>,
    );
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Search steps" })).toHaveFocus());
    expect(screen.getByRole("dialog", { name: "Add step" })).toBeVisible();
  });

  it("returns focus to the trigger on Escape and on the caller's own close", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "All events" });

    await user.click(trigger);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("closes on an outside press without pulling focus back", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "All events" }));

    const outside = screen.getByRole("button", { name: "Outside" });
    fireEvent.mouseDown(outside);
    outside.focus();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(outside).toHaveFocus();
  });

  it("portals outside transformed ancestors, flips and clamps to the viewport, and follows real trigger movement", async () => {
    let triggerElement: HTMLElement | null = null;
    let anchor = { x: 990, y: 740, left: 990, top: 740, right: 1010, bottom: 760, width: 20, height: 20, toJSON: () => ({}) };
    const nativeRect = HTMLElement.prototype.getBoundingClientRect;
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this === triggerElement) return anchor;
      return this.getAttribute("role") === "dialog" ? { x: 0, y: 0, left: 0, top: 0, right: 260, bottom: 200, width: 260, height: 200, toJSON: () => ({}) } : nativeRect.call(this);
    });
    const user = userEvent.setup();
    render(<div data-testid="transformed" style={{ transform: "scale(0.5)" }}><Harness portal /></div>);
    const trigger = screen.getByRole("button", { name: "All events" });
    triggerElement = trigger;
    try {
      await user.click(trigger);
      const surface = screen.getByRole("dialog", { name: "Period" });
      expect(screen.getByRole("dialog", { name: "Period" })).toBeVisible();
      expect(within(screen.getByTestId("transformed")).queryByRole("dialog")).not.toBeInTheDocument();
      await waitFor(() => expect(surface).toHaveStyle({ position: "fixed", left: "756px", top: "534px" }));
      anchor = { ...anchor, x: 20, y: 100, left: 20, top: 100, right: 40, bottom: 120 };
      fireEvent.scroll(window);
      await waitFor(() => expect(surface).toHaveStyle({ left: "20px", top: "126px" }));
      anchor = { ...anchor, x: 50, left: 50, right: 70 };
      fireEvent.resize(window);
      await waitFor(() => expect(surface).toHaveStyle({ left: "50px" }));
    } finally { rect.mockRestore(); }
  });

  it("keeps portaled surface presses inside and preserves Escape, caller-close and outside-focus semantics", async () => {
    const user = userEvent.setup();
    render(<Harness portal />);
    const trigger = screen.getByRole("button", { name: "All events" });
    await user.click(trigger);
    const first = screen.getByRole("button", { name: "First" });
    expect(first).toHaveFocus();
    fireEvent.mouseDown(first);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    const outside = screen.getByRole("button", { name: "Outside" });
    fireEvent.mouseDown(outside); outside.focus();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(outside).toHaveFocus();
  });

  it("keeps Escape from reaching a layer behind it", async () => {
    const user = userEvent.setup();
    const behind = vi.fn();
    document.addEventListener("keydown", behind);
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "All events" }));
    await user.keyboard("{Escape}");
    expect(behind).not.toHaveBeenCalled();
    document.removeEventListener("keydown", behind);
  });
});
