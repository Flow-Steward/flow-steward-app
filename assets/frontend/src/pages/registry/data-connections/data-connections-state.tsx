import { useI18n } from "@/lib/i18n/useI18n";
import { ConnectionSchemaField } from "./connection-schema-field";
import { useMemo, useRef, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { RowActionMenu, type DsFilterField } from "@/components/shared/design-system";

import { columnsFromGridConfig } from "@/components/shared/data-grid-adapter";

import { normalizeWorkspaceId } from "@/lib/utils/workspace-selection";

import {
  createDataConnection,
  deleteDataConnection,
  fetchDataConnections,
  fetchDatasetConnectors,
  introspectDataConnection,
  testDataConnection,
  toggleDataConnection,
  type DataConnectionRecord,
  type DataConnectionPayload,
} from "@/pages/registry/datasets/api";

import { fetchWorkspaces } from "@/pages/registry/workspaces/api";

import {
  type ConnectionFormField,
  type ConnectorOption,
  type MessageTone,
  csvStrings,
  clusterAdvancedFields,
  connectorOptionsFromCatalog,
  groupConnectionFormFields,
  mutationMessage,
  mutationTone,
  schemaFields,
} from "./data-connection-helpers";

import {
  buildConnectionRows,
  dataConnectionGridConfig,
  type DataConnectionRow,
} from "./data-connection-grid";

import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

import { useHasCapability } from "@/lib/auth/capabilities";

import { isHttpError } from "@/lib/http/errors";
import {
  type FormSubmitEvent,
  type ConnectionDraft,
  EMPTY_DRAFT,
  storedSecretKeys,
  draftValue,
  valueForSchemaField,
} from "./connection-page-helpers";

export function useDataConnectionsState() {
  const { t } = useI18n();
  const canAdministerPlatform = useHasCapability("administer_platform");
  const queryClient = useQueryClient();
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [enabledFilter, setEnabledFilter] = useState("");
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<MessageTone>("info");
  const [createOpen, setCreateOpen] = useState(false);
  const [editingConnectionName, setEditingConnectionName] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [draft, setDraft] = useState<ConnectionDraft>(EMPTY_DRAFT);
  const [pendingDeleteConnection, setPendingDeleteConnection] =
    useState<DataConnectionRecord | null>(null);
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  // The form dialog opens on its first field rather than on the close button.
  const workspaceFieldRef = useRef<HTMLElement | null>(null);

  const workspacesQuery = useQuery({
    queryKey: ["registry", "data-connections", "workspaces"],
    queryFn: () => fetchWorkspaces(),
    refetchInterval: 60_000,
  });

  const connectorsQuery = useQuery({
    queryKey: ["registry", "data-connections", "connectors"],
    queryFn: fetchDatasetConnectors,
    refetchInterval: 60_000,
  });

  const workspaces = useMemo(
    () => (Array.isArray(workspacesQuery.data) ? workspacesQuery.data : []),
    [workspacesQuery.data],
  );
  const resolvedWorkspaceId = useMemo(() => {
    const normalizedWorkspaceId = normalizeWorkspaceId(selectedWorkspaceId);
    if (!normalizedWorkspaceId || !workspacesQuery.isSuccess) {
      return normalizedWorkspaceId;
    }
    return workspaces.some(
      (workspace) => workspace.id === normalizedWorkspaceId,
    )
      ? normalizedWorkspaceId
      : "";
  }, [selectedWorkspaceId, workspaces, workspacesQuery.isSuccess]);
  const connectionsQuery = useQuery({
    queryKey: ["registry", "data-connections", resolvedWorkspaceId],
    queryFn: () =>
      fetchDataConnections({ workspaceId: resolvedWorkspaceId || undefined }),
    refetchInterval: 30_000,
  });
  const workspaceById = useMemo(
    () =>
      new Map(
        workspaces.map((workspace) => [workspace.id, workspace.display_name]),
      ),
    [workspaces],
  );
  const connectors = useMemo(
    () => (Array.isArray(connectorsQuery.data) ? connectorsQuery.data : []),
    [connectorsQuery.data],
  );
  const connectorOptions = useMemo<ConnectorOption[]>(() => {
    return connectors.flatMap((connector) =>
      connectorOptionsFromCatalog(connector as Record<string, unknown>),
    );
  }, [connectors]);
  const selectedConnectorOption = connectorOptions.find(
    (connector) => connector.option_id === draft.connector_option_id,
  );
  const selectedConnectionFields = useMemo(
    () =>
      schemaFields(selectedConnectorOption?.connection_schema || {}, {
        omit: Object.keys(selectedConnectorOption?.preset_config || {}),
        // The discriminator is usually NOT a form field - the catalog variant
        // presets it, which is exactly why it is in `omit`. Requirements still
        // depend on it, so the preset values are merged under what the operator
        // has typed.
        values: {
          ...Object.fromEntries(
            Object.entries(selectedConnectorOption?.preset_config || {}).map(
              ([key, value]) => [key, stringFromUnknownValue(value)],
            ),
          ),
          ...draft.connection_values,
        },
      }),
    [selectedConnectorOption, draft.connection_values],
  );
  const selectedSecretFields = useMemo(
    () => schemaFields(selectedConnectorOption?.secret_schema || {}),
    [selectedConnectorOption],
  );
  const connectionFieldGroups = useMemo(
    () =>
      groupConnectionFormFields(selectedConnectionFields, selectedSecretFields),
    [selectedConnectionFields, selectedSecretFields],
  );
  const advancedClusters = useMemo(
    () => clusterAdvancedFields(connectionFieldGroups.advanced, t),
    [connectionFieldGroups.advanced, t],
  );
  // What the closed summary reports. A stored secret counts as set even though
  // the box is blank, because the connection does carry that value.
  const advancedSetCount = connectionFieldGroups.advanced.filter((field) => {
    const value =
      field.storage === "connection"
        ? draft.connection_values[field.key]
        : draft.secret_values[field.key];
    if (value?.trim()) {
      return true;
    }
    return field.storage === "secret" && draft.existing_secret_keys.includes(field.key);
  }).length;
  const connectorSourceLabelByLocalId = useMemo(() => {
    return Object.fromEntries(
      connectorOptions.map((option) => [
        option.local_connector_id,
        option.label,
      ]),
    ) as Record<string, string>;
  }, [connectorOptions]);
  const allRows = useMemo(
    () =>
      buildConnectionRows(connectionsQuery.data || [], {
        workspaceById,
        connectorSourceLabelByLocalId,
      }, t),
    [connectionsQuery.data, connectorSourceLabelByLocalId, workspaceById, t],
  );
  const rows = useMemo(() => {
    const normalizedSearch = search.trim().toLowerCase();
    return allRows.filter((connection) => {
      const haystack = [
        connection.display_name,
        connection.description || "",
        connection.source_kind,
        connection.environment,
        connection.health_status,
        ...(connection.tags || []),
      ]
        .map((value) => stringFromUnknownValue(value))
        .join(" ")
        .toLowerCase();
      if (normalizedSearch && !haystack.includes(normalizedSearch)) {
        return false;
      }
      if (statusFilter && connection.health_status !== statusFilter) {
        return false;
      }
      if (enabledFilter === "enabled" && !connection.enabled) {
        return false;
      }
      if (enabledFilter === "disabled" && connection.enabled) {
        return false;
      }
      return true;
    });
  }, [allRows, enabledFilter, search, statusFilter]);

  function updateSelectedWorkspaceId(workspaceId: string) {
    setSelectedWorkspaceId(normalizeWorkspaceId(workspaceId));
  }

  function updateDraft(patch: Partial<ConnectionDraft>) {
    setDraft((current) => ({ ...current, ...patch }));
  }

  function showMessage(text: string, tone: MessageTone = "info") {
    setMessage(text);
    setMessageTone(tone);
  }

  function clearMessage() {
    setMessage("");
    setMessageTone("info");
  }

  function handleAddConnection() {
    const connector = connectorOptions[0];
    clearMessage();
    setEditingConnectionName("");
    setAdvancedOpen(false);
    const connectionValues = Object.fromEntries(
      schemaFields(connector?.connection_schema || {}, {
        omit: Object.keys(connector?.preset_config || {}),
      })
        .filter((field) => field.default_value !== undefined)
        .map((field) => [field.key, draftValue(field.default_value)]),
    );
    const secretValues = Object.fromEntries(
      schemaFields(connector?.secret_schema || {})
        .filter((field) => field.default_value !== undefined)
        .map((field) => [field.key, draftValue(field.default_value)]),
    );
    setDraft({
      ...EMPTY_DRAFT,
      workspace_id: resolvedWorkspaceId || workspaces[0]?.id || "",
      connector_option_id: connector?.option_id || "",
      display_name: connector ? `${connector.label} connection` : "",
      connection_values: connectionValues,
      secret_values: secretValues,
    });
    setCreateOpen(true);
  }

  function handleEditConnection(connection: (typeof rows)[number]) {
    const ref = connection.connector_ref_jsonb || {};
    const metadata = connection.metadata_jsonb || {};
    const connectionConfig = connection.connection_config_jsonb || {};
    const datasetConnector =
      metadata &&
      typeof metadata.dataset_connector === "object" &&
      metadata.dataset_connector
        ? (metadata.dataset_connector as Record<string, unknown>)
        : {};
    const connectorId = firstStringFromUnknownValue([
      ref.connector_id,
      datasetConnector.connector_id,
      "",
    ]).trim();
    const localConnectorId = firstStringFromUnknownValue([
      ref.local_connector_id,
      datasetConnector.local_connector_id,
      connectorId,
    ]).trim();
    const optionId = connectorId ? `${connectorId}::${localConnectorId}` : "";
    const option =
      connectorOptions.find((item) => item.option_id === optionId) ||
      connectorOptions.find(
        (item) =>
          item.connector_id === connectorId &&
          Object.keys(item.preset_config).length > 0 &&
          Object.entries(item.preset_config).every(
            ([key, value]) => connectionConfig[key] === value,
          ),
      ) ||
      connectorOptions.find((item) => item.connector_id === connectorId);
    clearMessage();
    setEditingConnectionName(
      connection.display_name || connection.connection_id,
    );
    setAdvancedOpen(false);
    setDraft({
      connection_id: connection.connection_id,
      workspace_id: connection.workspace_id,
      connector_option_id: option?.option_id || "",
      display_name: connection.display_name || "",
      description: connection.description || "",
      environment: connection.environment || "dev",
      enabled: connection.enabled,
      tags_csv: (connection.tags || []).join(", "),
      connection_values: Object.fromEntries(
        schemaFields(option?.connection_schema || {}, {
          omit: Object.keys(option?.preset_config || {}),
        })
          .map((field) => [field.key, draftValue(connectionConfig[field.key])])
          .filter(([, value]) => value !== ""),
      ),
      secret_values: {},
      existing_secret_keys: storedSecretKeys(
        connection.secret_refs_jsonb || {},
      ),
      original_connector_option_id: option?.option_id || "",
      original_connection_config: connectionConfig,
      original_metadata: metadata,
      touched_connection_fields: [],
    });
    setCreateOpen(true);
  }

  function connectionPayloadFromDraft(): DataConnectionPayload {
    const connectorId = firstStringFromUnknownValue([
      draft.connector_option_id.split("::")[0],
      "",
    ]).trim();
    const sameConnector = Boolean(draft.connection_id)
      && draft.original_connector_option_id === draft.connector_option_id;
    const connectionConfig: Record<string, unknown> = {
      ...(sameConnector ? draft.original_connection_config : {}),
      ...selectedConnectorOption?.preset_config,
    };
    for (const field of selectedConnectionFields) {
      // Blank form projections cannot distinguish a stored null/empty string
      // from an explicit clear. Preserve only untouched, known optional values;
      // changed fields still pass through the normal parser and validation.
      const original = draft.original_connection_config || {};
      if (
        sameConnector
        && !field.required
        && Object.hasOwn(original, field.key)
        && (original[field.key] === null || original[field.key] === "")
        && !draft.touched_connection_fields?.includes(field.key)
      ) continue;
      const value = valueForSchemaField(
        draft.connection_values[field.key] || "",
        field, t
      );
      if (value !== undefined) connectionConfig[field.key] = value;
      else delete connectionConfig[field.key];
    }
    const secretRefs: Record<string, unknown> = {};
    for (const field of selectedSecretFields) {
      const rawValue = draft.secret_values[field.key] || "";
      if (draft.connection_id && !rawValue.trim()) {
        continue;
      }
      const value = valueForSchemaField(rawValue, field, t);
      if (value !== undefined) secretRefs[field.key] = value;
    }
    return {
      connection_id: draft.connection_id || undefined,
      workspace_id: draft.workspace_id,
      connector_id: connectorId,
      display_name: draft.display_name.trim(),
      description: draft.description.trim(),
      environment: draft.environment.trim() || "dev",
      enabled: draft.enabled,
      tags: csvStrings(draft.tags_csv),
      connection_config_jsonb: connectionConfig,
      secret_refs_jsonb: secretRefs,
      metadata_jsonb: selectedConnectorOption
        ? {
            ...(sameConnector ? draft.original_metadata : {}),
            dataset_connector: {
              ...(sameConnector ? draft.original_metadata?.dataset_connector as Record<string, unknown> : {}),
              connector_id: selectedConnectorOption.connector_id,
              display_name: selectedConnectorOption.display_name,
              local_connector_id: selectedConnectorOption.local_connector_id,
            },
          }
        : {},
    };
  }

  const createMutation = useMutation({
    mutationFn: createDataConnection,
    onSuccess: async (connection) => {
      setCreateOpen(false);
      setEditingConnectionName("");
      setDraft(EMPTY_DRAFT);
      showMessage(
        draft.connection_id
          ? t("react.registry.display_data_connection_updated", "Data connection updated.")
          : t("react.registry.display_data_connection_created", "Data connection created."),
        "success",
      );
      updateSelectedWorkspaceId(connection.workspace_id || draft.workspace_id);
      await queryClient.invalidateQueries({
        queryKey: ["registry", "data-connections"],
      });
    },
    onError: (error) => {
      showMessage(
        error instanceof Error
          ? error.message
          : t("react.registry.display_failed_create_data_connection", "Failed to create data connection."),
        "error",
      );
    },
  });

  const toggleMutation = useMutation({
    mutationFn: toggleDataConnection,
    onSuccess: async () => {
      showMessage(t("react.registry.display_data_connection_status_updated", "Data connection status updated."), "success");
      await queryClient.invalidateQueries({
        queryKey: ["registry", "data-connections"],
      });
    },
    onError: (error) =>
      showMessage(
        error instanceof Error
          ? error.message
          : t("react.registry.display_failed_update_data_connection", "Failed to update data connection."),
        "error",
      ),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteDataConnection,
    onSuccess: async () => {
      setPendingDeleteConnection(null);
      showMessage(t("react.registry.display_data_connection_deleted", "Data connection deleted."), "success");
      await queryClient.invalidateQueries({
        queryKey: ["registry", "data-connections"],
      });
    },
    onError: (error) =>
      showMessage(
        error instanceof Error
          ? error.message
          : t("react.registry.display_failed_delete_data_connection", "Failed to delete data connection."),
        "error",
      ),
  });

  const testMutation = useMutation({
    mutationFn: testDataConnection,
    onSuccess: async (response) => {
      showMessage(
        mutationMessage(response, {
          success: t("react.registry.display_connection_test_succeeded", "Connection test succeeded."),
          failure: t("react.registry.display_connection_test_failed", "Connection test failed."),
        }),
        mutationTone(response),
      );
      await queryClient.invalidateQueries({
        queryKey: ["registry", "data-connections"],
      });
    },
    onError: (error) =>
      showMessage(
        isHttpError(error) &&
          error.kind === "server_error" &&
          error.message === `Request failed with status ${error.status}.`
          ? t("react.registry.display_unable_test_connection", "Unable to test the data connection. Please try again or contact an administrator.")
          : error instanceof Error
            ? error.message
            : t("react.registry.display_failed_test_connection", "Failed to test data connection."),
        "error",
      ),
  });

  const introspectMutation = useMutation({
    mutationFn: introspectDataConnection,
    onSuccess: async (response) => {
      const result =
        response && typeof response === "object"
          ? (response as Record<string, unknown>)
          : {};
      const payload =
        result.result && typeof result.result === "object"
          ? (result.result as Record<string, unknown>)
          : {};
      const count = Number(payload.resource_count || 0);
      showMessage(
        mutationMessage(response, {
          success: count ? t("react.registry.display_introspection_resource_count", "Introspection succeeded: {count} resources.", { count }) : t("react.registry.display_introspection_succeeded", "Introspection succeeded."),
          failure: t("react.registry.display_introspection_failed", "Introspection failed."),
        }),
        mutationTone(response),
      );
      await queryClient.invalidateQueries({
        queryKey: ["registry", "data-connections"],
      });
    },
    onError: (error) =>
      showMessage(
        error instanceof Error
          ? error.message
          : t("react.registry.failed_to_introspect_data_connection", "Failed to introspect data connection."),
        "error",
      ),
  });

  function handleDeleteConnection(connection: (typeof rows)[number]) {
    if (connection.linked_dataset_count > 0) {
      showMessage(
        t("react.registry.delete_is_blocked_while_datasets_depend_on_this_connection", "Delete is blocked while datasets depend on this connection."),
        "error",
      );
      return;
    }
    setPendingDeleteConnection(connection);
  }

  function confirmDeleteConnection() {
    const connectionId = pendingDeleteConnection?.connection_id;
    if (!connectionId) {
      setPendingDeleteConnection(null);
      return;
    }
    deleteMutation.mutate(connectionId);
  }

  function isMissingRequiredValue(field: ConnectionFormField): boolean {
    if (!field.required) {
      return false;
    }
    const value =
      field.storage === "connection"
        ? draft.connection_values[field.key]
        : draft.secret_values[field.key];
    if (value?.trim()) {
      return false;
    }
    // Editing an existing connection does not resend a stored secret, so a
    // blank secret box is only missing when there is no stored value behind it.
    if (field.storage === "secret" && draft.connection_id) {
      return !draft.existing_secret_keys.includes(field.key);
    }
    return true;
  }

  function firstMissingRequiredField(): ConnectionFormField | undefined {
    // Both groups, not just Advanced. Validating Advanced alone let the form
    // create a connection with its primary connector fields empty.
    return [
      ...connectionFieldGroups.primary,
      ...connectionFieldGroups.advanced,
    ].find(isMissingRequiredValue);
  }

  function handleCreate(event: FormSubmitEvent) {
    event.preventDefault();
    clearMessage();
    if (!draft.workspace_id) {
      showMessage(t("react.registry.display_select_workspace", "Select a workspace."), "error");
      return;
    }
    if (!draft.connector_option_id) {
      showMessage(t("react.registry.display_select_connector", "Select a connector."), "error");
      return;
    }
    if (!draft.display_name.trim()) {
      showMessage(t("react.registry.display_connection_name_required", "Connection name is required."), "error");
      return;
    }
    const missingField = firstMissingRequiredField();
    if (missingField) {
      // Only reveal Advanced when that is where the missing field lives;
      // opening it for a blank primary field hides what the operator must fix.
      if (connectionFieldGroups.advanced.includes(missingField)) {
        setAdvancedOpen(true);
      }
      showMessage(t("react.registry.display_field_required", "{field} is required.", { field: missingField.label }), "error");
      return;
    }
    try {
      createMutation.mutate(connectionPayloadFromDraft());
    } catch (error) {
      showMessage(
        error instanceof Error
          ? error.message
          : t("react.registry.display_connection_config_json", "Connection config must be valid JSON."),
        "error",
      );
    }
  }

  const saving = createMutation.isPending;
  const blockingField = firstMissingRequiredField();
  const createBlocked =
    !draft.workspace_id ||
    !draft.connector_option_id ||
    !draft.display_name.trim() ||
    Boolean(blockingField) ||
    saving;

  const gridConfig = dataConnectionGridConfig(!canAdministerPlatform, t);
  const filterFields: DsFilterField[] = [
    {
      key: "workspace",
      label: t("project_mode.workspace", "Workspace"),
      options: workspaces.map((workspace) => ({
        value: workspace.id,
        label: workspace.display_name,
      })),
    },
    {
      key: "health",
      label: t("settings.mcp.health", "Health"),
      options: [
        { value: "healthy", label: t("react.registry.healthy", "Healthy") },
        { value: "unhealthy", label: t("react.registry.unhealthy", "Unhealthy") },
        { value: "unknown", label: t("react.registry.unknown", "Unknown") },
      ],
    },
    {
      key: "enabled",
      label: t("settings.mcp.status.enabled", "Enabled"),
      options: [
        { value: "enabled", label: t("settings.mcp.status.enabled", "Enabled") },
        { value: "disabled", label: t("react.registry.disabled", "Disabled") },
      ],
    },
  ];

  function changeFilter(key: string, value: string) {
    if (key === "workspace") {
      updateSelectedWorkspaceId(value);
      return;
    }
    if (key === "health") {
      setStatusFilter(value);
      return;
    }
    setEnabledFilter(value);
  }

  function clearFilters() {
    updateSelectedWorkspaceId("");
    setStatusFilter("");
    setEnabledFilter("");
  }

  function renderConnectionCell(connection: DataConnectionRow) {
    return (
      <div>
        {canAdministerPlatform ? (
          <button
            className="fs-link"
            onClick={() => handleEditConnection(connection)}
            type="button"
          >
            {connection.display_name}
          </button>
        ) : (
          <span>{connection.display_name}</span>
        )}
        <div className="fs-table-mono">{connection.connection_id}</div>
        {connection._tags_label ? (
          <div className="fs-ink-tertiary">{connection._tags_label}</div>
        ) : null}
      </div>
    );
  }

  function renderRowActions(connection: DataConnectionRow) {
    const connectionId = connection.connection_id;
    const testPending =
      testMutation.isPending && testMutation.variables === connectionId;
    const introspectPending =
      introspectMutation.isPending &&
      introspectMutation.variables === connectionId;
    const deleteBlocked = connection.linked_dataset_count > 0;
    return (
      <RowActionMenu
        label={t("react.registry.actions_for_value0", "Actions for {value0}", { value0: connection.display_name || connectionId })}
        onOpenChange={(open) => setRowMenu(open ? connectionId : null)}
        open={rowMenu === connectionId}
      >
        <div className="fs-row-menu-group">
          <button
            onClick={() => {
              setRowMenu(null);
              handleEditConnection(connection);
            }}
            role="menuitem"
            type="button"
          >
            {t("extensions.repeater.edit", "Edit")}</button>
          <button
            disabled={!connection.enabled || testPending}
            onClick={() => {
              setRowMenu(null);
              testMutation.mutate(connectionId);
            }}
            role="menuitem"
            type="button"
          >
            {t("react.registry.test", "Test")}</button>
          <button
            disabled={!connection.enabled || introspectPending}
            onClick={() => {
              setRowMenu(null);
              introspectMutation.mutate(connectionId);
            }}
            role="menuitem"
            type="button"
          >
            {t("react.registry.introspect", "Introspect")}</button>
          <button
            disabled={toggleMutation.isPending}
            onClick={() => {
              setRowMenu(null);
              toggleMutation.mutate(connectionId);
            }}
            role="menuitem"
            type="button"
          >
            {connection.enabled ? t("extensions.detail.disable", "Disable") : t("extensions.detail.enable", "Enable")}
          </button>
        </div>
        <div className="fs-row-menu-group">
          <button
            className="danger"
            disabled={deleteBlocked || deleteMutation.isPending}
            onClick={() => {
              setRowMenu(null);
              handleDeleteConnection(connection);
            }}
            role="menuitem"
            title={
              deleteBlocked
                ? t("react.registry.delete_is_blocked_while_datasets_depend_on_this_connection", "Delete is blocked while datasets depend on this connection.")
                : t("react.registry.delete_connection", "Delete connection")
            }
            type="button"
          >
            {t("settings.model_providers.delete", "Delete")}</button>
        </div>
      </RowActionMenu>
    );
  }

  const connectionColumns = columnsFromGridConfig(gridConfig, (row) =>
    renderRowActions(row as DataConnectionRow),
  ).map((column) =>
    column.key === "display_name"
      ? { ...column, cell: (row: Record<string, unknown>) => renderConnectionCell(row as DataConnectionRow) }
      : column,
  );

  const tableState = connectionsQuery.isError ? (
    <div className="fs-table-state">
      <strong>{t("react.registry.unable_to_load_data_connections", "Unable to load data connections.")}</strong>
      <span>{t("react.registry.refresh_to_try_again", "Refresh to try again.")}</span>
    </div>
  ) : rows.length ? null : (
    <div className="fs-table-state">
      <strong>{t("react.registry.no_workspace_data_connections_match_these_filters", "No workspace data connections match these filters.")}</strong>
    </div>
  );

  function renderSchemaField(field: ConnectionFormField) {
    return <ConnectionSchemaField key={`${field.storage}:${field.key}`} field={field} draft={draft} updateDraft={updateDraft} />;
  }
  return {
    canAdministerPlatform,
    handleAddConnection,
    message,
    messageTone,
    connectionsQuery,
    filterFields,
    changeFilter,
    clearFilters,
    resolvedWorkspaceId,
    statusFilter,
    enabledFilter,
    setSearch,
    search,
    rows,
    allRows,
    connectionColumns,
    tableState,
    saving,
    draft,
    editingConnectionName,
    setCreateOpen,
    createBlocked,
    workspaceFieldRef,
    createOpen,
    handleCreate,
    workspacesQuery,
    workspaces,
    connectorsQuery,
    connectorOptions,
    updateDraft,
    setAdvancedOpen,
    connectionFieldGroups,
    renderSchemaField,
    advancedOpen,
    advancedSetCount,
    advancedClusters,
    blockingField,
    deleteMutation,
    pendingDeleteConnection,
    setPendingDeleteConnection,
    confirmDeleteConnection,
  };
}
