import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { Link, useSearchParams } from "react-router";
import { useQuery } from "@tanstack/react-query";

import {
  DsAlert,
  DsPageHeader,
  DsSelect,
  DsTabs,
  PageContainer,
} from "@/components/shared/design-system";
import { ChevronRight } from "lucide-react";
import { fetchRegistryList } from "@/pages/registry/shared/api";
import { RegistryEntityChangelogPanel } from "@/pages/registry/shared/entity-changelog-panel";
import {
  firstStringArray,
  parseCommaSeparated,
  useRegistryEntityDetail,
} from "@/pages/registry/shared/detail-helpers";
import { SaveErrorAlert, SaveStatusBadge } from "@/pages/registry/shared/save-feedback";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { AssignmentSelector } from "@/components/shared/assignment-selector";
import {
  GuardrailRuleEditor,
} from "@/pages/registry/shared/guardrail-rule-editor";
import {
  defaultGuardrailRuleType,
} from "@/pages/registry/shared/guardrail-rule-options";

function localizedGUARDRAIL_TABS(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { key: "config", label: t("react.registry.config", "Config") },
  { key: "scope", label: t("registry.guardrail_detail.tab.agent_scope", "Agent Scope") },
  { key: "changelog", label: t("react.registry.changelog", "Changelog") },
] as const;
}

const GUARDRAIL_TABS = localizedGUARDRAIL_TABS();

type GuardrailTabKey = (typeof GUARDRAIL_TABS)[number]["key"];

const GUARDRAIL_TAB_KEYS = new Set<string>(GUARDRAIL_TABS.map((tab) => tab.key));

