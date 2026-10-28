import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";

type ChangelogEntrySummary = Record<string, unknown> | null | undefined;

export function changelogRevisionLabel(
  entityType: string,
  entry: ChangelogEntrySummary,
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  const revision = entry?.revision;
  const workflowContent = entityType.trim().toLowerCase() === "workflow"
    && entry?.revision_kind === "content"
    && typeof revision === "number"
    && Number.isFinite(revision);
  return workflowContent
    ? t("react.registry.revision_value0", "Revision {value0}", { value0: revision })
    : t("react.registry.saved_revision", "Saved revision");
}

export function changelogActionLabel(
  value: unknown,
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  const action = typeof value === "string" ? value.trim().toLowerCase() : "";
  const labels: Record<string, string> = {
    update: t("react.registry.action_updated", "Updated"),
    create: t("react.registry.action_created", "Created"),
    delete: t("react.registry.action_deleted", "Deleted"),
    restore: t("react.registry.action_restored", "Restored"),
  };
  return Object.prototype.hasOwnProperty.call(labels, action)
    ? labels[action]
    : typeof value === "string" ? value : "";
}

export function changelogComparisonLabel(
  entityType: string,
  previous: ChangelogEntrySummary,
  selected: ChangelogEntrySummary,
  previousEntryId: string,
  selectedEntryId: string,
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  const numberedWorkflowPair = entityType.trim().toLowerCase() === "workflow"
    && previous?.revision_kind === "content"
    && selected?.revision_kind === "content"
    && typeof previous.revision === "number"
    && Number.isFinite(previous.revision)
    && typeof selected.revision === "number"
    && Number.isFinite(selected.revision);
  const from = numberedWorkflowPair
    ? changelogRevisionLabel(entityType, previous, t)
    : `#${previousEntryId}`;
  const to = numberedWorkflowPair
    ? changelogRevisionLabel(entityType, selected, t)
    : `#${selectedEntryId}`;
  return t("react.registry.comparing_revision_values", "Comparing {previous} to {selected}", { previous: from, selected: to });
}

export function workflowChangePathLabel(
  entityType: string,
  value: unknown,
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  if (entityType.trim().toLowerCase() !== "workflow" || typeof value !== "string") {
    return typeof value === "string" ? value : "";
  }
  if (value === "display_name") {
    return t("react.registry.display_name", "Display name");
  }
  const outputLabel = /^settings\.callable\.expected_outputs\[(\d+)\]\.label$/u.exec(value);
  if (outputLabel) {
    return t("react.registry.workflow_output_label_path", "Outputs · Output {index} · Label", { index: Number(outputLabel[1]) + 1 });
  }
  return value;
}

export function changelogRevisionRestoreCopy(
  entityType: string,
  entityId: string,
  entry: ChangelogEntrySummary,
  entryId: string,
  t: I18nContextValue["t"] = fallbackContext.t,
): { title: string; body: string; success: string } {
  const revisionValue = entry?.revision;
  const workflowRevision = entityType.trim().toLowerCase() === "workflow"
    && entry?.revision_kind === "content"
    && typeof revisionValue === "number"
    && Number.isFinite(revisionValue);
  const revision = workflowRevision
    ? t("react.registry.revision_value0", "Revision {value0}", { value0: revisionValue })
    : "";
  const prefix = workflowRevision
    ? t("react.registry.restore_workflow_revision_as_new", "Restore {value0} as a new revision?", { value0: revision })
    : t("react.registry.restore_saved_revision_question", "Restore this saved revision?");
  const result = workflowRevision
    ? t("react.registry.restored_workflow_revision_as_new", "Restored {value0} as a new revision.", { value0: revision })
    : t("react.registry.restored_saved_revision", "Restored this saved revision.");
  return {
    title: t("react.registry.restore_saved_revision_title", "Restore saved revision"),
    body: `${prefix} ${t("react.registry.restore_target", "Target: {value0}/{value1}.", { value0: entityType, value1: entityId })} ${t("react.registry.entry", "Entry #")}${entryId}`,
    success: result,
  };
}

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
