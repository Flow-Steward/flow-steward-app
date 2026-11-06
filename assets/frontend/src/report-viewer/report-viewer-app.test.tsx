import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import fixture from "@/components/report/__fixtures__/sales-report-snapshot.json";

import results from "./__fixtures__/workflow-run-report-results.json";
import type * as BridgeModule from "./mcp-app-bridge";
import { readToolResult, REPORT_META_KEY, type BridgeHandlers } from "./mcp-app-bridge";
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

  it.each([
    ["pending", "Job job-1 is running; its report is not ready yet."],
    ["no_report", "Job job-1 ended (failed) without a report."],
  ] as const)("shows the text of the tool's actual %s result, not its JSON", (state, text) => {
    render(<ReportViewerApp />);
    act(() => host().onToolResult(readToolResult(results[state])));
    expect(screen.getByRole("status")).toHaveTextContent(text);
    expect(screen.getByRole("status")).not.toHaveTextContent("{");
  });

  it("renders the snapshot of the tool's actual ready result", () => {
    render(<ReportViewerApp />);
    act(() => host().onToolResult(readToolResult(results.ready)));
    expect(screen.getByRole("heading", { level: 2, name: "Weekly sales" })).toBeInTheDocument();
  });

  it("says when the host cancelled the request, and stops the bridge on unmount", () => {
    const { unmount } = render(<ReportViewerApp />);
    act(() => host().onCancelled());
    expect(screen.getByRole("status")).toHaveTextContent("The request was cancelled.");
    unmount();
    expect(captured.stop).toHaveBeenCalled();
  });
});

type OpenAiWindow = Window & { openai?: { toolOutput?: unknown; toolResponseMetadata?: unknown } };

describe("ReportViewerApp on window.openai when MCP Apps stays silent", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    delete (window as OpenAiWindow).openai;
  });

  function openAiResult(state: "ready" | "pending" | "no_report") {
    const result: { structuredContent: unknown; _meta?: unknown } = results[state];
    (window as OpenAiWindow).openai = { toolOutput: result.structuredContent, toolResponseMetadata: result._meta ?? null };
  }

  it.each([
    ["pending", "Job job-1 is running; its report is not ready yet."],
    ["no_report", "Job job-1 ended (failed) without a report."],
  ] as const)("shows the text of the %s envelope instead of waiting forever", (state, text) => {
    openAiResult(state);
    render(<ReportViewerApp />);
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.getByRole("status")).toHaveTextContent(text);
  });

  it("renders the snapshot from the result metadata", () => {
    openAiResult("ready");
    render(<ReportViewerApp />);
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.getByRole("heading", { level: 2, name: "Weekly sales" })).toBeInTheDocument();
    expect(results.ready._meta[REPORT_META_KEY]).toEqual(fixture);
  });

  it("keeps waiting until the result is set, then follows set_globals", () => {
    (window as OpenAiWindow).openai = { toolOutput: null, toolResponseMetadata: null };
    render(<ReportViewerApp />);
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for the report…");
    openAiResult("no_report");
    act(() => {
      window.dispatchEvent(new Event("openai:set_globals"));
    });
    expect(screen.getByRole("status")).toHaveTextContent("Job job-1 ended (failed) without a report.");
  });
});
