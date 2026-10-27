import { fetchJson } from "@/lib/http/client";

export type MonitoringSummary = {
  ready?: boolean;
  summary?: {
    checks_total?: number;
    checks_ok?: number;
    checks_failed?: number;
  };
  meta?: Record<string, unknown>;
  checks?: Record<string, unknown>;
  components?: unknown;
  [key: string]: unknown;
};

export type MonitoringStorage = {
  generated_at?: string;
  backend_matrix?: Record<string, unknown>;
  summary?: Record<string, unknown>;
  memory?: Record<string, unknown>;
  vector?: Record<string, unknown>;
  [key: string]: unknown;
};

export type MonitoringStorageOrphanCleanupPayload = {
  backend: string;
  scope: string;
  confirm: string;
};

export type MonitoringStorageOrphanCleanupResult = {
  status?: string;
  backend?: string;
  scope?: string;
  message?: string;
  deleted?: Record<string, unknown>;
  [key: string]: unknown;
};

export async function fetchMonitoringSummary() {
  return fetchJson<MonitoringSummary>("/api/system/monitoring/summary");
}

export async function fetchMonitoringStorage() {
  return fetchJson<MonitoringStorage>("/api/system/monitoring/storage");
}

export async function cleanupMonitoringStorageOrphan(payload: MonitoringStorageOrphanCleanupPayload) {
  return fetchJson<MonitoringStorageOrphanCleanupResult>("/api/system/monitoring/storage/orphans/cleanup", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
