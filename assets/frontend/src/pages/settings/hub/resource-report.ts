import { resolveLinkHref } from "@/components/shared/data-grid-adapter";
import { isHttpError } from "@/lib/http/errors";
import { appHref } from "@/lib/routing/app-href";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";
import { normalizeChoiceOptions, type ChoiceOption } from "@/pages/settings/hub/choice-options";

/**
 * The `resource_report` declaration as the host renderer reads it (ADR 0082,
 * section 6): a read-only, paged, filterable table a platform module declares
 * without shipping frontend code. The server validates the declaration at
 * registration; everything here is checked again before it reaches a request
 * or a link.
 */

export type ReportRow = Record<string, unknown>;

export type ResourceReportColumn = {
  key: string;
  label: string;
  /** A row field: when it is falsy the row's label is plain text, not a link. */
  linkIf: string;
  linkKey: string;
  /** Muted text beside the plain label when `linkIf` turns the link off. */
  linkMissingLabel: string;
  linkPath: string;
  type: "badge" | "datetime" | "link" | "text";
};

export type ResourceReportFilter = {
  key: string;
  label: string;
  options: ChoiceOption[];
  /** A select whose choices are records: loaded as `[{value, label}]` from here. */
  optionsPath: string;
  type: "date" | "select" | "text";
};

export type ResourceReportSurface = {
  columns: ResourceReportColumn[];
  description: string;
  emptyText: string;
  filters: ResourceReportFilter[];
  itemsKey: string;
  kind: "resource_report";
  listPath: string;
  pageSize: number;
  totalKey: string;
};

const COLUMN_TYPES = new Set(["badge", "datetime", "link", "text"]);
const FILTER_TYPES = new Set(["date", "select", "text"]);
const PAGING_KEYS = new Set(["limit", "offset"]);
const REPORT_KEY = /^[A-Za-z_]\w*$/;
const PATH_TOKEN = /\{[A-Za-z_]\w*\}/g;
/** A declared link template: app paths only, braces only as `{name}` tokens. */
const IN_APP_TEMPLATE = /^\/[\w\-./~{}]*$/;
/** A link after row values are filled in (they arrive percent-encoded). */
const IN_APP_RESOLVED = /^\/[\w\-./~%]*$/;
const MAX_PAGE_SIZE = 500;

// Resource requests carry app cookies and CSRF headers, so they must stay on
// same-origin API paths. The server validates this at registration; this is
// the defence-in-depth check.
export function isSameOriginApiPath(path: string): boolean {
  return path.startsWith("/api/") && !path.startsWith("//") && !path.includes("..") && !path.includes("://");
}

/**
 * Whether `path` is a page inside this app (`/jobs/42`), never another origin,
 * a scheme, the API, a parent segment, a query or a fragment.
 */
export function isInAppLinkPath(path: string, { template = false }: { template?: boolean } = {}): boolean {
  const pattern = template ? IN_APP_TEMPLATE : IN_APP_RESOLVED;
  if (!pattern.test(path) || path.startsWith("//") || path.includes("..") || path === "/api" || path.startsWith("/api/")) {
    return false;
  }
  const rest = template ? path.replaceAll(PATH_TOKEN, "") : path;
  return !rest.includes("{") && !rest.includes("}");
}

export function reportText(value: unknown): string {
  return firstStringFromUnknownValue([value, ""]).trim();
}

