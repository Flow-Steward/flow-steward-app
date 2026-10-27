import { act, render, screen } from "@testing-library/react";

import { PAGE_LOADING_DELAY_MS, PageLoadingState } from "@/components/shared/page-loading-state";

describe("PageLoadingState", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("paints nothing before the Loader delay, so a fast restore never flashes", () => {
    render(<PageLoadingState label="Restoring your session" />);

    act(() => {
      vi.advanceTimersByTime(PAGE_LOADING_DELAY_MS - 1);
    });

    const region = screen.getByRole("status");
    expect(region).toBeEmptyDOMElement();
    expect(screen.queryByText("Restoring your session")).not.toBeInTheDocument();
  });

  it("then shows the published wide page Loader and the line that names the wait", () => {
    render(<PageLoadingState label="Restoring your session" />);

    act(() => {
      vi.advanceTimersByTime(PAGE_LOADING_DELAY_MS);
    });

    const region = screen.getByRole("status");
    expect(region).toHaveTextContent("Restoring your session");
    const loader = screen.getByTestId("loader-mark");
    expect(loader).toHaveAttribute("aria-hidden", "true");
    expect(loader).toHaveClass("fs-loader", "lg");
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("hands a painted wait to the page loader that replaces it without a gap, and counts an unpainted one from its start", () => {
    // The session restore gives way to the page in one commit: the page's loader mounts as it unmounts.
    const view = render(<PageLoadingState key="restore" label="Restoring your session" standalone />);
    act(() => {
      vi.advanceTimersByTime(PAGE_LOADING_DELAY_MS);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Restoring your session");
    view.rerender(<PageLoadingState key="page" label="Preparing this page" />);
    expect(screen.getByRole("status")).toHaveTextContent("Preparing this page");
    view.unmount();

    const { rerender } = render(<PageLoadingState key="restore" label="Restoring your session" standalone />);
    act(() => {
      vi.advanceTimersByTime(300);
    });
    rerender(<PageLoadingState key="page" label="Preparing this page" />);
    act(() => {
      vi.advanceTimersByTime(PAGE_LOADING_DELAY_MS - 300 - 1);
    });
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Preparing this page");
  });

  it("starts a wait of its own once every page loader has gone", () => {
    const view = render(<PageLoadingState label="Restoring your session" />);
    act(() => {
      vi.advanceTimersByTime(PAGE_LOADING_DELAY_MS);
    });
    view.unmount();
    render(<PageLoadingState label="Preparing this page" />);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("draws no card, heading or eyebrow: a wait is an absence, not a surface", () => {
    render(<PageLoadingState label="Preparing this page" />);

    act(() => {
      vi.advanceTimersByTime(PAGE_LOADING_DELAY_MS);
    });

    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).not.toHaveClass("fs-card");
  });
});
