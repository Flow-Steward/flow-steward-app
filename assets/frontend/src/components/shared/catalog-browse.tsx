import { useI18n } from "@/lib/i18n/useI18n";
/*
 * The browse parts both static catalogs draw the same way: the facet rail,
 * the applied-filter chips, the results header (count, phone Add filter, sort,
 * cards or table), the card itself, the chip row with +N, the pager, the suggest line
 * and the state block, and the logo tile an extension or MCP server shows.
 * Lifted from the workflow template catalog; each catalog brings its own cards,
 * copy and vocabulary through props.
 */
import {
  Fragment,
  type MouseEvent,
  type ReactNode,
  type RefObject,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { ArrowLeft, BadgeCheck, Check, ExternalLink, LayoutGrid, List, Package, Search, TriangleAlert, Workflow, X } from "lucide-react";

import {
  AdvancedTableFilters,
  DsBadge,
  type DsBadgeTone,
  DsCheckbox,
  DsSelect,
  Pagination,
} from "@/components/shared/design-system";
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  CATALOG_TABLE_PAGE_SIZES,
  type CatalogFacetView,
  type CatalogFilterState,
  type CatalogKind,
  type CatalogView,
  catalogInitials,
  fittedChipCount,
  plural,
  selectedValues,
  shownFacetValues,
} from "@/lib/utils/catalog-facets";

import "@/components/shared/catalog-browse.css";

/** Tile side in px: inline (beside a name), row (a table row), card, page head. */
const CATALOG_ICON_PX = { inline: 20, row: 32, card: 40, page: 48 } as const;

export type CatalogIconSize = keyof typeof CATALOG_ICON_PX;

/**
 * The logo tile of an MCP server, an extension or a template. `src` is a URL
 * Flow Steward serves the logo from (it verified the file first); without
 * one, or when it fails to load, the tile shows `fallback` — by default the
 * kind's own placeholder, whoever publishes the item: a server's initials, the
 * Lucide package, a flow. The Flow Mark is never a placeholder. A logo sits on
 * a light surface in the dark theme (`data-catalog-icon="image"`), so a dark
 * drawing on a transparent PNG does not vanish; the placeholder keeps the
 * theme's muted surface.
 */
export function CatalogIcon({
  src = "",
  size = "card",
  fallback,
  kind = "extension",
  name = "",
}: Readonly<{
  src?: string;
  size?: CatalogIconSize;
  fallback?: ReactNode;
  kind?: CatalogKind;
  name?: string;
}>) {
  const [failedSrc, setFailedSrc] = useState("");
  const showImage = Boolean(src) && failedSrc !== src;
  const px = CATALOG_ICON_PX[size];
  let placeholder: ReactNode = fallback;
  if (placeholder === undefined) {
    if (kind === "mcp") placeholder = catalogInitials(name);
    else if (kind === "workflow") placeholder = <Workflow aria-hidden="true" />;
    else placeholder = <Package aria-hidden="true" />;
  }
  return (
    <span className={`fs-catalog-icon fs-catalog-icon-${size}`} data-catalog-icon={showImage ? "image" : "fallback"} data-testid="catalog-icon">
      {showImage ? (
        <img alt="" decoding="async" height={px} loading="lazy" onError={() => setFailedSrc(src)} src={src} width={px} />
      ) : (
        <span aria-hidden="true" className="fs-catalog-icon-fallback">
          {placeholder}
        </span>
      )}
    </span>
  );
}

/** "template"/"templates", "extension"/"extensions": what a count counts. */
export type CatalogNoun = readonly [one: string, many: string];

/** The +N tooltip opens after this long under the pointer, at once on focus. */
const CHIP_TIP_DELAY_MS = 500;

function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    const measure = () => setWidth(element.clientWidth);
    measure();
    // Canvas measures in the badge face only once the web font has loaded.
    const fonts = document.fonts as FontFaceSet | undefined;
    if (fonts) ignoreAsyncResult(fonts.ready.then(measure));
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** What bounds the tooltip: the card, the phone row, or the table cell the "+N" sits in. */
const CHIP_TIP_BOUNDS = ".fs-catalog-card, .fs-catalog-row, td, th";

/**
 * What "+N" stands for, above it (below, when there is no room above): at most
 * the tooltip token's width, never wider than the card or cell it sits in and
 * never beyond its edges. Placed by CSSOM before paint, so a strict style-src
 * allows it.
 */
