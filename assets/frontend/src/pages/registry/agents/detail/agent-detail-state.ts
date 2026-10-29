import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo, useState } from "react";

import { useSearchParams } from "react-router";

import { useQuery } from "@tanstack/react-query";

import { fetchJson } from "@/lib/http/client";

import { useKeyedState } from "@/lib/react/useKeyedState";

import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";

import { fetchRegistryList } from "@/pages/registry/shared/api";

import { builderMetadataDraftFromUnknown } from "@/pages/registry/shared/builder-metadata";

import {
  firstObject,
  firstRecordArray,
  useRegistryEntityDetail,
} from "@/pages/registry/shared/detail-helpers";

import {
  type ModelSelectionOptions,
  isStandardToolAction,
  lineList,
  modelOptionsForProvider,
  normalizeModelList,
  normalizeProviderModels,
  registryOptionId,
  registryOptionLabel,
  textOrEmpty,
} from "./agent-detail-helpers";

import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

import { normalizeProviderOptionLabels } from "@/lib/utils/provider-options";

import {
  buildSkillSaveNote,
  type AgentTabKey,
  type AgentDraft,
  AGENT_TAB_KEYS,
  factText,
  routeIsUnavailable,
} from "./agent-detail-page-helpers";

export function useAgentDetailState() {
  const { t } = useI18n();
  // The URL owns the active section, so every tab stays directly addressable
  // and survives a reload. Same shape as pages/settings/account.
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") || "";
  const activeTab: AgentTabKey = AGENT_TAB_KEYS.has(requestedTab)
    ? (requestedTab as AgentTabKey)
    : "prompts";
  function goToTab(key: string) {
    const next = new URLSearchParams(searchParams);
    if (key === "prompts") {
      next.delete("tab");
    } else {
      next.set("tab", key);
    }
    setSearchParams(next, { replace: true });
  }
  const [secondarySkillSearch, setSecondarySkillSearch] = useState("");
  const [toolSearch, setToolSearch] = useState("");
  const userTimezone = useUserTimezone();
  const {
    detailQuery,
    entity,
    entityName,
    historyQuery,
    resolvedEntityId,
    routeEntityId,
    saveEntityOperation,
    saveEntityPatch,
    saveError,
    saveStatus,
    saving,
    setSaveStatus,
    toggleEntityEnabled,
  } = useRegistryEntityDetail({
    endpoint: "/api/agents",
    idKey: "agent_id",
    idParam: "agentId",
    title: t("react.registry.agent", "Agent"),
    updateMode: "patch",
  });

  const prompts = firstObject(entity, ["prompts"]);
  const skills = firstRecordArray(entity, [
    "skills",
    "_skills",
    "agent_skills",
  ]);
  const tools = firstRecordArray(entity, [
    "tools",
    "_tools",
    "agent_tools",
    "effective_tools",
  ]);
  const skillsCatalogQuery = useQuery({
    queryKey: ["registry", "agent-detail", "skills-catalog"],
    queryFn: () => fetchRegistryList("/api/skills"),
    retry: false,
  });
  const toolsCatalogQuery = useQuery({
    queryKey: ["registry", "agent-detail", "tools-catalog"],
    queryFn: () => fetchRegistryList("/api/tools"),
    retry: false,
  });
  const modelOptionsQuery = useQuery<ModelSelectionOptions>({
    queryKey: ["model-selection", "options"],
    queryFn: () => fetchJson<ModelSelectionOptions>("/api/model-selection/options"),
    retry: false,
  });
  const skillOptions = Array.isArray(skillsCatalogQuery.data)
    ? skillsCatalogQuery.data
    : skills;
  const toolOptions = Array.isArray(toolsCatalogQuery.data)
    ? toolsCatalogQuery.data
    : tools;
  const enabledSkillOptions = skillOptions.filter((row) => row.enabled !== false);
  const enabledToolOptions = toolOptions.filter((row) => row.enabled !== false);
  const filteredToolOptions = enabledToolOptions.filter((row, index) => {
    const toolId = registryOptionId(row, ["tool_id", "id", "name"]) || `tool-${index + 1}`;
    const label = registryOptionLabel(row, toolId);
    const needle = toolSearch.trim().toLowerCase();
    return !needle || `${toolId} ${label}`.toLowerCase().includes(needle);
  });
  const expectedInputs = firstRecordArray(entity, [
    "expected_inputs",
    "_inputs",
    "inputs",
  ]);
  const expectedOutputs = firstRecordArray(entity, [
    "required_outputs",
    "expected_outputs",
    "_outputs",
    "outputs",
  ]);
  const allPersistedSkillIds = skills
    .map((row) => firstStringFromUnknownValue([row.skill_id, row.id, row.name, ""]).trim())
    .filter(isTruthy);
  const persistedBaseSkill = firstStringFromUnknownValue([
    entity.base_skill_id,
    entity.base_skill,
    allPersistedSkillIds[0],
    "",
  ]).trim();
  const skillIdsDraft = allPersistedSkillIds
    .filter((skillId) => skillId !== persistedBaseSkill)
    .join("\n");
  const toolIdsDraft = tools
    .filter((row) => {
      const toolId = registryOptionId(row, ["tool_id", "id", "name"]);
      return !isStandardToolAction(row, toolId);
    })
    .map((row) => registryOptionId(row, ["tool_id", "id", "name"]))
    .filter(isTruthy)
    .join("\n");
  const persistedSkillIds = allPersistedSkillIds;
  const persistedToolIds = lines(toolIdsDraft);
  const builderMetadataDraft = builderMetadataDraftFromUnknown(
    entity.builder_metadata_jsonb || entity.builder_metadata,
  );
  const agentConfig = firstObject(entity, ["config"]);
  const providerModels = useMemo(
    () => normalizeProviderModels(modelOptionsQuery.data?.provider_models),
    [modelOptionsQuery.data],
  );
  const allModels = useMemo(
    () => normalizeModelList(modelOptionsQuery.data?.all_models),
    [modelOptionsQuery.data],
  );
  const providerLabels = useMemo(
    () => normalizeProviderOptionLabels(modelOptionsQuery.data?.provider_options, modelOptionsQuery.data?.provider_labels),
    [modelOptionsQuery.data],
  );
  const draftSeed: AgentDraft = {
    agentType: firstStringFromUnknownValue([entity.agent_type_key, entity.agent_type_id, ""]).trim(),
    baseSkill: persistedBaseSkill,
    category: firstStringFromUnknownValue([entity.category, ""]).trim(),
    compositionRole: builderMetadataDraft.compositionRole,
    contextTemplate: firstStringFromUnknownValue([prompts.context_template, ""]).trim(),
    description: firstStringFromUnknownValue([entity.description, ""]).trim(),
    determinismLevel: builderMetadataDraft.determinismLevel,
    displayName: firstStringFromUnknownValue([entity.display_name, entity.name, ""]).trim(),
    model: textOrEmpty(agentConfig.model),
    prompt: firstStringFromUnknownValue([prompts.system, entity.prompt, ""]).trim(),
    antiUseCases: builderMetadataDraft.antiUseCases,
    provider: textOrEmpty(agentConfig.provider),
    purpose: builderMetadataDraft.purpose,
    safeForAutoUse: builderMetadataDraft.safeForAutoUse,
    secondarySkills: skillIdsDraft,
    tools: toolIdsDraft,
    useCases: builderMetadataDraft.useCases,
  };
  const draftStateKey = `${resolvedEntityId || routeEntityId || "agent"}:${JSON.stringify(draftSeed)}`;
  const [draft, setDraft] = useKeyedState<AgentDraft>(draftStateKey, draftSeed);
  const saveRelationshipOperation = async (operation: () => Promise<unknown>) => {
    const saved = await saveEntityOperation(operation);
    if (!saved) {
      setDraft(draftSeed);
    }
  };
  const providerOptions = useMemo(
    () =>
      Array.from(
        new Set([draft.provider, ...Object.keys(providerModels)].filter(isTruthy)),
      ).sort((left, right) => left.localeCompare(right)),
    [draft.provider, providerModels],
  );
  const availableAgentModels = useMemo(
    () => modelOptionsForProvider(providerModels, allModels, draft.provider, draft.model),
    [allModels, draft.model, draft.provider, providerModels],
  );
  const providerValue = textOrEmpty(draft.provider);
  const modelValue = textOrEmpty(draft.model);
  const modelCatalogUnavailable = modelOptionsQuery.isError && !modelOptionsQuery.data;
  const modelCatalogLoading = modelOptionsQuery.isLoading && !modelOptionsQuery.data;
  const agentRouteUnavailable = routeIsUnavailable({
    catalogUnavailable: modelCatalogUnavailable,
    catalogLoading: modelCatalogLoading,
    provider: providerValue,
    model: modelValue,
    providerModels,
    allModels,
  });
  const selectedSecondarySkillIds = lineList(draft.secondarySkills);
  const secondarySkillNeedle = secondarySkillSearch.trim().toLowerCase();
  const visibleSecondarySkillOptions = enabledSkillOptions
    .map((row, index) => {
      const skillId =
        registryOptionId(row, ["skill_id", "id", "name"]) ||
        `skill-${index + 1}`;
      const label = registryOptionLabel(row, skillId);
      const selected = selectedSecondarySkillIds.includes(skillId);
      const matches =
        !secondarySkillNeedle ||
        `${skillId} ${label}`.toLowerCase().includes(secondarySkillNeedle);
      return {
        label,
        selected,
        skillId,
        visible: skillId !== draft.baseSkill && (selected || matches),
      };
    })
    .filter((option) => option.visible)
    .sort((left, right) => {
      if (left.selected !== right.selected) {
        return left.selected ? -1 : 1;
      }
      return left.label.localeCompare(right.label);
    });

  // Four rows, not six. Category and Type were read-only copies of the editable
  // inputs eight lines above them in the same panel, and a value the user edits
  // here is a form field, not a fact.
  // The base skill's own facts, so they can sit under its own field instead of
  // stranded at the far end of the panel.
  const baseSkillRow = skillOptions.find(
    (row) => registryOptionId(row, ["skill_id", "id", "name"]) === draft.baseSkill,
  );
  const baseSkillDescription = baseSkillRow
    ? firstStringFromUnknownValue([baseSkillRow.description, baseSkillRow.summary, ""]).trim()
    : "";
  const baseSkillLabel = baseSkillRow
    ? registryOptionLabel(baseSkillRow, draft.baseSkill)
    : draft.baseSkill;
  // Every enabled skill except the base one: the base is excluded from the list
  // outright rather than shown as a disabled row, so the count and the rows
  // always agree.
  const secondarySkillTotal = enabledSkillOptions.filter(
    (row, index) =>
      (registryOptionId(row, ["skill_id", "id", "name"]) || `skill-${index + 1}`) !==
      draft.baseSkill,
  ).length;
  const skillSaveNote = buildSkillSaveNote(
    baseSkillLabel,
    Boolean(draft.baseSkill),
    selectedSecondarySkillIds.length, t
  );

  const facts: Array<[string, string]> = [
    [t("react.registry.identifier", "Identifier"), factText(resolvedEntityId, t)],
    [t("settings.mcp.status.enabled", "Enabled"), isTruthy(entity.enabled) ? t("extensions.repeater.yes", "Yes") : t("extensions.repeater.no", "No")],
    [t("registry.data_contracts.column.version", "Version"), factText(entity.version, t)],
    [
      t("react.workflows.updated", "Updated"),
      factText(
        formatUserDateTime(entity.updated_at || entity.last_updated_at, userTimezone), t
      ),
    ],
  ];

  function lines(value: string): string[] {
    return value
      .split(/\r?\n|,/)
      .map((item) => item.trim())
      .filter(isTruthy);
  }
  return {
    saving,
    toggleEntityEnabled,
    entity,
    saveError,
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
  };
}
