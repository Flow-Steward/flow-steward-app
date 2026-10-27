import { useI18n } from "@/lib/i18n/useI18n";
import { AgentConfigPanel } from "./agent-config-panel";

import { Link } from "react-router";

import {
  DsAlert,
  DsBadge,
  DsCheckbox,
  DsPageHeader,
  DsSelect,
  DsTabs,
  PageContainer,
  Search,
} from "@/components/shared/design-system";

import { BuilderMetadataEditor } from "@/pages/registry/shared/builder-metadata-editor";

import { builderMetadataPayload } from "@/pages/registry/shared/builder-metadata";

import { RegistryEntityChangelogPanel } from "@/pages/registry/shared/entity-changelog-panel";

import { SaveErrorAlert, SaveStatusBadge } from "@/pages/registry/shared/save-feedback";
import {
  lineList,
  registryOptionId,
  registryOptionLabel,
  toggleLineValue,
} from "./agent-detail-helpers";

import { ContractPanel } from "./contract-panel";

import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

import { ignoreAsyncResult } from "@/lib/utils/async";

import { replaceAgentRelationships } from "./api";

import "./agents-detail.css";
import { renderWhen, renderChoice , localizedAGENT_TABS} from "./agent-detail-page-helpers";
import { useAgentDetailState } from "./agent-detail-state";

