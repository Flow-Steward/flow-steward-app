import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type ChangelogEntry = {
  id?: number | string;
  entity_type: string;
  entity_id: string;
  action?: string;
  revision_kind?: "content" | "lifecycle";
  author?: string;
  created_at?: string;
  snapshot?: Record<string, unknown>;
  [key: string]: unknown;
};

export type ChangelogListParams = {
  entityType: string;
  entityId: string;
  limit: number;
};

export type ChangelogDiff = {
  comparison_kind?: "content" | "lifecycle";
  entity_type?: string;
  entity_id?: string;
  from_entry_id?: string | null;
  to_entry_id?: string | null;
  changes?: Array<{
    path?: string;
    change?: string;
    from?: unknown;
    to?: unknown;
  }>;
  summary?: {
    total?: number;
    added?: number;
    removed?: number;
    changed?: number;
  };
  [key: string]: unknown;
};

type ChangelogStats = Record<string, unknown>;
type ChangelogValidation = {
  valid?: boolean;
  errors?: unknown[];
  warnings?: unknown[];
  [key: string]: unknown;
};
type RegistrySearchResult = {
  entity_type?: string;
  entity_id?: string;
  display_name?: string;
  description?: string;
  enabled?: boolean;
  [key: string]: unknown;
};

function normalizeEntries(payload: unknown): ChangelogEntry[] {
  if (Array.isArray(payload)) {
    return payload.filter((row): row is ChangelogEntry => Boolean(row) && typeof row === "object");
  }
  if (payload && typeof payload === "object") {
    for (const value of Object.values(payload as Record<string, unknown>)) {
      if (Array.isArray(value)) {
        return value.filter((row): row is ChangelogEntry => Boolean(row) && typeof row === "object");
      }
    }
  }
  return [];
}

export async function fetchChangelogList(params: ChangelogListParams) {
  const query = new URLSearchParams();
  if (params.entityType.trim()) {
    query.set("entity_type", params.entityType.trim());
  }
  if (params.entityId.trim()) {
    query.set("entity_id", params.entityId.trim());
  }
  query.set("limit", stringFromUnknownValue(Math.max(1, Math.min(200, params.limit))));

  const payload = await fetchJson<unknown>(`/api/changelog?${query.toString()}`);
  return normalizeEntries(payload);
}

export async function fetchChangelogHistory(entityType: string, entityId: string, limit = 50) {
  return fetchJson<ChangelogEntry[]>(
    `/api/changelog/${encodeURIComponent(entityType)}/${encodeURIComponent(entityId)}/history?limit=${Math.max(
      1,
      Math.min(200, limit),
    )}`,
  );
}

export async function fetchChangelogDiff(args: {
  entityType: string;
  entityId: string;
  fromEntryId?: string;
  toEntryId?: string;
}) {
  const query = new URLSearchParams();
  if (args.fromEntryId) {
    query.set("from_entry_id", stringFromUnknownValue(args.fromEntryId));
  }
  if (args.toEntryId) {
    query.set("to_entry_id", stringFromUnknownValue(args.toEntryId));
  }
  return fetchJson<ChangelogDiff>(
    `/api/changelog/${encodeURIComponent(args.entityType)}/${encodeURIComponent(args.entityId)}/diff?${query.toString()}`,
  );
}

export async function fetchRegistryStats() {
  return fetchJson<ChangelogStats>("/api/changelog/stats");
}

export async function fetchRegistryValidation() {
  return fetchJson<ChangelogValidation>("/api/changelog/validate");
}

export async function fetchChangelogSearch(args: { query: string; entityType?: string }) {
  const query = new URLSearchParams();
  query.set("q", firstStringFromUnknownValue([args.query, ""]).trim());
  if (firstStringFromUnknownValue([args.entityType, ""]).trim()) {
    query.set("entity_type", firstStringFromUnknownValue([args.entityType, ""]).trim());
  }
  return fetchJson<RegistrySearchResult[]>(`/api/changelog/search?${query.toString()}`);
}

/** A workflow revision a restore can be guarded by: a positive safe integer. */
export function isWorkflowRevision(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

/**
 * The workflow's current revision, read once when a reader opens a revision to
 * restore. It is the baseline that restore is guarded by, not part of restore.
 */
export async function fetchWorkflowCurrentRevision(workflowId: string): Promise<number> {
  const workflow = await fetchJson<{ revision?: unknown }>(
    `/api/workflows/${encodeURIComponent(workflowId)}`,
  );
  const revision = workflow && typeof workflow === "object" ? workflow.revision : undefined;
  if (!isWorkflowRevision(revision)) {
    throw new Error(`Workflow '${workflowId}' did not report its current revision.`);
  }
  return revision;
}

export async function restoreChangelogEntry(args: {
  entityType: string;
  entityId: string;
  entryId: string;
  /**
   * The workflow revision the reader was shown; required for a workflow. A newer
   * save then answers 409 instead of being overwritten. Never read here: a fresh
   * read at click time would accept whatever the server has now.
   */
  expectedRevision?: number;
}) {
  if (args.entityType === "workflow") {
    if (!isWorkflowRevision(args.expectedRevision)) {
      throw new Error("A workflow is restored only against the revision that was shown.");
    }
    return fetchJson<Record<string, unknown>>(
      `/api/workflows/${encodeURIComponent(args.entityId)}/restore`,
      {
        method: "POST",
        body: JSON.stringify({ entry_id: args.entryId, expected_revision: args.expectedRevision }),
      },
    );
  }
  return fetchJson<Record<string, unknown>>(
    `/api/changelog/${encodeURIComponent(args.entityType)}/${encodeURIComponent(args.entityId)}/restore`,
    {
      method: "POST",
      body: JSON.stringify({ entry_id: args.entryId }),
    },
  );
}
