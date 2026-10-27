import type { FetchJsonOptions } from "@/lib/http/client";
import { fetchProjectsList, type ProjectListItem } from "@/pages/projects/list/api";
import { fetchProjectWorkflowBindings } from "@/pages/projects/workflows/api";
import { isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

function textOrEmpty(value: unknown): string {
  if (typeof value === "string") {
    return value.trim();
  }
  return value == null ? "" : stringFromUnknownValue(value).trim();
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => textOrEmpty(item)).filter(isTruthy);
}

function objectOrEmpty(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function workflowIdsFromProjectBindingPayload(payload: unknown): string[] {
  const row = objectOrEmpty(payload);
  const explicit = stringList(row.workflow_ids);
  if (explicit.length) {
    return explicit;
  }
  const projectWorkflows = Array.isArray(row.project_workflows) ? row.project_workflows : [];
  return projectWorkflows
    .map((workflow) => {
      const value = objectOrEmpty(workflow);
      if (value.enabled === false || value.deleted_at) {
        return "";
      }
      return textOrEmpty(value.workflow_id);
    })
    .filter(isTruthy);
}

export async function fetchAllProjectsForWorkflowContext(options?: FetchJsonOptions): Promise<ProjectListItem[]> {
  const perPage = 200;
  const firstPage = await fetchProjectsList({
    page: 1,
    perPage,
    search: "",
    includeAllWorkspaces: true,
  }, options);
  const projects = [...(firstPage.projects || [])];
  const totalPages = Number(firstPage.total_pages || 1);
  for (let page = 2; page <= totalPages; page += 1) {
    const payload = await fetchProjectsList({
      page,
      perPage,
      search: "",
      includeAllWorkspaces: true,
    }, options);
    projects.push(...(payload.projects || []));
  }
  return projects;
}

export async function findWorkflowLinkedProjectId(workflowId: string, options?: FetchJsonOptions): Promise<string> {
  const normalizedWorkflowId = textOrEmpty(workflowId);
  if (!normalizedWorkflowId) {
    return "";
  }
  const projects = await fetchAllProjectsForWorkflowContext(options);
  const bindings = await Promise.all(
    projects.map(async (project) => {
      const projectId = textOrEmpty(project.id);
      const payload = projectId ? await fetchProjectWorkflowBindings(projectId, options) : {};
      return {
        projectId,
        workflowIds: workflowIdsFromProjectBindingPayload(payload),
        defaultWorkflowId: textOrEmpty(objectOrEmpty(payload).operator_default_workflow_id),
      };
    }),
  );
  const linked = bindings.find(
    (binding) =>
      binding.projectId &&
      (binding.defaultWorkflowId === normalizedWorkflowId ||
        binding.workflowIds.includes(normalizedWorkflowId)),
  );
  return linked?.projectId || "";
}

/** Complete authorized link enumeration; a failed read must not become no links. */
export async function fetchWorkflowLinkedProjects(workflowId: string): Promise<ProjectListItem[]> {
  const normalizedWorkflowId = textOrEmpty(workflowId);
  if (!normalizedWorkflowId) return [];
  const projects = await fetchAllProjectsForWorkflowContext();
  const linked = await Promise.all(projects.map(async (project) => {
    const projectId = textOrEmpty(project.id);
    if (!projectId) return null;
    const payload = await fetchProjectWorkflowBindings(projectId);
    return workflowIdsFromProjectBindingPayload(payload).includes(normalizedWorkflowId)
      ? project : null;
  }));
  return linked.filter((project): project is ProjectListItem => project !== null);
}
