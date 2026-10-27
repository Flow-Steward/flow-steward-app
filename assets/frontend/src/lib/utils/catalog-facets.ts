/**
 * Browser-side narrowing shared by the static catalogs (workflow templates and
 * extensions). Each catalog loads its whole index in one request, so facets,
 * search, sorting and the URL its selection lives in are computed here from
 * what was loaded, and both catalogs narrow the same way. Each catalog brings
 * only its own vocabulary: which facets it has and what an item's text is.
 */

/** Facets show this many values before "Show N more". */
export const FACET_VISIBLE_VALUES = 6;
/** A facet with at least this many values gets its own search box. */
export const FACET_SEARCH_FROM = 8;

export type CatalogFacetValue = { value: string; label: string; count: number };

export type CatalogFacetDef<Item, Key extends string = string> = {
  key: Key;
  title: string;
  /** Plural noun for "No … match" and "Filter …". */
  noun: string;
  /** "total": most common first; "fixed": the vocabulary's own order. */
  order: "total" | "fixed";
  fixed?: readonly string[];
  valuesOf: (item: Item) => readonly string[];
  label: (value: string) => string;
};

/** What a view needs of a facet: its values with counts, not how they are read. */
export type CatalogFacetView<Key extends string = string> = {
  key: Key;
  title: string;
  noun: string;
  order: "total" | "fixed";
  values: CatalogFacetValue[];
  label: (value: string) => string;
};

export type CatalogFacet<Item, Key extends string = string> = CatalogFacetDef<Item, Key> & {
  values: CatalogFacetValue[];
};

/** The search text and the values chosen in each facet. */
export type CatalogFilterState<Key extends string> = { q: string } & Record<Key, string[]>;

/** The values chosen in one facet. */
export function selectedValues<Key extends string>(filters: CatalogFilterState<Key>, key: Key): string[] {
  return (filters as unknown as Record<Key, string[]>)[key] ?? [];
}

/** The current time, read once per mount (`useState(currentTime)`), for a New badge's window. */
export function currentTime(): number {
  return Date.now();
}

const DAY_MS = 24 * 60 * 60 * 1000;
/** An item is New for its first 30 days in the catalog… */
export const CATALOG_NEW_FOR_MS = 30 * DAY_MS;
/**
 * …unless it came in the catalog's launch week, when everything would be new:
 * the MCP catalog was seeded over two days (16 servers, then 239).
 */
export const CATALOG_NEW_AFTER_FIRST_MS = 7 * DAY_MS;

/** The earliest `added_at` among `dates`, or NaN when none carries one. */
export function earliestAddedTime(dates: ReadonlyArray<string | undefined>): number {
  const times = dates.map((value) => Date.parse(value ?? "")).filter(Number.isFinite);
  return times.length ? Math.min(...times) : Number.NaN;
}

/** Added within 30 days, and after the catalog's launch week. */
export function isRecentlyAdded(addedAt: string | undefined, earliest: number, now: number): boolean {
  const added = Date.parse(addedAt ?? "");
  if (!Number.isFinite(added) || !Number.isFinite(earliest)) return false;
  return now >= added && now - added < CATALOG_NEW_FOR_MS && added - earliest > CATALOG_NEW_AFTER_FIRST_MS;
}

/** "Recently added": the newest `added_at` first; an item without one goes last. */
export function compareAddedNewestFirst(left: string | undefined, right: string | undefined): number {
  return String(right ?? "").localeCompare(String(left ?? ""));
}

export function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** Whether any of an entry's searchable texts contains the lowercased needle. */
export function catalogTextMatches(texts: readonly string[], needle: string): boolean {
  if (!needle) return true;
  return texts.join(" ").toLowerCase().includes(needle);
}

