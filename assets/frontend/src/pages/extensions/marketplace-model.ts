import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/*
 * The browse rules of the Extensions Marketplace: facets and counts, search,
 * sorting, the badges a card wears, why an extension is incompatible, the one
 * main action on its page, and the `mk_` URL the selection lives in. Pure
 * functions over the loaded index; the facet engine and the URL spelling are
 * the static catalogs' shared ones, and this module brings the extension
 * vocabulary.
 */
import type { DsBadgeTone } from "@/components/shared/design-system";
import {
  type CatalogFacet,
  type CatalogFacetDef,
  type CatalogFilterState,
  type CatalogUrlSpec,
  type CatalogUrlState,
  catalogTextMatches,
  compareAddedNewestFirst,
  compareVersions,
  computeCatalogFacets,
  earliestAddedTime,
  filterCatalogItems,
  isRecentlyAdded,
  readCatalogUrlState,
  selectedValues,
  sortCatalogItems,
  writeCatalogUrlState,
} from "@/lib/utils/catalog-facets";
import type {
  ExtensionCatalogDisplayState,
  ExtensionCatalogEntry,
  ExtensionCatalogInstallState,
  ExtensionCatalogListing,
} from "@/pages/extensions/catalog-api";

export type MarketplaceFacetKey = "category" | "capability" | "status";

export const MARKETPLACE_FACET_KEYS: readonly MarketplaceFacetKey[] = ["category", "capability", "status"];

export type MarketplaceFilters = CatalogFilterState<MarketplaceFacetKey>;

export const EMPTY_MARKETPLACE_FILTERS: MarketplaceFilters = { q: "", category: [], capability: [], status: [] };

export type MarketplaceSort = "name" | "added";

export const MARKETPLACE_PAGE_SIZE = 24;

export const SUGGEST_EXTENSION_URL = "https://github.com/Flow-Steward/extensions";

/* ---- Vocabulary ---------------------------------------------------------- */

export type MarketplaceStatus = "not_installed" | "installed" | "update_available" | "disabled" | "incompatible";

/** The Status facet's values, in this fixed order. */
export function marketplaceStatusLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<MarketplaceStatus, string> {
  return {
    not_installed: t("react.extensions.marketplace_final_not_installed", "Not installed"),
    installed: t("react.extensions.installed", "Installed"),
    update_available: t("react.extensions.update_available", "Update available"),
    disabled: t("react.workflows.disabled", "Disabled"),
    incompatible: t("react.extensions.incompatible", "Incompatible"),
  };
}

export const STATUS_LABELS: Record<MarketplaceStatus, string> = marketplaceStatusLabels();

const STATUS_ORDER = Object.keys(STATUS_LABELS);

/** A newer install than the catalog's is still Installed: the same actions apply. */
const STATUS_OF: Record<ExtensionCatalogDisplayState, MarketplaceStatus> = {
  install: "not_installed",
  installed: "installed",
  installed_newer: "installed",
  update_available: "update_available",
  installed_disabled: "disabled",
  incompatible: "incompatible",
};

export function marketplaceStatus(entry: ExtensionCatalogEntry): MarketplaceStatus {
  return STATUS_OF[entry.display_state] ?? "not_installed";
}

export type MarketplaceLabels = {
  categories: Record<string, string>;
  capabilities: Record<string, string>;
  permissions: Record<string, string>;
};

export const NO_MARKETPLACE_LABELS: MarketplaceLabels = { categories: {}, capabilities: {}, permissions: {} };

/** The names the listing carries; Core names capabilities and permissions, the index categories. */
export function marketplaceLabels(listing: ExtensionCatalogListing | undefined): MarketplaceLabels {
  if (!listing) return NO_MARKETPLACE_LABELS;
  return {
    categories: listing.category_labels ?? {},
    capabilities: listing.capability_labels ?? {},
    permissions: listing.permission_labels ?? {},
  };
}

export function categoryName(labels: MarketplaceLabels, id: string): string {
  return labels.categories[id] || id;
}

