import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { Dispatch, ReactNode, SetStateAction, SyntheticEvent } from "react";
import { DsAlert, DsFormSelect, DsSelect, DsTabs } from "@/components/shared/design-system";

import { AssignmentSelector, type AssignmentSelectorRow } from "@/components/shared/assignment-selector";
import {
  textOrEmpty,
  type AgentCatalogMode,
  type AgentOverrideDraft,
  type DataConnectionStatusFilter,
  type Entity,
  type ProjectEditNotice,
  type ProjectTabKey,
  type ProviderOption,
  type WorkflowStatusFilter,
} from "@/pages/projects/detail/project-edit-helpers";
import { ProjectHistoryPanel } from "@/pages/projects/history";
import { ProjectNotificationOverview } from "@/pages/projects/notifications/overview";
import { ProjectPolicyEditor } from "@/pages/projects/policy";
import { ProjectConnectionsEditor } from "@/pages/projects/connections";
import { ProjectDatasetsEditor } from "@/pages/projects/datasets";
import type { ModelOption } from "@/lib/utils/model-options";

/* Notifications (a read-only overview), Runtime Connections and Datasets
   persist through their own screens, so the shared submission never reaches them, and offering
   `Save Project` there would report success for changes that never left the
   screen. Policy stays on this list because it is mixed: the execution policy
   below it saves itself, but `Project rules` is a field of this form. */
const FORM_OWNED_TABS = new Set<ProjectTabKey>([
  "overview",
  "workflows",
  "data-connections",
  "policy",
  "integrations",
]);

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

function renderChoice(condition: boolean, yes: ReactNode, no: ReactNode): ReactNode {
  return condition ? yes : no;
}

function footerStatus(pending: boolean, formOwned: boolean, tab: ProjectTabKey, readOnly: boolean, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (readOnly) {
    return t("react.projects.your_role_can_view_this_project_but_not_change_it", "Your role can view this project but not change it.");
  }
  if (pending) return t("react.projects.saving", "Saving…");
  // A read-only list has nothing to save, so it gets no saving line.
  if (tab === "connections" || tab === "notifications") return "";
  // U1: on the mixed Policy tab this line promised "every section", and the
  // execution policy -- concurrency cap among it -- stayed unsaved behind a success.
  if (tab === "policy") {
    return t("react.projects.save_project_saves_project_rules_the_execution_policy_below_saves_with_its_own_save_policy_button", "Save Project saves Project rules. The execution policy below saves with its own Save policy button.");
  }
  return formOwned
    ? t("react.projects.saving_applies_changes_from_every_section_of_this_form", "Saving applies changes from every section of this form.")
    : t("react.projects.this_section_saves_on_its_own", "This section saves on its own.");
}

function pluralSuffix(count: number): string {
  return count > 1 ? "s" : "";
}

function staleWorkflowPronoun(count: number): string {
  return count > 1 ? "them" : "it";
}

type WorkflowCatalogRow = {
  defaultCandidate: boolean;
  displayName: string;
  enabled: boolean;
  linked: boolean;
  stale: boolean;
  workflowId: string;
};