function ChipTip({ id, anchor, children }: Readonly<{ id: string; anchor: RefObject<HTMLElement | null>; children: ReactNode }>) {
  const tip = useRef<HTMLSpanElement | null>(null);
  useLayoutEffect(() => {
    const element = tip.current;
    const wrap = anchor.current;
    if (!element || !wrap) return;
    const bounds = (wrap.closest(CHIP_TIP_BOUNDS) ?? wrap.parentElement ?? wrap).getBoundingClientRect();
    const own = getComputedStyle(element);
    const offset = Number.parseFloat(own.getPropertyValue("--fs-tooltip-offset")) || 8;
    const widest = Number.parseFloat(own.getPropertyValue("--fs-tooltip-max-width")) || 240;
    element.style.maxWidth = `${bounds.width > 0 ? Math.min(widest, bounds.width) : widest}px`;
    const size = element.getBoundingClientRect();
    const anchorBox = wrap.getBoundingClientRect();
    const centred = anchorBox.left + anchorBox.width / 2 - size.width / 2;
    const left = bounds.width > 0 ? Math.min(Math.max(centred, bounds.left), bounds.right - size.width) : centred;
    element.style.left = `${left - anchorBox.left}px`;
    if (anchorBox.top - offset - size.height < 0) {
      element.style.bottom = "auto";
      element.style.top = `calc(100% + ${offset}px)`;
    }
  }, [anchor, children]);
  return (
    <span className="fs-catalog-chip-tip" id={id} ref={tip} role="tooltip">
      {children}
    </span>
  );
}

/**
 * "+N": a pill like the others, a real button. Hovering it lists the hidden
 * ones after 500ms; keyboard focus lists them at once; leaving, blur and Escape
 * close the list; a click does nothing.
 */
function ChipMore({ hidden }: Readonly<{ hidden: readonly string[] }>) {
  const { t } = useI18n();
  const tipId = useId();
  const wrap = useRef<HTMLSpanElement | null>(null);
  const [open, setOpen] = useState(false);
  // A press focuses the button too, but that is not keyboard focus.
  const pressed = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const close = () => {
    clearTimeout(timer.current);
    setOpen(false);
  };
  const list = hidden.join(", ");
  return (
    <span className="fs-catalog-chip-more-wrap" ref={wrap}>
      <button
        aria-describedby={open ? tipId : undefined}
        aria-label={t("react.shared.catalog_more_chips", "{count} more: {list}", { count: hidden.length, list })}
        className="fs-badge fs-catalog-chip-more"
        onBlur={() => {
          pressed.current = false;
          close();
        }}
        onClick={(event) => event.stopPropagation()}
        onFocus={() => {
          if (pressed.current) return;
          clearTimeout(timer.current);
          setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key !== "Escape" || !open) return;
          // The list closes first; a dialog around the catalog stays open.
          event.stopPropagation();
          close();
        }}
        onMouseDown={() => {
          pressed.current = true;
        }}
        onMouseEnter={() => {
          clearTimeout(timer.current);
          timer.current = setTimeout(() => setOpen(true), CHIP_TIP_DELAY_MS);
        }}
        onMouseLeave={close}
        type="button"
      >
        +{hidden.length}
      </button>
      {open ? (
        <ChipTip anchor={wrap} id={tipId}>
          {list}
        </ChipTip>
      ) : null}
    </span>
  );
}

/** Capability or integration pills: whole ones only, then "+N" with the rest. */
export function CatalogChipRow({ labels, wrap = false }: Readonly<{ labels: string[]; wrap?: boolean }>) {
  const [ref, width] = useElementWidth<HTMLSpanElement>();
  const count = wrap ? labels.length : fittedChipCount(labels, width);
  const shown = labels.slice(0, count);
  const hidden = labels.slice(count);
  return (
    <span className={`fs-catalog-chips${wrap ? " wrap" : ""}`} ref={ref}>
      {shown.map((label) => (
        <span className="fs-catalog-chip" key={label}>
          <DsBadge>{label}</DsBadge>
        </span>
      ))}
      {hidden.length ? <ChipMore hidden={hidden} /> : null}
    </span>
  );
}