export function capabilityName(labels: MarketplaceLabels, id: string): string {
  return labels.capabilities[id] || id;
}

export function permissionName(labels: MarketplaceLabels, id: string): string {
  return labels.permissions[id] || id;
}

/* ---- Facets, search, sort ------------------------------------------------ */

export type MarketplaceFacet = CatalogFacet<ExtensionCatalogEntry, MarketplaceFacetKey>;

function facetDefs(labels: MarketplaceLabels, t: I18nContextValue["t"] = fallbackContext.t): Array<CatalogFacetDef<ExtensionCatalogEntry, MarketplaceFacetKey>> {
  return [
    {
      key: "category",
      title: t("trash.columns.category", "Category"),
      noun: t("react.extensions.marketplace_final_categories", "categories"),
      order: "total",
      valuesOf: (entry) => entry.categories,
      label: (value) => categoryName(labels, value),
    },
    {
      key: "capability",
      title: t("react.extensions.capability", "Capability"),
      noun: t("react.extensions.marketplace_final_capabilities", "capabilities"),
      order: "total",
      valuesOf: (entry) => entry.capabilities,
      label: (value) => capabilityName(labels, value),
    },
    {
      key: "status",
      title: t("extensions.runtime.status", "Status"),
      noun: t("react.extensions.marketplace_final_statuses", "statuses"),
      order: "fixed",
      fixed: STATUS_ORDER,
      valuesOf: (entry) => [marketplaceStatus(entry)],
      label: (value) => marketplaceStatusLabels(t)[value as MarketplaceStatus] || value,
    },
  ];
}

/** Name, summary, description, id and tags, case-insensitive. */
function matchesSearch(entry: ExtensionCatalogEntry, needle: string): boolean {
  return catalogTextMatches(
    [entry.name, entry.summary, entry.description ?? "", entry.extension_id, ...(entry.tags ?? [])],
    needle,
  );
}

export function computeMarketplaceFacets(
  items: readonly ExtensionCatalogEntry[],
  filters: MarketplaceFilters,
  labels: MarketplaceLabels, t: I18nContextValue["t"] = fallbackContext.t
): MarketplaceFacet[] {
  return computeCatalogFacets(items, filters, facetDefs(labels, t), matchesSearch);
}

const COMPARE: Record<MarketplaceSort, (a: ExtensionCatalogEntry, b: ExtensionCatalogEntry) => number> = {
  name: (a, b) => a.name.localeCompare(b.name),
  added: (a, b) => compareAddedNewestFirst(a.added_at, b.added_at),
};

/** OR within a facet, AND between facets, then the search; deprecated ones last. */
export function filterMarketplace(
  items: readonly ExtensionCatalogEntry[],
  filters: MarketplaceFilters,
  labels: MarketplaceLabels,
  sort: MarketplaceSort, t: I18nContextValue["t"] = fallbackContext.t
): ExtensionCatalogEntry[] {
  const matched = filterCatalogItems(items, filters, facetDefs(labels, t), matchesSearch);
  return sortCatalogItems(matched, COMPARE[sort] ?? COMPARE.name);
}

/**
 * The sorts the loaded index supports, read from the index and not the
 * results, so Sort never comes and goes while someone types: A–Z, and
 * Recently added when the index carries dates. A single option is no choice.
 */
export function marketplaceSortOptions(
  items: readonly ExtensionCatalogEntry[], t: I18nContextValue["t"] = fallbackContext.t
): Array<{ value: MarketplaceSort; label: string }> {
  if (items.length < 2 || !items.some((entry) => entry.added_at)) return [];
  return [
    { value: "name", label: "A–Z" },
    { value: "added", label: t("react.extensions.recently_added", "Recently added") },
  ];
}

