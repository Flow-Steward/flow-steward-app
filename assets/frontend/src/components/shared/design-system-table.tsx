import { useI18n } from "@/lib/i18n/useI18n";
import { ChevronDown, ChevronLeft, ChevronRight, X } from "lucide-react";
import { useEffect, useMemo, useRef, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { DsCheckboxMark } from "./design-system-primitives";
import { DsSelect } from "./design-system-selects";

/**
 * The page numbers a numbered pager shows: all of them up to seven, otherwise
 * the first, the last and the current one with its neighbours, with a gap
 * marker wherever numbers are skipped.
 */
function paginationItems(page: number, count: number): Array<number | `gap-before-${number}`> {
  if (count <= 7) return Array.from({ length: count }, (_, index) => index + 1);
  const pages = [...new Set([1, count, page - 1, page, page + 1])]
    .filter((value) => value >= 1 && value <= count)
    .sort((a, b) => a - b);
  const items: Array<number | `gap-before-${number}`> = [];
  let previous = 0;
  for (const value of pages) {
    // A gap is named by the page it precedes, which is what makes it unique.
    if (value - previous > 1) items.push(`gap-before-${value}`);
    items.push(value);
    previous = value;
  }
  return items;
}

export function Pagination({
  page,
  perPage,
  total,
  onPageChange,
  onPerPageChange,
  perPageOptions = [5, 10, 20, 50],
  showPerPage = true,
  numbered = false,
}: Readonly<{
  page: number;
  perPage: number;
  total: number;
  onPageChange: (page: number) => void;
  onPerPageChange?: (perPage: number) => void;
  /** Inventories carry heavier steps than settings tables. */
  perPageOptions?: readonly number[];
  /** Hide the size control when the endpoint pages at a fixed size. */
  showPerPage?: boolean;
  /**
   * Numbered page buttons between the arrows, for a surface a person returns
   * to by position (a catalog), where "page 3" is worth showing.
   */
  numbered?: boolean;
}>) {
  const { t } = useI18n();
  const start = total ? (page - 1) * perPage + 1 : 0;
  const end = Math.min(page * perPage, total);
  const totalPages = Math.max(1, Math.ceil(total / perPage));
  const numbers = numbered
    ? paginationItems(page, totalPages).map((item) =>
        typeof item === "string" ? (
          <span aria-hidden="true" className="fs-pagination-gap" key={item}>…</span>
        ) : (
          <button
            aria-current={item === page ? "page" : undefined}
            aria-label={t("react.shared.page_value1", "Page {value1}", { value1: String(item) })}
            className="fs-pagination-button"
            key={item}
            onClick={() => item !== page && onPageChange(item)}
            type="button"
          >
            {item}
          </button>
        ),
      )
    : null;
  return (
    <footer className="fs-table-footer">
      <div className="fs-pagination">
        {showPerPage && onPerPageChange ? (
          <>
            <span>{t("react.shared.rows_per_page", "Rows per page")}</span>
            <DsSelect ariaLabel={t("react.shared.rows_per_page", "Rows per page")} className="fs-page-size" onChange={(value) => onPerPageChange(Number(value))} options={perPageOptions.map((value) => ({ value: String(value), label: String(value) }))} value={String(perPage)} />
          </>
        ) : null}
        <span>{t("react.shared.pagination_range", "{start}–{end} of {total}", { start, end, total })}</span>
        <button aria-label={t("react.shared.previous_page", "Previous page")} className="fs-pagination-button" disabled={page <= 1} onClick={() => onPageChange(page - 1)} type="button"><ChevronLeft aria-hidden="true" /></button>
        {numbers}
        <button aria-label={t("react.shared.next_page", "Next page")} className="fs-pagination-button" disabled={page >= totalPages} onClick={() => onPageChange(page + 1)} type="button"><ChevronRight aria-hidden="true" /></button>
      </div>
    </footer>
  );
}

export type DsTableSort = { key: string; direction: "asc" | "desc" };

export type DsTableColumn<Row> = {
  key: string;
  header: ReactNode;
  /** Omit for the actions column; pass an aria-label instead. */
  ariaLabel?: string;
  width?: number;
  /**
   * Numeric columns only. Emits the published `data-numeric` hook on the header
   * and the cell, which carries right alignment and tabular figures together.
   */
  align?: "left" | "right";
  sortable?: boolean;
  /**
   * Clamp the cell to this CSS width with an ellipsis. The untruncated value
   * must still be reachable, so pass `tooltip` alongside it.
   */
  truncate?: string;
  /** Sets the cell's title attribute — the bridge called this tooltip_key. */
  tooltip?: (row: Row) => string | undefined;
  cell: (row: Row) => ReactNode;
};

/**
 * The table shell for the Advanced data table composition. Filters, pagination
 * and row menus are separate exports here; this owns only the semantic table,
 * its sort affordance and the loading / empty / error block beneath it.
 */
export function DsTable<Row>({
  ariaLabel,
  columns,
  rows,
  rowKey,
  sort,
  onSortChange,
  busy = false,
  state = null,
  onRowClick,
  selectable = false,
  selectedKeys,
  onSelectionChange,
  embedded = false,
  maxHeight,
  selectionLabel,
  rowSelectable,
  rowReplacement,
  rowClickFocusable = true,
}: Readonly<{
  ariaLabel: string;
  columns: ReadonlyArray<DsTableColumn<Row>>;
  rows: readonly Row[];
  rowKey: (row: Row) => string;
  sort?: DsTableSort | null;
  onSortChange?: (sort: DsTableSort) => void;
  busy?: boolean;
  state?: ReactNode;
  onRowClick?: (row: Row) => void;
  /** Only for surfaces that already offer bulk actions. */
  selectable?: boolean;
  selectedKeys?: ReadonlySet<string>;
  onSelectionChange?: (keys: Set<string>) => void;
  /** Inside a dialog or panel rather than at page width. */
  embedded?: boolean;
  /** Scroll the body past this height, e.g. a long picker inside a dialog. */
  maxHeight?: string;
  /** Accessible name for a row's selection checkbox; defaults to its key. */
  selectionLabel?: (row: Row) => string;
  /** Rows this table refuses to select; select-all skips them too. */
  rowSelectable?: (row: Row) => boolean;
  /**
   * Content that replaces a row in place, spanning every column — the canon's
   * Inline confirm strip for a row action. Return null to render the row.
   */
  rowReplacement?: (row: Row) => ReactNode;
  /**
   * False when a cell already holds the row's own link: a pointer may still
   * click anywhere else on the row, but the keyboard meets the link once, not
   * the row and then the link, and a click on the link stays the link's.
   */
  rowClickFocusable?: boolean;
}>) {
  const { t } = useI18n();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const selected = selectedKeys ?? new Set<string>();
  const visibleKeys = rows.filter((row) => rowSelectable?.(row) ?? true).map((row) => rowKey(row));
  const allSelected = visibleKeys.length > 0 && visibleKeys.every((key) => selected.has(key));
  const someSelected = visibleKeys.some((key) => selected.has(key));

  function toggleAllVisible() {
    const next = new Set(selected);
    if (allSelected) {
      for (const key of visibleKeys) next.delete(key);
    } else {
      for (const key of visibleKeys) next.add(key);
    }
    onSelectionChange?.(next);
  }

  function toggleOne(key: string) {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onSelectionChange?.(next);
  }
  function nextDirection(key: string): "asc" | "desc" {
    return sort?.key === key && sort.direction === "asc" ? "desc" : "asc";
  }

  // The frozen column's divider is structure and holds at every scroll position;
  // its overlap shadow is state, and this is what carries that state. Without an
  // observer the flag never changes and the state rule is dead, which is the
  // defect the published boundary was written against. Re-evaluated on resize
  // and at startup too, because a table can move in or out of overflow without
  // a scroll event ever firing.
  useEffect(() => {
    const node = scrollRef.current;
    if (!node) {
      return undefined;
    }
    const sync = () => {
      node.dataset.scrolled = node.scrollLeft > 0 ? "true" : "false";
    };
    sync();
    node.addEventListener("scroll", sync, { passive: true });
    window.addEventListener("resize", sync);
    return () => {
      node.removeEventListener("scroll", sync);
      window.removeEventListener("resize", sync);
    };
  }, [rows, columns]);

  return (
    <>
      <div className="fs-table-scroll" ref={scrollRef} style={maxHeight ? { maxHeight, overflowY: "auto" } : undefined}>
        <table
          aria-busy={busy}
          aria-label={ariaLabel}
          className={embedded ? "fs-table embedded" : "fs-table"}
        >
          <thead>
            <tr>
              {selectable ? (
                <th className="fs-table-select-cell" scope="col">
                  <label className="fs-checkbox-label bare">
                    <input
                      aria-label={t("react.shared.select_visible_rows", "Select visible rows")}
                      checked={allSelected}
                      disabled={!visibleKeys.length}
                      onChange={toggleAllVisible}
                      ref={(node) => {
                        if (node) node.indeterminate = !allSelected && someSelected;
                      }}
                      type="checkbox"
                    />
                    <DsCheckboxMark />
                  </label>
                </th>
              ) : null}
              {columns.map((column) => {
                const sorted = sort?.key === column.key;
                const sortDirection = sort?.direction === "asc" ? "ascending" : "descending";
                const ariaSort = sorted ? sortDirection : undefined;
                return (
                  <th
                    aria-label={column.ariaLabel === "Actions" ? t("monitoring.table.actions", "Actions") : column.ariaLabel === "Action" ? t("registry.agent_detail.changelog.action", "Action") : column.ariaLabel}
                    aria-sort={column.sortable ? (ariaSort ?? "none") : undefined}
                    data-numeric={column.align === "right" ? true : undefined}
                    key={column.key}
                    scope="col"
                    style={column.width ? { width: `${column.width}px` } : undefined}
                  >
                    {column.sortable && onSortChange ? (
                      <button
                        className="fs-table-sort"
                        data-direction={sorted ? sort?.direction : undefined}
                        onClick={() => onSortChange({ key: column.key, direction: nextDirection(column.key) })}
                        type="button"
                      >
                        {column.header}
                        <ChevronDown aria-hidden="true" />
                      </button>
                    ) : (
                      column.header
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const replacement = rowReplacement?.(row);
              if (replacement) {
                return (
                  <tr className="fs-table-confirm-row" key={rowKey(row)}>
                    <td colSpan={columns.length + (selectable ? 1 : 0)}>{replacement}</td>
                  </tr>
                );
              }
              return (
              <tr
                className={selectable && selected.has(rowKey(row)) ? "is-selected" : undefined}
                // Marks the row as its own click target so the cursor can say so.
                data-clickable={onRowClick ? true : undefined}
                key={rowKey(row)}
                {...(onRowClick && rowClickFocusable
                  ? {
                      onClick: () => onRowClick(row),
                      onKeyDown: (event: ReactKeyboardEvent<HTMLTableRowElement>) => {
                        if (event.key === "Enter") onRowClick(row);
                      },
                      tabIndex: 0,
                    }
                  : {})}
                {...(onRowClick && !rowClickFocusable
                  ? {
                      // A click on the row's own link is the link's: it may be
                      // asking for a new tab.
                      onClick: (event: ReactMouseEvent<HTMLTableRowElement>) => {
                        if ((event.target as Element).closest("a, button")) return;
                        onRowClick(row);
                      },
                    }
                  : {})}
              >
                {selectable ? (
                  <td className="fs-table-select-cell" onClick={(event) => event.stopPropagation()}>
                    <label className="fs-checkbox-label bare">
                      <input
                        aria-label={selectionLabel?.(row) ?? t("react.shared.select_value1", "Select {value1}", { value1: String(rowKey(row)) })}
                        checked={selected.has(rowKey(row))}
                        disabled={!(rowSelectable?.(row) ?? true)}
                        onChange={() => toggleOne(rowKey(row))}
                        type="checkbox"
                      />
                      <DsCheckboxMark />
                    </label>
                  </td>
                ) : null}
                {columns.map((column) => {
                  const content = column.cell(row);
                  return (
                    <td
                      className={column.key === "actions" ? "fs-table-action-cell" : undefined}
                      data-numeric={column.align === "right" ? true : undefined}
                      key={column.key}
                      // Row actions must not also trigger the row's own click, or
                      // opening the menu navigates away from the row.
                      onClick={column.key === "actions" ? (event) => event.stopPropagation() : undefined}
                      title={column.tooltip?.(row)}
                    >
                      {column.truncate ? (
                        <span
                          className="fs-table-truncate"
                          style={{ "--fs-table-truncate-width": column.truncate } as CSSProperties}
                        >
                          {content}
                        </span>
                      ) : (
                        content
                      )}
                    </td>
                  );
                })}
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {state}
    </>
  );
}

export type DsBulkAction = { id: string; label: string; variant?: "default" | "danger" };

/**
 * Every bulk bar on the page shares one dock, so a page with two collections -
 * worker subprocesses under jobs on the monitoring page - stacks its bars in a
 * column instead of landing them on top of each other.
 */
function bulkActionBarDock(): HTMLElement {
  const existing = document.querySelector(".fs-bulk-action-bar-dock");
  if (existing instanceof HTMLElement) {
    return existing;
  }
  const dock = document.createElement("div");
  dock.className = "fs-bulk-action-bar-dock";
  document.body.appendChild(dock);
  return dock;
}

/**
 * How much room the page owes the dock. Counted rather than flagged, because a
 * second live selection adds a second bar and the reservation has to grow with
 * it or the floating surface covers the last row after all.
 */
let liveBulkActionBars = 0;

function countLiveBulkActionBar(delta: number) {
  liveBulkActionBars = Math.max(0, liveBulkActionBars + delta);
  if (liveBulkActionBars > 0) {
    document.body.dataset.bulkActionBar = String(liveBulkActionBars);
  } else {
    delete document.body.dataset.bulkActionBar;
  }
}

/**
 * Contextual action surface for a current multi-selection.
 *
 * Floating is the canonical presentation: a bar in document flow scrolls out of
 * reach exactly when a long selection needs it, and it reflows the rows it acts
 * on. It is portalled to the body and fixed to the viewport, so a call site
 * renders it wherever it likes and the placement no longer depends on where.
 *
 * Per the published contract the selection survives until the action succeeds or
 * the operator clears it, an invoked action disables the bar rather than hiding
 * it, and Escape clears the selection. The bar owns no action paint: neutral
 * actions are Secondary Buttons and a destructive one takes the quiet Ghost
 * danger lane, because among peer actions a filled Danger claims the primary
 * path of the surface. The confirmation the destructive action opens still owns
 * the final filled Danger Button.
 */
export function BulkActionBar({
  count,
  actions,
  onAction,
  onClear,
  busy = false,
  confirmation,
}: Readonly<{
  count: number;
  actions: readonly DsBulkAction[];
  onAction: (id: string) => void;
  onClear: () => void;
  busy?: boolean;
  /**
   * An Inline confirm that replaces the action buttons in place. The count
   * stays, because it is the scope the user is agreeing to.
   */
  confirmation?: ReactNode;
}>) {
  const { t } = useI18n();
  const visible = count > 0;
  const confirming = Boolean(confirmation);
  const dock = useMemo(() => bulkActionBarDock(), []);

  // The bar stays mounted while it is hidden so the exit motion has something to
  // run on; `aria-hidden` keeps the actions out of the accessibility tree in the
  // meantime, which is also why an empty selection still reads as no region.
  useEffect(() => {
    if (!visible) {
      return undefined;
    }
    countLiveBulkActionBar(1);
    const onKeyDown = (event: KeyboardEvent) => {
      // While a confirmation is open, Escape belongs to it, not to the selection.
      if (event.key === "Escape" && !confirming) {
        onClear();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      countLiveBulkActionBar(-1);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [visible, onClear, confirming]);

  return createPortal(
    <section
      aria-busy={busy}
      aria-hidden={!visible}
      aria-label={t("react.shared.bulk_actions", "Bulk actions")}
      className="fs-bulk-action-bar"
      data-state={visible ? "visible" : "hidden"}
    >
      <span aria-live="polite" className="fs-bulk-action-count">{t("react.shared.selected_count", "{count} selected", { count })}</span>
      <span aria-hidden="true" className="fs-bulk-action-divider" />
      {confirming ? (
        confirmation
      ) : (
        <>
          <div className="fs-bulk-actions">
            {actions.map((action) => (
              <button
                className={`fs-button ${action.variant === "danger" ? "ghost-danger" : "secondary"}`}
                data-bulk-action={action.id}
                disabled={busy}
                key={action.id}
                onClick={() => onAction(action.id)}
                type="button"
              >
                {action.label}
              </button>
            ))}
          </div>
          <span aria-hidden="true" className="fs-bulk-action-divider" />
          <button
            aria-label={t("react.shared.clear_selection", "Clear selection")}
            className="fs-icon-button fs-bulk-action-clear"
            disabled={busy}
            onClick={onClear}
            type="button"
          >
            <X aria-hidden="true" />
          </button>
        </>
      )}
    </section>,
    dock,
  );
}