/** A state on a catalog item: Preview, Deprecated, Connected, Installed, New… */
export type CatalogPill = { tone: DsBadgeTone; label: string; icon?: "check" | "alert" };

export function CatalogPills({ pills }: Readonly<{ pills: readonly CatalogPill[] }>) {
  if (!pills.length) return null;
  return (
    <span className="fs-catalog-pills">
      {pills.map((pill) => (
        <DsBadge key={pill.label} tone={pill.tone}>
          {pill.icon === "check" ? <Check aria-hidden="true" /> : null}
          {pill.icon === "alert" ? <TriangleAlert aria-hidden="true" /> : null}
          {pill.label}
        </DsBadge>
      ))}
    </span>
  );
}

/**
 * Who stands behind an item. Official: the vendor publishes it (MCP).
 * Verified: the catalog reserves the publisher's namespace (extensions).
 * Community: an MCP server its vendor did not publish. None: no claim.
 */
export type CatalogTrust = "official" | "verified" | "community" | "none";

/**
 * The line under a name: "by Ahrefs · ✓ Official", "Community",
 * "by Flow-Steward [Verified]", or a template's trigger, then the version and
 * state pills on a page. A pill carries its own edge, so no dot goes before it.
 */
export function CatalogByline({
  publisher = "",
  trust = "none",
  trigger,
  version = "",
  pills = [],
}: Readonly<{
  publisher?: string;
  trust?: CatalogTrust;
  trigger?: ReactNode;
  /** "v1.0.0", on a page; a card shows it beside the name. */
  version?: string;
  pills?: readonly CatalogPill[];
}>) {
  const { t } = useI18n();
  const parts: Array<{ key: string; node: ReactNode; pill?: boolean }> = [];
  if (trigger) parts.push({ key: "trigger", node: trigger });
  if (publisher) {
    parts.push({ key: "publisher", node: <span className="fs-catalog-byline-text">{t("react.extensions.by_publisher", "by {value0}", { value0: publisher })}</span> });
  }
  if (trust === "community") {
    parts.push({ key: "community", node: <span className="fs-catalog-byline-text">{t("react.shared.catalog_trust_community", "Community")}</span> });
  }
  if (trust === "official") {
    parts.push({
      key: "official",
      node: (
        <span className="fs-catalog-official">
          <BadgeCheck aria-hidden="true" />
          {t("react.registry.mcp_catalog.official", "Official")}
        </span>
      ),
    });
  }
  if (trust === "verified") {
    parts.push({ key: "verified", pill: true, node: <DsBadge tone="green">{t("react.shared.catalog_trust_verified", "Verified")}</DsBadge> });
  }
  if (version) parts.push({ key: "version", node: <span className="fs-catalog-byline-version">{version}</span> });
  if (pills.length) parts.push({ key: "pills", pill: true, node: <CatalogPills pills={pills} /> });
  if (!parts.length) return null;
  return (
    <span className="fs-catalog-byline">
      {parts.map((part, index) => (
        <Fragment key={part.key}>
          {index > 0 && !part.pill ? <span aria-hidden="true">·</span> : null}
          {part.node}
        </Fragment>
      ))}
    </span>
  );
}

/** The name of a catalog item: a link to its page, or a button that opens it. */
export type CatalogOpen = Readonly<{
  /** A real link: a plain click calls `onOpen`, a new-tab click keeps the browser's. */
  href?: string;
  onOpen: () => void;
  /** Read after the name: the summary, category, state, one sentence each. */
  description?: string;
  /** data-* hooks on the control, e.g. what focus returns to. */
  data?: Readonly<Record<string, string>>;
}>;

function dataAttributes(data: Readonly<Record<string, string>> | undefined) {
  return Object.fromEntries(Object.entries(data ?? {}).map(([key, value]) => [`data-${key}`, value]));
}

function openOnPlainClick(event: MouseEvent<HTMLAnchorElement>, onOpen: () => void) {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  onOpen();
}

