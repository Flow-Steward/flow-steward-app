import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, RotateCcw, Trash2 } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router";

import {
  DsAlert,
  DsBadge,
  DsCard,
  DsPageHeader,
  DsTable,
  DsTabs,
  PageContainer,
  type DsTableColumn,
} from "@/components/shared/design-system";
import { LifecyclePlanModal, type LifecyclePlan } from "@/components/shared/lifecycle-plan-modal";
import { WorkspaceDeletePlanModal } from "@/components/shared/workspace-delete-plan-modal";
import {
  fetchDataConnections,
  fetchDatasets,
  type DataConnectionRecord,
  type DatasetRecord,
} from "@/pages/registry/datasets/api";
import { executeLifecyclePlan, fetchLifecyclePlan } from "@/pages/trash/api";
import {
  executeWorkspaceDeletePlan,
  fetchWorkspace,
  fetchWorkspaceDeletePlan,
  fetchWorkspaceProjects,
  updateWorkspace,
  type WorkspaceDeletePlan,
  type WorkspacePayload,
} from "@/pages/registry/workspaces/api";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

type FormSubmitEvent = {
  currentTarget: HTMLFormElement;
  preventDefault: () => void;
};

type WorkspaceTabKey = "projects" | "data-connections" | "datasets";

const TAB_KEYS = new Set<string>(["projects", "data-connections", "datasets"]);

type ProjectRow = Record<string, unknown>;

function localizedPROJECT_COLUMNS(t: I18nContextValue["t"] = fallbackContext.t): DsTableColumn<ProjectRow>[] {
  return [
  {
    key: "name",
    header: t("settings.mcp.fields.name", "Name"),
    cell: (row) => {
      const projectId = firstStringFromUnknownValue([row.id, ""]);
      const name = firstStringFromUnknownValue([row.name, "Project"]);
      return <Link to={`/projects/${projectId}`}>{name}</Link>;
    },
  },
];
}

function localizedCONNECTION_COLUMNS(t: I18nContextValue["t"] = fallbackContext.t): DsTableColumn<DataConnectionRecord>[] {
  return [
  { key: "display_name", header: t("settings.mcp.fields.name", "Name"), cell: (row) => row.display_name },
  {
    key: "source_kind",
    header: t("react.registry.source_kind", "Source kind"),
    cell: (row) => <DsBadge>{row.source_kind || "connector"}</DsBadge>,
  },
  {
    key: "environment",
    header: t("react.registry.environment_variant_9e471951", "Environment"),
    cell: (row) => <DsBadge>{row.environment || "dev"}</DsBadge>,
  },
  {
    key: "status",
    header: t("extensions.runtime.status", "Status"),
    cell: (row) => (
      <DsBadge tone={row.enabled ? "green" : "neutral"}>
        {row.enabled ? t("settings.extensions.enabled", "enabled") : t("settings.notifications.status.disabled", "disabled")}
      </DsBadge>
    ),
  },
  {
    key: "linked_dataset_count",
    header: t("react.registry.datasets", "Datasets"),
    align: "right",
    cell: (row) => row.linked_dataset_count,
  },
];
}

function localizedDATASET_COLUMNS(t: I18nContextValue["t"] = fallbackContext.t): DsTableColumn<DatasetRecord>[] {
  return [
  {
    key: "display_name",
    header: t("settings.mcp.fields.name", "Name"),
    cell: (row) => <Link to={`/registry/datasets/${row.dataset_id}`}>{row.display_name}</Link>,
  },
  {
    key: "query_language",
    header: t("react.registry.query_language", "Query language"),
    cell: (row) => <DsBadge>{row.query_language || "sql"}</DsBadge>,
  },
  {
    key: "status",
    header: t("extensions.runtime.status", "Status"),
    cell: (row) => (
      <DsBadge tone={row.enabled ? "green" : "neutral"}>
        {row.enabled ? t("settings.extensions.enabled", "enabled") : t("settings.notifications.status.disabled", "disabled")}
      </DsBadge>
    ),
  },
];
}

