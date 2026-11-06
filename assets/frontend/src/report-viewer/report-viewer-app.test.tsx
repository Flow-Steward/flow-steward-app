import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import fixture from "@/components/report/__fixtures__/sales-report-snapshot.json";

import type * as BridgeModule from "./mcp-app-bridge";
import type { BridgeHandlers } from "./mcp-app-bridge";
import { ReportViewerApp } from "./report-viewer-app";

const captured = vi.hoisted(() => ({ handlers: null as BridgeHandlers | null, stop: vi.fn() }));

vi.mock("./mcp-app-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof BridgeModule>()),
  createMcpAppBridge: (_view: Window, handlers: BridgeHandlers) => {
    captured.handlers = handlers;
    return { start: vi.fn(), stop: captured.stop, stats: vi.fn() };
  },
}));

vi.mock("@/components/report/report-chart-loader", () => ({
  loadReportChartEngine: () =>
    Promise.resolve({ mountReportChart: () => ({ update: vi.fn(), resize: vi.fn(), dispose: vi.fn() }) }),
}));

function host(): BridgeHandlers {
  if (!captured.handlers) throw new Error("the viewer did not start its bridge");
  return captured.handlers;
}

describe("ReportViewerApp", () => {
  afterEach(() => {
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.classList.remove("dark");
  });

  it("waits, then renders the snapshot the tool result carries", () => {
    render(<ReportViewerApp />);
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for the report…");
    act(() => host().onToolResult({ report: JSON.parse(JSON.stringify(fixture)), summary: "Weekly sales" }));
    expect(screen.getByRole("heading", { level: 2, name: "Weekly sales" })).toBeInTheDocument();
  });

  it("applies the host theme to the root the renderer reads", () => {
    render(<ReportViewerApp />);
    act(() => host().onHostContext({ theme: "dark" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(document.documentElement).toHaveClass("dark");
  });

  it("speaks the host's language when the catalog has it", () => {
    render(<ReportViewerApp />);
    act(() => host().onHostContext({ locale: "lt-LT" }));
    expect(screen.getByRole("status")).toHaveTextContent("Laukiama ataskaitos…");
  });

  it("shows the model-visible summary when the result has no snapshot", () => {
    render(<ReportViewerApp />);
    act(() => host().onToolResult({ report: undefined, summary: "Weekly sales: 3 rows" }));
    expect(screen.getByRole("status")).toHaveTextContent("Weekly sales: 3 rows");
  });

  it("says when the host cancelled the request, and stops the bridge on unmount", () => {
    const { unmount } = render(<ReportViewerApp />);
    act(() => host().onCancelled());
    expect(screen.getByRole("status")).toHaveTextContent("The request was cancelled.");
    unmount();
    expect(captured.stop).toHaveBeenCalled();
  });
});
