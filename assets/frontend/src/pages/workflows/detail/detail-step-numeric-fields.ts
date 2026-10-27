import type { I18nContextValue } from "@/lib/i18n/context";

export type StepNumericField = "duration_ms" | "duration_source" | "row_limit" | "timeout_seconds" | "max_response_bytes";
export type StepNumericFieldErrors = Partial<Record<StepNumericField, string>>;

/** Only an edited, complete finite number becomes a JSON number. Blank input stays blank. */
export function numericDraftFromInput(raw: string): number | string {
  const text = raw.trim();
  if (!text || text.endsWith(".") || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(text)) return raw;
  const value = Number(text);
  return Number.isFinite(value) ? value : raw;
}

export function numericInputValue(value: unknown): number | string {
  return typeof value === "number" || typeof value === "string" ? value : "";
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function optionalBlank(value: unknown): boolean {
  return value === undefined || value === null || value === "";
}

/** Validate the backend's Unicode decimal dialect without rewriting the saved value. */
function decimalDigitsForValidation(raw: string): string {
  return raw.replace(/\p{Decimal_Number}/gu, (digit) => {
    const codePoint = digit.codePointAt(0);
    if (codePoint === undefined) return digit;
    // Nd digit sets are contiguous groups of ten, including adjacent sets.
    let start = codePoint;
    while (start > 0 && /^\p{Decimal_Number}$/u.test(String.fromCodePoint(start - 1))) start -= 1;
    return String((codePoint - start) % 10);
  });
}

function positiveDatasetRows(value: unknown): boolean {
  if (optionalBlank(value)) return true;
  if (typeof value === "number") return Number.isInteger(value) && value > 0;
  if (typeof value !== "string") return false;
  const raw = decimalDigitsForValidation(value.trim());
  return raw === "" || (/^\d+$/.test(raw) && Number(raw) > 0);
}

function positiveHttpTimeout(value: unknown): boolean {
  if (typeof value === "number") return value > 0;
  if (typeof value !== "string") return false;
  const raw = decimalDigitsForValidation(value.trim());
  // The backend uses float(str(value)), not a strict JSON-number type or a
  // finite-only policy. Preserve its existing numeric-string dialect on Save.
  if (/^[+-]?(?:inf(?:inity)?|nan)$/i.test(raw)) return !/^-(?:inf(?:inity)?)$/i.test(raw);
  const digits = "[0-9](?:_?[0-9])*";
  const decimal = new RegExp(String.raw`^[+-]?(?:${digits}(?:\.(?:${digits})?)?|\.${digits})(?:[eE][+-]?${digits})?$`);
  return decimal.test(raw) && Number(raw.replaceAll("_", "")) > 0;
}

/** Derived authoring checks only; backend deployment and runtime policies remain authoritative. */
export function stepNumericFieldErrors(
  kind: string,
  config: Record<string, unknown>,
  t: I18nContextValue["t"],
): StepNumericFieldErrors {
  const errors: StepNumericFieldErrors = {};
  if (kind === "wait") {
    const literal = Object.hasOwn(config, "duration_ms");
    const mapped = Object.hasOwn(record(config.input_mapping), "duration_ms");
    if (literal === mapped) {
      errors[mapped ? "duration_source" : "duration_ms"] = t("react.workflows.wait_duration_source_invalid", "Choose exactly one duration source.");
    }
    if (literal && (typeof config.duration_ms !== "number" || !Number.isInteger(config.duration_ms) || config.duration_ms < 0 || config.duration_ms > 31_536_000_000)) {
      errors.duration_ms = t("react.workflows.wait_duration_invalid", "Enter a whole number from 0 to 31536000000.");
    }
  }
  if (kind === "dataset_query" && !positiveDatasetRows(config.row_limit)) {
    errors.row_limit = t("react.workflows.numeric_optional_positive_integer", "Enter a whole number greater than zero, or leave this blank.");
  }
  if (kind === "http_request") {
    if (!positiveHttpTimeout(config.timeout_seconds)) {
      errors.timeout_seconds = t("react.workflows.http_timeout_positive", "Enter a number greater than zero.");
    }
    const maximum = typeof config.response_mode === "string" && config.response_mode.trim().toLowerCase() === "artifact" ? 200 * 1024 * 1024 : 5 * 1024 * 1024;
    const bytes = config.max_response_bytes;
    if (!optionalBlank(bytes) && (typeof bytes !== "number" || !Number.isInteger(bytes) || bytes <= 0 || bytes > maximum)) {
      errors.max_response_bytes = t("react.workflows.http_response_bytes_range", "Enter a whole number from 1 to {maximum}, or leave this blank.", { maximum });
    }
  }
  return errors;
}
