import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";

import { Plus } from "lucide-react";

import { useNavigate, type NavigateFunction } from "react-router";

import {
  persistWorkspaceSelection,
  readStoredWorkspaceId,
  resolveWorkspaceSelection,
} from "@/lib/utils/workspace-selection";
import {
  AdvancedTableFilters,
  BlankCell,
  DsAlert,
  DsDialog,
  DsFormSelect,
  DsPageHeader,
  DsTable,
  Pagination,
  PageContainer,
  RowActionMenu,
  Search,
  type DsTableColumn,
  type DsTableSort,
} from "@/components/shared/design-system";
import { fetchJson } from "@/lib/http/client";
import {
  fetchProjectsList,
  type ProjectListItem,
} from "@/pages/projects/list/api";
import { fetchWorkspaces, type WorkspaceRecord } from "@/pages/registry/workspaces/api";
import { fetchWorkflows, type WorkflowListItem } from "@/pages/workflows/shared/api";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { useHasCapability } from "@/lib/auth/capabilities";

/**
 * Every field here is answered by the list endpoint, so a filter narrows the
 * whole inventory rather than the loaded page.
 */


function getProjectFilterFields(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  {
    key: "has_jobs",
    label: t("jobs.page_title", "Jobs"),
    options: [
      { value: "yes", label: t("react.projects.with_jobs", "With jobs") },
      { value: "no", label: t("react.projects.without_jobs", "Without jobs") },
    ],
  },
];
}

type ProjectColumnsOptions = {
  canMutateProjects: boolean;
  navigate: NavigateFunction;
  rowMenu: string | null;
  setDeleteError: Dispatch<SetStateAction<string>>;
  setDeleteTarget: Dispatch<SetStateAction<{ id: string; name: string } | null>>;
  setRowMenu: Dispatch<SetStateAction<string | null>>;
  workflowCatalog: WorkflowListItem[];
};

function buildProjectColumns({
  canMutateProjects,
  navigate,
  rowMenu,
  setDeleteError,
  setDeleteTarget,
  setRowMenu,
  workflowCatalog,
}: ProjectColumnsOptions, t: I18nContextValue["t"] = fallbackContext.t): ReadonlyArray<DsTableColumn<Record<string, unknown>>> {
  return [
    {
      key: "name",
      header: t("projects.grid.column.name", "Name"),
      sortable: true,
      truncate: "20rem",
      tooltip: (row) => firstStringFromUnknownValue([row.name, ""]) || undefined,
      cell: (row) => <strong>{firstStringFromUnknownValue([row.name, "—"])}</strong>,
    },
    {
      key: "operator_default_workflow_id",
      header: t("react.projects.operator_default", "Operator default"),
      width: 190,
      truncate: "12rem",
      tooltip: (row) => stringFromUnknownValue(row.operator_default_workflow_id) || undefined,
      cell: (row) => {
        const workflowId = stringFromUnknownValue(row.operator_default_workflow_id);
        if (!workflowId) {
          return <BlankCell />;
        }
        const workflow = workflowCatalog.find(
          (item) => firstStringFromUnknownValue([item.workflow_id, ""]) === workflowId,
        );
        return firstStringFromUnknownValue([workflow?.display_name, workflow?.name, workflowId]);
      },
    },
    {
      key: "job_count",
      header: t("jobs.page_title", "Jobs"),
      width: 80,
      align: "right",
      sortable: true,
      cell: (row) => stringFromUnknownValue(row.job_count),
    },
    {
      key: "actions",
      header: "",
      ariaLabel: t("monitoring.table.actions", "Actions"),
      cell: (row) => {
        const projectId = firstStringFromUnknownValue([row.id, ""]).trim();
        const projectName = firstStringFromUnknownValue([row.name, `#${projectId}`]);
        const canDelete = canMutateProjects;
        return (
          <RowActionMenu
            label={t("react.projects.actions_for_value1", "Actions for {value1}", { value1: String(projectName) })}
            onOpenChange={(open) => setRowMenu(open ? projectId : null)}
            open={rowMenu === projectId}
          >
            <div className="fs-row-menu-group">
              <button onClick={() => ignoreAsyncResult(navigate(`/projects/${projectId}`))} role="menuitem" type="button">{t("jobs.actions.view", "View")}</button>
              {/* One page shows and edits the project, so View is the only way in;
                  a role without mutate_projects sees the same page read-only. */}
            </div>
            {canDelete ? (
              <>
                <div className="fs-row-menu-separator" />
                <div className="fs-row-menu-group">
                  <button
                    className="danger"
                    onClick={() => {
                      setDeleteError("");
                      setDeleteTarget({ id: projectId, name: projectName });
                      setRowMenu(null);
                    }}
                    role="menuitem"
                    type="button"
                  >
                    {t("projects.grid.action.delete", "Delete")} </button>
                </div>
              </>
            ) : null}
          </RowActionMenu>
        );
      },
    },
  ];
}

