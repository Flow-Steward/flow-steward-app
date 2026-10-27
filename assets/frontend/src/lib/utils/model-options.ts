import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type ModelOption = {
  modelId: string;
  label: string;
};

function modelOptionFromUnknown(value: unknown): ModelOption | null {
  if (typeof value === "string" || typeof value === "number") {
    const modelId = stringFromUnknownValue(value).trim();
    return modelId ? { modelId, label: modelId } : null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const row = value as Record<string, unknown>;
  const modelId = firstStringFromUnknownValue([
    row.model_id,
    row.model,
    row.id,
    row.value,
    row.name,
    "",
  ]).trim();
  if (!modelId) {
    return null;
  }
  return {
    modelId,
    label: firstStringFromUnknownValue([row.label, row.display_name, modelId]).trim() || modelId,
  };
}

export function normalizeModelOptions(value: unknown): ModelOption[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const byId = new Map<string, ModelOption>();
  value.map(modelOptionFromUnknown).filter(isTruthy).forEach((option) => {
    if (!byId.has(option.modelId)) {
      byId.set(option.modelId, option);
    }
  });
  return Array.from(byId.values());
}

export function normalizeProviderModelOptions(value: unknown): Record<string, ModelOption[]> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  const normalized: Record<string, ModelOption[]> = {};
  for (const [provider, models] of Object.entries(value as Record<string, unknown>)) {
    const options = normalizeModelOptions(models);
    if (provider.trim() && options.length) {
      normalized[provider.trim()] = options;
    }
  }
  return normalized;
}

export function modelOptionLabel(options: ModelOption[], modelId: string): string {
  return options.find((option) => option.modelId === modelId)?.label || modelId;
}
