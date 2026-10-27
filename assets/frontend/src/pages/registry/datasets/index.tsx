import { useI18n } from "@/lib/i18n/useI18n";
import { Plus, RefreshCw } from "lucide-react";

import { ResourceSelectionList } from "./resource-selection-list";

import { AssignmentSelector } from "@/components/shared/assignment-selector";

import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

import { ignoreAsyncResult } from "@/lib/utils/async";

import {
  AdvancedTableFilters,
  DsAlert,
  DsCheckbox,
  DsDialog,
  DsPageHeader,
  DsSelect,
  DsTable,
  DsValueChip,
  PageContainer,
  RequiredLabel,
  Search,
} from "@/components/shared/design-system";

import {
  CREATE_FORM_ID,
  connectionSchemaSnapshot,
  stringValue,
  registryLoadMessage,
} from "./dataset-page-helpers";
import { useDatasetsState } from "./datasets-state";

export function RegistryDatasetsPage() {
  const { t } = useI18n();
  const {
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
    introspectCreateConnection,
    connectorsQuery,
    connectors,
    updateDraft,
    changeCreateContext,
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
    introspectionError,
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
  } = useDatasetsState();

  return (
    <PageContainer width="wide">
      <section className="fs-panel-stack">
      <DsPageHeader
        action={
          <>
            <button
              className="fs-button"
              onClick={() => ignoreAsyncResult(refreshDatasets())}
              type="button"
            >
              <RefreshCw aria-hidden="true" />
              {t("trash.refresh", "Refresh")}</button>
            {canAdministerPlatform ? (
              <button className="fs-button primary" onClick={openCreate} type="button">
                <Plus aria-hidden="true" />
                {t("react.registry.new_dataset", "New Dataset")}</button>
            ) : null}
          </>
        }
        description={t("react.registry.define_reusable_data_sources_from_extension_provided_dataset_connectors_then_link_them_to_projects", "Define reusable data sources from extension-provided dataset connectors, then link them to projects.")}
        eyebrow={t("trash.category.registry", "Registry")}
        title={t("react.registry.datasets", "Datasets")}
      />

      {loadError ? (
        <DsAlert title={t("react.registry.unable_to_load_registry_data", "Unable to load registry data.")} tone="danger">
          {registryLoadMessage(loadError, t("react.registry.unable_to_load_registry_data", "Unable to load registry data."))}
        </DsAlert>
      ) : null}
      {message ? <DsAlert title={message} tone="success" /> : null}

      <article className="fs-card fs-table-card">
        <div className="fs-table-toolbar">
          <AdvancedTableFilters
            disabled={datasetsQuery.isLoading}
            fields={filterFields}
            onChange={(key, value) => setFilters((current) => ({ ...current, [key]: value }))}
            onClear={() => setFilters({})}
            values={filters}
          />
          {/* `.fs-search` carries `margin-left: auto`, so the filters first and
              the search second is all the alignment this needs. */}
          <label className="fs-search">
            <Search aria-hidden="true" />
            <input
              aria-label={t("react.registry.search_datasets", "Search datasets")}
              onChange={(event) => setSearch(event.target.value)}
              placeholder={t("react.registry.filter_datasets_variant_6402a336", "Filter datasets…")}
              type="search"
              value={search}
            />
          </label>
          <span aria-live="polite" className="fs-management-result-announcement">
            {visibleRows.length} {" "}{t("react.registry.of", "of")}{" "}{rows.length}
          </span>
        </div>
        <DsTable
          ariaLabel={t("react.registry.datasets_catalog", "Datasets catalog")}
          busy={datasetsQuery.isLoading}
          columns={datasetColumns}
          onRowClick={openDataset}
          rowKey={(row) => stringValue(row.dataset_id)}
          rows={visibleRows}
          state={tableState}
        />
      </article>

      <DsDialog
        busy={saving}
        closeLabel={t("react.registry.close_create_form", "Close create form")}
        description={t("react.registry.dataset_id_is_generated_automatically_you_can_assign_projects_here_now_and_update_them_later_from_either_the_d", "Dataset ID is generated automatically. You can assign projects here now and update them later from either the dataset or project side.")}
        eyebrow={t("trash.category.registry", "Registry")}
        footer={
          <>
            <button className="fs-button" onClick={closeCreate} type="button">
              {t("extensions.repeater.cancel", "Cancel")}</button>
            <button
              className="fs-button primary"
              disabled={saving}
              form={CREATE_FORM_ID}
              type="submit"
            >
              {saving ? t("react.registry.creating", "Creating...") : t("react.registry.create_dataset", "Create dataset")}
            </button>
          </>
        }
        initialFocusRef={displayNameRef}
        onClose={closeCreate}
        open={createOpen && canAdministerPlatform}
        title={t("react.registry.create_dataset", "Create dataset")}
      >
        <form className="fs-dialog-form" id={CREATE_FORM_ID} noValidate onSubmit={handleCreate}>
          {connectorsQuery.isError || (!connectorsQuery.isLoading && !connectors.length) ? (
            <DsAlert
              title={
                connectorsQuery.isError
                  ? registryLoadMessage(connectorsQuery.error, t("react.registry.unable_to_load_connectors", "Unable to load connectors."))
                  : t("react.registry.no_dataset_connectors_are_installed", "No dataset connectors are installed")
              }
              tone="warning"
            >
              {connectorsQuery.isError
                ? null
                : t("react.registry.install_an_extension_that_exposes_dataset_connectors_before_creating_one", "Install an extension that exposes dataset connectors before creating one.")}
            </DsAlert>
          ) : null}

          <div className="fs-field full">
            <label className="fs-field-label" htmlFor="dataset-display-name">
              <RequiredLabel>{t("users.modal.display_name", "Display Name")}</RequiredLabel>
            </label>
            <input
              aria-label={t("users.modal.display_name", "Display Name")}
              className="fs-input"
              id="dataset-display-name"
              onChange={(event) => updateDraft({ display_name: event.target.value })}
              ref={displayNameRef}
              required
              value={draft.display_name}
            />
          </div>

          <div className="fs-field full">
            <label className="fs-field-label" htmlFor="dataset-description">
              {t("registry.workflows.simulation.description", "Description")}</label>
            <textarea
              className="fs-textarea"
              id="dataset-description"
              onChange={(event) => updateDraft({ description: event.target.value })}
              value={draft.description}
            />
          </div>

          <div className="fs-form-grid">
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="dataset-workspace">
                {t("react.registry.workspace_owner", "Workspace owner")}</label>
              <DsSelect
                ariaLabel={t("react.registry.workspace_owner", "Workspace owner")}
                id="dataset-workspace"
                onChange={(workspaceId) => {
                  const nextConnection = dataConnections.find(
                    (connection) =>
                      connection.query_capable &&
                      firstStringFromUnknownValue([connection.workspace_id, ""]).trim() === workspaceId,
                  );
                  changeCreateContext({
                    workspace_id: workspaceId,
                    connection_id: nextConnection?.connection_id || "",
                    project_ids: [],
                  });
                  setSchemaSnapshot(connectionSchemaSnapshot(nextConnection));
                  setSelectedResources({});
                  setResourceSearch("");
                }}
                options={[
                  { value: "", label: t("react.registry.select_workspace", "Select workspace") },
                  ...workspaces.map((workspace) => ({
                    value: stringValue(workspace.id),
                    label: stringValue(workspace.display_name),
                  })),
                ]}
                value={draft.workspace_id}
              />
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="dataset-availability">
                {t("react.registry.availability", "Availability")}</label>
              <DsSelect
                ariaLabel={t("react.registry.availability", "Availability")}
                id="dataset-availability"
                onChange={(value) =>
                  updateDraft({
                    availability_mode:
                      value === "all_workspace_projects"
                        ? "all_workspace_projects"
                        : "specific_projects_only",
                  })
                }
                options={[
                  { value: "all_workspace_projects", label: t("react.registry.all_workspace_projects", "All workspace projects") },
                  { value: "specific_projects_only", label: t("react.registry.specific_projects_only", "Specific projects only") },
                ]}
                value={draft.availability_mode}
              />
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="dataset-connection">
                {t("react.registry.data_connection", "Data Connection")}</label>
              <DsSelect
                ariaLabel={t("react.registry.data_connection", "Data Connection")}
                id="dataset-connection"
                onChange={(connectionId) => {
                  const connection = workspaceConnections.find(
                    (item) => item.connection_id === connectionId,
                  );
                  changeCreateContext({ connection_id: connectionId });
                  setSchemaSnapshot(connectionSchemaSnapshot(connection));
                  setSelectedResources({});
                  setResourceSearch("");
                }}
                options={[
                  { value: "", label: t("react.registry.select_data_connection", "Select data connection") },
                  ...workspaceConnections.map((connection) => ({
                    value: stringValue(connection.connection_id),
                    label: `${stringValue(connection.display_name)} (${connection.health_status || "unknown"})`,
                  })),
                ]}
                value={draft.connection_id}
              />
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="dataset-tags">
                {t("registry.tools.field.tags", "Tags")}</label>
              <input
                className="fs-input"
                id="dataset-tags"
                onChange={(event) => updateDraft({ tags_csv: event.target.value })}
                placeholder="orders, reporting"
                value={draft.tags_csv}
              />
            </div>
          </div>

          {selectedConnector ? (
            <DsAlert title={selectedConnector.display_name} tone="neutral">
              {selectedConnector.description || t("react.registry.extension_provided_dataset_connector", "Extension-provided dataset connector.")}
            </DsAlert>
          ) : null}
          {/* The chip row lives on a plain wrapper, not on `.fs-field`:
              design-system.css is unlayered, so it beats Tailwind's layered
              utilities and `.fs-field`'s `display: grid` would drop the `flex`
              outright, stacking the chips one per line. */}
          {selectedConnector ? (
            <div className="fs-field full">
              <div className="flex flex-wrap gap-2">
                <DsValueChip>{t("react.registry.query_variant_9e009c3c", "query:")}{" "}{selectedConnector.query_language}</DsValueChip>
                <DsValueChip>{t("react.registry.runtime", "runtime:")}{" "}{selectedConnector.runtime_kind}</DsValueChip>
                <DsValueChip>
                  {t("react.registry.id", "id:")}{" "}{selectedConnector.local_connector_id || selectedConnector.connector_id}
                </DsValueChip>
              </div>
            </div>
          ) : null}
          {selectedConnection ? (
            <DsAlert title={selectedConnection.display_name} tone="success">
              {t("react.registry.source_variant_26ffdce3", "source:")}{" "}{selectedConnection.source_kind || t("react.registry.extension", "extension")}{" "}{t("react.registry.environment", "· environment:")}{" "}
              {selectedConnection.environment || "dev"}
            </DsAlert>
          ) : null}

          {draft.availability_mode === "specific_projects_only" ? (
            <div className="fs-field full">
              <span className="fs-field-label">{t("react.registry.project_availability", "Project availability")}</span>
              <span className="fs-field-helper">
                {t("react.registry.select_the_projects_in_this_workspace_that_should_be_able_to_use_the_dataset", "Select the projects in this workspace that should be able to use the dataset.")}</span>
              <AssignmentSelector
                activeFilter={projectFilter}
                emptyState={t("react.registry.display_no_workspace_projects", "No projects are available in this workspace.")}
                filters={projectFilterItems}
                loading={workspaceProjectsQuery.isLoading}
                loadingLabel={t("react.registry.loading_workspace_projects", "Loading workspace projects...")}
                onFilterChange={setProjectFilter}
                onSearchChange={setProjectSearch}
                onToggleRow={toggleProjectAssignment}
                rows={projectSelectorRows}
                searchPlaceholder={t("react.registry.project_name_or_id", "Project name or ID")}
                searchValue={projectSearch}
                selectedCount={draft.project_ids.length}
                totalCount={projectSelectorRows.length}
              />
            </div>
          ) : null}

          <div className="fs-field full">
            <div className="fs-footer-actions">
              <span className="fs-field-label">{t("react.registry.schema_resources", "Schema resources")}</span>
              <button
                className="fs-button"
                disabled={!selectedConnection || introspectMutation.isPending}
                onClick={() =>
                  selectedConnection && introspectCreateConnection(selectedConnection.connection_id)
                }
                type="button"
              >
                {introspectMutation.isPending ? t("react.registry.loading_schema", "Loading schema...") : t("react.registry.introspect_schema", "Introspect schema")}
              </button>
            </div>
            <span className="fs-field-helper">
              {t("react.registry.start_with_an_empty_selection_then_choose_only_the_tables_and_columns_you_want_to_expose", "Start with an empty selection, then choose only the tables and columns you want to expose.")}</span>
            {introspectionError ? <DsAlert title={introspectionError} tone="danger" /> : null}
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
                <strong>{t("react.registry.no_schema_loaded_yet", "No schema loaded yet")}</strong>
                <span>{t("react.registry.introspect_the_selected_connection_to_load_available_resources", "Introspect the selected connection to load available resources.")}</span>
              </div>
            )}
            <span className="fs-field-helper">{t("react.registry.selected_resources", "Selected resources:")}{" "}{selectedResourceCount || 0}</span>
          </div>

          <div className="fs-field full">
            <div className="flex flex-wrap gap-4">
              <DsCheckbox
                checked={draft.read_only}
                onChange={(checked) => updateDraft({ read_only: checked })}
              >
                {t("settings.access.service_accounts.form.level.read", "Read only")}</DsCheckbox>
              <DsCheckbox
                checked={draft.enabled}
                onChange={(checked) => updateDraft({ enabled: checked })}
              >
                {t("settings.mcp.status.enabled", "Enabled")}</DsCheckbox>
            </div>
          </div>

          {/* `role="alert"` comes from the tone — it is not written here. */}
          {createError ? <DsAlert title={createError} tone="danger" /> : null}
        </form>
      </DsDialog>

      <DsDialog
        busy={deleteMutation.isPending}
        description={t("react.registry.the_dataset_is_removed_from_this_registry_and_can_be_restored_from_trash_anything_already_referencing_it_keeps", "The dataset is removed from this registry and can be restored from Trash. Anything already referencing it keeps its recorded copy.")}
        eyebrow={t("trash.category.registry", "Registry")}
        footer={
          <>
            <button
              className="fs-button"
              disabled={deleteMutation.isPending}
              onClick={() => {
                setDeleteTarget("");
                focusRowTrigger();
              }}
              type="button"
            >
              {t("extensions.repeater.cancel", "Cancel")}</button>
            <button
              className="fs-button danger"
              disabled={deleteMutation.isPending}
              onClick={() => {
                const datasetId = deleteTarget;
                setDeleteTarget("");
                deleteMutation.mutate(datasetId);
                focusRowTrigger();
              }}
              type="button"
            >
              {t("settings.model_providers.delete", "Delete")}</button>
          </>
        }
        onClose={() => {
          setDeleteTarget("");
          focusRowTrigger();
        }}
        open={Boolean(deleteTarget)}
        title={t("react.registry.delete_value0", "Delete {value0}", { value0: deleteTarget || t("react.workflows.dataset", "dataset") })}
        variant="confirm"
      >
        <DsAlert tone="danger" title={t("react.registry.this_moves_value0_to_trash", "This moves {value0} to Trash.", { value0: deleteTarget || t("react.registry.display_the_dataset", "the dataset") })} />
      </DsDialog>
      </section>
    </PageContainer>
  );
}
