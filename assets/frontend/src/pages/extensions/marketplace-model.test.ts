import type { ExtensionCatalogEntry } from "@/pages/extensions/catalog-api";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  computeMarketplaceFacets,
  dateLabel,
  describeEntry,
  EMPTY_MARKETPLACE_FILTERS,
  earliestAddedAt,
  entryPath,
  filterMarketplace,
  incompatibleReason,
  isNew,
  mainAction,
  marketplaceBadges,
  marketplaceLabels,
  marketplaceSortOptions,
  type MarketplaceFilters,
  type MarketplaceLabels,
  orderedCapabilities,
  orderedPermissions,
  readMarketplaceUrl,
  resolveFind,
  sizeLabel,
  versionText,
  withoutMarketplaceParams,
  withoutStaleSelections,
  worksWith,
  writeMarketplaceUrl,
} from "@/pages/extensions/marketplace-model";

const DAY = 24 * 60 * 60 * 1000;
const NOW = Date.parse("2026-10-20T12:00:00Z");

const LABELS: MarketplaceLabels = {
  categories: { crm: "CRM", data: "Data" },
  capabilities: { tool: "Agent tools", query: "Data queries", action: "Workflow actions" },
  permissions: { "extension:invoke": "Run its own operations", "artifact:read": "Read artifacts" },
};

function entry(overrides: Partial<ExtensionCatalogEntry> & { extension_id: string }): ExtensionCatalogEntry {
  return {
    version: "1.0.0",
    name: overrides.extension_id,
    summary: "",
    description: "",
    categories: [],
    capabilities: [],
    repository_url: "https://example.test/repo",
    release_zip_url: "https://example.test/x.zip",
    release_zip_bytes: 1000,
    release_zip_sha256: "a".repeat(64),
    permissions_summary: [],
    setup_summary: "",
    publication_status: "published",
    verification_status: "verified",
    has_icon: true,
    icon_url: `/api/extensions/catalog/${overrides.extension_id}/icon?v=abc`,
    display_state: "install",
    compatible: true,
    installed_version: "",
    installed_install_id: "",
    installed_state: "",
    ...overrides,
  };
}

const ITEMS = [
  entry({ extension_id: "acme.crm", name: "Acme CRM", categories: ["crm"], capabilities: ["tool", "action"], tags: ["contacts"] }),
  entry({ extension_id: "acme.mail", name: "Acme Mail", categories: ["crm"], capabilities: ["action"], display_state: "installed" }),
  entry({ extension_id: "acme.db", name: "Acme DB", categories: ["data"], capabilities: ["query"], display_state: "installed_newer" }),
];

function filters(patch: Partial<MarketplaceFilters>): MarketplaceFilters {
  return { ...EMPTY_MARKETPLACE_FILTERS, ...patch };
}

test("localized marketplace labels retain protocol filters and version compatibility decisions", () => {
  const messages: Record<string, string> = {
    "react.extensions.marketplace_final_not_installed": "Neįdiegta",
    "react.extensions.installed": "Įdiegta",
    "react.extensions.marketplace_final_needs_version": "Reikia Flow Steward {min} arba naujesnės versijos.",
    "react.extensions.marketplace_final_only_admins_install": "Plėtinius gali įdiegti tik administratoriai.",
  };
  const t: I18nContextValue["t"] = (key, defaultText, params) =>
    fallbackContext.t(key, messages[key] || defaultText, params);
  const facets = computeMarketplaceFacets(ITEMS, EMPTY_MARKETPLACE_FILTERS, LABELS, t);
  expect(facets.find((facet) => facet.key === "status")?.values.map((value) => [value.value, value.label])).toEqual([
    ["not_installed", "Neįdiegta"],
    ["installed", "Įdiegta"],
  ]);
  expect(filterMarketplace(ITEMS, filters({ status: ["installed"] }), LABELS, "name", t).map((item) => item.extension_id)).toEqual(["acme.db", "acme.mail"]);
  const incompatible = entry({ extension_id: "a", display_state: "incompatible", compatible_flow_steward: { min: "1.2.0" } });
  expect(incompatibleReason(incompatible, "1.1.0", t)).toEqual({ short: "Reikia Flow Steward 1.2.0 arba naujesnės versijos.", full: "Reikia Flow Steward 1.2.0 arba naujesnės versijos." });
  expect(mainAction(ITEMS[0], { canAdminister: false, installBlockedReason: "", installationVersion: "1.1.0" }, t)).toEqual({ kind: "install", disabled: true, note: "Plėtinius gali įdiegti tik administratoriai." });
});

