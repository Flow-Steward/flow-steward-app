import { useI18n } from "@/lib/i18n/useI18n";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useQuery } from "@tanstack/react-query";
import { Package, RefreshCw, Search, SearchX, TriangleAlert } from "lucide-react";
import { useNavigate } from "react-router";

import {
  CatalogFacetRail,
  CatalogPager,
  CatalogResultsHead,
  CatalogStateBlock,
  CatalogSuggestLine,
} from "@/components/shared/catalog-browse";
import { DsAlert } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { useCompactCatalog } from "@/lib/react/use-compact-catalog";
import { useLatestSearchParams } from "@/lib/routing/use-latest-search-params";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { CATALOG_TABLE_PAGE_SIZES, clearFacetValues, currentTime, toggleFacetValue } from "@/lib/utils/catalog-facets";
import {
  EXTENSION_CATALOG_QUERY_KEY,
  fetchExtensionCatalog,
  type ExtensionCatalogEntry,
  type ExtensionCatalogInstallState,
} from "@/pages/extensions/catalog-api";
import { errorMessage } from "@/pages/extensions/hub-support";
import {
  InstallAnnouncements,
  MarketplaceCard,
  MarketplaceRow,
  MarketplaceTable,
  type ResultContext,
} from "@/pages/extensions/marketplace-card";
import { MarketplaceEntry } from "@/pages/extensions/marketplace-entry";
import {
  computeMarketplaceFacets,
  dateLabel,
  earliestAddedAt,
  filterMarketplace,
  MARKETPLACE_FACET_KEYS,
  MARKETPLACE_PAGE_SIZE,
  type MarketplaceFacetKey,
  type MarketplaceFilters,
  marketplaceLabels,
  marketplaceSortOptions,
  type MarketplaceUrlState,
  readMarketplaceUrl,
  resolveFind,
  SUGGEST_EXTENSION_URL,
  withoutStaleSelections,
  writeMarketplaceUrl,
} from "@/pages/extensions/marketplace-model";
import { fetchInstallationVersion } from "@/pages/workflows/catalog/api";
import "@/pages/extensions/marketplace-catalog.css";

const IDLE: ExtensionCatalogInstallState = { phase: "idle", status: "" };
const EXTENSION_NOUN = ["extension", "extensions"] as const;
const SUGGEST_PROMPT = "Don't see the extension you need?";
/** The live count waits for typing to settle before it speaks. */
const ANNOUNCE_DELAY_MS = 500;

function SuggestLine() {
  return <CatalogSuggestLine href={SUGGEST_EXTENSION_URL} prompt={SUGGEST_PROMPT} />;
}

/** The rail's shape while the index loads, after the loader's own delay. */
function SkeletonRail() {
  const visible = useLoaderDelay();
  return (
    <div aria-hidden="true" className="fs-catalog-rail">
      {visible
        ? [0, 1, 2].map((group) => (
            <div className="fs-skeleton-lines" key={group}>
              <span className="fs-skeleton fs-skeleton-line" style={{ width: "40%" }} />
              <span className="fs-skeleton fs-skeleton-line" />
              <span className="fs-skeleton fs-skeleton-line" />
              <span className="fs-skeleton fs-skeleton-line" style={{ width: "60%" }} />
            </div>
          ))
        : null}
    </div>
  );
}

/**
 * The Marketplace tab: the published extension catalog with the template
 * catalog's browse layout (filter rail, sort, grid or list, numbered pages)
 * and one extension's page in place of the results. Everything it shows is
 * in the URL as `mk_*`, so Back, reload and a shared link lose nothing.
 */
