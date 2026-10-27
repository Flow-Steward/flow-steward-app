import { type ReactNode } from "react";

import { type DsBadgeTone } from "@/components/shared/design-system";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
/* eslint-disable @typescript-eslint/no-explicit-any */

import {
  triggerUiFieldPath,
  type WorkflowWebhookTrigger,
} from "@/pages/workflows/detail/detail-helpers";

/** Types, labels and small pieces the Triggers tab shares (Settings › Triggers). */
export type WorkflowTriggersSurfaceContext = Record<string, any>;
export type TriggersView = "list" | "webhook" | "provider";
export type WebhookExpectedInput = {
  name: string;
  required?: boolean;
  type?: string;
};
export type ProviderTriggerCatalogEntry = {
  catalog_id: string;
  config_schema?: unknown;
  description?: string;
  display_name?: string;
  provider_key?: string;
  ui_schema?: unknown;
};
export type ProviderTriggerUiField = {
  field_id?: unknown;
  label?: unknown;
  name?: unknown;
  options?: Array<{ label?: unknown; value?: unknown }>;
  path?: unknown;
  placeholder?: string;
  required?: unknown;
  widget?: unknown;
  widget_type?: unknown;
};
export type ProviderTriggerRow = {
  display_name?: string;
  enabled?: boolean;
  event_name?: string;
  provider_ingress_url?: string;
  provider_key?: string;
  readiness?: {
    errors?: unknown[];
    status?: unknown;
  };
  trigger_id: string;
  trigger_type_id?: string;
};
export type Readiness = { label: string; tone: DsBadgeTone; hint: string };

export const CONTENT_TYPES = [
  "application/json",
  "multipart/form-data",
  "application/x-www-form-urlencoded",
  "text/plain",
];

export function webhookSaveLabel(
  pending: boolean,
  editing: boolean,
  selected: boolean, t: I18nContextValue["t"] = fallbackContext.t
): string {
  if (pending) {
    return t("react.workflows.saving_trigger_progress", "Saving trigger…");
  }
  if (editing && selected) {
    return t("react.workflows.save_webhook_settings", "Save webhook settings");
  }
  return t("react.workflows.create_webhook", "Create webhook");
}

export function providerSaveLabel(pending: boolean, editing: boolean, t: I18nContextValue["t"]): string {
  if (pending) return t("react.workflows.saving_trigger_progress", "Saving trigger…");
  return editing
    ? t("react.workflows.display_save_provider_trigger", "Save provider trigger")
    : t("react.workflows.display_create_provider_trigger", "Create provider trigger");
}

export function countLabel(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

export function humanizeKey(value: string): string {
  const words = value.replaceAll(/[._-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "";
}

export function readinessMessages(errors: unknown): string[] {
  return (Array.isArray(errors) ? errors : [])
    .map((item) => {
      if (typeof item === "string") return item;
      const row = item && typeof item === "object" ? item as Record<string, unknown> : {};
      return firstStringFromUnknownValue([row.message, row.detail, row.code, ""]);
    })
    .filter(Boolean);
}

/** The server's ready / draft / invalid, in plain words: Ready, Off, Needs setup. */
export function providerReadiness(trigger: ProviderTriggerRow, t: I18nContextValue["t"]): Readiness {
  const hint = readinessMessages(trigger.readiness?.errors).join(" ");
  if (trigger.enabled === false) {
    return { label: t("react.workflows.trigger_readiness_off", "Off"), tone: "neutral", hint };
  }
  if (firstStringFromUnknownValue([trigger.readiness?.status, ""]) === "ready") {
    return { label: t("react.workflows.trigger_readiness_ready", "Ready"), tone: "green", hint: "" };
  }
  return { label: t("react.workflows.trigger_readiness_needs_setup", "Needs setup"), tone: "orange", hint };
}

export function webhookReadinessLabel(webhook: WorkflowWebhookTrigger, t: I18nContextValue["t"]): string {
  const status = firstStringFromUnknownValue([webhook.readiness?.status, ""]);
  if (!webhook.enabled) return t("react.workflows.trigger_readiness_off", "Off");
  if (status === "ready") return t("react.workflows.trigger_readiness_ready", "Ready");
  if (!status) return t("project_detail.connections.status.unknown", "unknown");
  return t("react.workflows.trigger_readiness_needs_setup", "Needs setup");
}

/** Required fields come from the trigger's config schema; null when the extension does not say. */
export function requiredFieldPaths(entry: ProviderTriggerCatalogEntry, fields: ProviderTriggerUiField[]): Set<string> | null {
  const schema = entry.config_schema && typeof entry.config_schema === "object"
    ? entry.config_schema as Record<string, unknown>
    : {};
  const fromSchema = Array.isArray(schema.required) ? schema.required.map(String) : null;
  const fromFields = fields.filter((field) => field.required === true).map(triggerUiFieldPath);
  if (!fromSchema && !fields.some((field) => typeof field.required === "boolean")) return null;
  return new Set([...(fromSchema || []), ...fromFields]);
}
