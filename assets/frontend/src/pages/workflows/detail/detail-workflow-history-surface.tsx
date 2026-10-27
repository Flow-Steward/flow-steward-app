import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useQuery } from "@tanstack/react-query";

import { fetchJson } from "@/lib/http/client";
import { RegistryEntityChangelogPanel } from "@/pages/registry/shared/entity-changelog-panel";
import { historyRowsFromPayload } from "@/pages/registry/shared/detail-helpers";

/**
 * The workflow's saved revisions in the settings drawer. Restore writes the
 * selected revision as a new one, guarded by the revision on screen, and is
 * offered only when the canvas has no unapplied changes to lose.
 */
export function WorkflowHistorySurface({ ctx }: Readonly<{ ctx: Record<string, any> }>) {
  const { t } = useI18n();
  const { changeSet, drawerMode, drawerTab, workflowId } = ctx;
  const open = drawerMode === "workflow-settings" && drawerTab === "history";
  const serverRevision = typeof ctx.serverRevision === "number" ? ctx.serverRevision : undefined;
  const historyQuery = useQuery({
    // Every applied or saved revision adds an entry: the list follows the revision on screen.
    queryKey: ["workflows", "history", workflowId, serverRevision],
    queryFn: async () =>
      historyRowsFromPayload(
        await fetchJson<unknown>(`/api/workflows/${encodeURIComponent(workflowId)}/history`),
      ),
    enabled: open && Boolean(workflowId),
    retry: false,
  });
  if (!open) {
    return null;
  }
  return (
    <div className="mt-3" id="workflow-history-surface">
      <RegistryEntityChangelogPanel
        entityId={workflowId}
        entityType="workflow"
        expectedRevision={serverRevision}
        loading={historyQuery.isLoading}
        onRestored={async () => {
          // A restored revision is a new local base: the previous Undo history ends here.
          changeSet?.acceptServerVersion?.("");
          ctx.forgetEditorSelection?.();
          await historyQuery.refetch();
          await ctx.refetchWorkflowSurfaces();
        }}
        restoreDisabledReason={changeSet?.hasChanges
          ? t("react.workflows.history_restore_blocked_by_changes", "Apply or discard the unapplied canvas changes before restoring a revision.")
          : undefined}
        restoreNote={t(
          "react.workflows.history_restore_note",
          "Restore saves this revision's steps, groups, For each containers and workflow settings as a new revision; the next runs use it. Whether the workflow is on, its webhooks, provider triggers, schedules, reactions, hook bindings, canvas notes and input secrets stay as they are now.",
        )}
        rows={historyQuery.data || []}
      />
    </div>
  );
}