/** Selected values the loaded index no longer has; null when every one is still there. */
export function withoutStaleSelections(
  filters: MarketplaceFilters,
  facets: readonly MarketplaceFacet[],
): MarketplaceFilters | null {
  let changed = false;
  const next = { ...filters };
  for (const key of MARKETPLACE_FACET_KEYS) {
    const known = new Set(facets.find((facet) => facet.key === key)?.values.map((value) => value.value) ?? []);
    const kept = selectedValues(filters, key).filter((value) => known.has(value));
    if (kept.length !== selectedValues(filters, key).length) {
      next[key] = kept;
      changed = true;
    }
  }
  return changed ? next : null;
}

/* ---- Badges and text ----------------------------------------------------- */

/** A pill a card, row or page wears; an icon reads with the tone, never alone. */
export type MarketplaceBadge = { tone: DsBadgeTone; label: string; icon?: "check" | "alert" };

function localizedSTATE_BADGES(t: I18nContextValue["t"] = fallbackContext.t): Partial<Record<ExtensionCatalogDisplayState, MarketplaceBadge>> {
  return {
  installed: { tone: "green", icon: "check", label: t("react.extensions.installed", "Installed") },
  installed_newer: { tone: "green", icon: "check", label: t("react.extensions.installed", "Installed") },
  update_available: { tone: "orange", label: t("react.extensions.update_available", "Update available") },
  installed_disabled: { tone: "neutral", label: t("react.workflows.disabled", "Disabled") },
  incompatible: { tone: "danger", icon: "alert", label: t("react.extensions.incompatible", "Incompatible") },
};
}

/** The earliest `added_at` in the index, or NaN when none carries one. */
export function earliestAddedAt(items: readonly ExtensionCatalogEntry[]): number {
  return earliestAddedTime(items.map((entry) => entry.added_at));
}

/** Added within 30 days, and more than a day after the catalog's first entries. */
export function isNew(entry: ExtensionCatalogEntry, earliest: number, now: number): boolean {
  return isRecentlyAdded(entry.added_at, earliest, now);
}

/** The state badge; Not installed wears none. */
export function stateBadge(entry: ExtensionCatalogEntry, t: I18nContextValue["t"] = fallbackContext.t): MarketplaceBadge | null {
  return localizedSTATE_BADGES(t)[entry.display_state] ?? null;
}

/**
 * Deprecated, or New on an extension that is neither installed nor deprecated.
 * New is the design's Iridescent pill.
 */
export function lifecycleBadge(entry: ExtensionCatalogEntry, earliest: number, now: number, t: I18nContextValue["t"] = fallbackContext.t): MarketplaceBadge | null {
  if (entry.publication_status === "deprecated") return { tone: "orange", icon: "alert", label: t("react.extensions.deprecated", "Deprecated") };
  if (entry.display_state === "install" && isNew(entry, earliest, now)) return { tone: "new", label: t("react.extensions.new", "New") };
  return null;
}

/** At most two: the state first, then Deprecated or New. */
export function marketplaceBadges(entry: ExtensionCatalogEntry, earliest: number, now: number, t: I18nContextValue["t"] = fallbackContext.t): MarketplaceBadge[] {
  return [stateBadge(entry, t), lifecycleBadge(entry, earliest, now, t)].filter(
    (badge): badge is MarketplaceBadge => badge !== null,
  );
}

export type IncompatibleReason = { short: string; full: string };

/**
 * Why an extension is incompatible here. The API says only `compatible:
 * false`, so the reason is derived; the server's own architecture is never
 * named, only the ones a package exists for.
 */
