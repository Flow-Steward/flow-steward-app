import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo, useState } from "react";

import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";

import {
  DsAlert,
  DsPageHeader,
  DsSelect,
  DsTabs,
  PageContainer,
} from "@/components/shared/design-system";
import { fetchJson } from "@/lib/http/client";
import { useKeyedState } from "@/lib/react/useKeyedState";
import { fetchRegistryList } from "@/pages/registry/shared/api";
import { BuilderMetadataEditor } from "@/pages/registry/shared/builder-metadata-editor";
import { useCatalogCategories } from "@/pages/registry/shared/catalog-categories";
import {
  builderMetadataDraftFromUnknown,
  builderMetadataPayload,
} from "@/pages/registry/shared/builder-metadata";
import { RegistryEntityChangelogPanel } from "@/pages/registry/shared/entity-changelog-panel";
import {
  firstRecordArray,
  firstStringArray,
  normalizeRecordList,
  normalizeStringList,
  useRegistryEntityDetail,
} from "@/pages/registry/shared/detail-helpers";
import { SaveErrorAlert, SaveStatusBadge } from "@/pages/registry/shared/save-feedback";
import {
  ContractEditorCard,
  type ContractDraftItem,
  type DataContractOption,
  emptyContractDraftItem,
  isStructuredContractValueType,
  normalizeCatalogFieldOptions,
  normalizeContractDraftItems,
} from "./contract-editor";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

function localizedSKILL_TABS(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { key: "config", label: t("react.registry.config", "Config") },
  { key: "builder", label: t("react.registry.builder", "Builder") },
  { key: "instructions", label: t("registry.skills.field.instructions_short", "Instructions") },
  { key: "io", label: "I/O" },
  { key: "agents", label: t("react.registry.agents_title", "Agents") },
  { key: "changelog", label: t("react.registry.changelog", "Changelog") },
] as const;
}

const SKILL_TABS = localizedSKILL_TABS();

type SkillTabKey = (typeof SKILL_TABS)[number]["key"];

const SKILL_TAB_KEYS = new Set<string>(SKILL_TABS.map((tab) => tab.key));

type SkillDraft = {
  antiUseCases: string;
  category: string;
  compositionRole: string;
  description: string;
  determinismLevel: string;
  displayName: string;
  instructions: string;
  purpose: string;
  safeForAutoUse: boolean;
  useCases: string;
};

function normalizeDataContractOptions(rows: Array<Record<string, unknown>>): DataContractOption[] {
  return rows
    .map((row) => {
      const id = firstStringFromUnknownValue([row.contract_id, row.id, ""]).trim();
      if (!id) {
        return null;
      }
      return {
        id,
        label: firstStringFromUnknownValue([row.display_name, id]).trim() || id,
      };
    })
    .filter(isTruthy);
}

