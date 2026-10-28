import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import {
  startTransition,
  type ReactNode,
  useCallback,
  useDeferredValue,
  useMemo,
  useRef,
  useState,
} from "react";

import { useQuery } from "@tanstack/react-query";
import { Plus, Upload } from "lucide-react";
import { useNavigate } from "react-router";

import { useCanReadWorkflowRegistry, useIsAdmin } from "@/lib/auth/use-is-admin";
import { HttpError } from "@/lib/http/errors";
import { queryKeys } from "@/lib/query/keys";
import { appHref } from "@/lib/routing/app-href";
import { useLatestSearchParams } from "@/lib/routing/use-latest-search-params";
import {
  AdvancedTableFilters,
  DsAlert,
  DsDialog,
  DsPageHeader,
  DsSelect,
  DsTable,
  DsTabs,
  PageContainer,
  RowActionMenu,
  Search,
} from "@/components/shared/design-system";
import { fetchWorkflowCatalogAvailability } from "@/pages/workflows/catalog/api";
import { WorkflowCatalogDialog } from "@/pages/workflows/catalog/catalog-dialog";
import { CreateWorkflowFields } from "@/pages/workflows/list/create-workflow-fields";
import { useCreateWorkflowForm } from "@/pages/workflows/list/create-workflow-state";
import { useWorkflowProjectFilter } from "@/pages/workflows/list/use-workflow-project-filter";
import { WorkflowCloneDialog } from "@/pages/workflows/shared/clone-dialog";
import {
  columnsFromGridConfig,
  visibleRowActions,
} from "@/components/shared/data-grid-adapter";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import { fetchWorkflows, type WorkflowListItem } from "@/pages/workflows/shared/api";
import { missingExtensionNotice, missingExtensionShortLine } from "@/pages/workflows/shared/missing-extension";
import { workflowSourceExtension } from "@/pages/workflows/shared/source-extension";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

function statusLabel(workflow: WorkflowListItem, unavailableReason: string): string {
  if (unavailableReason) return "unavailable";
  return workflow.enabled ? "enabled" : "disabled";
}

/** Why a listed workflow cannot be offered, or "" when it can. A missing
    extension is named the way a person reads it, in one short line; the
    workflow's own page says what to do about it. */
function workflowUnavailableReason(workflow: WorkflowListItem, t: I18nContextValue["t"] = fallbackContext.t): string {
  const status = typeof workflow.availability_status === "string" ? workflow.availability_status.trim() : "";
  if (!status || status === "available") return "";
  const missing = missingExtensionNotice([workflow, workflow.portability], false);
  if (missing) return missingExtensionShortLine(missing, t);
  const reason = typeof workflow.availability_reason_text === "string" ? workflow.availability_reason_text.trim() : "";
  return reason || t("react.workflows.this_workflow_is_currently_unavailable", "This workflow is currently unavailable.");
}

/* The catalog endpoint answers one status narrowing — `enabled_only` — so the
   field publishes exactly that option. A "Disabled" option would be client-side
   filtering the backend does not support. */
const ENABLED_FILTER_VALUE = "enabled";

function localizedWORKFLOW_FILTER_FIELDS(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  {
    key: "status",
    label: t("extensions.runtime.status", "Status"),
    options: [{ value: ENABLED_FILTER_VALUE, label: t("react.workflows.enabled_only", "Enabled only") }],
  },
];
}

function normalizeWorkflows(payload: unknown): WorkflowListItem[] {
  if (Array.isArray(payload)) {
    return payload as WorkflowListItem[];
  }
  if (payload && typeof payload === "object") {
    const candidate = payload as Record<string, unknown>;
    if (Array.isArray(candidate.items)) {
      return candidate.items as WorkflowListItem[];
    }
    if (Array.isArray(candidate.results)) {
      return candidate.results as WorkflowListItem[];
    }
  }
  return [];
}

function workflowIdFromRow(workflow: WorkflowListItem): string {
  return firstStringFromUnknownValue([workflow.workflow_id, workflow.entity_id, workflow.id, ""]).trim();
}

