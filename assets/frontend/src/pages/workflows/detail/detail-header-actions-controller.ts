/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback } from "react";
import { useI18n } from "@/lib/i18n/useI18n";
import { useQueryClient } from "@tanstack/react-query";

import { isHttpError } from "@/lib/http/errors";
import { saveJson } from "@/pages/workflows/detail/detail-helpers";
import { useWorkflowHeaderMutations } from "@/pages/workflows/shared/header-actions";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export function useWorkflowHeaderActionsController(ctx: Record<string, any>) {
  const { t } = useI18n();
  const {
    canvasGraphQuery,
    dagQuery,
    detailQuery,
    navigate,
    selectedProjectId,
    surface,
    validationQuery,
    workflowContractQuery,
    workflowId,
  } = ctx;

  const queryClient = useQueryClient();
  const refetchWorkflowSurfaces = useCallback(async () => {
    const refetches: Array<Promise<unknown>> = [
      detailQuery.refetch(),
      validationQuery.refetch(),
      dagQuery.refetch(),
      workflowContractQuery.refetch(),
      // A saved step can declare other inputs; its Test Run form reads them
      // from the step contract preview, which is cached per step.
      queryClient.invalidateQueries({
        queryKey: ["workflows", "step-contract-preview", workflowId],
      }),
    ];
    if (surface === "builder") {
      refetches.push(canvasGraphQuery.refetch());
    }
    const [detailResult] = await Promise.allSettled(refetches);
    if (detailResult.status !== "fulfilled") return null;
    const refreshed = detailResult.value as { data?: unknown; isError?: boolean } | undefined;
    return refreshed?.isError ? null : refreshed?.data ?? null;
  }, [
    canvasGraphQuery,
    dagQuery,
    detailQuery,
    queryClient,
    surface,
    validationQuery,
    workflowContractQuery,
    workflowId,
  ]);

  const handleCloneWorkflow = useCallback(async () => {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (!normalizedWorkflowId) {
      return;
    }
    const cloneWorkflowId = firstStringFromUnknownValue([globalThis.prompt(t("react.workflows.display_clone_workflow_as", "Clone workflow as"), `${normalizedWorkflowId}_copy`), ""]).trim();
    if (!cloneWorkflowId) {
      return;
    }
    try {
      const cloneResponse = await saveJson(
        `/api/workflows/${encodeURIComponent(normalizedWorkflowId)}/clone`,
        {
          method: "POST",
          body: JSON.stringify({
            ...(selectedProjectId ? { project_id: selectedProjectId } : {}),
            workflow_id: cloneWorkflowId,
          }),
        },
      );
      const resolvedWorkflowId = firstStringFromUnknownValue([cloneResponse.workflow_id, cloneWorkflowId]).trim();
      const projectQuery = selectedProjectId
        ? `?project_id=${encodeURIComponent(selectedProjectId)}`
        : "";
      ignoreAsyncResult(navigate(
        `/registry/workflows/${encodeURIComponent(resolvedWorkflowId)}/builder${projectQuery}`,
      ));
    } catch (error) {
      const message =
        error instanceof Error ? error.message : t("react.workflows.failed_to_clone_workflow", "Failed to clone workflow.");
      globalThis.alert(message);
    }
  }, [navigate, selectedProjectId, workflowId, t]);

  const handleDeleteWorkflow = useCallback(async () => {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (!normalizedWorkflowId) {
      return;
    }
    const confirmation = firstStringFromUnknownValue([globalThis.prompt(t("react.workflows.display_type_delete_workflow", "Type DELETE to remove this workflow."), ""), ""]).trim();
    if (confirmation.toUpperCase() !== "DELETE") {
      return;
    }
    try {
      try {
        await saveJson(
          `/api/workflows/${encodeURIComponent(normalizedWorkflowId)}`,
          {
            method: "DELETE",
          },
        );
      } catch (error) {
        if (!isHttpError(error) || error.status !== 404) {
          throw error;
        }
      }
      ignoreAsyncResult(navigate("/registry/workflows"));
    } catch (error) {
      const message =
        error instanceof Error ? error.message : t("react.workflows.display_failed_to_delete_workflow", "Failed to delete workflow.");
      globalThis.alert(message);
    }
  }, [navigate, workflowId, t]);

  const {
    toggleEnabled: toggleWorkflowEnabled,
    togglingEnabled: togglingWorkflowEnabled,
  } = useWorkflowHeaderMutations({
    workflowId,
    onChanged: refetchWorkflowSurfaces,
  });

  return {
    handleCloneWorkflow,
    handleDeleteWorkflow,
    refetchWorkflowSurfaces,
    toggleWorkflowEnabled,
    togglingWorkflowEnabled,
  };
}
