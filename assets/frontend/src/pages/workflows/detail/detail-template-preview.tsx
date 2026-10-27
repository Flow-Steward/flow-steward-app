import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useRef, useState } from "react";

import { templatePreviewBody } from "@/pages/workflows/detail/detail-template-core";

// Shared with core/entrypoints/web/template_preview_frame.py.
export const TEMPLATE_PREVIEW_FRAME_URL = "/app/template-preview-frame";
const READY_MESSAGE = "flow-steward:template-preview-ready";
const CONTENT_MESSAGE = "flow-steward:template-preview";
// The origin a page sandboxed into its own opaque origin reports.
const OPAQUE_ORIGIN = "null";

/**
 * The preview is shown in a page the server serves with its own policy: an
 * opaque origin without same-origin access, no script but its own, and images
 * from any HTTPS address. A `srcdoc` frame would inherit the admin's policy,
 * which allows images only from this server. The rendered HTML is handed over
 * once the page says it is ready, and again whenever it changes.
 */
function TemplatePreviewFrame({ html }: Readonly<{ html: string }>) {
  const { t } = useI18n();
  const frameRef = useRef<HTMLIFrameElement>(null);
  // Counts the page's "ready" messages, so a reloaded page gets the HTML again.
  const [readyCount, setReadyCount] = useState(0);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const frameWindow = frameRef.current?.contentWindow;
      // Only the preview page, which is sandboxed into an opaque ("null") origin.
      if (!frameWindow || event.source !== frameWindow || event.origin !== OPAQUE_ORIGIN) return;
      const data = event.data as { type?: unknown } | null;
      if (data?.type === READY_MESSAGE) setReadyCount((count) => count + 1);
    };
    globalThis.addEventListener("message", onMessage);
    return () => globalThis.removeEventListener("message", onMessage);
  }, []);

  useEffect(() => {
    if (!readyCount) return;
    // The page has an opaque origin, so "*" is the only target that reaches it;
    // the HTML is the author's own preview and the page checks the sender.
    frameRef.current?.contentWindow?.postMessage({ type: CONTENT_MESSAGE, html }, "*"); // NOSONAR: an opaque origin cannot be named as a target
  }, [html, readyCount]);

  return (
    <iframe
      className="h-96 w-full rounded border border-default bg-[var(--fs-bg-surface)]"
      ref={frameRef}
      referrerPolicy="no-referrer"
      // No sandbox attribute: the page's own response header sandboxes it
      // (opaque origin, its script only). Some embedded Chromium browsers run
      // no script in a network-loaded frame that has the attribute.
      src={TEMPLATE_PREVIEW_FRAME_URL}
      title={t("react.workflows.template_preview", "Template preview")}
    />
  );
}


export function TemplateResultPreview({
  format,
  stale,
  text,
}: Readonly<{ format: string; stale: boolean; text: string }>) {
  const { t } = useI18n();
  const [view, setView] = useState<"text" | "preview">("text");
  const canPreview = format === "markdown" || format === "html";
  const showing = canPreview ? view : "text";
  const tab = (key: "text" | "preview", label: string) => (
    <button
      aria-pressed={showing === key}
      className={`rounded border px-2 py-1 text-xs ${
        showing === key
          ? "border-selected fs-surface-selected fs-ink-brand"
          : "border-default fs-ink-secondary"
      }`}
      onClick={() => setView(key)}
      type="button"
    >
      {label}
    </button>
  );
  return (
    <section aria-label={t("react.workflows.template_result_variant_2c42625f", "Template result")} className="mt-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <p className="fs-ink-secondary">{t("react.workflows.template_result", "Template result ·")}{" "}{format}</p>
        {tab("text", t("react.workflows.display_result_text", "Result text"))}
        {canPreview ? tab("preview", t("registry.workflow_detail.run_lab.tab.preview", "Preview")) : null}
      </div>
      {stale ? (
        <p className="rounded border border-attention fs-surface-attention px-2 py-1 fs-ink-attention" role="status">
          {t("react.workflows.this_is_the_result_of_the_previous_test_the_template_or_the_test_inputs_changed_since_run_the_test_again_to_se", "This is the result of the previous test. The template or the test inputs changed since; run the test again to see the current result.")}</p>
      ) : null}
      {showing === "text" ? (
        <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded fs-surface-inset p-2 font-mono text-[12px] fs-ink-primary">
          {text}
        </pre>
      ) : (
        <TemplatePreviewFrame html={templatePreviewBody(text, format)} />
      )}
    </section>
  );
}

/**
 * What the step and its test inputs looked like when each result arrived. Kept
 * with the result object itself, which outlives this component: going to Smart
 * setup unmounts it, and a result coming back from there must still be compared
 * with the step it was produced from, not with the edited one.
 */
const resultKeys = new WeakMap<object, string>();

/**
 * The template part of a step Test Run. It marks the result as old once the
 * step or its test inputs changed since the run, without keeping any history.
 */
export function TemplateTestRunResult({
  currentKey,
  result,
}: Readonly<{ currentKey: string; result: unknown }>) {
  const resultObject = result && typeof result === "object" ? (result as Record<string, unknown>) : null;
  if (resultObject && !resultKeys.has(resultObject)) {
    resultKeys.set(resultObject, currentKey);
  }
  const template = resultObject?.template as Record<string, unknown> | undefined;
  const text = template?.output;
  if (typeof text !== "string") return null;
  const format = typeof template?.format === "string" && template.format ? template.format : "text";
  const producedFrom = resultObject ? resultKeys.get(resultObject) : undefined;
  return (
    <TemplateResultPreview
      format={format}
      stale={producedFrom !== undefined && producedFrom !== currentKey}
      text={text}
    />
  );
}