export function RegistryWorkspacesDetailPage() {
  const { t } = useI18n();
  const { workspaceId = "" } = useParams();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") || "";
  const activeTab: WorkspaceTabKey = TAB_KEYS.has(requestedTab)
    ? (requestedTab as WorkspaceTabKey)
    : "projects";
  function goToTab(key: string) {
    const next = new URLSearchParams(searchParams);
    if (key === "projects") {
      next.delete("tab");
    } else {
      next.set("tab", key);
    }
    setSearchParams(next, { replace: true });
  }
  const [message, setMessage] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [deletePlan, setDeletePlan] = useState<WorkspaceDeletePlan | null>(null);
  const [modalError, setModalError] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [modalPlan, setModalPlan] = useState<LifecyclePlan | null>(null);

  const workspaceQuery = useQuery({
    queryKey: ["registry", "workspaces", workspaceId],
    queryFn: () => fetchWorkspace(workspaceId),
    enabled: Boolean(workspaceId),
  });
  const projectsQuery = useQuery({
    queryKey: ["registry", "workspaces", workspaceId, "projects"],
    queryFn: () => fetchWorkspaceProjects(workspaceId),
    enabled: Boolean(workspaceId),
  });
  const connectionsQuery = useQuery({
    queryKey: ["registry", "workspaces", workspaceId, "data-connections"],
    queryFn: () => fetchDataConnections({ workspaceId }),
    enabled: Boolean(workspaceId),
  });
  const datasetsQuery = useQuery({
    queryKey: ["registry", "workspaces", workspaceId, "datasets"],
    queryFn: () => fetchDatasets(),
    enabled: Boolean(workspaceId),
  });

  const workspace = workspaceQuery.data;

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["registry", "workspaces"] });
  };

  const saveMutation = useMutation({
    mutationFn: (payload: WorkspacePayload) => updateWorkspace(workspaceId, payload),
    onSuccess: async () => {
      setMessage(t("react.registry.workspace_saved", "Workspace saved."));
      await refresh();
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_save_workspace", "Failed to save workspace.")),
  });

  const projects = useMemo(() => projectsQuery.data || [], [projectsQuery.data]);
  const connections = useMemo(() => connectionsQuery.data || [], [connectionsQuery.data]);
  const workspaceDatasets = useMemo(() => {
    const all = datasetsQuery.data || [];
    return all.filter((dataset) => firstStringFromUnknownValue([dataset.workspace_id, ""]).trim() === workspaceId);
  }, [datasetsQuery.data, workspaceId]);

  function submit(event: FormSubmitEvent) {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    saveMutation.mutate({
      display_name: firstStringFromUnknownValue([formData.get("display_name"), ""]).trim(),
      description: firstStringFromUnknownValue([formData.get("description"), ""]).trim(),
    });
  }

  async function openLifecyclePlan(actionType: "restore" | "hard_delete") {
    setModalError("");
    const plan = await fetchLifecyclePlan("workspace", workspaceId, actionType);
    setModalPlan(plan as LifecyclePlan);
    setModalOpen(true);
  }

  async function openDeletePlan() {
    setDeleteBusy(true);
    setDeleteError("");
    try {
      const plan = await fetchWorkspaceDeletePlan(workspaceId);
      setDeletePlan(plan);
      setDeleteModalOpen(true);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : t("react.registry.failed_to_load_workspace_delete_plan", "Failed to load workspace delete plan."));
    } finally {
      setDeleteBusy(false);
    }
  }

  // `DsTabItem.label` is a string, so the count lives in the label rather than in
  // a nested pill — it is the only place the page says how much each view holds
  // before you switch to it.
  const tabs = [
    { key: "projects", label: t("react.registry.projects_value0", "Projects ({value0})", { value0: projects.length }) },
    { key: "data-connections", label: t("react.registry.data_connections_value0", "Data connections ({value0})", { value0: connections.length }) },
    { key: "datasets", label: t("react.registry.datasets_value0", "Datasets ({value0})", { value0: workspaceDatasets.length }) },
  ];

  const projectColumns = localizedPROJECT_COLUMNS(t);
  const connectionColumns = localizedCONNECTION_COLUMNS(t);
  const datasetColumns = localizedDATASET_COLUMNS(t);

  return (
    <PageContainer className="fs-page-sections" width="standard">
      <DsPageHeader
        action={
          <>
            {/* The back link belongs in the action slot, styled, the way every
                other registry detail page does it. It used to sit above the
                header with no class at all, rendering as bare text. It carries
                the icon because the controls beside it do. */}
            <Link className="fs-button" to="/registry/workspaces">
              <ArrowLeft aria-hidden="true" />
              {t("react.registry.back_to_workspaces", "Back to workspaces")}</Link>
            {workspace?.deleted_at ? (
              <>
                <button
                  className="fs-button"
                  onClick={() => ignoreAsyncResult(openLifecyclePlan("restore"))}
                  type="button"
                >
                  <RotateCcw aria-hidden="true" />
                  {t("trash.row_actions.restore", "Restore")}</button>
                <button
                  className="fs-button danger"
                  onClick={() => ignoreAsyncResult(openLifecyclePlan("hard_delete"))}
                  type="button"
                >
                  <Trash2 aria-hidden="true" />
                  {t("react.registry.hard_delete", "Hard delete")}</button>
              </>
            ) : (
              <button
                className="fs-button danger"
                onClick={() => ignoreAsyncResult(openDeletePlan())}
                type="button"
              >
                <Trash2 aria-hidden="true" />
                {t("react.registry.move_to_trash", "Move to trash")}</button>
            )}
          </>
        }
        description={workspace?.workspace_key || workspaceId}
        eyebrow={t("trash.category.registry", "Registry")}
        title={workspace?.display_name || t("project_mode.workspace", "Workspace")}
      />

      {deleteError ? (
        <DsAlert title={t("react.registry.workspace_delete_failed_variant_32163595", "Workspace delete failed")} tone="danger">{deleteError}</DsAlert>
      ) : null}

      <DsCard
        description={t("react.registry.edit_the_required_grouping_scope_for_projects", "Edit the required grouping scope for projects.")}
        title={t("react.registry.workspace_settings", "Workspace settings")}
      >
        <form
          aria-label={t("react.registry.workspace_settings_form", "Workspace settings form")}
          key={workspace?.id || workspaceId}
          onSubmit={submit}
        >
          <div className="fs-form-grid">
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="workspace-display-name">
                {t("react.registry.workspace_name", "Workspace name")}</label>
              <input
                className="fs-input"
                defaultValue={workspace?.display_name || ""}
                id="workspace-display-name"
                name="display_name"
                required
              />
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="workspace-key">
                {t("react.registry.workspace_key", "Workspace key")}</label>
              {/* Stays `readOnly`, never `disabled`: the test reads it with
                  `getByDisplayValue`, and disabling it would change both the
                  semantics and what the form submits. */}
              <input
                className="fs-input"
                defaultValue={workspace?.workspace_key || ""}
                id="workspace-key"
                readOnly
              />
              <span className="fs-field-helper">
                {t("react.registry.stable_key_changes_require_the_separate_preview_and_exact_confirmation_api", "Stable key. Changes require the separate preview and exact-confirmation API.")}</span>
            </div>
            <div className="fs-field full">
              <label className="fs-field-label" htmlFor="workspace-description">
                {t("registry.workflows.simulation.description", "Description")}</label>
              <textarea
                className="fs-textarea"
                defaultValue={workspace?.description || ""}
                id="workspace-description"
                name="description"
              />
            </div>
          </div>
          <div className="fs-footer-actions">
            <button className="fs-button primary" disabled={saveMutation.isPending} type="submit">
              {t("users.modal.save", "Save")}</button>
            {/* A successful save used to be a bare span: silent to assistive
                technology. `DsAlert` derives `role="status"` from its tone. */}
            {message ? <DsAlert title={message} tone="success" /> : null}
          </div>
        </form>
      </DsCard>

      {/* No `DsCard` around this: `.fs-tabs-panel` already owns its border,
          radius, surface and 24px padding, so wrapping it drew a second frame
          around the first. No other registry detail page wraps it. */}
      <section className="fs-settings-tabs">
        <DsTabs
          active={activeTab}
          ariaLabel={t("react.registry.workspace_contents", "Workspace contents")}
          idPrefix="workspace"
          items={tabs}
          onSelect={goToTab}
        />
        <div
          aria-labelledby={`workspace-tab-${activeTab}`}
          className="fs-tabs-panel"
          id={`workspace-panel-${activeTab}`}
          role="tabpanel"
        >
          <div className="fs-panel-stack">
            {activeTab === "projects" ? (
              <>
                <h3 className="fs-panel-title">{t("react.registry.projects_in_this_workspace", "Projects in this workspace")}</h3>
                <DsTable
                  ariaLabel={t("react.registry.projects_in_this_workspace", "Projects in this workspace")}
                  busy={projectsQuery.isLoading}
                  columns={projectColumns}
                  rowKey={(row) => firstStringFromUnknownValue([row.id, ""])}
                  rows={projects}
                  state={
                    projects.length ? null : (
                      <div className="fs-table-state">
                        <strong>{t("projects.empty.all_title", "No projects yet")}</strong>
                        <span>{t("react.registry.no_projects_are_assigned_to_this_workspace_yet", "No projects are assigned to this workspace yet.")}</span>
                      </div>
                    )
                  }
                />
              </>
            ) : null}

            {activeTab === "data-connections" ? (
              <>
                <h3 className="fs-panel-title">{t("react.registry.data_connections_in_this_workspace", "Data connections in this workspace")}</h3>
                <DsTable
                  ariaLabel={t("react.registry.data_connections_in_this_workspace", "Data connections in this workspace")}
                  busy={connectionsQuery.isLoading}
                  columns={connectionColumns}
                  rowKey={(row) => row.connection_id}
                  rows={connections}
                  state={
                    connections.length ? null : (
                      <div className="fs-table-state">
                        <strong>{t("react.registry.no_data_connections_yet", "No data connections yet")}</strong>
                        <span>{t("react.registry.no_data_connections_in_this_workspace_yet", "No data connections in this workspace yet.")}</span>
                      </div>
                    )
                  }
                />
              </>
            ) : null}

            {activeTab === "datasets" ? (
              <>
                <h3 className="fs-panel-title">{t("react.registry.datasets_in_this_workspace", "Datasets in this workspace")}</h3>
                <DsTable
                  ariaLabel={t("react.registry.datasets_in_this_workspace", "Datasets in this workspace")}
                  busy={datasetsQuery.isLoading}
                  columns={datasetColumns}
                  rowKey={(row) => row.dataset_id}
                  rows={workspaceDatasets}
                  state={
                    workspaceDatasets.length ? null : (
                      <div className="fs-table-state">
                        <strong>{t("react.registry.no_datasets_yet", "No datasets yet")}</strong>
                        <span>{t("react.registry.no_datasets_in_this_workspace_yet", "No datasets in this workspace yet.")}</span>
                      </div>
                    )
                  }
                />
              </>
            ) : null}
          </div>
        </div>
      </section>

      <WorkspaceDeletePlanModal
        busy={deleteBusy}
        error={deleteError}
        onClose={() => {
          setDeleteModalOpen(false);
          setDeletePlan(null);
          setDeleteError("");
        }}
        onConfirm={(projectActions, connectionActions) => {
          setDeleteBusy(true);
          setDeleteError("");
          ignoreAsyncResult(executeWorkspaceDeletePlan(
            workspaceId,
            projectActions,
            connectionActions,
          )
            .then(async () => {
              setMessage(t("react.registry.workspace_moved_to_trash", "Workspace moved to trash."));
              setDeleteModalOpen(false);
              setDeletePlan(null);
              await refresh();
            })
            .catch((error) => {
              setDeleteError(error instanceof Error ? error.message : t("react.registry.workspace_delete_failed", "Workspace delete failed."));
            })
            .finally(() => {
              setDeleteBusy(false);
            }));
        }}
        open={deleteModalOpen}
        plan={deletePlan}
      />

      <LifecyclePlanModal
        busy={deleteBusy}
        error={modalError}
        onClose={() => {
          setModalOpen(false);
          setModalPlan(null);
          setModalError("");
        }}
        onConfirm={(selectedNodeIds) => {
          if (!modalPlan) {
            return;
          }
          ignoreAsyncResult(executeLifecyclePlan("workspace", workspaceId, modalPlan.action_type as "restore" | "hard_delete", selectedNodeIds, modalPlan.plan_version)
            .then(async () => {
              setMessage(modalPlan.action_type === "restore" ? t("react.registry.workspace_restored", "Workspace restored.") : t("react.registry.workspace_deleted", "Workspace deleted."));
              setModalOpen(false);
              setModalPlan(null);
              await refresh();
            })
            .catch((error) => {
              setModalError(error instanceof Error ? error.message : t("react.registry.lifecycle_action_failed", "Lifecycle action failed."));
            }));
        }}
        open={modalOpen}
        plan={modalPlan}
      />
    </PageContainer>
  );
}
