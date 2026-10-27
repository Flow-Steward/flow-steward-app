import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo, useRef, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw } from "lucide-react";
import { Link } from "react-router";

import {
  DsAlert,
  DsDialog,
  DsPageHeader,
  DsTable,
  PageContainer,
  RequiredLabel,
  RowActionMenu,
  Search,
  type DsTableColumn,
} from "@/components/shared/design-system";
import { LifecyclePlanModal, type LifecyclePlan } from "@/components/shared/lifecycle-plan-modal";
import { WorkspaceDeletePlanModal } from "@/components/shared/workspace-delete-plan-modal";
import { executeLifecyclePlan, fetchLifecyclePlan } from "@/pages/trash/api";
import {
  createWorkspace,
  executeWorkspaceDeletePlan,
  fetchWorkspaceDeletePlan,
  fetchWorkspaces,
  type WorkspaceDeletePlan,
  type WorkspacePayload,
  type WorkspaceRecord,
} from "@/pages/registry/workspaces/api";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { useHasCapability } from "@/lib/auth/capabilities";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import {
  notifyWorkspacesChanged,
  persistWorkspaceSelection,
  readStoredWorkspaceId,
} from "@/lib/utils/workspace-selection";

type FormSubmitEvent = {
  currentTarget: HTMLFormElement;
  preventDefault: () => void;
};

const CREATE_FORM_ID = "workspace-create-form";

type WorkspaceLifecycleAction = "restore" | "hard_delete";

type WorkspaceColumnActions = Readonly<{
  activeRowMenuId: string | null;
  canAdministerPlatform: boolean;
  openDeletePlan: (workspaceId: string) => Promise<void>;
  openLifecyclePlan: (
    workspaceId: string,
    actionType: WorkspaceLifecycleAction,
  ) => Promise<void>;
  setRowMenu: (workspaceId: string | null) => void;
  userTimezone: string;
}>;

function buildWorkspaceColumns(
  actions: WorkspaceColumnActions, t: I18nContextValue["t"] = fallbackContext.t
): DsTableColumn<WorkspaceRecord>[] {
  return [
    {
      key: "display_name",
      header: t("react.registry.workspace_name", "Workspace name"),
      cell: (row) =>
        actions.canAdministerPlatform ? (
          <Link to={`/registry/workspaces/${row.id}`}>{row.display_name}</Link>
        ) : (
          row.display_name
        ),
    },
    { key: "projects_count", header: t("shell.nav.projects", "Projects"), align: "right", cell: (row) => row.projects_count },
    { key: "jobs_count", header: t("react.registry.jobs", "Jobs"), align: "right", cell: (row) => row.jobs_count },
    {
      key: "updated_at",
      header: t("react.workflows.updated", "Updated"),
      align: "right",
      cell: (row) => formatUserDateTime(row.updated_at, actions.userTimezone),
    },
    {
      key: "actions",
      header: "",
      ariaLabel: t("extensions.table.actions", "Actions"),
      cell: (row) => {
        if (!actions.canAdministerPlatform) {
          return <span className="fs-ink-tertiary">{t("settings.access.service_accounts.form.level.read", "Read only")}</span>;
        }
        return (
          <RowActionMenu
            label={t("react.registry.actions_for_value0", "Actions for {value0}", { value0: row.display_name })}
            onOpenChange={(open) => actions.setRowMenu(open ? row.id : null)}
            open={actions.activeRowMenuId === row.id}
          >
            <div className="fs-row-menu-group">
              {row.deleted_at ? (
                <>
                  <button
                    onClick={() => {
                      actions.setRowMenu(null);
                      ignoreAsyncResult(actions.openLifecyclePlan(row.id, "restore"));
                    }}
                    role="menuitem"
                    type="button"
                  >{t("trash.row_actions.restore", "Restore")}</button>
                  <button
                    className="danger"
                    onClick={() => {
                      actions.setRowMenu(null);
                      ignoreAsyncResult(actions.openLifecyclePlan(row.id, "hard_delete"));
                    }}
                    role="menuitem"
                    type="button"
                  >{t("react.registry.hard_delete", "Hard delete")}</button>
                </>
              ) : (
                <button
                  className="danger"
                  onClick={() => {
                    actions.setRowMenu(null);
                    ignoreAsyncResult(actions.openDeletePlan(row.id));
                  }}
                  role="menuitem"
                  type="button"
                >{t("react.registry.move_to_trash", "Move to trash")}</button>
              )}
            </div>
          </RowActionMenu>
        );
      },
    },
  ];
}