function portabilityFromRow(workflow: WorkflowListItem): Record<string, unknown> {
  const policy = workflow.portability;
  return policy && typeof policy === "object" ? policy : {};
}

function workflowSourceLabel(protectedWorkflow: boolean, extensionOwned: boolean, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (protectedWorkflow) {
    return t("react.workflows.protected", "protected");
  }
  return extensionOwned ? t("react.registry.extension", "extension") : t("react.workflows.manual", "manual");
}

/** What the Source badge says on hover: the extension by name, never an install id. */
function workflowSourceDetail(extensionOwned: boolean, extensionLabel: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (!extensionOwned) {
    return t("react.workflows.display_manual_workflow", "Manual workflow");
  }
  return extensionLabel
    ? t("react.workflows.provided_by_extension_value0", "Provided by extension: {value0}", { value0: extensionLabel })
    : t("react.workflows.display_provided_by_extension", "Provided by extension");
}

function buildWorkflowsGridConfig(canAuthor: boolean, projectId: string, t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  const projectSuffix = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  // Authoring row actions (open the builder, clone a workflow) are admin-only; read
  // actions (YAML export) remain available to operators/viewers.
  const authoringRowActions = canAuthor
    ? [
        {
          action_id: "builder",
          "data-row-action": "builder",
          label: t("react.workflows.builder_title", "Builder"),
          variant: "default",
          visible_if: { field: "structure_change_allowed", eq: true },
        },
        {
          // A protected extension workflow cannot open in the builder for
          // editing, but it is read there: the builder shows it read-only and
          // connects its hooks.
          action_id: "view",
          "data-row-action": "view",
          label: t("react.workflows.view_workflow", "View workflow"),
          variant: "default",
          visible_if: { field: "structure_change_allowed", eq: false },
        },
        {
          action_id: "clone",
          "data-row-action": "clone",
          label: t("react.workflows.clone", "Clone"),
          variant: "default",
          visible_if: { field: "clone_allowed", eq: true },
        },
      ]
    : [];
  return {
    columns: [
      {
        key: "display_name",
        label: t("react.workflows.workflow_variant_2e2d5c56", "Workflow"),
        type: "link",
        link_href: appHref(`/registry/workflows/{workflow_id}${projectSuffix}`),
        truncate: true,
        truncate_width: "clamp(16rem, 32vw, 30rem)",
      },
      {
        key: "enabled_label",
        label: t("extensions.runtime.status", "Status"),
        type: "badge",
        width: "120px",
        badge_map: { enabled: "green", disabled: "neutral", unavailable: "amber" },
      },
      {
        key: "source_label",
        label: t("react.workflows.source", "Source"),
        type: "badge",
        tooltip_key: "source_detail",
        width: "190px",
        badge_map: { manual: "neutral", extension: "amber", protected: "amber" },
      },
      {
        key: "description",
        label: t("registry.workflows.simulation.description", "Description"),
        type: "text",
        truncate: true,
        truncate_width: "clamp(16rem, 30vw, 28rem)",
      },
      { key: "updated_display", label: t("react.workflows.updated", "Updated"), type: "text", width: "190px" },
    ],
    row_actions: [
      ...authoringRowActions,
      {
        action_id: "export",
        "data-row-action": "export",
        label: t("react.workflows.export_yaml", "Export YAML"),
        variant: "default",
        visible_if: { field: "full_export_allowed", eq: true },
      },
      {
        action_id: "export-with-children",
        "data-row-action": "export-with-children",
        label: t("react.workflows.export_yaml_with_child_workflows", "Export YAML with child workflows"),
        variant: "default",
        visible_if: { field: "full_export_allowed", eq: true },
      },
    ],
    id_field: "workflow_id",
    pagination: { page_size_default: 25, page_size_options: [25, 50, 100, 200] },
    empty_state: {
      title: t("registry.workflows.empty_title", "No workflows found"),
      description: canAuthor
        ? t("react.workflows.try_adjusting_filters_or_create_a_workflow", "Try adjusting filters or create a workflow.")
        : t("react.workflows.try_adjusting_filters", "Try adjusting filters."),
    },
  };
}