/** The name as the item's one control; its ::after stretches over the card. */
export function CatalogOpenControl({
  open,
  id,
  labelledBy,
  className,
  children,
}: Readonly<{
  open: CatalogOpen;
  id: string;
  /** The name alone, when the control also shows the version. */
  labelledBy?: string;
  className: string;
  children: ReactNode;
}>) {
  const descriptionId = `${id}-description`;
  const shared = {
    "aria-describedby": open.description ? descriptionId : undefined,
    "aria-labelledby": labelledBy,
    className,
    id,
    ...dataAttributes(open.data),
  };
  return (
    <>
      {open.href ? (
        <a {...shared} href={open.href} onClick={(event) => openOnPlainClick(event, open.onOpen)}>
          {children}
        </a>
      ) : (
        <button {...shared} onClick={open.onOpen} type="button">
          {children}
        </button>
      )}
      {open.description ? (
        <span className="fs-sr-only" id={descriptionId}>
          {open.description}
        </span>
      ) : null}
    </>
  );
}

/**
 * One catalog card, the same for MCP servers, extensions and workflow
 * templates: logo tile, name (two lines) with its version, the byline,
 * two lines of summary, chips with the state pills on the right, then the
 * category and the action (or the state that takes its place). The name is
 * the card's one link or button; the action sits above it, never inside.
 * Without the right to act, `action` is absent and the category stays alone.
 */
export function CatalogItemCard({
  id,
  icon,
  title,
  version = "",
  byline,
  summary,
  chips,
  statuses = [],
  category,
  action,
  state,
  notes,
  open,
  data,
}: Readonly<{
  /** Unique on the page: the name's id, and its description's. */
  id: string;
  icon: ReactNode;
  title: string;
  version?: string;
  byline?: ReactNode;
  summary: string;
  chips: string[];
  statuses?: readonly CatalogPill[];
  category: string;
  action?: ReactNode;
  state?: CatalogPill | null;
  /** A line under the chips: why it is incompatible, how an install ended. */
  notes?: ReactNode;
  open: CatalogOpen;
  data?: Readonly<Record<string, string>>;
}>) {
  const titleId = `${id}-title`;
  return (
    <article aria-labelledby={titleId} className="fs-card fs-catalog-card" {...dataAttributes(data)}>
      <div className="fs-catalog-card-head">
        {icon}
        <div className="fs-catalog-card-heading">
          <CatalogOpenControl className="fs-catalog-card-open" id={`${id}-open`} labelledBy={titleId} open={open}>
            <span className="fs-catalog-card-title" id={titleId}>
              {title}
            </span>
            {version ? <span className="fs-catalog-card-version">{version}</span> : null}
          </CatalogOpenControl>
          {byline}
        </div>
      </div>
      <p className="fs-catalog-card-summary">{summary}</p>
      <div className="fs-catalog-card-tags">
        <CatalogChipRow labels={chips} />
        <CatalogPills pills={statuses} />
      </div>
      {notes}
      <div className="fs-catalog-card-foot">
        <span className="fs-catalog-card-category">{category}</span>
        {state ? <CatalogPills pills={[state]} /> : action}
      </div>
    </article>
  );
}

function FacetGroup<Key extends string>({
  facet,
  selected,
  noun,
  onToggle,
  renderCountLabel,
}: Readonly<{
  facet: CatalogFacetView<Key>;
  selected: readonly string[];
  noun: CatalogNoun;
  onToggle: (key: Key, value: string) => void;
  renderCountLabel?: (count: number) => string;
}>) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const [query, setQuery] = useState("");
  const { shown, hidden, searchable } = shownFacetValues(facet, selected, query, expanded);
  const titleId = `fs-catalog-facet-${facet.key}`;
  const long = !query.trim() && facet.order === "total" && (hidden > 0 || expanded);
  return (
    <section aria-labelledby={titleId} className="fs-catalog-facet" role="group">
      <h3 className="fs-field-label fs-catalog-facet-title" id={titleId}>
        {facet.title}
        {selected.length ? <span className="fs-catalog-count">{selected.length}</span> : null}
      </h3>
      {searchable ? (
        <label className="fs-search fs-catalog-facet-search">
          <Search aria-hidden="true" />
          <input
            aria-label={t("react.shared.filter_value1", "Filter {value1}", { value1: String(facet.noun) })}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("react.shared.filter_value1_2", "Filter {value1}…", { value1: String(facet.noun) })}
            type="search"
            value={query}
          />
        </label>
      ) : null}
      {query.trim() && !shown.length ? (
        <p className="fs-field-helper">
          {t("react.shared.catalog_no_matching_facets", "No {noun} match “{query}”.", { noun: facet.noun, query })}
        </p>
      ) : null}
      {shown.map((value) => {
        const isSelected = selected.includes(value.value);
        const zero = value.count === 0 && !isSelected;
        return (
          <div className={`fs-catalog-facet-value${zero ? " is-zero" : ""}`} key={value.value}>
            <DsCheckbox checked={isSelected} disabled={zero} onChange={() => onToggle(facet.key, value.value)}>
              {value.label}
              <span className="fs-sr-only">, {renderCountLabel ? renderCountLabel(value.count) : plural(value.count, noun[0], noun[1])}</span>
            </DsCheckbox>
            <span aria-hidden="true" className="fs-catalog-count">
              {value.count}
            </span>
          </div>
        );
      })}
      {long ? (
        <button className="fs-clear-filters fs-catalog-more" onClick={() => setExpanded(!expanded)} type="button">
          {expanded ? t("react.shared.show_fewer", "Show fewer") : t("react.shared.show_value1_more", "Show {value1} more", { value1: String(hidden) })}
        </button>
      ) : null}
    </section>
  );
}