type ProjectEditFormProps = {
  activeTab: ProjectTabKey;
  addAgentOverride: (agentName: string) => void;
  agentCatalogFilterItems: Array<{ key: AgentCatalogMode; label: string; count: number }>;
  agentCatalogMessage: string;
  agentCatalogMode: AgentCatalogMode;
  agentCatalogOpen: boolean;
  agentCatalogSearch: string;
  agentModelFilter: string;
  agentModelOptions: string[];
  agentProviderFilter: string;
  agentProviderOptions: string[];
  agentSelectorRows: AssignmentSelectorRow[];
  assignedDataConnectionRows: Entity[];
  availableWorkflowRows: WorkflowCatalogRow[];
  dataConnectionFilterItems: Array<{ key: DataConnectionStatusFilter; label: string; count: number }>;
  dataConnectionSearch: string;
  dataConnectionSelectorRows: AssignmentSelectorRow[];
  dataConnectionStatusFilter: DataConnectionStatusFilter;
  dataConnectionsLoading: boolean;
  defaultModelDraft: string;
  defaultModelOptions: ModelOption[];
  defaultProviderDraft: string;
  /** Bumped by Cancel: remounts the uncontrolled fields with the saved values. */
  draftRevision: number;
  isModelRouteUnavailable: (provider: string, model: string) => boolean;
  linkedWorkflowCount: number;
  modelOptionsForProvider: (provider: string, currentValue?: string) => ModelOption[];
  mutationPending: boolean;
  modelRouteUnavailable: boolean;
  nextModelForProvider: (provider: string, currentModel: string) => string;
  notice: ProjectEditNotice | null;
  onDiscard: () => void;
  onSubmit: (event: SyntheticEvent<HTMLFormElement>) => void;
  project: Entity;
  projectDataReady: boolean;
  projectId: string;
  projectRelevantAgentIds: string[];
  providerOptions: ProviderOption[];
  /** Without `mutate_projects`: every control is disabled and nothing offers Save. */
  readOnly: boolean;
  removeAgentOverride: (agentName: string) => void;
  selectProjectTab: (tabKey: ProjectTabKey) => void;
  selectedDataConnectionIds: string[];
  selectedDefaultWorkflowId: string;
  selectedOverrideAgentIdSet: Set<string>;
  selectedOverrideAgentIds: string[];
  selectedWorkflowIds: string[];
  staleSelectedWorkflowIds: string[];
  setAgentCatalogMode: Dispatch<SetStateAction<AgentCatalogMode>>;
  setAgentCatalogOpen: Dispatch<SetStateAction<boolean>>;
  setAgentCatalogSearch: Dispatch<SetStateAction<string>>;
  setAgentModelFilter: Dispatch<SetStateAction<string>>;
  setAgentProviderFilter: Dispatch<SetStateAction<string>>;
  setDataConnectionSearch: Dispatch<SetStateAction<string>>;
  setDataConnectionStatusFilter: Dispatch<SetStateAction<DataConnectionStatusFilter>>;
  setDefaultModelDraft: Dispatch<SetStateAction<string>>;
  setDefaultProviderDraft: Dispatch<SetStateAction<string>>;
  setDefaultWorkflowId: Dispatch<SetStateAction<string>>;
  setWorkflowSearch: Dispatch<SetStateAction<string>>;
  setWorkflowStatusFilter: Dispatch<SetStateAction<WorkflowStatusFilter>>;
  tabItems: Array<{ key: ProjectTabKey; label: string }>;
  toggleDataConnection: (connectionId: string) => void;
  toggleWorkflow: (workflowId: string) => void;
  updateAgentOverride: (agentName: string, patch: Partial<AgentOverrideDraft>) => void;
  visibleOverrideEditors: AgentOverrideDraft[];
  workflowAssignmentsLoading: boolean;
  workflowFilterItems: Array<{ key: WorkflowStatusFilter; label: string; count: number }>;
  workflowSearch: string;
  workflowSelectorRows: AssignmentSelectorRow[];
  workflowStatusFilter: WorkflowStatusFilter;
  workspaceId: string;
  workspaces: Array<{ id: string; display_name?: string }>;
};

