import { act, render, screen } from "@testing-library/react";

import { DsTimeline } from "@/components/shared/design-system";
import { LOADER_DELAY_MS } from "@/components/shared/use-loader-delay";

describe("DsTimeline pending state", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("paints nothing before the Loader delay", () => {
    render(<DsTimeline ariaLabel="Recent activity" events={[]} loading />);

    act(() => {
      vi.advanceTimersByTime(LOADER_DELAY_MS - 1);
    });

    expect(screen.getByRole("status", { name: "Recent activity" })).toBeEmptyDOMElement();
  });

  it("then holds three events on the canon Skeleton, with the real rail", () => {
    render(<DsTimeline ariaLabel="Recent activity" events={[]} loading />);

    act(() => {
      vi.advanceTimersByTime(LOADER_DELAY_MS);
    });

    const region = screen.getByRole("status", { name: "Recent activity" });
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    // eslint-disable-next-line testing-library/no-node-access
    const bars = region.querySelectorAll(".fs-skeleton.fs-skeleton-line");
    expect(bars).toHaveLength(6);
    // eslint-disable-next-line testing-library/no-node-access
    expect(region.querySelectorAll(".fs-timeline-dot")).toHaveLength(3);
    // eslint-disable-next-line testing-library/no-node-access
    expect(region.querySelector(".fs-timeline-skeleton")).toBeNull();
  });
});
