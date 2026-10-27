import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useState } from "react";

import { useQuery } from "@tanstack/react-query";

import { DsAlert, DsCard, DsSelect } from "@/components/shared/design-system";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { ProjectNotificationRoutesEditor } from "@/pages/projects/notifications";
import {
  normalizeWorkspaceId,
  readStoredWorkspaceId,
  WORKSPACE_SELECTED_EVENT,
} from "@/lib/utils/workspace-selection";

export function SettingsNotificationsPage() {
  const { t } = useI18n();
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [workspaceId, setWorkspaceId] = useState(readStoredWorkspaceId);

  useEffect(() => {
    const onWorkspaceSelected = (event: Event) => {
      const detail =
        (event as CustomEvent<{ workspaceId?: string }>).detail || {};
      const nextWorkspaceId = normalizeWorkspaceId(detail.workspaceId);
      setWorkspaceId(nextWorkspaceId);
      setSelectedProjectId("");
    };
    globalThis.addEventListener(
      WORKSPACE_SELECTED_EVENT,
      onWorkspaceSelected as EventListener,
    );
    return () =>
      globalThis.removeEventListener(
        WORKSPACE_SELECTED_EVENT,
        onWorkspaceSelected as EventListener,
      );
  }, []);

  const projectsQuery = useQuery({
    queryKey: ["settings", "notifications", "projects", workspaceId],
    queryFn: () =>
      fetchProjectsList({
        page: 1,
        perPage: 200,
        search: "",
        workspaceId,
        includeAllWorkspaces: !workspaceId,
      }),
    refetchInterval: 60_000,
  });

  const projects = Array.isArray(projectsQuery.data?.projects)
    ? projectsQuery.data.projects
    : [];
  // No implicit fallback to projects[0]. Notification routing is per project,
  // so opening the editor for a project the operator never chose invites them
  // to change the wrong project's routing while the select shows that project
  // as if it had been picked. The empty state below asks for a choice instead.
  const effectiveProjectId = selectedProjectId;

  const projectScopePlaceholder = projectsQuery.isLoading
    ? t("react.settings.loading_projects", "Loading projects...")
    : projectsQuery.isError
      ? t("react.settings.projects_unavailable", "Projects unavailable")
      : t("react.settings.select_a_project", "Select a project");
  const projectOptions = [
    { value: "", label: projectScopePlaceholder },
    ...projects.map((project) => ({
      value: String(project.id),
      label: (project.name || "").trim() || t("react.settings.unnamed_project", "Unnamed project"),
    })),
  ];

  return (
    <>
      <DsCard
        description={t("react.settings.manage_where_operational_updates_are_delivered_and_which_workflows_can_use_each_channel_channels_are_the_email", "Manage where operational updates are delivered and which workflows can use each channel. Channels are the email, chat, and webhook destinations used by workflow notifications; routing chooses which project workflows can send to each destination.")}
        title={t("react.settings.notification_routing", "Notification routing")}
      >
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="settings-notification-project-select">
            {t("extensions.detail.project_scope", "Project scope")} </label>
          <DsSelect
            ariaLabel={t("extensions.detail.project_scope", "Project scope")}
            disabled={projectsQuery.isLoading || projectsQuery.isError}
            id="settings-notification-project-select"
            onChange={setSelectedProjectId}
            options={projectOptions}
            placeholder={projectScopePlaceholder}
            value={effectiveProjectId}
          />
          <span className="fs-field-helper">{t("react.settings.notification_routing_is_configured_per_project", "Notification routing is configured per project.")}</span>
        </div>
        {projectsQuery.isError ? (
          <DsAlert title={t("react.settings.projects_could_not_be_loaded", "Projects could not be loaded")} tone="danger">
            {projectsQuery.error instanceof Error ? projectsQuery.error.message : t("projects.grid.load_failed", "Failed to load projects.")}
          </DsAlert>
        ) : null}
      </DsCard>

      {effectiveProjectId ? (
        <ProjectNotificationRoutesEditor projectId={effectiveProjectId} />
      ) : (
        <DsCard>
          <div className="fs-table-state">
            <strong>{t("react.settings.select_a_project_to_configure_notification_channels", "Select a project to configure notification channels.")}</strong>
          </div>
        </DsCard>
      )}
    </>
  );
}
