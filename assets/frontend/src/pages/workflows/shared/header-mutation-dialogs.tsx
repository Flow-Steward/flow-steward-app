import { useI18n } from "@/lib/i18n/useI18n";
import { useRef, useState } from "react";

import { DsAlert, DsDialog } from "@/components/shared/design-system";
import { WorkflowCloneDialog } from "@/pages/workflows/shared/clone-dialog";
import type { WorkflowHeaderMutations } from "@/pages/workflows/shared/header-actions";
import { ignoreAsyncResult } from "@/lib/utils/async";

const DELETE_CONFIRMATION = "DELETE";

/**
 * The surfaces for the summary header's mutations. Rendered directly under the
 * header, so a failure appears on the surface that failed rather than in a
 * browser alert. `DsDialog` portals, so the dialogs do not care where this sits.
 */
export function WorkflowHeaderMutationDialogs({
  controller,
  workflowId,
}: Readonly<{ controller: WorkflowHeaderMutations; workflowId: string }>) {
  const { t } = useI18n();
  const [confirmation, setConfirmation] = useState("");
  const confirmationRef = useRef<HTMLInputElement | null>(null);

  // A reopened dialog must ask again; carrying the previous answer would turn the
  // second delete into one click. Cleared on the way out rather than in an effect
  // on the way in — closing is the only path back to a closed dialog, and a
  // successful delete navigates away.
  function closeDelete() {
    setConfirmation("");
    controller.closeDelete();
  }

  const confirmed = confirmation.trim().toUpperCase() === DELETE_CONFIRMATION;

  return (
    <>
      {controller.error ? (
        <DsAlert title={t("react.workflows.workflow_action_failed", "Workflow action failed")} tone="danger">
          {controller.error}
        </DsAlert>
      ) : null}

      <WorkflowCloneDialog
        onClose={controller.closeClone}
        onCloned={controller.finishClone}
        projectId={controller.cloneProjectId}
        sourceWorkflowId={controller.cloneSource}
      />

      <DsDialog
        busy={controller.deleting}
        closeLabel={t("react.workflows.close_delete_workflow_dialog", "Close delete workflow dialog")}
        description={t("react.workflows.the_workflow_its_steps_groups_and_edges_are_removed_jobs_that_already_ran_keep_their_recorded_copy", "The workflow, its steps, groups and edges are removed. Jobs that already ran keep their recorded copy.")}
        eyebrow={t("trash.category.registry", "Registry")}
        footer={
          <>
            <button
              className="fs-button"
              disabled={controller.deleting}
              onClick={closeDelete}
              type="button"
            >
              {t("extensions.repeater.cancel", "Cancel")}</button>
            <button
              className="fs-button danger"
              disabled={controller.deleting || !confirmed}
              onClick={() => ignoreAsyncResult(controller.performDelete())}
              type="button"
            >
              {controller.deleting ? t("react.workflows.deleting_variant_43b5894c", "Deleting…") : t("react.workflows.delete_workflow_variant_b438c4a8", "Delete workflow")}
            </button>
          </>
        }
        initialFocusRef={confirmationRef}
        onClose={closeDelete}
        open={controller.deleteOpen}
        title={t("react.workflows.delete_workflow_variant_b438c4a8", "Delete workflow")}
        variant="confirm"
      >
        <div className="fs-field full">
          <label className="fs-field-label" htmlFor="delete_workflow_confirmation">
            {t("react.workflows.type_delete_to_remove_this_workflow", "Type DELETE to remove this workflow.")}</label>
          <input
            autoComplete="off"
            className="fs-input"
            id="delete_workflow_confirmation"
            name="delete_workflow_confirmation"
            onChange={(event) => setConfirmation(event.target.value)}
            ref={confirmationRef}
            type="text"
            value={confirmation}
          />
          <span className="fs-field-helper">{t("react.workflows.deleting", "Deleting")}{" "}{workflowId}.</span>
        </div>
      </DsDialog>
    </>
  );
}
