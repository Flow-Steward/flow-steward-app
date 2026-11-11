import { act, render } from "@testing-library/react";

import { holdForLoader, markLoaderHidden } from "@/components/shared/loader-clock";
import { PAGE_LOADING_DELAY_MS, PageLoadingState } from "@/components/shared/page-loading-state";
import { LOADER_MIN_SHOWN_MS } from "@/components/shared/use-loader-delay";

/** Resolve the held page and report whether it has resolved after `ms`. */
async function settledAfter(load: Promise<string>, ms: number) {
  let settled = false;
  void load.then(() => { settled = true; });
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  return settled;
}

describe("holdForLoader", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    markLoaderHidden();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not delay a page that loaded before the loader painted", async () => {
    const view = render(<PageLoadingState label="Preparing this page" />);
    const page = holdForLoader(Promise.resolve("page"));

    expect(await settledAfter(page, 1)).toBe(true);
    view.unmount();
  });

  it("holds a page until the painted loader has been visible for the minimum time", async () => {
    const view = render(<PageLoadingState label="Preparing this page" />);
    act(() => { vi.advanceTimersByTime(PAGE_LOADING_DELAY_MS); });
    act(() => { vi.advanceTimersByTime(100); });

    const page = holdForLoader(Promise.resolve("page"));

    expect(await settledAfter(page, LOADER_MIN_SHOWN_MS - 101)).toBe(false);
    expect(await settledAfter(page, 1)).toBe(true);
    view.unmount();
  });

  it("forgets the paint once the last page loader is gone", async () => {
    const view = render(<PageLoadingState label="Preparing this page" />);
    act(() => { vi.advanceTimersByTime(PAGE_LOADING_DELAY_MS); });
    view.unmount();

    expect(await settledAfter(holdForLoader(Promise.resolve("page")), 1)).toBe(true);
  });
});
