/** Which generated files the run view can preview, and where from. */
import type { Entity } from "@/pages/jobs/detail/job-detail-core";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

const JOB_ARTIFACT_URL = /^\/api\/jobs\/[^/?#]+\/artifacts\/[^/?#]+$/;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const IMAGE_EXTENSIONS = /\.(png|jpe?g|gif|webp)$/i;
const TEXT_TYPES = new Set(["text/plain", "text/markdown", "application/json"]);
const TEXT_EXTENSIONS = /\.(txt|md|json)$/i;

/** The inline preview of a job artifact, or "" when the file has none. */
export function filePreviewUrl(downloadUrl: string): string {
  return JOB_ARTIFACT_URL.test(downloadUrl) ? `${downloadUrl}/preview` : "";
}

export type FilePreviewKind = "image" | "pdf" | "text" | "";

/**
 * A declared type decides; the file name speaks only when the type is missing, so
 * a file declared as something else is never previewed because of its extension.
 */
export function filePreviewKind(file: Entity): FilePreviewKind {
  const contentType = firstStringFromUnknownValue([file.content_type, file.mime_type, ""])
    .split(";")[0]
    .trim()
    .toLowerCase();
  const filename = firstStringFromUnknownValue([file.filename, file.name, ""]);
  if (IMAGE_TYPES.has(contentType) || (!contentType && IMAGE_EXTENSIONS.test(filename))) {
    return "image";
  }
  if (contentType === "application/pdf" || (!contentType && /\.pdf$/i.test(filename))) {
    return "pdf";
  }
  if (TEXT_TYPES.has(contentType) || (!contentType && TEXT_EXTENSIONS.test(filename))) {
    return "text";
  }
  return "";
}
