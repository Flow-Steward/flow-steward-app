import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { KeyboardEvent } from "react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { Search, X } from "lucide-react";

import type { GlobalSearchItem } from "@/components/layout/app-shell-helpers";
import { AdvancedSearchFilters } from "@/components/search/advanced-search-filters";
import { AdvancedSearchResults } from "@/components/search/advanced-search-results";
import { AdvancedSearchTabs, type CategoryTab } from "@/components/search/advanced-search-tabs";
import { moveActiveIndex, resolveEscape } from "@/components/search/advanced-search-state";
import { useAdvancedSearch } from "@/components/search/use-advanced-search";

import "@/components/search/advanced-search.css";



function getTabLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  all: t("shell.search.category.all", "Everything"),
  jobs: t("jobs.page_title", "Jobs"),
  events: t("monitoring.card.events.title", "Events"),
  runtime_messages: t("react.shared.messages", "Messages"),
  workflows: t("project_edit.tab.workflows", "Workflows"),
};
}

const FALLBACK_CATEGORIES = [
  "all",
  "jobs",
  "events",
  "runtime_messages",
  "workflows",
];

type AdvancedSearchDialogProps = {
  canSearchAccountWide: boolean;
  onClose: () => void;
  onOpenResult: (item: GlobalSearchItem) => void;
  open: boolean;
  selectedWorkspaceId: string;
  workspaceOptions: Array<{ value: string; label: string }>;
};

