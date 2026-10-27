import { executeExtensionQuery } from "@/pages/extensions/api";
import { fetchJson } from "@/lib/http/client";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type NotificationEntity = Record<string, unknown>;
export type ProjectNotificationProvider = {
  provider_key: string;
  display_name: string;
  provider_type_key: string;
  enabled: boolean;
  provider_enabled: boolean;
  /** Capabilities the provider declares; the UI reads these, never the key. */
  features: string[];
  /**
   * What this provider addresses when it delivers — `recipient` or `channel`.
   * Derived by the platform from the extension's own typed declaration.
   */
  notification_target_kind: string;
  /** Format declared on normalized subscription recipient items; no implicit email. */
  notification_recipient_format?: string | null;
  [key: string]: unknown;
};
export type ProviderChannel = {
  channel_id: string;
  display_name: string;
  chat_id: string;
  enabled: boolean;
  capabilities: {
    notify: boolean;
    human_decision: boolean;
  };
  [key: string]: unknown;
};

export type ProjectNotificationRoutesResponse = {
  notification_routes: NotificationEntity[];
  project_id: string;
};

export type ProjectNotificationWorkflow = {
  workflow_id: string;
  display_name?: string;
};

export async function fetchProjectNotificationWorkflows(projectId: string, t: I18nContextValue["t"] = fallbackContext.t): Promise<ProjectNotificationWorkflow[]> {
  const payload = await fetchJson<unknown>(
    `/api/projects/${encodeURIComponent(projectId)}/workflow-catalog?enabled_only=false`,
  );
  const rows = objectOrEmpty(payload).workflows;
  if (!Array.isArray(rows) || rows.some((value) => {
    const row = objectOrEmpty(value);
    return typeof row.workflow_id !== "string" || !row.workflow_id.trim();
  })) {
    throw new Error(t("react.projects.workflow_inventory_unavailable", "Project workflow inventory is unavailable."));
  }
  return rows.map((value) => {
    const row = objectOrEmpty(value);
    return { workflow_id: textOrEmpty(row.workflow_id), display_name: textOrEmpty(row.display_name) };
  });
}

function objectOrEmpty(value: unknown): NotificationEntity {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as NotificationEntity)
    : {};
}

function listFromPayload(payload: unknown, preferredKey: string): NotificationEntity[] {
  if (Array.isArray(payload)) {
    return payload.filter((row): row is NotificationEntity => Boolean(row) && typeof row === "object");
  }
  const obj = objectOrEmpty(payload);
  const preferred = obj[preferredKey];
  if (Array.isArray(preferred)) {
    return preferred.filter((row): row is NotificationEntity => Boolean(row) && typeof row === "object");
  }
  for (const value of Object.values(obj)) {
    if (Array.isArray(value)) {
      return value.filter((row): row is NotificationEntity => Boolean(row) && typeof row === "object");
    }
  }
  return [];
}

function textOrEmpty(value: unknown): string {
  return stringFromUnknownValue(value ?? "").trim();
}

function booleanOr(value: unknown, fallback = false): boolean {
  return value === undefined ? fallback : Boolean(value);
}

function normalizeProvider(value: unknown): ProjectNotificationProvider | null {
  const row = objectOrEmpty(value);
  const providerKey = textOrEmpty(row.provider_key || row.integration_key || row.extension_id);
  if (!providerKey) {
    return null;
  }
  return {
    ...row,
    provider_key: providerKey,
    display_name: textOrEmpty(row.display_name || providerKey) || providerKey,
    provider_type_key: textOrEmpty(row.provider_type_key || "notification_provider"),
    enabled: booleanOr(row.enabled, true),
    provider_enabled: booleanOr(row.provider_enabled, true),
    features: Array.isArray(row.features)
      ? row.features.map((feature) => textOrEmpty(feature)).filter(Boolean)
      : [],
    // A provider that declares nothing addresses a channel.
    notification_target_kind: textOrEmpty(row.notification_target_kind) || "channel",
    notification_recipient_format: textOrEmpty(row.notification_recipient_format) || null,
  };
}

