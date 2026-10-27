import { fetchJson } from "@/lib/http/client";

export type DashboardSummary = {
  hero_kpis?: Record<string, { value?: number | string; label?: string }>;
  groups?: Record<string, Array<Record<string, unknown>>>;
  breakdown_rows?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export async function fetchDashboardSummary(range: string) {
  const params = new URLSearchParams({ range });
  return fetchJson<DashboardSummary>(`/api/v2/dashboard/summary?${params.toString()}`);
}