export function incompatibleReason(
  entry: ExtensionCatalogEntry,
  installationVersion: string, t: I18nContextValue["t"] = fallbackContext.t
): IncompatibleReason | null {
  if (entry.display_state !== "incompatible") return null;
  if (entry.available_for_host === false) {
    const targets = entry.runtime_targets ?? [];
    const short = t("react.extensions.no_package_for_this_server", "No package for this server.");
    return { short, full: targets.length ? t("react.extensions.marketplace_final_host_targets", "{short} Available for {targets}.", { short, targets: targets.join(", ") }) : short };
  }
  const min = entry.compatible_flow_steward?.min || "";
  const max = entry.compatible_flow_steward?.max || "";
  const needs = t("react.extensions.marketplace_final_needs_version", "Needs Flow Steward {min} or later.", { min });
  const upTo = t("react.extensions.marketplace_final_maximum_version", "Works up to Flow Steward {max}.", { max });
  const same = (text: string) => ({ short: text, full: text });
  if (min && compareVersions(installationVersion, min) === -1) return same(needs);
  if (max && compareVersions(installationVersion, max) === 1) return same(upTo);
  // Without this installation's version, the range the entry asks for is the likeliest reason.
  if (compareVersions(installationVersion, "0.0.0") === null) {
    if (min) return same(needs);
    if (max) return same(upTo);
  }
  return same(t("react.extensions.marketplace_final_incompatible", "Not compatible with this server."));
}

/** `v1.0.1`: the catalog's release; the installed one is a fact on the page. */
export function versionText(entry: ExtensionCatalogEntry): string {
  return `v${entry.version}`;
}

/** Capabilities an active Capability filter asked for come first. */
export function orderedCapabilities(
  entry: ExtensionCatalogEntry,
  active: readonly string[],
  labels: MarketplaceLabels,
): string[] {
  const first = entry.capabilities.filter((id) => active.includes(id));
  const rest = entry.capabilities.filter((id) => !active.includes(id));
  return [...first, ...rest].map((id) => capabilityName(labels, id));
}

const RUN_ITS_OWN = "extension:invoke";

/** Every permission, with running its own operations always last. */
export function orderedPermissions(entry: ExtensionCatalogEntry): string[] {
  const rest = entry.permissions_summary.filter((id) => id !== RUN_ITS_OWN);
  return entry.permissions_summary.includes(RUN_ITS_OWN) ? [...rest, RUN_ITS_OWN] : rest;
}

/** 1000-based, as a download is counted: `65 KB`, `12.4 MB`. */
export function sizeLabel(bytes: number, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (!bytes || bytes < 0) return "";
  if (bytes < 1000) return `${Math.round(bytes)} B`;
  if (bytes < 1000 * 1000) return t("react.extensions.value0_kb", "{value0} KB", { value0: Math.max(1, Math.round(bytes / 1000)) });
  return t("react.extensions.value0_mb", "{value0} MB", { value0: (bytes / (1000 * 1000)).toFixed(1) });
}