export function ExtensionMarketplaceCatalog({
  active,
  canAdminister,
  installBlockedReason,
  installStateById,
  onInstall,
}: Readonly<{
  /** Whether the Marketplace tab is the one on screen. */
  active: boolean;
  canAdminister: boolean;
  /**
   * Why this installation cannot install from the web interface at all, in
   * the page's own words, or "" when it can.
   */
  installBlockedReason: string;
  /**
   * Per-entry install state, owned by the page. It has to live there: a `412`
   * hands the flow to the browser-authority approval modal, and only the page
   * knows whether that ended in an install, a cancellation, or a failure.
   */
  installStateById: Readonly<Record<string, ExtensionCatalogInstallState>>;
  onInstall: (entry: ExtensionCatalogEntry) => void;
}>) {
  const { t, locale } = useI18n();
  const countLabel = (count: number) => count === 1
    ? t("react.extensions.catalog_extension_count_one", "{count} extension", { count })
    : t("react.extensions.catalog_extension_count_many", "{count} extensions", { count });
  const [params, updateParams] = useLatestSearchParams();
  const navigate = useNavigate();
  const urlState = useMemo(() => readMarketplaceUrl(params), [params]);
  const { filters } = urlState;
  const [now] = useState(currentTime);
  // The table pages by its own Rows per page; the cards by MARKETPLACE_PAGE_SIZE.
  const [tablePerPage, setTablePerPage] = useState<number>(CATALOG_TABLE_PAGE_SIZES[0]);
  const compact = useCompactCatalog();
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const countRef = useRef<HTMLHeadingElement | null>(null);
  // The card an extension's page was opened from, to return focus to it.
  const returnToRef = useRef("");
  // Whether the open page was pushed from these results, so "← Marketplace" is Back.
  const openedHereRef = useRef(false);

  // One request for the whole index; search, facets, sorting and paging all
  // happen in the browser.
  const catalogQuery = useQuery({
    queryKey: EXTENSION_CATALOG_QUERY_KEY,
    queryFn: fetchExtensionCatalog,
    retry: false,
  });
  const versionQuery = useQuery({
    queryKey: ["health", "build-version"],
    queryFn: fetchInstallationVersion,
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  });

  const listing = catalogQuery.data;
  const items = useMemo(() => listing?.items ?? [], [listing]);
  const labels = useMemo(() => marketplaceLabels(listing), [listing]);
  const earliest = useMemo(() => earliestAddedAt(items), [items]);
  const facets = useMemo(() => computeMarketplaceFacets(items, filters, labels, t), [items, filters, labels, t]);
  const visible = useMemo(
    () => filterMarketplace(items, filters, labels, urlState.sort, t),
    [items, filters, labels, urlState.sort, t],
  );
  const sortChoices = useMemo(() => marketplaceSortOptions(items, t), [items, t]);
  const availability = listing?.availability;
  const loaded = Boolean(listing) && availability?.available !== false;
  const entry = items.find((candidate) => candidate.extension_id === urlState.entry) ?? null;
  // A `?find=` id the catalog does not list, or an open page whose extension
  // left the catalog on a refresh: the catalog shows, saying so.
  const findId = params.get("find")?.trim() ?? "";
  let missing = "";
  if (loaded && findId && !items.some((candidate) => candidate.extension_id === findId)) missing = findId;
  else if (loaded && urlState.entry && !entry) missing = urlState.entry;

  const writeUrl = useCallback(
    (
      patch: (latest: MarketplaceUrlState) => Partial<MarketplaceUrlState>,
      options?: { push?: boolean },
    ) => {
      updateParams((current) => {
        const latest = readMarketplaceUrl(current);
        return writeMarketplaceUrl(current, { ...latest, ...patch(latest) });
      }, options);
    },
    [updateParams],
  );
  const onFilters = (update: (current: MarketplaceFilters) => MarketplaceFilters) =>
    writeUrl((latest) => ({ filters: update(latest.filters), page: 1 }));
  const toggle = (key: MarketplaceFacetKey, value: string) =>
    onFilters((current) => toggleFacetValue(current, key, value));
  const clearFacet = (key: MarketplaceFacetKey) => onFilters((current) => ({ ...current, [key]: [] }));
  const clearAll = () => onFilters((current) => clearFacetValues(current, MARKETPLACE_FACET_KEYS));

  // `?find=<id>` of a listed extension opens its page on the whole catalog.
  useEffect(() => {
    if (!active || !loaded) return;
    const found = resolveFind(params, items);
    if (found && !found.missing) updateParams(() => found.params);
  }, [active, loaded, params, items, updateParams]);

  // A selected value the loaded index no longer has would hide everything.
  useEffect(() => {
    if (!active || !loaded) return;
    const kept = withoutStaleSelections(filters, facets);
    if (kept) writeUrl(() => ({ filters: kept }));
  }, [active, loaded, filters, facets, writeUrl]);

  // Focus follows the page: its heading on the way in, the card it was
  // opened from on the way back.
  const entryId = entry?.extension_id ?? "";
  useEffect(() => {
    if (!active) return;
    if (entryId) {
      queueMicrotask(() => headingRef.current?.focus());
      return;
    }
    const back = returnToRef.current;
    returnToRef.current = "";
    if (!back) return;
    queueMicrotask(() => {
      const marker = [...document.querySelectorAll<HTMLElement>("[data-marketplace-entry]")].find(
        (element) => element.dataset.marketplaceEntry === back,
      );
      marker?.focus();
    });
  }, [active, entryId]);

  const openEntry = (extensionId: string) => {
    returnToRef.current = extensionId;
    openedHereRef.current = true;
    writeUrl(() => ({ entry: extensionId }), { push: true });
  };
  const backToResults = () => {
    if (openedHereRef.current) {
      openedHereRef.current = false;
      ignoreAsyncResult(navigate(-1));
      return;
    }
    returnToRef.current = entryId;
    writeUrl(() => ({ entry: "" }));
  };
  const showAll = () =>
    updateParams((current) => {
      const next = new URLSearchParams(current);
      next.delete("find");
      next.delete("mk_entry");
      return next;
    });
  const retry = () => ignoreAsyncResult(catalogQuery.refetch());

  const context: ResultContext = {
    labels,
    earliest,
    now,
    installationVersion: versionQuery.data ?? "",
    activeCapabilities: filters.capability,
    onOpen: openEntry,
    install: { canAdminister, installBlockedReason, stateById: installStateById, onInstall },
  };

  const retryButton = (
    <button className="fs-button sm" onClick={retry} type="button">
      <RefreshCw aria-hidden="true" />
      {t("settings.watchdog.retry", "Retry")}</button>
  );

  if (availability && !availability.configured) {
    return (
      <CatalogStateBlock
        icon={<Package aria-hidden="true" className="fs-catalog-state-icon" />}
        title={t("react.extensions.the_extension_catalog_isn_t_set_up_on_this_server", "The extension catalog isn't set up on this server.")}
      >
        {t("react.extensions.extensions_can_still_be_installed_from_a_file_with_install_extension_from_file_at_the_top_of_this_page", "Extensions can still be installed from a file with Install extension from file, at the top of this page.")}</CatalogStateBlock>
    );
  }
  if (catalogQuery.isError || availability?.available === false) {
    return (
      <CatalogStateBlock
        actions={
          <button className="fs-button" onClick={retry} type="button">
            <RefreshCw aria-hidden="true" />
            {t("settings.watchdog.retry", "Retry")}</button>
        }
        icon={<TriangleAlert aria-hidden="true" className="fs-catalog-state-icon" />}
        title={t("react.extensions.couldn_t_load_the_extension_catalog", "Couldn't load the extension catalog.")}
      >
        {availability?.message || errorMessage(catalogQuery.error, t("react.extensions.the_catalog_host_did_not_answer", "The catalog host did not answer."))} {" "}{t("react.extensions.installed_extensions_are_not_affected", "Installed extensions are not affected.")}</CatalogStateBlock>
    );
  }

  if (entry && !missing) {
    return (
      <div className="fs-marketplace">
        <MarketplaceEntry
          canAdminister={canAdminister}
          context={context}
          entry={entry}
          headingRef={headingRef}
          install={installStateById[entry.extension_id] ?? IDLE}
          installBlockedReason={installBlockedReason}
          onBack={backToResults}
          onInstall={onInstall}
        />
      </div>
    );
  }

  const loading = catalogQuery.isLoading;
  const railShown = !compact && (loading || facets.length > 0);
  let results: ReactNode;
  if (loading) {
    results = <PageLoadingState label={t("react.extensions.fetching_the_extension_catalog", "Fetching the extension catalog")} />;
  } else if (!items.length) {
    results = (
      <CatalogStateBlock
        icon={<Package aria-hidden="true" className="fs-catalog-state-icon" />}
        title={t("react.extensions.the_catalog_lists_no_extensions_yet", "The catalog lists no extensions yet.")}
      >
        <SuggestLine />
      </CatalogStateBlock>
    );
  } else if (!visible.length) {
    const hasFacets = MARKETPLACE_FACET_KEYS.some((key) => filters[key].length);
    results = (
      <>
        <CatalogStateBlock
          actions={
            <>
              {filters.q ? (
                <button className="fs-button" onClick={() => onFilters((current) => ({ ...current, q: "" }))} type="button">
                  {t("react.extensions.clear_search", "Clear search")}</button>
              ) : null}
              {hasFacets ? (
                <button className="fs-button" onClick={clearAll} type="button">
                  {t("react.extensions.clear_filters", "Clear filters")}</button>
              ) : null}
            </>
          }
          icon={<SearchX aria-hidden="true" className="fs-catalog-state-icon" />}
          title={t("react.extensions.no_extensions_match", "No extensions match.")}
        />
        <SuggestLine />
      </>
    );
  } else {
    const tableView = urlState.view === "list" && !compact;
    const perPage = tableView ? tablePerPage : MARKETPLACE_PAGE_SIZE;
    const pageCount = Math.max(1, Math.ceil(visible.length / perPage));
    const page = Math.min(urlState.page, pageCount);
    const rows = visible.slice((page - 1) * perPage, page * perPage);
    let list: ReactNode;
    if (compact) {
      list = (
        <ul aria-label={t("settings.tab.extensions", "Extensions")} className="fs-catalog-rows">
          {rows.map((row) => (
            <li key={row.extension_id}>
              <MarketplaceRow context={context} entry={row} />
            </li>
          ))}
        </ul>
      );
    } else if (tableView) {
      list = <MarketplaceTable context={context} rows={rows} />;
    } else {
      list = (
        <ul aria-label={t("settings.tab.extensions", "Extensions")} className="fs-catalog-grid">
          {rows.map((row) => (
            <li key={row.extension_id}>
              <MarketplaceCard context={context} entry={row} />
            </li>
          ))}
        </ul>
      );
    }
    results = (
      <div className="fs-catalog-results-list">
        <InstallAnnouncements entries={rows} stateById={installStateById} />
        {list}
        <CatalogPager
          label={t("react.extensions.extension_pages", "Extension pages")}
          onPage={(next) => {
            writeUrl(() => ({ page: next }));
            countRef.current?.focus();
          }}
          onPerPage={
            tableView
              ? (next) => {
                  setTablePerPage(next);
                  writeUrl(() => ({ page: 1 }));
                }
              : undefined
          }
          page={page}
          perPage={perPage}
          total={visible.length}
        />
        <SuggestLine />
      </div>
    );
  }

  const staleSince = dateLabel(availability?.fetched_at || availability?.published_at, locale);
  return (
    <div className={`fs-catalog-browse is-page fs-marketplace fs-marketplace-browse${railShown ? "" : " is-single"}`}>
      {railShown && loading ? <SkeletonRail /> : null}
      {railShown && !loading ? (
        <CatalogFacetRail
          facets={facets}
          filters={filters}
          label={t("react.extensions.extension_filters", "Extension filters")}
          noun={EXTENSION_NOUN}
          renderCountLabel={countLabel}
          onToggle={toggle}
        />
      ) : null}
      <div aria-busy={catalogQuery.isFetching && !loading} className="fs-catalog-results" id="fs-marketplace-results">
        {missing ? (
          <DsAlert title={t("react.extensions.value0_isn_t_in_the_catalog", "{value0} isn't in the catalog.", { value0: missing })}>
            <button className="fs-button sm" onClick={showAll} type="button">
              {t("react.extensions.show_all", "Show all")}</button>
          </DsAlert>
        ) : null}
        <label className="fs-search fs-marketplace-search">
          <Search aria-hidden="true" />
          <input
            aria-label={t("react.extensions.search_extension_catalog", "Search extension catalog")}
            disabled={loading}
            onChange={(event) => {
              const q = event.target.value;
              writeUrl((latest) => ({ filters: { ...latest.filters, q }, page: 1 }));
            }}
            placeholder={t("react.extensions.search_extensions_variant_0f46a3c9", "Search extensions…")}
            type="search"
            value={filters.q}
          />
        </label>
        {loading || !items.length ? null : (
          <CatalogResultsHead
            announceDelayMs={ANNOUNCE_DELAY_MS}
            compact={compact}
            countLabel={countLabel(visible.length)}
            countRef={countRef}
            facets={facets}
            filters={filters}
            notice={
              availability?.stale ? (
                <div data-catalog-stale="true">
                  <DsAlert
                    title={
                      staleSince
                        ? t("react.extensions.showing_the_catalog_as_of_value0_the_latest_refresh_failed", "Showing the catalog as of {value0}. The latest refresh failed.", { value0: staleSince })
                        : t("react.extensions.showing_the_last_catalog_we_could_load_the_latest_refresh_failed", "Showing the last catalog we could load. The latest refresh failed.")
                    }
                    tone="warning"
                  >
                    {retryButton}
                  </DsAlert>
                </div>
              ) : null
            }
            onClearAll={clearAll}
            onClearFacet={clearFacet}
            onSort={(sort) => writeUrl(() => ({ sort, page: 1 }))}
            onToggle={toggle}
            onView={(view) => writeUrl(() => ({ view, page: 1 }))}
            sort={urlState.sort}
            sortChoices={sortChoices}
            view={urlState.view}
          />
        )}
        {results}
      </div>
    </div>
  );
}
