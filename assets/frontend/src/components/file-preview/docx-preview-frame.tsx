/**
 * A Word (DOCX) document drawn by docx-preview inside a sandboxed frame (ADR 0077).
 *
 * Two guards, each enough on its own for script:
 * - The frame is `sandbox="allow-same-origin"` without `allow-scripts`: nothing
 *   inside it runs, it opens no popup and cannot navigate the app. Same origin
 *   lets the page write the document into it as DOM, and its CSS stays in it.
 * - The rendered nodes are sanitized first (docx-sanitize.ts), and docx-preview
 *   runs without embedded HTML parts (altChunk), embedded fonts, comments or
 *   tracked changes. Images become `data:` URLs read from the file itself.
 *
 * The document's CSS is applied as a constructed stylesheet, because the app's
 * CSP (inherited by the frame) does not allow inline `<style>` elements.
 */
import { useEffect, useState } from "react";
import { parseAsync, renderDocument, type Options } from "docx-preview";

import { useI18n } from "@/lib/i18n/useI18n";

import { docxStyleText, sanitizeDocxNodes } from "./docx-sanitize";
import { previewFailureMessage } from "./file-preview-messages";

/** The sandbox tokens of the frame: same origin for the page to write into it, never scripts. */
export const DOCX_FRAME_SANDBOX = "allow-same-origin";

const RENDER_OPTIONS: Partial<Options> = {
  inWrapper: true,
  breakPages: true,
  ignoreFonts: true,
  renderAltChunks: false,
  renderComments: false,
  renderChanges: false,
  useBase64URL: true,
  experimental: false,
  debug: false,
};

const FRAME_CSS = "html,body{margin:0;padding:0;background:transparent;}";

type RenderedDocx = { status: "loading" } | { status: "ready"; body: Node[]; css: string } | { status: "failed" };

/** The sanitized document into the frame, its CSS as an adopted sheet where the browser has them. */
function fillFrame(frame: HTMLIFrameElement, body: readonly Node[], css: string): void {
  const frameDocument = frame.contentDocument;
  const frameWindow = frame.contentWindow as (Window & typeof globalThis) | null;
  if (!frameDocument?.body) return;
  // The nodes move into the frame (no copies): docx-preview sizes drawn shapes on them after rendering.
  frameDocument.body.replaceChildren(...body);
  try {
    if (frameWindow && "adoptedStyleSheets" in frameDocument && typeof frameWindow.CSSStyleSheet === "function") {
      const sheet = new frameWindow.CSSStyleSheet();
      sheet.replaceSync(`${FRAME_CSS}\n${css}`);
      frameDocument.adoptedStyleSheets = [sheet];
    }
  } catch {
    // A browser without constructed sheets shows the document unstyled, still readable.
  }
}

export default function DocxPreviewFrame({ data, name }: Readonly<{ data: ArrayBuffer; name: string }>) {
  const { t } = useI18n();
  const [rendered, setRendered] = useState<RenderedDocx>({ status: "loading" });
  const [frame, setFrame] = useState<HTMLIFrameElement | null>(null);

  useEffect(() => {
    let live = true;
    parseAsync(data, RENDER_OPTIONS)
      .then((parsed: unknown) => renderDocument(parsed, RENDER_OPTIONS))
      .then(
        (nodes) => {
          if (!live) return;
          const css = docxStyleText(nodes);
          const body = sanitizeDocxNodes(nodes.filter((node) => node.nodeName !== "STYLE"));
          setRendered({ status: "ready", body, css });
        },
        () => {
          if (live) setRendered({ status: "failed" });
        },
      );
    return () => {
      live = false;
    };
  }, [data]);

  useEffect(() => {
    if (!frame || rendered.status !== "ready") return;
    const fill = () => fillFrame(frame, rendered.body, rendered.css);
    fill();
    // A browser may replace the frame's first empty document once it loads; write again then.
    frame.addEventListener("load", fill);
    return () => frame.removeEventListener("load", fill);
  }, [frame, rendered]);

  if (rendered.status === "failed") {
    return (
      <p className="fs-file-preview-status fs-ink-danger" role="alert">
        {previewFailureMessage("unreadable", t)}
      </p>
    );
  }
  return (
    <iframe
      className="fs-file-preview-docx"
      data-testid="file-preview-docx"
      ref={setFrame}
      referrerPolicy="no-referrer"
      sandbox={DOCX_FRAME_SANDBOX}
      title={name}
    />
  );
}
