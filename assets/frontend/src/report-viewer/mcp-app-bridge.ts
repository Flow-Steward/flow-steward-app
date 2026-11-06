/**
 * The View side of MCP Apps (io.modelcontextprotocol/ui, spec 2026-01-26) for
 * the report viewer: initialize, receive the tool result, follow host theme
 * changes, report size, and answer teardown.
 *
 * Only the embedding host may talk to the View. Its origin is not known in
 * advance (hosts serve the View from their own sandbox origin), so it is pinned
 * from the first accepted message of window.parent and enforced afterwards.
 * Anything else is counted and dropped. The report snapshot arrives only in the
 * tool result's `_meta`, which hosts keep from the model; the bridge never
 * fetches, never holds a token and never calls back into the server.
 */

export const REPORT_META_KEY = "flowsteward.org/report";
export const MCP_APPS_PROTOCOL_VERSION = "2026-01-26";

export type HostContext = { theme?: "light" | "dark"; locale?: string };

export type ToolResultPayload = {
  /** The snapshot, when the tool result carried one in `_meta`. */
  report: unknown;
  /** The model-visible summary, shown when there is no snapshot. */
  summary: string;
};

export type BridgeHandlers = {
  onToolResult: (payload: ToolResultPayload) => void;
  onHostContext: (context: HostContext) => void;
  onCancelled: () => void;
};

export type BridgeStats = { rejected: number; lastRejection: string; hostOrigin: string | null };

type JsonRpcMessage = {
  jsonrpc: "2.0";
  id?: number | string;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: unknown;
};

type Pending = { resolve: (value: unknown) => void; reject: (reason: unknown) => void };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * The readable text of the tool's model-visible envelope,
 * `{ ok, tool, data: { state, text, report, ... }, meta }`: `data.text` is the
 * report summary when the report is ready, and says why there is none otherwise.
 */
export function envelopeText(envelope: unknown): string {
  return isRecord(envelope) && isRecord(envelope.data) ? textOf(envelope.data.text) : "";
}

/** A text content block is the same envelope as JSON; plain text is shown as it is. */
function contentText(text: string): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return text;
  }
  return envelopeText(parsed);
}

/** The snapshot from `_meta` and a readable summary from the model-visible envelope. */
export function readToolResult(params: Record<string, unknown>): ToolResultPayload {
  const meta = isRecord(params._meta) ? params._meta : {};
  const content = Array.isArray(params.content) ? params.content : [];
  const firstText = content.find((item) => isRecord(item) && item.type === "text");
  return {
    report: meta[REPORT_META_KEY],
    summary:
      envelopeText(params.structuredContent) || (isRecord(firstText) ? contentText(textOf(firstText.text)) : ""),
  };
}

function readHostContext(value: unknown): HostContext {
  if (!isRecord(value)) return {};
  const context: HostContext = {};
  if (value.theme === "light" || value.theme === "dark") context.theme = value.theme;
  if (typeof value.locale === "string") context.locale = value.locale;
  return context;
}

export type McpAppBridge = {
  start: () => void;
  stop: () => void;
  stats: () => BridgeStats;
};