export function AdvancedSearchDialog({
  canSearchAccountWide,
  onClose,
  onOpenResult,
  open,
  selectedWorkspaceId,
  workspaceOptions,
}: Readonly<AdvancedSearchDialogProps>) {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const baseId = useId();
  const listboxId = `${baseId}-results`;
  const [entered, setEntered] = useState(false);

  const search = useAdvancedSearch({ open, selectedWorkspaceId, workspaceOptions });
  const {
    activeIndex,
    bestMatch,
    canLoadMore,
    category,
    config,
    counts,
    dispatchFilter,
    draft,
    error,
    filterDefinitions,
    filters,
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
  } = search;

  const optionId = useCallback((index: number) => `${baseId}-option-${index}`, [baseId]);
  const tabId = useCallback((value: string) => `${baseId}-tab-${value}`, [baseId]);

  const tabs = useMemo<CategoryTab[]>(() => {
    const values = config?.categories.length
      ? config.categories.map((entry) => entry.value).filter((value) => value !== "kb")
      : FALLBACK_CATEGORIES;
    return values.map((value) => ({
      value,
      label: getTabLabels(t)[value] ?? config?.categories.find((c) => c.value === value)?.label ?? value,
      accessibleName: value === "runtime_messages" ? t("shell.search.category.runtime_messages", "Runtime messages") : undefined,
    }));
  }, [config, t]);

  const groupLabel = useCallback(
    (group: string) => config?.groupLabels[group] ?? getTabLabels(t)[group] ?? group,
    [config, t],
  );

  // A native dialog gives the modal a real focus trap instead of a hand-rolled one. The
  // surface is mounted only while open, so unmounting is what closes it.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog || !open) {
      return undefined;
    }
    if (!dialog.open) {
      dialog.showModal();
    }
    // The enter transition needs one frame with the pre-transition style applied.
    const frame = globalThis.requestAnimationFrame(() => {
      setEntered(true);
      inputRef.current?.focus({ preventScroll: true });
    });
    return () => globalThis.cancelAnimationFrame(frame);
  }, [open]);

  const closeMenu = useCallback(() => dispatchFilter({ type: "close-menu" }), [dispatchFilter]);

  // Only one filter menu may be open, and an outside press closes it.
  useEffect(() => {
    if (!filters.menu) {
      return undefined;
    }
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target?.closest(".fs-advanced-search__filter-zone")) {
        closeMenu();
      }
    };
    globalThis.document.addEventListener("pointerdown", onPointerDown);
    return () => globalThis.document.removeEventListener("pointerdown", onPointerDown);
  }, [closeMenu, filters.menu]);

  function onQueryKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex(
        moveActiveIndex(activeIndex, event.key === "ArrowDown" ? 1 : -1, visibleItems.length),
      );
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const item = visibleItems[activeIndex];
      if (item) {
        onOpenResult(item);
      }
    }
  }

  function onDialogKeyDown(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key !== "Escape") {
      return;
    }
    // Escape resolves the innermost layer first, so it never destroys more than expected.
    event.preventDefault();
    const action = resolveEscape({ menu: filters.menu, query: draft });
    if (action === "close-menu") {
      closeMenu();
    } else if (action === "clear-query") {
      setDraft("");
      inputRef.current?.focus();
    } else {
      onClose();
    }
  }

  const announcement = useMemo(() => {
    if (view === "searching") {
      return t("react.shared.searching", "Searching");
    }
    if (view === "error") {
      return t("react.shared.search_could_not_load", "Search couldn’t load");
    }
    if (view === "empty") {
      return t("react.shared.no_matching_results", "No matching results");
    }
    if (view === "results") {
      return t("react.shared.search_results_count", "{count} search results", { count: visibleItems.length });
    }
    return "";
  }, [view, visibleItems.length, t]);

  return (
    <dialog
      aria-label={t("react.shared.advanced_search", "Advanced search")}
      className="fs-advanced-search"
      data-state={entered ? "open" : "closed"}
      data-testid="advanced-search"
      onCancel={(event) => {
        // The browser's own Escape close would skip the resolution order above.
        event.preventDefault();
      }}
      onClose={onClose}
      onKeyDown={onDialogKeyDown}
      ref={dialogRef}
    >
      <div className="fs-advanced-search__panel">
        <form
          className="fs-advanced-search__search-row"
          onSubmit={(event) => event.preventDefault()}
          role="search"
        >
          <Search aria-hidden="true" />
          <input
            aria-activedescendant={
              view === "results" && activeIndex >= 0 ? optionId(activeIndex) : undefined
            }
            aria-autocomplete="list"
            aria-controls={listboxId}
            aria-expanded={view === "results"}
            aria-label={t("react.shared.search_flow_steward", "Search Flow Steward")}
            autoComplete="off"
            className="fs-advanced-search__input"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onQueryKeyDown}
            placeholder={t("react.shared.search_jobs_workflows_docs", "Search jobs, workflows, docs…")}
            ref={inputRef}
            role="combobox"
            spellCheck={false}
            type="search"
            value={draft}
          />
          {draft ? (
            <button
              aria-label={t("react.shared.clear_search", "Clear search")}
              className="fs-advanced-search__clear"
              onClick={() => {
                setDraft("");
                inputRef.current?.focus();
              }}
              type="button"
            >
              <X aria-hidden="true" />
            </button>
          ) : null}
          <button
            aria-label={t("react.shared.close_advanced_search", "Close advanced search")}
            className="fs-advanced-search__escape"
            onClick={onClose}
            type="button"
          >
            {t("react.shared.esc", "Esc")} </button>
        </form>

        <AdvancedSearchFilters
          canSearchAccountWide={canSearchAccountWide}
          definitions={filterDefinitions}
          dispatch={dispatchFilter}
          state={filters}
        />

        <AdvancedSearchTabs
          activeCategory={category}
          counts={counts}
          listboxId={listboxId}
          onSelect={setCategory}
          tabId={tabId}
          tabs={tabs}
        />

        <AdvancedSearchResults
          activeIndex={activeIndex}
          bestMatch={bestMatch}
          canLoadMore={canLoadMore}
          error={error}
          groupLabel={groupLabel}
          groups={visibleGroups}
          isGrouped={category === "all"}
          listboxId={listboxId}
          loadingMore={loadingMore}
          onLoadMore={loadMore}
          onOpenResult={onOpenResult}
          onRetry={retry}
          onSetActiveIndex={setActiveIndex}
          optionId={optionId}
          query={submittedQuery}
          resultsAt={resultsAt}
          selectedTabId={tabId(category)}
          view={view}
        />

        <p aria-live="polite" className="fs-advanced-search__sr-only">
          {announcement}
        </p>
      </div>
    </dialog>
  );
}
