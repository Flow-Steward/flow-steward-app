import { act, renderHook } from "@testing-library/react";

import { pageRefreshSeconds, useExtensionPageRefresh } from "./use-extension-page-refresh";

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(globalThis.document, "visibilityState", { configurable: true, get: () => state });
}

function renderRefresh(seconds: number, available = true) {
  const setRevision = vi.fn();
  const view = renderHook(
    ({ pageKey }) => useExtensionPageRefresh({ pageKey, seconds, available, setComponentQueryRevision: setRevision }),
    { initialProps: { pageKey: "runs" } },
  );
  return { setRevision, ...view };
}

describe("page refresh", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility("visible");
  });
  afterEach(() => {
    vi.useRealTimers();
    setVisibility("visible");
  });

  it("reads only a bounded whole number of seconds", () => {
    expect(pageRefreshSeconds({ refresh_seconds: 30 })).toBe(30);
    expect(pageRefreshSeconds({ refresh_seconds: 9 })).toBe(0);
    expect(pageRefreshSeconds({ refresh_seconds: 3601 })).toBe(0);
    expect(pageRefreshSeconds({ refresh_seconds: "30" })).toBe(0);
    expect(pageRefreshSeconds({ refresh_seconds: 12.5 })).toBe(0);
    expect(pageRefreshSeconds(undefined)).toBe(0);
  });

  it("reloads on its interval while the tab is visible", () => {
    const { setRevision } = renderRefresh(30);

    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(setRevision).toHaveBeenCalledTimes(1);

    setVisibility("hidden");
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(setRevision).toHaveBeenCalledTimes(1);
  });

  it("never starts a reload while a load is running", () => {
    const { setRevision, result } = renderRefresh(30);

    act(() => {
      result.current.loadStarted();
    });
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(setRevision).not.toHaveBeenCalled();

    act(() => {
      result.current.loadFinished(false);
    });
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(setRevision).toHaveBeenCalledTimes(1);
  });

  it("stops after a failed load until the person presses Refresh", () => {
    const { setRevision, result } = renderRefresh(30);

    act(() => {
      result.current.loadStarted();
      result.current.loadFinished(true);
    });
    expect(result.current.stopped).toBe(true);
    act(() => {
      vi.advanceTimersByTime(90_000);
    });
    expect(setRevision).not.toHaveBeenCalled();

    act(() => {
      result.current.refresh();
    });
    expect(result.current.stopped).toBe(false);
    expect(setRevision).toHaveBeenCalledTimes(1);
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(setRevision).toHaveBeenCalledTimes(2);
  });

  it("starts again on another page after a failure", () => {
    const { result, rerender } = renderRefresh(30);

    act(() => {
      result.current.loadFinished(true);
    });
    expect(result.current.stopped).toBe(true);
    rerender({ pageKey: "run_detail" });
    expect(result.current.stopped).toBe(false);
  });

  it("does not reload a page without refresh_seconds", () => {
    const { setRevision, result } = renderRefresh(0);

    act(() => {
      vi.advanceTimersByTime(3_600_000);
    });
    expect(setRevision).not.toHaveBeenCalled();
    act(() => {
      result.current.loadFinished(true);
    });
    expect(result.current.stopped).toBe(false);
  });
});
