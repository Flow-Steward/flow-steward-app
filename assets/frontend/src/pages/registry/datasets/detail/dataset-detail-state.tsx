import { isInstallOwned } from "../dataset-ownership";
import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useMemo, useRef, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useResourceSelection } from "../use-resource-selection";

import { useParams, useSearchParams } from "react-router";

import { ResourceSelectionList } from "../resource-selection-list";

import {
  createDatasetQueryTemplate,
  deleteDatasetQueryTemplate,
  fetchDataConnections,
  fetchDataset,
  fetchDatasetConnectors,
  fetchDatasetProjectCatalog,
  fetchDatasetQueryTemplates,
  introspectDataConnection,
  previewDatasetQueryTemplate,
  toggleDataset,
  toggleDatasetQueryTemplate,
  updateDatasetProjects,
  updateDataset,
  updateDatasetQueryTemplate,
  validateDatasetQueryTemplate,
  type DatasetPayload,
  type DatasetRecord,
  type DatasetQueryTemplate,
  type DatasetQueryTemplatePayload,
} from "@/pages/registry/datasets/api";

import {
  EMPTY_QUERY_DRAFT,
  type DatasetEditDraft,
  type DatasetSchemaSnapshot,
  type QueryDraft,
  type QueryPreviewState,
  buildProjectSelectorRows,
  csvStrings,
  draftFromDataset,
  isRecord,
  normalizePreviewState,
  normalizeSchemaSnapshot,
  parseObjectJson,
  paramsSchemaForSql,
  previewParamsJsonForTemplate,
  projectFilterItemsFromRows,
  queryDraftFromTemplate,
  resolveConnectorIdForConnection,
  resourceScopeFromSelection,
  scopeSelectionFromDataset,
} from "./dataset-detail-helpers";

import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import {
  type DatasetTabKey,
  TAB_KEYS,
  type QueryPreviewDraft,
  buildSavedQueryColumns,
} from "./dataset-detail-page-helpers";

