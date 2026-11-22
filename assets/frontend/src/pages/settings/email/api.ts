import { fetchJson } from "@/lib/http/client";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

/** The connection that sends the platform's own email, as the server last confirmed it. */
export type SystemEmailSender = {
  connection_id: string;
  display_name: string;
  connection_type: string;
  project_id: string;
  extension_install_id: string;
  /** `admin` (chosen here) or `upgrade_pin` (carried over by an upgrade). */
  source: string;
  confirmed_by: string;
  confirmed_at: string;
};

/** A project connection that could send system email. */
export type SystemEmailCandidate = {
  connection_id: string;
  display_name: string;
  connection_type: string;
  project_id: string;
  project_name: string;
  extension_key: string;
  extension_display_name: string;
  extension_install_id: string;
};

export type SystemEmailSettings = {
  available: boolean;
  /** Why system email is not available; "" when it is. */
  reason: string;
  selected: SystemEmailSender | null;
  candidates: SystemEmailCandidate[];
};

const SYSTEM_EMAIL_PATH = "/api/settings/system-email";

function objectOrEmpty(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string {
  return stringFromUnknownValue(value ?? "").trim();
}

function normalizeSender(value: unknown): SystemEmailSender | null {
  const row = objectOrEmpty(value);
  const connectionId = text(row.connection_id);
  if (!connectionId) return null;
  return {
    connection_id: connectionId,
    display_name: text(row.display_name),
    connection_type: text(row.connection_type),
    project_id: text(row.project_id),
    extension_install_id: text(row.extension_install_id),
    source: text(row.source),
    confirmed_by: text(row.confirmed_by),
    confirmed_at: text(row.confirmed_at),
  };
}

function normalizeCandidate(value: unknown): SystemEmailCandidate | null {
  const row = objectOrEmpty(value);
  const connectionId = text(row.connection_id);
  if (!connectionId) return null;
  return {
    connection_id: connectionId,
    display_name: text(row.display_name),
    connection_type: text(row.connection_type),
    project_id: text(row.project_id),
    project_name: text(row.project_name),
    extension_key: text(row.extension_key),
    extension_display_name: text(row.extension_display_name),
    extension_install_id: text(row.extension_install_id),
  };
}

function normalizeSettings(payload: unknown): SystemEmailSettings {
  const root = objectOrEmpty(payload);
  const candidates = Array.isArray(root.candidates) ? root.candidates : [];
  return {
    available: root.available === true,
    reason: text(root.reason),
    selected: normalizeSender(root.selected),
    candidates: candidates
      .map((candidate) => normalizeCandidate(candidate))
      .filter((candidate): candidate is SystemEmailCandidate => candidate !== null),
  };
}

export async function fetchSystemEmailSettings(): Promise<SystemEmailSettings> {
  return normalizeSettings(await fetchJson<unknown>(SYSTEM_EMAIL_PATH));
}

/** Choose a connection as the sender and confirm it in one step. */
export async function selectSystemEmailSender(connectionId: string): Promise<SystemEmailSettings> {
  return normalizeSettings(await fetchJson<unknown>(SYSTEM_EMAIL_PATH, {
    method: "PUT",
    body: JSON.stringify({ connection_id: connectionId }),
  }));
}

/** Confirm the current sender again, after its connection changed. */
export async function confirmSystemEmailSender(): Promise<SystemEmailSettings> {
  return normalizeSettings(await fetchJson<unknown>(`${SYSTEM_EMAIL_PATH}/confirm`, { method: "POST" }));
}

/** Stop sending system email: clears the sender. */
export async function clearSystemEmailSender(): Promise<SystemEmailSettings> {
  return normalizeSettings(await fetchJson<unknown>(SYSTEM_EMAIL_PATH, { method: "DELETE" }));
}
