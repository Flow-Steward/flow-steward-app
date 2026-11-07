import { fetchJson, type FetchJsonOptions } from "@/lib/http/client";
import { fetchProjectsList, type ProjectListItem } from "@/pages/projects/list/api";
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

type WorkflowLinkedProjectsResponse = { workflow_id?: string; projects?: ProjectListItem[] };

/** The projects that link the workflow, in project-list order, in one read.
 *  The server answers it from each project's own bindings; a failed read
 *  rejects instead of becoming "no links". */
async function fetchWorkflowLinkedProjectRows(workflowId: string, options?: FetchJsonOptions): Promise<ProjectListItem[]> {
  const payload = await fetchJson<WorkflowLinkedProjectsResponse>(
    `/api/workflows/${encodeURIComponent(workflowId)}/linked-projects`,
    undefined,
    options,
  );
  return Array.isArray(payload?.projects) ? payload.projects.filter((project) => textOrEmpty(project?.id)) : [];
}

export async function findWorkflowLinkedProjectId(workflowId: string, options?: FetchJsonOptions): Promise<string> {
  const normalizedWorkflowId = textOrEmpty(workflowId);
  if (!normalizedWorkflowId) {
    return "";
  }
  const [first] = await fetchWorkflowLinkedProjectRows(normalizedWorkflowId, options);
  return first ? textOrEmpty(first.id) : "";
}

/** Complete authorized link enumeration; a failed read must not become no links. */
export async function fetchWorkflowLinkedProjects(workflowId: string, options?: FetchJsonOptions): Promise<ProjectListItem[]> {
  const normalizedWorkflowId = textOrEmpty(workflowId);
  if (!normalizedWorkflowId) return [];
  return fetchWorkflowLinkedProjectRows(normalizedWorkflowId, options);
}
