import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { type DsBadgeTone, type DsOption } from "@/components/shared/design-system";

import { type EventCommandRecord, type EventFacetOption, type EventListItem } from "@/pages/monitoring/shared/events/api";

import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type Entity = Record<string, unknown>;

export function eventRef(row: EventListItem) {
  return firstStringFromUnknownValue([row.event_id, row.id, ""]).trim();
}

export function normalizeStatus(row: { status?: unknown; dispatch_status?: unknown }) {
  return firstStringFromUnknownValue([row.status, row.dispatch_status, ""]).trim().toLowerCase();
}

export function backendLabelOrCode(label: unknown, code: unknown, fallback: string, limit = 160) {
  const readableLabel = readableText(label, limit);
  if (readableLabel) {
    return readableLabel;
  }
  const readableCode = firstStringFromUnknownValue([code, ""]).trim();
  return readableCode || fallback;
}

export function eventStatusLabel(row: { status_label?: unknown; status?: unknown; dispatch_status?: unknown }, t: I18nContextValue["t"] = fallbackContext.t) {
  return backendLabelOrCode(row.status_label, normalizeStatus(row), t("dashboard.classic.breakdown.unknown_project", "Unknown"));
}

export function eventTypeLabel(row: { event_type_label?: unknown; event_type?: unknown; type?: unknown }, t: I18nContextValue["t"] = fallbackContext.t) {
  return backendLabelOrCode(row.event_type_label, row.event_type || row.type, t("react.monitoring.unknown_event", "Unknown event"));
}

export function workflowPath(workflowId: string) {
  const workflowRoute = `/registry/workflows/${encodeURIComponent(workflowId)}`;
  if (globalThis.window === undefined) {
    return workflowRoute;
  }
  return globalThis.location.pathname.startsWith("/app/") || globalThis.location.pathname === "/app"
    ? `/app${workflowRoute}`
    : workflowRoute;
}

export function eventWorkflowId(row: { workflow_id?: unknown }) {
  return firstStringFromUnknownValue([row.workflow_id, ""]).trim();
}

export function commandTypeLabel(row: EventCommandRecord | Entity, t: I18nContextValue["t"] = fallbackContext.t): string {
  const canonicalLabel = readableText(row.command_type_label, 160);
  if (canonicalLabel) {
    return canonicalLabel;
  }
  const code = firstStringFromUnknownValue([row.command_type, row.type, ""]).trim();
  if (code) {
    return code;
  }
  return readableText(row.command_label || row.label, 160) || t("processes.columns.command", "Command");
}

export function commandStatusLabel(row: EventCommandRecord | Entity, t: I18nContextValue["t"] = fallbackContext.t): string {
  return backendLabelOrCode(row.status_label, row.status || row.state, t("dashboard.classic.breakdown.unknown_project", "Unknown"), 120);
}

export function eventWorkflowLabel(row: Entity): string {
  const workflowId = eventWorkflowId(row);
  const metadata = objectOrEmpty(row.metadata || row.payload || row.attributes);
  const display = readableText(
    row.workflow_display_name ||
      row.workflow_label ||
      row.workflow_name ||
      row.selected_workflow_display_name ||
      metadata.workflow_display_name ||
      metadata.workflow_label ||
      metadata.workflow_name,
    160,
  );
  if (display && display !== workflowId) {
    return display;
  }
  return workflowId;
}

export function canRedrive(status: string) {
  return status === "failed" || status === "dead_lettered";
}

export function objectOrEmpty(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

export function normalizeValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "string" || typeof value === "number") {
    return stringFromUnknownValue(value);
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (Array.isArray(value)) {
    const readableItems = value
      .map((item) => readableText(item))
      .filter(isTruthy)
      .slice(0, 4);
    return readableItems.length ? readableItems.join(", ") : "Recorded details available in advanced metadata.";
  }
  return readableText(value) || "Recorded details available in advanced metadata.";
}

export function readableText(value: unknown, limit = 260): string {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    const text = stringFromUnknownValue(value).trim();
    return text.length > limit ? `${text.slice(0, limit - 1).trim()}…` : text;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => readableText(item, limit))
      .filter(isTruthy)
      .slice(0, 4)
      .join(", ");
  }
  if (typeof value === "object") {
    const record = value as Entity;
    for (const key of ["summary", "message", "result", "detail", "title", "reason", "path", "url", "href"]) {
      const text = readableText(record[key], limit);
      if (text) {
        return text;
      }
    }
  }
  return "";
}

export type ActiveFilterChip = {
  key: string;
  label: string;
  href?: string;
  clear: () => void;
};

const STATUS_TONES: Record<string, DsBadgeTone> = {
  failed: "danger",
  dead_lettered: "danger",
  succeeded: "green",
  dispatched: "green",
  delivered: "green",
  completed: "green",
  pending: "orange",
  queued: "orange",
  retrying: "orange",
};

export function statusTone(status: string): DsBadgeTone {
  return STATUS_TONES[status] || "neutral";
}

export function facetOptions(options: EventFacetOption[], placeholder: string): DsOption[] {
  const choices = options.flatMap((option) => {
    const value = firstStringFromUnknownValue([option.value, ""]).trim();
    if (!value) return [];
    const label = firstStringFromUnknownValue([option.label, value]).trim();
    const count = Number(option.count || 0);
    return [{ value, label: count > 0 ? `${label} (${count})` : label }];
  });
  // The empty value is a real choice, so "All …" can be picked again to clear.
  return [{ value: "", label: placeholder }, ...choices];
}

type FilterLabels = {
  workflows: Map<string, string>;
  families: Map<string, string>;
  categories: Map<string, string>;
  types: Map<string, string>;
  statuses: Map<string, string>;
};

type FilterValues = {
  workflowId: string;
  eventFamily: string;
  eventCategory: string;
  eventType: string;
  status: string;
};

type FilterClearActions = {
  workflow: () => void;
  family: () => void;
  category: () => void;
  type: () => void;
  status: () => void;
};

export function createActiveFilterChips(
  values: FilterValues,
  labels: FilterLabels,
  clear: FilterClearActions,
): ActiveFilterChip[] {
  const definitions = [
    {
      key: "workflow",
      prefix: "Workflow",
      value: values.workflowId,
      labels: labels.workflows,
      href: workflowPath(values.workflowId),
      clear: clear.workflow,
    },
    {
      key: "family",
      prefix: "Family",
      value: values.eventFamily,
      labels: labels.families,
      clear: clear.family,
    },
    {
      key: "category",
      prefix: "Subtype",
      value: values.eventCategory,
      labels: labels.categories,
      clear: clear.category,
    },
    {
      key: "type",
      prefix: "Type",
      value: values.eventType,
      labels: labels.types,
      clear: clear.type,
    },
    {
      key: "status",
      prefix: "Status",
      value: values.status,
      labels: labels.statuses,
      clear: clear.status,
    },
  ];
  return definitions
    .filter((definition) => Boolean(definition.value))
    .map((definition) => ({
      key: definition.key,
      label: `${definition.prefix}: ${definition.labels.get(definition.value) || definition.value}`,
      href: definition.href,
      clear: definition.clear,
    }));
}

export type DrawerFeedback = { message: string; tone: "success" | "danger" };
