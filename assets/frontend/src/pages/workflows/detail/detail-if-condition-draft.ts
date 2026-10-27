import { IF_CONSTANT_OPERATORS, IF_OPERATOR_OPTIONS } from "@/pages/workflows/detail/detail-automation-core";
import { objectOrEmpty, valueFromMapEditorText } from "@/pages/workflows/detail/detail-interface";
import { textOrEmpty } from "@/pages/workflows/detail/detail-test-run";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type IfConditionDraft = {
  canApplyFix?: boolean;
  invalidReason?: string;
  invalidSource?: string;
  selector: string;
  operator: string;
  value: string;
};

export function ifConditionRawSourceText(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value.trim();
  }
  try {
    return JSON.stringify(value) ?? stringFromUnknownValue(value).trim();
  } catch {
    return stringFromUnknownValue(value).trim();
  }
}

export function isLikelyIfSelector(value: string): boolean {
  const normalized = textOrEmpty(value);
  if (!normalized || /^(false|null|true|undefined)$/i.test(normalized)) {
    return false;
  }
  return /^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*|\[[^\]]+\])*$/.test(
    normalized,
  );
}

export function unsupportedIfConditionDraft(
  value: unknown,
  reason: string,
  draft: Partial<IfConditionDraft> = {},
): IfConditionDraft {
  return {
    invalidReason: reason,
    invalidSource: ifConditionRawSourceText(value),
    selector: "",
    operator: "truthy",
    value: "",
    ...draft,
  };
}

function emptyIfConditionDraft(): IfConditionDraft {
  return { selector: "", operator: "truthy", value: "" };
}

function stringIfConditionDraft(value: string): IfConditionDraft {
  const raw = textOrEmpty(value);
  if (raw.toLowerCase() === "true" || raw.toLowerCase() === "false") {
    return unsupportedIfConditionDraft(
      value,
      "This raw IF condition uses a constant value. Apply the replacement below to store it as a structured constant condition.",
      { operator: raw.toLowerCase() === "true" ? "always_true" : "always_false", canApplyFix: true },
    );
  }
  if (isLikelyIfSelector(raw)) return { selector: raw, operator: "truthy", value: "" };
  const equalsAt = raw.indexOf("==");
  const notEqualsAt = raw.indexOf("!=");
  const operatorAt = equalsAt < 0 ? notEqualsAt : notEqualsAt < 0 ? equalsAt : Math.min(equalsAt, notEqualsAt);
  const selector = operatorAt < 0 ? "" : raw.slice(0, operatorAt).trimEnd();
  const operand = operatorAt < 0 ? "" : raw.slice(operatorAt + 2).trimStart();
  if (/^state\.[A-Za-z_][\w.]*$/.test(selector) && operand) {
    try {
      const literal: unknown = JSON.parse(operand);
      if (typeof literal === "string" && (literal === "state" || literal.startsWith("state."))) {
        return unsupportedIfConditionDraft(value,
          "This raw IF condition cannot be edited safely as a structured selector.");
      }
      if (literal === null || ["string", "number", "boolean"].includes(typeof literal)) {
        return unsupportedIfConditionDraft(value,
          "This state comparison can be stored as a structured condition without changing its value.",
          { selector, operator: raw.slice(operatorAt, operatorAt + 2) === "==" ? "equals" : "not_equals",
            value: operand, canApplyFix: true });
      }
    } catch {
      // A variable expression is not a literal: automatic repair must not guess.
    }
  }
  return unsupportedIfConditionDraft(
    value,
    "This raw IF condition cannot be edited safely as a structured selector.",
  );
}

function explicitIfConditionDraft(
  value: unknown,
  row: Record<string, unknown>
): IfConditionDraft {
  const rawSelector = textOrEmpty(row.selector);
  const rawOperator = textOrEmpty(row.operator);
  const operator = rawOperator || "equals";
  const operatorSupported = IF_OPERATOR_OPTIONS.some((option) => option.value === operator);
  const draft = {
    selector: rawSelector,
    operator: operatorSupported ? operator : "equals",
    value: ifOperandEditorText(row.value),
  };
  const selectorIsString = typeof row.selector === "string";
  if (selectorIsString || IF_CONSTANT_OPERATORS.has(operator)) {
    if (operatorSupported) return draft;
  }
  const reason =
    (!selectorIsString || !rawSelector) && !IF_CONSTANT_OPERATORS.has(operator)
      ? "This IF condition is missing the selector field needed by the editor."
      : `The IF operator "${rawOperator}" is not supported by the structured editor.`;
  return unsupportedIfConditionDraft(value, reason, draft);
}

function legacyIfConditionDraft(
  value: unknown,
  row: Record<string, unknown>
): IfConditionDraft {
  for (const operator of IF_OPERATOR_OPTIONS.map((option) => option.value)) {
    if (!(operator in row)) continue;
    const operand = row[operator];
    if (operator === "exists" || operator === "truthy") {
      const draft = { selector: textOrEmpty(operand), operator, value: "" };
      return draft.selector
        ? draft
        : unsupportedIfConditionDraft(
            value,
            "This IF condition is missing the selector field needed by the editor.",
            draft,
          );
    }
    const comparison = objectOrEmpty(operand);
    const draft = {
      selector: textOrEmpty(comparison.left),
      operator,
      value: ifOperandEditorText(comparison.right),
    };
    return draft.selector
      ? draft
      : unsupportedIfConditionDraft(
          value,
          "This IF condition is missing the left-side selector needed by the editor.",
          draft,
        );
  }
  return unsupportedIfConditionDraft(
    value,
    "This raw IF condition shape is not supported by the structured editor.",
  );
}

export function ifConditionDraftFromUnknown(value: unknown): IfConditionDraft {
  const emptyDraft = emptyIfConditionDraft();
  // Only text is "empty" by its text: textOrEmpty() of an object is "", and reading a
  // structured condition as empty lost the selector on the editor's first keystroke.
  if (value === null || value === undefined || (typeof value === "string" && !value.trim())) {
    return emptyDraft;
  }
  if (typeof value === "string") {
    return stringIfConditionDraft(value);
  }
  if (typeof value === "boolean") {
    return unsupportedIfConditionDraft(
      value,
      "This raw IF condition uses a constant value. Apply the replacement below to store it as a structured constant condition.",
      { operator: value ? "always_true" : "always_false", canApplyFix: true },
    );
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    return unsupportedIfConditionDraft(
      value,
      "This raw IF condition is not a structured selector condition.",
    );
  }
  const row = objectOrEmpty(value);
  if (!Object.keys(row).length) {
    return emptyDraft;
  }
  const hasExplicitShape = textOrEmpty(row.selector) || "operator" in row || "value" in row;
  return hasExplicitShape
    ? explicitIfConditionDraft(value, row)
    : legacyIfConditionDraft(value, row);
}

function ifOperandEditorText(value: unknown): string {
  if (typeof value === "string") {
    const parsed = valueFromMapEditorText(value);
    return typeof parsed !== "string" || value.trim().startsWith('"')
      ? JSON.stringify(value)
      : value;
  }
  return value === undefined ? "" : JSON.stringify(value);
}