test("OR inside a facet, AND between facets, then search", () => {
  const ids = (list: ExtensionCatalogEntry[]) => list.map((item) => item.extension_id);
  expect(ids(filterMarketplace(ITEMS, filters({ capability: ["tool", "query"] }), LABELS, "name"))).toEqual([
    "acme.crm",
    "acme.db",
  ]);
  expect(ids(filterMarketplace(ITEMS, filters({ category: ["crm"], capability: ["action"], status: ["installed"] }), LABELS, "name"))).toEqual(["acme.mail"]);
  // installed_newer counts as Installed.
  expect(ids(filterMarketplace(ITEMS, filters({ status: ["installed"] }), LABELS, "name"))).toEqual(["acme.db", "acme.mail"]);
});

test("search covers name, summary, description, id and tags, case-insensitively", () => {
  const ids = (q: string) => filterMarketplace(ITEMS, filters({ q }), LABELS, "name").map((item) => item.extension_id);
  expect(ids("CONTACTS")).toEqual(["acme.crm"]);
  expect(ids("acme.db")).toEqual(["acme.db"]);
  expect(ids("mail")).toEqual(["acme.mail"]);
});

test("facet counts apply the other facets but not their own; zero stays in place", () => {
  const facets = computeMarketplaceFacets(ITEMS, filters({ status: ["not_installed"] }), LABELS);
  const capability = facets.find((facet) => facet.key === "capability");
  // By count over the whole catalog, then by name.
  expect(capability?.values.map((value) => [value.label, value.count])).toEqual([
    ["Workflow actions", 1],
    ["Agent tools", 1],
    ["Data queries", 0],
  ]);
  const status = facets.find((facet) => facet.key === "status");
  // Fixed order, and only the values the catalog has.
  expect(status?.values.map((value) => [value.label, value.count])).toEqual([
    ["Not installed", 1],
    ["Installed", 2],
  ]);
});

test("a facet with a single value is still shown", () => {
  const facets = computeMarketplaceFacets([ITEMS[0], ITEMS[1]], EMPTY_MARKETPLACE_FILTERS, LABELS);
  expect(facets.map((facet) => facet.key)).toEqual(["category", "capability", "status"]);
});

test("an unknown capability or category is shown as its id", () => {
  const labels = marketplaceLabels(undefined);
  const facets = computeMarketplaceFacets(ITEMS, EMPTY_MARKETPLACE_FILTERS, labels);
  expect(facets.find((facet) => facet.key === "capability")?.values.map((value) => value.label)).toEqual([
    "action",
    "query",
    "tool",
  ]);
  expect(orderedCapabilities(ITEMS[0], ["action"], labels)).toEqual(["action", "tool"]);
});

test("sorts: A–Z and Recently added; deprecated always last", () => {
  const items = [
    entry({ extension_id: "a", name: "Alpha", added_at: "2026-09-01T00:00:00Z", updated_at: "2026-10-05T00:00:00Z" }),
    entry({ extension_id: "b", name: "Bravo", added_at: "2026-09-10T00:00:00Z", updated_at: "2026-09-11T00:00:00Z" }),
    entry({ extension_id: "c", name: "Charlie", added_at: "2026-09-20T00:00:00Z", publication_status: "deprecated" }),
  ];
  const order = (sort: "name" | "added") =>
    filterMarketplace(items, EMPTY_MARKETPLACE_FILTERS, LABELS, sort).map((item) => item.extension_id);
  expect(order("name")).toEqual(["a", "b", "c"]);
  expect(order("added")).toEqual(["b", "a", "c"]);
  // No Newest, no Recently updated: the two choices, however much the index dates.
  expect(marketplaceSortOptions(items).map((option) => option.label)).toEqual(["A–Z", "Recently added"]);
  expect(marketplaceSortOptions([items[0]])).toEqual([]);
  // A–Z alone is no choice.
  expect(marketplaceSortOptions([entry({ extension_id: "x" }), entry({ extension_id: "y" })])).toEqual([]);
});