/** `1 Oct 2026`. */
export function dateLabel(value: string | undefined, locale = "en-GB"): string {
  const time = Date.parse(value ?? "");
  if (!Number.isFinite(time)) return "";
  return new Date(time).toLocaleDateString(locale === "en" ? "en-GB" : locale, {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** `Flow Steward 0.5.0 or later`, or "" when the entry names no range. */
export function worksWith(entry: ExtensionCatalogEntry, t: I18nContextValue["t"] = fallbackContext.t): string {
  const min = entry.compatible_flow_steward?.min || "";
  const max = entry.compatible_flow_steward?.max || "";
  if (min && max) return t("react.extensions.marketplace_final_version_range", "Flow Steward {min} to {max}", { min, max });
  if (min) return t("react.extensions.marketplace_final_version_minimum", "Flow Steward {min} or later", { min });
  if (max) return t("react.extensions.marketplace_final_version_maximum", "Flow Steward up to {max}", { max });
  return "";
}

export type BrowseContext = {
  labels: MarketplaceLabels;
  earliest: number;
  now: number;
  installationVersion: string;
};

/** What a screen reader hears after the name: one sentence each. */
export function describeEntry(entry: ExtensionCatalogEntry, context: BrowseContext, t: I18nContextValue["t"] = fallbackContext.t): string {
  const badges = marketplaceBadges(entry, context.earliest, context.now, t).map((badge) => badge.label);
  const reason = incompatibleReason(entry, context.installationVersion, t);
  const sentence = (text: string) => (text && !/[.!?]$/.test(text) ? `${text}.` : text);
  return [
    sentence(entry.summary),
    entry.categories.length
      ? sentence(t("react.extensions.marketplace_final_category_description", "Category: {categories}", { categories: entry.categories.map((id) => categoryName(context.labels, id)).join(", ") }))
      : "",
    entry.capabilities.length
      ? sentence(t("react.extensions.marketplace_final_capabilities_description", "Capabilities: {capabilities}", { capabilities: entry.capabilities.map((id) => capabilityName(context.labels, id)).join(", ") }))
      : "",
    sentence(badges.length ? badges.join(", ") : marketplaceStatusLabels(t).not_installed),
    reason ? reason.short : "",
  ]
    .filter(Boolean)
    .join(" ");
}

/* ---- The extension page's one main action -------------------------------- */

export type MainAction =
  | { kind: "install"; disabled: boolean; note: string }
  /** Update to the catalog release; "Open installed extension" stays beside it. */
  | { kind: "update"; disabled: boolean; note: string; addedPermissions: string[] }
  | { kind: "open"; note: string };

export const ONLY_ADMINISTRATORS = "Only administrators can install extensions.";
export const ONLY_ADMINISTRATORS_UPDATE = "Only administrators can update extensions.";
export const STAYS_TURNED_OFF = "It's turned off and stays off after the update.";

/**
 * Whether this viewer may install from the results at all — the
 * action-rights rule: without it the cards have no Install button and the
 * table has no Install column.
 */
export function canInstallFromResults(install?: { canAdminister: boolean; installBlockedReason: string }): boolean {
  return Boolean(install?.canAdminister && !install.installBlockedReason);
}

export function mainAction(
  entry: ExtensionCatalogEntry,
  {
    canAdminister,
    installBlockedReason,
    installationVersion,
  }: { canAdminister: boolean; installBlockedReason: string; installationVersion: string }, t: I18nContextValue["t"] = fallbackContext.t
): MainAction {
  switch (entry.display_state) {
    case "incompatible":
      return { kind: "install", disabled: true, note: incompatibleReason(entry, installationVersion, t)?.full ?? "" };
    case "install":
      if (!canAdminister) return { kind: "install", disabled: true, note: t("react.extensions.marketplace_final_only_admins_install", "Only administrators can install extensions.") };
      if (installBlockedReason) return { kind: "install", disabled: true, note: installBlockedReason };
      return { kind: "install", disabled: false, note: "" };
    case "installed_disabled":
      return { kind: "open", note: t("react.extensions.installed_but_turned_off", "Installed but turned off.") };
    case "update_available": {
      const addedPermissions = entry.added_permissions ?? [];
      const turnedOff = entry.installed_state === "disabled" ? ` ${t("react.extensions.marketplace_final_stays_disabled", "It's turned off and stays off after the update.")}` : "";
      const available = `${t("react.extensions.marketplace_final_available_version", "Version {version} is available; {installed} is installed.", { version: entry.version, installed: entry.installed_version })}${turnedOff}`;
      if (!canAdminister) {
        return { kind: "update", disabled: true, note: `${available} ${t("react.extensions.marketplace_final_only_admins_update", "Only administrators can update extensions.")}`, addedPermissions };
      }
      if (installBlockedReason) {
        return { kind: "update", disabled: true, note: `${available} ${installBlockedReason}`, addedPermissions };
      }
      return { kind: "update", disabled: false, note: available, addedPermissions };
    }
    default:
      return { kind: "open", note: "" };
  }
}

/* ---- URL ----------------------------------------------------------------- */

const PREFIX = "mk_";
/** The open extension's page in the Catalog tab. */
export const MARKETPLACE_ENTRY_PARAM = `${PREFIX}entry`;
const ENTRY_PARAM = MARKETPLACE_ENTRY_PARAM;
const FIND_PARAM = "find";

const URL_SPEC: CatalogUrlSpec<MarketplaceFacetKey, MarketplaceSort> = {
  prefix: PREFIX,
  facetKeys: MARKETPLACE_FACET_KEYS,
  sorts: ["name", "added"],
  defaultSort: "name",
};

export type MarketplaceUrlState = CatalogUrlState<MarketplaceFacetKey, MarketplaceSort> & {
  /** The extension whose page is open in place of the results. */
  entry: string;
};

export function readMarketplaceUrl(params: URLSearchParams): MarketplaceUrlState {
  return { ...readCatalogUrlState(params, URL_SPEC), entry: params.get(ENTRY_PARAM)?.trim() ?? "" };
}

/** Writes the browse state into a copy of `params`, leaving other keys alone. */
export function writeMarketplaceUrl(params: URLSearchParams, state: MarketplaceUrlState): URLSearchParams {
  const next = writeCatalogUrlState(params, state, URL_SPEC);
  if (state.entry) next.set(ENTRY_PARAM, state.entry);
  else next.delete(ENTRY_PARAM);
  return next;
}

/** Every Marketplace key gone, `find` too: what the Installed tab's URL keeps. */
export function withoutMarketplaceParams(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams();
  for (const [key, value] of params) {
    if (!key.startsWith(PREFIX) && key !== FIND_PARAM) next.append(key, value);
  }
  return next;
}

/** Where one extension's page is, for a link that opens in a new tab. */
export function entryPath(extensionId: string): string {
  return `/extensions?${ENTRY_PARAM}=${encodeURIComponent(extensionId)}#marketplace`;
}

/**
 * `?find=<id>`, the template catalog's link to one extension, opens that
 * extension's page on the whole catalog; an id the catalog does not list
 * leaves the catalog on screen with a line saying so.
 */
export function resolveFind(
  params: URLSearchParams,
  items: readonly ExtensionCatalogEntry[],
): { params: URLSearchParams; missing: string } | null {
  const id = params.get(FIND_PARAM)?.trim();
  if (id === undefined) return null;
  const next = withoutMarketplaceParams(params);
  const known = Boolean(id) && items.some((entry) => entry.extension_id === id);
  if (known) next.set(ENTRY_PARAM, id);
  return { params: next, missing: known ? "" : id };
}

/**
 * Who publishes an extension: the owner of the repository it is released
 * from, verified when the catalog reserves the id's namespace for that owner.
 */
export function publisherOf(entry: ExtensionCatalogEntry): { name: string; url: string; verified: boolean } | null {
  const match = /^https:\/\/github\.com\/([^/]+)/iu.exec(entry.repository_url ?? "");
  if (!match?.[1]) return null;
  return { name: match[1], url: `https://github.com/${match[1]}`, verified: entry.verification_status === "verified" };
}

/** The Install button's words for a phase of the install. */
/**
 * How an install or update started from the results ended, when it did not:
 * the server's reason, or that a newer release was published meanwhile.
 */
export function installNotice(state: ExtensionCatalogInstallState | undefined): { text: string; failed: boolean } | null {
  if (state?.phase !== "idle" || !state.status) return null;
  if (state.outcome === "failed") return { text: state.status, failed: true };
  if (state.outcome === "superseded") return { text: state.status, failed: false };
  return null;
}

export function installLabel(phase: ExtensionCatalogInstallState["phase"], t: I18nContextValue["t"] = fallbackContext.t): string {
  if (phase === "installing") return t("react.extensions.installing", "Installing…");
  // Not "Installing…": nothing is being installed until the operator approves.
  if (phase === "awaiting_approval") return t("react.extensions.awaiting_approval", "Awaiting approval");
  return t("react.extensions.install", "Install");
}

export function updateLabel(phase: ExtensionCatalogInstallState["phase"], version: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (phase === "installing") return t("react.extensions.updating", "Updating…");
  if (phase === "awaiting_approval") return t("react.extensions.awaiting_approval", "Awaiting approval");
  return t("react.extensions.update_to_version", "Update to v{value0}", { value0: version });
}