/**
 * After a workspace is trashed, prefer the Default workspace as the next active
 * selection, then any remaining workspace. There is no "all workspaces" scope to
 * fall back to, so "" now means only that nothing is left to select.
 */
function pickFallbackWorkspaceId(rows: WorkspaceRecord[], excludeWorkspaceId: string): string {
  const candidates = rows.filter(
    (workspace) => workspace.id && workspace.id !== excludeWorkspaceId && !workspace.deleted_at,
  );
  const preferred = candidates.find((workspace) => {
    const key = workspace.workspace_key.trim().toLowerCase();
    const name = workspace.display_name.trim().toLowerCase();
    return key === "default" || name === "default workspace" || name === "default";
  });
  return preferred?.id || candidates[0]?.id || "";
}

type WorkspaceDraft = {
  display_name: string;
  description: string;
};

const EMPTY_DRAFT: WorkspaceDraft = {
  display_name: "",
  description: "",
};

function WorkspaceTableState({ loading, count, search }: Readonly<{
  loading: boolean;
  count: number;
  search: string;
}>) {
  const { t } = useI18n();
  if (loading) {
    return (
      <div className="fs-table-state">
        <strong>{t("react.registry.loading_workspaces", "Loading workspaces")}</strong>
        <span>{t("react.registry.the_registry_will_remain_in_this_table_layout", "The registry will remain in this table layout.")}</span>
      </div>
    );
  }
  if (count) return null;
  const searching = Boolean(search.trim());
  let message = t("react.registry.display_create_workspace_to_start", "Create a workspace to get started.");
  if (searching) message = t("react.registry.display_change_clear_search", "Change or clear the search.");
  return (
    <div className="fs-table-state">
      <strong>{searching ? t("react.registry.no_workspaces_match_this_search", "No workspaces match this search") : t("react.registry.no_workspaces_found", "No workspaces found")}</strong>
      <span>{message}</span>
    </div>
  );
}

