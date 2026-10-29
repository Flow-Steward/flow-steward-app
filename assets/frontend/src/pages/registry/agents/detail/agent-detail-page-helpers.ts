import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { type ReactNode } from "react";

import {
  compatibleModelOptionsForProvider,
  type normalizeModelList,
  type normalizeProviderModels,
} from "./agent-detail-helpers";

import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export function buildSkillSaveNote(baseSkillLabel: string, hasBaseSkill: boolean, secondaryCount: number, t: I18nContextValue["t"] = fallbackContext.t): string {
  const secondaryLabel = secondaryCount === 1
    ? t("react.registry.count_secondary_skill", "{count} secondary skill", { count: secondaryCount })
    : t("react.registry.count_secondary_skills", "{count} secondary skills", { count: secondaryCount });
  if (hasBaseSkill && secondaryCount) return t("react.registry.display_saves_base_plus_secondary", "Saves {base} as base, plus {secondary}.", { base: baseSkillLabel, secondary: secondaryLabel });
  if (hasBaseSkill) return t("react.registry.display_saves_base_only", "Saves {base} as base, with no secondary skills.", { base: baseSkillLabel });
  if (secondaryCount) return t("react.registry.display_saves_secondary_only", "Saves no base skill, plus {secondary}.", { secondary: secondaryLabel });
  return t("react.registry.saves_no_base_skill_and_no_secondary_skills", "Saves no base skill and no secondary skills.");
}

export function localizedAGENT_TABS(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { key: "prompts", label: t("react.registry.prompts", "Prompts") },
  { key: "skills", label: t("react.registry.skills", "Skills") },
  { key: "tools", label: t("react.registry.tools", "Tools") },
  { key: "config", label: t("react.registry.config", "Config") },
  { key: "builder", label: t("react.registry.builder", "Builder") },
  { key: "io", label: "I/O" },
  { key: "preview", label: t("registry.workflow_detail.run_lab.tab.preview", "Preview") },
  { key: "changelog", label: t("react.registry.changelog", "Changelog") },
] as const;
}

export const AGENT_TABS = localizedAGENT_TABS();

export type AgentTabKey = (typeof AGENT_TABS)[number]["key"];

export type AgentDraft = {
  baseSkill: string;
  category: string;
  compositionRole: string;
  contextTemplate: string;
  description: string;
  determinismLevel: string;
  displayName: string;
  model: string;
  prompt: string;
  antiUseCases: string;
  provider: string;
  purpose: string;
  safeForAutoUse: boolean;
  secondarySkills: string;
  tools: string;
  useCases: string;
};

export const AGENT_TAB_KEYS = new Set<string>(AGENT_TABS.map((tab) => tab.key));

/**
 * A key-value list never leaves a cell blank and never prints a bare dash.
 * `normalizePrimitive` returns "—" and has fourteen call sites across five
 * files, so the phrasing is decided here rather than there.
 */
export function factText(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  const text = firstStringFromUnknownValue([value, ""]).trim();
  return text && text !== "—" ? text : t("react.registry.not_set", "Not set");
}

export function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

export function renderChoice(condition: boolean, yes: ReactNode, no: ReactNode): ReactNode {
  return condition ? yes : no;
}

export function routeIsUnavailable({
  catalogUnavailable,
  catalogLoading,
  provider,
  model,
  providerModels,
  allModels,
}: Readonly<{
  catalogUnavailable: boolean;
  catalogLoading: boolean;
  provider: string;
  model: string;
  providerModels: ReturnType<typeof normalizeProviderModels>;
  allModels: ReturnType<typeof normalizeModelList>;
}>): boolean {
  if (catalogUnavailable || catalogLoading || (!provider && !model)) return false;
  if (!provider || !model) return true;
  return !compatibleModelOptionsForProvider(providerModels, allModels, provider).some(
    (option) => option.modelId === model,
  );
}
