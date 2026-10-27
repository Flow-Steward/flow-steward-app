import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/*
 * The browse logic of the workflow template catalog: facets, search, sorting,
 * the one badge a card may carry, paging, and the URL the selection lives in.
 * Pure functions over the loaded index, so the dialog stays a view and every
 * rule here is tested without rendering anything. The facet engine and the
 * URL spelling are the static catalogs' shared ones; this module brings the
 * template vocabulary.
 */
import {
  type CatalogFacet,
  type CatalogFacetDef,
  type CatalogFilterState,
  type CatalogUrlSpec,
  type CatalogUrlState as SharedCatalogUrlState,
  catalogTextMatches,
  compareAddedNewestFirst,
  compareVersions,
  computeCatalogFacets,
  filterCatalogItems,
  readCatalogUrlState,
  sortCatalogItems,
  writeCatalogUrlState,
} from "@/lib/utils/catalog-facets";
import { Clock, Hand, Webhook, Workflow, Zap } from "lucide-react";

import type { CatalogPill } from "@/components/shared/catalog-browse";
import type { WorkflowCatalogItem } from "@/pages/workflows/catalog/api";

export { compareVersions, plural, shownFacetValues } from "@/lib/utils/catalog-facets";
export type { CatalogView } from "@/lib/utils/catalog-facets";

export type FacetKey = "category" | "integration" | "trigger" | "requires";

export const FACET_KEYS: readonly FacetKey[] = ["category", "integration", "trigger", "requires"];

export type CatalogFilters = CatalogFilterState<FacetKey>;

export const EMPTY_FILTERS: CatalogFilters = {
  q: "",
  category: [],
  integration: [],
  trigger: [],
  requires: [],
};

export type CatalogSort = "name" | "added";

export const PAGE_SIZE = 24;
/** A template is New for its first 30 days in the catalog. */
export const NEW_FOR_MS = 30 * 24 * 60 * 60 * 1000;

export const SUGGEST_TEMPLATE_URL = "https://github.com/Flow-Steward/workflows";

/** Where Marketplace shows one extension, so it can be installed or updated in a new tab. */
export function marketplaceHref(extensionId: string): string {
  return `/extensions?find=${encodeURIComponent(extensionId)}#marketplace`;
}

export function triggerLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
    manual: t("react.workflows.catalog_trigger_manual", "Manual"),
    schedule: t("react.workflows.catalog_trigger_schedule", "Schedule"),
    webhook: t("react.workflows.catalog_trigger_webhook", "Webhook"),
    sub_workflow: t("react.workflows.catalog_trigger_parent", "Called by another workflow"),
    event: t("react.workflows.catalog_trigger_event", "Event"),
  };
}

export const TRIGGER_LABELS: Record<string, string> = triggerLabels();

/** The glyph beside each trigger, on a card, a row and the template's page. */
export const TRIGGER_ICONS: Record<string, typeof Clock> = {
  manual: Hand,
  schedule: Clock,
  webhook: Webhook,
  sub_workflow: Workflow,
  event: Zap,
};


export function requirementLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
    extension: t("react.workflows.catalog_requires_extension", "Extension"),
    connection: t("react.workflows.catalog_requires_connection", "Connection"),
    llm: t("react.workflows.catalog_requires_model", "AI model"),
    tool: t("react.workflows.catalog_requires_tool", "Tool"),
    workflow: t("react.workflows.catalog_requires_workflow", "Child workflow"),
    none: t("react.workflows.catalog_requires_none", "Nothing to set up"),
  };
}

export const REQUIRES_LABELS: Record<string, string> = requirementLabels();

const TRIGGER_ORDER = Object.keys(TRIGGER_LABELS);
const REQUIRES_ORDER = Object.keys(REQUIRES_LABELS);

export type CatalogLabels = {
  categories: Record<string, string>;
  integrations: Record<string, string>;
};

