/**
 * Pure state for the Advanced Search pattern.
 *
 * Everything here is deliberately free of React and of transport: the filter algebra,
 * the keyboard resolution order and the count formatting are the parts of the canonical
 * contract that must be provable without rendering a dialog.
 */

export const FILTER_KEYS = ["workspace", "project", "status", "updated"] as const;

export type FilterKey = (typeof FILTER_KEYS)[number];

export type FilterOption = {
  value: string;
  label: string;
};

export type FilterDefinition = {
  key: FilterKey;
  label: string;
  options: FilterOption[];
};

/** An empty value means the key was just added and does not constrain results yet. */
export type ActiveFilter = {
  k: FilterKey;
  v: string;
};

export type MenuState =
  | { kind: "value"; index: number }
  | { kind: "add" }
  | null;

export type FilterState = {
  active: ActiveFilter[];
  menu: MenuState;
};

export type FilterAction =
  | { type: "open-add-menu" }
  | { type: "open-value-menu"; index: number }
  | { type: "close-menu" }
  | { type: "add-filter"; key: FilterKey }
  | { type: "set-value"; index: number; value: string }
  | { type: "remove-filter"; index: number }
  | { type: "clear-filters" };

export function isFilterKey(value: unknown): value is FilterKey {
  return FILTER_KEYS.includes(value as FilterKey);
}

/** A key may appear only once, so the Add filter menu offers the remainder. */
export function availableFilterKeys(
  definitions: FilterDefinition[],
  active: ActiveFilter[],
): FilterKey[] {
  return definitions
    .map((definition) => definition.key)
    .filter((key) => !active.some((filter) => filter.k === key));
}

export function filterDefinition(
  definitions: FilterDefinition[],
  key: FilterKey,
): FilterDefinition | undefined {
  return definitions.find((definition) => definition.key === key);
}

export function filterValueLabel(
  definitions: FilterDefinition[],
  filter: ActiveFilter,
): string | null {
  const definition = filterDefinition(definitions, filter.k);
  const option = definition?.options.find((candidate) => candidate.value === filter.v);
  return option ? option.label : null;
}

/**
 * A workspace scope is removable only when account-wide search is permitted; otherwise it
 * is the request's non-removable context rather than a refinement the user chose.
 */
export function isFilterRemovable(
  filter: ActiveFilter,
  options: { canSearchAccountWide: boolean },
): boolean {
  if (filter.k !== "workspace") {
    return true;
  }
  return options.canSearchAccountWide;
}

export function filterReducer(state: FilterState, action: FilterAction): FilterState {
  switch (action.type) {
    case "open-add-menu": {
      // Clicking an already-open trigger toggles it closed.
      const open = state.menu?.kind === "add";
      return { ...state, menu: open ? null : { kind: "add" } };
    }
    case "open-value-menu": {
      const open = state.menu?.kind === "value" && state.menu.index === action.index;
      return { ...state, menu: open ? null : { kind: "value", index: action.index } };
    }
    case "close-menu":
      return { ...state, menu: null };
    case "add-filter": {
      if (state.active.some((filter) => filter.k === action.key)) {
        return state;
      }
      const active = [...state.active, { k: action.key, v: "" }];
      // Adding a key immediately opens its value menu: the key alone constrains nothing.
      return { active, menu: { kind: "value", index: active.length - 1 } };
    }
    case "set-value": {
      const target = state.active[action.index];
      if (!target) {
        return state;
      }
      const active = state.active.map((filter, index) =>
        index === action.index ? { ...filter, v: action.value } : filter,
      );
      return { active, menu: null };
    }
    case "remove-filter": {
      if (!state.active[action.index]) {
        return state;
      }
      return {
        active: state.active.filter((_filter, index) => index !== action.index),
        menu: null,
      };
    }
    case "clear-filters":
      return { active: [], menu: null };
    default:
      return state;
  }
}

/**
 * Escape has a fixed priority: an open filter menu first, then a non-empty query, then
 * the dialog itself. Collapsing these stages would make the key destroy more than the
 * user expected.
 */
export type EscapeAction = "close-menu" | "clear-query" | "close-dialog";

export function resolveEscape(state: { menu: MenuState; query: string }): EscapeAction {
  if (state.menu) {
    return "close-menu";
  }
  if (state.query.trim()) {
    return "clear-query";
  }
  return "close-dialog";
}

/** Arrow keys wrap without moving DOM focus out of the combobox. */
export function moveActiveIndex(current: number, step: number, length: number): number {
  if (length <= 0) {
    return -1;
  }
  if (current < 0) {
    return step > 0 ? 0 : length - 1;
  }
  return (current + step + length) % length;
}

/** Roving tabindex over the rendered category tabs. */
export function moveTabIndex(current: number, step: number, length: number): number {
  if (length <= 0) {
    return -1;
  }
  return (current + step + length) % length;
}

/**
 * A count states only what the response supports. `maybe_more` without an exact total
 * means the returned set was capped, so the count must not read as a total.
 */
export function formatCount(total: number, maybeMore: boolean): string {
  const safeTotal = Number.isFinite(total) && total > 0 ? Math.trunc(total) : 0;
  return maybeMore && safeTotal > 0 ? `${safeTotal}+` : String(safeTotal);
}

export type CategoryCount = {
  total: number;
  maybeMore: boolean;
};

/**
 * Counts describe the query-plus-filter set before the selected category narrows it, so
 * they come from the grouped `all` response and a tab switch must not change them.
 */
export function categoryCountsFromGroups(
  groups: Array<{ group: string; total: number; maybe_more?: boolean }>,
): Record<string, CategoryCount> {
  const counts: Record<string, CategoryCount> = {};
  let everythingTotal = 0;
  let everythingMaybeMore = false;
  for (const group of groups) {
    const key = String(group.group || "").trim();
    if (!key) {
      continue;
    }
    const total = Number.isFinite(group.total) ? Math.max(0, Math.trunc(group.total)) : 0;
    const maybeMore = Boolean(group.maybe_more);
    counts[key] = { total, maybeMore };
    everythingTotal += total;
    everythingMaybeMore = everythingMaybeMore || maybeMore;
  }
  counts.all = { total: everythingTotal, maybeMore: everythingMaybeMore };
  return counts;
}

/** Map the active filter list onto the request model the search API publishes. */
export function searchParamsFromFilters(active: ActiveFilter[]): Record<string, string> {
  const params: Record<string, string> = {};
  for (const filter of active) {
    const value = filter.v.trim();
    if (!value) {
      continue;
    }
    if (filter.k === "workspace") {
      params.workspace_id = value;
    } else if (filter.k === "project") {
      params.project_id = value;
    } else {
      params[filter.k] = value;
    }
  }
  return params;
}