function normalizeChannel(value: unknown): ProviderChannel | null {
  const row = objectOrEmpty(value);
  const channelId = textOrEmpty(row.channel_id);
  if (!channelId) {
    return null;
  }
  const capabilities = objectOrEmpty(row.capabilities);
  return {
    ...row,
    channel_id: channelId,
    display_name: textOrEmpty(row.display_name || channelId) || channelId,
    chat_id: textOrEmpty(row.chat_id),
    enabled: booleanOr(row.enabled, true),
    capabilities: {
      notify: booleanOr(capabilities.notify, false),
      human_decision: booleanOr(capabilities.human_decision, false),
    },
  };
}

function parseExtensionQueryRows(payload: unknown): NotificationEntity[] {
  const root = objectOrEmpty(payload);
  const query = objectOrEmpty(root.query);
  const response = objectOrEmpty(query.response);
  const values = objectOrEmpty(response.values);
  const channelValues = values.channels;
  if (Array.isArray(channelValues)) {
    return channelValues.filter((row): row is NotificationEntity => Boolean(row) && typeof row === "object");
  }
  const items = response.items;
  if (Array.isArray(items)) {
    return items.filter((row): row is NotificationEntity => Boolean(row) && typeof row === "object");
  }
  return [];
}

export async function fetchProjectNotificationRoutes(
  projectId: string,
): Promise<ProjectNotificationRoutesResponse> {
  const payload = await fetchJson<unknown>(
    `/api/projects/${encodeURIComponent(projectId)}/notification-routes?enabled_only=false`,
  );
  const obj = objectOrEmpty(payload);
  return {
    notification_routes: listFromPayload(payload, "notification_routes"),
    project_id: firstStringFromUnknownValue([obj.project_id, projectId, ""]).trim(),
  };
}

export async function saveProjectNotificationRoutes(
  projectId: string,
  routes: NotificationEntity[],
): Promise<ProjectNotificationRoutesResponse> {
  const payload = await fetchJson<unknown>(
    `/api/projects/${encodeURIComponent(projectId)}/notification-routes`,
    {
      method: "POST",
      body: JSON.stringify({ routes }),
    },
  );
  const obj = objectOrEmpty(payload);
  return {
    notification_routes: listFromPayload(payload, "notification_routes"),
    project_id: firstStringFromUnknownValue([obj.project_id, projectId, ""]).trim(),
  };
}

export async function fetchNotificationProviders(): Promise<NotificationEntity[]> {
  const payload = await fetchJson<unknown>(
    "/api/providers?enabled_only=false&provider_type_key=notification_provider",
  );
  return listFromPayload(payload, "providers");
}

export async function fetchProjectNotificationProviders(
  projectId: string,
): Promise<ProjectNotificationProvider[]> {
  const payload = await fetchJson<unknown>(
    `/api/projects/${encodeURIComponent(projectId)}/providers?enabled_only=true`,
  );
  return listFromPayload(payload, "project_providers")
    .map((row) => normalizeProvider(row))
    .filter(isTruthy)
    .filter((row) => row.provider_type_key === "notification_provider");
}

export async function fetchProjectProviderChannels(
  projectId: string,
  providerKey: string,
): Promise<ProviderChannel[]> {
  const normalizedProjectId = textOrEmpty(projectId);
  const normalizedProviderKey = textOrEmpty(providerKey);
  if (!normalizedProjectId || !normalizedProviderKey) {
    return [];
  }
  const payload = await executeExtensionQuery(normalizedProviderKey, "list_channels", {
    context: { project_id: normalizedProjectId },
    params: {},
  });
  return parseExtensionQueryRows(payload)
    .map((row) => normalizeChannel(row))
    .filter(isTruthy);
}