export function ProjectsListPage() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const canMutateProjects = useHasCapability("mutate_projects");
  const canCreateProjects = useHasCapability("create_projects");
  const workspaceNameByIdRef = useRef<Map<string, string>>(new Map());
  const [loadError, setLoadError] = useState("");
  // Query state the platform-grid used to own internally and emit back through
  // a platform-query event.
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [total, setTotal] = useState(0);
  const [listLoading, setListLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(25);
  const [search, setSearch] = useState("");
  const [jobsFilter, setJobsFilter] = useState("");
  const [sort, setSort] = useState<DsTableSort | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createWorkspaceId, setCreateWorkspaceId] = useState("");
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState(readStoredWorkspaceId);
  const [workspaces, setWorkspaces] = useState<WorkspaceRecord[]>([]);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [workspaceError, setWorkspaceError] = useState("");
  const [createMessage, setCreateMessage] = useState("");
  const [workflowCatalog, setWorkflowCatalog] = useState<WorkflowListItem[]>([]);
  const loadProjectsPage = useCallback(async () => {
    const workspaceId = readStoredWorkspaceId() || selectedWorkspaceId;
    const workspaceNameById = workspaceNameByIdRef.current;
    setListLoading(true);
    try {
      const payload = await fetchProjectsList({
        page,
        perPage,
        search: search.trim(),
        workspaceId: workspaceId || "",
        hasJobs: jobsFilter,
        sort: sort?.key || "",
        direction: sort?.direction || "",
      });
      const nextRows = Array.isArray(payload.projects)
        ? payload.projects.map((project: ProjectListItem) => ({
            ...project,
            workspace_name: firstStringFromUnknownValue([project.workspace_name, project.workspace_display_name, project.workspace_key, workspaceNameById.get(firstStringFromUnknownValue([project.workspace_id, ""]).trim()), "—"]),
            operator_default_workflow_id: firstStringFromUnknownValue([project.operator_default_workflow_id, ""]),
            job_count: Number(project.job_count || 0),
          }))
        : [];
      setRows(nextRows);
      setTotal(Number(payload.total || nextRows.length));
      setLoadError("");
    } catch (error) {
      const message = error instanceof Error ? error.message : "request_failed";
      setRows([]);
      setTotal(0);
      setLoadError(t("react.projects.projects_load_error", "Failed to load projects: {message}", { message }));
    } finally {
      setListLoading(false);
    }
  }, [jobsFilter, page, perPage, search, selectedWorkspaceId, sort, t]);

  const projectColumns = useMemo(
    () => buildProjectColumns({
      canMutateProjects,
      navigate,
      rowMenu,
      setDeleteError,
      setDeleteTarget,
      setRowMenu,
      workflowCatalog,
    }, t),
    [canMutateProjects, navigate, rowMenu, workflowCatalog, t],
  );

  useEffect(() => {
    workspaceNameByIdRef.current = new Map(
      workspaces.map((workspace) => [workspace.id, workspace.display_name]),
    );
  }, [workspaces]);

  useEffect(() => {
    ignoreAsyncResult(loadProjectsPage());
  }, [loadProjectsPage]);

  async function confirmDeleteProject() {
    if (!deleteTarget) {
      return;
    }
    setDeleting(true);
    setDeleteError("");
    try {
      await fetchJson<unknown>(`/api/projects/${deleteTarget.id}`, { method: "DELETE" });
      setDeleteTarget(null);
      await loadProjectsPage();
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : "request_failed");
    } finally {
      setDeleting(false);
    }
  }

  useEffect(() => {
    const onWorkspaceSelected = (event: Event) => {
      const detail = (event as CustomEvent<{ workspaceId?: string }>).detail || {};
      const nextWorkspaceId = firstStringFromUnknownValue([detail.workspaceId, ""]).trim();
      setSelectedWorkspaceId(nextWorkspaceId);
      setCreateWorkspaceId(nextWorkspaceId);
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key !== "platform:selected-workspace-id") {
        return;
      }
      const nextWorkspaceId = firstStringFromUnknownValue([event.newValue, ""]).trim();
      setSelectedWorkspaceId(nextWorkspaceId);
      setCreateWorkspaceId(nextWorkspaceId);
    };
    globalThis.addEventListener("platform:workspace-selected", onWorkspaceSelected as EventListener);
    globalThis.addEventListener("storage", onStorage);
    return () => {
      globalThis.removeEventListener("platform:workspace-selected", onWorkspaceSelected as EventListener);
      globalThis.removeEventListener("storage", onStorage);
    };
  }, [loadProjectsPage]);

  useEffect(() => {
    if (!canCreateProjects) {
      setCreateOpen(false);
      setCreateName("");
      setCreateWorkspaceId("");
      setCreateMessage("");
      setWorkspaces([]);
      setWorkspaceError("");
      setWorkspaceLoading(false);
      return;
    }
    let cancelled = false;
    async function loadWorkspaces() {
      setWorkspaceLoading(true);
      setWorkspaceError("");
      try {
        const rows = await fetchWorkspaces();
        if (cancelled) return;
        setWorkspaces(rows);
        const resolvedWorkspaceId = resolveWorkspaceSelection(
          readStoredWorkspaceId() || selectedWorkspaceId,
          rows,
        );
        if (resolvedWorkspaceId !== selectedWorkspaceId) {
          persistWorkspaceSelection(resolvedWorkspaceId);
          setSelectedWorkspaceId(resolvedWorkspaceId);
        }
        if (!createWorkspaceId) {
          setCreateWorkspaceId(resolvedWorkspaceId || (rows.length === 1 ? rows[0].id : ""));
        }
      } catch {
        if (!cancelled) {
          setWorkspaces([]);
          setWorkspaceError(t("react.projects.unable_to_load_workspaces", "Unable to load workspaces."));
        }
      } finally {
        if (!cancelled) setWorkspaceLoading(false);
      }
    }
    ignoreAsyncResult(loadWorkspaces());
    return () => {
      cancelled = true;
    };
  }, [canCreateProjects, createWorkspaceId, selectedWorkspaceId, t]);

  // The table resolves the operator default to a workflow name, so the catalog
  // is needed for the list itself and not only while the dialog is open.
  useEffect(() => {
    let cancelled = false;
    fetchWorkflows({ enabledOnly: false, search: "" })
      .then((rows) => {
        if (!cancelled) {
          setWorkflowCatalog(Array.isArray(rows) ? rows : []);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setWorkflowCatalog([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function resetCreateProjectState() {
    setCreateName("");
  }

  async function handleCreateProject(event: React.SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canCreateProjects) return;
    setCreateMessage("");
    const normalizedName = createName.trim();
    const validationError = !normalizedName
      ? t("react.projects.project_name_is_required", "Project name is required.")
      : !createWorkspaceId
        ? t("react.projects.workspace_is_required", "Workspace is required.")
        : workspaceLoading || !workspaces.some((workspace) => workspace.id === createWorkspaceId)
          ? t("react.projects.workspace_is_not_available", "Workspace is not available.")
          : "";
    if (validationError) {
      setCreateMessage(validationError);
      return;
    }
    setCreating(true);
    try {
      // Explicit empty assignment prevents the server from auto-linking all workflows.
      const body = { name: normalizedName, workspace_id: createWorkspaceId, workflow_ids: [] };
      const payload = await fetchJson<Record<string, unknown>>("/api/projects", {
        method: "POST",
        body: JSON.stringify(body),
      });
      const createdProjectId = firstStringFromUnknownValue([payload.id, payload.project_id, ""]).trim();
      persistWorkspaceSelection(createWorkspaceId);
      setCreateMessage(t("react.projects.project_created", "Project created."));
      setCreateName("");
      resetCreateProjectState();
      setCreateOpen(false);
      await loadProjectsPage();
      if (createdProjectId) {
        setCreateMessage(t("react.projects.project_created", "Project created."));
        ignoreAsyncResult(navigate(`/projects/${createdProjectId}`));
      }
    } catch (error) {
      setCreateMessage(error instanceof Error ? error.message : t("react.projects.failed_to_create_project", "Failed to create project."));
    } finally {
      setCreating(false);
    }
  }

  let projectTableState: ReactNode = null;
  if (listLoading) {
    projectTableState = (
      <div className="fs-table-state">
        <strong>{t("react.projects.loading_projects", "Loading projects")}</strong>
        <span>{t("react.projects.the_inventory_will_remain_in_this_table_layout", "The inventory will remain in this table layout.")}</span>
      </div>
    );
  } else if (loadError) {
    projectTableState = (
      <div className="fs-table-state">
        <strong>{t("react.projects.projects_could_not_be_loaded", "Projects could not be loaded")}</strong>
        <span>{loadError}</span>
        <button
          className="fs-button"
          onClick={() => ignoreAsyncResult(loadProjectsPage())}
          type="button"
        >
          {t("job_security.retry", "Retry")} </button>
      </div>
    );
  } else if (!rows.length) {
    projectTableState = (
      <div className="fs-table-state">
        <strong>{search.trim() ? t("react.projects.no_projects_match_this_search", "No projects match this search") : t("react.projects.no_projects_found", "No projects found")}</strong>
        <span>{search.trim() ? t("react.projects.change_or_clear_the_search", "Change or clear the search.") : t("react.projects.create_a_project_to_get_started", "Create a project to get started.")}</span>
      </div>
    );
  }

  return (
    <PageContainer className="fs-page-sections" width="wide">
      <DsPageHeader
        action={
          canCreateProjects ? (
          <button
            className="fs-button primary"
            data-action="create-project"
            data-testid="create-project"
            onClick={() => setCreateOpen(true)}
            type="button"
          >
            <Plus aria-hidden="true" className="h-4 w-4" />
            {t("react.projects.new_project", "New project")} </button>
          ) : undefined
        }
        description={t("react.projects.review_active_projects_open_details_and_create_projects_inside_a_workspace", "Review active projects, open details, and create projects inside a workspace.")}
        eyebrow={t("monitoring.stat.projects.label", "Projects")}
        title={t("react.projects.projects_inventory", "Projects inventory")}
      />

      <article className="fs-card fs-table-card">
        <div className="fs-table-toolbar">
          <AdvancedTableFilters
            disabled={listLoading}
            fields={getProjectFilterFields(t)}
            onChange={(key, value) => {
              if (key === "has_jobs") {
                setJobsFilter(value);
              }
              setPage(1);
            }}
            onClear={() => {
              setJobsFilter("");
              setPage(1);
            }}
            values={{ has_jobs: jobsFilter }}
          />
          <label className="fs-search">
            <Search aria-hidden="true" />
            <input
              aria-label={t("react.projects.search_projects", "Search projects")}
              onChange={(event) => {
                setSearch(event.target.value);
                setPage(1);
              }}
              placeholder={t("react.projects.search_projects_2", "Search projects…")}
              type="search"
              value={search}
            />
          </label>
          <span aria-live="polite" className="fs-management-result-announcement">{t("monitoring.badge.projects", "{count} projects", { count: total })}</span>
        </div>
        <DsTable
          ariaLabel={t("react.projects.projects_inventory", "Projects inventory")}
          busy={listLoading}
          columns={projectColumns}
          onRowClick={(row) => {
            const projectId = firstStringFromUnknownValue([row.id, ""]).trim();
            if (projectId) {
              ignoreAsyncResult(navigate(`/projects/${projectId}`));
            }
          }}
          onSortChange={setSort}
          rowKey={(row) => firstStringFromUnknownValue([row.id, ""])}
          rows={rows}
          sort={sort}
          state={projectTableState}
        />
        <Pagination
          onPageChange={setPage}
          onPerPageChange={(nextPerPage) => {
            setPerPage(nextPerPage);
            setPage(1);
          }}
          page={page}
          perPage={perPage}
          perPageOptions={[25, 50, 100, 200]}
          total={total}
        />
      </article>

      <DsDialog
        busy={deleting}
        closeLabel={t("react.projects.close_delete_project_confirmation", "Close delete project confirmation")}
        description={t("react.projects.the_project_moves_to_trash_jobs_already_recorded_against_it_are_kept", "The project moves to Trash. Jobs already recorded against it are kept.")}
        eyebrow={t("monitoring.stat.projects.label", "Projects")}
        footer={
          <>
            <button className="fs-button" disabled={deleting} onClick={() => setDeleteTarget(null)} type="button">{t("common.cancel", "Cancel")}</button>
            <button className="fs-button danger" disabled={deleting} onClick={() => ignoreAsyncResult(confirmDeleteProject())} type="button">
              {deleting ? t("react.projects.deleting", "Deleting…") : t("react.projects.delete_project", "Delete project")}
            </button>
          </>
        }
        onClose={() => setDeleteTarget(null)}
        open={Boolean(deleteTarget)}
        title={t("react.projects.delete_value1", "Delete {value1}?", { value1: String(deleteTarget?.name || "project") })}
        variant="confirm"
      >
        {/* Deleting sets deleted_at: the project goes to Trash and can be
            restored from there. Only purging from Trash is irreversible, and
            that is where that warning belongs. */}
        <DsAlert tone="warning" title={t("react.projects.you_can_restore_this_from_trash", "You can restore this from Trash.")} />
        {deleteError ? <DsAlert tone="danger" title={t("react.projects.the_project_was_not_deleted", "The project was not deleted")}>{deleteError}</DsAlert> : null}
      </DsDialog>

      <DsDialog
        busy={creating}
        closeLabel={t("react.projects.close_create_project_dialog", "Close create project dialog")}
        description={t("react.projects.name_the_project_and_choose_its_workspace_configure_workflows_and_other_settings_inside_the_project", "Name the project and choose its workspace. Configure workflows and other settings inside the project.")}
        eyebrow={t("monitoring.stat.projects.label", "Projects")}
        footer={
          <>
                <button
                  className="fs-button"
                  onClick={() => {
                    setCreateOpen(false);
                    setCreateMessage("");
                    resetCreateProjectState();
                  }}
                  type="button"
                >
                  {t("common.cancel", "Cancel")} </button>
                <button
                  className="fs-button primary"
                  disabled={!canCreateProjects || creating || workspaceLoading || Boolean(workspaceError) || workspaces.length === 0}
                  form="create-project-form"
                  type="submit"
                >
                  {creating ? t("react.projects.creating", "Creating...") : t("projects.modal.create", "Create")}
                </button>
          </>
        }
        onClose={() => {
          setCreateOpen(false);
          setCreateMessage("");
          resetCreateProjectState();
        }}
        open={createOpen && canCreateProjects}
        title={t("react.projects.create_project", "Create project")}
      >
        <form
          autoComplete="off"
          className="fs-dialog-form"
          data-testid="create-modal"
          id="create-project-form"
          onSubmit={(event) => ignoreAsyncResult(handleCreateProject(event))}
        >
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="project-create-name">
              {t("projects.grid.column.name", "Name")} <span aria-hidden="true" className="fs-required">*</span>
            </label>
            <input
              aria-label={t("projects.grid.column.name", "Name")}
              autoComplete="off"
              className="fs-input"
              id="project-create-name"
              name="name"
              onChange={(event) => setCreateName(event.target.value)}
              placeholder={t("react.projects.project_name", "Project name")}
              required
              value={createName}
            />
          </div>
          <div className="fs-field">
            <span className="fs-field-label">
              {t("project_mode.workspace", "Workspace")} <span aria-hidden="true" className="fs-required">*</span>
            </span>
            {workspaceLoading ? (
              <p className="fs-field-helper">{t("react.projects.loading_workspaces", "Loading workspaces...")}</p>
            ) : workspaceError ? (
              <DsAlert title={workspaceError} tone="danger" />
            ) : workspaces.length === 1 ? (
              <p className="fs-field-helper">{workspaces[0].display_name || t("react.projects.unnamed_workspace", "Unnamed workspace")}</p>
            ) : workspaces.length > 1 ? (
              <DsFormSelect
                ariaLabel={t("project_mode.workspace", "Workspace")}
                name="workspace_id"
                onChange={setCreateWorkspaceId}
                options={workspaces.map((workspace) => ({
                  value: workspace.id,
                  label: workspace.display_name || t("react.projects.unnamed_workspace", "Unnamed workspace"),
                }))}
                placeholder={t("react.projects.select_workspace", "Select workspace")}
                required
                value={createWorkspaceId}
              />
            ) : (
              <p className="fs-field-helper">{t("react.projects.no_workspaces_available", "No workspaces available.")}</p>
            )}
          </div>
        </form>
            {createMessage ? (
              <div className="fs-alert">
                {createMessage}
              </div>
            ) : null}
      </DsDialog>
    </PageContainer>
  );
}
