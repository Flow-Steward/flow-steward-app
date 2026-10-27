import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/** UI capabilities come from the canonical revision projection, never action names. */
export function changelogRevisionPresentation(
  selected: Record<string, unknown> | null | undefined,
  previous: Record<string, unknown> | null | undefined, t: I18nContextValue["t"] = fallbackContext.t
) {
  const selectedContent = selected?.revision_kind === "content";
  const selectedLifecycle = selected?.revision_kind === "lifecycle";
  const canCompareContent = selectedContent && previous?.revision_kind === "content";
  // The server says when a content revision cannot be restored under the current policy.
  const restoreRefused = selected?.restorable === false;
  const restoreRefusal = restoreRefused
    ? (typeof selected?.restore_unavailable_reason === "string" && selected.restore_unavailable_reason)
      || t("react.registry.display_revision_not_restorable", "This revision cannot be restored.")
    : "";

  if (selectedLifecycle) {
    return {
      canRestore: false,
      canCompareContent: false,
      title: t("react.registry.lifecycle_event", "Lifecycle event"),
      explanation: t("react.registry.display_lifecycle_not_revision", "This audit event is not a content revision. Revision restore is not offered in this view."),
    };
  }
  if (!selectedContent) {
    return {
      canRestore: false,
      canCompareContent: false,
      title: t("react.registry.revision_details", "Revision details"),
      explanation: t("react.registry.display_revision_type_unavailable", "Revision type is unavailable. Content comparison and restore are unavailable."),
    };
  }
  if (previous && !canCompareContent) {
    const comparison = previous.revision_kind === "lifecycle"
      ? t("react.registry.display_previous_lifecycle_event", "The previous entry is a lifecycle event; this is not a content comparison.")
      : t("react.registry.display_previous_revision_unavailable", "The previous revision type is unavailable. Content comparison is unavailable.");
    return {
      canRestore: !restoreRefused,
      canCompareContent: false,
      title: t("react.registry.revision_details", "Revision details"),
      explanation: [comparison, restoreRefusal].filter(Boolean).join(" "),
    };
  }
  return {
    canRestore: !restoreRefused,
    canCompareContent,
    title: t("react.registry.revision_diff", "Revision diff"),
    explanation: restoreRefusal,
  };
}
