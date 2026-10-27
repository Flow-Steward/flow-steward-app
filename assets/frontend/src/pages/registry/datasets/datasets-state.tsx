import { isInstallOwned } from "./dataset-ownership";
import { useI18n } from "@/lib/i18n/useI18n";
import { useCallback, useDeferredValue, useMemo, useRef, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useResourceSelection } from "./use-resource-selection";

import { type AssignmentSelectorRow } from "@/components/shared/assignment-selector";

import {
  type DatasetRecord,
  createDataset,
  fetchDataConnections,
  deleteDataset,
  fetchDatasetConnectors,
  fetchDatasets,
  introspectDataConnection,
  toggleDataset,
} from "@/pages/registry/datasets/api";

import {
  buildDatasetRows,
  datasetEmptyState,
  datasetFilterFields,
  datasetGridConfig,
} from "@/pages/registry/datasets/dataset-grid";

import { fetchWorkspaceProjects, fetchWorkspaces } from "@/pages/registry/workspaces/api";

import { useHasCapability } from "@/lib/auth/capabilities";

import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

import { RowActionMenu, type DsTableColumn } from "@/components/shared/design-system";

import { columnsFromGridConfig, visibleRowActions } from "@/components/shared/data-grid-adapter";
import {
  type FormSubmitEvent,
  type DatasetDraft,
  type DatasetSchemaSnapshot,
  EMPTY_DRAFT,
  isRecord,
  normalizeSchemaSnapshot,
  connectionSchemaSnapshot,
  resourceScopeFromSelection,
  draftToPayload,
  stringValue,
  resolveConnectorIdForConnection,
} from "./dataset-page-helpers";

