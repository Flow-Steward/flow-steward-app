/**
 * Which files the app previews in the browser, and where from (ADR 0077).
 *
 * A declared type decides; the file name speaks only when the type is missing, so
 * a file declared as something else is never previewed because of its extension.
 * DOC, PPT, ODT, RTF, TIFF and anything else keep only their download.
 */
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

/** A file record as lists and reviews carry it: type, name and size, all optional. */
export type PreviewedFile = Readonly<Record<string, unknown>>;

export type FilePreviewKind = "image" | "pdf" | "spreadsheet" | "docx" | "text";

/** The kinds a page previewer reads as bytes, each with its own lazily loaded library. */
export type DocumentPreviewKind = Extract<FilePreviewKind, "pdf" | "spreadsheet" | "docx">;

const JOB_ARTIFACT_URL = /^\/api\/jobs\/[^/?#]+\/artifacts\/[^/?#]+$/;

const KINDS_BY_TYPE: ReadonlyMap<string, FilePreviewKind> = new Map([
  ["image/png", "image"],
  ["image/jpeg", "image"],
  ["image/gif", "image"],
  ["image/webp", "image"],
  ["application/pdf", "pdf"],
  ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "spreadsheet"],
  ["application/vnd.ms-excel", "spreadsheet"],
  ["text/csv", "spreadsheet"],
  ["application/csv", "spreadsheet"],
  ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"],
  ["text/plain", "text"],
  ["text/markdown", "text"],
  ["application/json", "text"],
]);

const KINDS_BY_EXTENSION: ReadonlyMap<string, FilePreviewKind> = new Map([
  ["png", "image"],
  ["jpg", "image"],
  ["jpeg", "image"],
  ["gif", "image"],
  ["webp", "image"],
  ["pdf", "pdf"],
  ["xlsx", "spreadsheet"],
  ["xls", "spreadsheet"],
  ["csv", "spreadsheet"],
  ["docx", "docx"],
  ["txt", "text"],
  ["md", "text"],
  ["json", "text"],
]);

/** The inline preview of a job artifact, or "" when the file has none. */
export function filePreviewUrl(downloadUrl: string): string {
  return JOB_ARTIFACT_URL.test(downloadUrl) ? `${downloadUrl}/preview` : "";
}

/** How the file is previewed, or "" when it is offered as a download only. */
export function filePreviewKind(file: PreviewedFile): FilePreviewKind | "" {
  const contentType = firstStringFromUnknownValue([file.content_type, file.mime_type, ""])
    .split(";")[0]
    .trim()
    .toLowerCase();
  if (contentType) {
    return KINDS_BY_TYPE.get(contentType) ?? "";
  }
  const filename = firstStringFromUnknownValue([file.filename, file.name, ""]);
  const extension = /\.([a-z0-9]+)$/i.exec(filename)?.[1]?.toLowerCase() ?? "";
  return KINDS_BY_EXTENSION.get(extension) ?? "";
}

export function isDocumentPreviewKind(kind: FilePreviewKind | ""): kind is DocumentPreviewKind {
  return kind === "pdf" || kind === "spreadsheet" || kind === "docx";
}

/**
 * Over the preview limit: the projection says so, or the size is above the
 * session's `FS_FILE_PREVIEW_MAX_BYTES`. Such a file is offered as a download only.
 */
export function previewTooLarge(file: PreviewedFile, maxBytes: number | undefined): boolean {
  if (file.preview_too_large === true) return true;
  const size = Number(file.size_bytes);
  return maxBytes !== undefined && maxBytes > 0 && Number.isFinite(size) && size > maxBytes;
}
