/** Which generated files the run view can preview, and where from. */
import type { Entity } from "@/pages/jobs/detail/job-detail-core";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

const JOB_ARTIFACT_URL = /^\/api\/jobs\/[^/?#]+\/artifacts\/[^/?#]+$/;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const IMAGE_EXTENSIONS = /\.(png|jpe?g|gif|webp)$/i;

/** The inline preview of a job artifact, or "" when the file has none. */
export function filePreviewUrl(downloadUrl: string): string {
  return JOB_ARTIFACT_URL.test(downloadUrl) ? `${downloadUrl}/preview` : "";
}

export function filePreviewKind(file: Entity): "image" | "pdf" | "" {
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
  return "";
}