/** The filter rail: one group per facet, OR inside a group, AND between groups. */
export function CatalogFacetRail<Key extends string>({
  label,
  facets,
  filters,
  noun,
  onToggle,
  renderCountLabel,
}: Readonly<{
  label: string;
  facets: ReadonlyArray<CatalogFacetView<Key>>;
  filters: CatalogFilterState<Key>;
  noun: CatalogNoun;
  onToggle: (key: Key, value: string) => void;
  renderCountLabel?: (count: number) => string;
}>) {
  return (
    <nav aria-label={label} className="fs-catalog-rail">
      {facets.map((facet) => (
        <FacetGroup
          facet={facet}
          key={facet.key}
          noun={noun}
          onToggle={onToggle}
          renderCountLabel={renderCountLabel}
          selected={selectedValues(filters, facet.key)}
        />
      ))}
    </nav>
  );
}

function appliedText(facet: CatalogFacetView, values: readonly string[]) {
  const labels = values.map((value) => facet.values.find((entry) => entry.value === value)?.label ?? facet.label(value));
  return labels.length > 2 ? `${labels.slice(0, 2).join(", ")} +${labels.length - 2}` : labels.join(", ");
}

/** The applied-filter chips, the same anatomy the Advanced data table filters use. */
export function CatalogAppliedFilters<Key extends string>({
  facets,
  filters,
  onClearFacet,
  onClearAll,
}: Readonly<{
  facets: ReadonlyArray<CatalogFacetView<Key>>;
  filters: CatalogFilterState<Key>;
  onClearFacet: (key: Key) => void;
  onClearAll: () => void;
}>) {
  const { t } = useI18n();
  const active = facets.filter((facet) => selectedValues(filters, facet.key).length);
  if (!active.length) return null;
  return (
    <div aria-label={t("react.shared.applied_filters", "Applied filters")} className="fs-catalog-applied" role="group">
      <div className="fs-active-filters">
        {active.map((facet) => (
          <span className="fs-applied-filter" key={facet.key}>
            <span>
              {facet.title} · {appliedText(facet, selectedValues(filters, facet.key))}
            </span>
            <button aria-label={t("react.shared.remove_value1_filter", "Remove {value1} filter", { value1: String(facet.title) })} onClick={() => onClearFacet(facet.key)} type="button">
              <X aria-hidden="true" />
            </button>
          </span>
        ))}
      </div>
      <button className="fs-clear-filters" onClick={onClearAll} type="button">
        {t("react.shared.clear_all", "Clear all")} </button>
    </div>
  );
}

/** The live count, said `delayMs` after the last change rather than on every keystroke. */
function CountAnnouncement({ text, delayMs }: Readonly<{ text: string; delayMs: number }>) {
  const [said, setSaid] = useState(delayMs ? "" : text);
  useEffect(() => {
    if (!delayMs) return undefined;
    const timer = globalThis.setTimeout(() => setSaid(text), delayMs);
    return () => globalThis.clearTimeout(timer);
  }, [text, delayMs]);
  return (
    <span aria-live="polite" className="fs-sr-only">
      {delayMs ? said : text}
    </span>
  );
}

