import { useI18n } from "@/lib/i18n/useI18n";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import {
  AdvancedTableFilters,
  DsAlert,
  DsCard,
  DsTable,
  DsTableLink,
  Pagination,
  type DsFilterField,
  type DsTableColumn,
} from "@/components/shared/design-system";
import { columnsFromGridConfig, type CatalogColumnConfig } from "@/components/shared/data-grid-adapter";
import { DsLoader } from "@/components/shared/ds-loader";
import { PagePlaceholder } from "@/components/shared/page-placeholder";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { fetchJson } from "@/lib/http/client";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { isTruthy } from "@/lib/utils/unknown-values";
import { choiceOptionsFromPayload, type ChoiceOption } from "@/pages/settings/hub/choice-options";
import {
  isFeatureNotEntitled,
  reportLinkHref,
  reportLinkMissing,
  reportMapping,
  reportRequestPath,
  reportText,
  type ReportRow,
  type ResourceReportSurface,
} from "@/pages/settings/hub/resource-report";

/**
 * Host renderer for `control_surface.kind = "resource_report"` (ADR 0082, section 6):
 * a read-only, paged, filterable table a platform module declares without shipping
 * frontend code. The server validates the declaration at registration; everything
 * here is checked again before it reaches a request or a link.
 */

/** The state a registered page shows when its feature is not available to the account. */
export function RegisteredPageUnavailable({ description }: Readonly<{ description: string }>) {
  const { t } = useI18n();
  return (
    <PagePlaceholder
      eyebrow={t("react.routing.page_unavailable", "Page unavailable")}
      title={t("react.routing.page_not_available", "This page is not available.")}
      description={description}
      status={t("react.routing.unavailable", "Unavailable")}
    />
  );
}

function reportColumns(
  surface: ResourceReportSurface,
  timezone: string,
): Array<DsTableColumn<ReportRow>> {
  const configs: CatalogColumnConfig[] = surface.columns.map((column) => ({
    key: column.key,
    label: column.label,
    type: column.type === "badge" ? "badge" : "text",
    truncate: column.type === "text",
  }));
  return columnsFromGridConfig({ columns: configs }, () => null).map((tableColumn, index) => {
    const column = surface.columns[index];
    if (column?.type === "datetime") {
      return { ...tableColumn, cell: (row: ReportRow) => formatUserDateTime(row[column.key], timezone) };
    }
    if (column?.type === "link") {
      return {
        ...tableColumn,
        cell: (row: ReportRow) => {
          const label = reportText(row[column.key]);
          const href = reportLinkHref(column, row);
          if (href && label) return <DsTableLink href={href}>{label}</DsTableLink>;
          if (column.linkMissingLabel && reportLinkMissing(column, row)) {
            return (
              <span>
                {label ? `${label} ` : null}
                <span className="fs-ink-tertiary">{column.linkMissingLabel}</span>
              </span>
            );
          }
          return label || null;
        },
      };
    }
    return tableColumn;
  });
}

function filterFields(surface: ResourceReportSurface, loadedOptions: Record<string, ChoiceOption[]>): DsFilterField[] {
  return surface.filters.map((item) => {
    if (item.type === "select") {
      return { key: item.key, label: item.label, options: item.optionsPath ? loadedOptions[item.optionsPath] ?? [] : item.options };
    }
    return { key: item.key, label: item.label, options: [], input: item.type };
  });
}

