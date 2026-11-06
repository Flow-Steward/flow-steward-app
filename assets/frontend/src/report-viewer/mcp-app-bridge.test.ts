import { beforeEach, describe, expect, it, vi } from "vitest";

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

function handlers(): BridgeHandlers & { [K in keyof BridgeHandlers]: ReturnType<typeof vi.fn> } {
  return { onToolResult: vi.fn(), onHostContext: vi.fn(), onCancelled: vi.fn() };
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

  it("passes the snapshot from _meta and the summary from the model-visible fields", async () => {
    await started();
    const report = { kind: "fs.report" };
    fake.deliver({
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: { _meta: { [REPORT_META_KEY]: report }, structuredContent: { summary_text: "Weekly sales" } },
    });
    expect(on.onToolResult).toHaveBeenCalledWith({ report, summary: "Weekly sales" });
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

describe("readToolResult", () => {
  it("falls back to the first text content when there is no structured summary", () => {
    expect(readToolResult({ content: [{ type: "text", text: "Summary" }] })).toEqual({
      report: undefined,
      summary: "Summary",
    });
  });
});
