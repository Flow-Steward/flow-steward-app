import { useI18n } from "@/lib/i18n/useI18n";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import {
  normalizeGlobalSearchGroups,
  type GlobalSearchGroup,
  type GlobalSearchItem,
} from "@/components/layout/app-shell-helpers";
import {
  categoryCountsFromGroups,
  filterReducer,
  searchParamsFromFilters,
  type CategoryCount,
  type FilterAction,
  type FilterDefinition,
  type FilterState,
} from "@/components/search/advanced-search-state";
import { fetchJson } from "@/lib/http/client";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

const SEARCH_DEBOUNCE_MS = 250;
const SEARCH_PAGE_SIZE = 20;

export type SearchCategoryOption = {
  value: string;
  label: string;
};

export type AdvancedSearchConfig = {
  defaultCategory: string;
  categories: SearchCategoryOption[];
  groupLabels: Record<string, string>;
  statusOptions: Array<{ value: string; label: string }>;
  updatedOptions: Array<{ value: string; label: string }>;
};

export type AdvancedSearchView = "initial" | "searching" | "results" | "empty" | "error";

function optionList(raw: unknown): Array<{ value: string; label: string }> {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw
    .map((entry) => {
      const record = (entry ?? {}) as Record<string, unknown>;
      const value = firstStringFromUnknownValue([record.value, ""]).trim();
      const label = firstStringFromUnknownValue([record.label, value]).trim();
      return value ? { value, label } : null;
    })
    .filter((option): option is { value: string; label: string } => option !== null);
}

function parseConfig(payload: unknown): AdvancedSearchConfig | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const record = payload as Record<string, unknown>;
  const categories = optionList(record.categories).filter((option) => option.value !== "kb");
  if (!categories.length) {
    return null;
  }
  const filters = (record.filters ?? {}) as Record<string, unknown>;
  const groupLabels: Record<string, string> = {};
  const rawLabels = record.group_labels;
  if (rawLabels && typeof rawLabels === "object" && !Array.isArray(rawLabels)) {
    for (const [key, value] of Object.entries(rawLabels as Record<string, unknown>)) {
      const label = firstStringFromUnknownValue([value, ""]).trim();
      if (label && key !== "kb") {
        groupLabels[key] = label;
      }
    }
  }
  const requestedDefault = firstStringFromUnknownValue([record.default_category, ""]).trim();
  return {
    defaultCategory: categories.some((option) => option.value === requestedDefault)
      ? requestedDefault
      : categories[0]?.value || "all",
    categories,
    groupLabels,
    statusOptions: optionList(filters.status),
    updatedOptions: optionList(filters.updated),
  };
}

function bestMatchFrom(payload: unknown): GlobalSearchItem | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const raw = (payload as Record<string, unknown>).best_match;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const result = raw as GlobalSearchItem;
  return firstStringFromUnknownValue([result.group, ""]).trim() === "kb" ? null : result;
}

function itemKey(item: GlobalSearchItem): string {
  const group = firstStringFromUnknownValue([item.group, ""]).trim();
  const id = firstStringFromUnknownValue([item.id, ""]).trim();
  return `${group}:${id}`;
}

/**
 * The promoted result stays inside its group in the response so the group counters remain
 * truthful; suppressing the repeat is the interface's job.
 */
export function groupsWithoutBestMatch(
  groups: GlobalSearchGroup[],
  bestMatch: GlobalSearchItem | null,
): GlobalSearchGroup[] {
  if (!bestMatch) {
    return groups;
  }
  const promoted = itemKey(bestMatch);
  return groups.map((group) => ({
    ...group,
    items: group.items.filter((item) => itemKey(item) !== promoted),
  }));
}

/**
 * The project registry is read lazily on first open rather than on every page load: the
 * Project refinement is the only surface in the shell that needs it.
 */
export function projectFilterOptions(payload: unknown): Array<{ value: string; label: string }> {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return [];
  }
  const rows = (payload as Record<string, unknown>).projects;
  if (!Array.isArray(rows)) {
    return [];
  }
  return rows
    .map((row) => {
      const record = (row ?? {}) as Record<string, unknown>;
      const value = firstStringFromUnknownValue([record.id, ""]).trim();
      const label = firstStringFromUnknownValue([record.name, value]).trim();
      return value ? { value, label } : null;
    })
    .filter((option): option is { value: string; label: string } => option !== null);
}

export type UseAdvancedSearchOptions = {
  open: boolean;
  selectedWorkspaceId: string;
  /** Permission-scoped registry; the search configuration never publishes it. */
  workspaceOptions: Array<{ value: string; label: string }>;
};