export function ProjectEditForm({
  activeTab,
  addAgentOverride,
  agentCatalogFilterItems,
  agentCatalogMessage,
  agentCatalogMode,
  agentCatalogOpen,
  agentCatalogSearch,
  agentModelFilter,
  agentModelOptions,
  agentProviderFilter,
  agentProviderOptions,
  agentSelectorRows,
  assignedDataConnectionRows,
  availableWorkflowRows,
  dataConnectionFilterItems,
  dataConnectionSearch,
  dataConnectionSelectorRows,
  dataConnectionStatusFilter,
  dataConnectionsLoading,
  defaultModelDraft,
  defaultModelOptions,
  defaultProviderDraft,
  draftRevision,
  isModelRouteUnavailable,
  linkedWorkflowCount,
  modelOptionsForProvider,
  mutationPending,
  modelRouteUnavailable,
  nextModelForProvider,
  notice,
  onDiscard,
  onSubmit,
  project,
  projectDataReady,
  projectId,
  projectRelevantAgentIds,
  providerOptions,
  readOnly,
  removeAgentOverride,
  selectProjectTab,
  selectedDataConnectionIds,
  selectedDefaultWorkflowId,
  selectedOverrideAgentIdSet,
  selectedOverrideAgentIds,
  selectedWorkflowIds,
  staleSelectedWorkflowIds,
  setAgentCatalogMode,
  setAgentCatalogOpen,
  setAgentCatalogSearch,
  setAgentModelFilter,
  setAgentProviderFilter,
  setDataConnectionSearch,
  setDataConnectionStatusFilter,
  setDefaultModelDraft,
  setDefaultProviderDraft,
  setDefaultWorkflowId,
  setWorkflowSearch,
  setWorkflowStatusFilter,
  tabItems,
  toggleDataConnection,
  toggleWorkflow,
  updateAgentOverride,
  visibleOverrideEditors,
  workflowAssignmentsLoading,
  workflowFilterItems,
  workflowSearch,
  workflowSelectorRows,
  workflowStatusFilter,
  workspaceId,
  workspaces,
}: Readonly<ProjectEditFormProps>) {
  const { t } = useI18n();
  const defaultModelLabel = defaultModelOptions.find(
    (option) => option.modelId === defaultModelDraft,
  )?.label || defaultModelDraft;
  const defaultProviderLabel =
    providerOptions.find((provider) => provider.value === defaultProviderDraft)?.label ||
    defaultProviderDraft;

  const formOwnsActiveTab = FORM_OWNED_TABS.has(activeTab);
  const historyActive = activeTab === "history";

  return (
    <form
      aria-label={t("react.projects.project_edit_form", "Project edit form")}
      autoComplete="off"
      className="fs-settings-tabs"
      key={`${textOrEmpty(project.id) || projectId}:${draftRevision}`}
      noValidate
      onSubmit={onSubmit}
    >
      <DsTabs
        active={activeTab}
        ariaLabel={t("react.projects.project_sections", "Project sections")}
        idPrefix="project-section"
        items={tabItems.map((tab) => ({ key: tab.key, label: tab.label }))}
        onSelect={(key) => selectProjectTab(key as ProjectTabKey)}
      />
      {/* Every section stays mounted and hidden rather than unmounted: one form
          spans all of them, so a field that leaves the DOM leaves the payload
          with it. */}
      <div
        aria-labelledby={`project-section-tab-${activeTab}`}
        className="fs-tabs-panel"
        id={`project-section-panel-${activeTab}`}
        role="tabpanel"
      >
        <div className="fs-panel-stack">
          {renderWhen(historyActive, <ProjectHistoryPanel projectId={projectId} />)}
          {/* Read-only is one switch for every section: a disabled fieldset
              disables each control inside it, the sections' own Save buttons
              included, while the data stays on screen. `contents` keeps the
              fieldset out of the panel's layout. */}
          <fieldset className="contents" disabled={readOnly}>

            <div className="fs-panel-group" hidden={activeTab !== "overview"}>
              <div className="fs-panel-heading">
                <h2 className="fs-panel-title">{t("react.projects.overview", "Overview")}</h2>
              </div>
              <div className="fs-form-block grid gap-3 md:grid-cols-2">
                <label className="fs-field md:col-span-2">
                  <span className="fs-field-label">{t("projects.grid.column.name", "Name")}</span>
                  <input
                    autoComplete="organization"
                    className="fs-input"
                    defaultValue={textOrEmpty(project.name)}
                    name="name"
                    required
                  />
                </label>
                <div className="fs-field">
                  <label className="fs-field-label" htmlFor="project-edit-workspace">{t("project_mode.workspace", "Workspace")}</label>
                  {/* A reference value the operator may read but not change: the
                      contract calls for read-only, never disabled, and the
                      read-only surface is what carries that meaning. */}
                  <input
                    className="fs-input"
                    id="project-edit-workspace"
                    readOnly
                    value={workspaces.find((workspace) => workspace.id === workspaceId)?.display_name || "—"}
                  />
                  <span className="fs-field-helper">{t("react.projects.set_when_the_project_was_created", "Set when the project was created.")}</span>
                  <input name="workspace_id" type="hidden" value={workspaceId} />
                </div>
                <label className="fs-field md:col-span-2">
                  <span className="fs-field-label">{t("registry.agents.field.description", "Description")}</span>
                  <textarea
                    className="fs-textarea"
                    defaultValue={textOrEmpty(project.description)}
                    name="description"
                  />
                </label>
              </div>
            </div>

            <div className="fs-panel-group" hidden={activeTab !== "workflows"}>
              <div className="fs-panel-heading">
                {/* The selector below reports the same two counts; one pair
                    is enough. */}
                <div>
                  <h2 className="fs-panel-title">{t("project_edit.tab.workflows", "Workflows")}</h2>
                  <p>
                    {t("react.projects.select_linked_workflows_and_optionally_record_an_operator_default_execution_still_requires_an_explicit_workflo", "Select linked workflows and optionally record an operator default. Execution still requires an explicit workflow choice at launch time.")} </p>
                </div>
              </div>
              <div className="space-y-4">
                {renderWhen(Boolean(staleSelectedWorkflowIds.length), (
                  <div className="fs-surface-radius border border-selected fs-surface-attention px-3 py-2 text-sm fs-ink-attention">
                    {t("react.projects.this_project_still_references_missing_workflow", "This project still references missing workflow")} {pluralSuffix(staleSelectedWorkflowIds.length)}{" "}
                    {staleSelectedWorkflowIds.map((workflowId) => `"${workflowId}"`).join(", ")}{t("react.projects.unselect", ". Unselect")} {staleWorkflowPronoun(staleSelectedWorkflowIds.length)} {t("react.projects.before_saving_project_changes", "before saving project changes.")} </div>
                ))}
                <AssignmentSelector
                  activeFilter={workflowStatusFilter}
                  emptyState={t("react.projects.no_workflows_available", "No workflows available.")}
                  filters={workflowFilterItems}
                  loading={workflowAssignmentsLoading}
                  loadingLabel={t("react.projects.loading_workflow_catalog", "Loading workflow catalog...")}
                  onFilterChange={(value) => setWorkflowStatusFilter(value as WorkflowStatusFilter)}
                  onSearchChange={setWorkflowSearch}
                  onToggleRow={toggleWorkflow}
                  readOnly={readOnly}
                  renderRowAction={(row) => (
                    selectedDefaultWorkflowId === row.id ? (
                      <span className="rounded-full border border-selected fs-surface-selected px-2 py-0.5 text-[11px] fs-ink-success">
                        {t("react.projects.operator_default_2", "operator default")} </span>
                    ) : (
                      <span className="text-xs fs-ink-tertiary">—</span>
                    )
                  )}
                  rows={workflowSelectorRows}
                  searchPlaceholder={t("react.projects.workflow_name_or_id", "Workflow name or ID")}
                  searchValue={workflowSearch}
                  selectedCount={linkedWorkflowCount}
                  totalCount={availableWorkflowRows.length}
                />
                <div className="fs-field fs-form-block">
                  <span className="fs-field-label">{t("react.projects.operator_default_workflow", "Operator default workflow")}</span>
                  <DsFormSelect
                    ariaLabel={t("react.projects.operator_default_workflow", "Operator default workflow")}
                    disabled={!selectedWorkflowIds.length}
                    name="operator_default_workflow_id"
                    onChange={setDefaultWorkflowId}
                    options={[
                      { value: "", label: t("react.projects.no_operator_default", "No operator default") },
                      ...selectedWorkflowIds.filter(
                        (workflowId) =>
                          !availableWorkflowRows.find((item) => item.workflowId === workflowId)?.stale,
                      )
                      .map((workflowId) => ({
                        value: workflowId,
                        label:
                          availableWorkflowRows.find((item) => item.workflowId === workflowId)?.displayName ||
                          workflowId,
                      })),
                    ]}
                    placeholder={t("react.projects.no_operator_default", "No operator default")}
                    value={selectedDefaultWorkflowId}
                  />
                  <p className="mt-2 text-xs fs-ink-tertiary">
                    {t("react.projects.this_setting_is_informational_for_operators_and_does_not_auto_select_a_workflow_for_execution_or_preview", "This setting is informational for operators and does not auto-select a workflow for execution or preview.")} </p>
                </div>
              </div>
            </div>

            <div className="fs-panel-group" hidden={activeTab !== "data-connections"}>
              <div className="fs-panel-heading">
                <div>
                  <h2 className="fs-panel-title">{t("shell.nav.data_connections", "Data Connections")}</h2>
                  <p>
                    {t("react.projects.assign_the_workspace_data_connections_this_project_s_workflows_and_extensions_use", "Assign the workspace data connections this project's workflows and extensions use.")} </p>
                </div>
              </div>
              <div className="space-y-4">
                    <div className="flex flex-wrap items-center gap-2 text-xs fs-ink-secondary">
                      <span className="rounded-full border border-selected fs-surface-selected px-2.5 py-1 fs-ink-brand">
                        {t("react.projects.assigned_connections_count", "Assigned: {count}", { count: selectedDataConnectionIds.length })}
                      </span>
                      <span className="rounded-full border border-subtle px-2.5 py-1">
                        {dataConnectionSelectorRows.length} {t("react.projects.workspace_connections", "workspace connections")} </span>
                    </div>
                    <div>
                      <h3 className="text-sm font-medium fs-ink-primary">{t("react.projects.project_data_connections", "Project data connections")}</h3>
                      <p className="mt-1 text-xs fs-ink-tertiary">
                        {t("react.projects.these_workspace_data_connections_are_assigned_to_this_project_this_is_separate_from_the", "These workspace data connections are assigned to this project; its workflows and extensions read data through them. This is separate from the")} <span className="font-medium fs-ink-secondary">{t("react.projects.runtime_connections", "Runtime Connections")}</span> {t("react.projects.tab", "tab.")} </p>
                    </div>
                    <AssignmentSelector
                      activeFilter={dataConnectionStatusFilter}
                      emptyState={t("react.projects.no_workspace_data_connections", "No workspace data connections exist for this workspace yet. Create them in the registry, then assign them here so project workflows can read data through them.")}
                      filters={dataConnectionFilterItems}
                      loading={dataConnectionsLoading}
                      loadingLabel={t("react.projects.loading_project_data_connections", "Loading project data connections...")}
                      onFilterChange={(value) =>
                        setDataConnectionStatusFilter(value as DataConnectionStatusFilter)
                      }
                      onSearchChange={setDataConnectionSearch}
                      onToggleRow={toggleDataConnection}
                      readOnly={readOnly}
                      rows={dataConnectionSelectorRows}
                      searchPlaceholder={t("react.projects.connection_name_engine_or_status", "Connection name, engine, or status")}
                      searchValue={dataConnectionSearch}
                      selectedCount={selectedDataConnectionIds.length}
                      totalCount={dataConnectionSelectorRows.length}
                    />
                    <p className="text-xs fs-ink-tertiary">
                      {t("react.projects.disabled_or_unhealthy_assignments_stay_visible_so_operators_can_repair_them_instead_of_losing_them_silently", "Disabled or unhealthy assignments stay visible so operators can repair them instead of losing them silently.")} </p>
                  </div>
            </div>

            <div className="fs-panel-group" hidden={activeTab !== "policy"}>
              <div className="fs-panel-heading">
                <div>
                  <h2 className="fs-panel-title">{t("project_detail.project_settings.policy", "Policy")}</h2>
                  <p>{t("react.projects.project_rules_and_execution_controls", "Project rules and execution controls.")}</p>
                </div>
              </div>
              <div className="space-y-3">
                <label className="fs-field fs-form-block">
                  <span className="fs-field-label">{t("react.projects.project_rules", "Project rules")}</span>
                  <textarea
                    className="fs-textarea"
                    defaultValue={textOrEmpty(project.project_rules)}
                    name="project_rules"
                    placeholder={t("react.projects.define_project_level_behavior_and_quality_policy", "Define project-level behavior and quality policy...")}
                  />
                </label>
              </div>
            </div>
            {renderWhen(activeTab === "policy", <ProjectPolicyEditor projectId={projectId} />)}

            <div className="fs-panel-group" hidden={activeTab !== "integrations"}>
              <div className="fs-panel-heading">
                <div>
                  <h2 className="fs-panel-title">{t("react.projects.models", "Models")}</h2>
                  <p>
                  {t("react.projects.set_the_project_default_model_route_then_override_only_the_agents_that_actually_need_a_different_provider_or_m", "Set the project default model route, then override only the agents that actually need a different provider or model.")} </p>
                </div>
              </div>
              <div className="space-y-6">
                <div className="fs-form-block grid gap-3 md:grid-cols-2">
                  <div className="fs-field">
                    <span className="fs-field-label">{t("react.projects.default_provider", "Default provider")}</span>
                    <DsFormSelect
                      ariaLabel={t("react.projects.default_provider", "Default provider")}
                      name="default_provider"
                      onChange={(nextProvider) => {
                        setDefaultProviderDraft(nextProvider);
                        setDefaultModelDraft((currentModel) => nextModelForProvider(nextProvider, currentModel));
                      }}
                      options={providerOptions}
                      placeholder={t("react.projects.use_platform_default", "Use platform default")}
                      value={defaultProviderDraft}
                    />
                  </div>
                  <div className="fs-field">
                    <span className="fs-field-label">{t("react.projects.default_model", "Default model")}</span>
                    <DsFormSelect
                      ariaLabel={t("react.projects.default_model", "Default model")}
                      disabled={!defaultProviderDraft}
                      name="default_model"
                      onChange={setDefaultModelDraft}
                      options={defaultModelOptions.map((modelOption) => ({
                        value: modelOption.modelId,
                        label: isModelRouteUnavailable(defaultProviderDraft, modelOption.modelId)
                          ? t("react.projects.value1_unavailable", "{value1} (unavailable)", { value1: String(modelOption.label) })
                          : modelOption.label,
                      }))}
                      placeholder={t("react.projects.use_project_provider_default", "Use project/provider default")}
                      value={defaultModelDraft}
                    />
                  </div>
                </div>
                {renderWhen(isModelRouteUnavailable(defaultProviderDraft, defaultModelDraft), (
                  <div className="fs-surface-radius border border-selected fs-surface-attention px-3 py-2 text-sm fs-ink-attention">
                    {t("react.projects.the_saved_project_model_route_is_unavailable_select_an_available_provider_and_model_before_saving", "The saved project model route is unavailable. Select an available provider and model before saving.")} </div>
                ))}
                <div className="space-y-3">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <h3 className="text-sm font-medium fs-ink-primary">{t("react.projects.per_agent_model_overrides", "Per-agent model overrides")}</h3>
                      <p className="mt-1 text-xs fs-ink-tertiary">
                        {t("react.projects.start_with_agents_already_used_by_linked_workflows_then_expand_into_the_full_catalog_only_when_needed", "Start with agents already used by linked workflows, then expand into the full catalog only when needed.")} </p>
                    </div>
                    <div className="flex flex-wrap gap-2 text-xs fs-ink-secondary">
                      <span className="rounded-full border border-selected fs-surface-selected px-2.5 py-1 fs-ink-brand">
                        {visibleOverrideEditors.length} {t("react.projects.active_overrides", "active overrides")} </span>
                      <span className="rounded-full border border-subtle px-2.5 py-1">
                        {projectRelevantAgentIds.length} {t("react.projects.project_relevant_agents", "project-relevant agents")} </span>
                    </div>
                  </div>

                  {renderChoice(Boolean(visibleOverrideEditors.length), (
                    <div className="space-y-3">
                      {visibleOverrideEditors.map((row) => {
                        const currentProvider = row.provider || "";
                        const currentModel = row.model || "";
                        const availableOverrideModels = modelOptionsForProvider(currentProvider, currentModel);
                        return (
                          <div
                            className="fs-surface-radius border border-subtle fs-surface-inset p-4"
                            key={row.agentName}
                          >
                            <div className="flex flex-wrap items-start justify-between gap-3">
                              <div>
                                <div className="flex flex-wrap items-center gap-2">
                                  <h4 className="font-mono text-sm fs-ink-primary">{row.agentName}</h4>
                                  {renderWhen(row.relevant, (
                                    <span className="rounded-full border border-selected fs-surface-selected px-2 py-0.5 text-[11px] fs-ink-success">
                                      project-relevant
                                    </span>
                                  ))}
                                </div>
                                <p className="mt-1 text-xs fs-ink-tertiary">
                                  {t("react.projects.default_route", "Default route:")} {row.defaultProvider || t("react.projects.platform_default", "platform default")}
                                  {row.defaultModel ? ` / ${row.defaultModel}` : ""}
                                </p>
                              </div>
                              <button
                                className="fs-button secondary sm"
                                onClick={() => removeAgentOverride(row.agentName)}
                                type="button"
                              >
                                {t("react.projects.remove_override", "Remove override")} </button>
                            </div>
                            <div className="mt-4 grid gap-3 md:grid-cols-2">
                              <div className="fs-field">
                                <span className="fs-field-label">{t("react.projects.provider_override", "Provider override")}</span>
                                <DsFormSelect
                                  ariaLabel={t("react.projects.provider_override_for_value1", "Provider override for {value1}", { value1: String(row.agentName) })}
                                  name={`agent_provider_${row.agentName}`}
                                  onChange={(nextProvider) => {
                                    updateAgentOverride(row.agentName, {
                                      provider: nextProvider,
                                      model: nextModelForProvider(nextProvider, currentModel),
                                    });
                                  }}
                                  options={providerOptions}
                                  placeholder={t("react.projects.use_project_default", "Use project default")}
                                  value={currentProvider}
                                />
                              </div>
                              <div className="fs-field">
                                <span className="fs-field-label">{t("react.projects.model_override", "Model override")}</span>
                                <DsFormSelect
                                  ariaLabel={t("react.projects.model_override_for_value1", "Model override for {value1}", { value1: String(row.agentName) })}
                                  name={`agent_model_${row.agentName}`}
                                  onChange={(model) => updateAgentOverride(row.agentName, { model })}
                                  options={availableOverrideModels.map((modelOption) => ({
                                    value: modelOption.modelId,
                                    label: isModelRouteUnavailable(currentProvider, modelOption.modelId)
                                      ? t("react.projects.value1_unavailable", "{value1} (unavailable)", { value1: String(modelOption.label) })
                                      : modelOption.label,
                                  }))}
                                  placeholder={t("react.projects.use_project_default", "Use project default")}
                                  value={currentModel}
                                />
                              </div>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ), (
                    <div className="fs-surface-radius border border-dashed border-subtle fs-surface-inset px-4 py-3 text-sm fs-ink-secondary">
                      {t("react.projects.no_per_agent_overrides_yet_add_only_the_agents_that_need_to_diverge_from_the_project_default_route", "No per-agent overrides yet. Add only the agents that need to diverge from the project default route.")} </div>
                  ))}

                  <div className="fs-surface-radius border border-subtle fs-surface-inset p-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <h4 className="text-sm font-medium fs-ink-primary">{t("react.projects.add_agent_override", "Add agent override")}</h4>
                        <p className="mt-1 text-xs fs-ink-tertiary">
                          {t("react.projects.search_project_relevant_agents_first_or_expand_to_the_full_catalog_for_rarer_overrides", "Search project-relevant agents first, or expand to the full catalog for rarer overrides.")} </p>
                      </div>
                      <button
                        className="fs-button secondary"
                        onClick={() => setAgentCatalogOpen((current) => !current)}
                        type="button"
                      >
                        {agentCatalogOpen ? t("react.projects.hide_catalog", "Hide catalog") : t("react.projects.browse_catalog", "Browse catalog")}
                      </button>
                    </div>

                    {renderWhen(agentCatalogOpen, (
                      <div className="mt-4 space-y-4">
                        <div className="grid gap-3 md:grid-cols-2">
                          <div className="fs-field">
                            <span className="fs-field-label">{t("react.projects.provider_filter", "Provider filter")}</span>
                            <DsSelect
                              ariaLabel={t("react.projects.provider_filter", "Provider filter")}
                              onChange={setAgentProviderFilter}
                              options={[
                                { value: "all", label: t("react.projects.all_providers", "All providers") },
                                ...agentProviderOptions.map((providerName) => ({
                                  value: providerName,
                                  label: providerName,
                                })),
                              ]}
                              value={agentProviderFilter}
                            />
                          </div>
                          <div className="fs-field">
                            <span className="fs-field-label">{t("react.projects.model_filter", "Model filter")}</span>
                            <DsSelect
                              ariaLabel={t("react.projects.model_filter", "Model filter")}
                              onChange={setAgentModelFilter}
                              options={[
                                { value: "all", label: t("react.projects.all_models", "All models") },
                                ...agentModelOptions.map((modelName) => ({
                                  value: modelName,
                                  label: modelName,
                                })),
                              ]}
                              value={agentModelFilter}
                            />
                          </div>
                        </div>
                        <AssignmentSelector
                          activeFilter={agentCatalogMode}
                          emptyState={t("react.projects.no_agents_match_filters", "No agents match the current filters.")}
                          filters={agentCatalogFilterItems}
                          message={agentCatalogMessage}
                          onFilterChange={(value) => setAgentCatalogMode(value as AgentCatalogMode)}
                          onSearchChange={setAgentCatalogSearch}
                          onToggleRow={(agentName) => {
                            if (selectedOverrideAgentIdSet.has(agentName)) {
                              removeAgentOverride(agentName);
                              return;
                            }
                            addAgentOverride(agentName);
                          }}
                          renderRowAction={(row) => (
                            <button
                              className="fs-button secondary sm"
                              onClick={() => {
                                if (selectedOverrideAgentIdSet.has(row.id)) {
                                  removeAgentOverride(row.id);
                                  return;
                                }
                                addAgentOverride(row.id);
                              }}
                              type="button"
                            >
                              {selectedOverrideAgentIdSet.has(row.id) ? t("registry.agent_detail.common.remove", "Remove") : t("react.projects.add_override", "Add override")}
                            </button>
                          )}
                          rows={agentSelectorRows}
                          searchPlaceholder={t("react.projects.agent_name_provider_or_model", "Agent name, provider, or model")}
                          searchValue={agentCatalogSearch}
                          selectedCount={selectedOverrideAgentIds.length}
                          selectedLabel="overrides"
                          totalCount={agentSelectorRows.length}
                          totalLabel="catalog"
                        />
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
            {renderWhen(activeTab === "notifications", (
              <ProjectNotificationOverview projectId={projectId} />
            ))}

            {renderWhen(activeTab === "connections", (
              <ProjectConnectionsEditor projectId={projectId} />
            ))}

            {renderWhen(activeTab === "datasets", (
              <ProjectDatasetsEditor projectId={projectId} />
            ))}

          {/* The rail this replaces stood beside the form and repeated what the
              sections already own. The pattern is one column, so the read-only
              identity stays with the section that opens the page. */}
          {/* The summary is a read-only object inside the form, so it gets the
              card's own surface, as Queue lanes does; its rows then align to
              the card's edges instead of the panel's. */}
          <article
            className="fs-card"
            data-testid="project-model-routing-summary"
            hidden={activeTab !== "overview"}
          >
            <header className="fs-card-header">
              <h2 className="fs-card-title">{t("react.projects.project_summary", "Project summary")}</h2>
            </header>
            <div className="fs-card-body">
            <div className="fs-detail-rows" data-columns="2">
              <div className="fs-detail-row">
                <span>{t("react.projects.project_id", "Project ID")}</span>
                <span className="fs-detail-value mono">{textOrEmpty(project.id) || projectId}</span>
              </div>
              <div className="fs-detail-row">
                <span>{t("react.projects.linked_workflows", "Linked workflows")}</span>
                <span className="fs-detail-value">{selectedWorkflowIds.length}</span>
              </div>
              <div className="fs-detail-row">
                <span>{t("react.projects.operator_default", "Operator default")}</span>
                <span className="fs-detail-value">{selectedDefaultWorkflowId || "—"}</span>
              </div>
              <div className="fs-detail-row">
                <span>{t("react.projects.agent_overrides", "Agent overrides")}</span>
                <span className="fs-detail-value">{selectedOverrideAgentIds.length}</span>
              </div>
              <div className="fs-detail-row">
                <span>{t("react.projects.default_provider", "Default provider")}</span>
                <span className="fs-detail-value">{defaultProviderLabel || t("react.projects.platform_default_2", "Platform default")}</span>
              </div>
              <div className="fs-detail-row">
                <span>{t("react.projects.default_model", "Default model")}</span>
                <span className="fs-detail-value">{defaultModelDraft ? defaultModelLabel : "—"}</span>
              </div>
              <div className="fs-detail-row">
                <span>{t("react.projects.data_connections", "Data connections")}</span>
                <span className="fs-detail-value">
                  {selectedDataConnectionIds.length
                    ? assignedDataConnectionRows
                        .slice(0, 3)
                        .map((connection) =>
                          textOrEmpty(connection.display_name) || textOrEmpty(connection.connection_id),
                        )
                        .join(", ")
                    : t("react.projects.none_assigned", "None assigned")}
                </span>
              </div>
            </div>
            </div>
          </article>
          </fieldset>

          {renderWhen(Boolean(notice), (
            <DsAlert
              tone={notice?.tone || "neutral"}
              title={notice?.tone === "danger" ? t("react.projects.changes_were_not_saved", "Changes were not saved") : t("react.projects.project_settings", "Project settings")}
            >
              {notice?.text}
            </DsAlert>
          ))}
        </div>

        {/* Canon puts the save action and its status in the footer, and forbids
            a page-level action row. One footer serves every section because one
            submission carries every section. */}
        {/* History restores revisions from its own dialog; nothing here saves it. */}
        {renderWhen(!historyActive, (
        <footer className="fs-card-footer">
          <span className={`fs-footer-status${mutationPending ? " is-saving" : ""}`}>
            {footerStatus(mutationPending, formOwnsActiveTab, activeTab, readOnly, t)}
          </span>
          {/* A section that saves on its own has nothing to cancel here. Cancel
              stays on the page and puts the saved values back. */}
          {renderWhen(formOwnsActiveTab && !readOnly, (
            <div className="fs-footer-actions">
              <button className="fs-button secondary" onClick={onDiscard} type="button">
                {t("common.cancel", "Cancel")} </button>
              <button
                className="fs-button primary"
                disabled={!projectDataReady || mutationPending || modelRouteUnavailable}
                type="submit"
              >
                {mutationPending ? t("project_edit.security.saving", "Saving...") : t("project_edit.button.save", "Save Project")}
              </button>
            </div>
          ))}
        </footer>
        ))}
      </div>
    </form>
  );
}