test("New: within 30 days and after the catalog's launch week", () => {
  const first = entry({ extension_id: "first", added_at: "2026-10-01T09:00:00Z" });
  const sameBatch = entry({ extension_id: "batch", added_at: "2026-10-01T10:17:00Z" });
  const later = entry({ extension_id: "later", added_at: "2026-10-15T09:00:00Z" });
  const old = entry({ extension_id: "old", added_at: "2026-09-01T09:00:00Z" });
  const earliest = earliestAddedAt([first, sameBatch, later, old]);
  expect(earliest).toBe(Date.parse("2026-09-01T09:00:00Z"));
  expect(isNew(later, earliest, NOW)).toBe(true);
  expect(isNew(old, earliest, NOW)).toBe(false);
  // The launch batch: everything arrived together, so nothing is New.
  const launch = earliestAddedAt([first, sameBatch]);
  expect(isNew(sameBatch, launch, NOW - 10 * DAY)).toBe(false);
  // A launch seeded over two days (the MCP catalog: 16 servers, then 239 the next day) is still the launch.
  const nextDay = entry({ extension_id: "next-day", added_at: "2026-10-02T11:00:00Z" });
  expect(isNew(nextDay, launch, Date.parse("2026-10-12T00:00:00Z"))).toBe(false);
  expect(isNew(entry({ extension_id: "undated" }), earliest, NOW)).toBe(false);
});

test("at most two badges: the state, then Deprecated; New only when not installed", () => {
  const earliest = Date.parse("2026-01-01T00:00:00Z");
  const fresh = { added_at: "2026-10-15T00:00:00Z" };
  // The design's tones: Installed success with a check, Incompatible danger and
  // Deprecated attention with an alert; New is the Iridescent pill.
  const labels = (item: ExtensionCatalogEntry) =>
    marketplaceBadges(item, earliest, NOW).map((badge) => `${badge.label}:${badge.tone}${badge.icon ? `:${badge.icon}` : ""}`);
  expect(labels(entry({ extension_id: "a", ...fresh }))).toEqual(["New:new"]);
  expect(labels(entry({ extension_id: "b", ...fresh, display_state: "installed" }))).toEqual(["Installed:green:check"]);
  expect(labels(entry({ extension_id: "c", display_state: "installed_newer" }))).toEqual(["Installed:green:check"]);
  expect(labels(entry({ extension_id: "d", display_state: "update_available" }))).toEqual(["Update available:orange"]);
  expect(labels(entry({ extension_id: "e", display_state: "installed_disabled", publication_status: "deprecated" }))).toEqual([
    "Disabled:neutral",
    "Deprecated:orange:alert",
  ]);
  expect(labels(entry({ extension_id: "f", ...fresh, display_state: "incompatible" }))).toEqual(["Incompatible:danger:alert"]);
  expect(labels(entry({ extension_id: "g", ...fresh, publication_status: "deprecated" }))).toEqual(["Deprecated:orange:alert"]);
  expect(labels(entry({ extension_id: "h" }))).toEqual([]);
});

