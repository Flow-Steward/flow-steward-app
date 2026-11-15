import { useState, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";

import { useQuery } from "@tanstack/react-query";
import { Link, useInRouterContext, useLocation, useNavigate } from "react-router";

import { DsSelect } from "@/components/shared/design-system";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { executeExtensionQuery } from "@/pages/extensions/api";
import { gridLinkHref, withPageParameter } from "@/pages/extensions/extension-grid-links";
import { listFromUnknown, objectOrEmpty, stringFrom, valueAtPath } from "@/pages/extensions/extension-manifest-utils";
import { pageUrlParameters } from "@/pages/extensions/extension-page-connection";
import { normalizeComponentPayload } from "@/pages/extensions/payload";

type GridLinkSource = Readonly<{ extensionId: string; link: unknown; row: Record<string, unknown> }>;

function RoutedGridCellLink({ children, extensionId, link, row }: GridLinkSource & Readonly<{ children: ReactNode }>) {
  const { search } = useLocation();
  const href = gridLinkHref(link, row, { extensionId, search });
  // A row that cannot fill the link shows its text, never a broken address.
  return href ? <Link className="fs-link" to={href}>{children}</Link> : <>{children}</>;
}

/** A grid cell that links to another page of the extension or to an in-app path. */
export function GridCellLink(props: GridLinkSource & Readonly<{ children: ReactNode }>) {
  return useInRouterContext() ? <RoutedGridCellLink {...props} /> : <>{props.children}</>;
}

function RoutedGridRow({ children, extensionId, link, row }: GridLinkSource & Readonly<{ children: ReactNode }>) {
  const { search } = useLocation();
  const navigate = useNavigate();
  const href = gridLinkHref(link, row, { extensionId, search });
  if (!href) return <tr>{children}</tr>;
  const open = (event: ReactMouseEvent<HTMLTableRowElement>) => {
    // A link, button or field inside the row keeps its own click.
    if ((event.target as HTMLElement).closest("a, button, input, select, textarea, [role='combobox']")) return;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    ignoreAsyncResult(navigate(href));
  };
  return <tr className="fs-manifest-row-link" data-row-href={href} onClick={open}>{children}</tr>;
}

/** A grid row; with `row_link` a click anywhere outside its controls opens the row's target. */
export function GridRow(props: Readonly<{ children: ReactNode; extensionId: string; link: unknown; row: Record<string, unknown> }>) {
  const inRouter = useInRouterContext();
  if (!props.link || !inRouter) return <tr>{props.children}</tr>;
  return <RoutedGridRow {...props} />;
}

type FilterOption = { value: string; label: string };

function optionsFromQuery(payload: unknown, source: Record<string, unknown>): FilterOption[] {
  const normalized = normalizeComponentPayload(payload);
  const result = objectOrEmpty(normalized.result || normalized);
  const valueField = stringFrom(source.value_field);
  const labelField = stringFrom(source.label_field) || valueField;
  return listFromUnknown(result.items)
    .map((item) => ({ value: stringFrom(valueAtPath(item, valueField)), label: stringFrom(valueAtPath(item, labelField)) }))
    .filter((option) => option.value)
    .map((option) => ({ ...option, label: option.label || option.value }));
}

function GridSelectFilter({
  extensionId,
  filter,
  onChange,
  projectId,
  t,
  value,
}: Readonly<{
  extensionId: string;
  filter: Record<string, unknown>;
  onChange: (value: string) => void;
  projectId: string;
  t: (key: string, fallback: string) => string;
  value: string;
}>) {
  const source = objectOrEmpty(filter.options_query);
  const queryId = stringFrom(source.query_id);
  const optionsQuery = useQuery({
    queryKey: ["extensions", extensionId, "grid-filter-options", queryId, projectId],
    queryFn: () => executeExtensionQuery(extensionId, queryId, {
      context: projectId ? { project_id: projectId } : {},
      params: {},
    }),
    enabled: Boolean(queryId && extensionId),
    retry: false,
    staleTime: 60_000,
  });
  const declared = listFromUnknown(filter.options).map((option) => ({ value: stringFrom(option.value), label: stringFrom(option.label) }));
  const loaded = queryId && optionsQuery.data !== undefined ? optionsFromQuery(optionsQuery.data, source) : [];
  const label = stringFrom(filter.label, stringFrom(filter.param));
  const anyLabel = stringFrom(filter.placeholder) || t("react.extensions.grid_filter_any", "Any");
  return (
    <label className="fs-manifest-filter">
      <span className="fs-manifest-filter-label">{label}</span>
      <DsSelect
        ariaLabel={label}
        disabled={Boolean(queryId && optionsQuery.isPending)}
        onChange={onChange}
        options={[{ value: "", label: anyLabel }, ...(queryId ? loaded : declared)]}
        placeholder={anyLabel}
        value={value}
      />
    </label>
  );
}

function GridTextFilter({ filter, onChange, value }: Readonly<{ filter: Record<string, unknown>; onChange: (value: string) => void; value: string }>) {
  const [draft, setDraft] = useState<string | null>(null);
  const label = stringFrom(filter.label, stringFrom(filter.param));
  const commit = () => {
    if (draft !== null && draft.trim() !== value) onChange(draft.trim());
    setDraft(null);
  };
  return (
    <label className="fs-manifest-filter">
      <span className="fs-manifest-filter-label">{label}</span>
      <input
        aria-label={label}
        className="fs-input"
        onBlur={commit}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); commit(); } }}
        placeholder={stringFrom(filter.placeholder)}
        type="search"
        value={draft ?? value}
      />
    </label>
  );
}

function RoutedGridFilters({
  extensionId,
  filters,
  onFiltered,
  projectId,
  t,
}: Readonly<{
  extensionId: string;
  filters: Array<Record<string, unknown>>;
  onFiltered: () => void;
  projectId: string;
  t: (key: string, fallback: string) => string;
}>) {
  const location = useLocation();
  const navigate = useNavigate();
  const current = pageUrlParameters(location.search);
  const change = (param: string, value: string) => {
    // The address holds the filter; the page reloads its queries with the new params.
    onFiltered();
    ignoreAsyncResult(navigate({ pathname: location.pathname, search: withPageParameter(location.search, param, value), hash: location.hash }, { replace: true }));
  };
  return (
    <div className="fs-manifest-filters" role="group" aria-label={t("react.extensions.grid_filters", "Filters")}>
      {filters.map((filter) => {
        const param = stringFrom(filter.param);
        if (!param) return null;
        const value = current[param] || "";
        return stringFrom(filter.type).toLowerCase() === "text"
          ? <GridTextFilter filter={filter} key={param} onChange={(next) => change(param, next)} value={value} />
          : <GridSelectFilter extensionId={extensionId} filter={filter} key={param} onChange={(next) => change(param, next)} projectId={projectId} t={t} value={value} />;
      })}
    </div>
  );
}

/** The filters a grid declares, each bound to one `p.<name>` page parameter in the address. */
export function GridFilters(props: Readonly<{
  extensionId: string;
  filters: Array<Record<string, unknown>>;
  onFiltered: () => void;
  projectId: string;
  t: (key: string, fallback: string) => string;
}>) {
  const inRouter = useInRouterContext();
  if (!inRouter || !props.filters.length) return null;
  return <RoutedGridFilters {...props} />;
}
