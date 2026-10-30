import { act, render, screen, waitFor } from "@testing-library/react";

import { MaintenanceBanner } from "@/components/layout/maintenance-banner";
import platformStyles from "../../../../platform-components.css?inline";

type MaintenanceStatus = "disabled" | "draining" | "enabled";

function response(status: MaintenanceStatus) {
  return new Response(
    JSON.stringify({
      status,
      activated_at: status === "disabled" ? null : "2026-08-23T10:00:00Z",
      owner_kind: status === "disabled" ? null : "manual",
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

describe("MaintenanceBanner", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("shows enabled maintenance without a reason field", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response("enabled")));

    render(<MaintenanceBanner />);

    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Maintenance Mode is enabled");
    expect(banner).toHaveTextContent(
      "Jobs, schedules, integrations, and changes are paused",
    );
    expect(screen.queryByText(/reason/i)).not.toBeInTheDocument();
  });

  it("renders no banner while maintenance is disabled", async () => {
    const fetchMock = vi.fn(async () => response("disabled"));
    vi.stubGlobal("fetch", fetchMock);

    render(<MaintenanceBanner />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows draining as a distinct closed state", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response("draining")));

    render(<MaintenanceBanner />);

    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Maintenance Mode is starting");
    expect(banner).toHaveTextContent(
      "New work is paused while active work finishes",
    );
  });

  it("re-checks every five minutes and on window focus at most once a minute", async () => {
    let status: MaintenanceStatus = "disabled";
    const fetchMock = vi.fn(async () => response(status));
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
    render(<MaintenanceBanner />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    status = "draining";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Maintenance Mode is starting",
    );

    status = "enabled";
    const focus = async () => {
      await act(async () => {
        globalThis.dispatchEvent(new Event("focus"));
        await Promise.resolve();
        await Promise.resolve();
      });
    };
    await focus();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Maintenance Mode is starting",
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    await focus();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Maintenance Mode is enabled",
    );
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("cancels an in-flight status request on unmount", () => {
    const observedSignals: AbortSignal[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.signal) {
          observedSignals.push(init.signal);
        }
        return new Promise<Response>(() => undefined);
      }),
    );

    const view = render(<MaintenanceBanner />);
    view.unmount();

    expect(observedSignals).toHaveLength(1);
    expect(observedSignals[0]?.aborted).toBe(true);
  });

  it("uses the high-contrast maintenance token treatment", () => {
    expect(platformStyles).toMatch(
      /\.fs-maintenance-banner\s*\{[^}]*background:\s*var\(--fs-status-warning-bg\);[^}]*border-bottom:\s*1px solid var\(--fs-status-warning-border\);[^}]*color:\s*var\(--fs-text-primary\);/s,
    );
    expect(platformStyles).toMatch(
      /\.fs-maintenance-banner\[data-status="enabled"\]\s*\{[^}]*background:\s*var\(--fs-status-danger-bg\);[^}]*border-bottom-color:\s*var\(--fs-status-danger-border\);/s,
    );
  });
});
