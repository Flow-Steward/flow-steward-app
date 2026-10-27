import type { I18nContextValue } from "@/lib/i18n/context";

export type TransformRequiredFieldErrors = Partial<Record<"source" | "result_target", string>>;

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

/** Required path presence only; the backend owns path syntax and other Transform rules. */
export function transformRequiredFieldErrors(
  config: Record<string, unknown>,
  t: I18nContextValue["t"],
): TransformRequiredFieldErrors {
  const errors: TransformRequiredFieldErrors = {};
  const source = record(config.input).source;
  if (typeof source !== "string" || !source.trim()) {
    errors.source = t("react.workflows.value0_is_required", "{value0} is required.", {
      value0: t("react.workflows.source", "Source"),
    });
  }
  if (typeof config.result_target !== "string" || !config.result_target.trim()) {
    errors.result_target = t("react.workflows.value0_is_required", "{value0} is required.", {
      value0: t("react.workflows.result_target_variant_6c946b46", "Result target"),
    });
  }
  return errors;
}
