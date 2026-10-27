import { useQuery } from "@tanstack/react-query";
import { useI18n } from "@/lib/i18n/useI18n";
import { fetchWorkflowLinkedProjects } from "@/pages/workflows/shared/project-context";
import { fetchProjectScheduledJobs } from "@/pages/projects/scheduled-jobs/api";

/** Shares the Schedules drawer's authorized project selection and query cache. */
export function CanvasScheduleSummary({ workflowId, projectHint = "" }: Readonly<{ workflowId: string; projectHint?: string }>) {
  const { t } = useI18n();
  const projects = useQuery({ queryKey: ["workflows", workflowId, "schedule-projects"], queryFn: () => fetchWorkflowLinkedProjects(workflowId), enabled: Boolean(workflowId), retry: false });
  const choices = projects.data || [];
  const project = choices.find((choice) => choice.id === projectHint) || (!projectHint && choices.length === 1 ? choices[0] : undefined);
  const projectId = project?.id || "";
  const schedules = useQuery({ queryKey: ["projects", "scheduled-jobs", projectId], queryFn: () => fetchProjectScheduledJobs(projectId), enabled: Boolean(projectId), retry: false });
  if (projects.isError || schedules.isError) return <p>{t("react.workflows.schedules_could_not_be_loaded", "Schedules could not be loaded")}</p>;
  if (projects.isPending || (projectId && schedules.isPending)) return <p>{t("react.workflows.loading_schedules", "Loading schedules…")}</p>;
  if (!projectId) return <p>{t("react.workflows.choose_schedule_project_summary", "Choose a linked project in Schedules to view its schedule.")}</p>;
  const jobs = (schedules.data?.jobs || []).filter((job) => job.workflow_id === workflowId);
  return <div className="space-y-1">
    <p>{project?.name || projectId}</p>
    {jobs.length ? jobs.map((job, index) => <p className="break-words" key={job.id || index}>
      {typeof job.cron_expr === "string" ? job.cron_expr : ""}{" · "}
      {typeof job.schedule_timezone === "string" && job.schedule_timezone ? job.schedule_timezone : t("react.workflows.project_timezone", "Project timezone")}{" · "}
      {job.enabled ? t("settings.mcp.status.enabled", "Enabled") : t("react.workflows.paused", "Paused")}
    </p>) : <p>{t("react.workflows.no_project_schedules", "No schedules in this project")}</p>}
  </div>;
}