export function ResourceReportPanel({
  surface,
  title,
}: Readonly<{
  surface: ResourceReportSurface;
  title: string;
}>) {
  const { t } = useI18n();
  const timezone = useUserTimezone();
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<ReportRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  /** The server's reason when the account may not see this page; null while it may. */
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  const requestPath = reportRequestPath(surface, filters, page);

  const load = useCallback(
    async (isCurrent: () => boolean) => {
      setLoading(true);
      setMessage("");
      try {
        const payload = await fetchJson<Record<string, unknown>>(requestPath);
        if (!isCurrent()) return;
        const items = Array.isArray(payload?.[surface.itemsKey]) ? (payload[surface.itemsKey] as unknown[]) : [];
        const nextRows = items.map(reportMapping).filter(isTruthy);
        const declaredTotal = Number(payload?.[surface.totalKey]);
        setRows(nextRows);
        setTotal(Number.isFinite(declaredTotal) && declaredTotal >= 0 ? declaredTotal : (page - 1) * surface.pageSize + nextRows.length);
        setUnavailable(null);
      } catch (error) {
        if (!isCurrent()) return;
        setRows([]);
        setTotal(0);
        if (isFeatureNotEntitled(error)) {
          setUnavailable(error instanceof Error ? error.message : "");
        } else {
          setMessage(error instanceof Error ? error.message : t("react.settings.failed_to_load_this_settings_area", "Failed to load this settings area."));
        }
      } finally {
        if (isCurrent()) setLoading(false);
      }
    },
    [page, requestPath, surface.itemsKey, surface.pageSize, surface.totalKey, t],
  );

  useEffect(() => {
    let current = true;
    ignoreAsyncResult(load(() => current));
    return () => {
      current = false;
    };
  }, [load, reloads]);

  const columns = useMemo(() => reportColumns(surface, timezone), [surface, timezone]);
  const [loadedOptions, setLoadedOptions] = useState<Record<string, ChoiceOption[]>>({});
  useEffect(() => {
    // Filters that choose a record (a workflow, a person) load their choices once.
    let current = true;
    const paths = [...new Set(surface.filters.map((item) => item.optionsPath).filter(isTruthy))];
    ignoreAsyncResult(
      Promise.all(
        paths.map(async (path) => {
          try {
            return [path, choiceOptionsFromPayload(await fetchJson<unknown>(path))] as const;
          } catch {
            return null;
          }
        }),
      ).then((entries) => {
        if (current) setLoadedOptions(Object.fromEntries(entries.filter(isTruthy)));
      }),
    );
    return () => {
      current = false;
    };
  }, [surface.filters]);
  const fields = useMemo(() => filterFields(surface, loadedOptions), [surface, loadedOptions]);
  const rowKeys = useMemo(() => {
    const keys = new WeakMap<ReportRow, string>();
    rows.forEach((row, index) => keys.set(row, reportText(row.id) || `row-${(page - 1) * surface.pageSize + index}`));
    return keys;
  }, [page, rows, surface.pageSize]);
  const loaderShown = useLoaderDelay(loading && !rows.length);

  if (unavailable !== null) {
    return <RegisteredPageUnavailable description={unavailable} />;
  }

  let tableState: ReactNode = null;
  if (loaderShown) {
    tableState = <div className="fs-table-state"><DsLoader variant="wide-block" /></div>;
  } else if (!loading && !rows.length && !message) {
    tableState = (
      <div className="fs-table-state">
        <strong>{surface.emptyText || t("react.settings.no_records_configured", "No records are configured.")}</strong>
      </div>
    );
  }

  return (
    <>
      <DsCard description={surface.description || undefined} title={title}>
        <div className="fs-panel-heading">
          <p className="fs-ink-secondary">{total === 1
            ? t("react.settings.records_count_one", "1 record")
            : t("react.settings.records_count", "{count} records", { count: total })}</p>
          <Button busy={loading} busyLabel={t("registry.index.reloading", "Reloading…")} onClick={() => setReloads((value) => value + 1)} type="button" variant="secondary">
            {t("account.tokens.refresh", "Refresh")} </Button>
        </div>
        {message ? <DsAlert title={message} tone="warning" /> : null}
      </DsCard>
      <article className="fs-card fs-table-card">
        {fields.length ? (
          <div className="fs-table-toolbar">
            <AdvancedTableFilters
              fields={fields}
              onChange={(key, value) => {
                setPage(1);
                setFilters((current) => ({ ...current, [key]: value }));
              }}
              onClear={() => {
                setPage(1);
                setFilters({});
              }}
              values={filters}
            />
          </div>
        ) : null}
        <DsTable
          ariaLabel={title}
          busy={loading}
          columns={columns}
          rowClickFocusable={false}
          rowKey={(row) => rowKeys.get(row) || ""}
          rows={rows}
          state={tableState}
        />
        <Pagination
          onPageChange={setPage}
          page={page}
          perPage={surface.pageSize}
          showPerPage={false}
          total={total}
        />
      </article>
    </>
  );
}
