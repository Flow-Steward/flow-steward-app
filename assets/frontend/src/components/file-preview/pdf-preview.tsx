/**
 * A PDF drawn by pdf.js onto a canvas, one page at a time (ADR 0080).
 *
 * Only the page picture is drawn: no annotation or form layer, no text layer,
 * no links, no document JavaScript (pdf.js runs scripts only through a
 * scripting sandbox this previewer never creates), no XFA, and no WebAssembly,
 * which the app's CSP does not allow compiling. pdf.js 5 and later no longer
 * evaluate code from a document, so its former `isEvalSupported` switch is gone.
 * The worker and the image decoders are the app's own files, never a CDN.
 */
import { ChevronLeft, ChevronRight, Minus, MoveHorizontal, Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { AnnotationMode, GlobalWorkerOptions, getDocument, type PDFDocumentProxy, type RenderTask } from "pdfjs-dist";

import { useI18n } from "@/lib/i18n/useI18n";

import { previewFailureMessage, type PreviewFailure } from "./file-preview-messages";
import { PDF_JS_DECODERS_URL, PDF_JS_WORKER_URL } from "./pdf-assets";

GlobalWorkerOptions.workerSrc = PDF_JS_WORKER_URL;

const MIN_SCALE = 0.25;
const MAX_SCALE = 4;
const ZOOM_STEP = 1.25;
/** The largest canvas drawn, in device pixels; above it the page is drawn at a lower density. */
const MAX_CANVAS_PIXELS = 16_777_216;

type PdfZoom = { mode: "fit" } | { mode: "scale"; scale: number };

type PdfDocument = { status: "loading" } | { status: "ready"; document: PDFDocumentProxy } | { status: "failed"; reason: PreviewFailure };

function failureOf(error: unknown): PreviewFailure {
  return error instanceof Error && error.name === "PasswordException" ? "password" : "unreadable";
}

function clampScale(scale: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
}

/** Device pixels per CSS pixel: the screen's density, lowered when the canvas would be too large. */
function canvasDensity(width: number, height: number): number {
  const density = Math.max(1, globalThis.devicePixelRatio || 1);
  const area = width * height * density * density;
  return area > MAX_CANVAS_PIXELS ? Math.max(0.5, Math.sqrt(MAX_CANVAS_PIXELS / (width * height))) : density;
}

function useWidth(element: HTMLElement | null): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0]?.contentRect.width ?? 0);
      setWidth((current) => (current === next ? current : next));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return width;
}