export type CatalogResultsHeadProps<Key extends string, Sort extends string> = Readonly<{
  /** "22 extensions": the heading the pager returns focus to. */
  countLabel: string;
  /** The live region repeats the count this long after the last change; 0 says it at once. */
  announceDelayMs?: number;
  facets: ReadonlyArray<CatalogFacetView<Key>>;
  filters: CatalogFilterState<Key>;
  sort: Sort;
  sortChoices: ReadonlyArray<{ value: Sort; label: string }>;
  view: CatalogView;
  compact: boolean;
  countRef: RefObject<HTMLHeadingElement | null>;
  /** Shown under the header, above the applied filters: a stale-catalog warning. */
  notice?: ReactNode;
  onSort: (sort: Sort) => void;
  onView: (view: CatalogView) => void;
  onToggle: (key: Key, value: string) => void;
  onClearFacet: (key: Key) => void;
  onClearAll: () => void;
}>;

/** Count, the phone's Add filter, Sort, the grid/list toggle, then the applied filters. */
export function CatalogResultsHead<Key extends string, Sort extends string>({
  countLabel,
  announceDelayMs = 0,
  facets,
  filters,
  sort,
  sortChoices,
  view,
  compact,
  countRef,
  notice,
  onSort,
  onView,
  onToggle,
  onClearFacet,
  onClearAll,
}: CatalogResultsHeadProps<Key, Sort>) {
  const { t } = useI18n();
  // The phone's one "Add filter" control: a field, then one of its values. A
  // value already applied, or one that would show nothing, is not offered.
  const filterFields = facets.map((facet) => ({
    key: facet.key,
    label: facet.title,
    options: facet.values
      .filter((value) => value.count > 0 && !selectedValues(filters, facet.key).includes(value.value))
      .map((value) => ({ value: value.value, label: `${value.label} (${value.count})` })),
  }));
  return (
    <>
      <div className="fs-catalog-results-head">
        <h3 className="fs-catalog-result-count" ref={countRef} tabIndex={-1}>
          {countLabel}
        </h3>
        {compact && facets.length ? (
          <AdvancedTableFilters
            fields={filterFields}
            onChange={(key, value) => {
              if (value) onToggle(key as Key, value);
            }}
            onClear={onClearAll}
            values={{}}
          />
        ) : null}
        {sortChoices.length ? (
          <span className="fs-catalog-sort">
            {compact ? null : <span aria-hidden="true">{t("react.shared.sort", "Sort")}</span>}
            <DsSelect
              ariaLabel={t("react.shared.sort", "Sort")}
              onChange={(value) => onSort(value as Sort)}
              options={[...sortChoices]}
              value={sort}
            />
          </span>
        ) : null}
        {compact ? null : (
          <span aria-label={t("react.shared.catalog_view", "View")} className="fs-catalog-view" role="group">
            <button
              aria-label={t("react.shared.catalog_view_cards", "Cards")}
              aria-pressed={view === "grid"}
              className="fs-icon-button"
              onClick={() => onView("grid")}
              type="button"
            >
              <LayoutGrid aria-hidden="true" />
            </button>
            <button
              aria-label={t("react.shared.catalog_view_table", "Table")}
              aria-pressed={view === "list"}
              className="fs-icon-button"
              onClick={() => onView("list")}
              type="button"
            >
              <List aria-hidden="true" />
            </button>
          </span>
        )}
      </div>
      <CountAnnouncement delayMs={announceDelayMs} text={countLabel} />
      {notice}
      <CatalogAppliedFilters facets={facets} filters={filters} onClearAll={onClearAll} onClearFacet={onClearFacet} />
    </>
  );
}

/** "Don't see the … you need? Suggest one on GitHub ↗", opening in a new tab. */
export function CatalogSuggestLine({ prompt, href }: Readonly<{ prompt: string; href: string }>) {
  const { t } = useI18n();
  return (
    <p className="fs-catalog-suggest">
      {prompt}{" "}
      <a href={href} rel="noreferrer" target="_blank">
        {t("react.shared.suggest_one_on_github", "Suggest one on GitHub")} <ExternalLink aria-hidden="true" />
        <span className="fs-sr-only"> {t("react.shared.opens_in_a_new_tab", "(opens in a new tab)")}</span>
      </a>
    </p>
  );
}