function WorkflowCatalogActions({
  canManage,
  onCreate,
  onImport,
}: Readonly<{
  canManage: boolean;
  onCreate: () => void;
  onImport: () => void;
}>) {
  const { t } = useI18n();
  // An operator reads the template catalog in its tab; only an administrator
  // creates or imports.
  if (!canManage) return null;
  return (
    <>
      {/* Create workflow opens the start-from-scratch form; templates live in
          the Catalog tab; importing YAML keeps its own page. */}
      <button className="fs-button" onClick={onImport} type="button">
        <Upload aria-hidden="true" />
        {t("react.workflows.import_yaml", "Import YAML")}</button>
      <button className="fs-button primary" onClick={onCreate} type="button">
        <Plus aria-hidden="true" />
        {t("react.workflows.create_workflow", "Create workflow")}</button>
    </>
  );
}

function WorkflowCatalogAlerts({
  error,
  restricted,
}: Readonly<{ error: unknown; restricted: boolean }>) {
  const { t } = useI18n();
  if (restricted) {
    return (
      <DsAlert title={t("react.workflows.workflow_catalog_is_restricted", "Workflow catalog is restricted")} tone="warning">
        {t("react.workflows.the_workflow_registry_catalog_is_available_to_administrators_and_operators_your_role_has_read_only_project_acc", "The workflow registry catalog is available to administrators and operators. Your role has read-only project access instead — open a project to view its workflows.")}</DsAlert>
    );
  }
  if (!error) return null;
  return (
    <DsAlert title={t("react.workflows.failed_to_load_workflows_variant_b793b2dd", "Failed to load workflows")} tone="danger">
      {error instanceof Error ? error.message : t("react.workflows.failed_to_load_workflows", "Failed to load workflows.")}
    </DsAlert>
  );
}

function builderPathFor(workflowId: string, projectId: string) {
  const builderPath = `/registry/workflows/${encodeURIComponent(workflowId)}/builder`;
  return projectId ? `${builderPath}?project_id=${encodeURIComponent(projectId)}` : builderPath;
}

/** The catalog dialog's own URL keys: the open template and its browse state. */
function isTemplateParam(key: string) {
  return key === "tpl" || key.startsWith("tpl_");
}

function workflowCatalogRestricted(canReadRegistry: boolean, error: unknown): boolean {
  return !canReadRegistry || (error instanceof HttpError && error.status === 403);
}

