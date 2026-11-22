/**
 * A file shown in the page itself (ADR 0080): an image, text as characters, or a
 * PDF, spreadsheet or Word document drawn by a previewer the page runs. The
 * browser's own PDF viewer is never used: it differs between browsers, and the
 * in-app browser downloads PDFs instead of showing them.
 *
 * The bytes come from the job's preview endpoint with the session. Each
 * document previewer, and the library it needs, is loaded only when a preview
 * of that kind opens, so the app's own bundle does not carry them.
 */
import { Component, Suspense, lazy, useEffect, useState, type ReactNode } from "react";

import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { fetchArrayBuffer, fetchText } from "@/lib/http/client";
import { useI18n } from "@/lib/i18n/useI18n";

import { isDocumentPreviewKind, type DocumentPreviewKind, type FilePreviewKind } from "./file-preview-model";
import { previewFailureMessage, previewRequestFailure, type PreviewFailure } from "./file-preview-messages";
import "./file-preview.css";

const PdfPreview = lazy(() => import("./pdf-preview"));
const SpreadsheetPreview = lazy(() => import("./spreadsheet-preview"));
const DocxPreview = lazy(() => import("./docx-preview-frame"));

type PreviewBody<T> =
  | { status: "loading" }
  | { status: "ready"; value: T }
  | { status: "failed"; reason: PreviewFailure };

/** The file at this address, read once per address; a later address starts over. */
function usePreviewBody<T>(url: string, read: (url: string) => Promise<T>): PreviewBody<T> {
  const [settled, setSettled] = useState<{ url: string; body: PreviewBody<T> } | null>(null);
  useEffect(() => {
    let live = true;
    read(url).then(
      (value) => {
        if (live) setSettled({ url, body: { status: "ready", value } });
      },
      (error: unknown) => {
        if (live) setSettled({ url, body: { status: "failed", reason: previewRequestFailure(error) } });
      },
    );
    return () => {
      live = false;
    };
  }, [url, read]);
  return settled?.url === url ? settled.body : { status: "loading" };
}

function PreviewLoader() {
  const { t } = useI18n();
  return (
    <p className="fs-file-preview-status">
      <DsLoader variant="orbit-inline" label={t("react.jobs.file_preview_loading", "Loading preview…")} />
    </p>
  );
}

/** Shown while a previewer's code loads: after the shared delay, as every loader. */
function PreviewCodeLoading() {
  return useLoaderDelay(true) ? <PreviewLoader /> : null;
}

function PreviewAlert({ reason }: Readonly<{ reason: PreviewFailure }>) {
  const { t } = useI18n();
  return (
    <p className="fs-file-preview-status fs-ink-danger" role="alert">
      {previewFailureMessage(reason, t)}
    </p>
  );
}

/** A previewer that cannot load or throws while drawing leaves the download, not a blank page. */
class PreviewBoundary extends Component<Readonly<{ children: ReactNode }>, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  render() {
    return this.state.failed ? <PreviewAlert reason="failed" /> : this.props.children;
  }
}

/** TXT, Markdown and JSON as characters: a Markdown or HTML file is never rendered. */
function TextPreview({ name, url }: Readonly<{ name: string; url: string }>) {
  const body = usePreviewBody(url, fetchText);
  const loaderShown = useLoaderDelay(body.status === "loading");
  return (
    <div data-testid="job-file-text-preview">
      {loaderShown ? <PreviewLoader /> : null}
      {body.status === "failed" ? <PreviewAlert reason={body.reason} /> : null}
      {body.status === "ready" && !loaderShown ? (
        <pre aria-label={name} className="fs-file-preview-text">
          {body.value}
        </pre>
      ) : null}
    </div>
  );
}

function DocumentPreview({ kind, name, url }: Readonly<{ kind: DocumentPreviewKind; name: string; url: string }>) {
  const body = usePreviewBody(url, fetchArrayBuffer);
  const loaderShown = useLoaderDelay(body.status === "loading");
  if (body.status === "failed") return <PreviewAlert reason={body.reason} />;
  if (body.status === "loading" || loaderShown) return loaderShown ? <PreviewLoader /> : null;
  return (
    <PreviewBoundary>
      <Suspense fallback={<PreviewCodeLoading />}>
        {kind === "pdf" ? <PdfPreview data={body.value} name={name} /> : null}
        {kind === "spreadsheet" ? <SpreadsheetPreview data={body.value} name={name} /> : null}
        {kind === "docx" ? <DocxPreview data={body.value} name={name} /> : null}
      </Suspense>
    </PreviewBoundary>
  );
}

/** One file previewed in the page; the caller keeps its download next to it. */
export function FilePreview({ kind, name, url }: Readonly<{ kind: FilePreviewKind; name: string; url: string }>) {
  return (
    <div className="fs-file-preview" data-kind={kind} data-testid="file-preview">
      {kind === "image" ? <img alt={name} className="fs-file-preview-image" src={url} /> : null}
      {kind === "text" ? <TextPreview key={url} name={name} url={url} /> : null}
      {isDocumentPreviewKind(kind) ? <DocumentPreview key={url} kind={kind} name={name} url={url} /> : null}
    </div>
  );
}
