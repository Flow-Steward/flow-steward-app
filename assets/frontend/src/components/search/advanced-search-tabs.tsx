import { useI18n } from "@/lib/i18n/useI18n";
import type { KeyboardEvent } from "react";
import { useCallback, useEffect, useRef } from "react";

import {
  formatCount,
  moveTabIndex,
  type CategoryCount,
} from "@/components/search/advanced-search-state";

export type CategoryTab = {
  value: string;
  label: string;
  accessibleName?: string;
};

type AdvancedSearchTabsProps = {
  activeCategory: string;
  counts: Record<string, CategoryCount>;
  listboxId: string;
  onSelect: (category: string) => void;
  tabId: (category: string) => string;
  tabs: CategoryTab[];
};

type OverflowEdge = "start" | "end" | "both" | null;

function overflowEdge(scroller: HTMLElement): OverflowEdge {
  const scrollable = scroller.scrollWidth - scroller.clientWidth;
  if (scrollable <= 1) {
    return null;
  }
  if (scroller.scrollLeft <= 1) {
    return "end";
  }
  if (scroller.scrollLeft >= scrollable - 1) {
    return "start";
  }
  return "both";
}

export function AdvancedSearchTabs({
  activeCategory,
  counts,
  listboxId,
  onSelect,
  tabId,
  tabs,
}: Readonly<AdvancedSearchTabsProps>) {
  const { t } = useI18n();
  const zoneRef = useRef<HTMLDivElement>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);

  // The full published configuration is wider than the canonical panel, so the row scrolls
  // and the masked edge has to follow the scroll position.
  const syncOverflow = useCallback(() => {
    const zone = zoneRef.current;
    const scroller = scrollerRef.current;
    if (!zone || !scroller) {
      return;
    }
    const edge = overflowEdge(scroller);
    if (edge) {
      zone.dataset.overflow = edge;
    } else {
      delete zone.dataset.overflow;
    }
  }, []);

  useEffect(() => {
    syncOverflow();
    globalThis.addEventListener("resize", syncOverflow);
    return () => globalThis.removeEventListener("resize", syncOverflow);
  }, [syncOverflow, tabs.length]);

  useEffect(() => {
    const scroller = scrollerRef.current;
    const selected = scroller?.querySelector<HTMLElement>('[aria-selected="true"]');
    selected?.scrollIntoView({ block: "nearest", inline: "nearest" });
    syncOverflow();
  }, [activeCategory, syncOverflow]);

  function onTabKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") {
      return;
    }
    event.preventDefault();
    const current = tabs.findIndex((tab) => tab.value === activeCategory);
    const next = moveTabIndex(current, event.key === "ArrowRight" ? 1 : -1, tabs.length);
    const target = tabs[next];
    if (!target) {
      return;
    }
    onSelect(target.value);
    scrollerRef.current
      ?.querySelector<HTMLElement>(`#${CSS.escape(tabId(target.value))}`)
      ?.focus();
  }

  return (
    <div className="fs-advanced-search__tabs-zone" ref={zoneRef}>
      <div
        aria-label={t("react.shared.search_categories", "Search categories")}
        className="fs-advanced-search__tabs"
        onScroll={syncOverflow}
        ref={scrollerRef}
        role="tablist"
      >
        {tabs.map((tab) => {
          const selected = tab.value === activeCategory;
          // A rendered tab always shows a count, including zero: it is part of the scan
          // model. A group the response omitted is an exact zero, not an absent count.
          const count = counts[tab.value] ?? { total: 0, maybeMore: false };
          return (
            <button
              aria-controls={listboxId}
              aria-label={tab.accessibleName}
              aria-selected={selected}
              className="fs-advanced-search__tab"
              id={tabId(tab.value)}
              key={tab.value}
              onClick={() => onSelect(tab.value)}
              onKeyDown={onTabKeyDown}
              role="tab"
              tabIndex={selected ? 0 : -1}
              type="button"
            >
              {tab.label}
              <span aria-hidden="true" className="fs-advanced-search__tab-count">
                {formatCount(count.total, count.maybeMore)}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