export function useDatasetDetailState() {
  const { t } = useI18n();
  const params = useParams();
  const queryClient = useQueryClient();
  const datasetId = firstStringFromUnknownValue([params.datasetId, ""]).trim();
  const [draft, setDraft] = useState<DatasetEditDraft>(draftFromDataset());
  const [queryDraft, setQueryDraft] = useState<QueryDraft>(EMPTY_QUERY_DRAFT);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") || "";
  const activeTab: DatasetTabKey = TAB_KEYS.has(requestedTab)
    ? (requestedTab as DatasetTabKey)
    : "dataset";
  function goToTab(key: string) {
    const next = new URLSearchParams(searchParams);
    if (key === "dataset") {
      next.delete("tab");
    } else {
      next.set("tab", key);
    }
    setSearchParams(next, { replace: true });
  }
  const [deleteQueryTarget, setDeleteQueryTarget] = useState<DatasetQueryTemplate | null>(null);
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const displayNameRef = useRef<HTMLInputElement>(null);
  const queryNameRef = useRef<HTMLInputElement>(null);
  const previewParamsRef = useRef<HTMLTextAreaElement>(null);
  const rowTriggerLabelRef = useRef("");
  const [queryModalOpen, setQueryModalOpen] = useState(false);
  const [editingQueryId, setEditingQueryId] = useState("");
  const [queryFormError, setQueryFormError] = useState("");
  const [previewDraft, setPreviewDraft] = useState<QueryPreviewDraft | null>(null);
  const [previewState, setPreviewState] = useState<QueryPreviewState | null>(null);
  const [message, setMessage] = useState("");
  const [schemaSnapshot, setSchemaSnapshot] = useState<DatasetSchemaSnapshot>({ resources: [] });
  const {
    resourceSearch, setResourceSearch, selectedResources, setSelectedResources,
    filteredResources, toggleResource, setAllColumns, toggleColumn, selectedResourceCount,
  } = useResourceSelection(schemaSnapshot);
  const [projectSearch, setProjectSearch] = useState("");
  const [projectFilter, setProjectFilter] = useState("all");
  const [selectedProjectIdsDraft, setSelectedProjectIdsDraft] = useState<string[] | null>(null);

  const detailQuery = useQuery({
    queryKey: ["registry", "datasets", datasetId],
    queryFn: () => fetchDataset(datasetId),
    enabled: Boolean(datasetId),
  });

  const connectorsQuery = useQuery({
    queryKey: ["registry", "datasets", "connectors"],
    queryFn: fetchDatasetConnectors,
    refetchInterval: 30_000,
  });

  const savedQueriesQuery = useQuery({
    queryKey: ["registry", "datasets", datasetId, "saved-queries"],
    queryFn: () => fetchDatasetQueryTemplates({ datasetId }),
    enabled: Boolean(datasetId),
    refetchInterval: 30_000,
  });

  const dataConnectionsQuery = useQuery({
    queryKey: ["registry", "datasets", "data-connections"],
    queryFn: () => fetchDataConnections(),
    refetchInterval: 20_000,
  });

  const projectCatalogQuery = useQuery({
    queryKey: ["registry", "datasets", datasetId, "projects"],
    queryFn: () => fetchDatasetProjectCatalog(datasetId),
    enabled: Boolean(datasetId),
    refetchInterval: 30_000,
  });

  const dataset = detailQuery.data;
  const configurationReadOnly = isInstallOwned(dataset);
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
  const workspaceConnections = useMemo(
    () =>
      dataConnections.filter(
        (connection) =>
          !draft.workspace_id ||
          connection.query_capable &&
          firstStringFromUnknownValue([connection.workspace_id, ""]).trim() === draft.workspace_id,
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
  const projectCatalog = projectCatalogQuery.data;
  const linkedProjectIds = projectCatalog?.project_ids || dataset?.project_ids || [];
  const previewRequiresProject = dataset?.metadata_jsonb?.connection_resolution === "project_scope";
  const previewProjectOptions = useMemo(() => {
    if (!projectCatalogQuery.isSuccess) return [];
    const assigned = new Set(projectCatalog?.project_ids || []);
    return (projectCatalog?.available_projects || []).flatMap((project) => {
      const id = firstStringFromUnknownValue([project.id, project.project_id, ""]).trim();
      if (!id || !assigned.has(id)) return [];
      return [{ value: id, label: firstStringFromUnknownValue([project.name, project.display_name, project.project_name, id]) }];
    });
  }, [projectCatalog, projectCatalogQuery.isSuccess]);
  const previewProjectReady = detailQuery.isSuccess && (!previewRequiresProject || previewProjectOptions.some((project) => project.value === previewDraft?.projectId));

  const explicitProjectIds = projectCatalog?.explicit_project_ids || [];
  const selectedProjectIds =
    selectedProjectIdsDraft ?? explicitProjectIds;
  const savedQueries = Array.isArray(savedQueriesQuery.data) ? savedQueriesQuery.data : [];
  const queryConfigurationReadOnly = isInstallOwned(savedQueries.find((query) => query.query_template_id === editingQueryId));
  function assertDatasetMutable() {
    const current = queryClient.getQueryData<DatasetRecord>(["registry", "datasets", datasetId]);
    if (!current || isInstallOwned(current)) {
      throw new Error(t("react.registry.package_configuration_read_only", "Package configuration is read-only."));
    }
  }
  function assertQueryMutable(queryTemplateId: string) {
    const current = queryClient.getQueryData<DatasetQueryTemplate[]>(["registry", "datasets", datasetId, "saved-queries"])
      ?.find((query) => query.query_template_id === queryTemplateId);
    if (!current || isInstallOwned(current)) {
      throw new Error(t("react.registry.package_query_read_only", "Package query configuration is read-only."));
    }
  }
  const queryParamsSchema = useMemo(() => {
    try {
      return parseObjectJson(queryDraft.params_json, t("react.registry.display_params_schema", "Params schema"), t);
    } catch {
      return {};
    }
  }, [queryDraft.params_json, t]);
  const queryOutputSchema = useMemo(() => {
    try {
      return parseObjectJson(queryDraft.output_json, t("react.registry.display_output_schema", "Output schema"), t);
    } catch {
      return {};
    }
  }, [queryDraft.output_json, t]);
  const selectedProjectIdSet = useMemo(() => new Set(selectedProjectIds), [selectedProjectIds]);
  const projectSelectorRows = useMemo(
    () =>
      buildProjectSelectorRows({
        availabilityMode: draft.availability_mode,
        projectCatalog,
        selectedProjectIdSet,
      }, t),
    [draft.availability_mode, projectCatalog, selectedProjectIdSet, t],
  );
  const projectFilterItems = useMemo(
    () => projectFilterItemsFromRows(projectSelectorRows, t),
    [projectSelectorRows, t],
  );

  useEffect(() => {
    const handle = globalThis.setTimeout(() => {
      setDraft(draftFromDataset(dataset));
      setSchemaSnapshot(normalizeSchemaSnapshot(dataset?.schema_snapshot_jsonb));
      setSelectedResources(scopeSelectionFromDataset(dataset));
      setResourceSearch("");
      setSelectedProjectIdsDraft(null);
      setProjectSearch("");
      setProjectFilter("all");
    }, 0);
    return () => globalThis.clearTimeout(handle);
  }, [dataset, setResourceSearch, setSelectedResources]);

  const saveMutation = useMutation({
    mutationFn: (payload: DatasetPayload) => {
      assertDatasetMutable();
      return updateDataset(datasetId, payload);
    },
    onSuccess: async () => {
      setMessage(t("react.registry.dataset_saved", "Dataset saved."));
      await queryClient.invalidateQueries({ queryKey: ["registry", "datasets"] });
      await queryClient.invalidateQueries({ queryKey: ["registry", "datasets", datasetId] });
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_save_dataset", "Failed to save dataset.")),
  });

  const saveAssignmentsMutation = useMutation({
    mutationFn: () => {
      assertDatasetMutable();
      return updateDatasetProjects(datasetId, selectedProjectIds);
    },
    onSuccess: async () => {
      setMessage(t("react.registry.dataset_project_assignments_updated", "Dataset project assignments updated."));
      setSelectedProjectIdsDraft(null);
      await queryClient.invalidateQueries({ queryKey: ["registry", "datasets", datasetId, "projects"] });
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (error) =>
      setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_update_dataset_project_assignments", "Failed to update dataset project assignments.")),
  });

  const toggleMutation = useMutation({
    mutationFn: () => {
      assertDatasetMutable();
      return toggleDataset(datasetId);
    },
    onSuccess: async () => {
      setMessage(t("react.registry.dataset_status_updated", "Dataset status updated."));
      await queryClient.invalidateQueries({ queryKey: ["registry", "datasets"] });
      await queryClient.invalidateQueries({ queryKey: ["registry", "datasets", datasetId] });
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_toggle_dataset", "Failed to toggle dataset.")),
  });

  const introspectMutation = useMutation({
    mutationFn: (connectionId: string) => {
      assertDatasetMutable();
      return introspectDataConnection(connectionId);
    },
    onSuccess: async (payload) => {
      const snapshot = normalizeSchemaSnapshot(
        isRecord(payload) && isRecord(payload.result) ? payload.result.schema_snapshot : {},
      );
      setSchemaSnapshot(snapshot);
      setSelectedResources({});
      setResourceSearch("");
      setMessage(
        snapshot.resources.length
          ? t("react.registry.schema_refreshed_choose_the_resources_and_columns_this_dataset_should_expose", "Schema refreshed. Choose the resources and columns this dataset should expose.")
          : t("react.registry.schema_refreshed_but_no_resources_were_returned", "Schema refreshed, but no resources were returned."),
      );
      await queryClient.invalidateQueries({ queryKey: ["registry", "datasets", "data-connections"] });
    },
    onError: (error) =>
      setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_introspect_data_connection", "Failed to introspect data connection.")),
  });

  const saveQueryMutation = useMutation({
    mutationFn: (payload: DatasetQueryTemplatePayload) => {
      const queryTemplateId = editingQueryId || payload.query_template_id;
      if (queryTemplateId) assertQueryMutable(queryTemplateId);
      return queryTemplateId
        ? updateDatasetQueryTemplate(queryTemplateId, payload)
        : createDatasetQueryTemplate(payload);
    },
    onSuccess: async () => {
      await queryClient.refetchQueries({
        queryKey: ["registry", "datasets", datasetId, "saved-queries"],
      });
      setMessage(editingQueryId ? t("react.registry.saved_query_updated", "Saved query updated.") : t("react.registry.saved_query_created", "Saved query created."));
      setQueryDraft(EMPTY_QUERY_DRAFT);
      setEditingQueryId("");
      setQueryFormError("");
      setQueryModalOpen(false);
    },
    onError: (error) => {
      const detail = error instanceof Error ? error.message : t("react.registry.display_failed_save_query", "Failed to save saved query.");
      setMessage(detail);
      setQueryFormError(detail);
    },
  });

  const toggleQueryMutation = useMutation({
    mutationFn: (queryTemplateId: string) => {
      assertQueryMutable(queryTemplateId);
      return toggleDatasetQueryTemplate(queryTemplateId);
    },
    onSuccess: async () => {
      setMessage(t("react.registry.saved_query_status_updated", "Saved query status updated."));
      await queryClient.invalidateQueries({ queryKey: ["registry", "datasets", datasetId, "saved-queries"] });
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_update_saved_query", "Failed to update saved query.")),
  });

  const deleteQueryMutation = useMutation({
    mutationFn: (queryTemplateId: string) => {
      assertQueryMutable(queryTemplateId);
      return deleteDatasetQueryTemplate(queryTemplateId);
    },
    onSuccess: async () => {
      setMessage(t("react.registry.saved_query_deleted", "Saved query deleted."));
      await queryClient.invalidateQueries({ queryKey: ["registry", "datasets", datasetId, "saved-queries"] });
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_delete_saved_query", "Failed to delete saved query.")),
  });

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

  const validateQueryMutation = useMutation({
    mutationFn: ({ params, queryTemplateId, projectId }: { params: Record<string, unknown>; queryTemplateId: string; projectId?: string }) =>
      projectId ? validateDatasetQueryTemplate(queryTemplateId, params, projectId) : validateDatasetQueryTemplate(queryTemplateId, params),
    onSuccess: (payload) => {
      const result = isRecord(payload) ? payload : {};
      const errors = Array.isArray(result.errors)
        ? result.errors.filter(isRecord)
        : [];
      if (result.ok === false) {
        const firstError = errors[0];
        const detail = firstStringFromUnknownValue([firstError?.message, firstError?.code, t("react.registry.display_query_validation_failed", "Saved query validation failed.")]).trim();
        setMessage(detail || t("react.registry.display_query_validation_failed", "Saved query validation failed."));
        return;
      }
      setMessage(t("react.registry.saved_query_is_valid", "Saved query is valid."));
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_validate_saved_query", "Failed to validate saved query.")),
  });

  const previewQueryMutation = useMutation({
    mutationFn: ({
      params,
      queryTemplateId,
      queryName,
      projectId,
    }: {
      params: Record<string, unknown>;
      queryTemplateId: string;
      queryName: string;
      projectId?: string;
    }) => (projectId ? previewDatasetQueryTemplate(queryTemplateId, params, 100, projectId) : previewDatasetQueryTemplate(queryTemplateId, params)).then((payload) => ({ payload, queryName })),
    onSuccess: ({ payload, queryName }) => {
      const result = isRecord(payload) ? payload : {};
      const errors = Array.isArray(result.errors) ? result.errors.filter(isRecord) : [];
      if (result.ok === false) {
        const firstError = errors[0];
        const detail = firstStringFromUnknownValue([firstError?.message, firstError?.code, t("react.registry.failed_to_preview_saved_query", "Failed to preview saved query.")]).trim();
        setMessage(detail || t("react.registry.failed_to_preview_saved_query", "Failed to preview saved query."));
        setPreviewDraft((current) => (current ? { ...current, error: detail || t("react.registry.failed_to_preview_saved_query", "Failed to preview saved query.") } : current));
        return;
      }
      setPreviewState(normalizePreviewState(queryName, payload, t));
      setPreviewDraft(null);
      setMessage(t("react.registry.saved_query_preview_loaded", "Saved query preview loaded."));
    },
    onError: (error) => {
      const detail = error instanceof Error ? error.message : t("react.registry.failed_to_preview_saved_query", "Failed to preview saved query.");
      setMessage(detail);
      setPreviewDraft((current) => (current ? { ...current, error: detail } : current));
    },
  });

  function updateDraft(patch: Partial<DatasetEditDraft>) {
    if (configurationReadOnly) return;
    setDraft((current) => ({ ...current, ...patch }));
  }

  function toggleProjectAssignment(projectId: string) {
    if (configurationReadOnly) return;
    setSelectedProjectIdsDraft((current) => {
      const source = current ?? selectedProjectIds;
      if (source.includes(projectId)) {
        return source.filter((item) => item !== projectId);
      }
      return [...source, projectId].sort((left, right) => left.localeCompare(right));
    });
  }

  function updateQueryDraft(patch: Partial<QueryDraft>) {
    if (queryConfigurationReadOnly) return;
    setQueryFormError("");
    setQueryDraft((current) => ({ ...current, ...patch }));
  }

  function openCreateQueryModal() {
    setEditingQueryId("");
    setQueryDraft(EMPTY_QUERY_DRAFT);
    setQueryFormError("");
    setQueryModalOpen(true);
    goToTab("saved-queries");
  }

  function openEditQueryModal(query: DatasetQueryTemplate) {
    setEditingQueryId(query.query_template_id);
    setQueryDraft(queryDraftFromTemplate(query));
    setQueryFormError("");
    setQueryModalOpen(true);
    goToTab("saved-queries");
  }

  function openPreviewQueryModal(query: DatasetQueryTemplate, mode: "preview" | "validate" = "preview") {
    setPreviewDraft({
      error: "",
      mode,
      paramsJson: previewParamsJsonForTemplate(query),
      projectId: "",
      query,
    });
  }

  function runPreviewQuery() {
    if (!previewDraft) {
      return;
    }
    if (!previewProjectReady) {
      setPreviewDraft((current) => current ? { ...current, error: t("react.registry.choose_a_project", "Choose a project.") } : current);
      return;
    }
    const projectId = previewRequiresProject ? previewDraft.projectId : undefined;
    try {
      const params = parseObjectJson(previewDraft.paramsJson, previewDraft.mode === "validate" ? t("react.registry.display_validation_parameters", "Validation parameters") : t("react.registry.display_preview_parameters", "Preview parameters"), t);
      if (previewDraft.mode === "validate") {
        validateQueryMutation.mutate({ params, queryTemplateId: previewDraft.query.query_template_id, projectId });
        setPreviewDraft(null);
        return;
      }
      previewQueryMutation.mutate({
        params,
        queryTemplateId: previewDraft.query.query_template_id,
        queryName: previewDraft.query.display_name || previewDraft.query.query_template_id,
        projectId,
      });
    } catch (error) {
      setPreviewDraft((current) =>
        current
          ? {
              ...current,
              error: error instanceof Error ? error.message : t("react.registry.preview_parameters_must_be_a_json_object", "Preview parameters must be a JSON object."),
            }
          : current,
      );
    }
  }

  function closeQueryModal() {
    setQueryModalOpen(false);
    setEditingQueryId("");
    setQueryDraft(EMPTY_QUERY_DRAFT);
    setQueryFormError("");
  }

  function handleSaveSavedQuery() {
    if (queryConfigurationReadOnly) return;
    setMessage("");
    setQueryFormError("");
    if (!datasetId) {
      const detail = t("react.registry.display_dataset_required_for_queries", "Dataset is required before creating saved queries.");
      setMessage(detail);
      setQueryFormError(detail);
      return;
    }
    if (!queryDraft.display_name.trim() || !queryDraft.sql.trim()) {
      const detail = t("react.registry.display_query_name_sql_required", "Saved query name and SQL are required.");
      setMessage(detail);
      setQueryFormError(detail);
      return;
    }
    try {
      const existingQuery = editingQueryId
        ? savedQueries.find((query) => query.query_template_id === editingQueryId)
        : undefined;
      saveQueryMutation.mutate({
        query_template_id: editingQueryId || undefined,
        dataset_id: datasetId,
        display_name: queryDraft.display_name.trim(),
        description: queryDraft.description.trim(),
        query_language: queryDraft.query_language || "sql",
        query_definition_jsonb: { sql: queryDraft.sql.trim() },
        params_schema_jsonb: paramsSchemaForSql(
          queryDraft.sql,
          parseObjectJson(queryDraft.params_json, t("react.registry.display_params_schema", "Params schema"), t),
        ),
        output_schema_jsonb: parseObjectJson(queryDraft.output_json, t("react.registry.display_output_schema", "Output schema"), t),
        validation_result_jsonb: existingQuery?.validation_result_jsonb || {},
        enabled: existingQuery?.enabled ?? true,
        version: existingQuery?.version ?? 1,
      });
    } catch (error) {
      const detail = error instanceof Error ? error.message : t("react.registry.display_invalid_query_payload", "Invalid saved query payload.");
      setMessage(detail);
      setQueryFormError(detail);
    }
  }
  function handleSave() {
    if (!dataset || configurationReadOnly) return;
    setMessage("");
    if (!selectedConnector) {
      setMessage(t("react.registry.select_an_installed_dataset_connector", "Select an installed dataset connector."));
      return;
    }
    const resourceScopeJsonb = resourceScopeFromSelection(schemaSnapshot, selectedResources);
    if (!resourceScopeJsonb.resources.length) {
      setMessage(t("react.registry.select_at_least_one_resource_before_saving_the_dataset", "Select at least one resource before saving the dataset."));
      return;
    }
    const payload: DatasetPayload = {
      display_name: draft.display_name.trim(),
      workspace_id: draft.workspace_id,
      availability_mode: draft.availability_mode,
      description: draft.description.trim(),
      connection_id: draft.connection_id.trim(),
      read_only: draft.read_only,
      enabled: draft.enabled,
      project_ids:
        draft.availability_mode === "specific_projects_only" ? selectedProjectIds : [],
      tags: csvStrings(draft.tags_csv),
      schema_snapshot_jsonb: {},
      resource_scope_jsonb: resourceScopeJsonb,
    };
    saveMutation.mutate(payload);
  }

  const savedQueryColumns = buildSavedQueryColumns({
    activeRowMenuId: rowMenu,
    closeRowMenu: () => setRowMenu(null),
    openEdit: openEditQueryModal,
    openPreview: openPreviewQueryModal,
    requestDelete: (query) => {
      const current = queryClient.getQueryData<DatasetQueryTemplate[]>(["registry", "datasets", datasetId, "saved-queries"])
        ?.find((item) => item.query_template_id === query.query_template_id);
      if (current && !isInstallOwned(current)) setDeleteQueryTarget(current);
    },
    setRowMenuOpen: (query, label, open) => {
      if (open) {
        rowTriggerLabelRef.current = label;
      }
      setRowMenu(open ? query.query_template_id : null);
    },
    toggleQuery: (queryTemplateId) => toggleQueryMutation.mutate(queryTemplateId),
  }, t);

  const resourceBlock = (
    <div className="fs-field full">
      <div className="fs-footer-actions">
        <span className="fs-field-label">{t("react.registry.schema_resources", "Schema resources")}</span>
        <button
          className="fs-button"
          disabled={!selectedConnection || introspectMutation.isPending}
          onClick={() =>
            selectedConnection && introspectMutation.mutate(selectedConnection.connection_id)
          }
          type="button"
        >
          {introspectMutation.isPending ? t("react.registry.refreshing_schema", "Refreshing schema...") : t("react.registry.introspect_schema", "Introspect schema")}
        </button>
      </div>
      <span className="fs-field-helper">
        {t("react.registry.refresh_schema_from_the_connection_then_choose_the_exact_resources_and_columns_this_dataset_may_expose", "Refresh schema from the connection, then choose the exact resources and columns this dataset may expose.")}</span>
      <input
        aria-label={t("react.registry.search_tables_or_columns", "Search tables or columns")}
        className="fs-input"
        onChange={(event) => setResourceSearch(event.target.value)}
        placeholder={t("react.registry.search_tables_or_columns", "Search tables or columns")}
        type="search"
        value={resourceSearch}
      />
      {schemaSnapshot.resources.length ? (
        filteredResources.length ? (
          <ResourceSelectionList
            onSetAllColumns={setAllColumns}
            onToggleColumn={toggleColumn}
            onToggleResource={toggleResource}
            resources={filteredResources}
            selectedResources={selectedResources}
          />
        ) : (
          <div className="fs-table-state">
            <strong>{t("react.registry.no_resources_match_the_current_search", "No resources match the current search")}</strong>
            <span>{t("react.registry.clear_the_search_to_see_the_rest_of_the_schema", "Clear the search to see the rest of the schema.")}</span>
          </div>
        )
      ) : (
        <div className="fs-table-state">
          <strong>{t("react.registry.no_schema_snapshot_loaded", "No schema snapshot loaded")}</strong>
          <span>
            {t("react.registry.use_introspection_on_the_selected_connection_to_load_resources", "Use introspection on the selected connection to load resources.")}</span>
        </div>
      )}
      <span className="fs-field-helper">{t("react.registry.selected_resources", "Selected resources:")}{" "}{selectedResourceCount || 0}</span>
    </div>
  );
  return {
    configurationReadOnly,
    queryConfigurationReadOnly,
    linkedProjectIds,
    toggleMutation,
    datasetId,
    draft,
    dataset,
    detailQuery,
    activeTab,
    goToTab,
    updateDraft,
    displayNameRef,
    workspaceConnections,
    setSchemaSnapshot,
    setSelectedResources,
    setResourceSearch,
    dataConnectionsQuery,
    selectedConnection,
    selectedConnector,
    resourceBlock,
    projectFilter,
    projectFilterItems,
    projectCatalogQuery,
    setProjectFilter,
    saveAssignmentsMutation,
    setProjectSearch,
    toggleProjectAssignment,
    projectSelectorRows,
    projectSearch,
    selectedProjectIds,
    saveMutation,
    selectedResourceCount,
    handleSave,
    message,
    openCreateQueryModal,
    savedQueriesQuery,
    savedQueryColumns,
    savedQueries,
    previewState,
    setPreviewState,
    previewQueryMutation,
    validateQueryMutation,
    previewDraft,
    previewRequiresProject,
    previewProjectOptions,
    previewProjectReady,
    setPreviewDraft,
    runPreviewQuery,
    previewParamsRef,
    saveQueryMutation,
    closeQueryModal,
    handleSaveSavedQuery,
    queryNameRef,
    queryModalOpen,
    editingQueryId,
    queryFormError,
    updateQueryDraft,
    queryDraft,
    queryOutputSchema,
    queryParamsSchema,
    schemaSnapshot,
    deleteQueryMutation,
    setDeleteQueryTarget,
    focusRowTrigger,
    deleteQueryTarget,
  };
}
