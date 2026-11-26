import { HttpError } from "@/lib/http/errors";
import type { ApiErrorPayload } from "@/lib/http/types";
import type { I18nContextValue } from "@/lib/i18n/context";

export function entityCreateErrorMessage(error: unknown, fallback: string, t: I18nContextValue["t"]): string {
  const message = error instanceof Error ? error.message : fallback;
  if (!(error instanceof HttpError) || error.status !== 409) return message;

  const payload = error.payload as ApiErrorPayload | null | undefined;
  const detail = payload?.detail;
  const conflict = detail && typeof detail === "object" ? detail as ApiErrorPayload : null;
  // A registry match is omitted when the ID belongs to a trashed or otherwise hidden entry.
  const mayBeInTrash = conflict
    ? conflict.code === "registry_id_conflict" && Array.isArray(conflict.matches) && !conflict.matches.length
    : /\balready (?:exists|taken|in use)\b/i.test(message);

  if (!mayBeInTrash) return message;
  const hint = t("react.shared.create_conflict_trash_hint", "If you don’t see it in the list, check whether it’s in Trash.");
  return /[.!?]$/.test(message) ? `${message} ${hint}` : `${message}. ${hint}`;
}
