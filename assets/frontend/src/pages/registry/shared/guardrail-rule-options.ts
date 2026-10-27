export type GuardrailMode = "deterministic" | "semantic";

export function defaultGuardrailRuleType(mode: GuardrailMode): string {
  return mode === "semantic" ? "semantic_condition" : "forbidden_patterns";
}

export function defaultGuardrailConfig(ruleType: string): string {
  if (ruleType === "required_sections") return JSON.stringify({ sections: [] }, null, 2);
  if (ruleType === "forbidden_patterns") return JSON.stringify({ patterns: [] }, null, 2);
  return "{}";
}