export function RegistryAgentsDetailPage() {
  const { t } = useI18n();
  const {
    saveError,
    saving,
    toggleEntityEnabled,
    entity,
    saveStatus,
    entityName,
    detailQuery,
    activeTab,
    goToTab,
    setDraft,
    draft,
    saveEntityPatch,
    prompts,
    draftSeed,
    setSaveStatus,
    lines,
    enabledSkillOptions,
    baseSkillDescription,
    selectedSecondarySkillIds,
    secondarySkillTotal,
    visibleSecondarySkillOptions,
    secondarySkillSearch,
    setSecondarySkillSearch,
    skillSaveNote,
    saveRelationshipOperation,
    resolvedEntityId,
    persistedSkillIds,
    persistedToolIds,
    setToolSearch,
    toolSearch,
    filteredToolOptions,
    tools,
    providerModels,
    allModels,
    providerOptions,
    providerLabels,
    availableAgentModels,
    agentRouteUnavailable,
    modelCatalogLoading,
    modelCatalogUnavailable,
    modelOptionsQuery,
    facts,
    agentConfig,
    expectedInputs,
    expectedOutputs,
    skills,
    routeEntityId,
    historyQuery,
  } = useAgentDetailState();

  return (
    <PageContainer className="fs-page-sections" width="standard">
      <DsPageHeader
        action={
          <>
            <Link className="fs-button" to="/registry/agents">
              {t("react.registry.back_to_agents", "Back to agents")}</Link>
            {/* No identifier chip in the header. #40 took the UUID off
                Guardrails detail; the decision that followed was to drop the
                identifier everywhere, readable or not. The id is in the URL,
                which is where anyone who needs it looks. */}
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
        description={t("react.registry.review_prompts_skills_tools_preview_data_and_change_history_for_this_agent", "Review prompts, skills, tools, preview data, and change history for this agent.")}
        eyebrow={t("trash.category.registry", "Registry")}
        title={entityName || t("react.registry.agent_detail", "Agent detail")}
      />
      <SaveErrorAlert error={saveError} />
      {renderWhen(detailQuery.isError, (
        <DsAlert title={t("monitoring.health.unavailable", "Unavailable")} tone="danger">
          {t("react.registry.the_agent_detail_could_not_be_loaded", "The agent detail could not be loaded.")}</DsAlert>
      ))}

      <section aria-label={t("react.registry.agent_detail", "Agent detail")} className="fs-settings-tabs">
        <DsTabs
          active={activeTab}
          ariaLabel={t("react.registry.agent_detail_sections", "Agent detail sections")}
          idPrefix="agent"
          items={localizedAGENT_TABS(t)}
          onSelect={goToTab}
        />
        <div
          aria-labelledby={`agent-tab-${activeTab}`}
          className="fs-tabs-panel"
          id={`agent-panel-${activeTab}`}
          role="tabpanel"
        >
          <div className="fs-panel-stack">

            {renderWhen(activeTab === "prompts", (
              <div className="fs-panel-group">
                <div className="fs-panel-heading">
                  <div>
                    <h2 className="fs-panel-title">{t("registry.agents.field.system_prompt", "System Prompt")}</h2>
                    <p>
                      {t("react.registry.canonical_prompt_contract_for_this_agent_identity_and_runtime_behavior", "Canonical prompt contract for this agent identity and runtime behavior.")}</p>
                  </div>
                </div>
                <div className="fs-field full">
                  <label className="fs-field-label" htmlFor="agent-system-prompt">
                    {t("registry.agents.field.system_prompt", "System Prompt")}</label>
                  <textarea
                    className="fs-textarea"
                    id="agent-system-prompt"
                    onChange={(event) =>
                      setDraft((current) => ({ ...current, prompt: event.target.value }))
                    }
                    placeholder={t("react.registry.you_are", "You are...")}
                    value={draft.prompt}
                  />
                </div>
                <div className="fs-field full">
                  <label className="fs-field-label" htmlFor="agent-context-template">
                    {t("registry.agent_detail.context_template.title", "Context Template")}</label>
                  <textarea
                    className="fs-textarea"
                    id="agent-context-template"
                    onChange={(event) =>
                      setDraft((current) => ({ ...current, contextTemplate: event.target.value }))
                    }
                    placeholder={t("react.registry.project_context", "project context...")}
                    value={draft.contextTemplate}
                  />
                  <span className="fs-field-helper">
                    {t("react.registry.optional_runtime_context_appended_to_this_agent_s_prompt_use_it_for_project_facts_boundaries_or_handoff_notes_", "Optional runtime context appended to this agent's prompt. Use it for project facts, boundaries, or handoff notes the agent should always see; leave it empty when the base skill already supplies the needed context.")}</span>
                </div>
                <div className="fs-badge-row">
                  <button
                    className="fs-button primary"
                    disabled={saving}
                    onClick={() => {
                      ignoreAsyncResult(saveEntityPatch({
                        prompts: {
                          ...prompts,
                          system: draft.prompt.trim(),
                          context_template: draft.contextTemplate.trim(),
                        },
                      }));
                    }}
                    type="button"
                  >
                    {t("react.registry.save_prompts", "Save prompts")}</button>
                  <button
                    className="fs-button"
                    onClick={() => {
                      setDraft(draftSeed);
                      setSaveStatus("");
                    }}
                    type="button"
                  >
                    {t("trash.reset", "Reset")}</button>
                </div>
              </div>
            ))}

            {renderWhen(activeTab === "skills", (
              <div className="fs-panel-group">
                <div className="fs-panel-heading">
                  <div>
                    <h2 className="fs-panel-title">{t("registry.agent_detail.skills.assigned", "Assigned Skills")}</h2>
                    {/* The description explains the distinction rather than
                        restating the two labels below it. */}
                    <p>
                      {t("react.registry.the_base_skill_defines_what_this_agent_does_secondary_skills_are_the_ones_it_may_reach_for_along_the_way", "The base skill defines what this agent does. Secondary skills are the ones it may reach for along the way.")}</p>
                  </div>
                </div>

                <div className="fs-field">
                  <span className="fs-field-label">{t("react.registry.base_skill", "Base skill")}</span>
                  {/* A chevron, not a magnifier: this control opens a list of
                      values to choose from, and a magnifier means "filter what
                      is below" — which is what the secondary field does, eight
                      lines down. Two identical pills for two different jobs was
                      the defect. */}
                  <DsSelect
                    ariaLabel={t("react.registry.base_skill", "Base skill")}
                    onChange={(value) =>
                      setDraft((current) => ({
                        ...current,
                        baseSkill: value,
                        secondarySkills: lines(current.secondarySkills)
                          .filter((skillId) => skillId !== value)
                          .join("\n"),
                      }))
                    }
                    options={[
                      { label: t("react.registry.no_base_skill", "No base skill"), value: "" },
                      ...enabledSkillOptions.map((row, index) => {
                        const skillId =
                          registryOptionId(row, ["skill_id", "id", "name"]) ||
                          `skill-${index + 1}`;
                        return { label: registryOptionLabel(row, skillId), value: skillId };
                      }),
                    ]}
                    value={draft.baseSkill}
                  />
                  {/* Metadata about a value sits under that value, not at the far
                      end of the panel behind the whole secondary list. */}
                  {renderChoice(Boolean(draft.baseSkill), (
                    <span className="fs-field-helper">
                      <span className="fs-table-mono">{draft.baseSkill}</span>
                      {" · "}
                      {baseSkillDescription || t("react.registry.no_description_yet", "No description yet.")}{" "}
                      <Link
                        className="fs-link"
                        to={`/registry/skills/${encodeURIComponent(draft.baseSkill)}`}
                      >
                        {t("react.registry.open_skill", "Open skill")}</Link>
                    </span>
                  ), (
                    <span className="fs-field-helper">
                      {t("react.registry.no_base_skill_selected_so_this_agent_has_no_defining_skill", "No base skill selected, so this agent has no defining skill.")}</span>
                  ))}
                </div>

                <div className="fs-skill-split" />

                <div className="fs-field full">
                  <div className="fs-panel-heading">
                    <div className="fs-badge-row">
                      <label className="fs-field-label" htmlFor="agent-secondary-skill-search">
                        {t("react.registry.secondary_skills", "Secondary skills")}</label>
                      <DsBadge>
                        {selectedSecondarySkillIds.length} / {secondarySkillTotal} {" "}{t("project_history.selected", "selected")}</DsBadge>
                    </div>
                    <div className="fs-badge-row">
                      <button
                        className="fs-button"
                        disabled={!visibleSecondarySkillOptions.some((option) => !option.selected)}
                        onClick={() =>
                          setDraft((current) => ({
                            ...current,
                            secondarySkills: Array.from(
                              new Set([
                                ...lines(current.secondarySkills),
                                ...visibleSecondarySkillOptions.map((option) => option.skillId),
                              ]),
                            ).join("\n"),
                          }))
                        }
                        type="button"
                      >
                        {secondarySkillSearch.trim()
                          ? t("react.registry.select_value0_shown", "Select {value0} shown", { value0: visibleSecondarySkillOptions.length })
                          : t("react.registry.select_all", "Select all")}
                      </button>
                      <button
                        className="fs-button"
                        disabled={!selectedSecondarySkillIds.length}
                        onClick={() =>
                          setDraft((current) => ({ ...current, secondarySkills: "" }))
                        }
                        type="button"
                      >
                        {t("settings.common.clear", "Clear")}</button>
                      <span className="fs-search">
                        <Search aria-hidden="true" />
                        <input
                          id="agent-secondary-skill-search"
                          onChange={(event) => setSecondarySkillSearch(event.target.value)}
                          placeholder={t("react.registry.filter_skills_variant_f47dd0b5", "Filter skills")}
                          value={secondarySkillSearch}
                        />
                      </span>
                    </div>
                  </div>
                  <div className="fs-surface-radius fs-surface-inset border border-subtle p-3 fs-skill-picker">
                    {renderChoice(Boolean(visibleSecondarySkillOptions.length), (
                      <div className="fs-control-grid">
                        {visibleSecondarySkillOptions.map((option) => (
                          <DsCheckbox
                            checked={option.selected}
                            key={option.skillId}
                            onChange={() =>
                              setDraft((current) => ({
                                ...current,
                                secondarySkills: toggleLineValue(
                                  current.secondarySkills,
                                  option.skillId,
                                ),
                              }))
                            }
                          >
                            <span className="fs-field">
                              <span>{option.label}</span>
                              <span className="fs-field-helper fs-table-mono">
                                {option.skillId}
                              </span>
                            </span>
                          </DsCheckbox>
                        ))}
                      </div>
                    ), (
                      <p className="fs-empty-copy">
                        {secondarySkillSearch.trim()
                          ? t("react.registry.no_skill_matches_value0", "No skill matches “{value0}”.", { value0: secondarySkillSearch.trim() })
                          : t("react.registry.no_secondary_skills_available", "No secondary skills available.")}
                      </p>
                    ))}
                  </div>
                </div>

                {/* Save belongs to the surface it acts on, with a note stating
                    the outcome in the user's terms. */}
                <div className="fs-card-footer">
                  <span className="fs-footer-status">{skillSaveNote}</span>
                  <div className="fs-badge-row">
                    <button
                      className="fs-button"
                      onClick={() => {
                        setDraft(draftSeed);
                        setSaveStatus("");
                      }}
                      type="button"
                    >
                      {t("extensions.repeater.cancel", "Cancel")}</button>
                    <button
                      className="fs-button primary"
                      disabled={saving}
                      onClick={() => {
                        ignoreAsyncResult(
                          saveRelationshipOperation(() =>
                            replaceAgentRelationships(
                              resolvedEntityId,
                              { skills: persistedSkillIds, tools: persistedToolIds },
                              {
                                skills: [draft.baseSkill, ...lines(draft.secondarySkills)].filter(
                                  isTruthy,
                                ),
                                tools: persistedToolIds,
                              },
                            ),
                          ),
                        );
                      }}
                      type="button"
                    >
                      {t("react.registry.save_skills", "Save skills")}</button>
                  </div>
                </div>
              </div>
            ))}

            {renderWhen(activeTab === "tools", (
              <div className="fs-panel-group">
                <div className="fs-panel-heading">
                  <div>
                    <h2 className="fs-panel-title">{t("react.registry.tool_access", "Tool Access")}</h2>
                    <p>{t("react.registry.direct_and_effective_runtime_tools_visible_to_this_agent", "Direct and effective runtime tools visible to this agent.")}</p>
                  </div>
                </div>
                <div className="fs-field full">
                  <div className="fs-panel-heading">
                    <label className="fs-field-label" htmlFor="agent-tool-search">
                      {t("react.registry.allowed_tools", "Allowed tools")}</label>
                    <span className="fs-search">
                      <Search aria-hidden="true" />
                      <input
                        id="agent-tool-search"
                        onChange={(event) => setToolSearch(event.target.value)}
                        placeholder={t("react.registry.search_tools", "Search tools")}
                        value={toolSearch}
                      />
                    </span>
                  </div>
                  <div className="fs-surface-radius fs-surface-inset border border-subtle p-3">
                    {renderChoice(Boolean(filteredToolOptions.length), (
                      <div className="fs-control-grid">
                        {filteredToolOptions.map((row, index) => {
                          const toolId =
                            registryOptionId(row, ["tool_id", "id", "name"]) ||
                            `tool-${index + 1}`;
                          return (
                            <DsCheckbox
                              checked={lineList(draft.tools).includes(toolId)}
                              key={toolId}
                              onChange={() =>
                                setDraft((current) => ({
                                  ...current,
                                  tools: toggleLineValue(current.tools, toolId),
                                }))
                              }
                            >
                              <span className="fs-field">
                                <span>{registryOptionLabel(row, toolId)}</span>
                                <span className="fs-field-helper fs-table-mono">{toolId}</span>
                              </span>
                            </DsCheckbox>
                          );
                        })}
                      </div>
                    ), (
                      <p className="fs-empty-copy">{t("react.registry.no_tools_match_this_filter", "No tools match this filter.")}</p>
                    ))}
                  </div>
                </div>
                {renderChoice(Boolean(tools.length), (
                  <dl className="fs-kv is-bare">
                    {tools.map((row, index) => {
                      const toolId = firstStringFromUnknownValue([
                        row.tool_id,
                        row.id,
                        row.name,
                        `tool-${index + 1}`,
                      ]);
                      return (
                        <div className="fs-kv-row" key={toolId}>
                          <dt>{toolId}</dt>
                          <dd className="is-plain">
                            {firstStringFromUnknownValue([
                              row.display_name,
                              row.description,
                              row.source,
                              t("react.registry.display_no_description_period", "No description."),
                            ])}{" "}
                            <Link
                              className="fs-link"
                              to={`/registry/tools/${encodeURIComponent(toolId)}`}
                            >
                              {t("settings.common.open", "Open")}</Link>
                          </dd>
                        </div>
                      );
                    })}
                  </dl>
                ), (
                  <div className="fs-table-state">
                    <strong>{t("react.registry.no_tools_assigned", "No tools assigned.")}</strong>
                  </div>
                ))}
                <div className="fs-badge-row">
                  <button
                    className="fs-button primary"
                    disabled={saving}
                    onClick={() => {
                      ignoreAsyncResult(
                        saveRelationshipOperation(() =>
                          replaceAgentRelationships(
                            resolvedEntityId,
                            { skills: persistedSkillIds, tools: persistedToolIds },
                            { skills: persistedSkillIds, tools: lines(draft.tools) },
                          ),
                        ),
                      );
                    }}
                    type="button"
                  >
                    {t("react.registry.save_tools", "Save tools")}</button>
                </div>
              </div>
            ))}

            {renderWhen(activeTab === "config", <AgentConfigPanel
                setDraft={setDraft}
                draft={draft}
                providerModels={providerModels}
                allModels={allModels}
                providerOptions={providerOptions}
                providerLabels={providerLabels}
                availableAgentModels={availableAgentModels}
                agentRouteUnavailable={agentRouteUnavailable}
                modelCatalogLoading={modelCatalogLoading}
                modelCatalogUnavailable={modelCatalogUnavailable}
                modelOptionsQuery={modelOptionsQuery}
                facts={facts}
                saving={saving}
                saveEntityPatch={saveEntityPatch}
                agentConfig={agentConfig}
                draftSeed={draftSeed}
                setSaveStatus={setSaveStatus}
              />)}

            {renderWhen(activeTab === "builder", (
              <div className="fs-panel-group">
                <div className="fs-panel-heading">
                  <div>
                    <h2 className="fs-panel-title">{t("react.registry.builder_metadata", "Builder Metadata")}</h2>
                    <p>
                      {t("react.registry.authoring_guidance_used_for_workflow_authoring_when_this_agent_is_ranked_as_a_workflow_primitive", "Authoring guidance used for workflow authoring when this agent is ranked as a workflow primitive.")}</p>
                  </div>
                </div>
                <BuilderMetadataEditor
                  draft={{
                    antiUseCases: draft.antiUseCases,
                    compositionRole: draft.compositionRole,
                    determinismLevel: draft.determinismLevel,
                    purpose: draft.purpose,
                    safeForAutoUse: draft.safeForAutoUse,
                    useCases: draft.useCases,
                  }}
                  onChange={(next) =>
                    setDraft((current) => ({
                      ...current,
                      antiUseCases: next.antiUseCases,
                      compositionRole: next.compositionRole,
                      determinismLevel: next.determinismLevel,
                      purpose: next.purpose,
                      safeForAutoUse: next.safeForAutoUse,
                      useCases: next.useCases,
                    }))
                  }
                />
                <div className="fs-badge-row">
                  <button
                    className="fs-button primary"
                    disabled={saving}
                    onClick={() => {
                      ignoreAsyncResult(saveEntityPatch({
                        builder_metadata_jsonb: builderMetadataPayload({
                          antiUseCases: draft.antiUseCases,
                          compositionRole: draft.compositionRole,
                          determinismLevel: draft.determinismLevel,
                          purpose: draft.purpose,
                          safeForAutoUse: draft.safeForAutoUse,
                          useCases: draft.useCases,
                        }),
                      }));
                    }}
                    type="button"
                  >
                    {t("react.registry.save_builder_metadata", "Save Builder metadata")}</button>
                  <button
                    className="fs-button"
                    onClick={() => {
                      setDraft(draftSeed);
                      setSaveStatus("");
                    }}
                    type="button"
                  >
                    {t("trash.reset", "Reset")}</button>
                </div>
              </div>
            ))}

            {renderWhen(activeTab === "io", (
              <>
                <ContractPanel
                  emptyMessage={t("react.registry.no_expected_inputs_defined", "No expected inputs defined.")}
                  rows={expectedInputs}
                  title={t("registry.workflow_detail.inputs.expected", "Expected Inputs")}
                />
                <ContractPanel
                  emptyMessage={t("react.registry.no_expected_outputs_defined", "No expected outputs defined.")}
                  rows={expectedOutputs}
                  title={t("registry.skill_detail.io.expected_outputs", "Expected Outputs")}
                />
              </>
            ))}

            {renderWhen(activeTab === "preview", (
              <div className="fs-panel-group">
                <div className="fs-panel-heading">
                  <div>
                    <h2 className="fs-panel-title">{t("registry.agent_detail.preview.title", "Prompt Preview")}</h2>
                    <p>
                      {t("react.registry.preview_of_key_runtime_prompt_material_before_running_this_agent_in_workflows", "Preview of key runtime prompt material before running this agent in workflows.")}</p>
                  </div>
                </div>
                <dl className="fs-kv is-bare">
                  <div className="fs-kv-row">
                    <dt>{t("jobs.columns.summary", "Summary")}</dt>
                    <dd className="is-plain">
                      {skills.length} {" "}{t("react.registry.skills_attached", "skills attached,")}{" "}{tools.length} {" "}{t("react.registry.tools_attached", "tools attached.")}</dd>
                  </div>
                </dl>
                {/* A local composition, not a component: `codeBlock` is still a
                    brief-only placeholder upstream, and DsLogOutput is scoped to
                    machine-produced text, which an authored prompt is not. */}
                <pre className="fs-surface-radius fs-surface-inset fs-preview-scroll">
                  {draft.prompt || t("react.registry.display_empty_system_prompt", "(empty system prompt)")}
                </pre>
              </div>
            ))}

            {renderWhen(activeTab === "changelog", (
              <RegistryEntityChangelogPanel
                entityId={resolvedEntityId || routeEntityId}
                entityType="agent"
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