export default function PdfPreview({ data, name }: Readonly<{ data: ArrayBuffer; name: string }>) {
  const { t } = useI18n();
  const [loaded, setLoaded] = useState<PdfDocument>({ status: "loading" });
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState<PdfZoom>({ mode: "fit" });
  const [drawnScale, setDrawnScale] = useState(1);
  const [drawFailed, setDrawFailed] = useState(false);
  const [viewport, setViewport] = useState<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const width = useWidth(viewport);

  useEffect(() => {
    // pdf.js hands the bytes to its worker; a copy keeps the caller's buffer usable.
    const task = getDocument({
      data: new Uint8Array(data.slice(0)),
      enableXfa: false,
      useWasm: false,
      wasmUrl: PDF_JS_DECODERS_URL || undefined,
      stopAtErrors: false,
    });
    let live = true;
    task.promise.then(
      (document) => {
        if (live) setLoaded({ status: "ready", document });
      },
      (error: unknown) => {
        if (live) setLoaded({ status: "failed", reason: failureOf(error) });
      },
    );
    return () => {
      live = false;
      void task.destroy();
    };
  }, [data]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (loaded.status !== "ready" || !canvas) return;
    let live = true;
    let render: RenderTask | null = null;
    loaded.document.getPage(page).then(
      (pdfPage) => {
        if (!live) return;
        const natural = pdfPage.getViewport({ scale: 1 });
        const scale = zoom.mode === "fit" && width > 0 ? clampScale(width / natural.width) : zoom.mode === "scale" ? zoom.scale : 1;
        const cssWidth = Math.floor(natural.width * scale);
        const cssHeight = Math.floor(natural.height * scale);
        const density = canvasDensity(cssWidth, cssHeight);
        const drawn = pdfPage.getViewport({ scale: scale * density });
        canvas.width = Math.floor(drawn.width);
        canvas.height = Math.floor(drawn.height);
        canvas.style.width = `${cssWidth}px`;
        canvas.style.height = `${cssHeight}px`;
        setDrawnScale(scale);
        // ENABLE draws annotation appearances (stamps, signatures) into the picture; no
        // annotation layer, form field or action is created.
        render = pdfPage.render({ canvas, viewport: drawn, annotationMode: AnnotationMode.ENABLE });
        render.promise.then(
          () => {
            if (live) setDrawFailed(false);
          },
          (error: unknown) => {
            if (live && !(error instanceof Error && error.name === "RenderingCancelledException")) setDrawFailed(true);
          },
        );
      },
      () => {
        if (live) setDrawFailed(true);
      },
    );
    return () => {
      live = false;
      render?.cancel();
    };
  }, [loaded, page, zoom, width]);

  if (loaded.status === "failed") {
    return (
      <p className="fs-file-preview-status fs-ink-danger" role="alert">
        {previewFailureMessage(loaded.reason, t)}
      </p>
    );
  }
  const pages = loaded.status === "ready" ? loaded.document.numPages : 0;
  const current = Math.min(page, Math.max(1, pages));
  return (
    <div className="fs-file-preview-pdf" data-testid="file-preview-pdf">
      <div className="fs-file-preview-toolbar">
        <button
          aria-label={t("react.jobs.file_preview_previous_page", "Previous page")}
          className="fs-icon-button"
          disabled={current <= 1}
          onClick={() => setPage(current - 1)}
          type="button"
        >
          <ChevronLeft aria-hidden="true" />
        </button>
        <span aria-live="polite">
          {pages ? t("react.jobs.file_preview_page_position", "Page {page} of {total}", { page: String(current), total: String(pages) }) : null}
        </span>
        <button
          aria-label={t("react.jobs.file_preview_next_page", "Next page")}
          className="fs-icon-button"
          disabled={current >= pages}
          onClick={() => setPage(current + 1)}
          type="button"
        >
          <ChevronRight aria-hidden="true" />
        </button>
        <span className="fs-file-preview-toolbar-gap" />
        <button
          aria-label={t("react.jobs.file_preview_zoom_out", "Zoom out")}
          className="fs-icon-button"
          disabled={drawnScale <= MIN_SCALE}
          onClick={() => setZoom({ mode: "scale", scale: clampScale(drawnScale / ZOOM_STEP) })}
          type="button"
        >
          <Minus aria-hidden="true" />
        </button>
        <span>{`${Math.round(drawnScale * 100)}%`}</span>
        <button
          aria-label={t("react.jobs.file_preview_zoom_in", "Zoom in")}
          className="fs-icon-button"
          disabled={drawnScale >= MAX_SCALE}
          onClick={() => setZoom({ mode: "scale", scale: clampScale(drawnScale * ZOOM_STEP) })}
          type="button"
        >
          <Plus aria-hidden="true" />
        </button>
        <button
          aria-label={t("react.jobs.file_preview_fit_width", "Fit to width")}
          aria-pressed={zoom.mode === "fit"}
          className="fs-icon-button"
          onClick={() => setZoom({ mode: "fit" })}
          type="button"
        >
          <MoveHorizontal aria-hidden="true" />
        </button>
      </div>
      <div className="fs-file-preview-pdf-page" ref={setViewport}>
        <canvas aria-label={t("react.jobs.file_preview_pdf_page", "{name}, page {page}", { name, page: String(current) })} ref={canvasRef} role="img" />
      </div>
      {drawFailed ? (
        <p className="fs-file-preview-status fs-ink-danger" role="alert">
          {previewFailureMessage("unreadable", t)}
        </p>
      ) : null}
    </div>
  );
}
