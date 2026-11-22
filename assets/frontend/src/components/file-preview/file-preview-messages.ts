/** Why a preview cannot be shown, in the reader's language. The download always stays. */
import { HttpError } from "@/lib/http/errors";
import type { useI18n } from "@/lib/i18n/useI18n";

type Translate = ReturnType<typeof useI18n>["t"];

export type PreviewFailure = "too_large" | "not_utf8" | "unsupported" | "password" | "unreadable" | "failed";

/** The reason a preview request was refused, from the server's status. */
export function previewRequestFailure(error: unknown): PreviewFailure {
  const status = error instanceof HttpError ? error.status : 0;
  if (status === 413) return "too_large";
  if (status === 422) return "not_utf8";
  if (status === 415) return "unsupported";
  return "failed";
}

export function previewFailureMessage(reason: PreviewFailure, t: Translate): string {
  switch (reason) {
    case "too_large":
      return t("react.jobs.file_preview_too_large", "This file is larger than the preview limit. Download the original to read it.");
    case "not_utf8":
      return t("react.jobs.file_preview_not_utf8", "This file is not UTF-8 text, so it cannot be shown here. Download it instead.");
    case "unsupported":
      return t("react.jobs.file_preview_unsupported", "This file type has no preview. Download it instead.");
    case "password":
      return t("react.jobs.file_preview_password", "This file is protected with a password. Download it to open it.");
    case "unreadable":
      return t("react.jobs.file_preview_unreadable", "This file could not be read for a preview. Download it to open it.");
    case "failed":
      return t("react.jobs.file_preview_failed", "The preview could not be loaded. Download the file instead.");
  }
}
