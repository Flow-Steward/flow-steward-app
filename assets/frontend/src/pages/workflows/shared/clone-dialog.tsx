import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useRef, useState } from "react";

import { DsAlert, DsDialog } from "@/components/shared/design-system";
import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

/**
 * Cloning used to run through `globalThis.prompt` for the name and
 * `globalThis.alert` for the failure — neither is on any design system. The
 * dialog owns the request so both callers get the same surface: the list
 * navigates to the builder afterwards, the summary header to the overview.
 */
export function WorkflowCloneDialog({
  onClose,
  onCloned,
  projectId,
  sourceWorkflowId,
}: Readonly<{
  onClose: () => void;
  onCloned: (workflowId: string) => void;
  projectId?: string;
  sourceWorkflowId: string | null;
}>) {
  const { t } = useI18n();
  const open = Boolean(sourceWorkflowId);
  const [targetWorkflowId, setTargetWorkflowId] = useState("");
  const [cloning, setCloning] = useState(false);
  const [error, setError] = useState("");
  const targetRef = useRef<HTMLInputElement | null>(null);

  // The suggestion follows the source, so reopening on another row does not
  // carry the previous row's name.
  useEffect(() => {
    if (sourceWorkflowId) {
      setTargetWorkflowId(`${sourceWorkflowId}_copy`);
      setError("");
    }
  }, [sourceWorkflowId]);

  async function submit( ) {
    const sourceId = firstStringFromUnknownValue([sourceWorkflowId, ""]).trim();
    const targetId = targetWorkflowId.trim();
    if (!sourceId || !targetId) {
      setError(t("react.workflows.workflow_id_is_required", "Workflow ID is required."));
      return;
    }
    setCloning(true);
    setError("");
    try {
      const cloneResponse = await fetchJson<Record<string, unknown>>(
        `/api/workflows/${encodeURIComponent(sourceId)}/clone`,
        {
          body: JSON.stringify({
            ...(projectId?.trim() ? { project_id: projectId.trim() } : {}),
            workflow_id: targetId,
          }),
          method: "POST",
        },
      );
      const resolvedWorkflowId = firstStringFromUnknownValue([
        cloneResponse.workflow_id,
        targetId,
      ]).trim();
      onCloned(resolvedWorkflowId);
    } catch (cloneError) {
      setError(
        cloneError instanceof Error ? cloneError.message : t("react.workflows.failed_to_clone_workflow", "Failed to clone workflow."),
      );
    } finally {
      setCloning(false);
    }
  }

  return (
    <DsDialog
      busy={cloning}
      closeLabel={t("react.workflows.close_clone_workflow_dialog", "Close clone workflow dialog")}
      description={t("react.workflows.the_copy_starts_as_a_draft_steps_groups_and_edges_are_duplicated_run_history_is_not", "The copy starts as a draft. Steps, groups and edges are duplicated; run history is not.")}
      eyebrow={t("trash.category.registry", "Registry")}
      footer={
        <>
          <button className="fs-button" disabled={cloning} onClick={onClose} type="button">
            {t("extensions.repeater.cancel", "Cancel")}</button>
          <button
            className="fs-button primary"
            disabled={cloning}
            onClick={() => ignoreAsyncResult(submit( ))}
            type="button"
          >
            {cloning ? t("react.workflows.cloning", "Cloning…") : t("react.workflows.clone_workflow", "Clone workflow")}
          </button>
        </>
      }
      initialFocusRef={targetRef}
      onClose={onClose}
      open={open}
      title={t("react.workflows.clone_workflow", "Clone workflow")}
      variant="form"
    >
      {error ? (
        <DsAlert title={t("react.workflows.clone_failed", "Clone failed")} tone="danger">
          {error}
        </DsAlert>
      ) : null}
      <div className="fs-field full">
        <label className="fs-field-label" htmlFor="clone_workflow_id">
          {t("react.workflows.new_workflow_id", "New workflow ID")}</label>
        <input
          aria-invalid={Boolean(error)}
          className="fs-input"
          id="clone_workflow_id"
          name="clone_workflow_id"
          onChange={(event) => {
            setTargetWorkflowId(event.target.value);
            if (error) {
              setError("");
            }
          }}
          ref={targetRef}
          type="text"
          value={targetWorkflowId}
        />
        <span className="fs-field-helper">
          {t("react.workflows.cloned_from", "Cloned from")}{" "}{firstStringFromUnknownValue([sourceWorkflowId, "—"])}.
        </span>
      </div>
    </DsDialog>
  );
}