export function createMcpAppBridge(
  view: Window,
  handlers: BridgeHandlers,
  appInfo: { name: string; version: string },
): McpAppBridge {
  const pending = new Map<number, Pending>();
  let nextId = 1;
  let hostOrigin: string | null = null;
  let rejected = 0;
  let lastRejection = "";
  let lastHeight = 0;
  let resizeObserver: ResizeObserver | null = null;

  const post = (message: JsonRpcMessage) => {
    // Before the host has spoken its origin is unknown, so only ui/initialize
    // (protocol version and app name, no report data) goes to "*"; every later
    // message is addressed to the pinned host origin.
    view.parent.postMessage(message, hostOrigin ?? "*"); // NOSONAR: the first message carries no data
  };
  const notify = (method: string, params: Record<string, unknown> = {}) =>
    post({ jsonrpc: "2.0", method, params });
  const request = (method: string, params: Record<string, unknown>) => {
    const id = nextId++;
    post({ jsonrpc: "2.0", id, method, params });
    return new Promise<unknown>((resolve, reject) => pending.set(id, { resolve, reject }));
  };
  const reject = (reason: string) => {
    rejected += 1;
    lastRejection = reason;
  };

  const handleResponse = (message: JsonRpcMessage) => {
    const waiting = typeof message.id === "number" ? pending.get(message.id) : undefined;
    if (!waiting || (message.result === undefined && message.error === undefined)) {
      reject("response without a matching request");
      return;
    }
    pending.delete(message.id as number);
    if (message.error === undefined) waiting.resolve(message.result);
    else waiting.reject(message.error);
  };

  const handleRequestOrNotification = (message: JsonRpcMessage) => {
    switch (message.method) {
      case "ui/notifications/tool-result":
        if (!message.params) {
          reject("tool-result without params");
          return;
        }
        handlers.onToolResult(readToolResult(message.params));
        return;
      case "ui/notifications/host-context-changed":
        handlers.onHostContext(readHostContext(message.params));
        return;
      case "ui/notifications/tool-cancelled":
        handlers.onCancelled();
        return;
      case "ui/notifications/tool-input":
      case "ui/notifications/tool-input-partial":
        return;
      case "ui/resource-teardown":
        if (message.id !== undefined) post({ jsonrpc: "2.0", id: message.id, result: {} });
        return;
      default:
        // A request the View does not implement gets a JSON-RPC error, not silence.
        if (message.id !== undefined) {
          post({ jsonrpc: "2.0", id: message.id, error: { code: -32601, message: "Method not found" } });
        }
    }
  };

  const onMessage = (event: MessageEvent) => {
    if (event.source !== view.parent || view.parent === view) {
      reject("source is not the host frame");
      return;
    }
    if (hostOrigin !== null && event.origin !== hostOrigin) {
      reject("origin changed");
      return;
    }
    const message: unknown = event.data;
    if (!isRecord(message) || message.jsonrpc !== "2.0") {
      reject("not a JSON-RPC 2.0 object");
      return;
    }
    if (message.method !== undefined && typeof message.method !== "string") {
      reject("method is not a string");
      return;
    }
    if (message.params !== undefined && !isRecord(message.params)) {
      reject("params is not an object");
      return;
    }
    if (message.id !== undefined && typeof message.id !== "number" && typeof message.id !== "string") {
      reject("id is neither a number nor a string");
      return;
    }
    hostOrigin ??= event.origin;
    const rpc = message as JsonRpcMessage;
    if (rpc.method === undefined) handleResponse(rpc);
    else handleRequestOrNotification(rpc);
  };

  const reportSize = () => {
    const root = view.document.documentElement;
    const height = Math.ceil(root.scrollHeight);
    if (height === lastHeight) return;
    lastHeight = height;
    notify("ui/notifications/size-changed", { width: Math.ceil(root.scrollWidth), height });
  };

  return {
    start() {
      view.addEventListener("message", onMessage);
      if (typeof ResizeObserver === "function") {
        resizeObserver = new ResizeObserver(reportSize);
        resizeObserver.observe(view.document.documentElement);
      }
      request("ui/initialize", {
        protocolVersion: MCP_APPS_PROTOCOL_VERSION,
        appInfo,
        appCapabilities: { availableDisplayModes: ["inline"] },
      }).then(
        (result) => {
          handlers.onHostContext(readHostContext(isRecord(result) ? result.hostContext : undefined));
          notify("ui/notifications/initialized");
        },
        () => {
          reject("initialize was refused");
        },
      );
    },
    stop() {
      view.removeEventListener("message", onMessage);
      resizeObserver?.disconnect();
      pending.clear();
    },
    stats: () => ({ rejected, lastRejection, hostOrigin }),
  };
}
