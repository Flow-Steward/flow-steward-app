import type { DsOption } from "@/components/shared/design-system";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

/**
 * Options of a choice field or filter on a host-rendered settings page (ADR 0082,
 * section 6). A module declares them as text or `{value, label, description?}`;
 * the renderer shows the label, never the stored value. A field that refers to
 * other records (`options_path`) loads the same shape from a same-origin API path.
 */
export type ChoiceOption = Pick<DsOption, "description" | "label" | "value">;

function optionText(value: unknown): string {
  return firstStringFromUnknownValue([value, ""]).trim();
}

export function normalizeChoiceOption(value: unknown): ChoiceOption | null {
  if (typeof value === "string") {
    const text = value.trim();
    return text ? { label: text, value: text } : null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const optionValue = optionText(record.value);
  if (!optionValue) {
    return null;
  }
  const description = optionText(record.description);
  return {
    label: optionText(record.label) || optionValue,
    value: optionValue,
    ...(description ? { description } : {}),
  };
}

export function normalizeChoiceOptions(value: unknown): ChoiceOption[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const seen = new Set<string>();
  return value
    .map(normalizeChoiceOption)
    .filter(isTruthy)
    .filter((option) => {
      if (seen.has(option.value)) return false;
      seen.add(option.value);
      return true;
    });
}

/** The options an `options_path` returns: a list, or `{options: [...]}`. */
export function choiceOptionsFromPayload(payload: unknown): ChoiceOption[] {
  if (Array.isArray(payload)) {
    return normalizeChoiceOptions(payload);
  }
  if (payload && typeof payload === "object") {
    return normalizeChoiceOptions((payload as Record<string, unknown>).options);
  }
  return [];
}

/** The label of a stored value; an unknown value shows as it is stored. */
export function choiceLabel(options: readonly ChoiceOption[], value: string): string {
  return options.find((option) => option.value === value)?.label ?? value;
}
