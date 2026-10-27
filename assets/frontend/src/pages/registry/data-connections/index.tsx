import { useI18n } from "@/lib/i18n/useI18n";
import { ChevronRight, Database, RefreshCw } from "lucide-react";

import {
  AdvancedTableFilters,
  DsAlert,
  DsDialog,
  DsPageHeader,
  DsSelect,
  DsTable,
  PageContainer,
  RequiredLabel,
  Search,
} from "@/components/shared/design-system";

import { schemaFields } from "./data-connection-helpers";

import { type DataConnectionRow } from "./data-connection-grid";

import { ignoreAsyncResult } from "@/lib/utils/async";

import { draftValue, saveButtonLabel } from "./connection-page-helpers";
import { useDataConnectionsState } from "./data-connections-state";

export function RegistryDataConnectionsPage() {
  const { t } = useI18n();
  const {
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
  } = useDataConnectionsState();

  return (
    <PageContainer width="wide">
      {/* `.fs-panel-stack` is the published vertical rhythm — grid with
          `--fs-dashboard-section-gap` between sections. `fs-page` was a class that
          does not exist, so the header and the table card sat flush at 0px. */}
      <section className="fs-panel-stack">
      <DsPageHeader
        action={
          canAdministerPlatform ? (
            <button className="fs-button primary" onClick={handleAddConnection} type="button">
              <Database aria-hidden="true" />
              {t("react.registry.add_connection", "Add connection")}</button>
          ) : null
        }
        description={t("react.registry.manage_workspace_owned_connection_instances_connector_types_come_from_enabled_extensions_while_credentials_and", "Manage workspace-owned connection instances. Connector types come from enabled extensions, while credentials and health live on Core-managed connections.")}
        eyebrow={t("trash.category.registry", "Registry")}
        title={t("react.registry.data_connections", "Data Connections")}
      />

      {message ? (
        <DsAlert
          title={message}
          tone={
            messageTone === "error"
              ? "danger"
              : messageTone === "success"
                ? "success"
                : "neutral"
          }
        />
      ) : null}

      <article className="fs-card fs-table-card">
        <div className="fs-table-toolbar">
          <AdvancedTableFilters
            disabled={connectionsQuery.isLoading}
            fields={filterFields}
            onChange={changeFilter}
            onClear={clearFilters}
            values={{
              workspace: resolvedWorkspaceId,
              health: statusFilter,
              enabled: enabledFilter,
            }}
          />
          <label className="fs-search">
            <Search aria-hidden="true" />
            <input
              aria-label={t("react.registry.search_connections", "Search connections")}
              onChange={(event) => setSearch(event.target.value)}
              placeholder={t("react.registry.name_source_tag", "name, source, tag")}
              type="search"
              value={search}
            />
          </label>
          <span aria-live="polite" className="fs-management-result-announcement">
            {rows.length} {" "}{t("react.registry.of", "of")}{" "}{allRows.length}
          </span>
          <button
            className="fs-button"
            disabled={connectionsQuery.isFetching}
            onClick={() => ignoreAsyncResult(connectionsQuery.refetch())}
            type="button"
          >
            <RefreshCw aria-hidden="true" />
            {t("trash.refresh", "Refresh")}</button>
        </div>
        <DsTable
          ariaLabel={t("react.registry.connection_registry", "Connection registry")}
          busy={connectionsQuery.isLoading}
          columns={connectionColumns}
          rowKey={(row) => String((row as DataConnectionRow).connection_id)}
          rows={rows}
          state={tableState}
        />
      </article>

      <DsDialog
        busy={saving}
        closeLabel={t("react.registry.close_connection_editor", "Close connection editor")}
        description={
          draft.connection_id
            ? t("react.registry.editing_value0_stored_secrets_remain_unchanged_unless_replaced", "Editing \"{value0}\". Stored secrets remain unchanged unless replaced.", { value0: editingConnectionName })
            : t("react.registry.create_a_workspace_owned_connection_and_provide_its_configuration", "Create a workspace-owned connection and provide its configuration.")
        }
        eyebrow={t("trash.category.registry", "Registry")}
        footer={
          <>
            <button className="fs-button" onClick={() => setCreateOpen(false)} type="button">
              {t("extensions.repeater.cancel", "Cancel")}</button>
            <button
              className="fs-button primary"
              disabled={createBlocked}
              form="data-connection-editor-form"
              type="submit"
            >
              {saveButtonLabel(saving, Boolean(draft.connection_id), t)}
            </button>
          </>
        }
        initialFocusRef={workspaceFieldRef}
        onClose={() => setCreateOpen(false)}
        open={createOpen}
        title={draft.connection_id ? t("react.registry.edit_data_connection", "Edit data connection") : t("react.registry.add_data_connection", "Add data connection")}
      >
        <form
          className="fs-dialog-form"
          id="data-connection-editor-form"
          noValidate
          onSubmit={handleCreate}
        >
          {workspacesQuery.isError ||
          (!workspacesQuery.isLoading && !workspaces.length) ? (
            <DsAlert
              title={
                workspacesQuery.isError
                  ? t("react.registry.unable_to_load_workspaces", "Unable to load workspaces.")
                  : t("react.registry.create_a_workspace_before_adding_a_data_connection", "Create a workspace before adding a data connection.")
              }
              tone="warning"
            />
          ) : null}
          {connectorsQuery.isError ||
          (!connectorsQuery.isLoading && !connectorOptions.length) ? (
            <DsAlert
              title={
                connectorsQuery.isError
                  ? t("react.registry.unable_to_load_connectors", "Unable to load connectors.")
                  : t("react.registry.install_an_extension_with_dataset_connectors_before_adding_a_data_connection", "Install an extension with dataset connectors before adding a data connection.")
              }
              tone="warning"
            />
          ) : null}

          <div className="fs-form-grid">
            <div
              className="fs-field full"
              // DsSelect owns its trigger, so the dialog's initial focus is
              // taken from the rendered node; without it the dialog opens
              // focused on its close button.
              ref={(node) => {
                workspaceFieldRef.current =
                  node?.querySelector<HTMLElement>("#data-connection-workspace") ??
                  null;
              }}
            >
              <RequiredLabel>{t("project_mode.workspace", "Workspace")}</RequiredLabel>
              <DsSelect
                ariaLabel={t("project_mode.workspace", "Workspace")}
                id="data-connection-workspace"
                onChange={(workspaceId) => updateDraft({ workspace_id: workspaceId })}
                options={workspaces.map((workspace) => ({
                  value: workspace.id,
                  label: workspace.display_name,
                }))}
                placeholder={t("react.registry.select_workspace", "Select workspace")}
                required
                value={draft.workspace_id}
              />
            </div>
            <div className="fs-field full">
              <RequiredLabel>{t("react.registry.connector", "Connector")}</RequiredLabel>
              <DsSelect
                ariaLabel={t("react.registry.connector", "Connector")}
                onChange={(optionId) => {
                  const option = connectorOptions.find(
                    (item) => item.option_id === optionId,
                  );
                  const connectionValues = Object.fromEntries(
                    schemaFields(option?.connection_schema || {}, {
                      omit: Object.keys(option?.preset_config || {}),
                    })
                      .filter((field) => field.default_value !== undefined)
                      .map((field) => [field.key, draftValue(field.default_value)]),
                  );
                  const secretValues = Object.fromEntries(
                    schemaFields(option?.secret_schema || {})
                      .filter((field) => field.default_value !== undefined)
                      .map((field) => [field.key, draftValue(field.default_value)]),
                  );
                  updateDraft({
                    connector_option_id: optionId,
                    connection_values: connectionValues,
                    // Selecting a connector replaces its form values, including
                    // when the user switches away and returns to the original.
                    touched_connection_fields: schemaFields(option?.connection_schema || {}, {
                      omit: Object.keys(option?.preset_config || {}),
                    }).map((field) => field.key),
                    secret_values: secretValues,
                    display_name: draft.connection_id
                      ? draft.display_name
                      : option
                        ? `${option.label} connection`
                        : draft.display_name,
                  });
                  setAdvancedOpen(false);
                }}
                options={connectorOptions.map((connector) => ({
                  value: connector.option_id,
                  label: connector.label,
                }))}
                placeholder={t("react.registry.select_connector", "Select connector")}
                required
                value={draft.connector_option_id}
              />
            </div>
            <label className="fs-field">
              <RequiredLabel>{t("settings.mcp.fields.name", "Name")}</RequiredLabel>
              <input
                className="fs-input"
                onChange={(event) => updateDraft({ display_name: event.target.value })}
                required
                value={draft.display_name}
              />
            </label>
            <label className="fs-field">
              <span className="fs-field-label">{t("react.registry.environment_variant_9e471951", "Environment")}</span>
              <input
                className="fs-input"
                onChange={(event) => updateDraft({ environment: event.target.value })}
                placeholder="dev"
                value={draft.environment}
              />
            </label>
            <label className="fs-field full">
              <span className="fs-field-label">{t("registry.workflows.simulation.description", "Description")}</span>
              <textarea
                className="fs-textarea"
                onChange={(event) => updateDraft({ description: event.target.value })}
                value={draft.description}
              />
            </label>
            <label className="fs-field full">
              <span className="fs-field-label">{t("registry.tools.field.tags", "Tags")}</span>
              <input
                className="fs-input"
                onChange={(event) => updateDraft({ tags_csv: event.target.value })}
                placeholder={t("react.registry.warehouse_reporting", "warehouse, reporting")}
                value={draft.tags_csv}
              />
            </label>
          </div>

          {connectionFieldGroups.primary.length ? (
            <div className="fs-form-grid">
              {connectionFieldGroups.primary.map(renderSchemaField)}
            </div>
          ) : null}

          {connectionFieldGroups.advanced.length ? (
            <details
              className="fs-settings-section"
              onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
              open={advancedOpen}
            >
              <summary>
                <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
                {t("react.registry.additional_settings", "Additional settings")}{/* Folding the section must not hide that it carries values. */}
                <span className="fs-settings-section-note">
                  {advancedSetCount} {" "}{t("react.registry.of", "of")}{" "}{connectionFieldGroups.advanced.length} {" "}{t("react.registry.set", "set")}</span>
                <span className="fs-disclosure-count">
                  {connectionFieldGroups.advanced.length}
                </span>
              </summary>
              <div className="fs-settings-section-body">
                {advancedClusters.map((cluster) => (
                  <fieldset className="fs-field-group" key={cluster.legend || "unlabelled"}>
                    {cluster.legend ? <legend>{cluster.legend}</legend> : null}
                    <div className="fs-field-group-body">
                      {cluster.fields.map(renderSchemaField)}
                    </div>
                  </fieldset>
                ))}
              </div>
            </details>
          ) : null}

          {blockingField ? (
            // A disabled button with no explanation is its own defect: the
            // operator sees a dead control and no reason. Name the field that is
            // holding it, and say where to find it.
            <DsAlert
              title={t("react.registry.value0_is_required_value1", "{value0} is required.{value1}", { value0: blockingField.label, value1: connectionFieldGroups.advanced.includes(blockingField)
                  ? t("react.registry.required_field_additional_settings_hint", " It is under Additional settings.")
                  : "" })}
              tone="warning"
            />
          ) : null}
        </form>
      </DsDialog>

      <DsDialog
        busy={deleteMutation.isPending}
        closeLabel={t("react.registry.close_delete_confirmation", "Close delete confirmation")}
        description={t("react.registry.delete_data_connection_value0_this_cannot_be_done_while_datasets_depend_on_it", "Delete data connection \"{value0}\"? This cannot be done while datasets depend on it.", { value0: pendingDeleteConnection?.display_name || "" })}
        eyebrow={t("trash.category.registry", "Registry")}
        footer={
          <>
            <button
              className="fs-button"
              disabled={deleteMutation.isPending}
              onClick={() => setPendingDeleteConnection(null)}
              type="button"
            >
              {t("extensions.repeater.cancel", "Cancel")}</button>
            <button
              className="fs-button danger"
              disabled={deleteMutation.isPending}
              onClick={confirmDeleteConnection}
              type="button"
            >
              {deleteMutation.isPending ? t("react.registry.deleting", "Deleting...") : t("react.registry.delete_connection", "Delete connection")}
            </button>
          </>
        }
        onClose={() => setPendingDeleteConnection(null)}
        open={Boolean(pendingDeleteConnection)}
        title={t("react.registry.delete_data_connection", "Delete data connection")}
        variant="confirm"
      />
      </section>
    </PageContainer>
  );
}
