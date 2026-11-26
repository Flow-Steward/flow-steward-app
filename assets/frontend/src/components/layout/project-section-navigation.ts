import { LayoutList, type LucideIcon } from "lucide-react";

import type { NavEntry, NavSection } from "@/components/layout/app-shell-helpers";
import type { OperatorSectionItem } from "@/pages/operator/types";

/** A section's page: both ids are in the address, also when the menu is flat. */
export function sectionWorkPath(projectId: string, workflowId: string): string {
  return `/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}/work`;
}

function entry(section: OperatorSectionItem, icon: LucideIcon): NavEntry {
  return {
    // Project id in the key: the same workflow in two projects stays two items.
    key: `section:${section.project_id}:${section.workflow_id}`,
    label: section.title || section.workflow_name || section.workflow_id,
    to: sectionWorkPath(section.project_id, section.workflow_id),
    icon,
  };
}

/**
 * The operator's and viewer's menu (ADR 0075) from `GET /api/v2/projects/sections`:
 * with one project its sections sit at the top level; with several, each project
 * is a collapsible group of its sections, in the server's order. A project
 * without sections is not in the answer, so it adds nothing. A blocked section
 * stays: its page says why.
 */
export function buildProjectSectionNavigation(
  sections: readonly OperatorSectionItem[],
  { singleLabel, icon = LayoutList }: Readonly<{ singleLabel: string; icon?: LucideIcon }>,
): NavSection[] {
  const groups = new Map<string, { name: string; items: NavEntry[] }>();
  for (const section of sections) {
    if (!section?.project_id || !section.workflow_id) continue;
    const group = groups.get(section.project_id) ?? { name: section.project_name || section.project_id, items: [] };
    group.items.push(entry(section, icon));
    groups.set(section.project_id, group);
  }
  if (groups.size === 1) {
    const [group] = groups.values();
    return [{ key: "sections", label: singleLabel, items: group.items }];
  }
  return [...groups.entries()].map(([projectId, group]) => ({
    key: `project:${projectId}`,
    label: group.name,
    items: group.items,
    collapsible: true,
  }));
}

/** The first section a user lands on, or "" when there is none. */
export function firstSectionPath(sections: readonly NavSection[]): string {
  return sections[0]?.items[0]?.to ?? "";
}
