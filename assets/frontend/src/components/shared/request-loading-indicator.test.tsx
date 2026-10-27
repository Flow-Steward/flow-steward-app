import { act, render, screen } from "@testing-library/react";

import { PageLoadingState } from "@/components/shared/page-loading-state";
import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import { beginRequest } from "@/lib/http/request-activity";

describe("RequestLoadingIndicator", () => {
  let finish: (() => void) | undefined;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    act(() => finish?.());
    finish = undefined;
    vi.useRealTimers();
  });

  it("paints its loader for a slow request when no page shows one", () => {
    render(<RequestLoadingIndicator />);
    act(() => {
      finish = beginRequest();
    });
    act(() => {
      vi.advanceTimersByTime(450);
    });

    expect(screen.getByRole("status", { name: "Loading requests" })).toBeInTheDocument();
  });

  it("leaves the wait to the page loader instead of painting a second one", () => {
    render(
      <>
        <PageLoadingState label="Preparing the workflow" />
        <RequestLoadingIndicator />
      </>,
    );
    act(() => {
      finish = beginRequest();
    });
    act(() => {
      vi.advanceTimersByTime(450);
    });

    expect(screen.getByText("Preparing the workflow")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Loading requests" })).not.toBeInTheDocument();
    // Interaction stays blocked while the request runs.
    expect(screen.getByRole("dialog", { name: "Loading requests" })).toHaveAttribute("aria-busy", "true");
  });
});
