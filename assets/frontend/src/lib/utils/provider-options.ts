import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

function text(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  return stringFromUnknownValue(value).trim();
}

/**
 * Build a `provider_id -> display label` map from the canonical
 * `provider_options` metadata returned by `/api/model-selection/options`.
 *
 * Generic and vendor-agnostic: labels come straight from the backend
 * (built-in or extension_llm_provider), never hardcoded per provider.
 */
export function normalizeProviderOptionLabels(
  value: unknown,
  /** `provider_labels`: every provider a saved route may name, including ones
   *  with no selectable model right now. Used only where `value` has no label. */
  knownLabels?: unknown,
): Record<string, string> {
  const labels: Record<string, string> = {};
  if (knownLabels && typeof knownLabels === "object" && !Array.isArray(knownLabels)) {
    for (const [id, label] of Object.entries(knownLabels as Record<string, unknown>)) {
      if (text(id) && text(label)) labels[text(id)] = text(label);
    }
  }
  if (!Array.isArray(value)) {
    return labels;
  }
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      continue;
    }
    const row = item as Record<string, unknown>;
    const id = text(row.provider_id);
    if (!id) {
      continue;
    }
    labels[id] = text(row.label) || id;
  }
  return labels;
}

/** Resolve a provider's display label, falling back to its stable id. */
export function providerOptionLabel(labels: Record<string, string>, providerId: string): string {
  const id = text(providerId);
  if (!id) {
    return id;
  }
  return labels[id] || id;
}
