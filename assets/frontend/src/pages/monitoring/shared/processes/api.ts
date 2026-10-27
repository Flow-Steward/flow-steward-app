import { fetchJson } from "@/lib/http/client";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type ProcessesPayload = {
  jobs?: Array<Record<string, unknown>>;
  subprocesses?: Array<Record<string, unknown>>;
  suspected_orphans?: Array<Record<string, unknown>>;
  project_leases?: Array<Record<string, unknown>>;
  workers?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export async function fetchProcessesPayload() {
  return fetchJson<ProcessesPayload>("/api/processes");
}

export async function cancelProcessJob(jobId: string | number) {
  return fetchJson<unknown>(`/api/jobs/${encodeURIComponent(stringFromUnknownValue(jobId))}/cancel`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function killWorkerProcess(pid: string | number) {
  return fetchJson<unknown>(`/api/processes/${encodeURIComponent(stringFromUnknownValue(pid))}/kill`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}
