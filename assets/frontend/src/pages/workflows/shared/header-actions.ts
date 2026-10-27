import { useI18n } from "@/lib/i18n/useI18n";
import { useCallback, useState } from "react";

import { useNavigate } from "react-router";

import { fetchJson } from "@/lib/http/client";
import { isHttpError } from "@/lib/http/errors";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

type WorkflowHeaderMutationsOptions = {
  onChanged?: () => unknown;
  workflowId: string;
};

/**
 * Clone, delete and toggle for the workflow summary header.
 *
 * The three flows used to run through `globalThis.prompt` and `globalThis.alert`,
 * which are on no design system: the name, the typed `DELETE` and every failure
 * were browser chrome. The hook now only holds the state; `WorkflowHeaderMutationDialogs`
 * renders it on the published surfaces. The typed `DELETE` stays — it is a safety
 * control, not styling.
 */
export type WorkflowHeaderMutations = {
  cloneProjectId: string;
  cloneSource: string | null;
  cloneWorkflow: (projectId?: string) => void;
  closeClone: () => void;
  finishClone: (workflowId: string) => void;
  deleteOpen: boolean;
  deleteWorkflow: () => void;
  closeDelete: () => void;
  performDelete: () => Promise<void>;
  deleting: boolean;
  error: string;
  dismissError: () => void;
  toggleEnabled: () => Promise<void>;
  togglingEnabled: boolean;
};

function workflowPath(workflowId: string) {
  return `/registry/workflows/${encodeURIComponent(workflowId)}`;
}

export function useWorkflowHeaderMutations({
  onChanged,
  workflowId,
}: WorkflowHeaderMutationsOptions): WorkflowHeaderMutations {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [togglingEnabled, setTogglingEnabled] = useState(false);
  const [cloneSource, setCloneSource] = useState<string | null>(null);
  const [cloneProjectId, setCloneProjectId] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");

  const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();

  const refresh = useCallback(async () => {
    await onChanged?.();
  }, [onChanged]);

  const cloneWorkflow = useCallback((projectId = "") => {
    if (!normalizedWorkflowId) {
      return;
    }
    setError("");
    setCloneProjectId(projectId.trim());
    setCloneSource(normalizedWorkflowId);
  }, [normalizedWorkflowId]);

  const closeClone = useCallback(() => {
    setCloneProjectId("");
    setCloneSource(null);
  }, []);

  // The dialog performs the request; this decides where the clone lands, which
  // differs by caller — the header stays on the summary, the list opens the builder.
  const finishClone = useCallback(
    (clonedWorkflowId: string) => {
      const resolvedWorkflowId = firstStringFromUnknownValue([clonedWorkflowId, ""]).trim();
      setCloneSource(null);
      if (resolvedWorkflowId) {
        const projectQuery = cloneProjectId
          ? `?project_id=${encodeURIComponent(cloneProjectId)}`
          : "";
        ignoreAsyncResult(navigate(`${workflowPath(resolvedWorkflowId)}${projectQuery}`));
      }
    },
    [cloneProjectId, navigate],
  );

  const deleteWorkflow = useCallback(() => {
    if (!normalizedWorkflowId) {
      return;
    }
    setError("");
    setDeleteOpen(true);
  }, [normalizedWorkflowId]);

  const closeDelete = useCallback(() => setDeleteOpen(false), []);

  const performDelete = useCallback(async () => {
    if (!normalizedWorkflowId) {
      return;
    }
    setDeleting(true);
    setError("");
    try {
      try {
        await fetchJson(`/api/workflows/${encodeURIComponent(normalizedWorkflowId)}`, {
          method: "DELETE",
        });
      } catch (deleteError) {
        // A workflow that is already gone is the outcome the user asked for.
        if (!isHttpError(deleteError) || deleteError.status !== 404) {
          throw deleteError;
        }
      }
      setDeleteOpen(false);
      ignoreAsyncResult(navigate("/registry/workflows"));
    } catch (deleteError) {
      setError(
        deleteError instanceof Error ? deleteError.message : t("react.workflows.failed_to_delete_workflow", "Failed to delete workflow."),
      );
    } finally {
      setDeleting(false);
    }
  }, [navigate, normalizedWorkflowId, t]);

  const toggleEnabled = useCallback(async () => {
    if (!normalizedWorkflowId) {
      return;
    }
    setTogglingEnabled(true);
    setError("");
    try {
      await fetchJson(`/api/workflows/${encodeURIComponent(normalizedWorkflowId)}/toggle`, {
        method: "PATCH",
      });
      await refresh();
    } catch (toggleError) {
      setError(
        toggleError instanceof Error
          ? toggleError.message
          : t("react.workflows.failed_to_update_workflow_status", "Failed to update workflow status."),
      );
    } finally {
      setTogglingEnabled(false);
    }
  }, [normalizedWorkflowId, refresh, t]);

  return {
    cloneProjectId,
    cloneSource,
    cloneWorkflow,
    closeClone,
    finishClone,
    deleteOpen,
    deleteWorkflow,
    closeDelete,
    performDelete,
    deleting,
    error,
    dismissError: useCallback(() => setError(""), []),
    toggleEnabled,
    togglingEnabled,
  };
}