test("the Incompatible reason is derived, never naming this server's architecture", () => {
  const incompatible = { display_state: "incompatible" as const };
  expect(
    incompatibleReason(entry({ extension_id: "a", ...incompatible, available_for_host: false, runtime_targets: ["linux-amd64", "linux-arm64"] }), "1.0.0"),
  ).toEqual({ short: "No package for this server.", full: "No package for this server. Available for linux-amd64, linux-arm64." });
  expect(incompatibleReason(entry({ extension_id: "b", ...incompatible, compatible_flow_steward: { min: "0.5.0" } }), "0.4.0")?.full).toBe(
    "Needs Flow Steward 0.5.0 or later.",
  );
  expect(incompatibleReason(entry({ extension_id: "c", ...incompatible, compatible_flow_steward: { max: "0.3.0" } }), "0.4.0")?.full).toBe(
    "Works up to Flow Steward 0.3.0.",
  );
  // Without this installation's version, the range is still the likeliest reason.
  expect(incompatibleReason(entry({ extension_id: "d", ...incompatible, compatible_flow_steward: { min: "0.5.0" } }), "")?.full).toBe(
    "Needs Flow Steward 0.5.0 or later.",
  );
  expect(incompatibleReason(entry({ extension_id: "e", ...incompatible }), "1.0.0")?.full).toBe("Not compatible with this server.");
  expect(incompatibleReason(entry({ extension_id: "f" }), "1.0.0")).toBeNull();
});

test("the main action and the text beside it, in every state", () => {
  const can = { canAdminister: true, installBlockedReason: "", installationVersion: "1.0.0" };
  expect(mainAction(entry({ extension_id: "a" }), can)).toEqual({ kind: "install", disabled: false, note: "" });
  expect(mainAction(entry({ extension_id: "a" }), { ...can, canAdminister: false })).toEqual({
    kind: "install",
    disabled: true,
    note: "Only administrators can install extensions.",
  });
  expect(mainAction(entry({ extension_id: "a" }), { ...can, installBlockedReason: "Archive changes are off." })).toEqual({
    kind: "install",
    disabled: true,
    note: "Archive changes are off.",
  });
  expect(mainAction(entry({ extension_id: "a", display_state: "incompatible", available_for_host: false }), can)).toEqual({
    kind: "install",
    disabled: true,
    note: "No package for this server.",
  });
  expect(mainAction(entry({ extension_id: "a", display_state: "installed" }), can)).toEqual({ kind: "open", note: "" });
  expect(mainAction(entry({ extension_id: "a", display_state: "installed_newer" }), can)).toEqual({ kind: "open", note: "" });
  expect(mainAction(entry({ extension_id: "a", display_state: "installed_disabled" }), can)).toEqual({
    kind: "open",
    note: "Installed but turned off.",
  });
  expect(
    mainAction(entry({ extension_id: "a", display_state: "update_available", version: "1.0.1", installed_version: "1.0.0" }), can),
  ).toEqual({
    kind: "update",
    disabled: false,
    note: "Version 1.0.1 is available; 1.0.0 is installed.",
    addedPermissions: [],
  });
});

test("Update is the main action for a newer release, with its permission changes and who may run it", () => {
  const can = { canAdminister: true, installBlockedReason: "", installationVersion: "1.0.0" };
  const update = entry({
    extension_id: "a",
    display_state: "update_available",
    version: "1.1.0",
    installed_version: "1.0.0",
    added_permissions: ["artifact:write"],
  });
  expect(mainAction(update, can)).toEqual({
    kind: "update",
    disabled: false,
    note: "Version 1.1.0 is available; 1.0.0 is installed.",
    addedPermissions: ["artifact:write"],
  });
  expect(mainAction(update, { ...can, canAdminister: false })).toEqual({
    kind: "update",
    disabled: true,
    note: "Version 1.1.0 is available; 1.0.0 is installed. Only administrators can update extensions.",
    addedPermissions: ["artifact:write"],
  });
  expect(mainAction(update, { ...can, installBlockedReason: "Archive changes are off." })).toEqual({
    kind: "update",
    disabled: true,
    note: "Version 1.1.0 is available; 1.0.0 is installed. Archive changes are off.",
    addedPermissions: ["artifact:write"],
  });
  // A turned-off install is updated too, and stays off.
  expect(mainAction({ ...update, installed_state: "disabled" }, can)).toEqual({
    kind: "update",
    disabled: false,
    note: "Version 1.1.0 is available; 1.0.0 is installed. It's turned off and stays off after the update.",
    addedPermissions: ["artifact:write"],
  });
});

