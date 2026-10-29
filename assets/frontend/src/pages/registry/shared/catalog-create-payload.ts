import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { ReactNode } from "react";
import { normalizeString, parseLines, type EntityForm, type RegistryEntity } from "./catalog-surface-helpers";

const SLUG_IDENTIFIER_PATTERN = /^[a-z][a-z0-9_]*$/;
const DOTTED_IDENTIFIER_PATTERN = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$/;

function identifierLabel(label: string, t: I18nContextValue["t"]): string {
  if (label === "Agent ID") return t("react.registry.display_agent_id", "Agent ID");
  if (label === "Tool ID") return t("react.registry.display_tool_id", "Tool ID");
  if (label === "Skill ID") return t("react.registry.display_skill_id", "Skill ID");
  return label;
}

function requireSlugIdentifier(value: unknown, label: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const identifier = normalizeString(value);
  if (!identifier) {
    throw new Error(t("react.workflows.value0_is_required", "{value0} is required.", { value0: identifierLabel(label, t) }));
  }
  if (!SLUG_IDENTIFIER_PATTERN.test(identifier)) {
    throw new Error(t("react.registry.display_slug_identifier_rule", "{label} must start with a lowercase letter and use only lowercase letters, numbers, and underscores.", { label: identifierLabel(label, t) }));
  }
  return identifier;
}

function requireIdentifier(value: unknown, label: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const identifier = normalizeString(value);
  if (!identifier) {
    throw new Error(t("react.workflows.value0_is_required", "{value0} is required.", { value0: identifierLabel(label, t) }));
  }
  return identifier;
}

function requireDottedIdentifier(value: unknown, label: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const identifier = requireIdentifier(value, label, t);
  if (!DOTTED_IDENTIFIER_PATTERN.test(identifier)) {
    throw new Error(t("react.registry.display_dotted_identifier_rule", "{label} must start with a lowercase letter and use only lowercase letters, numbers, underscores, and dots between segments.", { label: identifierLabel(label, t) }));
  }
  return identifier;
}

export function cloneIdentifier(endpoint: string, cloneId: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (endpoint === "/api/agents") return requireSlugIdentifier(cloneId, "Agent ID", t);
  if (endpoint === "/api/tools") return requireDottedIdentifier(cloneId, "Tool ID", t);
  if (endpoint === "/api/skills") return requireDottedIdentifier(cloneId, "Skill ID", t);
  return requireIdentifier(cloneId, "ID", t);
}

export function cloneBodyKey(endpoint: string, fallbackKey: string): string {
  if (endpoint === "/api/agents") return "agent_id";
  if (endpoint === "/api/tools") return "tool_id";
  if (endpoint === "/api/skills") return "skill_id";
  return fallbackKey;
}

function requireDisplayName(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  const displayName = normalizeString(value);
  if (!displayName) {
    throw new Error(t("react.registry.display_name_is_required", "Display name is required."));
  }
  return displayName;
}

export type AgentModelRoute = {
  provider: string;
  modelId: string;
};

export type CreatePayloadContext = {
  agentModelRoute: string;
  agentModelOptions: AgentModelRoute[];
  agentSystemPrompt: string;
  category: string;
  createForm: EntityForm;
  endpoint: string;
  guardrailAgentIds: string[];
  guardrailConfigJson: string;
  guardrailMode: "deterministic" | "semantic";
  guardrailPromptText: string;
  guardrailRuleType: string;
  guardrailSeverity: "error" | "warning";
  idKey: string;
  skillInstructions: string;
  toolImplementationType: string;
  toolPrompt: string;
};

function requireCategory(value: string, t: I18nContextValue["t"]) {
  const category = normalizeString(value);
  if (!category) throw new Error(t("react.registry.category_is_required", "Category is required."));
  return category;
}

function buildAgentPayload(basePayload: RegistryEntity, context: CreatePayloadContext, t: I18nContextValue["t"] = fallbackContext.t): RegistryEntity {
  const category = requireCategory(context.category, t);
  const resolvedSystemPrompt = normalizeString(context.agentSystemPrompt);
  if (!resolvedSystemPrompt) throw new Error(t("react.registry.system_prompt_is_required", "System prompt is required."));
  const resolvedAgentId = requireSlugIdentifier(context.createForm.identifier, "Agent ID", t);
  const selectedRoute = context.agentModelOptions.find(
    (route) => `${route.provider}::${route.modelId}` === context.agentModelRoute,
  );
  if (!selectedRoute) throw new Error(t("react.registry.select_a_verified_model_route_or_agent_executor", "Select a verified model route or agent executor."));
  return {
    ...basePayload,
    agent_id: resolvedAgentId,
    display_name: normalizeString(context.createForm.display_name) || resolvedAgentId,
    description: normalizeString(context.createForm.description),
    category,
    prompts: { system: resolvedSystemPrompt, skill_prompts: {}, context_template: "" },
    skills: [],
    tools: [],
    inputs: { required: [], optional: [] },
    outputs: [],
    depends_on: { soft: [], hard: [] },
    config: {
      provider: selectedRoute.provider,
      model: selectedRoute.modelId,
      max_iterations: 5,
      timeout_minutes: 10,
    },
    quality: { retry_on_failure: true },
    enabled: Boolean(context.createForm.enabled),
    version: "1.0.0",
    tags: [],
  };
}