export function WorkflowsListPage() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const userTimezone = useUserTimezone();
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const [searchParams, updateSearchParams] = useLatestSearchParams();
  const [createOpen, setCreateOpen] = useState(false);
  const [cloneSource, setCloneSource] = useState<string | null>(null);
  // A form dialog opens on its first field, not on the close button.
  const createIdRef = useRef<HTMLInputElement | null>(null);
  const pageProjectId = searchParams.get("project_id")?.trim() || "";
  const createForm = useCreateWorkflowForm(pageProjectId);
  // The catalog dialog's tab lives in the URL with its filters, so a link to it
  // reopens the same selection.
  const createParam = searchParams.get("create");
  const [catalogVisit, setCatalogVisit] = useState(0);
  // The page's tabs: the workflows themselves, and the template catalog. An
  // old link to the catalog dialog (?create=template) opens the Catalog tab.
  const activeTab = searchParams.get("tab") === "catalog" || createParam === "template" ? "catalog" : "workflows";
  const enabledOnly = searchParams.get("enabled") === "1";
  const search = firstStringFromUnknownValue([searchParams.get("search"), ""]);
  const deferredSearch = useDeferredValue(search);
  // Workflow authoring (create / import / builder / clone) is admin-only; the backend
  // returns 403 for operator/viewer. Gate the controls so non-admins never see an
  // authoring path they cannot use (UI parity with the backend RBAC).
  const canManageWorkflows = useIsAdmin();
  // The global workflow catalog (GET /api/workflows) is operator/admin only; viewers get
  // a 403. Skip the fetch entirely for viewers so the catalog page never emits a console
  // 403 — they see the restricted-catalog message instead.
  const canReadRegistry = useCanReadWorkflowRegistry();
  const projectFilter = useWorkflowProjectFilter(pageProjectId, canReadRegistry);
  const navigationProjectId = projectFilter.selectedProject?.id || "";
  // Whether this installation has a template catalog at all. Without one,
  // Create workflow opens the plain form and nobody is offered templates.
  const catalogAvailabilityQuery = useQuery({
    queryKey: ["workflows", "catalog", "availability"],
    queryFn: fetchWorkflowCatalogAvailability,
    enabled: canReadRegistry,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
  const catalogConfigured = catalogAvailabilityQuery.data?.configured === true;
  const catalogTabShown = canReadRegistry && catalogConfigured;

  const workflowsQuery = useQuery({
    queryKey: [...queryKeys.workflowsList({ search: deferredSearch, enabledOnly }), projectFilter.accountId],
    queryFn: () => fetchWorkflows({ enabledOnly, search: deferredSearch }, { presentation: "background" }),
    enabled: canReadRegistry,
    refetchInterval: canReadRegistry ? 20_000 : false,
    refetchOnMount: "always",
  });

  const workflows = useMemo(() => {
    if (!canReadRegistry) return [];
    const allWorkflows = normalizeWorkflows(workflowsQuery.data);
    return projectFilter.workflowIds === null ? allWorkflows
      : allWorkflows.filter((workflow) => projectFilter.workflowIds?.has(workflowIdFromRow(workflow)));
  }, [canReadRegistry, workflowsQuery.data, projectFilter.workflowIds]);
  const workflowRows = useMemo(
    () =>
      workflows.map((workflow, index) => {
        const workflowId = workflowIdFromRow(workflow) || `workflow-${index + 1}`;
        const portability = portabilityFromRow(workflow);
        const extensionOwned = Boolean(portability.extension_owned);
        const protectedWorkflow = Boolean(portability.protected);
        // An extension workflow that cannot be offered (a required extension is
        // missing) is listed as Unavailable, and says why where its description
        // would be: the same reason its page shows.
        const unavailableReason = workflowUnavailableReason(workflow, t);
        return {
          ...workflow,
          workflow_id: workflowId,
          display_name: workflow.display_name || workflowId,
          description: unavailableReason || firstStringFromUnknownValue([workflow.description, t("react.workflows.display_no_description", "No description")]),
          enabled_label: statusLabel(workflow, unavailableReason),
          source_label: workflowSourceLabel(protectedWorkflow, extensionOwned, t),
          source_detail: workflowSourceDetail(extensionOwned, workflowSourceExtension(portability).label, t),
          full_export_allowed: Boolean(portability.full_export_allowed),
          clone_allowed: Boolean(portability.clone_allowed),
          structure_change_allowed: Boolean(portability.structure_change_allowed),
          updated_display: formatUserDateTime(workflow.updated_at, userTimezone),
        };
      }),
    [userTimezone, workflows, t],
  );
  const refetchWorkflows = workflowsQuery.refetch;

  // `WorkflowCloneDialog` asks for the name and performs the request; this decides
  // where a clone lands, which differs by caller — the list opens the builder.
  const handleCloneWorkflow = useCallback(async (workflowId: string) => {
    const resolvedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (!resolvedWorkflowId) {
      return;
    }
    setCloneSource(null);
    await refetchWorkflows();
    ignoreAsyncResult(navigate(`/registry/workflows/${encodeURIComponent(resolvedWorkflowId)}/builder`));
  }, [navigate, refetchWorkflows]);

  function handleWorkflowAction(actionId: string, row: Record<string, unknown>) {
    const workflowId = firstStringFromUnknownValue([row.workflow_id, ""]).trim();
    if (!workflowId) {
      return;
    }
    if (actionId === "detail") {
      const path = `/registry/workflows/${encodeURIComponent(workflowId)}`;
      ignoreAsyncResult(navigate(navigationProjectId ? `${path}?project_id=${encodeURIComponent(navigationProjectId)}` : path));
      return;
    }
    if (actionId === "builder" || actionId === "view") {
      if (canManageWorkflows) {
        ignoreAsyncResult(navigate(builderPathFor(workflowId, navigationProjectId)));
      }
      return;
    }
    if (actionId === "clone") {
      if (canManageWorkflows) {
        setCloneSource(workflowId);
      }
      return;
    }
    if (actionId === "export") {
      globalThis.location.assign(`/api/workflows/${encodeURIComponent(workflowId)}/export.yaml`);
    }
    if (actionId === "export-with-children") {
      // One file with every workflow this one calls: what the template catalog takes.
      globalThis.location.assign(
        `/api/workflows/${encodeURIComponent(workflowId)}/export.yaml?include_children=true`,
      );
    }
  }

  const workflowsGridConfig = useMemo(
    () => buildWorkflowsGridConfig(canManageWorkflows, navigationProjectId, t),
    [canManageWorkflows, navigationProjectId, t],
  );

  const workflowColumns = useMemo(
    () =>
      columnsFromGridConfig(workflowsGridConfig, (row) => {
        const workflowId = firstStringFromUnknownValue([row.workflow_id, ""]).trim();
        return (
          <RowActionMenu
            label={t("react.workflows.actions_for_value0", "Actions for {value0}", { value0: workflowId })}
            onOpenChange={(open) => setRowMenu(open ? workflowId : null)}
            open={rowMenu === workflowId}
          >
            {visibleRowActions(workflowsGridConfig, row).map((action) => (
              <div className="fs-row-menu-group" key={action.action_id}>
                <button
                  className={action.variant === "danger" ? "danger" : undefined}
                  onClick={() => {
                    setRowMenu(null);
                    handleWorkflowAction(action.action_id, row);
                  }}
                  role="menuitem"
                  type="button"
                >
                  {action.label}
                </button>
              </div>
            ))}
          </RowActionMenu>
        );
      }),
    // handleWorkflowAction reads the latest props on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rowMenu, workflowsGridConfig, t],
  );


  function updateParams(next: { search?: string; enabledOnly?: boolean; projectId?: string }) {
    startTransition(() => {
      updateSearchParams((params) => {
        if (next.search !== undefined) {
          const value = next.search.trim();
          if (value) params.set("search", value);
          else params.delete("search");
        }
        if (next.enabledOnly !== undefined) {
          if (next.enabledOnly) params.set("enabled", "1");
          else params.delete("enabled");
        }
        if (next.projectId !== undefined) {
          if (next.projectId) params.set("project_id", next.projectId);
          else params.delete("project_id");
          params.delete("page");
        }
        return params;
      }, { push: true });
    });
  }

  async function handleCreateWorkflow() {
    if (!canManageWorkflows) {
      return;
    }
    const createdWorkflowId = await createForm.submit();
    if (!createdWorkflowId) {
      return;
    }
    await workflowsQuery.refetch();
    ignoreAsyncResult(navigate(builderPathFor(createdWorkflowId, pageProjectId)));
  }

  function selectTab(next: string) {
    // Leaving the catalog drops its own URL state (filters, the open template).
    // Choosing Catalog, including the tab already shown, closes an open
    // template and returns to the results with the filters kept.
    if (next === "catalog") setCatalogVisit((visit) => visit + 1);
    updateSearchParams((latest) => {
      const params = new URLSearchParams(
        Array.from(latest).filter(
          ([key]) => key !== "create" && key !== "tpl" && (next === "catalog" || !isTemplateParam(key)),
        ),
      );
      if (next === "catalog") params.set("tab", "catalog");
      else params.delete("tab");
      return params;
    });
  }

  function openCreate() {
    createForm.reset();
    setCreateOpen(true);
  }
  const restricted = workflowCatalogRestricted(canReadRegistry, workflowsQuery.error);
  let workflowTableState: ReactNode = null;
  if (workflowsQuery.isLoading || projectFilter.pending) {
    workflowTableState = (
      <div className="fs-table-state">
        <strong>{t("react.workflows.loading_workflows", "Loading workflows")}</strong>
        <span>{t("react.workflows.the_catalog_will_remain_in_this_table_layout", "The catalog will remain in this table layout.")}</span>
      </div>
    );
  } else if (workflowsQuery.isSuccess && !projectFilter.error && !workflowRows.length) {
    workflowTableState = (
      <div className="fs-table-state">
        <strong>{t("registry.workflows.empty_title", "No workflows found")}</strong>
        <span>
          {canManageWorkflows
            ? t("react.workflows.try_adjusting_filters_or_create_a_workflow", "Try adjusting filters or create a workflow.")
            : t("react.workflows.try_adjusting_filters", "Try adjusting filters.")}
        </span>
      </div>
    );
  }

  return (
    <PageContainer className="fs-page-sections" width="wide">
      <DsPageHeader
        action={
          <WorkflowCatalogActions
            canManage={canManageWorkflows}
            onCreate={openCreate}
            onImport={() => {
              const projectId = searchParams.get("project_id")?.trim() || "";
              const importPath = projectId
                ? `/registry/workflows/import?project_id=${encodeURIComponent(projectId)}`
                : "/registry/workflows/import";
              ignoreAsyncResult(navigate(importPath));
            }}
          />
        }
        description={
          canManageWorkflows
            ? t("react.workflows.browse_reusable_workflows_and_open_the_builder_for_changes", "Browse reusable workflows and open the builder for changes.")
            : t("react.workflows.browse_reusable_workflows", "Browse reusable workflows.")
        }
        eyebrow={t("trash.category.registry", "Registry")}
        title={t("react.workflows.tab_workflows", "Workflows")}
      />

      <div className="fs-settings-tabs">
        {catalogTabShown ? (
          <DsTabs
            active={activeTab}
            ariaLabel={t("react.workflows.workflow_sections", "Workflow sections")}
            idPrefix="workflows"
            items={[
              { key: "workflows", label: t("react.workflows.tab_workflows", "Workflows") },
              { key: "catalog", label: t("react.workflows.tab_catalog", "Catalog") },
            ]}
            onSelect={selectTab}
          />
        ) : null}
        {catalogTabShown && activeTab === "catalog" ? (
          <div aria-labelledby="workflows-tab-catalog" className="fs-tabs-panel" id="workflows-panel-catalog" role="tabpanel">
            <WorkflowCatalogDialog
              configured={catalogConfigured}
              inline
              // Each Catalog tab choice returns to the results: the open template
              // and the setup step are the catalog's own state.
              resetSignal={catalogVisit}
              onClose={() => selectTab("workflows")}
              onCreated={() => ignoreAsyncResult(refetchWorkflows())}
              onOpenBuilder={(workflowId, projectId) =>
                ignoreAsyncResult(navigate(builderPathFor(workflowId, projectId || pageProjectId)))
              }
              onStartScratch={canManageWorkflows ? openCreate : undefined}
              onViewWorkflow={(workflowId) =>
                ignoreAsyncResult(navigate(`/registry/workflows/${encodeURIComponent(workflowId)}`))
              }
              open
              projectId={pageProjectId}
              readOnly={!canManageWorkflows}
              tab="template"
            />
          </div>
        ) : (
          <div
            aria-labelledby={catalogTabShown ? "workflows-tab-workflows" : undefined}
            className={catalogTabShown ? "fs-tabs-panel" : undefined}
            id={catalogTabShown ? "workflows-panel-workflows" : undefined}
            role={catalogTabShown ? "tabpanel" : undefined}
          >
      {/* The id has to exist whether or not the catalog has rows: the UI lane
            waits on `#workflows-catalog-grid` before it asserts anything, and on
            the empty-state block alone that wait could only pass while the
            catalog was empty. */}
        <article className="fs-card fs-table-card" id="workflows-catalog-grid">
          {/* Filters left, search right: `.fs-search` carries `margin-left: auto`,
              so anything after it is pushed to the right edge with it. The filter is
              the published affordance — an applied chip per active dimension, an
              `Add filter` trigger and `Clear all` only while something is applied —
              rather than a bare checkbox parked next to a heading. */}
          <div className="fs-table-toolbar">
            {canReadRegistry ? (
              <DsSelect
                ariaLabel={t("events.columns.project", "Project")}
                disabled={projectFilter.projectsPending}
                invalid={projectFilter.unavailable}
                onChange={(projectId) => updateParams({ projectId })}
                options={[
                  { value: "", label: t("react.workflows.all_projects", "All projects") },
                  ...projectFilter.projects.map((project) => ({ value: project.id, label: project.name })),
                ]}
                placeholder={projectFilter.projectsPending
                  ? t("react.workflows.loading_projects", "Loading projects...")
                  : t("react.workflows.project_unavailable", "Project unavailable")}
                searchable
                value={pageProjectId}
              />
            ) : null}
            <AdvancedTableFilters
              disabled={workflowsQuery.isLoading}
              fields={localizedWORKFLOW_FILTER_FIELDS(t)}
              onChange={(_key, value) => updateParams({ enabledOnly: value === ENABLED_FILTER_VALUE })}
              onClear={() => updateParams({ enabledOnly: false })}
              values={{ status: enabledOnly ? ENABLED_FILTER_VALUE : "" }}
            />
            <label className="fs-search">
              <Search aria-hidden="true" />
              <input
                aria-label={t("react.workflows.search_workflows_variant_e827cf3e", "Search workflows")}
                id="search"
                onChange={(event) => updateParams({ search: event.target.value })}
                placeholder={t("react.workflows.search_workflows", "Search workflows…")}
                type="search"
                value={search}
              />
            </label>
            <span aria-live="polite" className="fs-management-result-announcement">
              {workflows.length} {" "}{t("react.workflows.workflows", "workflows")}</span>
          </div>
          <DsTable
            ariaLabel={t("react.workflows.workflows_catalog", "Workflows catalog")}
            busy={workflowsQuery.isFetching || projectFilter.fetching}
            columns={workflowColumns}
            onRowClick={(row) => handleWorkflowAction("detail", row)}
            rowKey={(row) => firstStringFromUnknownValue([row.workflow_id, ""])}
            rows={workflowRows}
            state={workflowTableState}
          />
  
          {projectFilter.error ? (
            <DsAlert title={t("react.workflows.project_filter_failed", "Project filter unavailable")} tone="danger">
              <p>{projectFilter.error.message}</p>
              <button className="fs-button" onClick={() => ignoreAsyncResult(projectFilter.retry())} type="button">
                {t("settings.watchdog.retry", "Retry")}
              </button>
            </DsAlert>
          ) : null}
          <WorkflowCatalogAlerts
            error={workflowsQuery.isError ? workflowsQuery.error : null}
            restricted={restricted}
          />
        </article>
          </div>
        )}
      </div>

      <DsDialog
        busy={createForm.creating}
        closeLabel={t("react.workflows.close_create_workflow_dialog", "Close create workflow dialog")}
        description={t("react.workflows.the_workflow_starts_empty_and_opens_in_the_builder_where_steps_and_groups_are_added", "The workflow starts empty and opens in the builder, where steps and groups are added.")}
        eyebrow={t("trash.category.registry", "Registry")}
        footer={
          <>
            <button
              className="fs-button"
              disabled={createForm.creating}
              onClick={() => setCreateOpen(false)}
              type="button"
            >
              {t("extensions.repeater.cancel", "Cancel")}</button>
            <button
              className="fs-button primary"
              disabled={createForm.creating}
              onClick={() => ignoreAsyncResult(handleCreateWorkflow())}
              type="button"
            >
              {createForm.creating ? t("react.workflows.creating_variant_def70944", "Creating...") : t("projects.modal.create", "Create")}
            </button>
          </>
        }
        initialFocusRef={createIdRef}
        onClose={() => setCreateOpen(false)}
        open={createOpen && canManageWorkflows}
        title={t("react.workflows.create_workflow", "Create workflow")}
        variant="form"
      >
        <CreateWorkflowFields form={createForm} idRef={createIdRef} />
      </DsDialog>

      <WorkflowCloneDialog
        onClose={() => setCloneSource(null)}
        onCloned={(workflowId) => {
          ignoreAsyncResult(handleCloneWorkflow(workflowId));
        }}
        sourceWorkflowId={cloneSource}
      />
    </PageContainer>
  );
}
