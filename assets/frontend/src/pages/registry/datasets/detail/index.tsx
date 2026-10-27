import { useI18n } from "@/lib/i18n/useI18n";
import { ArrowLeft } from "lucide-react";

import { Link } from "react-router";

import { AssignmentSelector } from "@/components/shared/assignment-selector";

import {
  DsAlert,
  DsCheckbox,
  DsDialog,
  DsFormSelect,
  DsPageHeader,
  DsSelect,
  DsTable,
  DsTabs,
  DsValueChip,
  PageContainer,
} from "@/components/shared/design-system";

import { stringifyJson } from "./dataset-detail-helpers";

import { QueryPreviewModal } from "./query-preview-modal";

import { SavedQuerySchemaBuilders } from "./saved-query-schema-builders";

import { localizedTABS} from "./dataset-detail-page-helpers";
import { useDatasetDetailState } from "./dataset-detail-state";

export function RegistryDatasetsDetailPage() {
  const { t } = useI18n();
  const {
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
    selectDetailConnection,
    displayNameRef,
    workspaceConnections,
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
  } = useDatasetDetailState();

  return (
    <PageContainer className="fs-page-sections" width="standard">
      <DsPageHeader
        action={
          <>
            <Link className="fs-button" to="/registry/datasets">
              <ArrowLeft aria-hidden="true" />
              {t("react.registry.back_to_datasets", "Back to datasets")}</Link>
            {/* No identifier chip in the header: the id is in the URL, which is
                where anyone who needs it looks. Matches the pass #40 made on
                Guardrails detail. `linked projects` stays — it is state a reader
                cannot get from anywhere else on this row. */}
            <DsValueChip>{t("react.registry.linked_projects", "linked projects:")}{" "}{linkedProjectIds.length || t("registry.published_blocks.none", "none")}</DsValueChip>
            {!configurationReadOnly ? <button
              className="fs-button"
              disabled={toggleMutation.isPending || !dataset}
              onClick={() => toggleMutation.mutate()}
              type="button"
            >
              {draft.enabled ? t("extensions.detail.disable", "Disable") : t("extensions.detail.enable", "Enable")}
            </button> : null}
          </>
        }
        description={configurationReadOnly ? t("react.registry.inspect_package_dataset", "Inspect package-defined dataset configuration and saved queries.") : t("react.registry.edit_dataset_metadata_and_scope_saved_queries_stay_bound_to_this_dataset", "Edit dataset metadata and scope. Saved queries stay bound to this dataset.")}
        eyebrow={t("trash.category.registry", "Registry")}
        title={dataset?.display_name || datasetId || t("react.registry.dataset", "Dataset")}
      />

      {detailQuery.isError ? (
        <DsAlert title={t("react.registry.failed_to_load_dataset_variant_f39afb1a", "Failed to load dataset")} tone="danger">
          {detailQuery.error instanceof Error ? detailQuery.error.message : t("react.registry.failed_to_load_dataset", "Failed to load dataset.")}
        </DsAlert>
      ) : null}

      <section aria-label={t("react.registry.dataset_detail", "Dataset detail")} className="fs-settings-tabs">
        <DsTabs
          active={activeTab}
          ariaLabel={t("react.registry.dataset_detail_sections", "Dataset detail sections")}
          idPrefix="dataset"
          items={localizedTABS(t)}
          onSelect={goToTab}
        />
        <div
          aria-labelledby={`dataset-tab-${activeTab}`}
          className="fs-tabs-panel"
          id={`dataset-panel-${activeTab}`}
          role="tabpanel"
        >
          <div className="fs-panel-stack">
            {activeTab === "dataset" ? configurationReadOnly ? (
              <>
                <DsAlert title={t("react.registry.package_owned_dataset", "Package-owned dataset")} tone="neutral">
                  <p>{t("react.registry.package_dataset_configuration", "This dataset is provided by an installed extension. Its configuration and schema are defined by the package.")}</p>
                  <p>{t("react.registry.package_dataset_workspace", "It has no permanent workspace. Queries use a compatible data connection selected for the project.")}</p>
                  <p>{t("react.registry.package_dataset_projects", "Open a project, link this dataset in Datasets, and select a compatible data connection in Data Connections.")}</p>
                  <Link className="fs-button" to="/projects">{t("shell.nav.projects", "Projects")}</Link>
                </DsAlert>
                {dataset?.description ? <p>{dataset.description}</p> : null}
                <section aria-label={t("react.registry.package_schema", "Package schema")}>
                  <h2 className="fs-panel-title">{t("react.registry.package_schema", "Package schema")}</h2>
                  <pre className="fs-code-value">{stringifyJson({ schema: dataset?.schema_snapshot_jsonb || {}, scope: dataset?.resource_scope_jsonb || {} })}</pre>
                </section>
                {message ? <DsAlert title={message} tone="neutral" /> : null}
              </>
            ) : (
              <>
                <h2 className="fs-panel-title">{t("react.registry.dataset_settings", "Dataset settings")}</h2>
                <p className="fs-field-helper">
                  {t("react.registry.workspace_ownership_is_explicit_availability_can_be_workspace_wide_or_restricted_to_specific_projects", "Workspace ownership is explicit. Availability can be workspace-wide or restricted to specific projects.")}</p>
                <div className="fs-form-grid">
                  <div className="fs-field full">
                    <label className="fs-field-label" htmlFor="dataset-display-name">
                      {t("users.modal.display_name", "Display Name")}</label>
                    <input
                      className="fs-input"
                      id="dataset-display-name"
                      onChange={(event) => updateDraft({ display_name: event.target.value })}
                      ref={displayNameRef}
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

                  <div className="fs-field">
                    <label className="fs-field-label" htmlFor="dataset-workspace">
                      {t("react.registry.workspace_owner", "Workspace owner")}</label>
                    {/* `readOnly`, never `disabled`: the value stays in the
                        accessibility tree and in the form, and
                        `.fs-input[readonly]` already styles it. */}
                    <input
                      className="fs-input"
                      id="dataset-workspace"
                      readOnly
                      value={draft.workspace_id || "Unresolved"}
                    />
                    <span className="fs-field-helper">
                      {t("react.registry.ownership_is_fixed_for_the_life_of_the_dataset", "Ownership is fixed for the life of the dataset.")}</span>
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
                      onChange={selectDetailConnection}
                      options={[
                        { value: "", label: t("react.registry.select_data_connection", "Select data connection") },
                        ...workspaceConnections.map((connection) => ({
                          value: connection.connection_id,
                          label: `${connection.display_name} (${connection.health_status || "unknown"})`,
                        })),
                      ]}
                      value={draft.connection_id}
                    />
                  </div>

                  {/* a60c0815a: a dataset whose stored connection is no longer
                      query-capable resolves to nothing here, and saving it
                      would bind it to an empty connection. The warning is the
                      published DsAlert rather than the amber Tailwind panel it
                      shipped as. */}
                  {!dataConnectionsQuery.isLoading && dataset && !selectedConnection ? (
                    <DsAlert
                      title={t("react.registry.this_dataset_needs_to_be_rebound_to_a_query_capable_data_connection", "This dataset needs to be rebound to a query-capable data connection.")}
                      tone="warning"
                    />
                  ) : null}
                  {selectedConnector ? (
                    <div className="fs-field full">
                      <DsAlert title={selectedConnector.display_name} tone="neutral">
                        {selectedConnector.description || t("react.registry.extension_provided_dataset_connector", "Extension-provided dataset connector.")}
                      </DsAlert>
                      <div className="flex flex-wrap gap-2">
                        <DsValueChip>{t("react.registry.query_variant_9e009c3c", "query:")}{" "}{selectedConnector.query_language}</DsValueChip>
                        <DsValueChip>{t("react.registry.runtime", "runtime:")}{" "}{selectedConnector.runtime_kind}</DsValueChip>
                        <DsValueChip>{t("react.registry.connector_variant_ff9022d6", "connector:")}{" "}
                          {selectedConnector.local_connector_id || selectedConnector.connector_id}
                        </DsValueChip>
                      </div>
                    </div>
                  ) : null}
                  {selectedConnection ? (
                    <div className="fs-field full">
                      <DsAlert title={selectedConnection.display_name} tone="success">
                        {t("react.registry.source_variant_26ffdce3", "source:")}{" "}{selectedConnection.source_kind || t("react.registry.extension", "extension")}{" "}{t("react.registry.environment", "· environment:")}{" "}
                        {selectedConnection.environment || "dev"}
                      </DsAlert>
                    </div>
                  ) : null}

                  {resourceBlock}

                  <div className="fs-field full">
                    <span className="fs-field-label">{t("react.registry.project_availability", "Project availability")}</span>
                    <span className="fs-field-helper">
                      {draft.availability_mode === "all_workspace_projects"
                        ? t("react.registry.this_dataset_is_available_to_all_current_and_future_projects_in_the_same_workspace", "This dataset is available to all current and future projects in the same workspace.")
                        : t("react.registry.choose_the_specific_projects_in_this_workspace_that_should_see_this_dataset", "Choose the specific projects in this workspace that should see this dataset.")}
                    </span>
                    {draft.availability_mode === "all_workspace_projects" ? (
                      <DsAlert title={t("react.registry.available_workspace_wide", "Available workspace-wide")} tone="neutral">
                        {t("react.registry.all_workspace_projects_can_use_this_dataset_explicit_per_project_links_are_not_required", "All workspace projects can use this dataset. Explicit per-project links are not required.")}</DsAlert>
                    ) : (
                      <AssignmentSelector
                        activeFilter={projectFilter}
                        emptyState={t("react.registry.display_no_workspace_projects", "No projects are available in this workspace.")}
                        filters={projectFilterItems}
                        loading={projectCatalogQuery.isLoading}
                        loadingLabel={t("react.registry.loading_workspace_projects", "Loading workspace projects...")}
                        onFilterChange={setProjectFilter}
                        onSave={() => saveAssignmentsMutation.mutate()}
                        onSearchChange={setProjectSearch}
                        onToggleRow={toggleProjectAssignment}
                        rows={projectSelectorRows}
                        saveLabel={t("react.registry.display_save_project_links", "Save project links")}
                        saving={saveAssignmentsMutation.isPending}
                        searchPlaceholder={t("react.registry.project_name_or_id", "Project name or ID")}
                        searchValue={projectSearch}
                        selectedCount={selectedProjectIds.length}
                        totalCount={projectSelectorRows.length}
                      />
                    )}
                  </div>

                  <div className="fs-field">
                    <label className="fs-field-label" htmlFor="dataset-tags">
                      {t("registry.tools.field.tags", "Tags")}</label>
                    <input
                      className="fs-input"
                      id="dataset-tags"
                      onChange={(event) => updateDraft({ tags_csv: event.target.value })}
                      value={draft.tags_csv}
                    />
                  </div>

                  {/* The row lives on a plain wrapper, not on `.fs-field`:
                      design-system.css is unlayered, so it beats Tailwind's
                      layered utilities and `.fs-field`'s `display: grid` would
                      drop the `flex` outright, stacking the two boxes. */}
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
                </div>

                <div className="fs-footer-actions">
                  <button
                    className="fs-button primary"
                    disabled={saveMutation.isPending || !datasetId || !selectedResourceCount}
                    onClick={handleSave}
                    type="button"
                  >
                    {saveMutation.isPending ? t("settings.retention.saving", "Saving...") : t("react.registry.save_dataset", "Save dataset")}
                  </button>
                  {/* A completed save used to be a chip in the page header. */}
                  {message ? <DsAlert title={message} tone="success" /> : null}
                </div>
              </>
            ) : (
              <>
                <h2 className="fs-panel-title">{t("react.registry.saved_queries", "Saved queries")}</h2>
                <p className="fs-field-helper">
                  {t("react.registry.saved_queries_belong_to_this_dataset_and_are_referenced_by_dataset_query_workflow_steps", "Saved queries belong to this dataset and are referenced by dataset_query workflow steps.")}</p>
                <div className="fs-footer-actions">
                  <button className="fs-button primary" onClick={openCreateQueryModal} type="button">
                    {t("react.registry.add_query", "Add query")}</button>
                  {message ? <DsAlert title={message} tone="success" /> : null}
                </div>
                <DsTable
                  ariaLabel={t("react.registry.saved_queries", "Saved queries")}
                  busy={savedQueriesQuery.isLoading}
                  columns={savedQueryColumns}
                  rowKey={(row) => row.query_template_id}
                  rows={savedQueries}
                  state={
                    savedQueries.length ? null : (
                      <div className="fs-table-state">
                        <strong>{t("react.registry.no_saved_queries_yet", "No saved queries yet")}</strong>
                        <span>
                          {t("react.registry.add_one_to_reference_this_dataset_from_a_dataset_query_workflow_step", "Add one to reference this dataset from a dataset_query workflow step.")}</span>
                      </div>
                    )
                  }
                />
              </>
            )}
          </div>
        </div>
      </section>

      {previewState ? (
        <QueryPreviewModal onClose={() => setPreviewState(null)} previewState={previewState} />
      ) : null}

      <DsDialog
        busy={previewQueryMutation.isPending || validateQueryMutation.isPending}
        description={t("react.registry.enter_values_for_the_named_parameters_they_are_not_saved_with_the_query", "Enter values for the named parameters. They are not saved with the query.")}
        eyebrow={previewDraft?.mode === "validate" ? t("react.registry.validate_query", "Validate query") : t("react.registry.preview_query", "Preview query")}
        footer={
          <>
            <button className="fs-button" onClick={() => setPreviewDraft(null)} type="button">
              {t("extensions.repeater.cancel", "Cancel")}</button>
            <button
              className="fs-button primary"
              disabled={previewQueryMutation.isPending || validateQueryMutation.isPending || !previewProjectReady}
              onClick={runPreviewQuery}
              type="button"
            >
              {previewDraft?.mode === "validate" ? t("react.registry.run_validation", "Run validation") : t("react.registry.run_preview", "Run preview")}
            </button>
          </>
        }
        initialFocusRef={previewParamsRef}
        onClose={() => setPreviewDraft(null)}
        open={Boolean(previewDraft)}
        title={previewDraft?.query.display_name || previewDraft?.query.query_template_id || t("react.registry.query", "Query")}
      >
        <div className="fs-dialog-form">
          {previewDraft?.error ? <DsAlert title={previewDraft.error} tone="danger" /> : null}
          {previewRequiresProject ? (
            <div className="fs-field full">
              <span className="fs-field-label">{t("events.columns.project", "Project")}<span aria-hidden="true" className="fs-required"> *</span></span>
              <DsFormSelect
                ariaLabel={t("events.columns.project", "Project")}
                name="saved_query_project_id"
                value={previewDraft?.projectId || ""}
                onChange={(projectId) => setPreviewDraft((current) => current ? { ...current, projectId, error: "" } : current)}
                options={previewProjectOptions}
                placeholder={t("extensions.detail.select_project", "Select a project")}
                disabled={previewQueryMutation.isPending || validateQueryMutation.isPending || !projectCatalogQuery.isSuccess || !previewProjectOptions.length}
              />
              {projectCatalogQuery.isError ? <DsAlert title={t("extensions.detail.projects_failed", "Failed to load projects.")} tone="danger" /> : null}
              {projectCatalogQuery.isSuccess && !previewProjectOptions.length ? (
                <DsAlert title={t("react.registry.link_dataset_to_project_before_query", "Link this dataset to a project from the project's Datasets page before running the query.")} tone="neutral">
                  <Link className="fs-link" to="/projects">{t("shell.nav.projects", "Projects")}</Link>
                </DsAlert>
              ) : null}
              {projectCatalogQuery.isSuccess && previewProjectOptions.length > 0 && !previewProjectReady ? <span className="fs-field-helper">{t("react.registry.choose_a_project", "Choose a project.")}</span> : null}
            </div>
          ) : null}
          <div className="fs-field full">
            <label className="fs-field-label" htmlFor="saved-query-preview-parameters">
              {previewDraft?.mode === "validate" ? t("react.registry.validation", "Validation") : t("registry.workflow_detail.run_lab.tab.preview", "Preview")} {" "}{t("react.registry.parameters", "parameters")}</label>
            <span className="fs-field-helper">
              {t("react.registry.use_the_parameter_name_without_the_colon", "Use the parameter name without the colon:")}{" "}
              <code className="fs-code-value">{'{"event":"purchase"}'}</code> {" "}{t("react.registry.supplies", "supplies")}{" "}
              <code className="fs-code-value">:event</code>.
            </span>
            <textarea
              className="fs-textarea fs-code-value"
              id="saved-query-preview-parameters"
              onChange={(event) =>
                setPreviewDraft((current) =>
                  current ? { ...current, error: "", paramsJson: event.target.value } : current,
                )
              }
              ref={previewParamsRef}
              value={previewDraft?.paramsJson || ""}
            />
          </div>
        </div>
      </DsDialog>

      <DsDialog
        busy={saveQueryMutation.isPending}
        description={t("react.registry.query_sql_is_saved_on_this_dataset_and_can_be_referenced_by_dataset_query_workflow_steps", "Query SQL is saved on this dataset and can be referenced by dataset_query workflow steps.")}
        eyebrow={t("react.registry.saved_queries", "Saved queries")}
        footer={
          <>
            <button className="fs-button" onClick={closeQueryModal} type="button">
              {t("extensions.repeater.cancel", "Cancel")}</button>
            {!queryConfigurationReadOnly ? <button
              className="fs-button primary"
              disabled={saveQueryMutation.isPending}
              onClick={handleSaveSavedQuery}
              type="button"
            >
              {saveQueryMutation.isPending ? t("settings.retention.saving", "Saving...") : t("react.registry.save_query", "Save query")}
            </button> : null}
          </>
        }
        initialFocusRef={queryNameRef}
        onClose={closeQueryModal}
        open={queryModalOpen}
        title={queryConfigurationReadOnly ? t("react.registry.view_query", "View query") : editingQueryId ? t("react.registry.edit_query", "Edit query") : t("react.registry.add_query", "Add query")}
      >
        <div className="fs-dialog-form">
          {queryFormError ? <DsAlert title={queryFormError} tone="danger" /> : null}
          <div className="fs-field full">
            <label className="fs-field-label" htmlFor="saved-query-name">
              {t("react.registry.query_name", "Query name")}</label>
            <input
              className="fs-input"
              id="saved-query-name"
              readOnly={queryConfigurationReadOnly}
              onChange={(event) => updateQueryDraft({ display_name: event.target.value })}
              placeholder={t("react.registry.manual_e_g_recent_orders", "e.g. Recent orders")}
              ref={queryNameRef}
              value={queryDraft.display_name}
            />
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="saved-query-language">
              {t("react.registry.query_language", "Query language")}</label>
            <DsSelect
              ariaLabel={t("react.registry.query_language", "Query language")}
              id="saved-query-language"
              disabled={queryConfigurationReadOnly}
              onChange={(value) => updateQueryDraft({ query_language: value })}
              options={[{ value: "sql", label: "SQL" }]}
              value={queryDraft.query_language}
            />
          </div>
          <div className="fs-field full">
            <label className="fs-field-label" htmlFor="saved-query-sql">
              {t("react.registry.sql_query", "SQL query")}</label>
            <textarea
              aria-describedby="saved-query-sql-help"
              className="fs-textarea fs-code-value"
              id="saved-query-sql"
              readOnly={queryConfigurationReadOnly}
              onChange={(event) => updateQueryDraft({ sql: event.target.value })}
              placeholder={"SELECT event, eventStart FROM event\nWHERE eventStart >= :from_date\nLIMIT 100"}
              value={queryDraft.sql}
            />
            <span className="fs-field-helper" id="saved-query-sql-help">
              {t("react.registry.use_name_in_sql_for_a_named_parameter", "Use :name in SQL for a named parameter.")}</span>
            <code className="fs-code-value">WHERE eventStart &gt;= :from_date</code>
          </div>
          <div className="fs-field full">
            {queryConfigurationReadOnly ? <pre className="fs-code-value">{stringifyJson({ params: queryParamsSchema, output: queryOutputSchema })}</pre> : <SavedQuerySchemaBuilders
              onOutputSchemaChange={(schema) => updateQueryDraft({ output_json: stringifyJson(schema) })}
              onParamsSchemaChange={(schema) => updateQueryDraft({ params_json: stringifyJson(schema) })}
              outputSchema={queryOutputSchema}
              paramsSchema={queryParamsSchema}
              resources={schemaSnapshot.resources}
              selectAllOutputByDefault={!editingQueryId}
              sql={queryDraft.sql}
            />}
          </div>
        </div>
      </DsDialog>

      <DsDialog
        busy={deleteQueryMutation.isPending}
        description={t("react.registry.the_saved_query_is_removed_from_this_dataset_workflow_steps_referencing_it_will_stop_resolving", "The saved query is removed from this dataset. Workflow steps referencing it will stop resolving.")}
        eyebrow={t("react.registry.saved_queries", "Saved queries")}
        footer={
          <>
            <button
              className="fs-button"
              disabled={deleteQueryMutation.isPending}
              onClick={() => {
                setDeleteQueryTarget(null);
                focusRowTrigger();
              }}
              type="button"
            >
              {t("extensions.repeater.cancel", "Cancel")}</button>
            <button
              className="fs-button danger"
              disabled={deleteQueryMutation.isPending}
              onClick={() => {
                const target = deleteQueryTarget;
                setDeleteQueryTarget(null);
                if (target) {
                  deleteQueryMutation.mutate(target.query_template_id);
                }
                focusRowTrigger();
              }}
              type="button"
            >
              {t("settings.model_providers.delete", "Delete")}</button>
          </>
        }
        onClose={() => {
          setDeleteQueryTarget(null);
          focusRowTrigger();
        }}
        open={Boolean(deleteQueryTarget)}
        title={t("react.registry.delete_saved_query_value0", "Delete saved query \"{value0}\"", { value0: deleteQueryTarget?.display_name || "" })}
        variant="confirm"
      >
        <DsAlert
          tone="danger"
          title={t("react.registry.this_permanently_removes_value0", "This permanently removes {value0}.", { value0: deleteQueryTarget?.display_name || t("react.registry.display_the_query", "the query") })}
        />
      </DsDialog>
    </PageContainer>
  );
}
