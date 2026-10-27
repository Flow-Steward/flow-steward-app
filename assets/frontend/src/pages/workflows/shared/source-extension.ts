import type { WorkflowExportPolicy } from "@/pages/workflows/shared/api";

/**
 * The extension a workflow is provided by: its display name and version, or its
 * extension id. Never the install or release id, which are unique to one
 * installation and mean nothing to the person reading the badge.
 */
export function workflowSourceExtension(policy: Partial<WorkflowExportPolicy> | undefined): {
  extensionId: string;
  label: string;
} {
  const extensionId = String(policy?.source_extension_id || "").trim();
  const name = String(policy?.source_extension_name || "").trim() || extensionId;
  const version = String(policy?.source_extension_version || "").trim();
  return { extensionId, label: name && version ? `${name} ${version}` : name };
}