/** What the results area shows instead of results: loading, empty, unavailable. */
export function CatalogStateBlock({
  icon,
  title,
  children,
  actions,
}: Readonly<{ icon: ReactNode; title: string; children?: ReactNode; actions?: ReactNode }>) {
  return (
    <div className="fs-table-state fs-catalog-state">
      {icon}
      <strong>{title}</strong>
      {children ? <span>{children}</span> : null}
      {actions ? <div className="fs-catalog-state-actions">{actions}</div> : null}
    </div>
  );
}

/** The table view's first, frozen cell: tile, the name as the row's link, byline, one line of summary. */
export function CatalogTableItem({
  id,
  icon,
  title,
  version = "",
  byline,
  summary,
  open,
}: Readonly<{
  id: string;
  icon: ReactNode;
  title: string;
  version?: string;
  byline?: ReactNode;
  summary: string;
  open: CatalogOpen;
}>) {
  return (
    <span className="fs-catalog-list-item">
      {icon}
      <span className="fs-catalog-list-text">
        <span className="fs-catalog-list-name">
          <CatalogOpenControl className="fs-catalog-list-open" id={id} open={open}>
            {title}
          </CatalogOpenControl>
          {version ? <span className="fs-catalog-card-version">{version}</span> : null}
        </span>
        {byline}
        <span className="fs-catalog-list-summary">{summary}</span>
      </span>
    </span>
  );
}

/**
 * An item's page head, the same for a server, an extension and a template:
 * "← Catalog", then the 48px tile, the name, the byline, the summary, and the
 * one main action on the right. Without the right to act there is no action.
 * A surface that draws its own way back (the template dialog) omits `onBack`.
 */
export function CatalogPageHead({
  backLabel,
  onBack,
  backDisabled = false,
  icon,
  title,
  headingRef,
  byline,
  summary,
  action,
  headingLevel = 2,
}: Readonly<{
  backLabel?: string;
  onBack?: () => void;
  backDisabled?: boolean;
  icon: ReactNode;
  title: string;
  headingRef: RefObject<HTMLHeadingElement | null>;
  byline?: ReactNode;
  summary: string;
  action?: ReactNode;
  /** 2 in a page tab, under the page's h1; 3 inside a dialog, under its title. */
  headingLevel?: 2 | 3;
}>) {
  const Heading = headingLevel === 3 ? "h3" : "h2";
  return (
    <>
      {onBack ? (
        <button className="fs-clear-filters fs-catalog-back" disabled={backDisabled} onClick={onBack} type="button">
          <ArrowLeft aria-hidden="true" />
          {backLabel}
        </button>
      ) : null}
      <header className="fs-catalog-page-head">
        {icon}
        <div className="fs-catalog-page-copy">
          <Heading className="fs-catalog-page-title fs-catalog-stage-heading" ref={headingRef} tabIndex={-1}>
            {title}
          </Heading>
          {byline}
          <p className="fs-catalog-page-summary">{summary}</p>
        </div>
        {action ? <div className="fs-catalog-page-action">{action}</div> : null}
      </header>
    </>
  );
}

/** One fact in a page's aside: a small tertiary term over its value. */
export function CatalogFact({ term, children }: Readonly<{ term: string; children: ReactNode }>) {
  return (
    <div className="fs-kv-row">
      <dt>{term}</dt>
      <dd className="is-plain">{children}</dd>
    </div>
  );
}

/**
 * "1–24 of 255" and the arrows, right-aligned, drawn only when there is more
 * than one page. The table view adds "Rows per page", and then stays drawn
 * while a smaller size would still page.
 */
export function CatalogPager({
  label,
  page,
  perPage,
  total,
  onPage,
  onPerPage,
}: Readonly<{
  label: string;
  page: number;
  perPage: number;
  total: number;
  onPage: (page: number) => void;
  onPerPage?: (perPage: number) => void;
}>) {
  const smallest = onPerPage ? Math.min(perPage, ...CATALOG_TABLE_PAGE_SIZES) : perPage;
  if (total <= smallest) return null;
  return (
    <nav aria-label={label} className="fs-catalog-pager">
      <Pagination
        onPageChange={onPage}
        onPerPageChange={onPerPage}
        page={page}
        perPage={perPage}
        perPageOptions={CATALOG_TABLE_PAGE_SIZES}
        showPerPage={Boolean(onPerPage)}
        total={total}
      />
    </nav>
  );
}
