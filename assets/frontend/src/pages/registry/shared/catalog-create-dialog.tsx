import { useI18n } from "@/lib/i18n/useI18n";
import type { Dispatch, SetStateAction, SyntheticEvent } from "react";
import { DsAlert, DsCheckbox, DsDialog, DsSelect } from "@/components/shared/design-system";
import { AssignmentSelector } from "@/components/shared/assignment-selector";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { normalizeString, objectOrEmpty, type EntityForm, type RegistryEntity } from "./catalog-surface-helpers";
import { renderWhen, type AgentModelRoute } from "./catalog-create-payload";
import {
  GuardrailRuleEditor,
} from "./guardrail-rule-editor";
import {
  defaultGuardrailConfig,
  defaultGuardrailRuleType,
} from "./guardrail-rule-options";

type RegistryCreateDialogProps = {
  saving: boolean;
  createLabel: string;
  endpoint: string;
  guardrailAgentIds: string[];
  modelOptionsQuery: { isLoading: boolean; isError: boolean; refetch: () => unknown };
  createModalId: string;
  setCreateFormOpen: (value: boolean) => void;
  createFormOpen: boolean;
  readOnly: boolean;
  handleCreate: (event: SyntheticEvent<HTMLFormElement>) => Promise<void>;
  showsIdentifierField: boolean;
  idKey: string;
  requiresIdentifier: boolean;
  setCreateForm: Dispatch<SetStateAction<EntityForm>>;
  createForm: EntityForm;
  requiresDisplayName: boolean;
  setAgentTypeId: (value: string) => void;
  agentTypesQuery: { data?: RegistryEntity[] };
  agentTypeId: string;
  setAgentSystemPrompt: (value: string) => void;
  agentSystemPrompt: string;
  setAgentModelRoute: (value: string) => void;
  agentModelOptions: Array<AgentModelRoute & { label: string; providerLabel: string }>;
  agentModelRoute: string;
  setToolCategory: (value: string) => void;
  toolCategory: string;
  setToolImplementationType: (value: string) => void;
  toolImplementationType: string;
  setToolPrompt: (value: string) => void;
  toolPrompt: string;
  setSkillCategory: (value: string) => void;
  skillCategory: string;
  setSkillInstructions: (value: string) => void;
  skillInstructions: string;
  setGuardrailMode: (value: "deterministic" | "semantic") => void;
  guardrailMode: "deterministic" | "semantic";
  guardrailPromptText: string;
  setGuardrailPromptText: (value: string) => void;
  setGuardrailSeverity: (value: "error" | "warning") => void;
  guardrailSeverity: "error" | "warning";
  setGuardrailRuleType: (value: string) => void;
  guardrailRuleType: string;
  setGuardrailConfigJson: (value: string) => void;
  guardrailConfigJson: string;
  guardrailAgentsQuery: { isLoading: boolean };
  setGuardrailAgentSearch: (value: string) => void;
  setGuardrailAgentIds: Dispatch<SetStateAction<string[]>>;
  guardrailAgentRows: Parameters<typeof AssignmentSelector>[0]["rows"];
  guardrailAgentSearch: string;
  guardrailAgentOptions: RegistryEntity[];
  createError: string;
};