test("text helpers: version, permissions order, size, date, range, description", () => {
  expect(versionText(entry({ extension_id: "a", version: "1.0.1" }))).toBe("v1.0.1");
  // The catalog's release, as the design writes it; the installed one is a page fact.
  expect(versionText(entry({ extension_id: "a", version: "1.0.1", display_state: "update_available", installed_version: "1.0.0" }))).toBe(
    "v1.0.1",
  );
  expect(orderedPermissions(entry({ extension_id: "a", permissions_summary: ["extension:invoke", "artifact:read"] }))).toEqual([
    "artifact:read",
    "extension:invoke",
  ]);
  expect(sizeLabel(70)).toBe("70 B");
  expect(sizeLabel(65_536)).toBe("66 KB");
  expect(sizeLabel(12_400_000)).toBe("12.4 MB");
  expect(dateLabel("2026-10-01T23:30:00Z")).toBe("1 Oct 2026");
  expect(worksWith(entry({ extension_id: "a", compatible_flow_steward: { min: "0.5.0" } }))).toBe("Flow Steward 0.5.0 or later");
  expect(worksWith(entry({ extension_id: "a" }))).toBe("");
  expect(
    describeEntry(ITEMS[1], { labels: LABELS, earliest: Number.NaN, now: NOW, installationVersion: "" }),
  ).toBe("Category: CRM. Capabilities: Workflow actions. Installed.");
});

test("the URL keeps the browse state in mk_ params and leaves other keys alone", () => {
  const state = readMarketplaceUrl(
    new URLSearchParams("mk_q=crm&mk_category=crm,data&mk_status=installed&mk_sort=added&mk_page=2&mk_view=list&mk_entry=acme.crm&tab=x"),
  );
  expect(state).toEqual({
    filters: { q: "crm", category: ["crm", "data"], capability: [], status: ["installed"] },
    sort: "added",
    page: 2,
    view: "list",
    entry: "acme.crm",
  });
  const written = writeMarketplaceUrl(new URLSearchParams("tab=x&mk_page=4"), { ...state, sort: "name", page: 1, view: "grid", entry: "" });
  expect(written.toString()).toBe("tab=x&mk_q=crm&mk_category=crm%2Cdata&mk_status=installed");
  expect(readMarketplaceUrl(new URLSearchParams("mk_sort=bogus&mk_page=-3")).sort).toBe("name");
  // The retired sorts fall back to A–Z.
  expect(readMarketplaceUrl(new URLSearchParams("mk_sort=newest")).sort).toBe("name");
  expect(readMarketplaceUrl(new URLSearchParams("mk_sort=updated")).sort).toBe("name");
  expect(withoutMarketplaceParams(new URLSearchParams("mk_q=a&find=b&project_id=p")).toString()).toBe("project_id=p");
  expect(entryPath("acme.crm")).toBe("/extensions?mk_entry=acme.crm#marketplace");
});

test("find opens a listed extension on the whole catalog, and names an unknown one", () => {
  const known = resolveFind(new URLSearchParams("find=acme.crm&mk_q=zzz&mk_category=data"), ITEMS);
  expect(known?.missing).toBe("");
  expect(known?.params.toString()).toBe("mk_entry=acme.crm");
  const unknown = resolveFind(new URLSearchParams("find=acme.nothing"), ITEMS);
  expect(unknown?.missing).toBe("acme.nothing");
  expect(unknown?.params.has("mk_entry")).toBe(false);
  expect(resolveFind(new URLSearchParams("mk_q=a"), ITEMS)).toBeNull();
});

test("a selected value the loaded index no longer has is dropped", () => {
  const current = filters({ category: ["crm", "gone"], capability: ["tool"] });
  const facets = computeMarketplaceFacets(ITEMS, current, LABELS);
  expect(withoutStaleSelections(current, facets)).toEqual(filters({ category: ["crm"], capability: ["tool"] }));
  expect(withoutStaleSelections(filters({ category: ["crm"] }), facets)).toBeNull();
});
