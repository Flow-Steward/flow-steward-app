import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { Save } from "lucide-react";
import { afterEach, vi } from "vitest";

import { Button } from "@/components/ui/button";

afterEach(() => { vi.useRealTimers(); });

describe("Button busy state (button.md, Loading)", () => {
  it("is busy and ignores presses at once, and shows the round mono Loader with the busy label only after the delay", () => {
    vi.useFakeTimers();
    const save = vi.fn();
    const { rerender } = render(<Button busy={false} busyLabel="Saving…" onClick={save} type="button"><Save aria-hidden="true" />Save</Button>);
    rerender(<Button busy busyLabel="Saving…" onClick={save} type="button"><Save aria-hidden="true" />Save</Button>);
    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(save).not.toHaveBeenCalled();

    act(() => { vi.advanceTimersByTime(399); });
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1); });
    const busy = screen.getByRole("button", { name: "Saving…" });
    const loader = within(busy).getByTestId("loader-mark");
    expect(loader).toHaveClass("fs-loader-orbit");
    expect(loader).not.toHaveClass("md");
    expect(loader).toHaveAttribute("aria-hidden", "true");
  });

  it("keeps both contents in one cell so the width never changes, and holds the busy label for 500ms", () => {
    vi.useFakeTimers();
    const { rerender } = render(<Button busy busyLabel="Saving…" type="button">Save</Button>);
    // Both contents are always laid out (the inactive one hidden), so the width is the wider of the two.
    expect(screen.getByRole("button")).toHaveTextContent("SaveSaving…");
    act(() => { vi.advanceTimersByTime(400); });
    rerender(<Button busy={false} busyLabel="Saving…" type="button">Save</Button>);
    expect(screen.getByRole("button")).not.toHaveAttribute("aria-busy");
    act(() => { vi.advanceTimersByTime(499); });
    expect(screen.getByRole("button", { name: "Saving…" })).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1); });
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("never shows the busy label for an action that ends before the delay", () => {
    vi.useFakeTimers();
    const { rerender } = render(<Button busy busyLabel="Saving…" type="button">Save</Button>);
    act(() => { vi.advanceTimersByTime(200); });
    rerender(<Button busy={false} busyLabel="Saving…" type="button">Save</Button>);
    act(() => { vi.advanceTimersByTime(1000); });
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });
});
