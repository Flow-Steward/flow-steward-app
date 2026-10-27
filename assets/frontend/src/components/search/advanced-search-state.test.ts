import {
  availableFilterKeys,
  categoryCountsFromGroups,
  filterReducer,
  filterValueLabel,
  formatCount,
  isFilterRemovable,
  moveActiveIndex,
  moveTabIndex,
  resolveEscape,
  searchParamsFromFilters,
  type FilterDefinition,
  type FilterState,
} from "@/components/search/advanced-search-state";

const DEFINITIONS: FilterDefinition[] = [
  {
    key: "workspace",
    label: "Workspace",
    options: [
      { value: "ws-1", label: "Acme Operations" },
      { value: "ws-2", label: "Northwind Labs" },
    ],
  },
  { key: "project", label: "Project", options: [{ value: "p-1", label: "Payments" }] },
  { key: "status", label: "Status", options: [{ value: "failed", label: "Failed" }] },
  { key: "updated", label: "Updated", options: [{ value: "7d", label: "Last 7 days" }] },
];

const EMPTY: FilterState = { active: [], menu: null };

describe("advanced search filter algebra", () => {
  it("adds a key once and immediately opens its value menu", () => {
    const added = filterReducer(EMPTY, { type: "add-filter", key: "project" });

    expect(added.active).toEqual([{ k: "project", v: "" }]);
    expect(added.menu).toEqual({ kind: "value", index: 0 });

    // A key already present must not be appended a second time.
    expect(filterReducer(added, { type: "add-filter", key: "project" })).toBe(added);
  });

  it("excludes active keys from the Add filter menu", () => {
    const state = filterReducer(EMPTY, { type: "add-filter", key: "status" });

    expect(availableFilterKeys(DEFINITIONS, state.active)).toEqual([
      "workspace",
      "project",
      "updated",
    ]);
    expect(availableFilterKeys(DEFINITIONS, [])).toHaveLength(4);
  });

  it("replaces the value and closes the menu on selection", () => {
    let state = filterReducer(EMPTY, { type: "add-filter", key: "status" });
    state = filterReducer(state, { type: "set-value", index: 0, value: "failed" });

    expect(state.active).toEqual([{ k: "status", v: "failed" }]);
    expect(state.menu).toBeNull();
    expect(filterValueLabel(DEFINITIONS, state.active[0])).toBe("Failed");
  });

  it("keeps a just-added key out of the request until it has a value", () => {
    const state = filterReducer(EMPTY, { type: "add-filter", key: "status" });

    expect(searchParamsFromFilters(state.active)).toEqual({});
  });

  it("maps active filters onto the published request model", () => {
    const active = [
      { k: "workspace" as const, v: "ws-1" },
      { k: "project" as const, v: "p-1" },
      { k: "status" as const, v: "failed" },
      { k: "updated" as const, v: "7d" },
    ];

    expect(searchParamsFromFilters(active)).toEqual({
      workspace_id: "ws-1",
      project_id: "p-1",
      status: "failed",
      updated: "7d",
    });
  });

  it("removes one filter and clears them all without touching anything else", () => {
    let state = filterReducer(EMPTY, { type: "add-filter", key: "status" });
    state = filterReducer(state, { type: "set-value", index: 0, value: "failed" });
    state = filterReducer(state, { type: "add-filter", key: "updated" });
    state = filterReducer(state, { type: "set-value", index: 1, value: "7d" });

    const removed = filterReducer(state, { type: "remove-filter", index: 0 });
    expect(removed.active).toEqual([{ k: "updated", v: "7d" }]);

    expect(filterReducer(state, { type: "clear-filters" })).toEqual({
      active: [],
      menu: null,
    });
  });

  it("toggles an open menu closed and lets another trigger replace it", () => {
    let state = filterReducer(EMPTY, { type: "add-filter", key: "status" });
    state = filterReducer(state, { type: "set-value", index: 0, value: "failed" });

    state = filterReducer(state, { type: "open-value-menu", index: 0 });
    expect(state.menu).toEqual({ kind: "value", index: 0 });

    // The same trigger closes it.
    state = filterReducer(state, { type: "open-value-menu", index: 0 });
    expect(state.menu).toBeNull();

    // Another trigger replaces it, so only one menu is ever open.
    state = filterReducer(state, { type: "open-value-menu", index: 0 });
    state = filterReducer(state, { type: "open-add-menu" });
    expect(state.menu).toEqual({ kind: "add" });
  });

  it("treats a workspace scope as non-removable context without account-wide search", () => {
    const workspace = { k: "workspace" as const, v: "ws-1" };
    const project = { k: "project" as const, v: "p-1" };

    expect(isFilterRemovable(workspace, { canSearchAccountWide: false })).toBe(false);
    expect(isFilterRemovable(workspace, { canSearchAccountWide: true })).toBe(true);
    expect(isFilterRemovable(project, { canSearchAccountWide: false })).toBe(true);
  });
});

describe("advanced search keyboard resolution", () => {
  it("resolves Escape as menu, then query, then dialog", () => {
    expect(resolveEscape({ menu: { kind: "add" }, query: "deploy" })).toBe("close-menu");
    expect(resolveEscape({ menu: null, query: "deploy" })).toBe("clear-query");
    expect(resolveEscape({ menu: null, query: "   " })).toBe("close-dialog");
    expect(resolveEscape({ menu: null, query: "" })).toBe("close-dialog");
  });

  it("wraps the active result in both directions", () => {
    expect(moveActiveIndex(0, 1, 3)).toBe(1);
    expect(moveActiveIndex(2, 1, 3)).toBe(0);
    expect(moveActiveIndex(0, -1, 3)).toBe(2);
    expect(moveActiveIndex(-1, 1, 3)).toBe(0);
    expect(moveActiveIndex(-1, -1, 3)).toBe(2);
    expect(moveActiveIndex(0, 1, 0)).toBe(-1);
  });

  it("rotates the roving tab index", () => {
    expect(moveTabIndex(0, -1, 8)).toBe(7);
    expect(moveTabIndex(7, 1, 8)).toBe(0);
    expect(moveTabIndex(0, 1, 0)).toBe(-1);
  });
});

describe("advanced search counts", () => {
  it("marks a count approximate only when more may exist", () => {
    expect(formatCount(5, true)).toBe("5+");
    expect(formatCount(5, false)).toBe("5");
    // Zero is exact: there is nothing beyond an empty set to imply.
    expect(formatCount(0, true)).toBe("0");
    expect(formatCount(-3, false)).toBe("0");
  });

  it("derives Everything from the group totals it was given", () => {
    const counts = categoryCountsFromGroups([
      { group: "jobs", total: 5, maybe_more: true },
      { group: "kb", total: 0, maybe_more: false },
      { group: "runtime_messages", total: 2, maybe_more: false },
    ]);

    expect(counts.jobs).toEqual({ total: 5, maybeMore: true });
    expect(counts.kb).toEqual({ total: 0, maybeMore: false });
    expect(counts.all).toEqual({ total: 7, maybeMore: true });
    expect(formatCount(counts.all.total, counts.all.maybeMore)).toBe("7+");
  });

  it("keeps Everything exact when no group reports more", () => {
    const counts = categoryCountsFromGroups([
      { group: "jobs", total: 1, maybe_more: false },
      { group: "kb", total: 0 },
    ]);

    expect(counts.all).toEqual({ total: 1, maybeMore: false });
    expect(formatCount(counts.all.total, counts.all.maybeMore)).toBe("1");
  });
});
