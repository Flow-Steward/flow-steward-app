import { useI18n } from "@/lib/i18n/useI18n";

import type { ReactNode } from "react";
import { Fragment, useEffect, useMemo, useRef } from "react";

import {
  Activity,
  Bot,
  LoaderCircle,
  Network,
  Search,
  Workflow,
  type LucideIcon,
} from "lucide-react";

import type { GlobalSearchGroup, GlobalSearchItem } from "@/components/layout/app-shell-helpers";
import {
  highlightSegments,
  resultMetaSegments,
  resultStatus,
  resultTitle,
} from "@/components/search/advanced-search-result-format";
import type { AdvancedSearchView } from "@/components/search/use-advanced-search";

const ICON_BY_GROUP: Record<string, LucideIcon> = {
  jobs: Network,
  events: Activity,
  runtime_messages: Bot,
  workflows: Workflow,
};

type AdvancedSearchResultsProps = {
  activeIndex: number;
  bestMatch: GlobalSearchItem | null;
  canLoadMore: boolean;
  error: string;
  groupLabel: (group: string) => string;
  groups: GlobalSearchGroup[];
  isGrouped: boolean;
  listboxId: string;
  loadingMore: boolean;
  onLoadMore: () => void;
  onOpenResult: (item: GlobalSearchItem) => void;
  onRetry: () => void;
  onSetActiveIndex: (index: number) => void;
  optionId: (index: number) => string;
  query: string;
  /** When the rendered results arrived; every row's relative age is measured from it. */
  resultsAt: number;
  selectedTabId: string;
  view: AdvancedSearchView;
};

function ResultRow({
  active,
  id,
  item,
  now,
  onActivate,
  onOpen,
  query,
}: Readonly<{
  active: boolean;
  id: string;
  item: GlobalSearchItem;
  now: number;
  onActivate: () => void;
  onOpen: () => void;
  query: string;
}>) {
  const { t } = useI18n();
  const Icon = ICON_BY_GROUP[String(item.group || "").trim()] ?? Search;
  const status = resultStatus(item);
  const segments = resultMetaSegments(item, now, t);
  const title = resultTitle(item, t);
  const highlightedTitle = highlightSegments(title, query).reduce<{
    items: Array<{ key: string; match: boolean; text: string }>;
    offset: number;
  }>(
    (result, segment) => ({
      items: [
        ...result.items,
        {
          ...segment,
          key: `${result.offset}-${segment.match ? "match" : "text"}`,
        },
      ],
      offset: result.offset + segment.text.length,
    }),
    { items: [], offset: 0 },
  ).items;

  const statusToneClass = status?.tone === "neutral" ? "" : ` is-${status?.tone}`;

  return (
    <button
      aria-selected={active}
      className="fs-advanced-search__result"
      id={id}
      onClick={onOpen}
      onPointerMove={onActivate}
      role="option"
      type="button"
    >
      <span className="fs-advanced-search__result-icon">
        <Icon aria-hidden="true" />
      </span>
      <span className="fs-advanced-search__result-copy">
        <span className="fs-advanced-search__result-title">
          {/* Unmatched runs stay bare text: wrapping each one would let the accessible
              name algorithm trim the spaces between them. */}
          {highlightedTitle.map((segment) =>
            segment.match ? (
              <mark className="fs-advanced-search__match" key={segment.key}>
                {segment.text}
              </mark>
            ) : (
              <Fragment key={segment.key}>{segment.text}</Fragment>
            ),
          )}
        </span>
        {segments.length ? (
          <span className="fs-advanced-search__result-meta">
            {"· "}
            <strong>{segments[0]}</strong>
            {segments.length > 1 ? ` · ${segments.slice(1).join(" · ")}` : null}
          </span>
        ) : null}
      </span>
      <span className="fs-advanced-search__result-actions">
        {status ? (
          <span
            aria-label={t("react.shared.status_value1", "Status: {value1}", { value1: String(status.label) })}
            className={`fs-advanced-search__status${statusToneClass}`}
          >
            {status.label}
          </span>
        ) : null}
        <span aria-hidden="true" className="fs-advanced-search__open-hint">
          ↵
        </span>
      </span>
    </button>
  );
}