export function RegistryWorkspacesPage() {
  const { t } = useI18n();
  const canAdministerPlatform = useHasCapability("administer_platform");
  const queryClient = useQueryClient();
  const userTimezone = useUserTimezone();
  const createNameRef = useRef<HTMLInputElement>(null);
  const [search, setSearch] = useState("");
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [draft, setDraft] = useState<WorkspaceDraft>(EMPTY_DRAFT);
  const [createError, setCreateError] = useState("");
  const [message, setMessage] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [deletePlan, setDeletePlan] = useState<WorkspaceDeletePlan | null>(null);
  const [deleteWorkspaceId, setDeleteWorkspaceId] = useState("");
  const [modalError, setModalError] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [modalPlan, setModalPlan] = useState<LifecyclePlan | null>(null);
  const [modalWorkspaceId, setModalWorkspaceId] = useState("");

  const query = useQuery({
    // Only the workspaces that exist: a trashed one lives in Trash, and is restored or
    // deleted there (the owner's decision). The endpoint returns active ones by default.
    queryKey: ["registry", "workspaces", search],
    queryFn: () => fetchWorkspaces({ search }),
  });

  const workspaces = useMemo(() => query.data || [], [query.data]);
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["registry", "workspaces"] });
  };

  const createMutation = useMutation({
    mutationFn: createWorkspace,
    onSuccess: async () => {
      setDraft(EMPTY_DRAFT);
      setCreateError("");
      setCreateOpen(false);
      setMessage(t("react.registry.workspace_created", "Workspace created."));
      notifyWorkspacesChanged();
      await refresh();
    },
    onError: (error) =>
      setCreateError(error instanceof Error ? error.message : t("react.registry.failed_to_create_workspace", "Failed to create workspace.")),
  });

  function submit(event: FormSubmitEvent) {
    event.preventDefault();
    const payload: WorkspacePayload = {
      workspace_key: "",
      display_name: draft.display_name.trim(),
      description: draft.description.trim(),
    };
    createMutation.mutate(payload);
  }

  function openCreate() {
    setDraft(EMPTY_DRAFT);
    setCreateError("");
    setMessage("");
    setCreateOpen(true);
  }

  async function openLifecyclePlan(workspaceId: string, actionType: WorkspaceLifecycleAction) {
    setModalError("");
    const plan = await fetchLifecyclePlan("workspace", workspaceId, actionType);
    setModalWorkspaceId(workspaceId);
    setModalPlan(plan as LifecyclePlan);
    setModalOpen(true);
  }

  async function openDeletePlan(workspaceId: string) {
    setDeleteBusy(true);
    setDeleteError("");
    try {
      const plan = await fetchWorkspaceDeletePlan(workspaceId);
      setDeleteWorkspaceId(workspaceId);
      setDeletePlan(plan);
      setDeleteModalOpen(true);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : t("react.registry.failed_to_load_workspace_delete_plan", "Failed to load workspace delete plan."));
    } finally {
      setDeleteBusy(false);
    }
  }

  const columns = buildWorkspaceColumns({
    activeRowMenuId: rowMenu,
    canAdministerPlatform,
    openDeletePlan,
    openLifecyclePlan,
    setRowMenu,
    userTimezone,
  }, t);

  return (
    <PageContainer width="wide">
      {/* `space-y-6` is what every other registry list roots in, through
          `catalog-surface.tsx`. The `fs-page` this replaced is a class no
          stylesheet defines, so the page had no vertical rhythm at all. */}
      <section className="fs-panel-stack">
      <DsPageHeader
        action={
          <>
            <button className="fs-button" onClick={() => ignoreAsyncResult(refresh())} type="button">
              <RefreshCw aria-hidden="true" />
              {t("trash.refresh", "Refresh")}</button>
            {canAdministerPlatform ? (
              <button className="fs-button primary" onClick={openCreate} type="button">
                <Plus aria-hidden="true" />
                {t("react.registry.new_workspace", "New workspace")}</button>
            ) : null}
          </>
        }
        description={t("react.registry.manage_the_required_parent_scope_for_projects", "Manage the required parent scope for projects.")}
        eyebrow={t("trash.category.registry", "Registry")}
        title={t("react.registry.workspaces_variant_1377264b", "Workspaces")}
      />

      {message ? <DsAlert title={message} tone="success" /> : null}
      {deleteError ? (
        <DsAlert title={t("react.registry.workspace_delete_failed_variant_32163595", "Workspace delete failed")} tone="danger">{deleteError}</DsAlert>
      ) : null}

      {/* The published table-card anatomy: toolbar and table are direct
          children of the card, as on every other list. The page header already
          names the page, so the card carries no title of its own. */}
      <article className="fs-card fs-table-card">
        <div className="fs-table-toolbar">
          {/* Announced, not printed. `search` is part of the query
              key, so this is the number of rows on screen, not the size of the
              registry. */}
          <span aria-live="polite" className="fs-management-result-announcement">
            {workspaces.length} {" "}{t("react.registry.workspaces", "workspaces")}</span>
          <label className="fs-search">
            <Search aria-hidden="true" />
            <input
              aria-label={t("react.registry.search_workspaces_variant_c0cd6bdf", "Search workspaces")}
              onChange={(event) => setSearch(event.target.value)}
              placeholder={t("react.registry.search_workspaces", "Search workspaces…")}
              type="search"
              value={search}
            />
          </label>
        </div>
        <DsTable
          ariaLabel={t("react.registry.workspace_registry", "Workspace registry")}
          busy={query.isLoading}
          columns={columns}
          rowKey={(row) => row.id}
          rows={workspaces}
          state={<WorkspaceTableState loading={query.isLoading} count={workspaces.length} search={search} />}
        />
      </article>

      <DsDialog
        description={t("react.registry.create_a_project_grouping_scope_inside_the_default_account", "Create a project grouping scope inside the default account.")}
        eyebrow={t("trash.category.registry", "Registry")}
        footer={
          <>
            <button className="fs-button" onClick={() => setCreateOpen(false)} type="button">
              {t("extensions.repeater.cancel", "Cancel")}</button>
            <button
              className="fs-button primary"
              disabled={createMutation.isPending}
              form={CREATE_FORM_ID}
              type="submit"
            >
              {t("projects.modal.create", "Create")}</button>
          </>
        }
        initialFocusRef={createNameRef}
        onClose={() => setCreateOpen(false)}
        open={createOpen && canAdministerPlatform}
        title={t("react.registry.new_workspace", "New workspace")}
        variant="form"
      >
        <form className="fs-dialog-form" id={CREATE_FORM_ID} onSubmit={submit}>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="workspace-display-name">
              <RequiredLabel>{t("react.registry.workspace_name", "Workspace name")}</RequiredLabel>
            </label>
            <input
              aria-describedby={createError ? "workspace-name-error" : undefined}
              aria-invalid={createError ? "true" : undefined}
              className="fs-input"
              id="workspace-display-name"
              onChange={(event) => {
                setCreateError("");
                setDraft((current) => ({ ...current, display_name: event.target.value }));
              }}
              ref={createNameRef}
              required
              value={draft.display_name}
            />
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="workspace-description">
              {t("registry.workflows.simulation.description", "Description")}</label>
            <textarea
              className="fs-textarea"
              id="workspace-description"
              onChange={(event) =>
                setDraft((current) => ({ ...current, description: event.target.value }))
              }
              value={draft.description}
            />
          </div>
          {/* `DsAlert` owns its role and takes no `id`, so the id the field's
              `aria-describedby` resolves to lives on a plain wrapper. */}
          {createError ? (
            <div id="workspace-name-error">
              <DsAlert title={createError} tone="danger" />
            </div>
          ) : null}
        </form>
      </DsDialog>

      <WorkspaceDeletePlanModal
        busy={deleteBusy}
        error={deleteError}
        onClose={() => {
          setDeleteModalOpen(false);
          setDeletePlan(null);
          setDeleteWorkspaceId("");
          setDeleteError("");
        }}
        onConfirm={(projectActions, connectionActions) => {
          if (!deleteWorkspaceId) {
            return;
          }
          setDeleteBusy(true);
          setDeleteError("");
          const deletedWorkspaceId = deleteWorkspaceId;
          ignoreAsyncResult(executeWorkspaceDeletePlan(
            deleteWorkspaceId,
            projectActions,
            connectionActions,
          )
            .then(async () => {
              setMessage(t("react.registry.workspace_moved_to_trash", "Workspace moved to trash."));
              setDeleteModalOpen(false);
              setDeletePlan(null);
              setDeleteWorkspaceId("");
              await refresh();
              // If the just-deleted workspace was the active selection, move the
              // whole app (shell selector + every mounted page that filters by
              // workspace) off it: Default workspace when available, else the
              // first one left. Resolve the fallback from the fresh, *unfiltered*
              // workspace list so an active search on this page cannot hide
              // Default and send the selection to a less obvious workspace.
              if (readStoredWorkspaceId() === deletedWorkspaceId) {
                let fallbackWorkspaces: WorkspaceRecord[] = workspaces;
                try {
                  fallbackWorkspaces = await fetchWorkspaces();
                } catch {
                  // Fall back to the page list if the unfiltered fetch fails.
                }
                persistWorkspaceSelection(
                  pickFallbackWorkspaceId(fallbackWorkspaces, deletedWorkspaceId),
                );
              }
              // Tell the app shell (and other surfaces with their own list) to drop
              // the deleted workspace without a full page refresh.
              notifyWorkspacesChanged();
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
          setModalWorkspaceId("");
          setModalError("");
        }}
        onConfirm={(selectedNodeIds) => {
          if (!modalPlan || !modalWorkspaceId) {
            return;
          }
          ignoreAsyncResult(executeLifecyclePlan("workspace", modalWorkspaceId, modalPlan.action_type as "restore" | "hard_delete", selectedNodeIds, modalPlan.plan_version)
            .then(async () => {
              setMessage(modalPlan.action_type === "restore" ? t("react.registry.workspace_restored", "Workspace restored.") : t("react.registry.workspace_deleted", "Workspace deleted."));
              setModalOpen(false);
              setModalPlan(null);
              setModalWorkspaceId("");
              await refresh();
            })
            .catch((error) => {
              setModalError(error instanceof Error ? error.message : t("react.registry.lifecycle_action_failed", "Lifecycle action failed."));
            }));
        }}
        open={modalOpen}
        plan={modalPlan}
      />
      </section>
    </PageContainer>
  );
}
