import { useI18n } from "@/lib/i18n/useI18n";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { DsAlert, DsFormSelect, DsSelect } from "@/components/shared/design-system";
import { fetchWorkflowLinkedProjects } from "@/pages/workflows/shared/project-context";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { useWorkflowHookProjectContext } from "./detail-hook-project-context";
import { WorkflowSchedules } from "./detail-workflow-schedules";

/**
 * R2-10: the project a workflow's hooks are edited for. Each project keeps its
 * own steps in a hook, so the builder of a workflow with hooks offers the
 * projects it is linked to (the choice sets `?project_id=`); with none linked it
 * says so and opens the workflow's Projects tab to link one.
 */
export function WorkflowHookProjectPicker({
  workflowId, projectId, onProjectChange, compact = false,
}: Readonly<{ workflowId: string; projectId: string; onProjectChange?: (id: string) => void; compact?: boolean }>) {
  const { t } = useI18n();
  const canAuthor = useIsAdmin();
  const { projectsQuery: projects, projects: choices, projectId: authorizedProjectId } = useWorkflowHookProjectContext({
    workflowId, projectHint: projectId, enabled: canAuthor,
  });
  const projectsHref = `/registry/workflows/${encodeURIComponent(workflowId)}/projects`;
  if (projects.isPending) return compact ? null : <p className="fs-empty-copy">{t("react.workflows.loading_linked_projects", "Loading linked projects…")}</p>;
  if (projects.isError) return <span className="text-xs fs-ink-attention" role="status">{t("react.workflows.linked_projects_could_not_be_loaded", "Linked projects could not be loaded")}</span>;
  if (!choices.length) {
    const reason = t("react.workflows.hooks_no_project", "Link this workflow to a project to connect its hooks.");
    // The builder header has no room for the sentence: there the button carries it as its hint.
    return <span className={`inline-flex items-center gap-2 text-xs fs-ink-secondary ${compact ? "shrink-0 whitespace-nowrap" : "flex-wrap"}`} data-testid="hook-project-picker">
      <span className={compact ? "fs-sr-only" : undefined}>{reason}</span>
      <Link className="fs-button sm secondary" title={compact ? reason : undefined} to={projectsHref}>{t("react.workflows.link_a_project", "Link a project")}</Link>
    </span>;
  }
  const selectedProject = choices.find((project) => project.id === authorizedProjectId);
  return <span className={`inline-flex items-center gap-2 text-xs ${compact ? "workflow-hook-project-picker-compact min-w-0" : "min-w-[12rem]"}`}
    data-testid="hook-project-picker" title={compact ? selectedProject?.name : undefined}>
    <DsSelect className={compact ? "workflow-hook-project-select" : ""} ariaLabel={t("react.workflows.hook_project", "Project for hooks")} value={selectedProject?.id || ""}
      onChange={(id) => { if (id) onProjectChange?.(id); }} placeholder={t("react.workflows.choose_a_linked_project", "Choose a linked project")}
      options={choices.map((project) => ({ value: project.id, label: project.name || project.id }))} />
  </span>;
}

export function WorkflowScheduleProjectContext({
  workflowId, projectHint, onProjectChange, onTurnOnScheduleChannel,
}: Readonly<{
  workflowId: string; projectHint: string; onProjectChange: (id: string) => void;
  /** Opens Smart setup at Trigger channels when the Schedule channel is off. */
  onTurnOnScheduleChannel?: () => void;
}>) {
  const { t } = useI18n();
  const projects = useQuery({
    queryKey: ["workflows", workflowId, "schedule-projects"],
    queryFn: () => fetchWorkflowLinkedProjects(workflowId),
    enabled: Boolean(workflowId), retry: false,
  });
  if (projects.isPending) return <p className="fs-empty-copy">{t("react.workflows.loading_linked_projects", "Loading linked projects…")}</p>;
  if (projects.isError) return <DsAlert title={t("react.workflows.linked_projects_could_not_be_loaded", "Linked projects could not be loaded")} tone="danger">{t("react.workflows.the_project_list_is_incomplete", "The project list is incomplete.")}{" "}{projects.error.message}</DsAlert>;
  const choices = projects.data || [];
  const validHint = choices.some((project) => project.id === projectHint);
  const projectId = validHint ? projectHint : !projectHint && choices.length === 1 ? choices[0].id : "";
  return <section aria-label={t("react.workflows.workflow_schedules", "Workflow schedules")} className="flex flex-col gap-3">
    {!choices.length ? <DsAlert title={t("react.workflows.link_a_project", "Link a project")}>{t("react.workflows.link_this_workflow_to_a_project_before_creating_a_schedule", "Link this workflow to a project before creating a schedule.")}{" "}<Link to={`/registry/workflows/${encodeURIComponent(workflowId)}/projects`}>{t("react.workflows.linked_projects", "Linked projects")}</Link></DsAlert> : <>
      <div className="fs-field">
        <span className="fs-field-label">{t("react.workflows.schedule_project", "Schedule project")}</span>
        <DsFormSelect ariaLabel={t("react.workflows.schedule_project", "Schedule project")} name="schedule_project_id" value={projectId}
          onChange={onProjectChange} placeholder={t("react.workflows.choose_a_linked_project", "Choose a linked project")}
          options={choices.map((project) => ({ value: project.id, label: project.name || project.id }))} />
      </div>
      {projectHint && !validHint ? <DsAlert title={t("react.workflows.project_context_unavailable", "Project context unavailable")}>{t("react.workflows.the_requested_project_is_not_linked_or_accessible_choose_a_linked_project_to_continue", "The requested project is not linked or accessible. Choose a linked project to continue.")}</DsAlert> : null}
      {projectId ? <WorkflowSchedules key={`${workflowId}:${projectId}`} projectId={projectId} workflowId={workflowId}
          projectName={choices.find((project) => project.id === projectId)?.name || projectId}
          onTurnOnScheduleChannel={onTurnOnScheduleChannel} /> : <p className="fs-empty-copy">{t("react.workflows.choose_a_linked_project_to_manage_its_schedules", "Choose a linked project to manage its schedules.")}</p>}
    </>}
  </section>;
}
