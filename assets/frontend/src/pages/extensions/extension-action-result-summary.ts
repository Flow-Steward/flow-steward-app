import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { stringFrom } from "@/pages/extensions/extension-manifest-utils";

const SENSITIVE_FIELD = /(?:authorization|cookie|credential|password|private[_-]?key|secret|token)/i;

/**
 * Keys that describe how the platform ran the action rather than what the user
 * asked for. They belong in the technical details, never in the summary.
 */
const RUNTIME_FIELD =
  /(?:^|_)(?:command|argv|args|cwd|path|paths|bundle|stdout|stderr|trace|traceback|duration|elapsed|install|account|correlation|request)(?:_|$)/i;

export function redactPayload(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => redactPayload(item));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, item]) => [
      key,
      SENSITIVE_FIELD.test(key) ? "[REDACTED]" : redactPayload(item),
    ]),
  );
}

function readableLabel(key: string): string {
  const words = key.split(/[_\s]+/).filter(Boolean);
  if (!words.length) {
    return key;
  }
  return [words[0].charAt(0).toUpperCase() + words[0].slice(1), ...words.slice(1)].join(" ");
}

function readableValue(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? t("extensions.repeater.yes", "Yes") : t("extensions.repeater.no", "No");
  }
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => stringFrom(item)).join(", ") : "—";
  }
  if (typeof value === "object") {
    return "";
  }
  return stringFromUnknownValue(value);
}

/**
 * The result fields worth showing without being asked for them.
 *
 * An action result is a runtime payload: the answer the user wanted arrives
 * alongside commands, paths and identifiers that only help when something needs
 * debugging. Only the plain values the extension returned are summarised here;
 * everything else stays behind the technical-details disclosure.
 */
export function summaryEntriesFromResult(result: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t): Array<[string, string]> {
  return Object.entries(redactPayload(result) as Record<string, unknown>)
    .filter(([key]) => !RUNTIME_FIELD.test(key))
    .map(([key, value]): [string, string] => [readableLabel(key), readableValue(value, t)])
    .filter(([, value]) => value !== "")
    .slice(0, 12);
}
