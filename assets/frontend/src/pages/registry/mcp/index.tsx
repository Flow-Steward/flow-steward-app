import { useI18n } from "@/lib/i18n/useI18n";
import { Plus, RefreshCw, Search } from "lucide-react";

import {
  AdvancedTableFilters,
  DsAlert,
  DsCheckbox,
  DsDialog,
  DsPageHeader,
  DsSelect,
  DsTable,
  DsTabs,
  Pagination,
  PageContainer,
} from "@/components/shared/design-system";

import { type McpConnectorDeletePlan } from "@/pages/registry/mcp/api";

import { McpConnectorDeletePlanModal } from "@/pages/registry/mcp/delete-plan-modal";

import { ignoreAsyncResult } from "@/lib/utils/async";

import { McpCatalogPanel } from "@/pages/registry/mcp/mcp-catalog";

import "./mcp-list.css";
import { RESERVED_OAUTH_PARAMETER_IDENTITIES } from "./key-value-rows";
import {
  scopeRows,
  TRANSPORT_OPTIONS,
  renderWhen,
 localizedAUTH_MODE_OPTIONS} from "./mcp-list-page-helpers";
import { KeyValueRepeater } from "./mcp-list-page-components";
import { useMcpListState } from "./mcp-list-state";

export function RegistryMcpPage() {
  const { t } = useI18n();
  const {
    refresh,
    canManageProject,
    projectId,
    healthFilter,
    credentialsFilter,
    filterFields,
    setFilter,
    clearFilters,
    connectorProject,
    openCreate,
    message,
    messageTone,
    searchParams,
    setSearchParams,
    projects,
    projectsQuery,
    setSearch,
    setPage,
    search,
    total,
    connectors,
    connectorsQuery,
    connectorColumns,
    navigate,
    setSort,
    pagedConnectors,
    sort,
    tableState,
    setPerPage,
    page,
    perPage,
    createMutation,
    setCreateOpen,
    createOpen,
    createNameRef,
    createProjectRef,
    submit,
    setDraft,
    draft,
    modalErrorField,
    createEndpointRef,
    createHeadersRef,
    createRequestedScopesRef,
    createAuthorizationParametersRef,
    createRefreshTokenRequiredRef,
    modalError,
    deletePlan,
    deleteMutation,
    deleteError,
    setDeletePlan,
    setDeleteError,
    deleteTriggerLabelRef,
  } = useMcpListState();
  const activeTab = searchParams.get("tab") === "catalog" ? "catalog" : "connectors";
  // Choosing a tab, including the Catalog tab already shown, closes a catalog
  // server's page: the tab is the more visible way back to the list.
  const selectTab = (key: string) => {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("mcp_entry");
    if (key === "catalog") nextParams.set("tab", "catalog");
    else nextParams.delete("tab");
    setSearchParams(nextParams);
  };

  return (
    <PageContainer className="fs-page-sections" width="wide">
      <DsPageHeader
        action={
          <div className="flex flex-wrap items-center justify-end gap-2">
            <button
              className="fs-button secondary"
              onClick={() => ignoreAsyncResult(refresh())}
              type="button"
            >
              <RefreshCw aria-hidden="true" />
              {t("trash.refresh", "Refresh")}</button>
            {renderWhen(canManageProject, (
              <button className="fs-button primary" onClick={openCreate} type="button">
                <Plus aria-hidden="true" />
                {t("react.registry.new_mcp", "New MCP")}</button>
            ))}
          </div>
        }
        description={t("react.registry.register_external_mcp_catalogs_sync_remote_actions_and_publish_selected_tools", "Register external MCP catalogs, sync remote actions, and publish selected tools.")}
        eyebrow={t("trash.category.registry", "Registry")}
        title="MCP"
      />

      {renderWhen(Boolean(message), <DsAlert title={message} tone={messageTone} />)}

      {renderWhen(projectsQuery.isError, <DsAlert title={t("extensions.detail.projects_failed", "Failed to load projects.")} tone="warning" />)}

      {/* Tabs and their panel are one block: as separate page sections the section
          gap opened between the tab row and the panel's top rule. */}
      <div className="fs-settings-tabs">
      <DsTabs
        active={activeTab}
        ariaLabel={t("react.registry.mcp_catalog.sections", "MCP sections")}
        idPrefix="mcp"
        items={[
          { key: "connectors", label: t("react.registry.mcp_catalog.tab_connectors", "Connected") },
          { key: "catalog", label: t("react.registry.mcp_catalog.tab_catalog", "Catalog") },
        ]}
        onSelect={selectTab}
      />

      {activeTab === "catalog" ? (
        <div aria-labelledby="mcp-tab-catalog" className="fs-tabs-panel" id="mcp-panel-catalog" role="tabpanel">
          <McpCatalogPanel
            canManage={canManageProject}
            onInstalled={(result, _item, installedProjectId) => {
              const projectQuery = installedProjectId ? `?project_id=${encodeURIComponent(installedProjectId)}` : "";
              ignoreAsyncResult(navigate(`/registry/mcp/${encodeURIComponent(result.connector.connector_id)}${projectQuery}`));
            }}
            projectId={projectId}
            projects={projects}
          />
        </div>
      ) : (
      <div aria-labelledby="mcp-tab-connectors" className="fs-tabs-panel" id="mcp-panel-connectors" role="tabpanel">
      <article className="fs-card fs-table-card fs-mcp-table">
        <div className="fs-table-toolbar">
          <AdvancedTableFilters
            fields={filterFields}
            onChange={(key, value) => setFilter(key as "project_id" | "health" | "credentials", value)}
            onClear={clearFilters}
            values={{ project_id: projectId, health: healthFilter, credentials: credentialsFilter }}
          />
          <label className="fs-search">
            <Search aria-hidden="true" />
            <input
              aria-label={t("react.registry.search_mcp_connectors_variant_cf424e65", "Search MCP connectors")}
              id="mcp-connector-search"
              name="mcp_connector_search"
              onChange={(event) => {
                setSearch(event.target.value);
                setPage(1);
              }}
              placeholder={t("react.registry.search_mcp_connectors", "Search MCP connectors…")}
              type="search"
              value={search}
            />
          </label>
          {/* The count is announced, not printed: `fs-mcp-count` had no rule
              behind it and the toolbar reads the same way on every other list. */}
          <span aria-live="polite" className="fs-management-result-announcement">
            {search.trim() ? t("react.registry.value0_of_value1_connectors", "{value0} of {value1} connectors", { value0: total, value1: connectors.length }) : t("react.registry.value0_connectors", "{value0} connectors", { value0: total })}
          </span>
        </div>
        <DsTable
          ariaLabel={t("react.registry.mcp_connectors", "MCP connectors")}
          busy={connectorsQuery.isLoading}
          columns={connectorColumns}
          onRowClick={(row) => {
            const rowProject = row.project_id || connectorProject(row.connector_id);
            const projectQuery = rowProject ? `?project_id=${encodeURIComponent(rowProject)}` : "";
            ignoreAsyncResult(navigate(`/registry/mcp/${encodeURIComponent(row.connector_id)}${projectQuery}`));
          }}
          onSortChange={setSort}
          rowKey={(row) => row.connector_id}
          rows={pagedConnectors}
          sort={sort}
          state={tableState}
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
      </div>
      )}
      </div>

      <DsDialog
        busy={createMutation.isPending}
        closeLabel={t("react.registry.close_new_mcp_form", "Close new MCP form")}
        description={t("react.registry.save_the_connector_definition_first_then_sync_remote_tools_and_choose_an_allowlist", "Save the connector definition first, then sync remote tools and choose an allowlist.")}
        eyebrow={t("trash.category.registry", "Registry")}
        footer={
          <>
            <button
              className="fs-button"
              disabled={createMutation.isPending}
              onClick={() => setCreateOpen(false)}
              type="button"
            >
              {t("extensions.repeater.cancel", "Cancel")}</button>
            <button
              className="fs-button primary"
              disabled={createMutation.isPending}
              form="mcp-create-form"
              type="submit"
            >
              {createMutation.isPending ? t("settings.retention.saving", "Saving...") : t("react.registry.save_mcp", "Save MCP")}
            </button>
          </>
        }
        onClose={() => setCreateOpen(false)}
        open={createOpen}
        initialFocusRef={createNameRef}
        title={t("react.registry.create_mcp_connector", "Create MCP connector")}
      >
        <form className="fs-form-grid" id="mcp-create-form" noValidate onSubmit={submit}>
          <div className="fs-field" ref={createProjectRef} tabIndex={-1}>
            <span className="fs-field-label">{t("events.columns.project", "Project")}<span aria-hidden="true" className="fs-required"> *</span></span>
            <DsSelect
              ariaLabel={t("events.columns.project", "Project")}
              onChange={(value) => setDraft((current) => ({ ...current, project_id: value }))}
              options={projects.map((project) => ({
                value: project.id,
                label: project.name || t("react.registry.project_value0", "Project {value0}", { value0: project.id }),
              }))}
              placeholder={t("extensions.detail.select_project", "Select a project")}
              value={draft.project_id}
            />
            <span className="fs-field-helper">{t("react.registry.mcp_connectors_and_oauth_credentials_are_isolated_to_the_selected_project", "MCP connectors and OAuth credentials are isolated to the selected project.")}</span>
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="mcp-create-name">{t("settings.mcp.fields.name", "Name")}<span aria-hidden="true" className="fs-required"> *</span></label>
            <input
              aria-describedby={modalErrorField === "name" ? "mcp-create-error" : undefined}
              aria-invalid={modalErrorField === "name"}
              className="fs-input"
              id="mcp-create-name"
              ref={createNameRef}
              onChange={(event) => setDraft((current) => ({ ...current, display_name: event.target.value }))}
              required
              value={draft.display_name}
            />
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="mcp-create-endpoint">{t("react.registry.endpoint", "Endpoint")}<span aria-hidden="true" className="fs-required"> *</span></label>
            <input
              aria-describedby={modalErrorField === "endpoint" ? "mcp-create-error" : undefined}
              aria-invalid={modalErrorField === "endpoint"}
              className="fs-input"
              id="mcp-create-endpoint"
              ref={createEndpointRef}
              onChange={(event) => setDraft((current) => ({ ...current, base_url: event.target.value }))}
              placeholder="https://example.test/mcp"
              required
              type="url"
              value={draft.base_url}
            />
          </div>
          <div className="fs-field">
            <span className="fs-field-label">{t("settings.mcp.fields.transport", "Transport")}</span>
            <DsSelect
              ariaLabel={t("settings.mcp.fields.transport", "Transport")}
              onChange={(value) => setDraft((current) => ({ ...current, transport: value }))}
              options={TRANSPORT_OPTIONS}
              value={draft.transport}
            />
            <span className="fs-field-helper">
              {t("react.registry.the_current_tool_runtime_supports_streamable_http_mcp_endpoints", "The current tool runtime supports streamable HTTP MCP endpoints.")}</span>
          </div>
          <div className="fs-field">
            <span className="fs-field-label">{t("react.registry.auth_mode", "Auth Mode")}</span>
            <DsSelect
              ariaLabel={t("react.registry.auth_mode", "Auth Mode")}
              onChange={(value) => setDraft((current) => ({ ...current, auth_mode: value }))}
              options={localizedAUTH_MODE_OPTIONS(t)}
              value={draft.auth_mode}
            />
          </div>
          <KeyValueRepeater label={t("react.registry.headers", "Headers")} onChange={(headers) => setDraft((current) => ({ ...current, headers }))} rows={draft.headers} sensitiveValues serverInvalid={modalErrorField === "headers"} wrapperRef={createHeadersRef} />
          <fieldset
            aria-describedby={modalErrorField === "requested_scopes" ? "mcp-create-error" : undefined}
            aria-label={t("react.registry.requested_scopes", "Requested scopes")}
            className={`fs-field full m-0 min-w-0 border-0 p-0${modalErrorField === "requested_scopes" ? " fs-field-error-state" : ""}`}
            ref={createRequestedScopesRef}
            tabIndex={-1}
          >
            <legend className="fs-field-label">{t("react.registry.requested_scopes", "Requested scopes")}</legend>
            {draft.requested_scopes.map((scope, index) => (
              <div className="fs-form-grid fs-mcp-key-value-row" key={scope.id}>
                <input
                  aria-label={t("react.registry.requested_scope_value0", "Requested scope {value0}", { value0: index + 1 })}
                  aria-describedby={modalErrorField === "requested_scopes" ? "mcp-create-error" : undefined}
                  aria-invalid={modalErrorField === "requested_scopes"}
                  className="fs-input"
                  onChange={(event) => setDraft((current) => ({ ...current, requested_scopes: current.requested_scopes.map((item) => item.id === scope.id ? { ...item, value: event.target.value } : item) }))}
                  value={scope.value}
                />
                <button className="fs-button secondary" onClick={() => setDraft((current) => ({ ...current, requested_scopes: current.requested_scopes.filter((item) => item.id !== scope.id) }))} type="button">{t("extensions.repeater.remove", "Remove")}</button>
              </div>
            ))}
            <button className="fs-button secondary" onClick={() => setDraft((current) => ({ ...current, requested_scopes: [...current.requested_scopes, ...scopeRows([""])] }))} type="button">{t("react.registry.add_requested_scope", "Add requested scope")}</button>
          </fieldset>
          <KeyValueRepeater label={t("react.registry.authorization_parameters", "Authorization parameters")} onChange={(authorization_parameters) => setDraft((current) => ({ ...current, authorization_parameters }))} reservedKeys={RESERVED_OAUTH_PARAMETER_IDENTITIES} rows={draft.authorization_parameters} serverInvalid={modalErrorField === "authorization_parameters"} wrapperRef={createAuthorizationParametersRef} />
          <div className="fs-field full" data-testid="mcp-create-authorization-parameters-help">
            <span className="fs-field-helper">{t("react.registry.these_parameters_are_added_to_the_browser_authorization_url_do_not_enter_secrets_or_credentials_here", "These parameters are added to the browser authorization URL. Do not enter secrets or credentials here.")}</span>
          </div>
          <fieldset
            aria-describedby={modalErrorField === "refresh_token_required" ? "mcp-create-error" : undefined}
            aria-label={t("react.registry.refresh_token_required", "Refresh token required")}
            className={`fs-field full m-0 min-w-0 border-0 p-0${modalErrorField === "refresh_token_required" ? " fs-field-error-state" : ""}`}
            ref={createRefreshTokenRequiredRef}
            tabIndex={-1}
          >
            <DsCheckbox
              checked={draft.refresh_token_required}
              onChange={(checked) => setDraft((current) => ({ ...current, refresh_token_required: checked }))}
            >
              {t("react.registry.require_a_refresh_token_for_oauth_completion", "Require a refresh token for OAuth completion.")}</DsCheckbox>
          </fieldset>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="mcp-create-namespace">{t("react.registry.namespace_prefix", "Namespace Prefix")}</label>
            <input
              className="fs-input"
              id="mcp-create-namespace"
              onChange={(event) => setDraft((current) => ({ ...current, namespace_prefix: event.target.value }))}
              placeholder={t("react.registry.catalog", "catalog")}
              value={draft.namespace_prefix}
            />
          </div>
          <div className="fs-field">
            <span className="fs-field-label">{t("settings.mcp.status.enabled", "Enabled")}</span>
            <DsCheckbox
              checked={draft.enabled}
              onChange={(checked) => setDraft((current) => ({ ...current, enabled: checked }))}
            >
              {t("react.registry.connector_can_be_used_after_it_is_saved", "Connector can be used after it is saved.")}</DsCheckbox>
          </div>
          <div className="fs-field full">
            <label className="fs-field-label" htmlFor="mcp-create-description">{t("registry.workflows.simulation.description", "Description")}</label>
            <textarea
              className="fs-textarea"
              id="mcp-create-description"
              onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))}
              value={draft.description}
            />
          </div>

          {renderWhen(Boolean(modalError), (
            <div id="mcp-create-error">
              <DsAlert title={modalError} tone="danger" />
            </div>
          ))}
        </form>
      </DsDialog>

      {renderWhen(Boolean(deletePlan), (
        <McpConnectorDeletePlanModal
          busy={deleteMutation.isPending}
          error={deleteError}
          key={`${deletePlan?.connector_id}:${deletePlan?.revision}`}
          onClose={() => {
            if (!deleteMutation.isPending) {
              setDeletePlan(null);
              setDeleteError("");
              const label = deleteTriggerLabelRef.current;
              deleteTriggerLabelRef.current = "";
              globalThis.setTimeout(() => {
                globalThis.document
                  ?.querySelector<HTMLElement>(`[aria-label="${label}"]`)
                  ?.focus();
              }, 0);
            }
          }}
          onConfirm={() =>
            deleteMutation.mutate({
              connectorId: deletePlan?.connector_id || "",
              revision: deletePlan?.revision || "",
            })
          }
          plan={deletePlan as McpConnectorDeletePlan}
        />
      ))}
    </PageContainer>
  );
}
