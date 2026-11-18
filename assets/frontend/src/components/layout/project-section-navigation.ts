import { LayoutList, type LucideIcon } from "lucide-react";

import type { NavEntry, NavSection } from "@/components/layout/app-shell-helpers";
import { fetchProjectsList, type ProjectListItem } from "@/pages/projects/list/api";
import type { ProjectUserSectionBrief } from "@/pages/projects/user-sections/types";

export function sectionPath(projectId: string, sectionId: string): string {
  return `/projects/${encodeURIComponent(projectId)}/sections/${encodeURIComponent(sectionId)}`;
}

function briefs(project: ProjectListItem): ProjectUserSectionBrief[] {
  const raw = project.user_sections;
  return (Array.isArray(raw) ? (raw as ProjectUserSectionBrief[]) : [])
    .filter((brief) => brief && typeof brief.id === "string" && typeof brief.title === "string")
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || a.title.localeCompare(b.title));
}

function entry(project: ProjectListItem, brief: ProjectUserSectionBrief, icon: LucideIcon): NavEntry {
  return {
    // Project id in the key: the same section id or title in two projects stays two items.
    key: `section:${project.id}:${brief.id}`,
    label: brief.title,
    to: sectionPath(project.id, brief.id),
    icon,
  };
}

/**
 * The operator's and viewer's menu (ADR 0075): with one project its sections sit
 * at the top level; with several, each project is a group of its sections.
 * Projects without visible sections add nothing and do not change the choice.
 */
export function buildProjectSectionNavigation(
  projects: readonly ProjectListItem[],
  { singleLabel, icon = LayoutList }: Readonly<{ singleLabel: string; icon?: LucideIcon }>,
): NavSection[] {
  const withSections = projects
    .map((project) => ({ project, sections: briefs(project) }))
    .filter((item) => item.sections.length > 0);
  if (withSections.length === 1) {
    const [{ project, sections }] = withSections;
    return [{ key: "sections", label: singleLabel, items: sections.map((brief) => entry(project, brief, icon)) }];
  }
  return withSections.map(({ project, sections }) => ({
    key: `project:${project.id}`,
    label: project.name || project.id,
    items: sections.map((brief) => entry(project, brief, icon)),
  }));
}

/** Every project the user may see, across pages, in one list for the menu. */
export async function fetchSectionNavigationProjects(): Promise<ProjectListItem[]> {
  const perPage = 200;
  const projects: ProjectListItem[] = [];
  for (let page = 1; page <= 50; page += 1) {
    const payload = await fetchProjectsList({ page, perPage, search: "", includeAllWorkspaces: true });
    projects.push(...(payload.projects ?? []));
    if (page >= (payload.total_pages || 1)) break;
  }
  return projects;
}

/** The first section a user lands on, or "" when there is none. */
export function firstSectionPath(sections: readonly NavSection[]): string {
  return sections[0]?.items[0]?.to ?? "";
}