export function humanize(id: string): string {
  const text = id.replaceAll(/[_-]+/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function integrationName(labels: CatalogLabels, id: string): string {
  return labels.integrations[id] || id;
}

export function categoryName(labels: CatalogLabels, id: string): string {
  return labels.categories[id] || id;
}

export function triggerName(id: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  return triggerLabels(t)[id] || humanize(id);
}

/** What a template asks to have set up, by kind; "none" when it asks for nothing. */
export function requirementKinds(item: WorkflowCatalogItem): string[] {
  const kinds = [...new Set(item.setup_requirements.map((entry) => entry.kind).filter(Boolean))];
  return kinds.length ? kinds : ["none"];
}

export type FacetDef = CatalogFacetDef<WorkflowCatalogItem, FacetKey>;

export function facetDefs(labels: CatalogLabels, t: I18nContextValue["t"] = fallbackContext.t): FacetDef[] {
  return [
    {
      key: "category",
      title: t("trash.columns.category", "Category"),
      noun: t("react.workflows.catalog_categories", "categories"),
      order: "total",
      valuesOf: (item) => item.categories,
      label: (value) => categoryName(labels, value),
    },
    {
      key: "integration",
      title: t("react.workflows.integration", "Integration"),
      noun: t("react.workflows.catalog_integrations", "integrations"),
      order: "total",
      valuesOf: (item) => item.integrations,
      label: (value) => integrationName(labels, value),
    },
    {
      key: "trigger",
      title: t("react.workflows.trigger", "Trigger"),
      noun: t("react.workflows.triggers", "triggers"),
      order: "fixed",
      fixed: TRIGGER_ORDER,
      valuesOf: (item) => item.trigger_types,
      label: (value) => triggerName(value, t),
    },
    {
      key: "requires",
      title: t("react.workflows.requires", "Requires"),
      noun: t("react.workflows.catalog_requirements", "requirements"),
      order: "fixed",
      fixed: REQUIRES_ORDER,
      valuesOf: requirementKinds,
      label: (value) => requirementLabels(t)[value] || humanize(value),
    },
  ];
}

export type Facet = CatalogFacet<WorkflowCatalogItem, FacetKey>;

function searchMatcher(labels: CatalogLabels) {
  return (item: WorkflowCatalogItem, needle: string) =>
    catalogTextMatches(
      [
        item.name,
        item.summary,
        item.description ?? "",
        item.item_id,
        ...item.tags,
        ...item.integrations,
        ...item.integrations.map((id) => integrationName(labels, id)),
      ],
      needle,
    );
}

/**
 * Each facet with its values and live counts. The order of values comes from
 * the whole catalog, so it never moves while filtering; a count is how many
 * templates the value would show with the other facets and the search applied,
 * and a zero dims in place. A facet with fewer than two values cannot narrow
 * anything and is left out, unless it is already in use.
 */
export function computeFacets(
  items: readonly WorkflowCatalogItem[],
  filters: CatalogFilters,
  labels: CatalogLabels, t: I18nContextValue["t"] = fallbackContext.t
): Facet[] {
  return computeCatalogFacets(items, filters, facetDefs(labels, t), searchMatcher(labels));
}

/**
 * The sorts the loaded index supports: A–Z, and Recently added when the index
 * carries dates. A single template, or a single option, is no choice.
 */
export function sortOptions(
  items: readonly WorkflowCatalogItem[], t: I18nContextValue["t"] = fallbackContext.t
): Array<{ value: CatalogSort; label: string }> {
  if (items.length < 2 || !items.some((item) => item.added_at)) return [];
  return [
    { value: "name", label: "A–Z" },
    { value: "added", label: t("react.workflows.recently_added", "Recently added") },
  ];
}

const COMPARE: Record<CatalogSort, (a: WorkflowCatalogItem, b: WorkflowCatalogItem) => number> = {
  name: (a, b) => a.name.localeCompare(b.name),
  added: (a, b) => compareAddedNewestFirst(a.added_at, b.added_at),
};

/** The templates the search and facets leave, sorted; deprecated ones come last. */
export function filterItems(
  items: readonly WorkflowCatalogItem[],
  filters: CatalogFilters,
  labels: CatalogLabels,
  sort: CatalogSort, t: I18nContextValue["t"] = fallbackContext.t
): WorkflowCatalogItem[] {
  const matched = filterCatalogItems(items, filters, facetDefs(labels, t), searchMatcher(labels));
  return sortCatalogItems(matched, COMPARE[sort] ?? COMPARE.name);
}

export type CardBadge = { tone: "orange" | "new"; label: string };

/** Extensions a template needs that this installation does not have. */
export function missingExtensions(
  item: WorkflowCatalogItem,
  installed: ReadonlySet<string> | null,
): string[] {
  if (!installed) return [];
  return item.setup_requirements
    .filter((entry) => entry.kind === "extension" && entry.ref && !installed.has(entry.ref))
    .map((entry) => entry.ref as string);
}

/**
 * Why this Core cannot import the template file at all, or "" when it can. A
 * template whose index entry names no bundle format counts as unknown.
 */
export function unavailableReason(item: WorkflowCatalogItem, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (item.importable !== false) return "";
  const schema = (item.bundle_schema || "").trim();
  return schema
    ? t("react.workflows.catalog_bundle_schema_unsupported", "This template is in a workflow format this installation cannot import ({schema}).", { schema })
    : t("react.workflows.catalog_bundle_schema_unknown", "This template does not say which workflow format it uses, so this installation cannot import it.");
}

/** At most one badge: Unavailable, Deprecated, then a missing extension, then New. */
export function cardBadge(
  item: WorkflowCatalogItem,
  installed: ReadonlySet<string> | null,
  now: number, t: I18nContextValue["t"] = fallbackContext.t
): CardBadge | null {
  if (unavailableReason(item, t)) return { tone: "orange", label: t("react.workflows.catalog_unavailable", "Unavailable") };
  if (item.publication_status === "deprecated") return { tone: "orange", label: t("react.workflows.deprecated", "Deprecated") };
  if (missingExtensions(item, installed).length) return { tone: "orange", label: t("react.workflows.needs_extension", "Needs extension") };
  const added = item.added_at ? Date.parse(item.added_at) : Number.NaN;
  if (Number.isFinite(added) && now >= added && now - added < NEW_FOR_MS) {
    return { tone: "new", label: t("react.workflows.new", "New") };
  }
  return null;
}

/** The one badge a template wears, as a pill: Deprecated, Needs extension, Unavailable carry the alert. */
export function templatePill(item: WorkflowCatalogItem, context: { installed: ReadonlySet<string> | null; now: number }, t: I18nContextValue["t"]): CatalogPill | null {
  const badge = cardBadge(item, context.installed, context.now, t);
  if (!badge) return null;
  return badge.tone === "orange" ? { tone: "orange", label: badge.label, icon: "alert" } : { tone: badge.tone, label: badge.label };
}

/** Why "Use template" is off on this installation, or "" when it is not. */
export function incompatibleReason(item: WorkflowCatalogItem, installationVersion: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const unavailable = unavailableReason(item, t);
  if (unavailable) return unavailable;
  const min = item.compatible_flow_steward?.min || "";
  if (min && compareVersions(installationVersion, min) === -1) {
    return t("react.workflows.this_template_needs_flow_steward_value0_or_later_this_installation_runs_value1", "This template needs Flow Steward {value0} or later; this installation runs {value1}.", { value0: min, value1: installationVersion });
  }
  const max = item.compatible_flow_steward?.max || "";
  if (max && compareVersions(installationVersion, max) === 1) {
    return t("react.workflows.this_template_works_up_to_flow_steward_value0_this_installation_runs_value1", "This template works up to Flow Steward {value0}; this installation runs {value1}.", { value0: max, value1: installationVersion });
  }
  return "";
}

/**
 * The first `base_N` (N from 2) that is neither an existing workflow nor
 * already promised to another workflow of the same template.
 */
export function freeWorkflowId(base: string, taken: ReadonlySet<string>): string {
  for (let n = 2; n < 10_000; n += 1) {
    const candidate = `${base}_${n}`;
    if (!taken.has(candidate)) return candidate;
  }
  return `${base}_copy`;
}

/* ---- URL ---------------------------------------------------------------- */

export type CatalogUrlState = SharedCatalogUrlState<FacetKey, CatalogSort>;

const URL_SPEC: CatalogUrlSpec<FacetKey, CatalogSort> = {
  prefix: "tpl_",
  facetKeys: FACET_KEYS,
  sorts: ["name", "added"],
  defaultSort: "name",
};

export function readCatalogUrl(params: URLSearchParams): CatalogUrlState {
  return readCatalogUrlState(params, URL_SPEC);
}

/** Writes the browse state into a copy of `params`, leaving other keys alone. */
export function writeCatalogUrl(params: URLSearchParams, state: CatalogUrlState): URLSearchParams {
  return writeCatalogUrlState(params, state, URL_SPEC);
}
