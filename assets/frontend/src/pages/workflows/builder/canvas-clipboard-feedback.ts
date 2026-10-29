import type { I18nContextValue } from "@/lib/i18n/context";
import type { GraphEditNotice } from "./canvas-core";

/** Consume protected paste before any clipboard read or authoring dispatch. */
export function reportProtectedPaste(
  command: "copy" | "cut" | "paste" | "duplicate",
  structureLocked: boolean,
  t: I18nContextValue["t"],
  notice: (notice: GraphEditNotice) => void,
): boolean {
  if (command !== "paste" || !structureLocked) return false;
  notice({
    label: t("react.workflows.paste_read_only_reason", "This workflow's structure is read-only. Paste into a workflow you can edit."),
    tone: "error",
  });
  return true;
}