function buildToolPayload(basePayload: RegistryEntity, context: CreatePayloadContext, t: I18nContextValue["t"] = fallbackContext.t): RegistryEntity {
  const resolvedToolId = requireDottedIdentifier(context.createForm.identifier, "Tool ID", t);
  return {
    ...basePayload,
    tool_id: resolvedToolId,
    display_name: requireDisplayName(context.createForm.display_name, t),
    category: requireCategory(context.category, t),
    prompt: normalizeString(context.toolPrompt),
    implementation: { type: normalizeString(context.toolImplementationType) || "internal" },
    enabled: Boolean(context.createForm.enabled),
    version: normalizeString(basePayload.version) || "1.0.0",
  };
}

function buildSkillPayload(basePayload: RegistryEntity, context: CreatePayloadContext, t: I18nContextValue["t"] = fallbackContext.t): RegistryEntity {
  const resolvedSkillId = requireDottedIdentifier(context.createForm.identifier, "Skill ID", t);
  return {
    ...basePayload,
    skill_id: resolvedSkillId,
    display_name: requireDisplayName(context.createForm.display_name, t),
    category: requireCategory(context.category, t),
    instructions: parseLines(context.skillInstructions),
    enabled: Boolean(context.createForm.enabled),
    version: normalizeString(basePayload.version) || "1.0.0",
  };
}

function parseGuardrailConfig(rawConfig: string, t: I18nContextValue["t"] = fallbackContext.t): RegistryEntity {
  if (!rawConfig) return { patterns: [] };
  try {
    const parsed: unknown = JSON.parse(rawConfig);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error(t("react.registry.guardrail_config_must_be_a_json_object", "Guardrail config must be a JSON object."));
    }
    return parsed as RegistryEntity;
  } catch (error) {
    throw new Error(error instanceof Error ? error.message : t("react.registry.invalid_guardrail_config_json", "Invalid guardrail config JSON."));
  }
}

function buildGuardrailPayload(basePayload: RegistryEntity, context: CreatePayloadContext, t: I18nContextValue["t"] = fallbackContext.t): RegistryEntity {
  const resolvedDisplayName = requireDisplayName(context.createForm.display_name, t);
  const resolvedRuleType = normalizeString(context.guardrailRuleType);
  if (!resolvedRuleType) throw new Error(t("react.registry.rule_type_is_required", "Rule type is required."));
  if (!context.guardrailAgentIds.length) throw new Error(t("react.registry.select_at_least_one_agent", "Select at least one agent."));
  const payload: RegistryEntity = {
    ...basePayload,
    display_name: resolvedDisplayName,
    description: normalizeString(context.createForm.description),
    mode: context.guardrailMode,
    rule_type: resolvedRuleType,
    severity: context.guardrailSeverity,
    config: parseGuardrailConfig(normalizeString(context.guardrailConfigJson), t),
    prompt_text: normalizeString(context.guardrailPromptText),
    enabled: Boolean(context.createForm.enabled),
    agents: [...context.guardrailAgentIds].sort((left, right) => left.localeCompare(right)),
  };
  delete payload[context.idKey];
  delete payload.id;
  delete payload.guardrail_id;
  return payload;
}

export function buildCreatePayload(basePayload: RegistryEntity, context: CreatePayloadContext, t: I18nContextValue["t"] = fallbackContext.t): RegistryEntity {
  if (context.endpoint === "/api/agents") return buildAgentPayload(basePayload, context, t);
  if (context.endpoint === "/api/tools") return buildToolPayload(basePayload, context, t);
  if (context.endpoint === "/api/skills") return buildSkillPayload(basePayload, context, t);
  if (context.endpoint === "/api/guardrails") return buildGuardrailPayload(basePayload, context, t);
  return basePayload;
}

export function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

export function renderChoice(condition: boolean, yes: ReactNode, no: ReactNode): ReactNode {
  return condition ? yes : no;
}
