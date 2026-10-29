import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import {
  type ModelOption,
  normalizeModelOptions,
  normalizeProviderModelOptions,
} from "@/lib/utils/model-options";

export type ModelSelectionOptions = {
  all_models?: unknown;
  provider_models?: unknown;
  provider_options?: unknown;
  provider_labels?: unknown;
};

export function registryOptionId(row: Record<string, unknown>, keys: string[]): string {
  return keys.map((key) => firstStringFromUnknownValue([row[key], ""]).trim()).find(Boolean) || "";
}

export function registryOptionLabel(row: Record<string, unknown>, id: string): string {
  return firstStringFromUnknownValue([row.display_name, row.name, row.label, id]).trim() || id;
}

export function isStandardToolAction(row: Record<string, unknown>, id: string): boolean {
  const source = firstStringFromUnknownValue([row.source, ""]).trim().toLowerCase();
  return id.startsWith("std.") || source === "standard_tool_action";
}

export function lineList(value: string): string[] {
  return firstStringFromUnknownValue([value, ""])
    .split(/\r?\n|,/)
    .map((line) => line.trim())
    .filter(isTruthy);
}

export function textOrEmpty(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  return stringFromUnknownValue(value).trim();
}

export function normalizeModelList(value: unknown): ModelOption[] {
  return normalizeModelOptions(value);
}

export function normalizeProviderModels(value: unknown): Record<string, ModelOption[]> {
  return normalizeProviderModelOptions(value);
}

export function modelOptionsForProvider(
  providerModels: Record<string, ModelOption[]>,
  allModels: ModelOption[],
  provider: string,
  currentValue: string,
): ModelOption[] {
  if (!textOrEmpty(provider)) {
    return [];
  }
  const baseModels = compatibleModelOptionsForProvider(providerModels, allModels, provider);
  return currentValue && !baseModels.some((option) => option.modelId === currentValue)
    ? [{ modelId: currentValue, label: currentValue }, ...baseModels]
    : baseModels;
}

export function compatibleModelOptionsForProvider(
  providerModels: Record<string, ModelOption[]>,
  _allModels: ModelOption[],
  provider: string,
): ModelOption[] {
  const normalizedProvider = textOrEmpty(provider);
  if (!normalizedProvider) {
    return [];
  }
  return providerModels[normalizedProvider] || [];
}

export function buildAgentConfigPayload(
  currentConfig: Record<string, unknown>,
  provider: string,
  model: string,
): Record<string, unknown> {
  const nextConfig = { ...currentConfig };
  const normalizedProvider = provider.trim();
  const normalizedModel = model.trim();
  // Keep empty route fields in the PATCH payload. They are the explicit
  // signal that an existing per-agent override must be cleared.
  nextConfig.provider = normalizedProvider;
  nextConfig.model = normalizedModel;
  return nextConfig;
}

export function toggleLineValue(value: string, item: string): string {
  const normalized = item.trim();
  const current = new Set(lineList(value));
  if (current.has(normalized)) {
    current.delete(normalized);
  } else if (normalized) {
    current.add(normalized);
  }
  return [...current].sort((a, b) => a.localeCompare(b)).join("\n");
}
