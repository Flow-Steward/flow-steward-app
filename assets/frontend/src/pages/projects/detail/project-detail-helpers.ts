
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { WorkflowContractField } from "@/pages/projects/detail/api";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type Entity = Record<string, unknown>;

export const WEEKDAY_OPTIONS = getWeekdayOptions();

export function getWeekdayOptions(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { value: "1", label: t("react.projects.monday", "Monday") },
  { value: "2", label: t("react.projects.tuesday", "Tuesday") },
  { value: "3", label: t("react.projects.wednesday", "Wednesday") },
  { value: "4", label: t("react.projects.thursday", "Thursday") },
  { value: "5", label: t("react.projects.friday", "Friday") },
  { value: "6", label: t("react.projects.saturday", "Saturday") },
  { value: "0", label: t("react.projects.sunday", "Sunday") },
];
}

const FALLBACK_TIMEZONES = [
  "UTC",
  "Europe/London",
  "Europe/Paris",
  "Europe/Vilnius",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "Asia/Tokyo",
  "Asia/Singapore",
  "Australia/Sydney",
];

export function objectOrEmpty(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

export function timezoneOptions(currentTimezone: string): string[] {
  const intlWithTimezoneList = Intl as typeof Intl & {
    supportedValuesOf?: (key: "timeZone") => string[];
  };
  const browserValues =
    typeof intlWithTimezoneList.supportedValuesOf === "function"
      ? intlWithTimezoneList.supportedValuesOf("timeZone")
      : FALLBACK_TIMEZONES;
  return Array.from(new Set([currentTimezone, ...browserValues, ...FALLBACK_TIMEZONES]))
    .map((value) => firstStringFromUnknownValue([value, ""]).trim())
    .filter(isTruthy)
    .sort((left, right) => left.localeCompare(right));
}

export function workflowIdList(payload: unknown): string[] {
  if (!payload || typeof payload !== "object") {
    return [];
  }
  const record = payload as Record<string, unknown>;
  const explicit = Array.isArray(record.workflow_ids)
    ? record.workflow_ids.map((item) => firstStringFromUnknownValue([item, ""]).trim()).filter(isTruthy)
    : [];
  if (explicit.length) {
    return explicit;
  }
  const rows = Array.isArray(record.project_workflows) ? record.project_workflows : [];
  return rows
    .map((row) => {
      if (!row || typeof row !== "object") {
        return "";
      }
      return firstStringFromUnknownValue([(row as Record<string, unknown>).workflow_id, ""]).trim();
    })
    .filter(isTruthy);
}

export function workflowAllowsUiLaunch(payload: unknown): boolean {
  const row = objectOrEmpty(payload);
  const contract = objectOrEmpty(row.interface);
  const triggerChannels = Array.isArray(contract.trigger_channels)
    ? contract.trigger_channels.map((item) => firstStringFromUnknownValue([item, ""]).trim())
    : [];
  const issues = Array.isArray(row.definition_issues)
    ? row.definition_issues
    : [];
  return (
    Boolean(row.enabled) &&
    Boolean(contract.enabled) &&
    triggerChannels.includes("ui") &&
    issues.length === 0
  );
}

export type StructuredInputValue = { value: string; asJson?: boolean };
export type ObjectInputRow = StructuredInputValue & { key: string };

function structuredInputValue(raw: unknown): StructuredInputValue {
  return typeof raw === "string"
    ? { value: raw, asJson: false }
    : { value: JSON.stringify(raw), asJson: true };
}

function parsedStructuredValue(row: StructuredInputValue): unknown {
  return row.asJson ? JSON.parse(row.value) : row.value;
}

export function fieldObjectRows(value: string): ObjectInputRow[] {
  try {
    const parsed = JSON.parse(value || "{}");
    const row = objectOrEmpty(parsed);
    const entries = Object.entries(row).map(([key, raw]) => ({
      key,
      ...structuredInputValue(raw),
    }));
    return entries.length ? entries : [{ key: "", value: "" }];
  } catch {
    return [{ key: "", value: "" }];
  }
}

export function objectRowsValue(rows: ObjectInputRow[]): string {
  const keys = new Set<string>();
  const entries = rows.flatMap((row) => {
    const key = firstStringFromUnknownValue([row.key, ""]).trim();
    if (!key && row.value) {
      throw new Error("Enter a key for every non-empty value.");
    }
    if (!key) return [];
    if (keys.has(key)) throw new Error(`Duplicate object key: ${key}.`);
    keys.add(key);
    return [[key, parsedStructuredValue(row)]];
  });
  return JSON.stringify(Object.fromEntries(entries));
}

export function fieldArrayRows(value: string): StructuredInputValue[] {
  try {
    const parsed = JSON.parse(value || "[]");
    return Array.isArray(parsed)
      ? parsed.map((item: unknown) => structuredInputValue(item))
      : [{ value: "" }];
  } catch {
    return [{ value: "" }];
  }
}

export function arrayRowsValue(rows: StructuredInputValue[]): string {
  return JSON.stringify(rows.map(parsedStructuredValue));
}

export function nextTaskId(projectId: string): string {
  const stamp = Date.now();
  const random = globalThis.crypto.randomUUID().replaceAll("-", "").slice(0, 6);
  return `react_${projectId}_${stamp}_${random}`;
}

function splitTime(value: string): { hour: number; minute: number } {
  const [hourRaw = "9", minuteRaw = "0"] = firstStringFromUnknownValue([value, ""]).split(":");
  const hour = Math.max(0, Math.min(23, Number(hourRaw) || 0));
  const minute = Math.max(0, Math.min(59, Number(minuteRaw) || 0));
  return { hour, minute };
}

export function buildFriendlyCronExpr(mode: string, time: string, weekday: string, monthDay: string, custom: string): string {
  if (mode === "custom") {
    return custom.trim();
  }
  const { hour, minute } = splitTime(time);
  if (mode === "weekly") {
    return `${minute} ${hour} * * ${weekday || "1"}`;
  }
  if (mode === "monthly") {
    const day = Math.max(1, Math.min(31, Number(monthDay) || 1));
    return `${minute} ${hour} ${day} * *`;
  }
  return `${minute} ${hour} * * *`;
}

export function cronDescription(mode: string, time: string, weekday: string, monthDay: string, timezone = "", t: I18nContextValue["t"] = fallbackContext.t): string {
  const zone = timezone || t("react.projects.local_timezone", "local timezone");
  if (mode === "weekly") {
    const weekdayLabel = getWeekdayOptions(t).find((item) => item.value === weekday)?.label || t("react.projects.monday", "Monday");
    return t("react.projects.weekly_schedule_description", "Every {weekday} at {time} ({zone})", { weekday: weekdayLabel, time, zone });
  }
  if (mode === "monthly") {
    return t("react.projects.monthly_schedule_description", "Every month on day {day} at {time} ({zone})", { day: Math.max(1, Math.min(31, Number(monthDay) || 1)), time, zone });
  }
  if (mode === "custom") {
    return t("react.projects.custom_schedule_description", "Custom cron schedule ({zone})", { zone });
  }
  return t("react.projects.daily_schedule_description", "Every day at {time} ({zone})", { time, zone });
}

export function fieldLabel(field: WorkflowContractField): string {
  return firstStringFromUnknownValue([field.label, field.name, ""]).replaceAll(/[_-]/g, " ");
}

export function emptyFieldValue(field: WorkflowContractField): string {
  const fieldType = firstStringFromUnknownValue([field.type, "string"]).toLowerCase();
  if (fieldType === "object") {
    return "{}";
  }
  if (fieldType === "array") {
    return "[]";
  }
  return "";
}

type ParsedWorkflowInput = { error: string; include: boolean; value?: unknown };

function parseWorkflowInputValue(field: WorkflowContractField, raw: string, t: I18nContextValue["t"]): ParsedWorkflowInput {
  const type = firstStringFromUnknownValue([field.type, "string"]).toLowerCase();
  const label = fieldLabel(field);
  const empty = !stringFromUnknownValue(raw).trim();
  if (field.required && empty) return { error: t("react.projects.input_required", "{field} is required.", { field: label }), include: false };
  if (empty && type !== "boolean") return { error: "", include: false };
  if (type === "number") {
    const parsed = Number(raw);
    return Number.isNaN(parsed)
      ? { error: t("react.projects.input_number_required", "{field} must be a number.", { field: label }), include: false }
      : { error: "", include: true, value: parsed };
  }
  if (type === "boolean") return { error: "", include: true, value: raw === "true" };
  if (type === "object" || type === "array") {
    try {
      const parsed: unknown = JSON.parse(raw);
      const validShape = type === "array"
        ? Array.isArray(parsed)
        : parsed !== null && typeof parsed === "object" && !Array.isArray(parsed);
      return validShape
        ? { error: "", include: true, value: parsed }
        : { error: type === "array"
          ? t("react.projects.field_json_array_required", "{field} must be a JSON array.", { field: label })
          : t("react.projects.field_json_object_required", "{field} must be a JSON object.", { field: label }), include: false };
    } catch {
      return { error: t("react.projects.field_valid_json_required", "{field} must be valid JSON.", { field: label }), include: false };
    }
  }
  return { error: "", include: true, value: raw };
}

export function parseWorkflowInputs(
  fields: WorkflowContractField[],
  values: Record<string, string>,
  t: I18nContextValue["t"] = fallbackContext.t,
): { inputs: Record<string, unknown>; error: string } {
  const inputs: Record<string, unknown> = {};
  for (const field of fields) {
    const name = firstStringFromUnknownValue([field.name, ""]).trim();
    if (!name) continue;
    const raw = values[name] ?? "";
    const parsed = parseWorkflowInputValue(field, raw, t);
    if (parsed.error) return { inputs: {}, error: parsed.error };
    if (parsed.include) inputs[name] = parsed.value;
  }
  return { inputs, error: "" };
}
