import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GraphEditNotice } from "./canvas-core";
import { CanvasToast } from "./canvas-toast";
import { canvasToastDuration, watchGraphIntent } from "./canvas-toast-feed";
import type { GraphIntentToken } from "./canvas-history";

describe("canvas toast (Components2 › Toasts)", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("leaves after 2.6s, 6s with Undo; an error and a running save stay", () => {
    expect(canvasToastDuration({ label: "Done.", tone: "success" })).toBe(2600);
    expect(canvasToastDuration({ label: "Deleted.", tone: "success", action: { label: "Undo", onAction: () => undefined } })).toBe(6000);
    expect(canvasToastDuration({ label: "Refused.", tone: "error" })).toBeNull();
    expect(canvasToastDuration({ label: "Saving", tone: "saving" })).toBeNull();
  });

  it("dismisses itself on time unless hovered, and Undo acts then dismisses", () => {
    const onDismiss = vi.fn();
    const onAction = vi.fn();
    const notice: GraphEditNotice = { label: "Reject order deleted.", tone: "success", action: { label: "Undo", onAction } };
    render(<CanvasToast notice={notice} onDismiss={onDismiss} />);
    const toast = screen.getByRole("status");
    expect(toast).toHaveTextContent("Reject order deleted.");
    fireEvent.pointerEnter(toast);
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(onDismiss).not.toHaveBeenCalled();
    fireEvent.pointerLeave(toast);
    act(() => { vi.advanceTimersByTime(5_999); });
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1); });
    expect(onDismiss).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledTimes(2);
  });

  it("keeps an error until its × dismisses it", () => {
    const onDismiss = vi.fn();
    render(<CanvasToast notice={{ label: "A connection stays inside one group or lane.", tone: "error" }} onDismiss={onDismiss} />);
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(screen.getByRole("alert")).toHaveTextContent("A connection stays inside one group or lane.");
    expect(onDismiss).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("holds the guidance of a gesture in progress", () => {
    const onDismiss = vi.fn();
    render(<CanvasToast notice={{ label: "Drag from a named outcome to the step that runs next.", tone: "success" }} onDismiss={onDismiss} held />);
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(onDismiss).not.toHaveBeenCalled();
  });
});

describe("Undo through the change-set history", () => {
  it("offers Undo only when the change staged a new intent, and reverses exactly that one", () => {
    let latest: GraphIntentToken | null = null;
    const undo = vi.fn(() => true);
    const owner = { lastStagedGraphIntent: () => latest, undoGraphIntentIfLatest: undo };
    expect(watchGraphIntent(owner)("Undo")).toBeUndefined();
    const watch = watchGraphIntent(owner);
    latest = { controller: Symbol("owner"), serial: 1 };
    const action = watch("Undo");
    expect(action?.label).toBe("Undo");
    action?.onAction();
    expect(undo).toHaveBeenCalledWith(latest);
    expect(watchGraphIntent(null)("Undo")).toBeUndefined();
    expect(watchGraphIntent({})("Undo")).toBeUndefined();
  });
});
