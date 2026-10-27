import { useI18n } from "@/lib/i18n/useI18n";
import { useId, useMemo, type ReactNode } from "react";

import { Search, X } from "lucide-react";

import {
  DsAlert,
  DsTable,
  type DsBadgeTone,
  type DsTableColumn,
} from "@/components/shared/design-system";
import { LocalizedStatusBadge } from "@/components/shared/localized-status-badge";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { trimRepeatedEdges } from "@/lib/utils/string-normalization";

export type AssignmentSelectorBadgeTone = "linked" | "available" | "disabled" | "default" | "info";

export type AssignmentSelectorBadge = {
  label: string;
  tone?: AssignmentSelectorBadgeTone;
};

export type AssignmentSelectorRow = {
  id: string;
  title: string;
  subtitle?: string;
  details?: string[];
  /**
   * Prose about the row, as opposed to the short facts in `details`. Rendered on
   * its own clamped line: an MCP tool's description is a prompt — two thousand
   * characters is normal — and `details` lays its items out as inline wrapping
   * spans, so prose there grew one row past three hundred pixels and the next
   * one was cut mid-sentence by the scroller.
   */
  description?: string;
  badges?: AssignmentSelectorBadge[];
  disabled?: boolean;
  selected?: boolean;
  filterKeys?: string[];
  searchText?: string;
  rightMeta?: string;
  actionHref?: string;
  actionLabel?: string;
};

export type AssignmentSelectorFilter = {
  key: string;
  label: string;
  count: number;
};

type AssignmentSelectorProps = {
  rows: AssignmentSelectorRow[];
  filters?: AssignmentSelectorFilter[];
  activeFilter?: string;
  onFilterChange?: (filterKey: string) => void;
  searchValue: string;
  onSearchChange: (value: string) => void;
  searchPlaceholder: string;
  selectedCount: number;
  totalCount: number;
  saveLabel?: string;
  saveDisabled?: boolean;
  saving?: boolean;
  onSave?: () => void;
  onToggleRow: (rowId: string) => void;
  readOnly?: boolean;
  loading?: boolean;
  loadingLabel?: string;
  message?: ReactNode;
  renderRowAction?: (row: AssignmentSelectorRow) => ReactNode;
  emptyState: ReactNode;
  selectedLabel?: string;
  totalLabel?: string;
};

function badgeTone(tone: AssignmentSelectorBadgeTone | undefined): DsBadgeTone {
  switch (tone) {
    case "linked":
      return "green";
    case "disabled":
      return "orange";
    // `default` was the only state without a tone of its own once ink went;
    // its label already says "default", so neutral carries it.
    case "default":
      return "neutral";
    case "info":
    case "available":
    default:
      return "neutral";
  }
}

function assignmentItemCell(row: AssignmentSelectorRow) {
  return (
    <div className="min-w-0">
      <div className="font-medium fs-ink-primary">{row.title}</div>
      {row.subtitle ? <div className="font-mono text-xs fs-ink-tertiary">{row.subtitle}</div> : null}
      {row.details?.length ? (
        <div className="mt-0.5 flex flex-wrap gap-2 text-xs fs-ink-tertiary">
          {row.details.map((detail) => <span key={detail}>{detail}</span>)}
        </div>
      ) : null}
      {row.description ? (
        <p className="mt-1 line-clamp-2 text-xs fs-ink-tertiary" title={row.description}>
          {row.description}
        </p>
      ) : null}
    </div>
  );
}

function assignmentStatusCell(row: AssignmentSelectorRow) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      {(row.badges || []).map((badge) => (
        <LocalizedStatusBadge key={`${row.id}-${badge.label}`} tone={badgeTone(badge.tone)} value={badge.label} />
      ))}
      {row.rightMeta ? <span className="text-[11px] fs-ink-tertiary">{row.rightMeta}</span> : null}
    </div>
  );
}

