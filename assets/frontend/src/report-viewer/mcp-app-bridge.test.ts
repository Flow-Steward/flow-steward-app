import { beforeEach, describe, expect, it, vi } from "vitest";

import results from "./__fixtures__/workflow-run-report-results.json";
import { createMcpAppBridge, readToolResult, REPORT_META_KEY, type BridgeHandlers } from "./mcp-app-bridge";

type Listener = (event: MessageEvent) => void;

const HOST = "https://host.example";

function fakeView() {
  const posted: Array<{ message: Record<string, unknown>; target: string }> = [];
  const parent = { postMessage: vi.fn((message: Record<string, unknown>, target: string) => posted.push({ message, target })) };
  let listener: Listener | null = null;
  const view = {
    parent,
    document,
    addEventListener: (type: string, handler: Listener) => {
      if (type === "message") listener = handler;
    },
    removeEventListener: () => {
      listener = null;
    },
  };
  const deliver = (data: unknown, { source = parent as unknown, origin = HOST } = {}) =>
    listener?.({ data, source, origin } as unknown as MessageEvent);
  return { view: view as unknown as Window, posted, deliver, listening: () => listener !== null };
}

function handlers() {
  return {
    onToolResult: vi.fn<BridgeHandlers["onToolResult"]>(),
    onHostContext: vi.fn<BridgeHandlers["onHostContext"]>(),
    onCancelled: vi.fn<BridgeHandlers["onCancelled"]>(),
  } satisfies BridgeHandlers;
}

const APP = { name: "viewer", version: "1" };

describe("createMcpAppBridge", () => {
  let fake: ReturnType<typeof fakeView>;
  let on: ReturnType<typeof handlers>;

  beforeEach(() => {
    fake = fakeView();
    on = handlers();
  });

  async function started() {
    const bridge = createMcpAppBridge(fake.view, on, APP);
    bridge.start();
    const init = fake.posted[0].message;
    fake.deliver({ jsonrpc: "2.0", id: init.id, result: { hostContext: { theme: "dark", locale: "lt-LT" } } });
    await Promise.resolve();
    await Promise.resolve();
    return bridge;
  }

  it("initializes, applies the host context and confirms with initialized", async () => {
    const bridge = await started();
    expect(fake.posted[0]).toMatchObject({ target: "*", message: { method: "ui/initialize" } });
    expect(fake.posted[0].message.params).toMatchObject({ protocolVersion: "2026-01-26", appInfo: APP });
    expect(on.onHostContext).toHaveBeenCalledWith({ theme: "dark", locale: "lt-LT" });
    expect(fake.posted.at(-1)).toEqual({
      target: HOST,
      message: { jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} },
    });
    expect(bridge.stats().hostOrigin).toBe(HOST);
  });

  it("passes the snapshot from _meta and the summary from the tool's envelope", async () => {
    await started();
    fake.deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: results.ready });
    expect(on.onToolResult).toHaveBeenCalledWith({
      report: results.ready._meta[REPORT_META_KEY],
      summary: "Revenue 48210.5 EUR in the week of 2026-11-02; target met: false.",
    });
  });

  it("drops messages from other frames, other origins and malformed envelopes", async () => {
    const bridge = await started();
    const result = { jsonrpc: "2.0", method: "ui/notifications/tool-result", params: {} };
    fake.deliver(result, { source: {} });
    fake.deliver(result, { origin: "https://evil.example" });
    fake.deliver({ ...result, jsonrpc: "1.0" });
    fake.deliver({ jsonrpc: "2.0", method: 7 });
    fake.deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: [] });
    fake.deliver({ jsonrpc: "2.0", id: 99, result: {} });
    expect(on.onToolResult).not.toHaveBeenCalled();
    expect(bridge.stats().rejected).toBe(6);
  });

  it("answers teardown and refuses requests it does not implement", async () => {
    await started();
    fake.deliver({ jsonrpc: "2.0", id: "t1", method: "ui/resource-teardown", params: {} });
    expect(fake.posted.at(-1)?.message).toEqual({ jsonrpc: "2.0", id: "t1", result: {} });
    fake.deliver({ jsonrpc: "2.0", id: 5, method: "ui/open-link", params: {} });
    expect(fake.posted.at(-1)?.message).toMatchObject({ id: 5, error: { code: -32601 } });
  });

  it("follows host theme changes and cancellation", async () => {
    await started();
    fake.deliver({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { theme: "light" } });
    expect(on.onHostContext).toHaveBeenLastCalledWith({ theme: "light" });
    fake.deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-cancelled", params: {} });
    expect(on.onCancelled).toHaveBeenCalledTimes(1);
  });

  it("stops listening on stop", async () => {
    const bridge = await started();
    bridge.stop();
    expect(fake.listening()).toBe(false);
  });
});

/** The tool's actual results; tests/unit/test_mcp_workflow_report_tool.py pins them to the server. */
const toolResults: Record<string, Record<string, unknown>> = results;

describe("readToolResult with the tool's actual results", () => {
  it.each([
    ["ready", "Revenue 48210.5 EUR in the week of 2026-11-02; target met: false."],
    ["pending", "Job job-1 is running; its report is not ready yet."],
    ["no_report", "Job job-1 ended (failed) without a report."],
  ])("reads data.text of the %s envelope", (state, text) => {
    const withoutMeta = Object.fromEntries(Object.entries(toolResults[state]).filter(([key]) => key !== "_meta"));
    expect(readToolResult(withoutMeta)).toEqual({ report: undefined, summary: text });
  });

  it.each(["ready", "pending", "no_report"])("reads the %s envelope from the JSON text content alone", (state) => {
    const { content } = toolResults[state];
    const fromStructured = readToolResult(toolResults[state]).summary;
    expect(readToolResult({ content }).summary).toBe(fromStructured);
    expect(fromStructured).not.toContain("{");
  });

  it("carries the snapshot only for the ready result", () => {
    expect(readToolResult(toolResults.ready).report).toEqual(results.ready._meta[REPORT_META_KEY]);
    expect(readToolResult(toolResults.pending).report).toBeUndefined();
    expect(readToolResult(toolResults.no_report).report).toBeUndefined();
  });

  it("shows no raw JSON for an envelope without text", () => {
    const text = JSON.stringify({ ok: false, tool: "workflow_run_report", error: { status_code: 401 } });
    expect(readToolResult({ content: [{ type: "text", text }] }).summary).toBe("");
  });
});

describe("readToolResult", () => {
  it("falls back to the first text content when there is no structured summary", () => {
    expect(readToolResult({ content: [{ type: "text", text: "Summary" }] })).toEqual({
      report: undefined,
      summary: "Summary",
    });
  });
});