function configValueToText(value: unknown): string {
  if (Array.isArray(value)) {
    return value.map((item) => firstStringFromUnknownValue([item, ""])).join("\n");
  }
  if (value && typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return stringFromUnknownValue(value ?? "");
}

function parseConfigValue(value: string, previous: unknown): unknown {
  const text = firstStringFromUnknownValue([value, ""]).trim();
  if (Array.isArray(previous)) {
    return text
      .split(/\r?\n|,/)
      .map((item) => item.trim())
      .filter(isTruthy);
  }
  if (previous && typeof previous === "object") {
    try {
      return JSON.parse(text || "{}");
    } catch {
      return previous;
    }
  }
  if (text === "true") return true;
  if (text === "false") return false;
  if (text === "null") return null;
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if (text.startsWith("[") || text.startsWith("{")) {
    try {
      return JSON.parse(text);
    } catch {
      return value;
    }
  }
  return value;
}

function parseConfigDraftValue(
  value: string,
  fallback: Record<string, unknown> | null,
): Record<string, unknown> {
  try {
    const parsed = JSON.parse(value);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    return fallback || {};
  }
  return fallback || {};
}

function stableJsonKey(value: unknown): string {
  try {
    return JSON.stringify(value ?? {});
  } catch {
    return "{}";
  }
}

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function RegistryGuardrailsDetailPage() {
  const { t } = useI18n();
  // The URL owns the active section, so a tab is addressable and survives a
  // reload. Same shape as agents/detail and skills/detail — Guardrails must not
  // become the third variant inside one section.
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") || "";
  const activeTab: GuardrailTabKey = GUARDRAIL_TAB_KEYS.has(requestedTab)
    ? (requestedTab as GuardrailTabKey)
    : "config";
  function goToTab(key: string) {
    const next = new URLSearchParams(searchParams);
    if (key === "config") {
      next.delete("tab");
    } else {
      next.set("tab", key);
    }
    setSearchParams(next, { replace: true });
  }
  const [displayNameDraft, setDisplayNameDraft] = useState("");
  const [descriptionDraft, setDescriptionDraft] = useState("");
  const [ruleTypeDraft, setRuleTypeDraft] = useState("");
  const [modeDraft, setModeDraft] = useState("deterministic");
  const [severityDraft, setSeverityDraft] = useState("error");
  const [projectIdDraft, setProjectIdDraft] = useState("");
  const [promptTextDraft, setPromptTextDraft] = useState("");
  const [agentPromptDraft, setAgentPromptDraft] = useState("");
  const [scopeDraft, setScopeDraft] = useState("");
  const [configDraft, setConfigDraft] = useState("{}");
  // The JSON parse error is a field error, not a page status.
  const [configDraftError, setConfigDraftError] = useState("");
  const [agentSearch, setAgentSearch] = useState("");
  const {
    detailQuery,
    entity,
    entityName,
    historyQuery,
    resolvedEntityId,
    routeEntityId,
    saveEntityPatch,
    saveError,
    saveStatus,
    saving,
    setSaveStatus,
    toggleEntityEnabled,
  } = useRegistryEntityDetail({
    endpoint: "/api/guardrails",
    idKey: "id",
    idParam: "guardrailId",
    title: t("react.registry.guardrail", "Guardrail"),
    updateMode: "patch",
  });

  const rawConfig = useMemo(
    () =>
      entity.config &&
      typeof entity.config === "object" &&
      !Array.isArray(entity.config)
        ? (entity.config as Record<string, unknown>)
        : {},
    [entity.config],
  );
  const configKey = stableJsonKey(rawConfig);
  const scopedAgents = firstStringArray(entity, [
    "_agents",
    "agent_ids",
    "agents",
  ]);
  const scopedAgentsKey = scopedAgents.join(", ");
  const agentsQuery = useQuery({
    queryKey: ["registry", "guardrail-scope", "agents"],
    queryFn: () => fetchRegistryList("/api/agents"),
    retry: false,
  });
  const projectsQuery = useQuery({
    queryKey: ["registry", "guardrail-scope", "projects"],
    queryFn: () => fetchRegistryList("/api/projects"),
    retry: false,
  });
  const agentOptions = Array.isArray(agentsQuery.data) ? agentsQuery.data : [];
  const projectOptions = Array.isArray(projectsQuery.data)
    ? projectsQuery.data
    : [];
  const selectedScope = parseCommaSeparated(scopeDraft);
  const selectedScopeSet = new Set(selectedScope);
  const agentRows = agentOptions.flatMap((agent) => {
    const agentId = firstStringFromUnknownValue([agent.agent_id, agent.id, agent.name, ""]).trim();
    if (!agentId) {
      return [];
    }
    const title = firstStringFromUnknownValue([agent.display_name, agent.name, agentId]);
    const selected = selectedScopeSet.has(agentId);
    return [{
      id: agentId,
      title,
      subtitle: agentId,
      selected,
      searchText: `${agentId} ${title}`,
      badges: [{ label: selected ? t("project_history.selected", "selected") : t("react.registry.available", "available"), tone: selected ? "linked" as const : "available" as const }],
    }];
  });
  const normalizedRuleType = ruleTypeDraft.trim().toLowerCase();
  const draftConfig = useMemo(
    () => parseConfigDraftValue(configDraft, rawConfig),
    [configDraft, rawConfig],
  );
  const typedConfigKeys = new Set<string>();
  if (normalizedRuleType === "forbidden_patterns") {
    typedConfigKeys.add("patterns");
  }
  if (normalizedRuleType === "required_sections") {
    typedConfigKeys.add("sections");
  }
  const editableConfigEntries = Object.entries(draftConfig).filter(
    ([key]) => !typedConfigKeys.has(key),
  );

  useEffect(() => {
    setDisplayNameDraft(
      firstStringFromUnknownValue([entity.display_name, entity.name, ""]).trim(),
    );
    setDescriptionDraft(firstStringFromUnknownValue([entity.description, ""]).trim());
    setRuleTypeDraft(firstStringFromUnknownValue([entity.rule_type, ""]).trim());
    setModeDraft(firstStringFromUnknownValue([entity.mode, "deterministic"]).trim() || "deterministic");
    setSeverityDraft(firstStringFromUnknownValue([entity.severity, "error"]).trim() || "error");
    setProjectIdDraft(firstStringFromUnknownValue([entity.project_id, ""]).trim());
    setPromptTextDraft(firstStringFromUnknownValue([entity.prompt_text, ""]).trim());
    setAgentPromptDraft(firstStringFromUnknownValue([entity.agent_prompt, ""]).trim());
    setScopeDraft(scopedAgentsKey);
    setConfigDraft(JSON.stringify(rawConfig, null, 2));
  }, [configKey, entity, rawConfig, scopedAgentsKey]);

  function parseConfigDraft() {
    try {
      const parsed = JSON.parse(configDraft);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error(t("react.registry.config_must_be_a_json_object", "Config must be a JSON object."));
      }
      setConfigDraftError("");
      return parsed as Record<string, unknown>;
    } catch (error) {
      // The message belongs to the JSON field, not to the page header, where it
      // used to sit beside "Saved." in the same neutral badge — no field, no
      // announcement, no error tone.
      const message = error instanceof Error ? error.message : t("react.registry.display_invalid_json", "Invalid JSON.");
      setConfigDraftError(message);
      return null;
    }
  }

  function updateConfigField(key: string, value: string) {
    const current = parseConfigDraft() || {};
    setConfigDraft(
      JSON.stringify(
        { ...current, [key]: parseConfigValue(value, current[key]) },
        null,
        2,
      ),
    );
  }

  function toggleScopeAgent(agentId: string) {
    setScopeDraft((currentDraft) => {
      const current = new Set(parseCommaSeparated(currentDraft));
      if (current.has(agentId)) {
        current.delete(agentId);
      } else {
        current.add(agentId);
      }
      return [...current].sort((a, b) => a.localeCompare(b)).join(", ");
    });
  }

  return (
    <PageContainer className="fs-page-sections" width="standard">
      <DsPageHeader
        action={
          <>
            <Link className="fs-button" to="/registry/guardrails">
              {t("react.registry.back_to_guardrails", "Back to guardrails")}</Link>
            <button
              className="fs-button"
              disabled={saving}
              onClick={() => {
                ignoreAsyncResult(toggleEntityEnabled());
              }}
              type="button"
            >
              {entity.enabled ? t("extensions.detail.disable", "Disable") : t("extensions.detail.enable", "Enable")}
            </button>
            <SaveStatusBadge status={saveStatus} />
          </>
        }
        description={t("react.registry.review_policy_settings_scoped_agents_and_recent_changes_for_this_guardrail", "Review policy settings, scoped agents, and recent changes for this guardrail.")}
        eyebrow={t("trash.category.registry", "Registry")}
        title={entityName || t("react.registry.guardrail_detail", "Guardrail detail")}
      />
      <SaveErrorAlert error={saveError} />

      {renderWhen(detailQuery.isError, (
        <DsAlert title={t("react.registry.failed_to_load_guardrail_detail_variant_f4bbba30", "Failed to load guardrail detail")} tone="danger">
          {errorMessage(detailQuery.error, t("react.registry.failed_to_load_guardrail_detail", "Failed to load guardrail detail."))}
        </DsAlert>
      ))}

      <section aria-label={t("react.registry.guardrail_detail", "Guardrail detail")} className="fs-settings-tabs">
        <DsTabs
          active={activeTab}
          ariaLabel={t("react.registry.guardrail_detail_sections", "Guardrail detail sections")}
          idPrefix="guardrail"
          items={localizedGUARDRAIL_TABS(t)}
          onSelect={goToTab}
        />
        <div
          aria-labelledby={`guardrail-tab-${activeTab}`}
          className="fs-tabs-panel"
          id={`guardrail-panel-${activeTab}`}
          role="tabpanel"
        >
          <div className="fs-panel-stack">
            {renderWhen(activeTab === "config", (
        <div className="fs-panel-group">
          <div className="fs-panel-heading">
            <div>
              <h2 className="fs-panel-title">{t("react.registry.guardrail_config", "Guardrail Config")}</h2>
              <p>
                {t("react.registry.rule_metadata_and_business_controls_that_define_what_this_guardrail_enforces_at_runtime", "Rule metadata and business controls that define what this guardrail enforces at runtime.")}</p>
            </div>
          </div>
          <div className="fs-form-grid">
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="guardrail-display-name">
                {t("users.modal.display_name", "Display Name")}</label>
              <input
                className="fs-input"
                id="guardrail-display-name"
                onChange={(event) => setDisplayNameDraft(event.target.value)}
                placeholder={t("react.registry.display_name", "Display name")}
                type="text"
                value={displayNameDraft}
              />
            </div>
            <div className="fs-field">
              <span className="fs-field-label">{t("registry.workflow_detail.run_lab.test_run.mode", "Mode")}</span>
              <DsSelect
                ariaLabel={t("registry.workflow_detail.run_lab.test_run.mode", "Mode")}
                onChange={(value) => {
                  const mode = value === "semantic" ? "semantic" : "deterministic";
                  const ruleType = defaultGuardrailRuleType(mode);
                  const currentConfig = parseConfigDraft() || {};
                  const nextConfig = mode === "deterministic"
                    ? {
                        ...currentConfig,
                        patterns: Array.isArray(currentConfig.patterns)
                          ? currentConfig.patterns
                          : [],
                      }
                    : currentConfig;
                  setModeDraft(mode);
                  setRuleTypeDraft(ruleType);
                  setConfigDraft(JSON.stringify(nextConfig, null, 2));
                  if (mode === "deterministic") setPromptTextDraft("");
                }}
                options={[
                  { label: t("react.registry.deterministic", "Deterministic"), value: "deterministic" },
                  { label: t("react.registry.semantic", "Semantic"), value: "semantic" },
                ]}
                value={modeDraft}
              />
            </div>
            <div className="fs-field">
              <span className="fs-field-label">{t("security_overview.columns.severity", "Severity")}</span>
              <DsSelect
                ariaLabel={t("security_overview.columns.severity", "Severity")}
                onChange={setSeverityDraft}
                options={[
                  { label: t("react.registry.error", "Error"), value: "error" },
                  { label: t("react.registry.warning", "Warning"), value: "warning" },
                ]}
                value={severityDraft}
              />
              <span className="fs-field-helper">
                {severityDraft === "warning"
                  ? t("react.registry.warning_reports_a_violation_without_blocking_the_step", "Warning reports a violation without blocking the step.")
                  : t("react.registry.error_reports_the_violation_as_blocking", "Error reports the violation as blocking.")}
              </span>
            </div>
            {/* `Global` has to be a real entry in `options`: `DsSelect` renders
                `placeholder` as non-selectable text, so a user who picked a
                project could otherwise never get back to global scope. */}
            <div className="fs-field">
              <span className="fs-field-label">{t("registry.guardrail_detail.field.project_scope", "Project Scope")}</span>
              <DsSelect
                ariaLabel={t("registry.guardrail_detail.field.project_scope", "Project Scope")}
                onChange={setProjectIdDraft}
                options={[
                  { label: t("react.registry.global", "Global"), value: "" },
                  ...projectOptions.flatMap((project) => {
                    const projectId = firstStringFromUnknownValue([
                      project.id,
                      project.project_id,
                      "",
                    ]).trim();
                    if (!projectId) {
                      return [];
                    }
                    return [
                      {
                        label: firstStringFromUnknownValue([
                          project.name,
                          project.display_name,
                          projectId,
                        ]),
                        value: projectId,
                      },
                    ];
                  }),
                ]}
                value={projectIdDraft}
              />
            </div>
            <div className="fs-field full">
              <label className="fs-field-label" htmlFor="guardrail-description">
                {t("registry.workflows.simulation.description", "Description")}</label>
              <textarea
                className="fs-textarea"
                id="guardrail-description"
                onChange={(event) => setDescriptionDraft(event.target.value)}
                placeholder={t("registry.workflows.simulation.description", "Description")}
                value={descriptionDraft}
              />
            </div>
            <div className="fs-field full">
              <label className="fs-field-label" htmlFor="guardrail-policy-guidance">
                {t("registry.guardrail_detail.field.policy_guidance", "Policy Guidance")}</label>
              <textarea
                className="fs-textarea"
                id="guardrail-policy-guidance"
                onChange={(event) => setAgentPromptDraft(event.target.value)}
                placeholder={t("react.registry.guidance_injected_into_the_agent_prompt", "Guidance injected into the agent prompt")}
                value={agentPromptDraft}
              />
            </div>
          </div>
          <div className="fs-form-grid">
            <GuardrailRuleEditor
              configJson={configDraft}
              mode={modeDraft === "semantic" ? "semantic" : "deterministic"}
              onConfigJsonChange={setConfigDraft}
              onPromptTextChange={setPromptTextDraft}
              onRuleTypeChange={setRuleTypeDraft}
              promptText={promptTextDraft}
              ruleType={ruleTypeDraft}
            />
          </div>

          <div className="fs-form-grid">
            {renderWhen(Boolean(editableConfigEntries.length), (
              editableConfigEntries.map(([key, value]) => {
                const isStructured =
                  Array.isArray(value) ||
                  (value && typeof value === "object");
                return (
                  <div
                    className={[
                      "fs-field",
                      isStructured ? "full" : "",
                    ].map((value) => stringFromUnknownValue(value)).join(" ")}
                    key={key}
                  >
                    <label className="fs-field-label" htmlFor={`guardrail-config-${key}`}>
                      {key.replaceAll("_", " ")}
                    </label>
                    {isStructured ? (
                      <textarea
                        className="fs-textarea"
                        id={`guardrail-config-${key}`}
                        onChange={(event) =>
                          updateConfigField(key, event.target.value)
                        }
                        value={configValueToText(value)}
                      />
                    ) : (
                      <input
                        className="fs-input"
                        id={`guardrail-config-${key}`}
                        onChange={(event) =>
                          updateConfigField(key, event.target.value)
                        }
                        value={configValueToText(value)}
                      />
                    )}
                  </div>
                );
              })
            ))}
            {renderWhen(
              !editableConfigEntries.length &&
                !typedConfigKeys.size &&
                modeDraft !== "semantic",
              (
                <div className="fs-field">
                  <label className="fs-field-label" htmlFor="guardrail-rule-value">
                    {t("react.registry.rule_value", "Rule value")}</label>
                  <input
                    className="fs-input"
                    id="guardrail-rule-value"
                    onChange={(event) =>
                      updateConfigField("value", event.target.value)
                    }
                    placeholder={t("react.registry.rule_value", "Rule value")}
                    value={configValueToText(draftConfig.value)}
                  />
                </div>
              ),
            )}
          </div>
          <details className="fs-disclosure">
            <summary>
              <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
              {t("react.registry.advanced_policy_json", "Advanced policy JSON")}</summary>
            <div className="fs-disclosure-body">
              {/* A summary is a toggle, not a field label, so the textarea gets
                  its own `label[for]` — the rule is every control, not every
                  control that has nowhere else to look. */}
              <label className="fs-field-label" htmlFor="guardrail-config-json">
                {t("react.registry.advanced_policy_json", "Advanced policy JSON")}</label>
              <textarea
                aria-describedby={configDraftError ? "guardrail-config-json-error" : undefined}
                aria-invalid={Boolean(configDraftError)}
                className="fs-textarea fs-code-value min-h-40"
                id="guardrail-config-json"
                onChange={(event) => {
                  setConfigDraft(event.target.value);
                  if (configDraftError) {
                    setConfigDraftError("");
                  }
                }}
                value={configDraft}
              />
              {/* A validation error belongs to the field that failed. It used
                  to land in `saveStatus`, i.e. in the page header, styled the
                  same as "Saved." — no field, no announcement, no error tone. */}
              {renderWhen(Boolean(configDraftError), (
                <p className="fs-field-helper error" id="guardrail-config-json-error">
                  {configDraftError}
                </p>
              ))}
            </div>
          </details>
          <div className="flex flex-wrap gap-2">
            <button
              className="fs-button primary"
              disabled={saving}
              onClick={() => {
                const parsedConfig = parseConfigDraft();
                if (!parsedConfig) {
                  return;
                }
                ignoreAsyncResult(saveEntityPatch({
                  display_name: displayNameDraft.trim(),
                  name: displayNameDraft.trim(),
                  description: descriptionDraft.trim(),
                  mode: modeDraft === "semantic" ? "semantic" : "deterministic",
                  severity: severityDraft === "warning" ? "warning" : "error",
                  rule_type: ruleTypeDraft.trim(),
                  project_id: projectIdDraft.trim() || null,
                  prompt_text: promptTextDraft.trim(),
                  agent_prompt: agentPromptDraft.trim(),
                  config: parsedConfig,
                }));
              }}
              type="button"
            >
              {t("react.registry.save_config", "Save config")}</button>
            <button
              className="fs-button"
              onClick={() => {
                setDisplayNameDraft(
                  firstStringFromUnknownValue([entity.display_name, entity.name, ""]).trim(),
                );
                setDescriptionDraft(firstStringFromUnknownValue([entity.description, ""]).trim());
                setRuleTypeDraft(firstStringFromUnknownValue([entity.rule_type, ""]).trim());
                setModeDraft(
                  firstStringFromUnknownValue([entity.mode, "deterministic"]).trim() || "deterministic",
                );
                setSeverityDraft(
                  firstStringFromUnknownValue([entity.severity, "error"]).trim() || "error",
                );
                setProjectIdDraft(firstStringFromUnknownValue([entity.project_id, ""]).trim());
                setPromptTextDraft(firstStringFromUnknownValue([entity.prompt_text, ""]).trim());
                setAgentPromptDraft(firstStringFromUnknownValue([entity.agent_prompt, ""]).trim());
                setConfigDraft(JSON.stringify(rawConfig, null, 2));
                setSaveStatus("");
              }}
              type="button"
            >
              {t("trash.reset", "Reset")}</button>
          </div>
        </div>
            ))}

            {renderWhen(activeTab === "scope", (
        <div className="fs-panel-group">
          <div className="fs-panel-heading">
            <div>
              <h2 className="fs-panel-title">{t("registry.guardrail_detail.tab.agent_scope", "Agent Scope")}</h2>
              <p>
                {t("react.registry.select_every_agent_this_guardrail_applies_to_at_least_one_explicit_assignment_is_required", "Select every agent this guardrail applies to. At least one explicit assignment is required.")}</p>
            </div>
          </div>
            <AssignmentSelector
              emptyState={t("react.registry.display_no_assignment_agents", "No agents are available for assignment.")}
              loading={agentsQuery.isLoading}
              loadingLabel={t("react.registry.loading_agents", "Loading agents…")}
              message={selectedScope.length ? "" : t("react.registry.select_agent_before_saving", "Select at least one agent before saving.")}
              onSave={() => {
                ignoreAsyncResult(saveEntityPatch({ agents: selectedScope }));
              }}
              onSearchChange={setAgentSearch}
              onToggleRow={toggleScopeAgent}
              rows={agentRows}
              saveDisabled={!selectedScope.length}
              saveLabel={t("react.registry.display_save_agent_scope", "Save agent scope")}
              saving={saving}
              searchPlaceholder={t("react.registry.search_agents", "Search agents")}
              searchValue={agentSearch}
              selectedCount={selectedScope.length}
              selectedLabel="selected"
              totalCount={agentOptions.length}
              totalLabel="agents"
            />
            <div className="flex flex-wrap gap-2">
              <button
                className="fs-button"
                onClick={() => {
                  setScopeDraft(scopedAgents.join(", "));
                  setSaveStatus("");
                }}
                type="button"
              >
                {t("trash.reset", "Reset")}</button>
            </div>
        </div>
            ))}

            {renderWhen(activeTab === "changelog", (
        <RegistryEntityChangelogPanel
          entityId={resolvedEntityId || routeEntityId}
          entityType="guardrail"
          loading={historyQuery.isLoading}
          onRestored={async () => {
            await historyQuery.refetch();
            await detailQuery.refetch();
          }}
          rows={historyQuery.data || []}
        />
            ))}

          </div>
        </div>
      </section>
    </PageContainer>
  );
}