export function RegistrySkillsDetailPage() {
  const { t } = useI18n();
  const categories = useCatalogCategories();
  // The URL owns the active section, so every tab stays directly addressable
  // and survives a reload. Same shape as agents/detail.
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") || "";
  const activeTab: SkillTabKey = SKILL_TAB_KEYS.has(requestedTab)
    ? (requestedTab as SkillTabKey)
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
  const [inputsStatus, setInputsStatus] = useState("");
  const [outputsStatus, setOutputsStatus] = useState("");
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
    endpoint: "/api/skills",
    idKey: "skill_id",
    idParam: "skillId",
    title: t("trash.type.registry_skill", "Skill"),
  });

  const skillId = resolvedEntityId || routeEntityId;
  const instructions = normalizeStringList(entity.instructions);
  const expectedInputs = firstRecordArray(entity, ["expected_inputs", "_inputs", "inputs"]);
  const expectedOutputs = firstRecordArray(entity, ["expected_outputs", "_outputs", "outputs"]);
  const usingAgentsQuery = useQuery({
    queryKey: ["/api/skills", "agents", skillId] as const,
    queryFn: async () => {
      const payload = await fetchJson<unknown>(`/api/skills/${encodeURIComponent(skillId)}/agents`);
      if (!Array.isArray(payload)) {
        return [];
      }
      return payload
        .map((row) => {
          if (typeof row === "string") {
            return row;
          }
          if (row && typeof row === "object") {
            return firstStringFromUnknownValue([
              (row as Record<string, unknown>).agent_id,
              "",
            ]).trim();
          }
          return "";
        })
        .filter(isTruthy);
    },
    enabled: Boolean(skillId),
    retry: false,
  });
  const skillInputsQuery = useQuery({
    queryKey: ["/api/skills", "inputs", skillId] as const,
    queryFn: async () => {
      const payload = await fetchJson<unknown>(`/api/skills/${encodeURIComponent(skillId)}/inputs`);
      return normalizeRecordList(payload);
    },
    enabled: Boolean(skillId),
    retry: false,
  });
  const skillOutputsQuery = useQuery({
    queryKey: ["/api/skills", "outputs", skillId] as const,
    queryFn: async () => {
      const payload = await fetchJson<unknown>(`/api/skills/${encodeURIComponent(skillId)}/outputs`);
      return normalizeRecordList(payload);
    },
    enabled: Boolean(skillId),
    retry: false,
  });
  const fieldCatalogQuery = useQuery({
    queryKey: ["/api/fields", "catalog"] as const,
    queryFn: () => fetchRegistryList("/api/fields"),
    retry: false,
  });
  const dataContractsQuery = useQuery({
    queryKey: ["/api/data-contracts", "catalog"] as const,
    queryFn: () => fetchRegistryList("/api/data-contracts"),
    retry: false,
  });

  const usingAgentsFromEntity = firstStringArray(entity, ["using_agents", "_agents", "agents"]);
  const usingAgents = (usingAgentsQuery.data || []).length ? usingAgentsQuery.data || [] : usingAgentsFromEntity;

  const inputDraftSeed = useMemo(
    () =>
      normalizeContractDraftItems(
        skillInputsQuery.data?.length ? skillInputsQuery.data : expectedInputs,
        false,
      ),
    [expectedInputs, skillInputsQuery.data],
  );
  const outputDraftSeed = useMemo(
    () =>
      normalizeContractDraftItems(
        skillOutputsQuery.data?.length ? skillOutputsQuery.data : expectedOutputs,
        true,
      ),
    [expectedOutputs, skillOutputsQuery.data],
  );
  const inputDraftKey = useMemo(
    () => `${skillId || "skill"}:inputs:${JSON.stringify(inputDraftSeed)}`,
    [inputDraftSeed, skillId],
  );
  const outputDraftKey = useMemo(
    () => `${skillId || "skill"}:outputs:${JSON.stringify(outputDraftSeed)}`,
    [outputDraftSeed, skillId],
  );
  const [inputDrafts, setInputDrafts] = useKeyedState<ContractDraftItem[]>(
    inputDraftKey,
    inputDraftSeed,
  );
  const [outputDrafts, setOutputDrafts] = useKeyedState<ContractDraftItem[]>(
    outputDraftKey,
    outputDraftSeed,
  );
  const builderMetadataDraft = useMemo(
    () =>
      builderMetadataDraftFromUnknown(
        entity.builder_metadata_jsonb || entity.builder_metadata,
      ),
    [entity.builder_metadata, entity.builder_metadata_jsonb],
  );

  const draftSeed = useMemo<SkillDraft>(
    () => ({
      antiUseCases: builderMetadataDraft.antiUseCases,
      category: firstStringFromUnknownValue([entity.category, ""]).trim(),
      compositionRole: builderMetadataDraft.compositionRole,
      description: firstStringFromUnknownValue([entity.description, ""]).trim(),
      determinismLevel: builderMetadataDraft.determinismLevel,
      displayName: firstStringFromUnknownValue([entity.display_name, entity.name, ""]).trim(),
      instructions: instructions.join("\n"),
      purpose: builderMetadataDraft.purpose,
      safeForAutoUse: builderMetadataDraft.safeForAutoUse,
      useCases: builderMetadataDraft.useCases,
    }),
    [
      builderMetadataDraft.antiUseCases,
      builderMetadataDraft.compositionRole,
      builderMetadataDraft.determinismLevel,
      builderMetadataDraft.purpose,
      builderMetadataDraft.safeForAutoUse,
      builderMetadataDraft.useCases,
      entity.category,
      entity.description,
      entity.display_name,
      entity.name,
      instructions,
    ],
  );
  const draftStateKey = useMemo(
    () => `${skillId || "skill"}:${JSON.stringify(draftSeed)}`,
    [draftSeed, skillId],
  );
  const [draft, setDraft] = useKeyedState<SkillDraft>(draftStateKey, draftSeed);

  const fieldOptions = useMemo(
    () => normalizeCatalogFieldOptions(fieldCatalogQuery.data || []),
    [fieldCatalogQuery.data],
  );
  const dataContractOptions = useMemo(
    () => normalizeDataContractOptions(dataContractsQuery.data || []),
    [dataContractsQuery.data],
  );

  async function saveContractItems(kind: "inputs" | "outputs") {
    const setContractStatus = kind === "inputs" ? setInputsStatus : setOutputsStatus;
    const contractQuery = kind === "inputs" ? skillInputsQuery : skillOutputsQuery;
    if (!skillId) {
      setContractStatus(t("react.registry.display_skill_id_missing", "Skill id is missing."));
      return;
    }

    const drafts = kind === "inputs" ? inputDrafts : outputDrafts;
    const filtered = drafts
      .map((item) => ({
        contract_id: isStructuredContractValueType(item.type) ? item.contract_id.trim() : "",
        description: item.description.trim(),
        name: item.name.trim(),
        required: item.required,
        type: item.type.trim() || "text",
      }))
      .filter((item) => Boolean(item.name));

    setContractStatus("");

    try {
      await fetchJson<unknown>(`/api/skills/${encodeURIComponent(skillId)}/${kind}`, {
        body: JSON.stringify(filtered),
        method: "PUT",
      });
      await Promise.all([
        detailQuery.refetch(),
        historyQuery.refetch(),
        contractQuery.refetch(),
      ]);
      setContractStatus("Saved.");
    } catch (error) {
      const message = error instanceof Error ? error.message : kind === "inputs"
        ? t("react.registry.display_failed_save_inputs", "Failed to save inputs.")
        : t("react.registry.display_failed_save_outputs", "Failed to save outputs.");
      setContractStatus(message);
    }
  }

  const detailErrorMessage =
    detailQuery.error instanceof Error
      ? detailQuery.error.message
      : t("react.registry.failed_to_load_skill_detail", "Failed to load skill detail.");

  const configPanel = (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{t("react.registry.skill_config", "Skill Config")}</h2>
        </div>
      </div>
      <div className="fs-form-grid">
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="skill-display-name">{t("users.modal.display_name", "Display Name")}</label>
          <input
            className="fs-input"
            id="skill-display-name"
            name="display_name"
            onChange={(event) =>
              setDraft((current) => ({ ...current, displayName: event.target.value }))
            }
            placeholder={t("react.registry.display_name", "Display name")}
            type="text"
            value={draft.displayName}
          />
        </div>
        <div className="fs-field">
          <span className="fs-field-label" id="skill-category-label">{t("trash.columns.category", "Category")}</span>
          <DsSelect
            ariaLabel={t("trash.columns.category", "Category")}
            id="skill-category"
            labelledBy="skill-category-label"
            onChange={(category) => setDraft((current) => ({ ...current, category }))}
            options={categories.options}
            placeholder={t("react.registry.select_category", "Select category")}
            value={draft.category}
          />
        </div>
        <div className="fs-field full">
          <label className="fs-field-label" htmlFor="skill-description">{t("registry.workflows.simulation.description", "Description")}</label>
          <textarea
            className="fs-textarea"
            id="skill-description"
            name="description"
            onChange={(event) =>
              setDraft((current) => ({ ...current, description: event.target.value }))
            }
            placeholder={t("registry.workflows.simulation.description", "Description")}
            value={draft.description}
          />
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <button
          className="fs-button primary"
          disabled={saving}
          onClick={() => {
            ignoreAsyncResult(saveEntityPatch({
              display_name: draft.displayName.trim(),
              name: draft.displayName.trim(),
              category: draft.category,
              description: draft.description.trim(),
            }));
          }}
          type="button"
        >
          {t("settings.extensions.save_config", "Save Config")}</button>
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
  );

  const builderPanel = (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{t("react.registry.builder_metadata", "Builder Metadata")}</h2>
          <p>
            {t("react.registry.technical_authoring_hints_used_for_workflow_authoring_when_this_skill_is_selected_as_part_of_a_workflow_plan", "Technical authoring hints used for workflow authoring when this skill is selected as part of a workflow plan.")}</p>
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
      <div className="flex flex-wrap gap-2">
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
  );

  const instructionsPanel = (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{t("registry.skills.field.instructions_short", "Instructions")}</h2>
          <p>{t("react.registry.one_instruction_per_line_in_execution_order", "One instruction per line, in execution order.")}</p>
        </div>
      </div>
      <div className="fs-field full">
        <label className="fs-field-label" htmlFor="skill-instructions">{t("registry.skills.field.instructions_short", "Instructions")}</label>
        <textarea
          className="fs-textarea min-h-56"
          id="skill-instructions"
          name="instructions"
          onChange={(event) =>
            setDraft((current) => ({ ...current, instructions: event.target.value }))
          }
          placeholder={t("react.registry.instruction_1", "Instruction 1...")}
          value={draft.instructions}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        <button
          className="fs-button primary"
          disabled={saving}
          onClick={() => {
            ignoreAsyncResult(saveEntityPatch({
              instructions: draft.instructions
                .split("\n")
                .map((line) => line.trim())
                .filter(isTruthy),
            }));
          }}
          type="button"
        >
          {t("react.registry.save_instructions", "Save instructions")}</button>
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
  );

  const ioPanel = (
    <>
      <ContractEditorCard
        addLabel={t("react.registry.display_add_input", "+ Add Input")}
        contractOptions={dataContractOptions}
        emptyMessage={t("react.registry.no_expected_inputs_defined_yet", "No expected inputs defined yet.")}
        fieldOptions={fieldOptions}
        items={inputDrafts}
        onAdd={() => {
          setInputDrafts((current) => [...current, emptyContractDraftItem(false)]);
          setInputsStatus("");
        }}
        onChange={(index, next) => {
          setInputDrafts((current) =>
            current.map((item, currentIndex) =>
              currentIndex === index ? { ...item, ...next } : item,
            ),
          );
        }}
        onRemove={(index) => {
          setInputDrafts((current) => current.filter((_, currentIndex) => currentIndex !== index));
          setInputsStatus("");
        }}
        onSave={() => {
          ignoreAsyncResult(saveContractItems("inputs"));
        }}
        requiredByDefault={false}
        saveLabel={t("react.registry.display_save_inputs", "Save Inputs")}
        saveStatus={inputsStatus}
        saving={saving || skillInputsQuery.isFetching}
        title={t("registry.workflow_detail.inputs.expected", "Expected Inputs")}
      />
      <ContractEditorCard
        addLabel={t("react.registry.display_add_output", "+ Add Output")}
        contractOptions={dataContractOptions}
        emptyMessage={t("react.registry.no_expected_outputs_defined_yet", "No expected outputs defined yet.")}
        fieldOptions={fieldOptions}
        items={outputDrafts}
        onAdd={() => {
          setOutputDrafts((current) => [...current, emptyContractDraftItem(true)]);
          setOutputsStatus("");
        }}
        onChange={(index, next) => {
          setOutputDrafts((current) =>
            current.map((item, currentIndex) =>
              currentIndex === index ? { ...item, ...next } : item,
            ),
          );
        }}
        onRemove={(index) => {
          setOutputDrafts((current) => current.filter((_, currentIndex) => currentIndex !== index));
          setOutputsStatus("");
        }}
        onSave={() => {
          ignoreAsyncResult(saveContractItems("outputs"));
        }}
        requiredByDefault={true}
        saveLabel={t("react.registry.display_save_outputs", "Save Outputs")}
        saveStatus={outputsStatus}
        saving={saving || skillOutputsQuery.isFetching}
        title={t("registry.skill_detail.io.expected_outputs", "Expected Outputs")}
      />
    </>
  );

  const agentsPanel = (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{t("registry.skill_detail.agents.title", "Agents Using This Skill")}</h2>
        </div>
      </div>
      {usingAgents.map((agentId) => (
        <div
          className="fs-surface-radius fs-surface-inset border border-subtle flex items-center justify-between gap-3 px-3 py-2"
          key={agentId}
        >
          <span>{agentId}</span>
          <Link className="fs-link" to={`/registry/agents/${encodeURIComponent(agentId)}`}>
            {t("settings.common.open", "Open")}</Link>
        </div>
      ))}
      {!usingAgents.length ? (
        <div className="fs-table-state">{t("react.registry.no_linked_agents_found", "No linked agents found.")}</div>
      ) : null}
    </div>
  );

  const changelogPanel = (
    <RegistryEntityChangelogPanel
      entityId={skillId}
      entityType="skill"
      loading={historyQuery.isLoading}
      onRestored={async () => {
        await historyQuery.refetch();
        await detailQuery.refetch();
        await skillInputsQuery.refetch();
        await skillOutputsQuery.refetch();
      }}
      rows={historyQuery.data || []}
    />
  );

  return (
    <PageContainer className="fs-page-sections" width="standard">
      <DsPageHeader
        action={
          <>
            <Link className="fs-button" to="/registry/skills">
              {t("react.registry.back_to_skills", "Back to skills")}</Link>
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
        description={t("react.registry.review_instructions_configuration_linked_agents_and_change_history_for_this_skill", "Review instructions, configuration, linked agents, and change history for this skill.")}
        eyebrow={t("trash.category.registry", "Registry")}
        title={entityName || t("react.registry.skill_detail", "Skill detail")}
      />
      <SaveErrorAlert error={saveError} />

      {detailQuery.isError ? (
        <DsAlert title={t("react.registry.failed_to_load_skill_detail_variant_0403403d", "Failed to load skill detail")} tone="danger">
          {detailErrorMessage}
        </DsAlert>
      ) : null}

      <section aria-label={t("react.registry.skill_detail", "Skill detail")} className="fs-settings-tabs">
        <DsTabs
          active={activeTab}
          ariaLabel={t("react.registry.skill_detail_sections", "Skill detail sections")}
          idPrefix="skill"
          items={localizedSKILL_TABS(t)}
          onSelect={goToTab}
        />
        <div
          aria-labelledby={`skill-tab-${activeTab}`}
          className="fs-tabs-panel"
          id={`skill-panel-${activeTab}`}
          role="tabpanel"
        >
          <div className="fs-panel-stack">
            {activeTab === "config" ? configPanel : null}
            {activeTab === "builder" ? builderPanel : null}
            {activeTab === "instructions" ? instructionsPanel : null}
            {activeTab === "io" ? ioPanel : null}
            {activeTab === "agents" ? agentsPanel : null}
            {activeTab === "changelog" ? changelogPanel : null}
          </div>
        </div>
      </section>
    </PageContainer>
  );
}
