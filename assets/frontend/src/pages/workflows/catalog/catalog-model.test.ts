import type { WorkflowCatalogItem } from "@/pages/workflows/catalog/api";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  cardBadge,
  computeFacets,
  EMPTY_FILTERS,
  filterItems,
  incompatibleReason,
  unavailableReason,
  marketplaceHref,
  NEW_FOR_MS,
  freeWorkflowId,
  readCatalogUrl,
  shownFacetValues,
  sortOptions,
  templatePill,
  writeCatalogUrl,
  type CatalogFilters,
  type CatalogLabels,
} from "@/pages/workflows/catalog/catalog-model";

const LABELS: CatalogLabels = {
  categories: { email: "Email", sales: "Sales" },
  integrations: { "fs.imap": "Generic IMAP Mailbox", "fs.shop": "Shopify" },
};

function item(overrides: Partial<WorkflowCatalogItem> & { item_id: string }): WorkflowCatalogItem {
  return {
    version: "1.0.0",
    name: overrides.item_id,
    summary: "",
    categories: [],
    tags: [],
    integrations: [],
    trigger_types: [],
    setup_requirements: [],
    workflow_yaml_url: "https://example.test/x.yaml",
    workflow_yaml_bytes: 1,
    workflow_yaml_sha256: "a".repeat(64),
    publication_status: "published",
    verification_status: "verified",
    ...overrides,
  };
}

const ITEMS = [
  item({ item_id: "a", name: "Alpha", categories: ["email"], integrations: ["fs.imap"], trigger_types: ["schedule"] }),
  item({ item_id: "b", name: "Bravo", categories: ["email"], integrations: ["fs.shop"], trigger_types: ["manual"] }),
  item({ item_id: "c", name: "Charlie", categories: ["sales"], integrations: ["fs.shop"], trigger_types: ["schedule"] }),
];

function filters(patch: Partial<CatalogFilters>): CatalogFilters {
  return { ...EMPTY_FILTERS, ...patch };
}

test("values of one facet are OR-ed and facets are AND-ed", () => {
  const either = filterItems(ITEMS, filters({ integration: ["fs.imap", "fs.shop"] }), LABELS, "name");
  expect(either.map((entry) => entry.item_id)).toEqual(["a", "b", "c"]);

  const both = filterItems(ITEMS, filters({ integration: ["fs.shop"], trigger: ["schedule"] }), LABELS, "name");
  expect(both.map((entry) => entry.item_id)).toEqual(["c"]);
});

test("localized catalog facets keep protocol values and filtering unchanged", () => {
  const messages: Record<string, string> = {
    "Manual": "Rankiniu būdu",
    "Schedule": "Tvarkaraštis",
    "Extension": "Plėtinys",
    "Nothing to set up": "Nieko nereikia nustatyti",
  };
  const t: I18nContextValue["t"] = (key, defaultText, params) =>
    fallbackContext.t(key, messages[defaultText || ""] || defaultText, params);
  const entries = [
    ITEMS[0],
    { ...ITEMS[1], setup_requirements: [{ kind: "extension", ref: "fs.shop", label: "Shopify" }] },
  ];
  const facets = computeFacets(entries, EMPTY_FILTERS, LABELS, t);
  expect(facets.find((facet) => facet.key === "trigger")?.values.map((value) => [value.value, value.label])).toEqual([
    ["manual", "Rankiniu būdu"],
    ["schedule", "Tvarkaraštis"],
  ]);
  expect(facets.find((facet) => facet.key === "requires")?.values.map((value) => [value.value, value.label])).toEqual([
    ["extension", "Plėtinys"],
    ["none", "Nieko nereikia nustatyti"],
  ]);
  expect(filterItems(entries, filters({ trigger: ["manual"] }), LABELS, "name", t).map((entry) => entry.item_id)).toEqual(["b"]);
});

test("search covers names, descriptions and integration names", () => {
  expect(filterItems(ITEMS, filters({ q: "imap mailbox" }), LABELS, "name").map((entry) => entry.item_id)).toEqual(["a"]);
  expect(filterItems(ITEMS, filters({ q: "bravo" }), LABELS, "name").map((entry) => entry.item_id)).toEqual(["b"]);
});