export function RegistryCreateDialog({
  saving,
  createLabel,
  endpoint,
  guardrailAgentIds,
  modelOptionsQuery,
  createModalId,
  setCreateFormOpen,
  createFormOpen,
  readOnly,
  handleCreate,
  showsIdentifierField,
  idKey,
  requiresIdentifier,
  setCreateForm,
  createForm,
  requiresDisplayName,
  setAgentTypeId,
  agentTypesQuery,
  agentTypeId,
  setAgentSystemPrompt,
  agentSystemPrompt,
  setAgentModelRoute,
  agentModelOptions,
  agentModelRoute,
  setToolCategory,
  toolCategory,
  setToolImplementationType,
  toolImplementationType,
  setToolPrompt,
  toolPrompt,
  setSkillCategory,
  skillCategory,
  setSkillInstructions,
  skillInstructions,
  setGuardrailMode,
  guardrailMode,
  guardrailPromptText,
  setGuardrailPromptText,
  setGuardrailSeverity,
  guardrailSeverity,
  setGuardrailRuleType,
  guardrailRuleType,
  setGuardrailConfigJson,
  guardrailConfigJson,
  guardrailAgentsQuery,
  setGuardrailAgentSearch,
  setGuardrailAgentIds,
  guardrailAgentRows,
  guardrailAgentSearch,
  guardrailAgentOptions,
  createError,
}: Readonly<RegistryCreateDialogProps>) {
  const { t } = useI18n();
  return (
        <DsDialog
          busy={saving}
          closeLabel={t("react.registry.close_create_form", "Close create form")}
          description={t("react.registry.register_a_new_value0_in_this_catalog", "Register a new {value0} in this catalog.", { value0: createLabel.toLowerCase() })}
          eyebrow={t("trash.category.registry", "Registry")}
          footer={
            <>
              <button
                className="fs-button"
                disabled={saving}
                onClick={() => setCreateFormOpen(false)}
                type="button"
              >
                {t("extensions.repeater.cancel", "Cancel")}</button>
              <button
                className="fs-button primary"
                disabled={
                  saving
                  || (endpoint === "/api/guardrails" && !guardrailAgentIds.length)
                  || (endpoint === "/api/agents" && (modelOptionsQuery.isLoading || modelOptionsQuery.isError))
                }
                form={createModalId}
                type="submit"
              >
                {saving ? t("react.registry.creating", "Creating...") : t("react.registry.create_entry", "Create entry")}
              </button>
            </>
          }
          onClose={() => setCreateFormOpen(false)}
          open={createFormOpen && !readOnly}
          title={t("react.registry.create_value0", "Create {value0}", { value0: createLabel.toLowerCase() })}
        >
              <form
                className="fs-dialog-form"
                data-testid="registry-create-form"
                id={createModalId}
                onSubmit={(event) => ignoreAsyncResult(handleCreate(event))}
              >
                {renderWhen(showsIdentifierField, (
                  <div className="fs-field">
                    <label className="fs-field-label" htmlFor="create-identifier">
                      {idKey.toUpperCase()}
                      {renderWhen(requiresIdentifier, <span aria-hidden="true" className="fs-required">*</span>)}
                    </label>
                    <input
                      className="fs-input"
                      id="create-identifier"
                      name={idKey}
                      onChange={(event) => setCreateForm((current) => ({ ...current, identifier: event.target.value }))}
                      required={requiresIdentifier}
                      value={createForm.identifier}
                    />
                  </div>
                ))}
                <div className="fs-field">
                  <label className="fs-field-label" htmlFor="create-display-name">
                    {t("users.modal.display_name", "Display Name")}{renderWhen(requiresDisplayName, <span aria-hidden="true" className="fs-required">*</span>)}
                  </label>
                  <input
                    className="fs-input"
                    id="create-display-name"
                    name="display_name"
                    onChange={(event) => setCreateForm((current) => ({ ...current, display_name: event.target.value }))}
                    required={requiresDisplayName}
                    value={createForm.display_name}
                  />
                </div>
                <div className="fs-field">
                  <label className="fs-field-label" htmlFor="create-description">
                    {t("registry.workflows.simulation.description", "Description")}</label>
                  <textarea
                    className="fs-textarea"
                    id="create-description"
                    name="description"
                    onChange={(event) => setCreateForm((current) => ({ ...current, description: event.target.value }))}
                    value={createForm.description}
                  />
                </div>

                {renderWhen(endpoint === "/api/agents", (
                  <>
                    <div className="fs-field">
                      <span className="fs-field-label">
                        <span>{t("registry.agents.field.agent_type", "Agent Type")}</span>
                        <span aria-hidden="true" className="fs-required">*</span>
                      </span>
                      <DsSelect
                        ariaLabel={t("registry.agents.field.agent_type", "Agent Type")}
                        onChange={setAgentTypeId}
                        options={(agentTypesQuery.data || []).flatMap((agentType) => {
                          const row = objectOrEmpty(agentType);
                          const value = normalizeString(row.key || row.agent_type_id || row.role_type);
                          if (!value) {
                            return [];
                          }
                          return [{ value, label: normalizeString(row.label || row.display_name || value) }];
                        })}
                        placeholder={t("registry.agents.agent_type.select", "Select agent type")}
                        required
                        value={agentTypeId}
                      />
                    </div>
                    <div className="fs-field">
                      <label className="fs-field-label" htmlFor="create-system-prompt">
                        <span>{t("registry.agents.field.system_prompt", "System Prompt")}</span>
                        <span aria-hidden="true" className="fs-required">*</span>
                      </label>
                      <textarea
                        className="fs-textarea"
                        id="create-system-prompt"
                        name="system_prompt"
                        onChange={(event) => setAgentSystemPrompt(event.target.value)}
                        required
                        value={agentSystemPrompt}
                      />
                    </div>
                    <div className="fs-field">
                      <span className="fs-field-label">{t("registry.agents.field.model", "Model")}</span>
                      {renderWhen(modelOptionsQuery.isError, (
                        <div className="mt-1 fs-surface-radius border border-danger fs-surface-danger px-3 py-2 text-sm fs-ink-danger">
                          <p>{t("react.registry.model_catalog_is_unavailable", "Model catalog is unavailable.")}</p>
                          <button
                            className="mt-2 rounded-lg border border-rose-200/30 px-2 py-1 text-xs hover:border-rose-200/60"
                            onClick={() => { void modelOptionsQuery.refetch(); }}
                            type="button"
                          >
                            {t("react.registry.retry_model_catalog", "Retry model catalog")}</button>
                        </div>
                      ))}
                      <DsSelect
                        ariaLabel={t("registry.agents.field.model", "Model")}
                        disabled={modelOptionsQuery.isLoading || modelOptionsQuery.isError}
                        onChange={setAgentModelRoute}
                        options={agentModelOptions.map((route) => ({
                          value: `${route.provider}::${route.modelId}`,
                          label: `${route.label} (${route.providerLabel})`,
                        }))}
                        placeholder={t("react.registry.select_model_or_executor", "Select model or executor")}
                        required
                        value={agentModelRoute}
                      />
                    </div>
                  </>
                ))}

                {renderWhen(endpoint === "/api/tools", (
                  <>
                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="fs-field">
                        <span className="fs-field-label">{t("trash.columns.category", "Category")}</span>
                        <DsSelect
                          ariaLabel={t("trash.columns.category", "Category")}
                          onChange={setToolCategory}
                          options={[{ value: "knowledge", label: t("react.registry.knowledge", "knowledge") }, { value: "code", label: t("react.registry.code", "code") }, { value: "version_control", label: "version_control" }, { value: "testing", label: t("react.registry.testing", "testing") }, { value: "integration", label: t("react.registry.integration", "integration") }, { value: "ui_automation", label: "ui_automation" }, { value: "general", label: t("react.registry.general", "general") }]}
                          value={toolCategory}
                        />
                      </div>
                      <div className="fs-field">
                        <span className="fs-field-label">{t("registry.tools.field.implementation", "Implementation")}</span>
                        <DsSelect
                          ariaLabel={t("registry.tools.field.implementation", "Implementation")}
                          onChange={setToolImplementationType}
                          options={[{ value: "internal", label: t("react.registry.internal", "internal") }, { value: "mcp", label: "mcp" }, { value: "native", label: t("react.registry.native", "native") }]}
                          value={toolImplementationType}
                        />
                      </div>
                    </div>
                    <div className="fs-field">
                      <label className="fs-field-label" htmlFor="create-tool-prompt">
                        {t("registry.tool_detail.tab.prompt", "Prompt")}</label>
                      <textarea
                        className="fs-textarea"
                        id="create-tool-prompt"
                        name="prompt"
                        onChange={(event) => setToolPrompt(event.target.value)}
                        value={toolPrompt}
                      />
                    </div>
                  </>
                ))}

                {renderWhen(endpoint === "/api/skills", (
                  <>
                    <div className="fs-field">
                      <span className="fs-field-label">{t("trash.columns.category", "Category")}</span>
                      <DsSelect
                        ariaLabel={t("trash.columns.category", "Category")}
                        onChange={setSkillCategory}
                        options={[{ value: "analysis", label: t("react.registry.analysis", "analysis") }, { value: "planning", label: t("react.registry.planning", "planning") }, { value: "design", label: t("react.registry.design", "design") }, { value: "development", label: t("react.registry.development", "development") }, { value: "delivery", label: t("react.registry.delivery", "delivery") }, { value: "quality", label: t("react.registry.quality", "quality") }, { value: "review", label: t("react.workflows.review_variant_c97ace4c", "review") }, { value: "knowledge", label: t("react.registry.knowledge", "knowledge") }, { value: "rework", label: t("react.registry.rework", "rework") }, { value: "security", label: t("react.registry.security", "security") }, { value: "documentation", label: t("react.registry.documentation", "documentation") }]}
                        value={skillCategory}
                      />
                    </div>
                    <div className="fs-field">
                      <label className="fs-field-label" htmlFor="create-skill-instructions">
                        {t("registry.skills.field.instructions_short", "Instructions")}</label>
                      <textarea
                        className="fs-textarea"
                        id="create-skill-instructions"
                        name="instructions"
                        onChange={(event) => setSkillInstructions(event.target.value)}
                        value={skillInstructions}
                      />
                    </div>
                  </>
                ))}

                {renderWhen(endpoint === "/api/guardrails", (
                  <>
                    <div className="grid gap-3 md:grid-cols-3">
                      <div className="fs-field">
                        <span className="fs-field-label">{t("registry.workflow_detail.run_lab.test_run.mode", "Mode")}</span>
                        <DsSelect
                          ariaLabel={t("registry.workflow_detail.run_lab.test_run.mode", "Mode")}
                          onChange={(next) => {
                            const mode = next === "semantic" ? "semantic" : "deterministic";
                            const ruleType = defaultGuardrailRuleType(mode);
                            setGuardrailMode(mode);
                            setGuardrailRuleType(ruleType);
                            setGuardrailConfigJson(defaultGuardrailConfig(ruleType));
                            if (mode === "deterministic") setGuardrailPromptText("");
                          }}
                          options={[{ value: "deterministic", label: t("react.registry.deterministic", "Deterministic") }, { value: "semantic", label: t("react.registry.semantic", "Semantic") }]}
                          value={guardrailMode}
                        />
                      </div>
                      <div className="fs-field">
                        <span className="fs-field-label">{t("security_overview.columns.severity", "Severity")}</span>
                        <DsSelect
                          ariaLabel={t("security_overview.columns.severity", "Severity")}
                          onChange={(next) => setGuardrailSeverity(next === "error" ? "error" : "warning")}
                          options={[{ value: "warning", label: t("react.registry.warning_variant_4bd9354b", "warning") }, { value: "error", label: t("react.registry.error_variant_ca00fccf", "error") }]}
                          value={guardrailSeverity}
                        />
                        <span className="fs-field-helper">
                          {guardrailSeverity === "warning"
                            ? t("react.registry.warning_reports_a_violation_without_blocking_the_step", "Warning reports a violation without blocking the step.")
                            : t("react.registry.error_reports_the_violation_as_blocking", "Error reports the violation as blocking.")}
                        </span>
                      </div>
                    </div>
                    <GuardrailRuleEditor
                      configJson={guardrailConfigJson}
                      mode={guardrailMode}
                      onConfigJsonChange={setGuardrailConfigJson}
                      onPromptTextChange={setGuardrailPromptText}
                      onRuleTypeChange={setGuardrailRuleType}
                      promptText={guardrailPromptText}
                      ruleType={guardrailRuleType}
                    />
                    <div className="fs-surface-radius border border-subtle fs-surface-inset p-3">
                      <AssignmentSelector
                        emptyState={t("react.registry.display_no_assignment_agents", "No agents are available for assignment.")}
                        loading={guardrailAgentsQuery.isLoading}
                        loadingLabel={t("react.registry.loading_agents", "Loading agents…")}
                        message={guardrailAgentIds.length ? "" : t("react.registry.display_select_one_agent", "Select at least one agent.")}
                        onSearchChange={setGuardrailAgentSearch}
                        onToggleRow={(agentId) => {
                          setGuardrailAgentIds((current) =>
                            current.includes(agentId)
                              ? current.filter((item) => item !== agentId)
                              : [...current, agentId].sort((left, right) => left.localeCompare(right)),
                          );
                        }}
                        rows={guardrailAgentRows}
                        searchPlaceholder={t("react.registry.search_agents", "Search agents")}
                        searchValue={guardrailAgentSearch}
                        selectedCount={guardrailAgentIds.length}
                        selectedLabel="selected"
                        totalCount={guardrailAgentOptions.length}
                        totalLabel="agents"
                      />
                    </div>
                  </>
                ))}

                <DsCheckbox
                  checked={createForm.enabled}
                  onChange={(checked) => setCreateForm((current) => ({ ...current, enabled: checked }))}
                >
                  {t("settings.mcp.status.enabled", "Enabled")}</DsCheckbox>

                {renderWhen(Boolean(createError), <DsAlert title={createError} tone="danger" />)}
              </form>
        </DsDialog>
  );
}