/** -1, 0 or 1 for two SemVer cores; null when either is not one. */
export function compareVersions(left: string, right: string): number | null {
  const a = /^(\d+)\.(\d+)\.(\d+)/.exec(left || "");
  const b = /^(\d+)\.(\d+)\.(\d+)/.exec(right || "");
  if (!a || !b) return null;
  for (let index = 1; index <= 3; index += 1) {
    const delta = Number(a[index]) - Number(b[index]);
    if (delta) return delta < 0 ? -1 : 1;
  }
  return 0;
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

/** OR within one facet, AND between facets; `except` leaves one facet out. */
function matchesFacets<Item, Key extends string>(
  item: Item,
  filters: CatalogFilterState<Key>,
  defs: ReadonlyArray<CatalogFacetDef<Item, Key>>,
  except?: Key,
) {
  return defs.every((def) => {
    if (def.key === except) return true;
    const selected = selectedValues(filters, def.key);
    if (!selected.length) return true;
    const values = def.valuesOf(item);
    return selected.some((value) => values.includes(value));
  });
}

function orderValues<Item, Key extends string>(def: CatalogFacetDef<Item, Key>, totals: Map<string, number>) {
  const values = [...totals.keys()];
  if (def.order === "fixed") {
    const fixed = def.fixed ?? [];
    const known = fixed.filter((value) => totals.has(value));
    const rest = values.filter((value) => !fixed.includes(value)).sort((a, b) => a.localeCompare(b));
    return [...known, ...rest];
  }
  return values.sort(
    (a, b) => (totals.get(b) ?? 0) - (totals.get(a) ?? 0) || def.label(a).localeCompare(def.label(b)),
  );
}

/**
 * Each facet with its values and live counts. The order of values comes from
 * the whole catalog, so it never moves while filtering; a count is how many
 * items the value would show with the other facets and the search applied,
 * and a zero dims in place. A facet with fewer than two values cannot narrow
 * anything and is left out, unless it is already in use.
 */
export function computeCatalogFacets<Item, Key extends string>(
  items: readonly Item[],
  filters: CatalogFilterState<Key>,
  defs: ReadonlyArray<CatalogFacetDef<Item, Key>>,
  matchesSearch: (item: Item, needle: string) => boolean,
): Array<CatalogFacet<Item, Key>> {
  const needle = filters.q.trim().toLowerCase();
  const searched = items.filter((item) => matchesSearch(item, needle));
  const facets: Array<CatalogFacet<Item, Key>> = [];
  for (const def of defs) {
    const totals = new Map<string, number>();
    for (const item of items) {
      for (const value of unique(def.valuesOf(item))) {
        totals.set(value, (totals.get(value) ?? 0) + 1);
      }
    }
    // A facet with even one value is shown, so every catalog keeps its filter
    // rail and the same layout however few entries it lists.
    if (!totals.size && !selectedValues(filters, def.key).length) continue;
    const counts = new Map<string, number>();
    for (const item of searched) {
      if (!matchesFacets(item, filters, defs, def.key)) continue;
      for (const value of unique(def.valuesOf(item))) {
        counts.set(value, (counts.get(value) ?? 0) + 1);
      }
    }
    facets.push({
      ...def,
      values: orderValues(def, totals).map((value) => ({
        value,
        label: def.label(value),
        count: counts.get(value) ?? 0,
      })),
    });
  }
  return facets;
}

/** The items the search and facets leave, in the order they came. */
export function filterCatalogItems<Item, Key extends string>(
  items: readonly Item[],
  filters: CatalogFilterState<Key>,
  defs: ReadonlyArray<CatalogFacetDef<Item, Key>>,
  matchesSearch: (item: Item, needle: string) => boolean,
): Item[] {
  const needle = filters.q.trim().toLowerCase();
  return items.filter((item) => matchesSearch(item, needle) && matchesFacets(item, filters, defs));
}

/** Sorted by `compare`, then by name; deprecated entries always come last. */
export function sortCatalogItems<Item extends { name: string; publication_status: string }>(
  items: readonly Item[],
  compare: (a: Item, b: Item) => number,
): Item[] {
  const deprecated = (item: Item) => (item.publication_status === "deprecated" ? 1 : 0);
  return [...items].sort(
    (a, b) => deprecated(a) - deprecated(b) || compare(a, b) || a.name.localeCompare(b.name),
  );
}

/** The values a facet group shows: the first six, plus any chosen beyond them. */
export function shownFacetValues(
  facet: CatalogFacetView,
  selected: readonly string[],
  query: string,
  expanded: boolean,
): { shown: CatalogFacetValue[]; hidden: number; searchable: boolean } {
  const searchable = facet.order === "total" && facet.values.length >= FACET_SEARCH_FROM;
  const needle = query.trim().toLowerCase();
  const values = needle
    ? facet.values.filter((value) => value.label.toLowerCase().includes(needle))
    : facet.values;
  if (needle || expanded || values.length <= FACET_VISIBLE_VALUES) {
    return { shown: values, hidden: 0, searchable };
  }
  const top = values.slice(0, FACET_VISIBLE_VALUES);
  const pinned = values.slice(FACET_VISIBLE_VALUES).filter((value) => selected.includes(value.value));
  const shown = [...top, ...pinned];
  return { shown, hidden: values.length - shown.length, searchable };
}

/** One value on or off in one facet. */
export function toggleFacetValue<Key extends string, Filters extends CatalogFilterState<Key>>(
  filters: Filters,
  key: Key,
  value: string,
): Filters {
  const values = selectedValues(filters, key);
  return {
    ...filters,
    [key]: values.includes(value) ? values.filter((entry) => entry !== value) : [...values, value],
  };
}

/** Every facet emptied; the search stays. */
export function clearFacetValues<Key extends string, Filters extends CatalogFilterState<Key>>(
  filters: Filters,
  keys: readonly Key[],
): Filters {
  const next = { ...filters };
  for (const key of keys) (next as Record<Key, string[]>)[key] = [];
  return next;
}

/* ---- URL ---------------------------------------------------------------- */

/** How one catalog spells its browse state in the URL: `<prefix>q`, `<prefix>sort`, … */
export type CatalogUrlSpec<Key extends string, Sort extends string> = {
  prefix: string;
  facetKeys: readonly Key[];
  sorts: readonly Sort[];
  defaultSort: Sort;
};

export type CatalogView = "grid" | "list";

export type CatalogUrlState<Key extends string, Sort extends string> = {
  filters: CatalogFilterState<Key>;
  sort: Sort;
  page: number;
  view: CatalogView;
};

function listParam(params: URLSearchParams, key: string): string[] {
  return (params.get(key) || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
}

export function readCatalogUrlState<Key extends string, Sort extends string>(
  params: URLSearchParams,
  spec: CatalogUrlSpec<Key, Sort>,
): CatalogUrlState<Key, Sort> {
  const { prefix } = spec;
  const sort = params.get(`${prefix}sort`) as Sort | null;
  const page = Number.parseInt(params.get(`${prefix}page`) || "1", 10);
  const filters = { q: params.get(`${prefix}q`) || "" } as CatalogFilterState<Key>;
  for (const key of spec.facetKeys) {
    (filters as Record<Key, string[]>)[key] = listParam(params, `${prefix}${key}`);
  }
  return {
    filters,
    sort: sort && spec.sorts.includes(sort) ? sort : spec.defaultSort,
    page: Number.isFinite(page) && page > 1 ? page : 1,
    view: params.get(`${prefix}view`) === "list" ? "list" : "grid",
  };
}

/** Writes the browse state into a copy of `params`, leaving other keys alone. */
export function writeCatalogUrlState<Key extends string, Sort extends string>(
  params: URLSearchParams,
  state: CatalogUrlState<Key, Sort>,
  spec: CatalogUrlSpec<Key, Sort>,
): URLSearchParams {
  const { prefix } = spec;
  const next = new URLSearchParams(params);
  const set = (key: string, value: string) => {
    if (value) next.set(`${prefix}${key}`, value);
    else next.delete(`${prefix}${key}`);
  };
  set("q", state.filters.q);
  for (const key of spec.facetKeys) set(key, selectedValues(state.filters, key).join(","));
  set("sort", state.sort === spec.defaultSort ? "" : state.sort);
  set("page", state.page > 1 ? String(state.page) : "");
  set("view", state.view === "grid" ? "" : state.view);
  return next;
}

/* ---- What the browse parts draw (components/shared/catalog-browse) ---- */

/** Rows per page the table view offers. */
export const CATALOG_TABLE_PAGE_SIZES = [25, 50] as const;

/** What a catalog lists: MCP servers, extensions, workflow templates. */
export type CatalogKind = "mcp" | "extension" | "workflow";

/** "Ah", "AC": the tile of an MCP server without a logo. */
export function catalogInitials(name: string): string {
  const word = name.trim().split(/\s+/)[0]?.replaceAll(/[^\p{L}\p{N}]/gu, "") ?? "";
  if (!word) return "?";
  const inner = Array.from(word.slice(1)).find((char) => char !== char.toLowerCase());
  return inner ? `${word.charAt(0).toUpperCase()}${inner}` : `${word.charAt(0).toUpperCase()}${word.charAt(1).toLowerCase()}`;
}

/*
 * Chips are whole pills only: as many as fit the row, the rest one "+N" pill.
 * A pill is its text, the badge's 7px insets and a 1px border each side; the
 * text is measured on a canvas in the badge's face, or estimated where there
 * is no canvas (tests). Without a measured row, the card's 220px is assumed.
 */
const CHIP_FALLBACK_WIDTH = 220;
const CHIP_GAP = 6;
const CHIP_CHROME = 16;
const CHIP_FONT = '500 11px "Google Sans Flex", system-ui, sans-serif';
let chipCanvas: CanvasRenderingContext2D | null | undefined;

function chipTextWidth(text: string): number {
  if (chipCanvas === undefined) {
    const jsdom = typeof navigator !== "undefined" && /jsdom/i.test(navigator.userAgent);
    chipCanvas = jsdom || typeof document === "undefined" ? null : document.createElement("canvas").getContext("2d");
  }
  if (!chipCanvas) return Math.ceil(text.length * 6.2);
  chipCanvas.font = CHIP_FONT;
  return Math.ceil(chipCanvas.measureText(text).width);
}

/** How many of `labels` fit `width` as whole pills, leaving room for "+N". */
export function fittedChipCount(labels: readonly string[], width: number): number {
  const budget = width > 0 ? width : CHIP_FALLBACK_WIDTH;
  let used = 0;
  for (let index = 0; index < labels.length; index += 1) {
    const pill = chipTextWidth(labels[index]) + CHIP_CHROME + (index ? CHIP_GAP : 0);
    const rest = labels.length - index - 1;
    const reserve = rest > 0 ? CHIP_GAP + chipTextWidth(`+${rest}`) + CHIP_CHROME : 0;
    if (used + pill + reserve > budget) return index;
    used += pill;
  }
  return labels.length;
}
