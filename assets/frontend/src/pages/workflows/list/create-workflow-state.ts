import { useI18n } from "@/lib/i18n/useI18n";
import { useState } from "react";

import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export const WORKFLOW_ID_PATTERN = /^[a-z][a-z0-9_]*$/;
/**
 * A Workflow ID from a display name: "Order Sync (EU)" reads "order_sync_eu".
 * Letters outside a-z are dropped, so a name in another script yields "".
 */
export function workflowIdFromName(name: string): string {
  const words = name.normalize("NFKD").toLowerCase().split(/[^a-z0-9]+/u).filter(Boolean);
  const slug = words.join("_").replace(/^[\d_]+/u, "");
  let end = Math.min(slug.length, 64);
  while (end > 0 && slug[end - 1] === "_") end -= 1;
  return slug.slice(0, end);
}

export const WORKFLOW_ID_RULE =
  "Workflow ID must start with a lowercase letter and use only lowercase letters, numbers, and underscores.";

/**
 * The start-from-scratch form: three fields and the one request that creates an
 * empty workflow. Shared by the plain Create workflow dialog and the "From
 * scratch" tab of the catalog dialog, so both create exactly the same way.
 */
export function useCreateWorkflowForm(projectId: string) {
  const { t } = useI18n();
  const [workflowId, setWorkflowId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [notice, setNotice] = useState("");
  const [idInvalid, setIdInvalid] = useState(false);
  const [nameInvalid, setNameInvalid] = useState(false);
  // The ID follows the display name until the user types an ID of their own.
  const [idEdited, setIdEdited] = useState(false);
  const [creating, setCreating] = useState(false);

  /** Validates and creates; resolves to the created id, or "" when nothing was created. */
  async function submit( ): Promise<string> {
    if (!displayName.trim()) {
      setNameInvalid(true);
      setNotice(t("react.workflows.display_name_is_required", "Display name is required."));
      return "";
    }
    const trimmedId = workflowId.trim();
    if (!trimmedId) {
      setIdInvalid(true);
      setNotice(t("react.workflows.workflow_id_is_required", "Workflow ID is required."));
      return "";
    }
    if (!WORKFLOW_ID_PATTERN.test(trimmedId)) {
      setIdInvalid(true);
      setNotice(t("react.workflows.display_workflow_id_rule", "Workflow ID must start with a lowercase letter and use only lowercase letters, numbers, and underscores."));
      return "";
    }
    setCreating(true);
    setIdInvalid(false);
    setNotice("");
    try {
      const response = await fetchJson<Record<string, unknown>>("/api/workflows", {
        method: "POST",
        body: JSON.stringify({
          workflow_id: trimmedId,
          display_name: displayName.trim(),
          description,
          ...(projectId ? { project_id: projectId } : {}),
        }),
      });
      return firstStringFromUnknownValue([response.workflow_id, trimmedId]).trim();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t("react.workflows.failed_to_create_workflow", "Failed to create workflow."));
      return "";
    } finally {
      setCreating(false);
    }
  }

  function reset() {
    setNotice("");
    setIdInvalid(false);
    setNameInvalid(false);
    setIdEdited(false);
    setWorkflowId("");
    setDisplayName("");
    setDescription("");
  }

  return {
    creating,
    description,
    displayName,
    idInvalid,
    nameInvalid,
    notice,
    reset,
    setDescription,
    setDisplayName: (value: string) => {
      setDisplayName(value);
      setNameInvalid(false);
      if (!idEdited) setWorkflowId(workflowIdFromName(value));
      if (notice) setNotice("");
    },
    setWorkflowId: (value: string) => {
      setWorkflowId(value);
      setIdEdited(true);
      setIdInvalid(false);
      if (notice) setNotice("");
    },
    submit,
    workflowId,
  };
}

export type CreateWorkflowForm = ReturnType<typeof useCreateWorkflowForm>;