export function AssignmentSelector({
  rows,
  filters = [],
  activeFilter = "all",
  onFilterChange,
  searchValue,
  onSearchChange,
  searchPlaceholder,
  selectedCount,
  totalCount,
  saveLabel,
  saveDisabled = false,
  saving = false,
  onSave,
  onToggleRow,
  emptyState,
  loading = false,
  loadingLabel: suppliedLoadingLabel,
  message = "",
  renderRowAction,
  readOnly = false,
  selectedLabel = "linked",
  totalLabel = "total",
}: Readonly<AssignmentSelectorProps>) {
  const { t } = useI18n();
  const loadingLabel = suppliedLoadingLabel ?? t("shell.search.loading_more", "Loading…");
  const countLabels: Record<string, string> = {
    linked: t("react.shared.assignment_linked", "linked"),
    total: t("react.shared.assignment_total", "total"),
    overrides: t("react.shared.assignment_overrides", "overrides"),
    catalog: t("react.shared.assignment_catalog", "catalog"),
  };
  const selectedDisplayLabel = countLabels[selectedLabel] ?? selectedLabel;
  const totalDisplayLabel = countLabels[totalLabel] ?? totalLabel;
  const inputNamePrefix = selectedLabel
    .trim()
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, "_");
  const normalizedInputNamePrefix = trimRepeatedEdges(inputNamePrefix, "_") || "assignment";
  const controlIdPrefix = `${normalizedInputNamePrefix}_${useId().replaceAll(/[^a-zA-Z0-9_-]/g, "") || "selector"}`;
  const normalizedSearch = searchValue.trim().toLowerCase();
  const visibleRows = useMemo(() => {
    return rows.filter((row) => {
      if (activeFilter !== "all" && !(row.filterKeys || []).includes(activeFilter)) {
        return false;
      }
      if (!normalizedSearch) {
        return true;
      }
      return firstStringFromUnknownValue([row.searchText, `${row.title} ${row.subtitle || ""}`])
        .toLowerCase()
        .includes(normalizedSearch);
    });
  }, [activeFilter, normalizedSearch, rows]);
  const visibleSelectableRows = visibleRows.filter((row) => !row.disabled && row.id);
  const visibleSelectedRows = visibleSelectableRows.filter((row) => row.selected);
  const showingOnlyDisabledRows = visibleRows.length > 0 && visibleSelectableRows.length === 0;

  const selectedRowIdSet = useMemo(
    () => new Set(visibleSelectedRows.map((row) => row.id)),
    [visibleSelectedRows],
  );

  /** The table reports the whole next selection; this owner toggles one row. */
  function applySelection(next: ReadonlySet<string>) {
    visibleSelectableRows.forEach((row) => {
      if (next.has(row.id) !== Boolean(row.selected)) {
        onToggleRow(row.id);
      }
    });
  }

  const columns = useMemo<Array<DsTableColumn<AssignmentSelectorRow>>>(() => {
    const base: Array<DsTableColumn<AssignmentSelectorRow>> = [
      {
        key: "item",
        header: t("react.shared.item", "Item"),
        cell: assignmentItemCell,
      },
      {
        key: "status",
        header: t("account_settings.card.status", "Status"),
        cell: assignmentStatusCell,
      },
    ];
    if (renderRowAction) {
      base.push({
        key: "actions",
        header: t("registry.agent_detail.changelog.action", "Action"),
        ariaLabel: t("registry.agent_detail.changelog.action", "Action"),
        cell: (row) => renderRowAction(row),
      });
    }
    return base;
  }, [renderRowAction, t]);

  return (
    <div className="fs-assignment-selector space-y-4">
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto]">
        <div className="space-y-3">
          {/* The icon and the placeholder carry the meaning; a second uppercase
              "Search" here would compete with the section label above it. */}
          <label className="block">
            <span className="fs-sr-only">{t("events.actions.search", "Search")}</span>
            <span className="fs-input-shell">
              <Search className="fs-input-icon" aria-hidden="true" />
              <input
                id={`${controlIdPrefix}_search`}
                inputMode="search"
                name={`${normalizedInputNamePrefix}_search`}
                onChange={(event) => onSearchChange(event.target.value)}
                placeholder={searchPlaceholder}
                role="searchbox"
                type="text"
                value={searchValue}
              />
              {searchValue ? (
                <button
                  aria-label={t("react.shared.clear_search", "Clear search")}
                  className="fs-input-clear"
                  onClick={() => onSearchChange("")}
                  title={t("react.shared.clear_search", "Clear search")}
                  type="button"
                >
                  <X aria-hidden="true" />
                </button>
              ) : null}
            </span>
          </label>
          {filters.length ? (
            <div className="flex flex-wrap items-end gap-2" role="group" aria-label={t("react.shared.assignment_filters", "Assignment filters")}>
              {filters.map((filter) => (
                <button
                  aria-pressed={activeFilter === filter.key}
                  className={`fs-filter-chip${activeFilter === filter.key ? " selected" : ""}`}
                  key={filter.key}
                  onClick={() => onFilterChange?.(filter.key)}
                  type="button"
                >
                  {filter.label} <span className="fs-ink-tertiary">{filter.count}</span>
                </button>
              ))}
            </div>
          ) : null}
        </div>
        <div className="flex min-h-8 flex-wrap items-center justify-end gap-2 self-start">
          <span className="fs-filter-chip static selected">
            {selectedCount} {selectedDisplayLabel}
          </span>
          <span className="fs-filter-chip static">
            {totalCount} {totalDisplayLabel}
          </span>
          {!readOnly && saveLabel && onSave ? (
            <button
              className="fs-button primary"
              disabled={saving || saveDisabled}
              onClick={() => onSave()}
              type="button"
            >
              {saving ? t("project_edit.security.saving", "Saving...") : saveLabel}
            </button>
          ) : null}
        </div>
      </div>

      {/* Select-all lives in the table's own header checkbox, per the table
          contract; only the result count belongs out here. */}
      <div className="flex flex-wrap items-center gap-2 text-xs fs-ink-tertiary">
        {normalizedSearch || activeFilter !== "all"
          ? t("react.shared.assignment_rows_shown_filtered", "{count} shown of {total}", { count: visibleRows.length, total: rows.length })
          : t("react.shared.assignment_rows_shown", "{count} shown", { count: visibleRows.length })}
      </div>

      {/* A blocked selection is a warning, not an absence, so it stays an alert. */}
      {showingOnlyDisabledRows ? (
        <DsAlert tone="warning" title={t("react.shared.nothing_here_can_be_selected", "Nothing here can be selected")}>
          {t("react.shared.the_rows_shown_by_the_current_search_or_filter_are_disabled", "The rows shown by the current search or filter are disabled.")} </DsAlert>
      ) : null}

      {loading ? <p className="fs-empty-copy">{loadingLabel}</p> : null}

      {/* emptyState and message are arbitrary nodes — callers pass block markup,
          so these wrappers cannot be <p>. */}
      {!loading && !rows.length ? <div className="fs-empty-copy">{emptyState}</div> : null}

      {!loading && rows.length && !visibleRows.length ? (
        <p className="fs-empty-copy">{t("react.shared.no_results_match_the_current_search_or_filter", "No results match the current search or filter.")}</p>
      ) : null}

      {visibleRows.length ? (
        <div className="fs-surface-radius overflow-hidden border border-subtle">
          <DsTable
            ariaLabel={t("react.shared.value1_items", "{value1} items", { value1: selectedDisplayLabel })}
            columns={columns}
            embedded
            maxHeight="24rem"
            onSelectionChange={readOnly ? undefined : applySelection}
            rowKey={(row) => row.id}
            rowSelectable={(row) => !row.disabled && Boolean(row.id)}
            rows={visibleRows}
            selectable={!readOnly}
            selectedKeys={selectedRowIdSet}
            selectionLabel={(row) => t("react.shared.toggle_row", "Toggle {title}", { title: row.title })}
          />
        </div>
      ) : null}

      {message ? <div className="fs-empty-copy">{message}</div> : null}
    </div>
  );
}