test("a facet's counts apply the other facets but not itself, and zero stays in place", () => {
  const facets = computeFacets(ITEMS, filters({ trigger: ["manual"] }), LABELS);
  const integration = facets.find((facet) => facet.key === "integration");
  // Most common first, from the whole catalog, so the order never moves.
  expect(integration?.values.map((value) => [value.label, value.count])).toEqual([
    ["Shopify", 1],
    ["Generic IMAP Mailbox", 0],
  ]);
  const trigger = facets.find((facet) => facet.key === "trigger");
  expect(trigger?.values.map((value) => [value.value, value.count])).toEqual([
    ["manual", 1],
    ["schedule", 2],
  ]);
});

test("a facet with one value is shown, so the rail keeps one layout; one with none is left out", () => {
  const single = [item({ item_id: "x", categories: ["email"], integrations: ["fs.imap"], trigger_types: ["manual"] })];
  expect(computeFacets(single, EMPTY_FILTERS, LABELS).map((facet) => facet.key)).toEqual(
    expect.arrayContaining(["category", "integration", "trigger"]),
  );

  const none = [item({ item_id: "y", categories: [], integrations: [], trigger_types: [] })];
  expect(computeFacets(none, EMPTY_FILTERS, LABELS).map((facet) => facet.key)).not.toContain("category");
});

test("category and integration names come from the labels, the id when missing", () => {
  const facets = computeFacets(
    [...ITEMS, item({ item_id: "d", categories: ["unlabelled"] })],
    EMPTY_FILTERS,
    LABELS,
  );
  const category = facets.find((facet) => facet.key === "category");
  expect(category?.values.map((value) => value.label)).toEqual(["Email", "Sales", "unlabelled"]);
});

test("a long facet shows six values, keeps chosen ones, and searches from eight", () => {
  const many = Array.from({ length: 9 }, (_, index) =>
    item({ item_id: `t${index}`, integrations: [`int_${index}`] }),
  );
  const facet = computeFacets(many, EMPTY_FILTERS, LABELS).find((entry) => entry.key === "integration");
  if (!facet) throw new Error("integration facet missing");

  const collapsed = shownFacetValues(facet, ["int_8"], "", false);
  expect(collapsed.searchable).toBe(true);
  expect(collapsed.shown.map((value) => value.value)).toEqual([
    "int_0", "int_1", "int_2", "int_3", "int_4", "int_5", "int_8",
  ]);
  expect(collapsed.hidden).toBe(2);

  expect(shownFacetValues(facet, [], "", true).shown).toHaveLength(9);
  expect(shownFacetValues(facet, [], "int_7", false).shown.map((value) => value.value)).toEqual(["int_7"]);

  const seven = computeFacets(many.slice(0, 7), EMPTY_FILTERS, LABELS).find((entry) => entry.key === "integration");
  if (!seven) throw new Error("integration facet missing");
  expect(shownFacetValues(seven, [], "", false).searchable).toBe(false);
});

test("sorts appear only when the data can support them", () => {
  expect(sortOptions([ITEMS[0]])).toEqual([]);
  // A–Z alone is no choice.
  expect(sortOptions(ITEMS)).toEqual([]);
  const dated = ITEMS.map((entry, index) => ({ ...entry, added_at: `2026-09-0${index + 1}T00:00:00Z` }));
  expect(sortOptions(dated).map((option) => option.label)).toEqual(["A–Z", "Recently added"]);
  // No Oldest, no Recently updated, however much the index dates.
  const updated = dated.map((entry) => ({ ...entry, updated_at: "2026-09-20T00:00:00Z" }));
  expect(sortOptions(updated).map((option) => option.value)).toEqual(["name", "added"]);
});

test("a template catalog's old sorts in the URL fall back to A–Z", () => {
  for (const sort of ["oldest", "newest", "updated"]) {
    expect(readCatalogUrl(new URLSearchParams(`tpl_sort=${sort}`)).sort).toBe("name");
  }
  expect(readCatalogUrl(new URLSearchParams("tpl_sort=added")).sort).toBe("added");
});

test("recently added sorts by date and deprecated templates always come last", () => {
  const dated = [
    { ...ITEMS[0], added_at: "2026-09-01T00:00:00Z" },
    { ...ITEMS[1], added_at: "2026-09-03T00:00:00Z", publication_status: "deprecated" },
    { ...ITEMS[2], added_at: "2026-09-02T00:00:00Z" },
  ];
  expect(filterItems(dated, EMPTY_FILTERS, LABELS, "added").map((entry) => entry.item_id)).toEqual(["c", "a", "b"]);
});