export function useDatasetsState() {
  const { t } = useI18n();
  const canAdministerPlatform = useHasCapability("administer_platform");
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [draft, setDraft] = useState<DatasetDraft>(EMPTY_DRAFT);
  const [schemaSnapshot, setSchemaSnapshot] = useState<DatasetSchemaSnapshot>({
    resources: [],
  });
  const {
    resourceSearch, setResourceSearch, selectedResources, setSelectedResources,
    filteredResources, toggleResource, setAllColumns, toggleColumn, selectedResourceCount,
  } = useResourceSelection(schemaSnapshot);
  const [projectSearch, setProjectSearch] = useState("");
  const [projectFilter, setProjectFilter] = useState("all");
  const [message, setMessage] = useState("");
  const [createError, setCreateError] = useState("");
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [deleteTarget, setDeleteTarget] = useState("");
  const displayNameRef = useRef<HTMLInputElement>(null);
  // The row trigger that opened the menu, so both close paths can hand focus
  // back to it — the same approach `catalog-surface.tsx` takes.
  const rowTriggerLabelRef = useRef("");

  const datasetsQuery = useQuery({
    queryKey: ["registry", "datasets"],
    queryFn: fetchDatasets,
    refetchInterval: 20_000,
  });

  const connectorsQuery = useQuery({
    queryKey: ["registry", "datasets", "connectors"],
    queryFn: fetchDatasetConnectors,
    refetchInterval: 30_000,
  });
  const dataConnectionsQuery = useQuery({
    queryKey: ["registry", "datasets", "data-connections"],
    queryFn: () => fetchDataConnections(),
    refetchInterval: 20_000,
  });
  const workspacesQuery = useQuery({
    queryKey: ["registry", "workspaces", "datasets"],
    queryFn: () => fetchWorkspaces(),
    refetchInterval: 30_000,
  });
  const workspaceProjectsQuery = useQuery({
    queryKey: ["registry", "workspaces", draft.workspace_id, "datasets", "projects"],
    queryFn: () => fetchWorkspaceProjects(draft.workspace_id),
    enabled: Boolean(draft.workspace_id),
    refetchInterval: 30_000,
  });

  const datasets = useMemo(
    () => (Array.isArray(datasetsQuery.data) ? datasetsQuery.data : []),
    [datasetsQuery.data],
  );
  const rows = useMemo(() => buildDatasetRows(datasets, t), [datasets, t]);
  const connectors = useMemo(
    () => (Array.isArray(connectorsQuery.data) ? connectorsQuery.data : []),
    [connectorsQuery.data],
  );
  const connectorById = useMemo(
    () => new Map(connectors.map((connector) => [connector.connector_id, connector])),
    [connectors],
  );
  const dataConnections = useMemo(
    () => (Array.isArray(dataConnectionsQuery.data) ? dataConnectionsQuery.data : []),
    [dataConnectionsQuery.data],
  );
  const workspaces = useMemo(
    () => (Array.isArray(workspacesQuery.data) ? workspacesQuery.data : []),
    [workspacesQuery.data],
  );
  const workspaceProjects = useMemo(
    () => (Array.isArray(workspaceProjectsQuery.data) ? workspaceProjectsQuery.data : []),
    [workspaceProjectsQuery.data],
  );
  const workspaceConnections = useMemo(
    () =>
      dataConnections.filter(
        (connection) =>
          connection.query_capable &&
          (!draft.workspace_id ||
            firstStringFromUnknownValue([connection.workspace_id, ""]).trim() === draft.workspace_id),
      ),
    [dataConnections, draft.workspace_id],
  );
  const selectedConnection = useMemo(
    () => workspaceConnections.find((connection) => connection.connection_id === draft.connection_id),
    [workspaceConnections, draft.connection_id],
  );
  const effectiveConnectorId = selectedConnection
    ? resolveConnectorIdForConnection(selectedConnection)
    : "";
  const selectedConnector = connectorById.get(effectiveConnectorId);
  const selectedProjectIdSet = useMemo(() => new Set(draft.project_ids), [draft.project_ids]);
  const projectSelectorRows = useMemo<AssignmentSelectorRow[]>(() => {
    return workspaceProjects.map((project) => {
      const projectId = firstStringFromUnknownValue([project.id, ""]).trim();
      const title = firstStringFromUnknownValue([project.name, project.display_name, projectId, "Project"]);
      const selected =
        draft.availability_mode === "all_workspace_projects" || selectedProjectIdSet.has(projectId);
      return {
        id: projectId,
        title,
        subtitle: projectId ? `#${projectId}` : "",
        badges: [
          draft.availability_mode === "all_workspace_projects"
            ? { label: t("react.registry.workspace_wide_variant_175e8123", "workspace-wide"), tone: "info" as const }
            : selected
              ? { label: t("react.workflows.linked_variant_2272bea6", "linked"), tone: "linked" as const }
              : { label: t("react.registry.available", "available"), tone: "available" as const },
        ],
        disabled: draft.availability_mode === "all_workspace_projects" || !projectId,
        selected,
        filterKeys: [
          "all",
          selected ? "linked" : "available",
          draft.availability_mode === "all_workspace_projects" ? "workspace-wide" : "",
        ].filter(isTruthy),
        searchText: `${title} ${projectId}`.toLowerCase(),
      };
    });
  }, [draft.availability_mode, selectedProjectIdSet, workspaceProjects, t]);
  const projectFilterItems = useMemo(
    () => [
      { key: "all", label: t("users.filter.all_states", "All"), count: projectSelectorRows.length },
      { key: "linked", label: t("react.registry.linked", "Linked"), count: projectSelectorRows.filter((row) => row.selected).length },
      {
        key: "available",
        label: t("react.registry.available_variant_e6744473", "Available"),
        count: projectSelectorRows.filter((row) => !row.selected).length,
      },
      {
        key: "workspace-wide",
        label: t("react.registry.workspace_wide", "Workspace-wide"),
        count: projectSelectorRows.filter((row) => (row.filterKeys || []).includes("workspace-wide")).length,
      },
    ],
    [projectSelectorRows, t],
  );

  const refreshDatasets = async () => {
    await queryClient.invalidateQueries({ queryKey: ["registry", "datasets"] });
    await queryClient.invalidateQueries({ queryKey: ["registry", "datasets", "connectors"] });
  };

  const createMutation = useMutation({
    mutationFn: createDataset,
    onSuccess: async () => {
      setDraft(EMPTY_DRAFT);
      setCreateOpen(false);
      setCreateError("");
      setProjectSearch("");
      setProjectFilter("all");
      setMessage(t("react.registry.dataset_created", "Dataset created."));
      await refreshDatasets();
    },
    onError: (error) => {
      const text = error instanceof Error ? error.message : t("react.registry.display_failed_create_dataset", "Failed to create dataset.");
      setCreateError(text);
      setMessage(text);
    },
  });

  const currentMutableDataset = useCallback((datasetId: string) => {
    const current = queryClient.getQueryData<DatasetRecord[]>(["registry", "datasets"])
      ?.find((dataset) => dataset.dataset_id === datasetId);
    return current && !isInstallOwned(current) ? current : undefined;
  }, [queryClient]);
  function assertDatasetMutable(datasetId: string) {
    if (!currentMutableDataset(datasetId)) {
      throw new Error(t("react.registry.package_configuration_read_only", "Package configuration is read-only."));
    }
  }

  const toggleMutation = useMutation({
    mutationFn: (datasetId: string) => {
      assertDatasetMutable(datasetId);
      return toggleDataset(datasetId);
    },
    onSuccess: async () => {
      setMessage(t("react.registry.dataset_status_updated", "Dataset status updated."));
      await refreshDatasets();
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_update_dataset", "Failed to update dataset.")),
  });

  const deleteMutation = useMutation({
    mutationFn: (datasetId: string) => {
      assertDatasetMutable(datasetId);
      return deleteDataset(datasetId);
    },
    onSuccess: async () => {
      setMessage(t("react.registry.dataset_moved_to_trash", "Dataset moved to Trash."));
      await refreshDatasets();
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_delete_dataset", "Failed to delete dataset.")),
  });

  const introspectMutation = useMutation({
    mutationFn: introspectDataConnection,
    onSuccess: (payload) => {
      const snapshot = normalizeSchemaSnapshot(
        isRecord(payload) && isRecord(payload.result) ? payload.result.schema_snapshot : {},
      );
      setSchemaSnapshot(snapshot);
      setSelectedResources({});
      setResourceSearch("");
      setMessage(
        snapshot.resources.length
          ? t("react.registry.schema_loaded_select_the_resources_you_want_to_expose_in_this_dataset", "Schema loaded. Select the resources you want to expose in this dataset.")
          : t("react.registry.schema_loaded_but_no_resources_were_returned", "Schema loaded, but no resources were returned."),
      );
    },
    onError: (error) =>
      setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_introspect_data_connection", "Failed to introspect data connection.")),
  });

  const saving = createMutation.isPending || toggleMutation.isPending || deleteMutation.isPending;
  const loadError = datasetsQuery.error || connectorsQuery.error;

  function focusRowTrigger() {
    const label = rowTriggerLabelRef.current;
    rowTriggerLabelRef.current = "";
    if (!label) {
      return;
    }
    globalThis.setTimeout(() => {
      globalThis.document?.querySelector<HTMLElement>(`[aria-label="${label}"]`)?.focus();
    }, 0);
  }

  const handleDelete = useCallback((datasetId: string) => {
    if (currentMutableDataset(datasetId)) setDeleteTarget(datasetId);
  }, [currentMutableDataset]);

  const handleToggle = useCallback((datasetId: string) => {
    if (currentMutableDataset(datasetId)) toggleMutation.mutate(datasetId);
  }, [currentMutableDataset, toggleMutation]);

  const toggleProjectAssignment = useCallback((projectId: string) => {
    setDraft((current) => ({
      ...current,
      project_ids: current.project_ids.includes(projectId)
        ? current.project_ids.filter((item) => item !== projectId)
        : [...current.project_ids, projectId],
    }));
  }, []);

  function openDataset(row: Record<string, unknown>) {
    const datasetId = stringValue(row.dataset_id || row.id);
    if (datasetId) {
      globalThis.location.href = `/registry/datasets/${encodeURIComponent(datasetId)}`;
    }
  }

  function handleDatasetAction(actionId: string, row: Record<string, unknown>) {
    const datasetId = stringValue(row.dataset_id || row.id);
    if (!datasetId || !actionId) {
      return;
    }
    if (actionId === "edit" || actionId === "view") {
      globalThis.location.href = `/registry/datasets/${encodeURIComponent(datasetId)}`;
      return;
    }
    if (actionId === "toggle-on" || actionId === "toggle-off" || actionId === "toggle") {
      handleToggle(datasetId);
      return;
    }
    if (actionId === "delete") {
      handleDelete(datasetId);
    }
  }

  const datasetsConfig = useMemo(() => datasetGridConfig( undefined, t), [ t]);
  const filterFields = useMemo(() => datasetFilterFields( t), [ t]);
  const emptyState = useMemo(() => datasetEmptyState( t), [ t]);
  const visibleRows = useMemo(() => {
    const needle = deferredSearch.trim().toLowerCase();
    return rows.filter((row) => {
      for (const [key, value] of Object.entries(filters)) {
        if (!value) {
          continue;
        }
        if (stringValue((row as Record<string, unknown>)[key]) !== value) {
          return false;
        }
      }
      if (!needle) {
        return true;
      }
      // Every string the row already carries is searchable: a per-column field
      // list drifts the first time a column is added.
      return Object.values(row as Record<string, unknown>).some(
        (value) => typeof value === "string" && value.toLowerCase().includes(needle),
      );
    });
  }, [deferredSearch, filters, rows]);

  const datasetColumns = useMemo(
    () =>
      columnsFromGridConfig(datasetsConfig, (row) => {
        const datasetId = stringValue(row.dataset_id || row.id);
        return (
          <RowActionMenu
            label={t("react.registry.actions_for_value0", "Actions for {value0}", { value0: datasetId })}
            onOpenChange={(open) => {
              if (open) {
                rowTriggerLabelRef.current = t("react.registry.actions_for_value0", "Actions for {value0}", { value0: datasetId });
              }
              setRowMenu(open ? datasetId : null);
            }}
            open={rowMenu === datasetId}
          >
            {visibleRowActions(datasetsConfig, row).map((action) => (
              <div className="fs-row-menu-group" key={action.action_id}>
                <button
                  className={action.variant === "danger" ? "danger" : undefined}
                  onClick={() => {
                    setRowMenu(null);
                    handleDatasetAction(action.action_id, row);
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
      }).map<DsTableColumn<Record<string, unknown>>>((column) => column.key === "_mode_label" ? {
        ...column,
        cell: (row) => row.read_only
          ? t("react.registry.display_read_only_lowercase", "read only")
          : t("react.registry.display_read_write_lowercase", "read/write"),
      } : column),
    // handleDatasetAction reads the latest state on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [datasetsConfig, rowMenu, t],
  );

  function updateDraft(patch: Partial<DatasetDraft>) {
    setDraft((current) => ({ ...current, ...patch }));
  }

  function closeCreate() {
    setCreateOpen(false);
    setDraft(EMPTY_DRAFT);
    setCreateError("");
    setSchemaSnapshot({ resources: [] });
    setSelectedResources({});
    setResourceSearch("");
    setProjectSearch("");
    setProjectFilter("all");
  }

  function openCreate() {
    setMessage("");
    setCreateError("");
    const firstWorkspaceId = workspaces[0]?.id || "";
    const firstWorkspaceConnection = dataConnections.find(
      (connection) =>
        connection.query_capable &&
        firstStringFromUnknownValue([connection.workspace_id, ""]).trim() === firstWorkspaceId,
    );
    const firstConnection =
      firstWorkspaceConnection ||
      (!firstWorkspaceId ? dataConnections.find((connection) => connection.query_capable) : undefined);
    const firstConnectionId = firstConnection?.connection_id || "";
    const initialWorkspaceId =
      firstWorkspaceId || firstStringFromUnknownValue([firstConnection?.workspace_id, ""]).trim();
    setDraft({
      ...EMPTY_DRAFT,
      workspace_id: initialWorkspaceId,
      connection_id: firstConnectionId,
    });
    setSchemaSnapshot(connectionSchemaSnapshot(firstConnection));
    setSelectedResources({});
    setResourceSearch("");
    setProjectSearch("");
    setProjectFilter("all");
    setCreateOpen(true);
  }
  function handleCreate(event: FormSubmitEvent) {
    event.preventDefault();
    setMessage("");
    setCreateError("");
    const showCreateError = (text: string) => {
      setCreateError(text);
      setMessage(text);
    };
    if (!draft.display_name.trim()) {
      showCreateError(t("react.registry.display_dataset_name_required", "Dataset display name is required."));
      return;
    }
    if (!draft.workspace_id.trim()) {
      showCreateError(t("react.registry.display_select_workspace_first", "Select a workspace first."));
      return;
    }
    if (!selectedConnection) {
      showCreateError(t("react.registry.display_select_data_connection_first", "Select a data connection first."));
      return;
    }
    if (!selectedConnector) {
      showCreateError(t("react.registry.display_no_valid_dataset_connector", "The selected data connection has no valid dataset connector."));
      return;
    }
    const resourceScopeJsonb = resourceScopeFromSelection(schemaSnapshot, selectedResources);
    if (!resourceScopeJsonb.resources.length) {
      showCreateError(t("react.registry.display_select_resource_create_dataset", "Select at least one resource before creating a dataset."));
      return;
    }
    createMutation.mutate(draftToPayload(draft, resourceScopeJsonb));
  }

  const tableState = datasetsQuery.isLoading ? (
    <div className="fs-table-state">
      <strong>{t("react.registry.loading_datasets", "Loading datasets")}</strong>
      <span>{t("react.registry.the_catalog_will_remain_in_this_table_layout", "The catalog will remain in this table layout.")}</span>
    </div>
  ) : visibleRows.length ? null : rows.length ? (
    <div className="fs-table-state">
      <strong>{t("react.registry.no_datasets_match_these_filters", "No datasets match these filters")}</strong>
      <span>{t("react.registry.clear_the_search_or_a_filter_to_see_the_rest_of_the_catalog", "Clear the search or a filter to see the rest of the catalog.")}</span>
    </div>
  ) : (
    <div className="fs-table-state">
      <strong>{emptyState.title}</strong>
      <span>{emptyState.description}</span>
    </div>
  );
  return {
    refreshDatasets,
    canAdministerPlatform,
    openCreate,
    loadError,
    message,
    datasetsQuery,
    filterFields,
    setFilters,
    filters,
    setSearch,
    search,
    visibleRows,
    rows,
    datasetColumns,
    openDataset,
    tableState,
    saving,
    closeCreate,
    displayNameRef,
    createOpen,
    handleCreate,
    connectorsQuery,
    connectors,
    updateDraft,
    draft,
    dataConnections,
    setSchemaSnapshot,
    setSelectedResources,
    setResourceSearch,
    workspaces,
    workspaceConnections,
    selectedConnector,
    selectedConnection,
    projectFilter,
    projectFilterItems,
    workspaceProjectsQuery,
    setProjectFilter,
    setProjectSearch,
    toggleProjectAssignment,
    projectSelectorRows,
    projectSearch,
    introspectMutation,
    resourceSearch,
    schemaSnapshot,
    filteredResources,
    selectedResources,
    toggleResource,
    setAllColumns,
    toggleColumn,
    selectedResourceCount,
    createError,
    deleteMutation,
    setDeleteTarget,
    focusRowTrigger,
    deleteTarget,
  };
}
