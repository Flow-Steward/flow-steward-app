import type { WorkflowDetail, WorkflowStep } from "@/pages/workflows/shared/api";
import { isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import {
  type ModelOption,
  normalizeProviderModelOptions,
} from "@/lib/utils/model-options";

export type ProjectTabKey =
  | "overview"
  | "workflows"
  | "data-connections"
  | "policy"
  | "integrations"
  | "notifications"
  | "connections"
  | "datasets"
  | "history";

export type Entity = Record<string, unknown>;
export type WorkflowStatusFilter = "all" | "linked" | "available" | "disabled";
export type DataConnectionStatusFilter = "all" | "linked" | "available" | "disabled" | "unhealthy";
export type AgentCatalogMode = "overridden" | "project-relevant" | "all";

/* Save and validation outcomes all reach the operator through one
   channel, so the tone has to travel with the text - otherwise a rejected save
   arrives wearing the same neutral styling as a successful one. */
export type ProjectEditNotice = Readonly<{ text: string; tone: "danger" | "success" }>;

export type ProviderOption = {
  value: string;
  label: string;
};

export type AgentOverrideDraft = {
  agentName: string;
  provider: string;
  model: string;
  defaultProvider: string;
  defaultModel: string;
  relevant: boolean;
};

export function textOrEmpty(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  return stringFromUnknownValue(value).trim();
}

export function listFromUnknown(value: unknown): Entity[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is Entity => Boolean(item) && typeof item === "object");
}

export function toStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => textOrEmpty(item)).filter(isTruthy);
}

export function tupleProviderOptions(value: unknown): ProviderOption[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => {
      if (!Array.isArray(item)) {
        return null;
      }
      const [rawValue, rawLabel] = item;
      const optionValue = textOrEmpty(rawValue);
      if (!optionValue) {
        return null;
      }
      return {
        value: optionValue,
        label: textOrEmpty(rawLabel) || optionValue,
      };
    })
    .filter(isTruthy);
}

export function connectionSearchText(row: Entity): string {
  return [
    textOrEmpty(row.connection_id),
    textOrEmpty(row.display_name),
    textOrEmpty(row.workspace_display_name),
    textOrEmpty(row.source_kind),
    textOrEmpty(row.engine_hint),
    textOrEmpty(row.status),
    textOrEmpty(row.health_status),
    textOrEmpty(row.last_error),
  ].map((value) => stringFromUnknownValue(value)).join(" ")
    .toLowerCase();
}

export function objectOrEmpty(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

export function normalizeProviderModels(value: unknown): Record<string, ModelOption[]> {
  return normalizeProviderModelOptions(objectOrEmpty(value).provider_models);
}

/** The workflow's own steps; a step's group never changes which agent it uses. */
export function workflowSteps(value: unknown): WorkflowStep[] {
  const steps = objectOrEmpty(value).steps;
  return Array.isArray(steps)
    ? steps.filter((item): item is WorkflowStep => Boolean(item) && typeof item === "object")
    : [];
}

export function workflowAgentIds(workflow: WorkflowDetail | Entity): string[] {
  const ids = new Set<string>();
  workflowSteps(workflow).forEach((step) => {
    const agentId = textOrEmpty(step.agent_id || objectOrEmpty(step).agent);
    if (agentId) {
      ids.add(agentId);
    }
  });
  return Array.from(ids);
}

export function normalizeProjectTab(value: unknown): ProjectTabKey {
  const candidate = textOrEmpty(value);
  if (
    candidate === "overview" ||
    candidate === "workflows" ||
    candidate === "data-connections" ||
    candidate === "policy" ||
    candidate === "integrations" ||
    candidate === "notifications" ||
    candidate === "connections" ||
    candidate === "datasets" ||
    candidate === "history"
  ) {
    return candidate;
  }
  return "overview";
}
