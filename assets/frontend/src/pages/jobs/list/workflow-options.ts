import type { DsOption } from "@/components/shared/design-system";
import type { WorkflowExportPolicy, WorkflowListItem } from "@/pages/workflows/shared/api";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

/** One workflow as the Workflows list knows it. */
export type WorkflowSource = {
  id: string;
  name: string;
  projectId: string;
  installId: string;
};

/** What the Workflow filter can say about where a workflow comes from. */
export type WorkflowOwnerNames = {
  /** Project id -> project name. */
  projects: ReadonlyMap<string, string>;
  /** Extension install id -> extension name. */
  extensions: ReadonlyMap<string, string>;
};

/** An extension workflow's id: `extension.<install id>.<local workflow id>`. */
const EXTENSION_WORKFLOW_ID = /^extension\.([^.]+)\.(.+)$/;

export function extensionWorkflowParts(workflowId: string): { installId: string; localId: string } | null {
  const match = EXTENSION_WORKFLOW_ID.exec(workflowId.trim());
  return match ? { installId: match[1], localId: match[2] } : null;
}

export function workflowSourceFromRow(row: WorkflowListItem): WorkflowSource | null {
  const id = firstStringFromUnknownValue([row.workflow_id, ""]).trim();
  if (!id) return null;
  const portability: Partial<WorkflowExportPolicy> = row.portability && typeof row.portability === "object" ? row.portability : {};
  return {
    id,
    name: firstStringFromUnknownValue([row.display_name, id]).trim() || id,
    projectId: firstStringFromUnknownValue([row.project_id, ""]).trim(),
    installId: firstStringFromUnknownValue([row.source_install_id, portability.source_install_id, extensionWorkflowParts(id)?.installId, ""]).trim(),
  };
}

/** Resolve only an exact identity; local ids can be shared by unrelated extensions. */
export function findWorkflowSource(workflowId: string, sources: ReadonlyMap<string, WorkflowSource>): WorkflowSource | null {
  return sources.get(workflowId) || null;
}

function ownerName(workflowId: string, source: WorkflowSource | null, owners: WorkflowOwnerNames): string {
  const installId = extensionWorkflowParts(workflowId)?.installId || source?.installId || "";
  const extensionName = (installId && owners.extensions.get(installId)) || (source?.installId && owners.extensions.get(source.installId)) || "";
  if (extensionName) return extensionName;
  return (source?.projectId && owners.projects.get(source.projectId)) || "";
}

/**
 * Workflow filter options, labelled with the names the Workflows list shows.
 * Several workflows can share a name, so a shared name carries its project or
 * extension as the description, and adds the workflow id when that still collides.
 */
export function workflowFilterOptions(
  candidates: ReadonlyMap<string, string>,
  sources: ReadonlyMap<string, WorkflowSource>,
  owners: WorkflowOwnerNames,
): DsOption[] {
  const rows = Array.from(candidates.entries()).map(([value, fallback]) => {
    const source = findWorkflowSource(value, sources);
    const owner = ownerName(value, source, owners);
    return {
      value,
      label: source?.name || fallback || value,
      context: owner,
    };
  });
  const count = (key: string, map: Map<string, number>) => map.set(key, (map.get(key) || 0) + 1);
  const labelCounts = new Map<string, number>();
  const contextCounts = new Map<string, number>();
  rows.forEach((row) => {
    count(row.label, labelCounts);
    count(`${row.label}\u0000${row.context}`, contextCounts);
  });
  return rows
    .map((row): DsOption => {
      if ((labelCounts.get(row.label) || 0) < 2) return { value: row.value, label: row.label };
      const contextIsUnique = row.context && (contextCounts.get(`${row.label}\u0000${row.context}`) || 0) < 2;
      return { value: row.value, label: row.label, description: contextIsUnique ? row.context : [row.context, row.value].filter(Boolean).join(" · ") };
    })
    .sort((left, right) => left.label.localeCompare(right.label) || (left.description || "").localeCompare(right.description || ""));
}
