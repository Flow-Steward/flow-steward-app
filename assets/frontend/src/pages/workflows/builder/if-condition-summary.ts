import type { I18nContextValue } from "@/lib/i18n/context";

export function ifConditionSummary(config: Record<string, unknown>, t: I18nContextValue["t"]): string {
  const fallback = () => t("react.workflows.if_default_summary", "conditional branch");
  const condition = config.condition;
  if (condition === undefined || condition === null || condition === "") {
    const selector = typeof config.selector === "string" ? config.selector.trim() : "";
    return selector || fallback();
  }
  if (typeof condition === "string") return condition.trim() || fallback();
  if (!condition || typeof condition !== "object" || Array.isArray(condition)) return fallback();

  const expression = condition as Record<string, unknown>;
  const operators: Record<string, string> = {
    equals: "=", not_equals: "≠", gt: ">", gte: "≥", lt: "<", lte: "≤",
    contains: "contains", in: "in", not_in: "not in",
  };
  const literal = (value: unknown): string | null => {
    if (typeof value === "string") {
      const selector = value.trim();
      if (selector === "state" || selector.startsWith("state.")) return selector;
    }
    if (typeof value === "number" && !Number.isFinite(value)) return null;
    try {
      return JSON.stringify(value) ?? null;
    } catch {
      return null;
    }
  };

  if ("selector" in expression || "operator" in expression) {
    if (typeof expression.operator !== "string") return fallback();
    const operator = expression.operator.trim().toLowerCase();
    const keys = Object.keys(expression);
    const selector = typeof expression.selector === "string" ? expression.selector.trim() : "";
    if (operator === "always_true" || operator === "always_false") {
      if (!(keys.length === 1 && keys[0] === "operator") &&
        !(keys.length === 2 && keys.includes("selector") && keys.includes("operator") && selector)) return fallback();
      return operator === "always_true" ? "always true" : "always false";
    }
    if (!selector) return fallback();
    if (operator === "exists" || operator === "truthy") {
      return keys.length === 2 && keys.includes("selector") && keys.includes("operator") ? `${selector} ${operator}` : fallback();
    }
    const token = Object.prototype.hasOwnProperty.call(operators, operator) ? operators[operator] : undefined;
    if (!token || keys.length !== 3 || !keys.includes("selector") || !keys.includes("operator") || !keys.includes("value")) return fallback();
    const value = literal(expression.value);
    return value === null ? fallback() : `${selector} ${token} ${value}`;
  }

  const entries = Object.entries(expression);
  if (entries.length !== 1) return fallback();
  const [operator, operand] = entries[0];
  if (operator === "exists" || operator === "truthy") {
    return typeof operand === "string" && operand.trim() ? `${operand.trim()} ${operator}` : fallback();
  }
  const token = Object.prototype.hasOwnProperty.call(operators, operator) ? operators[operator] : undefined;
  if (!token || !operand || typeof operand !== "object" || Array.isArray(operand)) return fallback();
  const comparison = operand as Record<string, unknown>;
  if (Object.keys(comparison).length !== 2 || !("left" in comparison) || !("right" in comparison)) return fallback();
  const left = typeof comparison.left === "string" ? comparison.left.trim() : "";
  const right = literal(comparison.right);
  return left && right !== null ? `${left} ${token} ${right}` : fallback();
}
