import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo } from "react";

import { DsSelect } from "@/components/shared/design-system";
import {
  defaultGuardrailRuleType,
  type GuardrailMode,
} from "./guardrail-rule-options";

type GuardrailRuleEditorProps = {
  configJson: string;
  mode: GuardrailMode;
  onConfigJsonChange: (value: string) => void;
  onPromptTextChange: (value: string) => void;
  onRuleTypeChange: (value: string) => void;
  promptText: string;
  ruleType: string;
};

function parseConfig(configJson: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(configJson);
    return value && typeof value === "object" && !Array.isArray(value)
      ? value as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function stringRows(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => typeof item === "string" ? item : "")
    : [];
}

export function GuardrailRuleEditor({
  configJson,
  mode,
  onConfigJsonChange,
  onPromptTextChange,
  onRuleTypeChange,
  promptText,
  ruleType,
}: Readonly<GuardrailRuleEditorProps>) {
  const { t } = useI18n();
  const config = useMemo(() => parseConfig(configJson), [configJson]);
  const normalizedRuleType = ruleType.trim();
  const configKey = normalizedRuleType === "required_sections" ? "sections" : "patterns";
  const configuredRows = stringRows(config[configKey]);
  const rows = configuredRows.length ? configuredRows : [""];
  const semanticOptions = [
    { value: "semantic_condition", label: t("react.registry.ai_evaluated_condition", "AI-evaluated condition") },
    { value: "output_addresses_input", label: t("react.registry.output_addresses_input", "Output addresses input") },
  ];
  if (
    normalizedRuleType
    && !semanticOptions.some((option) => option.value === normalizedRuleType)
  ) {
    semanticOptions.push({
      value: normalizedRuleType,
      label: t("react.registry.custom_semantic_rule_value0", "Custom semantic rule — {value0}", { value0: normalizedRuleType }),
    });
  }
  const options = mode === "deterministic"
    ? [
        { value: "forbidden_patterns", label: t("react.registry.forbidden_patterns", "Forbidden patterns") },
        { value: "required_sections", label: t("react.registry.required_sections", "Required sections") },
      ]
    : semanticOptions;

  function updateRows(nextRows: string[]) {
    onConfigJsonChange(JSON.stringify({ ...config, [configKey]: nextRows }, null, 2));
  }

  return (
    <>
      <div className="fs-field">
        <span className="fs-field-label">{t("react.registry.rule_type", "Rule Type")}</span>
        <DsSelect
          ariaLabel={t("react.registry.rule_type", "Rule Type")}
          onChange={(value) => {
            onRuleTypeChange(value);
            if (mode === "deterministic") {
              const nextKey = value === "required_sections" ? "sections" : "patterns";
              if (!Array.isArray(config[nextKey])) {
                onConfigJsonChange(JSON.stringify({ ...config, [nextKey]: [] }, null, 2));
              }
            }
          }}
          options={options}
          value={normalizedRuleType || defaultGuardrailRuleType(mode)}
        />
      </div>
      {mode === "semantic" ? (
        <div className="fs-field full">
          <label className="fs-field-label" htmlFor="guardrail-ai-condition">
            {t("react.registry.ai_condition", "AI condition")}</label>
          <textarea
            className="fs-textarea"
            id="guardrail-ai-condition"
            onChange={(event) => onPromptTextChange(event.target.value)}
            placeholder={t("react.registry.describe_the_condition_the_response_must_satisfy", "Describe the condition the response must satisfy.")}
            value={promptText}
          />
          <span className="fs-field-helper">
            {t("react.registry.the_semantic_evaluator_uses_this_natural_language_condition_at_runtime", "The semantic evaluator uses this natural-language condition at runtime.")}</span>
        </div>
      ) : (
        <div className="fs-field full">
          <div className="fs-panel-heading">
            <div>
              <strong>{normalizedRuleType === "required_sections" ? t("react.registry.required_sections", "Required sections") : t("react.registry.forbidden_patterns", "Forbidden patterns")}</strong>
              <p className="fs-field-helper">
                {normalizedRuleType === "required_sections"
                  ? t("react.registry.add_section_names_or_keywords_for_example_summary_or_acceptance_criteria", "Add section names or keywords, for example Summary or Acceptance criteria.")
                  : t("react.registry.each_value_is_a_python_regular_expression_matched_case_insensitively_for_example_secret_password", "Each value is a Python regular expression matched case-insensitively, for example secret|password.")}
              </p>
            </div>
            <button
              className="fs-button sm"
              onClick={() => updateRows([...configuredRows, ""])}
              type="button"
            >
              {normalizedRuleType === "required_sections" ? t("react.registry.add_section", "Add section") : t("react.registry.add_pattern", "Add pattern")}
            </button>
          </div>
          <div className="mt-3 grid gap-2">
            {rows.map((value, index) => (
              <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]" key={`${configKey}-${index}`}>
                <label className="fs-field">
                  <span className="fs-field-label">
                    {normalizedRuleType === "required_sections" ? t("react.registry.section", "Section") : t("react.registry.pattern", "Pattern")} {index + 1}
                  </span>
                  <input
                    className="fs-input"
                    onChange={(event) => {
                      const next = [...rows];
                      next[index] = event.target.value;
                      updateRows(next);
                    }}
                    value={value}
                  />
                </label>
                <button
                  aria-label={t("react.registry.remove_value0_value1", "Remove {value0} {value1}", { value0: configKey === "sections" ? "section" : "pattern", value1: index + 1 })}
                  className="fs-button self-end"
                  disabled={!configuredRows.length}
                  onClick={() => updateRows(configuredRows.filter((_, rowIndex) => rowIndex !== index))}
                  type="button"
                >
                  {t("extensions.repeater.remove", "Remove")}</button>
              </div>
            ))}
          </div>
          {!configuredRows.some((value) => value.trim()) ? (
            <span className="fs-field-helper error">
              {t("react.registry.add_at_least_one_non_blank_value_before_enabling_this_guardrail", "Add at least one non-blank value before enabling this guardrail.")}</span>
          ) : null}
        </div>
      )}
    </>
  );
}
