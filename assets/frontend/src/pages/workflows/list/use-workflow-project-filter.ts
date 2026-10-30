import { useContext, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AuthContext } from "@/lib/auth/context";
import { useI18n } from "@/lib/i18n/useI18n";
import {
  normalizeWorkspaceId,
  readStoredWorkspaceId,
  WORKSPACE_SELECTED_EVENT,
} from "@/lib/utils/workspace-selection";
import { fetchProjectsList, type ProjectListItem } from "@/pages/projects/list/api";

const EMPTY_PROJECTS: ProjectListItem[] = [];
const EMPTY_WORKFLOW_IDS = new Set<string>();

async function fetchFilterProjects(workspaceId: string, failureMessage: string): Promise<ProjectListItem[]> {
  const projects = new Map<string, ProjectListItem>();
  let totalPages = 1;
  for (let page = 1; page <= totalPages; page += 1) {
    const payload = await fetchProjectsList(
      { page, perPage: 200, search: "", workspaceId },
      { presentation: "background" },
    );
    if (!payload || !Array.isArray(payload.projects) || !Number.isInteger(payload.total_pages) || payload.total_pages < 1) {
      throw new Error(failureMessage);
    }
    totalPages = payload.total_pages;
    for (const project of payload.projects) {
      if (typeof project.id !== "string" || !project.id.trim()) continue;
      const id = project.id.trim();
      const workflowIds = Array.isArray(project.workflow_ids)
        ? project.workflow_ids.filter((workflowId): workflowId is string => typeof workflowId === "string" && Boolean(workflowId))
        : [];
      projects.set(id, { ...project, id, name: project.name || id, workflow_ids: workflowIds });
    }
  }
  return [...projects.values()];
}

/** Project membership narrows the complete global catalog, never a visible page. */
export function useWorkflowProjectFilter(projectId: string, enabled: boolean) {
  const { t } = useI18n();
  const accountId = useContext(AuthContext)?.session?.user?.account_id || "";
  const [workspaceId, setWorkspaceId] = useState(readStoredWorkspaceId);
  useEffect(() => {
    const onWorkspaceSelected = (event: Event) => {
      const detail = (event as CustomEvent<{ workspaceId?: string }>).detail;
      setWorkspaceId(normalizeWorkspaceId(detail?.workspaceId));
    };
    globalThis.addEventListener(WORKSPACE_SELECTED_EVENT, onWorkspaceSelected);
    return () => globalThis.removeEventListener(WORKSPACE_SELECTED_EVENT, onWorkspaceSelected);
  }, []);

  const projectsQuery = useQuery({
    queryKey: ["workflows", "project-filter", "projects", accountId, workspaceId],
    queryFn: () => fetchFilterProjects(workspaceId, t("projects.grid.load_failed", "Failed to load projects.")),
    enabled,
    retry: false,
    refetchOnMount: "always",
  });
  const projects = projectsQuery.isSuccess ? projectsQuery.data : EMPTY_PROJECTS;
  const selectedProject = enabled && projectsQuery.isSuccess
    ? projects.find((project) => project.id === projectId)
    : undefined;
  const unavailable = Boolean(enabled && projectId && projectsQuery.isSuccess && !selectedProject);
  const error = useMemo(() => enabled
    ? projectsQuery.error || (unavailable
      ? new Error(t("react.workflows.project_filter_unavailable", "This project is not available in the current workspace. Choose a project or All projects."))
      : null)
    : null, [enabled, projectsQuery.error, unavailable, t]);
  const workflowIds = useMemo(() => {
    if (!projectId) return null;
    if (!selectedProject || error) return EMPTY_WORKFLOW_IDS;
    return new Set(selectedProject.workflow_ids ?? []);
  }, [projectId, selectedProject, error]);
  // A workflow can be listed by several projects; each one is named in its row.
  const projectsByWorkflow = useMemo(() => {
    const byWorkflow = new Map<string, ProjectListItem[]>();
    for (const project of projects) {
      for (const workflowId of project.workflow_ids ?? []) {
        byWorkflow.set(workflowId, [...(byWorkflow.get(workflowId) ?? []), project]);
      }
    }
    return byWorkflow;
  }, [projects]);

  return {
    accountId,
    projects,
    projectsByWorkflow,
    selectedProject,
    workflowIds,
    error,
    unavailable,
    projectsPending: enabled && projectsQuery.isPending,
    pending: Boolean(enabled && projectId && !error && projectsQuery.isPending),
    fetching: Boolean(projectId && projectsQuery.isFetching),
    retry: () => projectsQuery.refetch(),
  };
}
