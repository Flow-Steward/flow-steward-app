import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type RouteDraft = {
  channel_id: string;
  clientId: string;
  connection_id: string;
  display_name: string;
  enabled: boolean;
  event_type: string;
  provider_key: string;
  route_key: string;
  severity: string;
  /** The shape this route is stored in, kept so a save cannot silently change it. */
  stored_target_kind: "recipient" | "channel" | "";
  workflow_id: string;
};

export type RouteModalMode = "create" | "edit";

export const SEVERITY_OPTIONS = ["any", "debug", "info", "warning", "error", "critical"];

export function textOrEmpty(value: unknown): string {
  return stringFromUnknownValue(value ?? "").trim();
}