test("a card carries one badge: Deprecated, then Needs extension, then New", () => {
  const now = Date.parse("2026-10-01T00:00:00Z");
  const fresh = new Date(now - NEW_FOR_MS + 60_000).toISOString();
  const old = new Date(now - NEW_FOR_MS - 60_000).toISOString();
  const needs = { kind: "extension", label: "Shopify", ref: "fs.shop" };
  const installed = new Set(["fs.imap"]);

  const everything = item({
    item_id: "x",
    publication_status: "deprecated",
    setup_requirements: [needs],
    added_at: fresh,
  });
  expect(cardBadge(everything, installed, now)?.label).toBe("Deprecated");
  expect(cardBadge({ ...everything, publication_status: "published" }, installed, now)?.label).toBe("Needs extension");
  const newest = cardBadge({ ...everything, publication_status: "published", setup_requirements: [] }, installed, now);
  expect(newest?.label).toBe("New");
  // New is the Iridescent pill, not a neutral one.
  expect(newest?.tone).toBe("new");
  expect(templatePill({ ...everything, publication_status: "published", setup_requirements: [] }, { installed, now }, fallbackContext.t)).toEqual({ tone: "new", label: "New" });
  expect(cardBadge(item({ item_id: "y", added_at: old }), installed, now)).toBeNull();
  // Installed here: nothing missing.
  expect(cardBadge(item({ item_id: "z", setup_requirements: [{ ...needs, ref: "fs.imap" }] }), installed, now)).toBeNull();
  // Without the installed list nothing is claimed to be missing.
  expect(cardBadge(item({ item_id: "z", setup_requirements: [needs] }), null, now)).toBeNull();
});

test("an installation older than the template's minimum explains why it cannot use it", () => {
  const needsNewer = item({ item_id: "x", compatible_flow_steward: { min: "0.5.0" } });
  expect(incompatibleReason(needsNewer, "0.4.0")).toBe(
    "This template needs Flow Steward 0.5.0 or later; this installation runs 0.4.0.",
  );
  expect(incompatibleReason(needsNewer, "0.5.0")).toBe("");
  // An unknown installation version does not block.
  expect(incompatibleReason(needsNewer, "")).toBe("");
});

test("a suggested id is free among existing and promised ids", () => {
  expect(freeWorkflowId("triage", new Set())).toBe("triage_2");
  expect(freeWorkflowId("triage", new Set(["triage", "triage_2", "triage_3"]))).toBe("triage_4");
  expect(freeWorkflowId("report_2026", new Set(["report_2026"]))).toBe("report_2026_2");
});

test("Marketplace opens on the one extension", () => {
  expect(marketplaceHref("acme.store")).toBe("/extensions?find=acme.store#marketplace");
});

test("search, filters, sort, page and view round-trip through the URL", () => {
  const state = {
    filters: filters({ q: "supplier", integration: ["fs.imap", "fs.shop"], trigger: ["schedule"] }),
    sort: "added" as const,
    page: 3,
    view: "list" as const,
  };
  const params = writeCatalogUrl(new URLSearchParams("project_id=p1&create=template"), state);
  expect(params.get("project_id")).toBe("p1");
  expect(params.get("tpl_integration")).toBe("fs.imap,fs.shop");
  expect(readCatalogUrl(params)).toEqual(state);

  const cleared = writeCatalogUrl(params, { filters: EMPTY_FILTERS, sort: "name", page: 1, view: "grid" });
  expect([...cleared.keys()]).toEqual(["project_id", "create"]);
});

test("a template whose bundle format Core cannot import is unavailable, whatever else it says", () => {
  const v1 = item({ item_id: "v1", bundle_schema: "platform.workflow_bundle.v1", importable: false, publication_status: "deprecated" });
  const unnamed = item({ item_id: "unnamed", bundle_schema: "", importable: false });
  const v2 = item({ item_id: "v2", bundle_schema: "platform.workflow_bundle.v2", importable: true });

  expect(unavailableReason(v1)).toBe("This template is in a workflow format this installation cannot import (platform.workflow_bundle.v1).");
  expect(unavailableReason(unnamed)).toBe("This template does not say which workflow format it uses, so this installation cannot import it.");
  expect(unavailableReason(v2)).toBe("");
  expect(cardBadge(v1, null, Date.now())?.label).toBe("Unavailable");
  expect(incompatibleReason(unnamed, "0.7.16")).toBe(unavailableReason(unnamed));
  expect(incompatibleReason(v2, "0.7.16")).toBe("");
});