function Message({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <div className="fs-advanced-search__message">
      <div className="fs-advanced-search__message-inner">{children}</div>
    </div>
  );
}

export function AdvancedSearchResults({
  activeIndex,
  bestMatch,
  canLoadMore,
  error,
  groupLabel,
  groups,
  isGrouped,
  listboxId,
  loadingMore,
  onLoadMore,
  onOpenResult,
  onRetry,
  onSetActiveIndex,
  optionId,
  query,
  resultsAt,
  selectedTabId,
  view,
}: Readonly<AdvancedSearchResultsProps>) {
  const { t } = useI18n();
  const regionRef = useRef<HTMLDivElement>(null);

  const sections = useMemo(() => {
    const rendered: Array<{ heading: string | null; items: GlobalSearchItem[] }> = [];
    if (isGrouped && bestMatch) {
      rendered.push({ heading: t("react.shared.best_match", "Best match"), items: [bestMatch] });
    }
    for (const group of groups) {
      if (!group.items.length) {
        continue;
      }
      rendered.push({ heading: isGrouped ? groupLabel(group.group) : null, items: group.items });
    }
    return rendered;
  }, [bestMatch, groupLabel, groups, isGrouped, t]);

  useEffect(() => {
    const active = regionRef.current?.querySelector<HTMLElement>('[aria-selected="true"]');
    active?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  let cursor = -1;

  return (
    <div
      aria-busy={view === "searching"}
      aria-label={t("react.shared.search_results", "Search results")}
      aria-labelledby={selectedTabId}
      className="fs-advanced-search__results"
      id={listboxId}
      ref={regionRef}
      role="listbox"
    >
      {view === "initial" ? (
        <Message>
          <Search aria-hidden="true" />
          <strong>{t("react.shared.search_across_flow_steward", "Search across Flow Steward")}</strong>
          <p>{t("react.shared.find_jobs_events_runtime_messages_workflows", "Find jobs, events, runtime messages, workflows.")}</p>
        </Message>
      ) : null}

      {view === "searching" ? (
        <Message>
          <LoaderCircle aria-hidden="true" className="fs-advanced-search__spinner" />
          <strong>{t("shell.search.loading", "Searching…")}</strong>
          <p>{t("react.shared.keeping_your_current_scope_and_filters", "Keeping your current scope and filters.")}</p>
        </Message>
      ) : null}

      {view === "empty" ? (
        <Message>
          <Search aria-hidden="true" />
          <strong>{t("react.shared.no_matching_results", "No matching results")}</strong>
          <p>{t("react.shared.try_a_shorter_query_another_category_or_remove_a_filter", "Try a shorter query, another category or remove a filter.")}</p>
        </Message>
      ) : null}

      {view === "error" ? (
        <div className="fs-advanced-search__message">
          <div className="fs-advanced-search__message-inner fs-advanced-search__error" role="alert">
            <strong>{t("react.shared.search_couldn_t_load", "Search couldn’t load")}</strong>
            <p>{error || t("react.shared.your_query_and_filters_are_still_here_try_again", "Your query and filters are still here. Try again.")}</p>
            <button className="fs-advanced-search__retry" onClick={onRetry} type="button">
              {t("job_security.retry", "Retry")} </button>
          </div>
        </div>
      ) : null}

      {view === "results" ? (
        <div className="fs-advanced-search__result-list">
          {sections.map((section) => (
            <div key={section.heading ?? "flat"}>
              {section.heading ? (
                <div className="fs-advanced-search__group">
                  {section.heading}
                </div>
              ) : null}
              {section.items.map((item) => {
                cursor += 1;
                const index = cursor;
                return (
                  <ResultRow
                    active={index === activeIndex}
                    id={optionId(index)}
                    item={item}
                    key={`${item.group}:${item.id}`}
                    now={resultsAt}
                    onActivate={() => onSetActiveIndex(index)}
                    onOpen={() => onOpenResult(item)}
                    query={query}
                  />
                );
              })}
            </div>
          ))}
          {canLoadMore ? (
            <button
              className="fs-advanced-search__load-more"
              data-loading={loadingMore ? "true" : undefined}
              disabled={loadingMore}
              onClick={onLoadMore}
              type="button"
            >
              {loadingMore ? t("shell.search.loading_more", "Loading…") : t("job_security.load_more", "Load more")}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
