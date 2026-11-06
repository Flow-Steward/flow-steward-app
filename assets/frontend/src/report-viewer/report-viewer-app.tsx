/**
 * The MCP Apps report viewer: the shared report renderer inside a host's
 * sandboxed frame. It shows the snapshot the tool result carries in `_meta`;
 * without one it shows the model-visible summary. It fetches nothing.
 */
import { useEffect, useState } from "react";

import { ReportView } from "@/components/report";

import { createMcpAppBridge, type HostContext, REPORT_META_KEY } from "./mcp-app-bridge";
import { viewerLabels } from "./viewer-labels";

const VIEWER_APP_INFO = { name: "flow-steward-report-viewer", version: "1.0.0" };
/** ChatGPT also exposes the tool result on window.openai; read it if MCP Apps stays silent. */
const OPENAI_FALLBACK_DELAY_MS = 3000;

type ViewerState =
  | { kind: "waiting" }
  | { kind: "report"; report: unknown }
  | { kind: "summary"; summary: string }
  | { kind: "cancelled" };

type OpenAiGlobals = { toolResponseMetadata?: Record<string, unknown>; toolOutput?: Record<string, unknown> };

function applyTheme(theme: HostContext["theme"]): void {
  if (!theme) return;
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.classList.toggle("dark", theme === "dark");
}

function readOpenAiGlobals(): { report: unknown; summary: string } | null {
  const globals = (window as unknown as { openai?: OpenAiGlobals }).openai;
  if (!globals) return null;
  const report = globals.toolResponseMetadata?.[REPORT_META_KEY];
  const summary = globals.toolOutput?.summary_text;
  if (report === undefined && typeof summary !== "string") return null;
  return { report, summary: typeof summary === "string" ? summary : "" };
}

export function ReportViewerApp() {
  const [state, setState] = useState<ViewerState>({ kind: "waiting" });
  const [hostLocale, setHostLocale] = useState<string | undefined>(undefined);

  useEffect(() => {
    const show = (report: unknown, summary: string) =>
      setState(report === undefined ? { kind: "summary", summary } : { kind: "report", report });
    const bridge = createMcpAppBridge(
      window,
      {
        onToolResult: ({ report, summary }) => show(report, summary),
        onHostContext: (context) => {
          applyTheme(context.theme);
          if (context.locale) setHostLocale(context.locale);
        },
        onCancelled: () => setState({ kind: "cancelled" }),
      },
      VIEWER_APP_INFO,
    );
    bridge.start();
    const fallback = () => {
      const globals = readOpenAiGlobals();
      if (globals) setState((current) => (current.kind === "waiting" ? toState(globals) : current));
    };
    const timer = window.setTimeout(fallback, OPENAI_FALLBACK_DELAY_MS);
    window.addEventListener("openai:set_globals", fallback);
    return () => {
      bridge.stop();
      window.clearTimeout(timer);
      window.removeEventListener("openai:set_globals", fallback);
    };
  }, []);

  const reportLocale =
    state.kind === "report" && typeof (state.report as { locale?: unknown })?.locale === "string"
      ? (state.report as { locale: string }).locale
      : undefined;
  const labels = viewerLabels(hostLocale, reportLocale);

  switch (state.kind) {
    case "report":
      return <ReportView labels={labels} report={state.report} />;
    case "summary":
      return (
        <p className="fs-report-notice" role="status">
          {state.summary || labels.unavailable}
        </p>
      );
    case "cancelled":
      return (
        <p className="fs-report-notice" role="status">
          {labels.cancelled}
        </p>
      );
    default:
      return (
        <p className="fs-report-notice" role="status">
          {labels.waiting}
        </p>
      );
  }
}

function toState(globals: { report: unknown; summary: string }): ViewerState {
  return globals.report === undefined
    ? { kind: "summary", summary: globals.summary }
    : { kind: "report", report: globals.report };
}