export function useAdvancedSearch({
  open,
  selectedWorkspaceId,
  workspaceOptions,
}: UseAdvancedSearchOptions) {
  const { t } = useI18n();
  const [projectOptions, setProjectOptions] = useState<Array<{ value: string; label: string }>>([]);
  // The dialog opens on the active workspace scope, as the pattern requires. The scope is
  // shown as a token only when the workspace registry is readable; it applies either way.
  const initialFilters = useMemo<FilterState>(
    () => ({
      active:
        selectedWorkspaceId && workspaceOptions.length
          ? [{ k: "workspace", v: selectedWorkspaceId }]
          : [],
      menu: null,
    }),
    // Only the values present when the dialog mounts seed the reducer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [config, setConfig] = useState<AdvancedSearchConfig | null>(null);
  const [category, setCategory] = useState("all");
  const [draft, setDraft] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [groups, setGroups] = useState<GlobalSearchGroup[]>([]);
  const [bestMatch, setBestMatch] = useState<GlobalSearchItem | null>(null);
  const [counts, setCounts] = useState<Record<string, CategoryCount>>({});
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [completedQuery, setCompletedQuery] = useState<string | null>(null);
  // Relative ages are measured from when the results arrived, not from render time.
  const [resultsAt, setResultsAt] = useState(0);
  const [filters, dispatchFilter] = useReducer(filterReducer, initialFilters);
  const [activeIndex, setActiveIndex] = useState(0);
  const [retryToken, setRetryToken] = useState(0);

  const generationRef = useRef(0);
  const configLoadedRef = useRef(false);

  // Workspace and Project options come from their own registries; Status and Updated come
  // from the published search configuration. A key with no options is not rendered.
  const filterDefinitions = useMemo<FilterDefinition[]>(() => {
    const definitions: FilterDefinition[] = [];
    if (workspaceOptions.length) {
      definitions.push({ key: "workspace", label: t("project_mode.workspace", "Workspace"), options: workspaceOptions });
    }
    if (projectOptions.length) {
      definitions.push({ key: "project", label: t("events.columns.project", "Project"), options: projectOptions });
    }
    if (config?.statusOptions.length) {
      definitions.push({ key: "status", label: t("account_settings.card.status", "Status"), options: config.statusOptions });
    }
    if (config?.updatedOptions.length) {
      definitions.push({ key: "updated", label: t("react.shared.updated", "Updated"), options: config.updatedOptions });
    }
    return definitions;
  }, [config, projectOptions, workspaceOptions, t]);

  const requestParams = useMemo(() => searchParamsFromFilters(filters.active), [filters.active]);
  const requestSignature = useMemo(() => JSON.stringify(requestParams), [requestParams]);

  useEffect(() => {
    if (!open || configLoadedRef.current) {
      return;
    }
    configLoadedRef.current = true;
    ignoreAsyncResult(
      (async () => {
        try {
          const payload = await fetchJson<unknown>("/api/search/config");
          const parsed = parseConfig(payload);
          if (parsed) {
            setConfig(parsed);
            setCategory((current) => (current === "all" ? parsed.defaultCategory : current));
          }
        } catch {
          // A missing configuration must not disable search; the caller keeps its fallback.
        }
        try {
          const payload = await fetchJson<unknown>("/api/projects?per_page=200");
          setProjectOptions(projectFilterOptions(payload));
        } catch {
          // Without a readable project registry the Project refinement simply never renders.
        }
      })(),
    );
  }, [open]);

  const buildPath = useCallback(
    (args: { query: string; category: string; cursor?: string }) => {
      const params = new URLSearchParams();
      params.set("query", args.query);
      params.set("category", args.category);
      params.set("limit", String(SEARCH_PAGE_SIZE));
      // The active workspace scopes the request even when it is not shown as a token.
      if (selectedWorkspaceId && !requestParams.workspace_id) {
        params.set("workspace_id", selectedWorkspaceId);
      }
      for (const [key, value] of Object.entries(requestParams)) {
        params.set(key, value);
      }
      const cursor = (args.cursor || "").trim();
      if (cursor && args.category !== "all") {
        params.set("cursor", cursor);
      }
      return `/api/search?${params.toString()}`;
    },
    [requestParams, selectedWorkspaceId],
  );

  const reset = useCallback(() => {
    generationRef.current += 1;
    setGroups([]);
    setBestMatch(null);
    setCounts({});
    setError("");
    setSubmittedQuery("");
    setCompletedQuery(null);
    setLoading(false);
    setLoadingMore(false);
    setActiveIndex(0);
  }, []);

  useEffect(() => {
    if (!open) {
      return;
    }
    const needle = draft.trim();
    if (!needle) {
      reset();
      return;
    }
    const timer = globalThis.setTimeout(() => {
      const generation = generationRef.current + 1;
      generationRef.current = generation;
      setLoading(true);
      setError("");
      ignoreAsyncResult(
        (async () => {
          try {
            // Counts describe the query-plus-filter set before the selected category
            // narrows it, so they always come from the grouped `all` request.
            const countsPromise = fetchJson<unknown>(
              buildPath({ query: needle, category: "all" }),
            );
            const resultsPromise =
              category === "all"
                ? countsPromise
                : fetchJson<unknown>(buildPath({ query: needle, category }));
            const [countsPayload, resultsPayload] = await Promise.all([
              countsPromise,
              resultsPromise,
            ]);
            if (generationRef.current !== generation) {
              return;
            }
            setCounts(categoryCountsFromGroups(normalizeGlobalSearchGroups(countsPayload)));
            setGroups(normalizeGlobalSearchGroups(resultsPayload));
            setBestMatch(category === "all" ? bestMatchFrom(resultsPayload) : null);
            setResultsAt(Date.now());
            setSubmittedQuery(needle);
            setCompletedQuery(needle);
            setActiveIndex(0);
          } catch (error_) {
            if (generationRef.current !== generation) {
              return;
            }
            setGroups([]);
            setBestMatch(null);
            setError(error_ instanceof Error ? error_.message : t("react.shared.search_failed", "Search failed"));
            setCompletedQuery(needle);
          } finally {
            if (generationRef.current === generation) {
              setLoading(false);
            }
          }
        })(),
      );
    }, SEARCH_DEBOUNCE_MS);
    return () => globalThis.clearTimeout(timer);
  }, [open, draft, category, requestSignature, retryToken, buildPath, reset, t]);

  const loadMore = useCallback(() => {
    const group = groups[0];
    const cursor = group?.next_cursor || "";
    if (category === "all" || !cursor || loadingMore) {
      return;
    }
    const generation = generationRef.current;
    setLoadingMore(true);
    ignoreAsyncResult(
      (async () => {
        try {
          const payload = await fetchJson<unknown>(
            buildPath({ query: submittedQuery, category, cursor }),
          );
          if (generationRef.current !== generation) {
            return;
          }
          const incoming = normalizeGlobalSearchGroups(payload)[0];
          if (!incoming) {
            return;
          }
          // Appending must never replace the rows already on screen.
          setGroups((previous) =>
            previous.map((existing, index) =>
              index === 0 && existing.group === incoming.group
                ? {
                    ...existing,
                    total: Math.max(existing.total, incoming.total),
                    has_more: incoming.has_more,
                    maybe_more: incoming.maybe_more,
                    next_cursor: incoming.next_cursor,
                    items: [...existing.items, ...incoming.items],
                  }
                : existing,
            ),
          );
        } catch (error_) {
          if (generationRef.current === generation) {
            setError(error_ instanceof Error ? error_.message : t("react.shared.search_failed", "Search failed"));
          }
        } finally {
          if (generationRef.current === generation) {
            setLoadingMore(false);
          }
        }
      })(),
    );
  }, [buildPath, category, groups, loadingMore, submittedQuery, t]);

  const retry = useCallback(() => {
    // Re-run the current request without disturbing the query or the refinements.
    setError("");
    setRetryToken((current) => current + 1);
  }, []);

  const visibleGroups = useMemo(
    () => (category === "all" ? groupsWithoutBestMatch(groups, bestMatch) : groups),
    [category, groups, bestMatch],
  );

  const visibleItems = useMemo(() => {
    const promoted = category === "all" && bestMatch ? [bestMatch] : [];
    return [...promoted, ...visibleGroups.flatMap((group) => group.items)];
  }, [bestMatch, category, visibleGroups]);

  const view: AdvancedSearchView = useMemo(() => {
    if (error) {
      return "error";
    }
    if (loading) {
      return "searching";
    }
    // Never report an empty result before a query has actually completed.
    if (!draft.trim() || completedQuery === null) {
      return "initial";
    }
    return visibleItems.length ? "results" : "empty";
  }, [completedQuery, draft, error, loading, visibleItems.length]);

  const canLoadMore = Boolean(
    category !== "all" && groups[0]?.has_more && groups[0]?.next_cursor && view === "results",
  );

  return {
    activeIndex,
    bestMatch,
    canLoadMore,
    category,
    config,
    counts,
    draft,
    error,
    filterDefinitions,
    filters,
    dispatchFilter: dispatchFilter as (action: FilterAction) => void,
    loadMore,
    loadingMore,
    resultsAt,
    retry,
    setActiveIndex,
    setCategory,
    setDraft,
    submittedQuery,
    view,
    visibleGroups,
    visibleItems,
  };
}
