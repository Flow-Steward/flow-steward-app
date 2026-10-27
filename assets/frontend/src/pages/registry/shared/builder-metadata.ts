import { isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
export type BuilderMetadataDraft = {
  purpose: string;
  useCases: string;
  antiUseCases: string;
  compositionRole: string;
  determinismLevel: string;
  safeForAutoUse: boolean;
};

function textOrEmpty(value: unknown): string {
  return stringFromUnknownValue(value ?? "").trim();
}

function normalizedLines(value: unknown): string[] {
  return stringFromUnknownValue(value ?? "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(isTruthy);
}

function linesFromUnknown(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => textOrEmpty(item))
    .filter(isTruthy);
}

export function builderMetadataDraftFromUnknown(
  value: unknown,
): BuilderMetadataDraft {
  const source =
    value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  return {
    purpose: textOrEmpty(source.purpose),
    useCases: linesFromUnknown(source.use_cases).join("\n"),
    antiUseCases: linesFromUnknown(source.anti_use_cases).join("\n"),
    compositionRole: textOrEmpty(source.composition_role),
    determinismLevel: textOrEmpty(source.determinism_level) || "hybrid",
    safeForAutoUse: Boolean(source.safe_for_auto_use),
  };
}

export function builderMetadataPayload(
  draft: BuilderMetadataDraft,
): Record<string, unknown> {
  return {
    purpose: textOrEmpty(draft.purpose),
    use_cases: normalizedLines(draft.useCases),
    anti_use_cases: normalizedLines(draft.antiUseCases),
    composition_role: textOrEmpty(draft.compositionRole),
    determinism_level: textOrEmpty(draft.determinismLevel) || "hybrid",
    safe_for_auto_use: Boolean(draft.safeForAutoUse),
  };
}