export function reportMapping(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function normalizeColumn(value: unknown): ResourceReportColumn | null {
  const record = reportMapping(value);
  if (!record) return null;
  const key = reportText(record.key);
  const label = reportText(record.label) || key;
  const type = reportText(record.type) || "text";
  if (!REPORT_KEY.test(key) || !COLUMN_TYPES.has(type)) return null;
  const linkPath = reportText(record.link_path);
  const linkKey = reportText(record.link_key);
  const linkIf = type === "link" ? reportText(record.link_if) : "";
  const linkMissingLabel = linkIf ? reportText(record.link_missing_label) : "";
  if (type === "link") {
    if (linkPath && !isInAppLinkPath(linkPath, { template: true })) return null;
    if (linkKey && !REPORT_KEY.test(linkKey)) return null;
    if (linkIf && !REPORT_KEY.test(linkIf)) return null;
    if (!linkPath && !linkKey) return null;
  }
  return { key, label, linkIf, linkKey, linkMissingLabel, linkPath, type: type as ResourceReportColumn["type"] };
}

function normalizeFilter(value: unknown): ResourceReportFilter | null {
  const record = reportMapping(value);
  if (!record) return null;
  const key = reportText(record.key);
  const label = reportText(record.label) || key;
  const type = reportText(record.type) || "text";
  if (!REPORT_KEY.test(key) || PAGING_KEYS.has(key) || !FILTER_TYPES.has(type)) return null;
  const options = normalizeChoiceOptions(record.options);
  const optionsPath = type === "select" ? reportText(record.options_path) : "";
  if (optionsPath && !isSameOriginApiPath(optionsPath)) return null;
  if (type === "select" && options.length === 0 && !optionsPath) return null;
  return { key, label, options, optionsPath, type: type as ResourceReportFilter["type"] };
}

export function normalizeResourceReportSurface(value: unknown): ResourceReportSurface | undefined {
  const record = reportMapping(value);
  if (record?.kind !== "resource_report") return undefined;
  const listPath = reportText(record.list_path);
  // The renderer appends the query itself, so the declared path carries none.
  if (!isSameOriginApiPath(listPath) || listPath.includes("?") || listPath.includes("#")) return undefined;
  const columns = Array.isArray(record.columns) ? record.columns.map(normalizeColumn).filter(isTruthy) : [];
  if (columns.length === 0) return undefined;
  const filters = Array.isArray(record.filters) ? record.filters.map(normalizeFilter).filter(isTruthy) : [];
  const declaredSize = Number(record.page_size);
  const pageSize = Number.isInteger(declaredSize) && declaredSize >= 1 && declaredSize <= MAX_PAGE_SIZE ? declaredSize : 50;
  const itemsKey = reportText(record.items_key) || "items";
  const totalKey = reportText(record.total_key) || "total";
  return {
    columns,
    description: reportText(record.description),
    emptyText: reportText(record.empty_text),
    filters,
    itemsKey,
    kind: "resource_report",
    listPath,
    pageSize,
    totalKey,
  };
}

/** A 403 the server gives when the account is not entitled to the page's feature. */
export function isFeatureNotEntitled(error: unknown): boolean {
  if (!isHttpError(error) || error.status !== 403) return false;
  const payload: Record<string, unknown> = reportMapping(error.payload) ?? {};
  return [payload.code, reportMapping(payload.detail)?.code, reportMapping(payload.error)?.code].includes("feature_not_entitled");
}

/** Whether the row turns the column's link off through `link_if` (its target is gone). */
export function reportLinkMissing(column: ResourceReportColumn, row: ReportRow): boolean {
  return Boolean(column.linkIf) && !row[column.linkIf];
}

/** The in-app href of a link cell, or null when the row would lead anywhere else. */
export function reportLinkHref(column: ResourceReportColumn, row: ReportRow): string | null {
  if (reportLinkMissing(column, row)) return null;
  let path = "";
  if (column.linkPath) {
    if (!isInAppLinkPath(column.linkPath, { template: true })) return null;
    const tokens = column.linkPath.match(PATH_TOKEN) ?? [];
    // A link to a job without a job id would open the list, not the job.
    if (tokens.some((token) => !reportText(row[token.slice(1, -1)]))) return null;
    path = resolveLinkHref(column.linkPath, row);
  } else {
    path = reportText(row[column.linkKey]);
  }
  return isInAppLinkPath(path) ? appHref(path) : null;
}

export function reportRequestPath(
  surface: ResourceReportSurface,
  filters: Readonly<Record<string, string>>,
  page: number,
): string {
  const params = new URLSearchParams();
  for (const item of surface.filters) {
    const value = (filters[item.key] || "").trim();
    if (value) params.set(item.key, value);
  }
  params.set("limit", String(surface.pageSize));
  params.set("offset", String((page - 1) * surface.pageSize));
  return `${surface.listPath}?${params.toString()}`;
}
